"""Execution receipts and finite campaign limits survive evidence and restarts."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from autoresearch import attempts, budget, external, render
from autoresearch.claims import Claims
from autoresearch.config import DomainConfig
from autoresearch.driver.brain import CostLedger, Reply
from autoresearch.driver.loop import Coordinator, Iteration
from autoresearch.errors import AutoresearchError
from autoresearch.runs import RunRecord, append, read_all
from autoresearch.workspaces import Pool
from conftest import close, make_entry

SOURCE = str(Path(__file__).resolve().parents[1] / "src")


def claim(config, store, entry_id="Q1"):
    make_entry(store, entry_id)
    return Claims(store, config, "owner").claim(entry_id, why="bounded execution")


def record_history(config, **usage):
    config.paths.iterations.mkdir(parents=True, exist_ok=True)
    (config.paths.iterations / "0001.json").write_text(json.dumps({"n": 1, **usage}))


@pytest.mark.parametrize("exitcode,measurements", [(0, 1), (7, 1), (7, 0), (0, 2)])
def test_exec_charges_retained_measurements_once_even_on_failure(
        sandbox, store, exitcode, measurements):
    entry = claim(sandbox, store)
    append(sandbox.paths.runs / "owner.jsonl", RunRecord(
        id="historical", entry="Q1", session="owner", metrics={},
        status="invalid", started=1, provenance={"source": "old"}))
    command = (
        f"import sys; sys.path.insert(0, {SOURCE!r}); "
        "from autoresearch.cli import main; "
        f"[main(['--domain', {str(sandbox.paths.root)!r}, '--session', 'owner', "
        f"'measure', '--entry', 'Q1']) for _ in range({measurements})]; "
        f"sys.exit({exitcode})"
    )
    result = attempts.execute(sandbox, "Q1", "owner", 30,
                              [sys.executable, "-c", command])
    produced = {r.id for r in read_all(sandbox.paths.runs)} - {"historical"}
    assert len(produced) == measurements
    assert result["status"] == ("failed" if exitcode else "completed")
    assert set(result["run_ids"]) == produced
    assert budget.claim_runs(sandbox, entry) == max(1, measurements)
    assert budget.total_runs(sandbox) == 1 + max(1, measurements)


def test_exec_does_not_link_other_entry_or_session_evidence(sandbox, store):
    entry = claim(sandbox, store)
    command = (
        f"import sys; sys.path.insert(0, {SOURCE!r}); "
        "from autoresearch.runs import RunRecord, append; "
        f"append({str(sandbox.paths.runs / 'other.jsonl')!r}, "
        "RunRecord(id='other-entry', entry='Q2', session='owner', metrics={})); "
        f"append({str(sandbox.paths.runs / 'foreign.jsonl')!r}, "
        "RunRecord(id='other-session', entry='Q1', session='foreign', metrics={}))"
    )
    result = attempts.execute(sandbox, "Q1", "owner", 30,
                              [sys.executable, "-c", command])
    assert result["run_ids"] == []
    assert budget.claim_runs(sandbox, entry) == 1
    assert budget.total_runs(sandbox) == 3


@pytest.mark.parametrize("money", [None, 5.0, 6.0], ids=["unknown", "exhausted", "overrun"])
@pytest.mark.parametrize("kind", ["exec", "external"])
def test_finite_money_refuses_new_execution(sandbox, store, tmp_path, money, kind):
    claim(sandbox, store)
    record_history(sandbox, cost_usd=money)
    marker = tmp_path / "launched"
    with pytest.raises(AutoresearchError, match="monetary"):
        if kind == "exec":
            attempts.execute(sandbox, "Q1", "owner", 30, [sys.executable, "-c",
                f"from pathlib import Path; Path({str(marker)!r}).touch()"])
        else:
            external.assign(sandbox, "Q1", "owner", 30, 1)
    assert not marker.exists()
    assert attempts.records(sandbox) == []


class FreshBackend:
    """A restarted backend has no local memory of historical campaign costs."""
    def __init__(self):
        self.ledger = CostLedger(5.0)
        self.calls = []

    def ask(self, role, brief, *, workspace=None):
        self.calls.append(role)
        return Reply(role=role, data={}, cost_usd=0.0)


@pytest.mark.parametrize("money", [None, 5.0], ids=["unknown", "exhausted"])
def test_every_model_phase_refuses_historical_money_with_fresh_backend(sandbox, store, money):
    make_entry(store, "Q1", impact=1.0)
    close(sandbox, store, make_entry(store, "Q2"))
    record_history(sandbox, cost_usd=money)
    backend = FreshBackend()
    coordinator = Coordinator(sandbox, backend)
    iteration = Iteration(n=2)
    allowance = budget.iteration_budget(sandbox)
    coordinator.generate(iteration, allowance)
    coordinator.rank(iteration, allowance)
    coordinator.research(iteration, allowance, "What remains?", count=1)
    with Pool(sandbox, "refused") as pool:
        coordinator.dispatch(iteration, [SimpleNamespace(entry_id="Q1", terms={})],
                             allowance, pool)
    coordinator.curate(iteration, allowance)
    coordinator.distil(iteration, allowance, force=True)
    coordinator.qc(iteration, allowance)
    assert backend.calls == []
    assert store.load("Q1").claim is None
    assert attempts.records(sandbox) == []
    assert render.check_views(sandbox, store.all()) == []


@pytest.mark.parametrize("money", [None, 5.0], ids=["unknown", "exhausted"])
def test_money_refusal_preserves_iteration_closeout(sandbox, store, money):
    make_entry(store, "Q1")
    record_history(sandbox, cost_usd=money)
    backend = FreshBackend()
    coordinator = Coordinator(sandbox, backend)
    iteration = coordinator.run_iteration(2)
    assert backend.calls == []
    assert iteration.stop == budget.BUDGET
    assert render.check_views(sandbox, store.all()) == []
    assert json.loads((sandbox.paths.iterations / "0002.json").read_text())["stop"] == budget.BUDGET
    assert any(phase.name == "qc" for phase in iteration.phases)


@pytest.mark.parametrize("reported,measured", [(0.25, 0.75), (0.75, 0.25)])
def test_external_gpu_usage_survives_restart_without_recharging_dispatch(
        sandbox, store, reported, measured):
    claim(sandbox, store)
    assigned = external.assign(sandbox, "Q1", "owner", 30, 1)
    workspace = Path(assigned["workspace"])
    append(workspace / "data/runs/owner.jsonl", RunRecord(
        id="external-run", entry="Q1", session="owner", metrics={"ops": 30, "peak": 2},
        provenance={"source": "worker"}, cost={"gpu_hours": measured}))
    report = {"runs": 1, "gpu_hours": reported, "verdict": "inconclusive",
              "verification": {"reread": True, "claims_checked": ["measurement retained"]}}
    completed = external.complete(sandbox, assigned["id"], "owner", report)
    assert completed["status"] == "completed"
    external.complete(sandbox, assigned["id"], "owner", report)
    claim(sandbox, store, "Q2")
    dispatch = attempts.reserve(sandbox, "Q2", "owner", 30, kind="dispatch")
    attempts.settle(sandbox, dispatch["id"], "owner", "completed", gpu_hours=2.0)
    record_history(sandbox, cost_usd=0.0, runs=1, gpu_hours=2.0,
                   attempt_ids=[dispatch["id"]])
    restarted_config = DomainConfig.load(sandbox.paths.root)
    restarted = Coordinator(restarted_config, FreshBackend())
    assert budget.recorded_usage(restarted_config)["gpu_hours"] == 2.75
    assert restarted.domain_budget["gpu_hours"].spent == 2.75
    assert restarted.domain_budget["runs"].spent == 2


@pytest.mark.parametrize("disposition", ["superseded", "already-shipped"])
def test_worker_nonexperiment_closure_preserves_scope(sandbox, store, disposition):
    from autoresearch import rank

    make_entry(store, "Q1", mechanisms=["shared-premise"])
    make_entry(store, "Q2", mechanisms=["shared-premise"], impact=1)

    class Worker:
        def ask(self, role, brief, *, workspace=None):
            (workspace / "inbox/result.md").write_text("superseded without testing")
            return Reply(role=role, cost_usd=0.0, data={
                "verdict": "refuted", "memo": "inbox/result.md", "runs": 0,
                "closure_kind": "mechanism", "disposition": disposition,
                "applicability": {"workload": "specific-case"},
                "verification": {"reread": True, "claims_checked": ["not an experiment"]}})

    coordinator = Coordinator(sandbox, Worker())
    iteration = Iteration(n=1)
    with Pool(sandbox, "closure") as pool:
        coordinator.dispatch(iteration, [SimpleNamespace(entry_id="Q1", terms={})],
                             budget.iteration_budget(sandbox), pool)
    assert iteration.verdicts["Q1"] == "refuted"
    result = store.load("Q1").result
    assert result.disposition == disposition
    assert result.applicability.workload == "specific-case"
    assert "Q2" in {card.entry_id for card in rank.rank(store.all(), sandbox).scored}


# -- reconciling a record the meter cannot trust (qsb H27) -------------------

def write_legacy_attempt(sandbox, identity, body=None):
    """A record in a schema older than the core's, like qsb's retained
    qsb-attempt-1 rows: valid JSON, unusable as a meter."""
    directory = attempts.directory(sandbox) / identity
    directory.mkdir(parents=True)
    row = body if body is not None else {
        "schema": "qsb-attempt-1", "id": identity, "status": "completed",
        "kind": "exec", "entry": "Q1", "session": "owner",
        "charged_runs": 3, "run_ids": []}
    (directory / "record.json").write_text(json.dumps(row))
    return directory


def test_one_legacy_record_names_itself_instead_of_bricking_the_ledger(
        sandbox, store):
    claim(sandbox, store)
    identity = "a" * 32
    write_legacy_attempt(sandbox, identity)
    with pytest.raises(AutoresearchError, match="reconcile"):
        attempts.records(sandbox)
    found = attempts.problems(sandbox)
    assert len(found) == 1 and found[0]["id"] == identity
    assert "schema" in found[0]["reason"] or "identity" in found[0]["reason"]


def test_reconcile_archives_the_record_and_asserts_its_spend(sandbox, store):
    claim(sandbox, store)
    identity = "b" * 32
    write_legacy_attempt(sandbox, identity)
    attempts.reconcile(sandbox, identity, "owner", 0,
                       "legacy qsb-attempt-1 row; predates metering, zero spend confirmed")
    quarantined = attempts.directory(sandbox) / identity
    assert not (quarantined / "record.json").exists()
    assert (quarantined / "quarantined" / "record.json").exists()
    tomb = attempts.tombstones(sandbox)
    assert len(tomb) == 1 and tomb[0]["id"] == identity
    assert tomb[0]["charged_runs"] == 0
    # The meter reads again, and the asserted zero spends nothing.
    assert attempts.records(sandbox) == []
    assert budget.recorded_usage(sandbox)["runs"] == 0.0


def test_a_tombstones_asserted_consumption_charges_the_campaign(sandbox, store):
    claim(sandbox, store)
    identity = "c" * 32
    write_legacy_attempt(sandbox, identity)
    attempts.reconcile(sandbox, identity, "owner", 2, "two runs, spent outside the ledger")
    assert budget.recorded_usage(sandbox)["runs"] == 2.0
    assert budget.total_runs(sandbox) >= 2.0


def test_reconcile_refuses_a_valid_record_and_a_mismatched_id(sandbox, store):
    claim(sandbox, store)
    reservation = attempts.reserve(sandbox, "Q1", "owner", 30)
    with pytest.raises(AutoresearchError, match="valid attempt record"):
        attempts.reconcile(sandbox, reservation["id"], "owner", 0, "why")
    with pytest.raises(AutoresearchError, match="nothing to reconcile"):
        attempts.reconcile(sandbox, "d" * 32, "owner", 0, "why")


def test_reserve_admits_the_declared_concurrency_when_the_key_is_missing(
        sandbox, store):
    """A domain.toml that omits `max_parallel` used to meet reserve's private
    default of 1: the second concurrent reservation was refused while
    loop._map ran five workers. Both now share DEFAULT_MAX_PARALLEL."""
    claim(sandbox, store, "Q1")
    claim(sandbox, store, "Q2")
    first = attempts.reserve(sandbox, "Q1", "owner", 30)
    second = attempts.reserve(sandbox, "Q2", "owner", 30)
    assert {r["entry"] for r in attempts.records(sandbox)} == {"Q1", "Q2"}
    assert first["status"] == second["status"] == "reserved"


def test_the_record_cache_never_hides_a_new_row_a_changed_one_or_a_gone_one(
        sandbox, store):
    """The cache is an accelerator, not a second truth: save() refreshes the
    row it wrote, a file changed underneath it has a different (size,
    mtime_ns) signature and is re-read, and a row deleted on disk stops
    existing."""
    claim(sandbox, store)
    reservation = attempts.reserve(sandbox, "Q1", "owner", 30)
    assert attempts.records(sandbox) == [json.loads(json.dumps(reservation))]
    attempts.settle(sandbox, reservation["id"], "owner", "completed")
    assert attempts.records(sandbox)[0]["status"] == "completed"
    path = attempts.directory(sandbox) / reservation["id"] / "record.json"
    row = json.loads(path.read_text())
    row["status"] = "failed"
    path.write_text(json.dumps(row))
    assert attempts.records(sandbox)[0]["status"] == "failed"
    path.unlink()
    assert attempts.records(sandbox) == []


def test_a_tombstone_whose_evidence_moved_refuses_the_meter(sandbox, store):
    claim(sandbox, store)
    identity = "e" * 32
    write_legacy_attempt(sandbox, identity)
    attempts.reconcile(sandbox, identity, "owner", 0, "zero spend")
    quarantined = (attempts.directory(sandbox) / identity / "quarantined" / "record.json")
    quarantined.write_text("{}")
    with pytest.raises(AutoresearchError, match="tombstone"):
        attempts.tombstones(sandbox)
    # And a corrupt tombstone must not be reconcilable away by listing alone:
    with pytest.raises(AutoresearchError, match="tombstone"):
        budget.recorded_usage(sandbox)
