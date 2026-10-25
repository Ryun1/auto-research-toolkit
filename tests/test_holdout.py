"""#38: a holdout the search never sees.

`confirm_independently` accepted any second `ok` row from another session or
entry -- still scored by the metric the search optimised, so it guarded
against one lucky execution and not against overfitting to the evaluator.
A domain may now declare `[commands] holdout`: core consults it only when a
win is on the table (the stop decision, the independent confirmation, an
explicit `ar holdout check`), keeps its rows in coordinator state no slot
receives, shows only pass/fail and the consultation count, and meters the
consultations per epoch -- every peek leaks a little of the holdout.
"""
import json

import pytest

from autoresearch import cli, holdout, runs
from autoresearch.budget import MET, RUNNING
from autoresearch.config import DomainConfig
from autoresearch.errors import ConfigError
from autoresearch.runs import RunRecord, append, read_all
from autoresearch.workspaces import Pool

#: The secret the search must never see: a metric value nothing else emits.
SECRET = 777.777

HOLDOUT = f"""#!/usr/bin/env python3
import json, os, uuid
row = json.loads(os.environ["AR_HOLDOUT_ROW"])
factor = float(open(os.path.join(os.path.dirname(__file__), "factor")).read())
ops = row["metrics"]["ops"] * factor
json.dump({{"schema": "ar-run-1", "id": uuid.uuid4().hex[:12], "session": "holdout",
           "status": "ok", "metrics": {{"ops": ops, "peak": 1.0, "secret": {SECRET}}},
           "config": {{}}, "provenance": {{"host": "holdout-box"}}, "outputs": [],
           "cost": {{}}}}, __import__("sys").stdout)
print()
"""


def _domain(sandbox, factor=1.0, holdout_table="", goal_extra=""):
    root = sandbox.paths.root
    (root / "holdout").mkdir()
    (root / "holdout/bin").write_text(HOLDOUT)
    (root / "holdout/bin").chmod(0o755)
    (root / "holdout/factor").write_text(str(factor))
    toml = root / "domain.toml"
    toml.write_text(toml.read_text().replace(
        "[commands]\n", '[commands]\nholdout = "holdout/bin"\n')
        + "\n[holdout]\nfiles = [\"holdout/factor\"]\n" + holdout_table)
    goal = root / "goal.yaml"
    text = goal.read_text().replace(
        "  target:\n    source: bin/probe-target\n    moving: true\n"
        "    refresh_seconds: 300\n", "  target: {value: 1000}\n")
    assert "value: 1000" in text
    goal.write_text(text.replace("  yield_floor:", goal_extra + "\n  yield_floor:")
                    if goal_extra else text)
    return DomainConfig.load(root)


def _best(config, ops=500.0, session="w", entry=None):
    """A search row that meets the target (objective = round(ops) * peak)."""
    row = RunRecord(id=f"best-{session}", metrics={"ops": ops, "peak": 1.0},
                    session=session, entry=entry, config={"knobs": {"u": 4}},
                    provenance={"host": "local"})
    append(config.paths.runs / f"{session}.jsonl", row)
    return row


def _ar(config, *args):
    return cli.main(["--domain", str(config.paths.root), *args])


# -- declaring it -----------------------------------------------------------

def test_holdout_files_are_the_command_script_plus_declared_ones(sandbox):
    config = _domain(sandbox)
    assert holdout.files(config) == ["holdout/bin", "holdout/factor"]


def test_an_unknown_holdout_key_is_refused(sandbox):
    toml = sandbox.paths.root / "domain.toml"
    toml.write_text(toml.read_text() + "\n[holdout]\npeeks = 3\n")
    with pytest.raises(ConfigError, match=r"\[holdout\]"):
        DomainConfig.load(sandbox.paths.root)


@pytest.mark.parametrize("git", [False, True], ids=["directory", "worktree"])
def test_a_slot_never_receives_the_holdout(sandbox, git):
    import subprocess
    config = _domain(sandbox)
    if git:
        for args in (("init", "-q"), ("add", "."),
                     ("-c", "user.name=T", "-c", "user.email=t@example.invalid",
                      "commit", "-qm", "fixture")):
            subprocess.run(["git", *args], cwd=config.paths.root, check=True,
                           capture_output=True)
    with Pool(config, "holdout-test") as pool:
        slot = pool.acquire("w1").path
        assert not (slot / "holdout/bin").exists()
        assert not (slot / "holdout/factor").exists()
        assert (slot / "bin/measure").exists()


# -- consulting it ----------------------------------------------------------

def test_a_check_reports_pass_or_fail_and_never_the_value(sandbox, capsys):
    config = _domain(sandbox, factor=1.0)
    _best(config)
    assert _ar(config, "holdout", "check") == 0
    out = capsys.readouterr().out
    assert "pass" in out and str(SECRET) not in out and "500" not in out
    assert "1 consultation" in out


