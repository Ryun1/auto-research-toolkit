"""Native dispatch: the loop hands work to the surrounding harness's own
subagents, and no model runs in this process.

The failure this guards against is the one qsb filed: `bin/ar loop` selecting
the SDK brain meant the unattended path reached for API money the operator
never approved. Here dispatch reserves a bounded external assignment per card
-- the same claim, run ceiling and workspace a core worker would get -- and
the iteration stops at the handoff, naming the verb that resumes it.
"""
import json
import pathlib

import pytest

from autoresearch import attempts, external
from autoresearch.driver.brain import ScriptedBrain
from autoresearch.driver.loop import Coordinator
from autoresearch.runs import RunRecord, append
from conftest import make_entry


@pytest.fixture
def coordinator(sandbox, store):
    sandbox.dispatch = "native"
    make_entry(store, "Q1")
    return Coordinator(sandbox, ScriptedBrain({}))


def test_native_dispatch_assigns_and_stops_without_a_model_in_process(
        coordinator, store):
    it = coordinator.run_iteration(1)
    assert it.verdicts == {"Q1": "assigned"}
    assert it.stop == "native-handoff"
    names = [p.name for p in it.phases]
    assert "dispatch" in names
    for name in ("curate", "distil", "qc"):
        phase = next(p for p in it.phases if p.name == name)
        assert phase.did == 0 and phase.detail, \
            f"{name} must record why it did not run"
    # The work itself is an external assignment: bounded, workspace-backed.
    row = attempts.load(coordinator.config, it.attempt_ids[0])
    assert row["kind"] == "external" and row["entry"] == "Q1"
    assert (row["ceiling"]["runs"]) >= 1
    assert store.load("Q1").claim is not None


def test_the_handoff_manifest_is_settled_by_external_complete(coordinator, store):
    it = coordinator.run_iteration(1)
    identity = it.attempt_ids[0]
    workspace = attempts.load(coordinator.config, identity)["workspace"]
    append(pathlib.Path(workspace) / "data/runs/owner.jsonl", RunRecord(
        id="native-run", entry="Q1", session="coordinator",
        metrics={"ops": 30, "peak": 2}, provenance={"source": "worker"}))
    report = {"runs": 1, "verdict": "confirmed",
              "memo": "inbox/native.md",
              "summary": "the pre-registered bar was met",
              "verification": {"reread": True,
                               "claims_checked": ["the measurement was retained"]}}
    (pathlib.Path(workspace) / "inbox/native.md").write_text("the bar was met")
    completed = external.complete(coordinator.config, identity, "coordinator", report)
    assert completed["status"] == "completed"
    assert store.load("Q1").result.verdict == "confirmed"


def test_the_next_native_iteration_curates_what_complete_returned(
        coordinator, store):
    it = coordinator.run_iteration(1)
    identity = it.attempt_ids[0]
    workspace = attempts.load(coordinator.config, identity)["workspace"]
    append(pathlib.Path(workspace) / "data/runs/owner.jsonl", RunRecord(
        id="native-run", entry="Q1", session="coordinator",
        metrics={"ops": 30, "peak": 2}, provenance={"source": "worker"}))
    (pathlib.Path(workspace) / "inbox/native.md").write_text("the bar was met")
    external.complete(coordinator.config, identity, "coordinator", {
        "runs": 1, "verdict": "confirmed", "memo": "inbox/native.md",
        "summary": "the pre-registered bar was met",
        "verification": {"reread": True,
                         "claims_checked": ["the measurement was retained"]}})
    # Q1 is now terminal, so the shortlist is empty and the loop's ordinary
    # closeout phases -- not a handoff -- are what the second iteration runs.
    it2 = coordinator.run_iteration(2)
    assert it2.stop != "native-handoff"
    for name in ("curate", "distil", "qc"):
        phase = next(p for p in it2.phases if p.name == name)
        assert not any("native dispatch" in line for line in phase.detail), \
            f"{name} must run once nothing is outstanding"


def test_a_settled_verdict_reaches_the_dispatching_iteration_record(
        coordinator, store):
    """The yield floor reads only iteration records; a handoff iteration
    otherwise stays confirmed=0 forever, and a native loop that is repaying
    its machine time gets stopped for yielding nothing."""
    it = coordinator.run_iteration(1)
    identity = it.attempt_ids[0]
    workspace = attempts.load(coordinator.config, identity)["workspace"]
    append(pathlib.Path(workspace) / "data/runs/coordinator.jsonl", RunRecord(
        id="native-run", entry="Q1", session="coordinator",
        metrics={"ops": 30, "peak": 2}, provenance={"source": "worker"}))
    (pathlib.Path(workspace) / "inbox/native.md").write_text("the bar was met")
    external.complete(coordinator.config, identity, "coordinator", {
        "runs": 1, "verdict": "confirmed", "memo": "inbox/native.md",
        "summary": "the pre-registered bar was met",
        "verification": {"reread": True,
                         "claims_checked": ["the measurement was retained"]}})
    record = json.loads((coordinator.config.paths.iterations / "0001.json").read_text())
    assert record["verdicts"]["Q1"] == "confirmed"


