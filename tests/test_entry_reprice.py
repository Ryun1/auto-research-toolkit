"""`ar entry reprice` — the out-of-band price correction.

Filing-time confidence/impact/cost are guesses. When evidence settles late
(an external assignment confirmed, a sibling closure killed a shared
mechanism) the prices are wrong while the entry is still open — and before
the shared writer existed, an out-of-band session had no path except a
hand-edit of the YAML, the exact move H8 forbids. One writer, both callers:
the loop's curator phase and this command.
"""
import pytest

from autoresearch import cli
from autoresearch.entries import reprice
from autoresearch.errors import AutoresearchError
from conftest import close, make_entry


def test_reprice_moves_the_prices_and_records_the_event(sandbox, store):
    """The curator-native-1 shape: a filing-time 0.5 corrected after the
    evidence settled."""
    entry = make_entry(store, "Q1", confidence=0.5, impact=0.0, cost=1.0)
    moved = reprice(store, entry, machine=sandbox.track_for("Q1").machine,
                    changes={"confidence": 0.85, "impact": 0.3},
                    session="curator-native-1", why="settled confirmed by 4438427a")
    assert moved == ["confidence", "impact"]
    saved = store.load("Q1")
    assert saved.confidence == 0.85 and saved.impact == 0.3 and saved.cost == 1.0
    event = saved.history[-1]
    assert event.kind == "repriced" and event.who == "curator-native-1"
    assert "4438427a" in event.detail
    # `updated` is what staleness measures; a re-price is a pricing.
    assert saved.updated == event.at and saved.updated >= saved.created


def test_a_terminal_entry_is_never_repriced(sandbox, store):
    """H140: a terminal verdict is the record's last word on those numbers."""
    entry = close(sandbox, store, make_entry(store, "Q1"))
    with pytest.raises(AutoresearchError, match="H140"):
        reprice(store, entry, machine=sandbox.track_for("Q1").machine,
                changes={"confidence": 0.9}, session="s", why="it moved")


def test_a_reprice_without_a_reason_is_refused(sandbox, store):
    entry = make_entry(store, "Q1")
    with pytest.raises(AutoresearchError, match="reason"):
        reprice(store, entry, machine=sandbox.track_for("Q1").machine,
                changes={"confidence": 0.9}, session="s", why="")


def test_a_reprice_that_moves_nothing_is_refused(sandbox, store):
    entry = make_entry(store, "Q1")
    before = entry.history
    with pytest.raises(AutoresearchError, match="nothing to reprice"):
        reprice(store, entry, machine=sandbox.track_for("Q1").machine,
                changes={}, session="s", why="n/a")
    assert store.load("Q1").history == before, "no event for a no-op"


def test_confidence_is_a_probability_not_a_percentage(sandbox, store):
    """The score multiplies these numbers; a typo'd 85 must reorder nothing."""
    entry = make_entry(store, "Q1")
    with pytest.raises(AutoresearchError, match=r"\[0, 1\]"):
        reprice(store, entry, machine=sandbox.track_for("Q1").machine,
                changes={"confidence": 85.0}, session="s", why="typo")


def test_the_command_shares_the_writer(sandbox, store, capsys):
    make_entry(store, "Q1", confidence=0.5)
    assert cli.main(["--domain", str(sandbox.paths.root), "--session", "hand",
                     "entry", "reprice", "Q1", "--confidence", "0.9",
                     "--why", "external assignment settled confirmed"]) == 0
    assert "Q1 repriced: confidence" in capsys.readouterr().out
    saved = store.load("Q1")
    assert saved.confidence == 0.9
    assert saved.history[-1].who == "hand" and saved.history[-1].kind == "repriced"


def test_the_command_refuses_a_closed_entry_loudly(sandbox, store, capsys):
    close(sandbox, store, make_entry(store, "Q1"))
    assert cli.main(["--domain", str(sandbox.paths.root), "entry", "reprice",
                     "Q1", "--confidence", "0.9", "--why", "late"]) == 2
    assert "H140" in capsys.readouterr().err


def test_the_loops_curator_phase_uses_the_same_writer(sandbox, store):
    """An in-loop re-price and an out-of-band one leave the same trace, so
    the guards cannot diverge either."""
    from autoresearch.driver.brain import Role, ScriptedBrain
    from autoresearch.driver.loop import Coordinator
    make_entry(store, "Q1", confidence=0.5)
    brain = ScriptedBrain({
        **{role: (lambda b: []) for role in (Role.GENERATOR, Role.JUDGE)},
        Role.WORKER: lambda b: {"verdict": "inconclusive", "summary": "n/a",
                                "verification": {"reread": True,
                                                 "claims_checked": ["summary"],
                                                 "corrections": []}},
        Role.CURATOR: lambda b: {"reprice": [
            {"entry_id": "Q1", "confidence": 0.8,
             "why": "sibling closure moved the shared mechanism"}],
            "notes": []},
        Role.QC: lambda b: {"problems": [], "harness_debt": [],
                            "verdict": "clean"}})
    Coordinator(sandbox, brain).run_iteration(1)
    saved = store.load("Q1")
    assert saved.confidence == 0.8
    event = saved.history[-1]
    assert event.kind == "repriced" and "shared mechanism" in event.detail