def test_a_second_check_of_the_same_config_does_not_peek_again(sandbox, capsys):
    config = _domain(sandbox)
    _best(config)
    assert _ar(config, "holdout", "check") == 0
    assert _ar(config, "holdout", "check") == 0
    assert "1 consultation" in capsys.readouterr().out.splitlines()[-1]


def test_holdout_rows_never_reach_the_ledger(sandbox):
    config = _domain(sandbox)
    _best(config)
    assert _ar(config, "holdout", "check") == 0
    assert all(not (r.config or {}).get("holdout")
               for r in read_all(config.paths.runs))
    assert len(read_all(config.paths.runs)) == 1


def test_a_holdout_row_in_the_ledger_never_stands_on_the_frontier(sandbox, store):
    row = RunRecord(id="h", metrics={"ops": 1.0, "peak": 1.0}, session="s",
                    config={"holdout": True}, provenance={"host": "x"})
    best, excluded = runs.frontier(store, [row], sandbox.goal)
    assert best is None and "holdout" in excluded[2]


def test_a_search_win_the_holdout_rejects_is_not_goal_met(sandbox):
    """The issue's negative test, through the loop's stop decision."""
    from autoresearch.driver.brain import ScriptedBrain
    from autoresearch.driver.loop import Coordinator
    from test_loop import IDLE
    config = _domain(sandbox, factor=3.0)     # holdout: 1500, misses 1000
    _best(config)
    it = Coordinator(config, ScriptedBrain(dict(IDLE))).run_iteration(1)
    assert it.stop == RUNNING and "holdout" in it.stop_detail
    assert str(SECRET) not in it.report() and "1500" not in it.report()


def test_a_search_win_the_holdout_confirms_is_goal_met(sandbox):
    from autoresearch.driver.brain import ScriptedBrain
    from autoresearch.driver.loop import Coordinator
    from test_loop import IDLE
    config = _domain(sandbox, factor=1.0)
    _best(config)
    it = Coordinator(config, ScriptedBrain(dict(IDLE))).run_iteration(1)
    assert it.stop == MET, it.report()


def test_budget_reads_the_verdict_and_never_consults(sandbox, capsys):
    config = _domain(sandbox, factor=3.0)
    _best(config)
    assert _ar(config, "budget", "--json") == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["stop"]["reason"] == RUNNING
    assert "not yet consulted" in doc["stop"]["detail"]
    assert doc["holdout"]["consults"] == 0
    assert _ar(config, "holdout", "check") == 0
    capsys.readouterr()
    assert _ar(config, "budget", "--json") == 0
    doc = json.loads(capsys.readouterr().out)
    assert "holdout" in doc["stop"]["detail"] and doc["holdout"]["consults"] == 1
    assert doc["holdout"]["last"] == "fail"


def test_a_passing_holdout_is_the_independent_confirmation(sandbox):
    from autoresearch.driver.brain import ScriptedBrain
    from autoresearch.driver.loop import Coordinator
    from test_loop import IDLE
    config = _domain(sandbox, factor=1.0,
                     goal_extra="  confirm_independently: true")
    _best(config)                 # one row, one session: no second row exists
    it = Coordinator(config, ScriptedBrain(dict(IDLE))).run_iteration(1)
    assert it.stop == MET, it.report()


def test_consultations_are_capped_per_epoch(sandbox, capsys):
    config = _domain(sandbox, holdout_table="max_consults = 1\n")
    _best(config, ops=500.0, session="a")
    assert _ar(config, "holdout", "check") == 0
    row = RunRecord(id="better", metrics={"ops": 400.0, "peak": 1.0},
                    session="b", config={"knobs": {"u": 5}},
                    provenance={"host": "local"})
    append(config.paths.runs / "b.jsonl", row)
    capsys.readouterr()
    assert _ar(config, "holdout", "check") == 2
    assert "max_consults" in capsys.readouterr().err
    assert _ar(config, "budget", "--json") == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["holdout"] == {"consults": 1, "max": 1, "last": None}
    assert "max_consults" in doc["stop"]["detail"]


def test_a_new_epoch_starts_a_fresh_count(sandbox):
    config = _domain(sandbox, holdout_table="max_consults = 1\n")
    _best(config)
    assert _ar(config, "holdout", "check") == 0
    assert holdout.consults(config) == 1
    measure = config.paths.root / "bin/measure"
    measure.write_text(measure.read_text() + "\n# a new evaluator\n")
    assert holdout.consults(config) == 0


def test_pass_when_can_tolerate_a_gap(sandbox, capsys):
    config = _domain(sandbox, factor=1.1,
                     holdout_table='pass_when = "objective <= search * 1.2"\n')
    _best(config, ops=950.0)      # holdout 1045 misses target 1000, within 20%
    assert _ar(config, "holdout", "check") == 0
    assert "pass" in capsys.readouterr().out


# -- what the search can see ------------------------------------------------

