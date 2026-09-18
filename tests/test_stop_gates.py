"""The goal text may demand evidence the objective expression cannot show.

Field evidence (eip8200-research): the loop recorded `goal-met` while the goal
text required a kernel-checked proof, because the stop decision saw only the
objective. A stop that records a win the goal text does not allow is the
defect; these tests pin the gate linkage, the zero-on-zero refusal, and the
precision of the met detail.
"""
import dataclasses

import pytest

from autoresearch.budget import MET, RUNNING, should_stop
from autoresearch.driver.brain import Role, ScriptedBrain
from autoresearch.driver.loop import Coordinator
from autoresearch.entries import Store
from autoresearch.errors import GoalError
from autoresearch.goal import Goal, Target
from autoresearch.runs import RunRecord, append
from conftest import make_entry

VERIFIED = {"reread": True, "claims_checked": ["summary", "verdict"],
            "corrections": []}

IDLE = {Role.GENERATOR: lambda b: [], Role.JUDGE: lambda b: [],
        Role.WORKER: lambda b: {"verdict": "inconclusive", "summary": "n/a",
                                "verification": dict(VERIFIED)},
        Role.CURATOR: lambda b: {"reprice": [], "notes": []},
        Role.QC: lambda b: {"problems": [], "harness_debt": [],
                            "verdict": "clean"}}


def make_goal(**over):
    data = {"id": "g",
            "objective": "ops",
            "metrics": {"ops": {"direction": "minimise"}},
            "direction": "minimise",
            "target": {"value": 0.05},
            "stop_when": "objective < target"}
    data.update(over)
    return Goal.from_dict(data)


@pytest.fixture
def cfg(toy):
    """should_stop reads only config.goal; tests swap the goal in."""
    return lambda goal: dataclasses.replace(toy, goal=goal)


def seed_best_run(sandbox, **over):
    """One ok run on disk that meets any target the tests set."""
    data = {"metrics": {"ops": 10.0, "peak": 1.0}, "session": "seed",
            "entry": "Q1", "provenance": {"host": "test"}}
    data.update(over)
    append(sandbox.paths.runs / "seed.jsonl", RunRecord(**data))


# -- should_stop -------------------------------------------------------------

def test_a_required_gate_not_passed_records_running_not_a_win(cfg):
    goal = make_goal(required_gates=["kernel-proof"])
    d = should_stop(cfg(goal), measurements={"ops": -0.1}, target=0.05,
                    unmet_required_gates=("kernel-proof",))
    assert d.reason == RUNNING and not d.should_stop
    assert "kernel-proof" in d.detail


def test_passed_gates_still_reach_the_met_exit(cfg):
    goal = make_goal(required_gates=["kernel-proof"])
    d = should_stop(cfg(goal), measurements={"ops": -0.1}, target=0.05,
                    unmet_required_gates=())
    assert d.reason == MET and d.should_stop


def test_no_required_gates_stops_exactly_as_before(cfg):
    d = should_stop(cfg(make_goal()), measurements={"ops": -0.1}, target=0.05)
    assert d.reason == MET
    assert d.detail == "objective -0.1 meets target 0.05"


def test_zero_on_zero_is_refused_unless_the_goal_allows_it(cfg):
    goal = make_goal(target={"value": 0}, stop_when="objective <= target")
    d = should_stop(cfg(goal), measurements={"ops": 0.0}, target=0)
    assert d.reason == RUNNING and not d.should_stop
    assert "allow_degenerate_target" in d.detail

    goal.allow_degenerate_target = True
    d = should_stop(cfg(goal), measurements={"ops": 0.0}, target=0)
    assert d.reason == MET


def test_the_met_detail_keeps_precision_the_old_format_rounded_away(cfg):
    """`{value:,.0f}` displayed a 0.1 gap as "-0", which is how eip8200's
    record showed a goal-met line that explained nothing."""
    d = should_stop(cfg(make_goal()), measurements={"ops": -0.1}, target=0.05)
    assert d.detail == "objective -0.1 meets target 0.05"


# -- goal loading ------------------------------------------------------------

