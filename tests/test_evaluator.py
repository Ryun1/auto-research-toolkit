"""#36: the evaluator a worker is scored by is not the worker's to edit, and a
row the worker wrote by hand is not a measurement.

A worker measures its candidate from the primary checkout: the command, the
policy and the evaluator files come from there, the candidate tree from the
slot. Core hashes the evaluator itself, times the run itself, and leaves a
receipt outside the slot; harvest admits only receipted rows. The tamper
canary below is a scripted worker trying each way around that.
"""
import json
import pathlib

import pytest

from autoresearch import cli, evaluator
from autoresearch import runs as runs_mod
from autoresearch.config import DomainConfig
from autoresearch.runs import RunRecord, append, read_all
from autoresearch.workspaces import Pool

FAKE = """#!/usr/bin/env python3
import json, sys, time
json.dump({"schema": "ar-run-1", "id": "fabricated", "session": "x",
           "status": "ok", "started": time.time(), "finished": time.time(),
           "metrics": {"ops": 1.0, "peak": 1.0}, "config": {}, "provenance": {},
           "outputs": [], "cost": {}}, sys.stdout)
print()
"""


def _ar(root, *args):
    return cli.main(["--domain", str(root), "--session", "it1-Q1", *args])


@pytest.fixture
def slot(sandbox):
    with Pool(sandbox, "tamper-test") as pool:
        yield pool.acquire("it1-Q1").path


def _slot_rows(sandbox, slot):
    return read_all(slot / sandbox.paths.runs.relative_to(sandbox.paths.root))


def test_measure_stamps_the_evaluator_hash_and_wall_clock_core_computes(sandbox):
    assert _ar(sandbox.paths.root, "measure", "--", "--knob", "unroll=4") == 0
    [row] = read_all(sandbox.paths.runs)
    assert row.provenance["evaluator_sha"] == evaluator.identity(sandbox)
    assert row.cost["wall_seconds"] > 0


def test_the_default_evaluator_is_the_measure_script(sandbox):
    assert evaluator.files(sandbox) == ["bin/measure"]


def test_a_declared_evaluator_set_is_hashed_whole(sandbox):
    (sandbox.paths.root / "lib").mkdir()
    (sandbox.paths.root / "lib/score.py").write_text("WEIGHT = 1\n")
    toml = sandbox.paths.root / "domain.toml"
    toml.write_text(toml.read_text()
                    + '\n[evaluator]\nfiles = ["bin/measure", "lib/score.py"]\n')
    config = DomainConfig.load(sandbox.paths.root)
    before = evaluator.identity(config)
    (sandbox.paths.root / "lib/score.py").write_text("WEIGHT = 2\n")
    assert evaluator.identity(config) != before


def test_a_candidate_is_measured_by_the_primary_evaluator(sandbox, slot):
    """The slot's domain.toml names a fabricating command; it is never read."""
    (slot / "bin/fake").write_text(FAKE)
    (slot / "bin/fake").chmod(0o755)
    toml = slot / "domain.toml"
    toml.write_text(toml.read_text().replace('measure      = "bin/measure"',
                                             'measure      = "bin/fake"'))
    assert _ar(sandbox.paths.root, "measure", "--candidate", str(slot),
               "--entry", "Q1", "--", "--knob", "unroll=4") == 0
    [row] = _slot_rows(sandbox, slot)
    assert row.id != "fabricated" and row.metrics["ops"] > 1.0
    assert row.provenance["evaluator_sha"] == evaluator.identity(sandbox)
    assert read_all(sandbox.paths.runs) == []      # the slot's row, not the ledger's
    assert evaluator.has_receipt(sandbox, row)


def test_a_candidate_whose_evaluator_was_edited_is_refused(sandbox, slot, capsys):
    measure = slot / "bin/measure"
    measure.write_text(measure.read_text().replace(
        'return {"ops": round(ops, 3)', 'return {"ops": 1.0 or round(ops, 3)'))
    assert _ar(sandbox.paths.root, "measure", "--candidate", str(slot),
               "--", "--knob", "unroll=4") == 2
    assert "evaluator differs" in capsys.readouterr().err
    assert _slot_rows(sandbox, slot) == []