def test_the_board_shows_the_verdict_and_count_only(sandbox, capsys):
    config = _domain(sandbox, factor=3.0)
    _best(config)
    assert _ar(config, "holdout", "check") == 0
    capsys.readouterr()
    assert _ar(config, "board") == 0
    out = capsys.readouterr().out
    assert "holdout    fail" in out and "1 consultation" in out
    assert str(SECRET) not in out and "1500" not in out


def test_a_worker_brief_carries_no_holdout_value(sandbox, store):
    from types import SimpleNamespace

    from autoresearch.budget import iteration_budget
    from autoresearch.driver.brain import Reply
    from autoresearch.driver.loop import Coordinator, Iteration
    from conftest import make_entry
    config = _domain(sandbox, factor=3.0)
    _best(config)
    assert _ar(config, "holdout", "check") == 0
    make_entry(store, "Q1", impact=1.0)
    briefs = []

    class Brain:
        def ask(self, role, brief, *, workspace=None):
            briefs.append(brief)
            return Reply(role=role, data={"verdict": "inconclusive", "summary": "x",
                                          "verification": {"reread": True,
                                                           "claims_checked": ["x"]}})

    with Pool(config, "brief") as pool:
        Coordinator(config, Brain()).dispatch(
            Iteration(n=1), [SimpleNamespace(entry_id="Q1", terms={})],
            iteration_budget(config), pool)
    assert briefs and all(str(SECRET) not in b and "1500" not in b for b in briefs)


# -- review fixes -----------------------------------------------------------

def test_a_cached_pass_does_not_outlive_its_target(sandbox):
    """Review: verdicts were cached by config alone, but a pass is a pass
    against a target. A moving target must be judged again."""
    config = _domain(sandbox, factor=1.5)
    row = _best(config, ops=500.0)                      # holdout: 750
    assert holdout.stop_reason(config, (500.0, row), 1000, session="loop") == (None, True)
    reason, passed = holdout.stop_reason(config, (500.0, row), 600, session="loop")
    assert passed is False and "does not confirm" in reason


def _failing(config, stderr="score 777"):
    script = config.paths.root / "holdout/bin"
    counter = config.paths.root / "count"
    script.write_text(f"#!/bin/sh\necho peek >> {counter}\necho '{stderr}' >&2\nexit 3\n")
    return counter


def test_a_failing_holdout_is_not_rerun_by_every_stop_decision(sandbox):
    config = _domain(sandbox)
    counter = _failing(config)
    row = _best(config)
    for _ in range(5):
        reason, passed = holdout.stop_reason(config, (500.0, row), 1000, session="loop")
    assert counter.read_text().count("peek") == 1
    assert passed is False and holdout.consults(config) == 1
    assert "777" not in reason                         # its output stays private


def test_a_failing_holdout_counts_against_max_consults(sandbox, capsys):
    config = _domain(sandbox, holdout_table="max_consults = 1\n")
    _failing(config)
    _best(config)
    assert _ar(config, "holdout", "check") == 2
    err = capsys.readouterr().err
    assert "777" not in err
    assert _ar(config, "holdout", "check") == 2
    assert "max_consults" in capsys.readouterr().err


def test_a_holdout_that_cannot_start_is_a_clean_error(sandbox, capsys):
    config = _domain(sandbox)
    toml = config.paths.root / "domain.toml"
    toml.write_text(toml.read_text().replace('holdout = "holdout/bin"',
                                             'holdout = "no-such-holdout-binary"'))
    config = DomainConfig.load(config.paths.root)
    _best(config)
    assert _ar(config, "holdout", "check") == 2
    assert "could not start" in capsys.readouterr().err


def test_a_held_out_directory_is_stripped_from_a_slot(sandbox):
    config = _domain(sandbox)
    toml = config.paths.root / "domain.toml"
    toml.write_text(toml.read_text().replace('files = ["holdout/factor"]',
                                             'files = ["holdout"]'))
    config = DomainConfig.load(config.paths.root)
    with Pool(config, "dir") as pool:
        slot = pool.acquire("w1").path
        assert not (slot / "holdout").exists()


def test_a_corrupt_holdout_ledger_is_a_clean_error(sandbox, capsys):
    config = _domain(sandbox)
    _best(config)
    config.paths.holdout.mkdir(parents=True)
    (config.paths.holdout / "consults.jsonl").write_text("{not json\n")
    assert _ar(config, "board") == 2
    assert "consults.jsonl" in capsys.readouterr().err


def test_validate_warns_when_the_holdout_is_committed(sandbox):
    """A committed holdout is readable from any worktree with `git show`,
    whatever is stripped from the working copy."""
    import subprocess

    from autoresearch import validate
    config = _domain(sandbox)
    for args in (("init", "-q"), ("add", "."),
                 ("-c", "user.name=T", "-c", "user.email=t@example.invalid",
                  "commit", "-qm", "fixture")):
        subprocess.run(["git", *args], cwd=config.paths.root, check=True,
                       capture_output=True)
    [warning] = validate.holdout_warnings(config)
    assert "holdout/factor" in warning and "gitignore" in warning
