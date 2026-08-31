import pytest

from autoresearch.errors import GoalError
from autoresearch.goal import Goal, Target, YieldFloor


def make(**over):
    data = {
        "id": "g", "objective": "round(T) * Q",
        "metrics": {"T": {}, "Q": {}},
        "derived": {"break_even": "T / Q"},
        "target": {"value": 1_000_000},
        "stop_when": "objective < target",
    }
    data.update(over)
    return Goal.from_dict({"goal": data})


def test_derived_constants_are_computed_never_stored():
    g = make()
    ns = g.namespace({"T": 1000.0, "Q": 10})
    assert ns["break_even"] == pytest.approx(100.0)
    # change the measurement, the derived value follows -- there is no copy to
    # go stale, which is the entire point (H102, H138).
    assert g.namespace({"T": 2000.0, "Q": 10})["break_even"] == pytest.approx(200.0)


def test_unknown_name_in_objective_fails_at_load_not_at_use():
    with pytest.raises(GoalError, match="unknown name"):
        make(objective="round(T) * Z")


def test_unknown_name_in_derived_fails_at_load():
    with pytest.raises(GoalError, match="unknown name"):
        make(derived={"x": "T / MISSING"})


def test_missing_measurement_is_refused_not_defaulted():
    """H134: a metric that reads as zero when absent is how four rows claiming a
    perfect score entered the corpus."""
    with pytest.raises(GoalError, match="missing declared metric"):
        make().namespace({"T": 5.0})


def test_distance_is_signed_and_normalised():
    g = make()
    assert g.distance({"T": 2000.0, "Q": 1000}, 1_000_000) == pytest.approx(1.0)
    assert g.distance({"T": 500.0, "Q": 1000}, 1_000_000) == pytest.approx(-0.5)


def test_maximise_flips_the_sign_of_distance():
    g = make(direction="maximise", objective="T", stop_when=None, derived={})
    assert g.distance({"T": 5.0, "Q": 1}, 10) == pytest.approx(0.5)
    assert g.distance({"T": 20.0, "Q": 1}, 10) == pytest.approx(-1.0)


def test_moving_target_caches_until_refresh():
    calls = []

    def probe(_):
        calls.append(1)
        return 100.0 + len(calls)

    t = Target(source="bin/probe", moving=True, refresh_seconds=60)
    assert t.resolve(probe, now=0) == 101.0
    assert t.resolve(probe, now=30) == 101.0      # cached
    assert t.resolve(probe, now=120) == 102.0     # refreshed
    assert len(calls) == 2


def test_target_needs_exactly_one_source():
    with pytest.raises(GoalError):
        Target()
    with pytest.raises(GoalError):
        Target(value=1, source="x")


def test_yield_floor_stops_a_loop_that_stopped_learning():
    floor = YieldFloor(confirmed_per_iteration=0.5, over_iterations=3)
    assert not floor.breached([1, 1])                 # not enough history
    assert not floor.breached([1, 1, 1, 0])           # 0.66 over last 3
    assert floor.breached([1, 1, 0, 0, 0])            # 0.0 over last 3


def test_inactive_yield_floor_never_breaches():
    assert not YieldFloor().breached([0] * 50)
