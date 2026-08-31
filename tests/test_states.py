import pytest

from autoresearch.errors import ConfigError, TransitionError
from autoresearch.states import (CLOSURE_KINDS, StateMachine, Status, Transition,
                                 default_machine)


def test_default_machine_is_total():
    default_machine().check_total()


def test_blocked_has_a_route_back_to_queued():
    """H111: `blocked -> queued` had no supported writer, so a stale block was
    permanent by construction."""
    m = default_machine()
    assert m.transition("blocked", "queued").writer == "ar close --reopen"


def test_in_progress_can_be_released():
    """H78: with no supported release, an agent had to close dishonestly or squat."""
    t = default_machine().transition("in-progress", "queued")
    assert t.writer == "ar claim --release" and t.reason_required


def test_every_terminal_status_can_be_reopened():
    m = default_machine()
    for name in m.terminal_names:
        assert m.transition(name, "queued").writer == "ar close --reopen"


def test_dead_end_is_refused():
    """The negative test: check_total must refuse something."""
    m = StateMachine(
        {"queued": Status("queued"), "done": Status("done", terminal=True)},
        "queued", [Transition("queued", "done", "ar close")])
    with pytest.raises(ConfigError, match="terminal by omission"):
        m.check_total()


def test_unreachable_status_is_refused():
    m = StateMachine(
        {"queued": Status("queued"), "ghost": Status("ghost")},
        "queued", [Transition("queued", "queued", "w"),
                   Transition("ghost", "queued", "w")])
    with pytest.raises(ConfigError, match="unreachable"):
        m.check_total()


def test_transition_with_no_writer_is_refused():
    m = StateMachine({"a": Status("a"), "b": Status("b")}, "a",
                     [Transition("a", "b", ""), Transition("b", "a", "w")])
    with pytest.raises(ConfigError, match="no named writer"):
        m.check_total()


def test_illegal_transition_names_the_legal_ones():
    with pytest.raises(TransitionError, match="legal moves"):
        default_machine().transition("queued", "confirmed")


def test_refutation_requires_a_closure_kind():
    m = default_machine()
    assert m.status("refuted").requires_closure_kind
    assert m.status("confirmed").requires_evidence
    assert set(CLOSURE_KINDS) == {"mechanism", "slope", "cell"}


def test_harness_shaped_terminal_set():
    m = default_machine({"fixed": {"requires_evidence": True},
                         "wontfix": {"requires_evidence": True}})
    m.check_total()
    assert m.terminal_names == {"fixed", "wontfix"}