def test_required_gates_parse_to_a_tuple_of_unique_names():
    goal = make_goal(required_gates=["kernel-proof", "benchmark"])
    assert goal.required_gates == ("kernel-proof", "benchmark")
    assert make_goal().required_gates == ()


@pytest.mark.parametrize("bad", [
    "kernel-proof",          # a string, not a list
    [""],                    # empty name
    ["ok", 2],               # non-string entry
    [None],
    ["dup", "dup"],          # duplicate
])
def test_malformed_required_gates_are_refused_at_load(bad):
    with pytest.raises(GoalError):
        make_goal(required_gates=bad)


def test_allow_degenerate_target_must_be_a_bool():
    with pytest.raises(GoalError):
        make_goal(allow_degenerate_target="yes")


# -- the coordinator's gate lookup ------------------------------------------

def test_a_run_without_an_entry_cannot_inherit_a_proof(sandbox):
    sandbox.goal.required_gates = ("kernel-proof", "benchmark")
    c = Coordinator(sandbox, ScriptedBrain(dict(IDLE)))
    record = RunRecord(metrics={"ops": 1.0}, session="s")
    assert c._unmet_required_gates((1.0, record)) == ("kernel-proof", "benchmark")


def test_a_missing_entry_record_counts_as_every_gate_unmet(sandbox):
    sandbox.goal.required_gates = ("kernel-proof",)
    c = Coordinator(sandbox, ScriptedBrain(dict(IDLE)))
    record = RunRecord(metrics={"ops": 1.0}, session="s", entry="Q404")
    assert c._unmet_required_gates((1.0, record)) == ("kernel-proof",)


def test_partial_gate_states_name_only_the_unmet(sandbox):
    sandbox.goal.required_gates = ("kernel-proof", "benchmark")
    make_entry(Store(sandbox.paths.entries), "Q1", gates=[
        {"name": "kernel-proof", "state": "passed", "evidence": ["probe log"]}])
    c = Coordinator(sandbox, ScriptedBrain(dict(IDLE)))
    record = RunRecord(metrics={"ops": 1.0}, session="s", entry="Q1")
    assert c._unmet_required_gates((1.0, record)) == ("benchmark",)


def test_no_required_gates_means_no_gate_lookup(sandbox):
    c = Coordinator(sandbox, ScriptedBrain(dict(IDLE)))
    record = RunRecord(metrics={"ops": 1.0}, session="s", entry="Q1")
    assert c._unmet_required_gates((1.0, record)) == ()
    assert c._unmet_required_gates(None) == ()


# -- end to end --------------------------------------------------------------

def test_the_loop_wont_record_a_win_the_gates_have_not_earned(sandbox):
    sandbox.goal.target = Target(value=1e9)
    sandbox.goal.required_gates = ("kernel-proof",)
    make_entry(Store(sandbox.paths.entries), "Q1")
    seed_best_run(sandbox)
    it = Coordinator(sandbox, ScriptedBrain(dict(IDLE))).run_iteration(1)
    assert it.stop == RUNNING
    assert "kernel-proof" in it.stop_detail


def test_the_loop_stops_when_the_gates_are_passed(sandbox):
    sandbox.goal.target = Target(value=1e9)
    sandbox.goal.required_gates = ("kernel-proof",)
    make_entry(Store(sandbox.paths.entries), "Q1", gates=[
        {"name": "kernel-proof", "state": "passed", "evidence": ["probe log"]}])
    seed_best_run(sandbox)
    it = Coordinator(sandbox, ScriptedBrain(dict(IDLE))).run_iteration(1)
    assert it.stop == MET
    assert "meets target" in it.stop_detail


def test_a_goal_without_required_gates_stops_as_before(sandbox):
    sandbox.goal.target = Target(value=1e9)
    seed_best_run(sandbox)
    it = Coordinator(sandbox, ScriptedBrain(dict(IDLE))).run_iteration(1)
    assert it.stop == MET


# -- independent confirmation (AIRA arXiv 2507.02554 §5.3) -------------------
# Selecting a search's final artifact by the proxy the search optimised
# overfits: validation keeps rising while held-out performance plateaus.
# `confirm_independently` holds a met target until a second, independent
# measurement row also meets it.

