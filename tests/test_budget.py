import pytest
from conftest import make_entry

from autoresearch.budget import (BUDGET, MET, RUNNING, YIELD, Budget, Meter,
                                 domain_budget, iteration_budget, should_stop)
from autoresearch.errors import BudgetExceeded


def test_meter_raises_rather_than_warning():
    """H138: a warning nobody can act on is worse than nothing. Meters refuse."""
    m = Meter("runs", 2, unit="runs")
    m.spend(); m.spend()
    with pytest.raises(BudgetExceeded) as exc:
        m.spend()
    assert exc.value.meter == "runs" and "Checkpoint" in str(exc.value)


def test_meter_without_a_ceiling_never_blocks():
    m = Meter("x", None)
    for _ in range(100):
        m.spend()
    assert m.remaining() == float("inf")


def test_iteration_fanout_makes_a_runaway_structurally_impossible(toy):
    """The coordinator allocates workers from a fixed pool, so it cannot spawn
    its way out of its own budget."""
    b = iteration_budget(toy)
    for _ in range(int(toy.budgets["iteration_fanout"])):
        b.spend("fanout")
    with pytest.raises(BudgetExceeded, match="fanout"):
        b.spend("fanout")


def test_unknown_meter_is_refused_not_ignored(toy):
    with pytest.raises(BudgetExceeded, match="no meter"):
        iteration_budget(toy).spend("nonexistent")


def test_money_ceiling_comes_from_policy(toy):
    b = domain_budget(toy)
    assert b["money"].ceiling == toy.policy.spend_ceiling
    b.spend("money", 4.0)
    with pytest.raises(BudgetExceeded, match="money"):
        b.spend("money", 2.0)


def test_report_shows_every_meter_and_flags_exhaustion(toy):
    b = iteration_budget(toy)
    b.spend("fanout", toy.budgets["iteration_fanout"])
    text = b.report()
    assert "fanout" in text and "EXHAUSTED" in text


def test_stop_when_goal_is_met(toy):
    d = should_stop(toy, measurements={"ops": 1000.0, "peak": 10.0}, target=6_000_000)
    assert d.reason == MET and d.should_stop


def test_stop_when_a_budget_is_exhausted(toy):
    b = Budget("iteration", {"fanout": Meter("fanout", 1)})
    b.spend("fanout")
    d = should_stop(toy, budgets=[b])
    assert d.reason == BUDGET and d.meters == ["fanout"]


def test_stop_when_yield_falls_below_the_floor(toy):
    """The third exit: the loop stops when it stops learning, rather than only
    when a human notices."""
    d = should_stop(toy, verdicts_per_iteration=[1, 1, 0, 0, 0, 0, 0, 0])
    assert d.reason == YIELD and "repaying" in d.detail


def test_healthy_yield_does_not_stop(toy):
    d = should_stop(toy, verdicts_per_iteration=[1, 1, 1, 1, 1, 1])
    assert d.reason == RUNNING and not d.should_stop


def test_a_malformed_measurement_is_not_a_stop(toy):
    d = should_stop(toy, measurements={"ops": 1.0}, target=100.0)   # `peak` absent
    assert d.reason == RUNNING


def test_claim_budget_uses_the_claims_own_ceiling(toy, sandbox, store):
    from autoresearch.budget import claim_budget
    from autoresearch.entries import Claim
    e = make_entry(store, "Q1",
                   claim=Claim(session="w", at="2026-08-31T00:00:00+00:00",
                               max_runs=3, max_hours=1.5))
    b = claim_budget(e, sandbox)
    assert b["runs"].ceiling == 3 and b["hours"].ceiling == 1.5
