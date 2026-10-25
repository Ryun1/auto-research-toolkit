"""#37: a gain inside measurement noise is not a new best.

`best_run` took the single best row by strict comparison, so one lucky row
0.1% better read as the frontier moving and could fire `stop_when`. With a
`noise` method declared, rows group by config, a group displaces the
incumbent only when its gain clears the floor, and a `confirmed` closure must
clear it too. `none` (the default) keeps today's behaviour.
"""
import json
import os
import subprocess
import sys

import pytest

from autoresearch import cli, noise, runs
from autoresearch.config import DomainConfig
from autoresearch.errors import GoalError
from autoresearch.goal import Goal
from autoresearch.runs import RunRecord, append
from conftest import make_entry


def _goal(**noise_block):
    data = {"id": "g", "objective": "ops", "metrics": {"ops": {}},
            "target": {"value": 1}}
    if noise_block:
        data["noise"] = noise_block
    return Goal.from_dict({"goal": data})


def _row(run_id, ops, config, entry=None, started=None):
    return RunRecord(id=run_id, metrics={"ops": float(ops), "peak": 1.0},
                     session=f"s-{run_id}", entry=entry,
                     started=started if started is not None else 0.0,
                     config={"knobs": config}, provenance={"host": "local"})


def _rows(spec):
    """[(config, [values]), ...] in order of first appearance."""
    out, t = [], 0
    for config, values in spec:
        for value in values:
            t += 1
            out.append(_row(f"{config}{t}", value, config, started=t))
    return out


def _with_noise(sandbox, block):
    path = sandbox.paths.root / "goal.yaml"
    path.write_text(path.read_text().replace(
        "  yield_floor:", block + "\n  yield_floor:"))
    return DomainConfig.load(sandbox.paths.root)


# -- the goal block ---------------------------------------------------------

def test_noise_defaults_to_none():
    assert _goal().noise.method == "none"


@pytest.mark.parametrize("block,match", [
    ({"method": "vibes"}, "method"),
    ({"method": "declared"}, "sigma"),
    ({"method": "mad", "k": -1}, "k"),
    ({"method": "bootstrap", "alpha": 0.7}, "alpha"),
    ({"method": "mad", "colour": "red"}, "unknown"),
])
def test_a_malformed_noise_block_is_refused_at_load(block, match):
    with pytest.raises(GoalError, match=match):
        _goal(**block)


# -- frontier admission -----------------------------------------------------

def test_without_a_noise_method_one_lucky_row_is_best(store):
    rows = _rows([("a", [1000, 1010, 990]), ("b", [995])])
    best, _ = runs.frontier(store, rows, _goal())
    assert best[1].id == "a3"          # the single lowest row, as before


def test_a_gain_within_the_mad_floor_does_not_move_the_frontier(store):
    rows = _rows([("a", [1000, 1010, 990]), ("b", [995])])
    best, excluded = runs.frontier(store, rows, _goal(method="mad"))
    assert best[0] == 1000.0 and best[1].config["knobs"] == "a"
    value, record, reason = excluded
    assert record.id == "b4" and "within noise of" in reason


def test_a_gain_past_the_mad_floor_displaces_the_incumbent(store):
    rows = _rows([("a", [1000, 1010, 990]), ("b", [900])])
    best, excluded = runs.frontier(store, rows, _goal(method="mad"))
    assert best[1].id == "b4" and excluded is None


def test_with_no_replicates_the_floor_is_unknown_and_the_gain_is_admitted(store):
    """pi-autoresearch's rule: the floor applies once there is noise to
    measure. Until then the frontier reads as before."""
    rows = _rows([("a", [1000]), ("b", [995])])
    best, _ = runs.frontier(store, rows, _goal(method="mad"))
    assert best[1].id == "b2"


def test_a_declared_sigma_sets_the_floor(store):
    rows = _rows([("a", [1000]), ("b", [995]), ("c", [950])])
    best, excluded = runs.frontier(store, rows, _goal(method="declared", sigma=10))
    assert best[1].id == "c3"
    rows = _rows([("a", [1000]), ("b", [985])])
    best, excluded = runs.frontier(store, rows, _goal(method="declared", sigma=10))
    assert best[1].id == "a1" and excluded[1].id == "b2"