def test_a_win_without_independent_confirmation_is_held(cfg):
    goal = make_goal(confirm_independently=True)
    d = should_stop(cfg(goal), measurements={"ops": -0.1}, target=0.05,
                    independent_confirmation=False)
    assert d.reason == RUNNING and not d.should_stop
    assert "independent confirmation" in d.detail


def test_the_parameter_defaults_to_confirmed(cfg):
    """Library callers who never heard of the flag stop exactly as before."""
    goal = make_goal(confirm_independently=True)
    d = should_stop(cfg(goal), measurements={"ops": -0.1}, target=0.05)
    assert d.reason == MET


def test_a_goal_without_the_flag_ignores_the_parameter(cfg):
    d = should_stop(cfg(make_goal()), measurements={"ops": -0.1}, target=0.05,
                    independent_confirmation=False)
    assert d.reason == MET


def test_confirm_independently_must_be_a_bool():
    with pytest.raises(GoalError):
        make_goal(confirm_independently="yes")


def _best_row():
    from autoresearch.runs import OK
    return (0.04, RunRecord(metrics={"ops": 0.04}, id="r1",
                            session="it1-Q5", entry="Q5", status=OK))


def test_a_second_row_from_another_claim_confirms():
    from autoresearch.budget import independent_confirmation
    from autoresearch.runs import OK

    goal = make_goal(confirm_independently=True)
    second = RunRecord(metrics={"ops": 0.03}, id="r2", session="it2-Q9",
                       entry="Q9", status=OK)
    assert independent_confirmation(goal, _best_row(), [_best_row()[1], second],
                                    0.05)


def test_a_same_claim_duplicate_does_not_confirm():
    """The failure mode the flag exists for: one execution measured twice."""
    from autoresearch.budget import independent_confirmation
    from autoresearch.runs import OK

    goal = make_goal(confirm_independently=True)
    dup = RunRecord(metrics={"ops": 0.03}, id="r2", session="it1-Q5",
                    entry="Q5", status=OK)
    assert not independent_confirmation(goal, _best_row(),
                                        [_best_row()[1], dup], 0.05)


def test_a_non_meeting_or_invalid_row_does_not_confirm():
    from autoresearch.budget import independent_confirmation
    from autoresearch.runs import OK

    goal = make_goal(confirm_independently=True)
    rows = [
        RunRecord(metrics={"ops": 9.0}, id="r2", session="it2-Q9",
                  entry="Q9", status=OK),                 # does not meet
        RunRecord(metrics={"ops": 0.01}, id="r3", session="it2-Q9",
                  entry="Q9", status="invalid"),          # not valid evidence
        RunRecord(metrics={}, id="r4", session="it2-Q9",
                 entry="Q9", status=OK),                  # malformed
    ]
    assert not independent_confirmation(goal, _best_row(), rows, 0.05)


def test_the_loop_holds_a_win_until_an_independent_row_confirms(sandbox):
    from autoresearch.runs import OK, append

    sandbox.goal.confirm_independently = True
    sandbox.goal.target = Target(value=1e9)
    seed_best_run(sandbox)
    it = Coordinator(sandbox, ScriptedBrain(dict(IDLE))).run_iteration(1)
    assert it.stop == RUNNING
    assert "independent confirmation" in it.stop_detail

    # A duplicate measured by the same execution changes nothing.
    append(sandbox.paths.runs / "dup.jsonl", RunRecord(
        metrics={"ops": 5.0, "peak": 1.0}, id="dup", session="seed",
        entry="Q1", status=OK))
    it = Coordinator(sandbox, ScriptedBrain(dict(IDLE))).run_iteration(2)
    assert it.stop == RUNNING

    # A genuinely independent re-measurement earns the win.
    append(sandbox.paths.runs / "conf.jsonl", RunRecord(
        metrics={"ops": 5.0, "peak": 1.0}, id="conf", session="worker-slot",
        entry="Q2", status=OK))
    it = Coordinator(sandbox, ScriptedBrain(dict(IDLE))).run_iteration(3)
    assert it.stop == MET
    assert "meets target" in it.stop_detail