def test_the_iteration_runs_meter_caps_native_dispatch(sandbox, store):
    """Worker dispatch charges the iteration's runs meter; native dispatch
    must not bypass it. One card assigns; the next is refused by the meter,
    whichever order the rank puts them in."""
    from autoresearch.driver.brain import ScriptedBrain
    from autoresearch.driver.loop import Coordinator as C
    sandbox.dispatch = "native"
    sandbox.budgets["iteration_max_runs"] = 1
    make_entry(store, "Q1")
    make_entry(store, "Q2", impact=0.2)
    coordinator = C(sandbox, ScriptedBrain({}))
    it = coordinator.run_iteration(1)
    assert list(it.verdicts.values()) == ["assigned"]
    phase = next(p for p in it.phases if p.name == "dispatch")
    assert any("exhausted" in line for line in phase.detail), phase.detail
    assert len(it.attempt_ids) == 1


def test_a_blocked_card_never_becomes_an_assignment(coordinator, store, monkeypatch):
    from autoresearch import preflight
    monkeypatch.setattr(preflight, "blocked_reason",
                        lambda config, entry: "measure command is human-only")
    it = coordinator.run_iteration(1)
    assert it.verdicts == {"Q1": "blocked"}
    assert it.attempt_ids == []


GOOD_REPORT = {"runs": 1, "verdict": "confirmed", "memo": "inbox/native.md",
               "summary": "the pre-registered bar was met",
               "verification": {"reread": True,
                                "claims_checked": ["the measurement was retained"]}}


def _assigned_assignment(coordinator):
    """One dispatched external assignment with one retained run record."""
    it = coordinator.run_iteration(1)
    identity = it.attempt_ids[0]
    workspace = pathlib.Path(attempts.load(coordinator.config, identity)["workspace"])
    append(pathlib.Path(workspace) / "data/runs/owner.jsonl", RunRecord(
        id="native-run", entry="Q1", session="coordinator",
        metrics={"ops": 30, "peak": 2}, provenance={"source": "worker"}))
    return identity, workspace


def test_a_rejected_completion_leaves_the_assignment_actionable(coordinator, store):
    """A completion whose memo path fails the findings-lane check must refuse
    without persisting the `completing` state. Persisting it first wedged the
    assignment permanently: the bad report was saved, a corrected report was
    then refused as "a different report", and the original re-failed forever
    on the same retention check. A refusal must leave the row assigned and
    report-free, so the corrected report simply re-runs."""
    from autoresearch.errors import AutoresearchError
    identity, workspace = _assigned_assignment(coordinator)
    bad = dict(GOOD_REPORT, memo="findings.md")     # not in the findings lane
    (workspace / "findings.md").write_text("wrong lane")
    with pytest.raises(AutoresearchError):
        external.complete(coordinator.config, identity, "coordinator", bad)
    row = attempts.load(coordinator.config, identity)
    assert row["status"] == "assigned", row["status"]
    assert row.get("report") is None
    (workspace / "inbox/native.md").write_text("the bar was met")
    completed = external.complete(coordinator.config, identity, "coordinator", GOOD_REPORT)
    assert completed["status"] == "completed"
    assert store.load("Q1").result.verdict == "confirmed"


def test_an_assignment_stuck_in_completing_is_recoverable(coordinator, store):
    """A row that did reach `completing` (a crash between retention and
    settlement) has a documented recovery: cancel refuses only terminal rows,
    so a completing row is cancellable; and the prepared report is still
    protected -- a different report is refused until the row is freed."""
    from autoresearch.errors import AutoresearchError
    identity, workspace = _assigned_assignment(coordinator)
    (pathlib.Path(workspace) / "inbox/native.md").write_text("the bar was met")
    stuck = attempts.load(coordinator.config, identity)
    stuck.update(status="completing", report=dict(GOOD_REPORT))
    attempts.save(coordinator.config, stuck)
    with pytest.raises(AutoresearchError, match="different report"):
        external.complete(coordinator.config, identity, "coordinator",
                          dict(GOOD_REPORT, summary="a different report"))
    external.cancel(coordinator.config, identity, "coordinator",
                    "stuck in completing; freeing for re-assignment",
                    confirm_inactive=True)
    assert attempts.load(coordinator.config, identity)["status"] == "cancelled"
    assert store.load("Q1").claim is None


REFUTED_REPORT = dict(GOOD_REPORT, verdict="refuted",
                      summary="refuted against the registered bar",
                      closure_kind="mechanism")