def test_harness_debt_may_measure_a_candidate_that_edits_the_evaluator(
        sandbox, slot):
    """Fixing a scorer is harness work; the primary's copy still scores it."""
    (slot / "bin/measure").write_text(
        (slot / "bin/measure").read_text() + "\n# a scorer fix\n")
    assert _ar(sandbox.paths.root, "measure", "--candidate", str(slot),
               "--entry", "H1", "--", "--knob", "unroll=4") == 0
    [row] = _slot_rows(sandbox, slot)
    assert row.provenance["evaluator_sha"] == evaluator.identity(sandbox)


def test_the_slot_cannot_lift_a_human_only_rule_on_measuring(sandbox, slot, capsys):
    toml = sandbox.paths.root / "domain.toml"
    toml.write_text(toml.read_text() + '\n[[policy.human_only]]\n'
                    'pattern = "bin/measure"\nreason = "measured by a person"\n')
    # The slot is a copy from before the rule: it has no such rule at all.
    assert _ar(sandbox.paths.root, "measure", "--candidate", str(slot)) == 2
    assert "measured by a person" in capsys.readouterr().err
    assert _slot_rows(sandbox, slot) == []


def test_a_candidate_outside_the_workspace_pool_is_refused(sandbox, tmp_path,
                                                           capsys):
    assert _ar(sandbox.paths.root, "measure", "--candidate", str(tmp_path)) == 2
    assert "not a workspace" in capsys.readouterr().err


def _harvest(sandbox, slot, before):
    relative = sandbox.paths.runs.relative_to(sandbox.paths.root)
    return runs_mod.harvest(slot / relative, before, sandbox.paths.runs,
                            workspace=slot, root=sandbox.paths.root,
                            goal=sandbox.goal, lanes=sandbox.lanes,
                            receipts=sandbox.paths.receipts)


def test_harvest_refuses_a_hand_written_row(sandbox, slot):
    relative = sandbox.paths.runs.relative_to(sandbox.paths.root)
    before = runs_mod.snapshot(slot / relative)
    append(slot / relative / "it1-Q1.jsonl", RunRecord(
        id="handmade", metrics={"ops": 1.0, "peak": 1.0}, session="it1-Q1",
        entry="Q1", provenance={"host": "local"}))
    result = _harvest(sandbox, slot, before)
    assert result.records == []
    assert any("no measurement receipt" in p for p in result.problems)
    assert read_all(sandbox.paths.runs) == []


def test_harvest_refuses_a_receipted_row_edited_after_measurement(sandbox, slot):
    relative = sandbox.paths.runs.relative_to(sandbox.paths.root)
    before = runs_mod.snapshot(slot / relative)
    assert _ar(sandbox.paths.root, "measure", "--candidate", str(slot),
               "--", "--knob", "unroll=4") == 0
    path = slot / relative / "it1-Q1.jsonl"
    row = json.loads(path.read_text())
    row["metrics"]["ops"] = 1.0
    path.write_text(json.dumps(row) + "\n")
    result = _harvest(sandbox, slot, before)
    assert result.records == []
    assert any("does not match its receipt" in p for p in result.problems)


def test_harvest_admits_a_row_ar_measure_recorded(sandbox, slot):
    relative = sandbox.paths.runs.relative_to(sandbox.paths.root)
    before = runs_mod.snapshot(slot / relative)
    assert _ar(sandbox.paths.root, "measure", "--candidate", str(slot),
               "--", "--knob", "unroll=4") == 0
    result = _harvest(sandbox, slot, before)
    assert result.problems == [] and len(result.records) == 1


def test_drift_names_the_evaluator_files_a_slot_changed(sandbox, slot):
    assert evaluator.drift(sandbox, slot) == []
    (slot / "bin/measure").write_text("#!/bin/sh\necho tampered\n")
    assert evaluator.drift(sandbox, slot) == ["bin/measure"]


