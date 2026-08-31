"""Renting compute: two gates, no invented speedups, and money stays human."""
import pytest

from autoresearch.escalate import (CAPACITY, GO, NEEDS_MEASUREMENT,
                                   NOT_TRIGGERED, REFUSE, STALLED, TOO_SLOW,
                                   RemoteClass, recommend, triggered)
from autoresearch.hardware import Capability, Throughput

LOCAL = Throughput(2.22, "candidates", "Apple-M2/8t/16g", 8, workload="screen")
FAST = RemoteClass("rtx-4090", 0.40, measured_ratio=11.1,
                   measured_at_concurrency=16384, measured_on="run-abc")
CHEAP = RemoteClass("cpu-32core", 0.15, measured_ratio=3.0,
                    measured_at_concurrency=32, measured_on="run-def")
UNKNOWN = RemoteClass("h100", 2.50)


def go(**over):
    args = dict(trigger=TOO_SLOW, local=LOCAL, units_needed=200_000,
                classes=[FAST, CHEAP], gate0_correct_locally=True)
    args.update(over)
    return recommend(**args)


# -- triggers --------------------------------------------------------------

def test_nothing_triggers_when_local_hardware_is_enough():
    assert triggered(hours_needed=1.0, hours_available=10.0) is None
    assert recommend(trigger=None, local=LOCAL, units_needed=1,
                     classes=[FAST]).verdict == NOT_TRIGGERED


def test_capacity_failure_triggers():
    cap = Capability(ok=False, requirement="wide", host="m2",
                     problems=["needs 64 GB"])
    assert triggered(capability=cap) == CAPACITY


def test_being_too_slow_triggers():
    assert triggered(hours_needed=100.0, hours_available=8.0) == TOO_SLOW


def test_a_stall_only_triggers_when_the_caller_says_it_is_compute_bound():
    """Buying a GPU to run bad ideas faster is the most expensive way to learn
    the ideas were bad, so a bare stall is deliberately not a trigger."""
    assert triggered(stalled_iterations=9) is None          # no threshold set
    assert triggered(stalled_iterations=9, stall_threshold=5) == STALLED


# -- gate 0: correct locally first -----------------------------------------

def test_gate0_refuses_before_the_work_is_correct_locally():
    out = go(gate0_correct_locally=False)
    assert out.verdict == REFUSE and "Gate 0" in out.detail
    assert "Renting to debug" in "\n".join(out.lines)


def test_gate0_refusal_does_not_price_anything():
    assert go(gate0_correct_locally=False).usd is None


# -- the rule: never invent a speedup --------------------------------------

def test_an_unmeasured_class_cannot_be_costed():
    """The same workload measured 0.82x on one GPU and 38.1x on another; nothing
    about the hardware predicted either."""
    out = go(classes=[UNKNOWN])
    assert out.verdict == NEEDS_MEASUREMENT
    assert "spec sheet" in "\n".join(out.lines)
    assert out.usd is None


def test_unmeasured_classes_are_listed_but_not_priced_alongside_measured_ones():
    out = go(classes=[FAST, UNKNOWN])
    body = "\n".join(out.lines)
    assert out.verdict == GO
    assert "h100" in body and "no measured ratio" in body


# -- gate A: the budget must reach a rung ----------------------------------

def test_gateA_refuses_when_even_the_best_class_cannot_fit():
    out = go(units_needed=10_000_000_000, hours_available=4.0)
    assert out.verdict == REFUSE and "Gate A" in out.detail
    assert "Renting buys wall clock, not the answer" in "\n".join(out.lines)
    assert "Required ratio to fit" in "\n".join(out.lines)


def test_a_reachable_target_is_a_go_and_picks_the_cheapest_overall():
    """Cheapest per hour is not cheapest overall: at 11.1x the $0.40/h box
    finishes in 2.3 h for $0.90, while the $0.15/h box takes 8.3 h for $1.25.
    Costing beats intuition, which is why this is arithmetic and not a rule of
    thumb."""
    out = go()
    assert out.verdict == GO
    assert out.best.name == "rtx-4090"
    assert out.usd == pytest.approx(0.90, abs=0.02)
    assert out.hours == pytest.approx(2.25, abs=0.05)


def test_the_cheaper_per_hour_class_is_costed_too_and_loses_on_total():
    body = "\n".join(go().lines)
    assert "cpu-32core" in body and "1.2" in body


def test_go_shows_the_arithmetic_for_every_priced_class():
    body = "\n".join(go().lines)
    assert "rtx-4090" in body and "cpu-32core" in body
    assert "local" in body and "need" in body


# -- money stays a human decision ------------------------------------------

def test_spend_ceiling_refuses_rather_than_assuming():
    out = go(spend_ceiling=0.01)
    assert out.verdict == REFUSE and "ceiling" in out.detail
    assert "human decision" in "\n".join(out.lines)


def test_a_go_is_a_recommendation_not_an_action():
    out = go()
    assert out.should_ask_human
    assert "not an action" in out.report()


def test_zero_local_throughput_is_refused_rather_than_extrapolated():
    from autoresearch.errors import ConfigError
    zero = Throughput(0.0, "c", "m", 1)
    with pytest.raises(ConfigError, match="must be positive"):
        recommend(trigger=TOO_SLOW, local=zero, units_needed=1, classes=[FAST],
                  gate0_correct_locally=True)