def test_a_refused_close_keeps_the_assignment_actionable(coordinator, store):
    """H9, the live Q19 repro: a close-gate refusal (refuted without a
    closure kind) applied no verdict, so it must write no external-complete
    receipt and must not settle the row. A refused close used to settle
    completed/refused behind a verdict-shaped receipt, and every later
    complete() then settled refused without applying the corrected report."""
    from autoresearch.entries import Store
    identity, workspace = _assigned_assignment(coordinator)
    (pathlib.Path(workspace) / "inbox/native.md").write_text("the bar was met")
    bad = dict(REFUTED_REPORT)
    bad.pop("closure_kind")
    refused = external.complete(coordinator.config, identity, "coordinator", bad)
    assert refused["status"] == "assigned", refused["status"]
    assert refused.get("report") is None
    assert any("close refused" in line for line in refused["detail"])
    entry = store.load("Q1")
    assert entry.claim is not None and entry.claim.session == "coordinator"
    assert external._receipt(entry, identity, "external-complete") is None
    completed = external.complete(coordinator.config, identity, "coordinator",
                                  REFUTED_REPORT)
    assert completed["status"] == "completed"
    assert completed["verdict"] == "refuted"
    entry = Store(coordinator.config.paths.entries).load("Q1")
    assert entry.status == "refuted"
    assert external._receipt(entry, identity,
                             "external-complete")["verdict"] == "refuted"


def test_a_missing_verification_block_keeps_the_assignment_actionable(
        coordinator, store):
    """The no-verification refusal applied no verdict either: the claim stays
    with the external worker and a corrected report re-runs the command."""
    identity, workspace = _assigned_assignment(coordinator)
    (pathlib.Path(workspace) / "inbox/native.md").write_text("the bar was met")
    bad = dict(GOOD_REPORT)
    bad.pop("verification")
    refused = external.complete(coordinator.config, identity, "coordinator", bad)
    assert refused["status"] == "assigned", refused["status"]
    entry = store.load("Q1")
    assert entry.claim is not None and entry.claim.session == "coordinator"
    assert external._receipt(entry, identity, "external-complete") is None
    completed = external.complete(coordinator.config, identity, "coordinator",
                                  GOOD_REPORT)
    assert completed["status"] == "completed"
    assert store.load("Q1").result.verdict == "confirmed"


def test_an_unknown_verdict_is_refused_not_settled(coordinator, store):
    """H9 hardening: the settled-verdict path takes only the output
    contract's vocabulary. A report with an out-of-contract verdict used to
    ride the non-terminal release with a receipt carrying the junk string --
    exactly the shape the H9 poison check keys on."""
    identity, workspace = _assigned_assignment(coordinator)
    (pathlib.Path(workspace) / "inbox/native.md").write_text("the bar was met")
    refused = external.complete(coordinator.config, identity, "coordinator",
                                dict(GOOD_REPORT, verdict="refused"))
    assert refused["status"] == "assigned", refused["status"]
    assert any("not a verdict" in line for line in refused["detail"])
    entry = store.load("Q1")
    assert entry.claim is not None and entry.claim.session == "coordinator"
    assert external._receipt(entry, identity, "external-complete") is None
    completed = external.complete(coordinator.config, identity, "coordinator",
                                  GOOD_REPORT)
    assert completed["status"] == "completed"
    assert store.load("Q1").result.verdict == "confirmed"


def test_a_legacy_refused_receipt_never_poisons_a_retry(coordinator, store):
    """Rows poisoned before the fix (settled completed/refused behind a
    refused receipt) must recover through the protocol: a retry refuses
    honestly on the released claim instead of silently settling refused, and
    cancel -- blocked before by the same receipt -- is a path out."""
    from autoresearch.entries import Event
    identity, workspace = _assigned_assignment(coordinator)
    (pathlib.Path(workspace) / "inbox/native.md").write_text("the bar was met")
    bad = dict(REFUTED_REPORT)
    bad.pop("closure_kind")
    external.complete(coordinator.config, identity, "coordinator", bad)
    # Hand-craft the pre-fix poison: settled refused + verdict-shaped receipt
    # + released claim, exactly what the old refusal path left on disk.
    attempts.settle(coordinator.config, identity, "coordinator", "completed",
                    verdict="refused")
    entry = store.load("Q1")
    entry.history.append(Event(attempts.now(), "external-complete", "coordinator",
                               json.dumps({"assignment": identity,
                                           "verdict": "refused"})))
    store.save(entry)
    attempts.claims(coordinator.config, "coordinator").release(
        "Q1", why="legacy refusal released the claim")
    from autoresearch.errors import AutoresearchError
    with pytest.raises(AutoresearchError, match="claim identity changed"):
        external.complete(coordinator.config, identity, "coordinator",
                          REFUTED_REPORT)
    row = attempts.load(coordinator.config, identity)
    assert row["status"] == "assigned"      # re-opened, not settled refused
    external.cancel(coordinator.config, identity, "coordinator",
                    "legacy poison; protocol recovery",
                    confirm_inactive=True)
    assert attempts.load(coordinator.config, identity)["status"] == "cancelled"