def test_validate_warns_when_a_row_reports_a_run_core_did_not_time(sandbox):
    """A patched `time.time` inside the candidate shrinks the duration the row
    reports; the wall clock core kept around the subprocess does not move."""
    from autoresearch import validate
    append(sandbox.paths.runs / "s.jsonl", RunRecord(
        id="fast", metrics={"ops": 1.0, "peak": 1.0}, session="s",
        started=100.0, finished=100.5, cost={"wall_seconds": 120.0}))
    append(sandbox.paths.runs / "s.jsonl", RunRecord(
        id="honest", metrics={"ops": 1.0, "peak": 1.0}, session="s",
        started=100.0, finished=219.0, cost={"wall_seconds": 120.0}))
    warnings = validate.measurement_warnings(sandbox)
    assert len(warnings) == 1 and "fast" in warnings[0]


def test_a_worker_that_edits_its_evaluator_lands_no_rows(sandbox, store):
    """The canary end to end: a worker edits `bin/measure` in its slot and
    measures through the old in-slot path, so the fabricated row is in its
    runs directory. Nothing is harvested, and the slot is kept for audit."""
    import subprocess
    import sys
    from types import SimpleNamespace

    from autoresearch.budget import iteration_budget
    from autoresearch.driver.brain import Reply, Role
    from autoresearch.driver.loop import Coordinator, Iteration
    from conftest import make_entry
    make_entry(store, "Q1", impact=1.0)

    class Brain:
        def ask(self, role, brief, *, workspace=None):
            measure = workspace / "bin/measure"
            measure.write_text(FAKE)
            subprocess.run([sys.executable, "-m", "autoresearch.cli", "--domain",
                            str(workspace), "--session", "it1-Q1", "measure"],
                           check=True, capture_output=True)
            (workspace / "inbox/result.md").write_text("ops 1.0")
            return Reply(role=role, data={
                "verdict": "confirmed", "memo": "inbox/result.md", "runs": 1,
                "summary": "ops 1.0",
                "verification": {"reread": True, "claims_checked": ["ops"]}})

    coordinator = Coordinator(sandbox, Brain())
    iteration = Iteration(n=1)
    with Pool(sandbox, "canary") as pool:
        coordinator.dispatch(iteration, [SimpleNamespace(entry_id="Q1", terms={})],
                             iteration_budget(sandbox), pool)
    assert read_all(sandbox.paths.runs) == []
    assert store.load("Q1").status != "confirmed"
    assert any("edited the evaluator" in line
               for line in iteration.phases[0].detail)
    assert (sandbox.paths.workspaces / "it1-Q1").exists()


def test_a_hand_written_row_in_an_untouched_slot_is_refused(sandbox, store):
    """The other half: the evaluator is left alone, the worker just writes
    the row it wanted. No receipt, no evidence, no confirmation."""
    from types import SimpleNamespace

    from autoresearch.budget import iteration_budget
    from autoresearch.driver.brain import Reply
    from autoresearch.driver.loop import Coordinator, Iteration
    from conftest import make_entry
    make_entry(store, "Q1", impact=1.0)

    class Brain:
        def ask(self, role, brief, *, workspace=None):
            append(workspace / "data/runs/it1-Q1.jsonl", RunRecord(
                id="wished", metrics={"ops": 1.0, "peak": 1.0},
                session="it1-Q1", entry="Q1", provenance={"host": "local"}))
            (workspace / "inbox/result.md").write_text("ops 1.0")
            return Reply(role=role, data={
                "verdict": "confirmed", "memo": "inbox/result.md", "runs": 1,
                "summary": "ops 1.0",
                "verification": {"reread": True, "claims_checked": ["ops"]}})

    coordinator = Coordinator(sandbox, Brain())
    iteration = Iteration(n=1)
    with Pool(sandbox, "canary") as pool:
        coordinator.dispatch(iteration, [SimpleNamespace(entry_id="Q1", terms={})],
                             iteration_budget(sandbox), pool)
    assert read_all(sandbox.paths.runs) == []
    assert store.load("Q1").status != "confirmed"
    assert any("no measurement receipt" in line
               for line in iteration.phases[0].detail)


