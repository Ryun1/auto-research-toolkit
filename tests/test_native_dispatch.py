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