def test_bootstrap_holds_a_straddling_group_and_admits_a_separated_one(store):
    goal = _goal(method="bootstrap")
    straddle = _rows([("a", [1000, 1010, 990]), ("b", [1004, 985, 996])])
    best, excluded = runs.frontier(store, straddle, goal)
    assert best[1].config["knobs"] == "a" and "within noise" in excluded[2]
    apart = _rows([("a", [1000, 1010, 990]), ("b", [900, 905, 895])])
    best, _ = runs.frontier(store, apart, goal)
    assert best[1].config["knobs"] == "b"


def test_the_bootstrap_is_reproducible(store):
    goal = _goal(method="bootstrap")
    rows = _rows([("a", [1000, 1010, 990]), ("b", [992, 1001, 985])])
    first = runs.frontier(store, rows, goal)
    assert all(runs.frontier(store, rows, goal) == first for _ in range(5))


def test_a_within_noise_row_does_not_trigger_goal_met(sandbox, capsys):
    """The issue's negative test, through the real stop decision."""
    config = _with_noise(sandbox, "  noise:\n    method: mad")
    target = sandbox.paths.root / "bin/probe-target"
    target.write_text("#!/bin/sh\necho 998\n")
    target.chmod(0o755)
    for row in _rows([("a", [1000, 1010, 990]), ("b", [995])]):
        append(config.paths.runs / "s.jsonl", row)
    assert cli.main(["--domain", str(config.paths.root), "budget", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["best"] == 1000.0
    assert doc["stop"]["reason"] != "goal-met"


# -- confirmed closures -----------------------------------------------------

def _close(config, entry_id, capsys):
    root = str(config.paths.root)
    assert cli.main(["--domain", root, "--session", "w", "claim", entry_id,
                     "--why", "t"]) == 0
    (config.paths.root / "inbox").mkdir(exist_ok=True)
    (config.paths.root / f"inbox/{entry_id}.md").write_text("memo")
    code = cli.main(["--domain", root, "--session", "w", "close", entry_id,
                     "confirmed", "--memo", f"inbox/{entry_id}.md",
                     "--summary", "better", "--why", "done"])
    return code, capsys.readouterr().err


def test_confirmed_rows_straddling_the_baseline_are_refused(sandbox, store, capsys):
    config = _with_noise(sandbox, "  noise:\n    method: mad")
    make_entry(store, "Q0")
    make_entry(store, "Q1")
    for n, value in enumerate([1000, 1010, 990]):
        append(config.paths.runs / "s.jsonl", _row(f"base{n}", value, "a", "Q0", n))
    for n, value in enumerate([1004, 985, 996]):
        append(config.paths.runs / "s.jsonl", _row(f"try{n}", value, "b", "Q1", 10 + n))
    code, err = _close(config, "Q1", capsys)
    assert code == 2 and "within noise" in err
    assert store.load("Q1").status != "confirmed"


def test_confirmed_rows_clearing_the_floor_close(sandbox, store, capsys):
    config = _with_noise(sandbox, "  noise:\n    method: mad")
    make_entry(store, "Q0")
    make_entry(store, "Q1")
    for n, value in enumerate([1000, 1010, 990]):
        append(config.paths.runs / "s.jsonl", _row(f"base{n}", value, "a", "Q0", n))
    for n, value in enumerate([900, 905, 895]):
        append(config.paths.runs / "s.jsonl", _row(f"try{n}", value, "b", "Q1", 10 + n))
    code, err = _close(config, "Q1", capsys)
    assert code == 0, err
    assert store.load("Q1").status == "confirmed"


def test_an_uncomputable_floor_names_the_runs_it_needs(sandbox, store, capsys):
    config = _with_noise(sandbox, "  noise:\n    method: bootstrap")
    make_entry(store, "Q0")
    make_entry(store, "Q1")
    for n, value in enumerate([1000, 1010]):
        append(config.paths.runs / "s.jsonl", _row(f"base{n}", value, "a", "Q0", n))
    append(config.paths.runs / "s.jsonl", _row("try", 900, "b", "Q1", 10))
    code, err = _close(config, "Q1", capsys)
    assert code == 2 and "1 more run" in err


def test_the_first_finding_has_no_baseline_to_clear(sandbox, store, capsys):
    config = _with_noise(sandbox, "  noise:\n    method: mad")
    make_entry(store, "Q1")
    append(config.paths.runs / "s.jsonl", _row("only", 900, "b", "Q1", 1))
    code, err = _close(config, "Q1", capsys)
    assert code == 0, err


def test_the_loop_applies_the_same_confirmation_test(sandbox, store):
    from types import SimpleNamespace

    from autoresearch.budget import iteration_budget
    from autoresearch.driver.brain import Reply
    from autoresearch.driver.loop import Coordinator, Iteration
    from autoresearch.workspaces import Pool
    config = _with_noise(sandbox, "  noise:\n    method: mad")
    make_entry(store, "Q0")
    make_entry(store, "Q1", impact=1.0)
    for n, value in enumerate([1000, 1010, 990]):
        append(config.paths.runs / "s.jsonl", _row(f"base{n}", value, "a", "Q0", n))
    for n, value in enumerate([1004, 985, 996]):
        append(config.paths.runs / "s.jsonl", _row(f"try{n}", value, "b", "Q1", 10 + n))

    class Brain:
        def ask(self, role, brief, *, workspace=None):
            (workspace / "inbox/r.md").write_text("better")
            return Reply(role=role, data={
                "verdict": "confirmed", "memo": "inbox/r.md", "runs": 0,
                "summary": "better",
                "verification": {"reread": True, "claims_checked": ["x"]}})

    iteration = Iteration(n=1)
    with Pool(config, "noise") as pool:
        Coordinator(config, Brain()).dispatch(
            iteration, [SimpleNamespace(entry_id="Q1", terms={})],
            iteration_budget(config), pool)
    assert store.load("Q1").status != "confirmed"
    assert any("within noise" in line for line in iteration.phases[0].detail)


# -- the toy domain can be noisy on purpose ---------------------------------

def _toy_ops(sandbox, env):
    proc = subprocess.run([sys.executable, str(sandbox.paths.root / "bin/measure")],
                          capture_output=True, text=True, check=True,
                          env={**os.environ, **env})
    return json.loads(proc.stdout)["metrics"]["ops"]


def test_the_toy_is_deterministic_unless_noise_is_asked_for(sandbox):
    assert _toy_ops(sandbox, {}) == _toy_ops(sandbox, {})
    noisy = {_toy_ops(sandbox, {"AR_TOY_NOISE": "0.01", "AR_TOY_SEED": str(n)})
             for n in range(4)}
    assert len(noisy) == 4
    seeded = {"AR_TOY_NOISE": "0.01", "AR_TOY_SEED": "7"}
    assert _toy_ops(sandbox, seeded) == _toy_ops(sandbox, seeded)


def test_config_digest_ignores_key_order():
    a = _row("x", 1, {"u": 1, "w": 2})
    b = _row("y", 1, {"w": 2, "u": 1})
    assert noise.config_digest(a) == noise.config_digest(b)


def test_admission_settles_against_the_final_incumbent(store, monkeypatch):
    """#47: a comparison is not transitive under the bootstrap. A group held
    against an early incumbent is judged again against the one that won."""
    goal = _goal(method="bootstrap")
    rows = _rows([("a", [1000, 1001]), ("b", [900, 901]), ("c", [950, 951])])
    verdicts = {("a", "b"): False, ("a", "c"): True, ("c", "b"): True}

    def name(group):
        return group.scored[0][1].config["knobs"]

    def table(goal, incumbent, candidate, all_groups):
        return noise.Verdict(verdicts[(name(incumbent), name(candidate))], "table")

    monkeypatch.setattr(noise, "compare", table)
    best, excluded = runs.frontier(store, rows, goal)
    assert best[1].config["knobs"] == "b" and excluded is None