def test_an_external_assignment_measures_through_its_record_command(
        sandbox, store):
    """The handoff names the one way a run counts, and that way settles."""
    import shlex
    import subprocess
    import sys

    from autoresearch import external
    from autoresearch.claims import Claims
    from conftest import make_entry
    make_entry(store, "Q1")
    Claims(store, sandbox, "owner").claim("Q1", why="bounded execution")
    assigned = external.assign(sandbox, "Q1", "owner", 30, 1)
    argv = shlex.split(assigned["record_command"])
    assert "--candidate" in argv and str(sandbox.paths.root) in argv
    subprocess.run([sys.executable, "-m", "autoresearch.cli", *argv[1:],
                    "--knob", "unroll=4"], check=True, capture_output=True)
    workspace = pathlib.Path(assigned["workspace"])
    (workspace / "inbox/m.md").write_text("measured")
    completed = external.complete(sandbox, assigned["id"], "owner", {
        "runs": 1, "verdict": "inconclusive", "memo": "inbox/m.md",
        "verification": {"reread": True, "claims_checked": ["row retained"]}})
    assert completed["status"] == "completed"
    [row] = read_all(sandbox.paths.runs)
    assert row.provenance["evaluator_sha"] == evaluator.identity(sandbox)


@pytest.mark.parametrize("spec", ['files = []', 'files = ["../outside.py"]',
                                  'file = ["bin/measure"]'])
def test_a_malformed_evaluator_table_is_refused_at_load(sandbox, spec):
    from autoresearch.errors import ConfigError
    toml = sandbox.paths.root / "domain.toml"
    toml.write_text(toml.read_text() + f"\n[evaluator]\n{spec}\n")
    with pytest.raises(ConfigError, match=r"\[evaluator\]"):
        DomainConfig.load(sandbox.paths.root)


def test_a_measure_script_that_reuses_a_run_id_is_refused(sandbox, slot, capsys):
    """A receipt is per run id; a second row under one id would be admitted
    on the first row's receipt if the digest matched, so it is never written."""
    for tree in (sandbox.paths.root, slot):
        (tree / "bin/measure").write_text(FAKE)
    assert _ar(sandbox.paths.root, "measure", "--candidate", str(slot)) == 0
    assert _ar(sandbox.paths.root, "measure", "--candidate", str(slot)) == 2
    assert "already recorded" in capsys.readouterr().err
    assert [r.id for r in _slot_rows(sandbox, slot)] == ["fabricated"]


def test_a_run_id_cannot_point_harvest_at_a_receipt_the_slot_wrote(sandbox, slot):
    """The run id names the receipt file, and the worker writes the run id. An
    id that climbs out of the receipts directory would read a receipt from
    inside the slot -- one the worker forged with a matching digest."""
    relative = sandbox.paths.runs.relative_to(sandbox.paths.root)
    before = runs_mod.snapshot(slot / relative)
    forged_id = str(pathlib.Path("..") / "workspaces" / slot.name / "forged")
    row = RunRecord(id=forged_id, metrics={"ops": 1.0, "peak": 1.0},
                    session="it1-Q1", entry="Q1", provenance={"host": "local"})
    append(slot / relative / "it1-Q1.jsonl", row)
    (slot / "forged.json").write_text(json.dumps(
        {"run": forged_id, "digest": evaluator._row_digest(row)}))
    result = _harvest(sandbox, slot, before)
    assert result.records == []
    assert any("run id" in p for p in result.problems)


def test_a_receipt_is_never_written_outside_the_receipts_directory(sandbox, tmp_path):
    row = RunRecord(id="../../escaped", metrics={"ops": 1.0, "peak": 1.0},
                    session="s")
    with pytest.raises(Exception, match="run id"):
        evaluator.write_receipt(sandbox.paths.receipts, row, tmp_path)
    assert not (sandbox.paths.root / "escaped.json").exists()
