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
from conftest import append_measured, make_entry


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
    append_measured(coordinator.config, pathlib.Path(workspace) / "data/runs/owner.jsonl", RunRecord(
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
    append_measured(coordinator.config, pathlib.Path(workspace) / "data/runs/owner.jsonl", RunRecord(
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
    append_measured(coordinator.config, pathlib.Path(workspace) / "data/runs/coordinator.jsonl", RunRecord(
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
    append_measured(coordinator.config, pathlib.Path(workspace) / "data/runs/owner.jsonl", RunRecord(
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


def _age_the_assignment(config, identity, hours):
    """Rewrite an assignment's creation time, the way a session that died ten
    hours ago leaves it."""
    import datetime as dt
    path = attempts.directory(config) / identity / "record.json"
    saved = json.loads(path.read_text())
    saved["created"] = (dt.datetime.now(dt.UTC)
                        - dt.timedelta(hours=hours)).isoformat()
    path.write_text(json.dumps(saved))


def test_an_expired_assignment_is_reaped_by_another_session(coordinator, store):
    """sig.golf H19: an assignment nobody settles held a concurrency slot with
    no path to clear it -- `external cancel` and `settle` both refuse another
    session's row, and `ar reap` knew only claims. One expired assignment is now
    freeable by anyone, and the row says who took it from whom."""
    from autoresearch.errors import AutoresearchError
    it = coordinator.run_iteration(1)
    identity = it.attempt_ids[0]
    owner = attempts.load(coordinator.config, identity)["session"]
    _age_the_assignment(coordinator.config, identity, 10)
    # the protocol path still refuses: cancelling is the holder's decision
    with pytest.raises(AutoresearchError, match="belongs to another session"):
        external.cancel(coordinator.config, identity, "other", "mine now",
                        confirm_inactive=True)
    reaped = attempts.reap_assignment(coordinator.config, identity, "other",
                                      why="owner silent")
    assert reaped["status"] == "cancelled"
    assert reaped["reaped_by"] == "other" and reaped["reaped_from"] == owner
    assert "owner silent" in reaped["reason"]
    assert not [r for r in attempts.records(coordinator.config)
                if r["status"] in attempts.ACTIVE]     # the slot is free


def test_a_live_assignment_is_not_reaped(coordinator, store):
    """The TTL is the whole safety margin: inside it the holder is presumed
    alive, because reaping a live assignment gives two sessions the same work
    (the same rule `Claims.reap` applies to claims)."""
    from autoresearch.errors import AutoresearchError
    it = coordinator.run_iteration(1)
    with pytest.raises(AutoresearchError, match="presumed alive"):
        attempts.reap_assignment(coordinator.config, it.attempt_ids[0], "other")
    assert attempts.load(coordinator.config, it.attempt_ids[0])["status"] \
        == "assigned"


def test_reaping_a_settled_assignment_is_refused(coordinator, store):
    from autoresearch.errors import AutoresearchError
    it = coordinator.run_iteration(1)
    identity = it.attempt_ids[0]
    attempts.settle(coordinator.config, identity, attempts.load(
        coordinator.config, identity)["session"], "failed")
    with pytest.raises(AutoresearchError, match="already settled"):
        attempts.reap_assignment(coordinator.config, identity, "other")


def test_cancel_folds_back_and_any_session_can_list_and_show(
        coordinator, capsys):
    """#17: the board said "N assignment(s) outstanding" after a cancel,
    because only `complete` folded back; and no session but the owner could
    list or show the assignment it named."""
    from autoresearch import cli
    it = coordinator.run_iteration(1)
    identity = it.attempt_ids[0]
    root = ["--domain", str(coordinator.config.paths.root), "--session", "curator"]
    assert cli.main([*root, "external", "list"]) == 0
    assert identity in capsys.readouterr().out
    assert cli.main([*root, "external", "show", identity]) == 0
    assert json.loads(capsys.readouterr().out)["entry"] == "Q1"

    external.cancel(coordinator.config, identity, "coordinator", "abandoned",
                    confirm_inactive=True)
    record = json.loads(next(
        coordinator.config.paths.iterations.glob("*.json")).read_text())
    assert record["verdicts"] == {"Q1": "cancelled"}
    assert record["stop_detail"].startswith("every assignment settled")
    assert cli.main([*root, "external", "list"]) == 0
    assert identity not in capsys.readouterr().out


def test_settling_an_entry_after_its_assignment_was_cancelled_links_them(
        coordinator, store):
    """#17: the assignment read `cancelled`, the entry `confirmed`, and
    nothing in the record tied the two."""
    from autoresearch import cli
    from conftest import memo
    it = coordinator.run_iteration(1)
    identity = it.attempt_ids[0]
    external.cancel(coordinator.config, identity, "coordinator", "abandoned",
                    confirm_inactive=True)
    root = ["--domain", str(coordinator.config.paths.root), "--session", "hand"]
    assert cli.main([*root, "claim", "Q1", "--why", "finishing by hand"]) == 0
    assert cli.main([*root, "close", "Q1", "refuted", "--closure", "cell",
                     "--reopen-condition", "a wider sweep",
                     "--summary", "no gain", "--memo",
                     memo(coordinator.config)]) == 0
    link = next(e for e in store.load("Q1").history if e.kind == "external-link")
    assert identity in link.detail
