import pytest
from conftest import make_entry

from autoresearch.entries import Claim, Result
from autoresearch.rank import Veto, apply_veto, rank


def close(store, entry_id, verdict, kind=None, mechanisms=(), reopen=""):
    from autoresearch.states import default_machine
    m = default_machine()
    e = make_entry(store, entry_id, mechanisms=list(mechanisms))
    e.apply(m, "in-progress", who="s", why="c")
    e.apply(m, verdict, who="s", memo_exists=lambda p: True,
            result=Result(verdict=verdict, memo="inbox/m.md", at="2026-08-31T00:00:00+00:00",
                          session="s", closure_kind=kind, reopen_condition=reopen))
    store.save(e)
    return e


def test_terminal_entries_are_excluded(sandbox, store):
    """H140: a curation ranked a terminal entry first, four hours after it closed."""
    close(store, "Q1", "confirmed")
    make_entry(store, "Q2", impact=1.0)
    r = rank(store.all(), sandbox)
    assert [s.entry_id for s in r.scored] == ["Q2"]
    assert any("terminal" in s.excluded for s in r.excluded)


def test_held_entries_are_excluded(sandbox, store):
    make_entry(store, "Q1", impact=1.0, status="in-progress",
               claim=Claim(session="w", at="2026-08-31T00:00:00+00:00"))
    make_entry(store, "Q2", impact=0.1)
    r = rank(store.all(), sandbox)
    assert [s.entry_id for s in r.scored] == ["Q2"]
    assert "held by w" in r.excluded[0].excluded


def test_mechanism_refutation_hard_excludes_a_sharing_entry(sandbox, store):
    """The closure taxonomy made operational: `mechanism` holds outside the range
    measured, so a queued entry on that mechanism is dead work."""
    close(store, "Q1", "refuted", kind="mechanism", mechanisms=["odc-dead-gates"])
    make_entry(store, "Q2", impact=1.0, mechanisms=["odc-dead-gates"])
    r = rank(store.all(), sandbox)
    assert r.scored == []
    assert any("closure_kind=mechanism" in s.excluded for s in r.excluded)


def test_slope_refutation_only_penalises(sandbox, store):
    """`slope` holds only inside the band measured, so excluding a sharing entry
    would be the over-claim the taxonomy exists to prevent."""
    close(store, "Q1", "refuted", kind="slope", mechanisms=["width-retrade"],
          reopen="measured 30-45 peak only")
    make_entry(store, "Q2", impact=1.0, mechanisms=["width-retrade"])
    make_entry(store, "Q3", impact=1.0)
    r = rank(store.all(), sandbox)
    ids = [s.entry_id for s in r.scored]
    assert set(ids) == {"Q2", "Q3"}
    assert ids[0] == "Q3"                      # Q2 penalised, not removed
    q2 = next(s for s in r.scored if s.entry_id == "Q2")
    assert q2.terms["overlap"] == pytest.approx(0.5)


def test_calibration_pulls_confidence_toward_observed_rate(sandbox, store):
    for i in range(6):                      # six refutations on one mechanism
        close(store, f"Q{i + 1}", "refuted", kind="cell", mechanisms=["m"],
              reopen="one point")
    make_entry(store, "Q10", confidence=0.9, impact=1.0, mechanisms=["m"])
    r = rank(store.all(), sandbox)
    card = r.scored[0]
    assert card.terms["confidence"] < 0.9      # the record disagrees with the filer
    assert r.calibration["m"]["rate"] == 0.0 and r.calibration["m"]["n"] == 6


def test_calibration_is_absent_with_no_history(sandbox, store):
    make_entry(store, "Q1", confidence=0.7, impact=1.0, mechanisms=["fresh"])
    r = rank(store.all(), sandbox)
    assert r.scored[0].terms["confidence"] == pytest.approx(0.7)


def test_cost_beyond_the_remaining_budget_is_excluded(sandbox, store):
    make_entry(store, "Q1", impact=1.0, cost=1000.0)
    make_entry(store, "Q2", impact=0.1, cost=1.0)
    r = rank(store.all(), sandbox, budget_ok=lambda e: e.cost <= 10)
    assert [s.entry_id for s in r.scored] == ["Q2"]
    assert "remaining budget" in r.excluded[0].excluded


def test_ordering_is_by_score_and_explained(sandbox, store):
    make_entry(store, "Q1", confidence=0.5, impact=0.1, cost=1.0)
    make_entry(store, "Q2", confidence=0.5, impact=1.0, cost=1.0)
    r = rank(store.all(), sandbox)
    assert [s.entry_id for s in r.scored] == ["Q2", "Q1"]
    assert "confidence=" in r.explain() and "impact=" in r.explain()


def test_veto_can_reorder_within_the_shortlist(sandbox, store):
    make_entry(store, "Q1", impact=1.0)
    make_entry(store, "Q2", impact=0.1)
    r = rank(store.all(), sandbox)
    out = apply_veto(r, [Veto("Q2", "promote", "the cheap arm de-risks Q1")])
    assert [s.entry_id for s in out.scored] == ["Q2", "Q1"]
    assert any("de-risks" in n for n in out.notes)


def test_veto_cannot_resurrect_a_hard_filtered_entry(sandbox, store):
    """The judge may reorder; it may not overrule a closed mechanism."""
    close(store, "Q1", "refuted", kind="mechanism", mechanisms=["dead"])
    make_entry(store, "Q2", impact=1.0, mechanisms=["dead"])
    make_entry(store, "Q3", impact=0.5)
    r = rank(store.all(), sandbox)
    with pytest.raises(ValueError, match="resurrect a hard-filtered entry"):
        apply_veto(r, [Veto("Q2", "promote", "I think it is still alive")])


def test_veto_requires_a_justification(sandbox, store):
    make_entry(store, "Q1", impact=1.0)
    r = rank(store.all(), sandbox)
    with pytest.raises(ValueError, match="no justification"):
        apply_veto(r, [Veto("Q1", "demote", "  ")])


def test_veto_on_an_unranked_entry_is_refused(sandbox, store):
    make_entry(store, "Q1", impact=1.0)
    r = rank(store.all(), sandbox)
    with pytest.raises(ValueError, match="was not ranked"):
        apply_veto(r, [Veto("Q99", "promote", "because")])


# -- the explore reserve -------------------------------------------------
# score = confidence x impact / cost is expected value per unit cost, which is
# risk-neutral and therefore prefers cheap certain increments over large
# uncertain swings. These fix the reserve that keeps a lane open for the swing.


def _increments(store, n=3):
    """Cheap, likely, small: exactly what the exploit formula loves."""
    for i in range(n):
        make_entry(store, f"Q1{i + 1}", confidence=0.85, impact=0.02, cost=1.0)


def test_explore_reserve_promotes_a_long_shot_the_formula_buries(sandbox, store):
    _increments(store)
    make_entry(store, "Q91", confidence=0.10, impact=0.40, cost=8.0)
    r = rank(store.all(), sandbox, explore_fraction=0.25)
    assert [s.entry_id for s in r.scored][-1] == "Q91"    # last on score
    assert "Q91" in [s.entry_id for s in r.shortlist(3)]  # first on impact


def test_explore_reserve_is_absent_when_the_fraction_is_zero(sandbox, store):
    _increments(store)
    make_entry(store, "Q91", confidence=0.10, impact=0.40, cost=8.0)
    r = rank(store.all(), sandbox, explore_fraction=0.0)
    assert [s.entry_id for s in r.shortlist(3)] == ["Q11", "Q12", "Q13"]


def test_explore_ranks_by_impact_alone(sandbox, store):
    _increments(store)
    make_entry(store, "Q91", confidence=0.30, impact=0.30, cost=8.0)
    make_entry(store, "Q92", confidence=0.05, impact=0.50, cost=8.0)
    r = rank(store.all(), sandbox, explore_fraction=0.25)
    scored = {s.entry_id: s.score for s in r.scored}
    assert scored["Q91"] > scored["Q92"]                   # L2 loses on score
    assert "Q92" in [s.entry_id for s in r.shortlist(3)]  # and wins on impact


def test_explore_reserve_never_resurrects_a_hard_filtered_entry(sandbox, store):
    """The reserve reorders among ranked entries; it is not a second chance at
    the filters. A mechanism-refuted direction is dead however big it looks."""
    close(store, "Q1", "refuted", kind="mechanism", mechanisms=["dead"])
    _increments(store)
    make_entry(store, "Q91", confidence=0.1, impact=1.0, mechanisms=["dead"])
    r = rank(store.all(), sandbox, explore_fraction=0.5)
    assert "Q91" not in [s.entry_id for s in r.shortlist(3)]


def test_a_single_slot_is_never_spent_on_the_explore_lane(sandbox, store):
    _increments(store)
    make_entry(store, "Q91", confidence=0.10, impact=0.40, cost=8.0)
    r = rank(store.all(), sandbox, explore_fraction=0.25)
    assert [s.entry_id for s in r.shortlist(1)] == ["Q11"]


def test_explore_picks_are_marked_and_explained(sandbox, store):
    _increments(store)
    make_entry(store, "Q91", confidence=0.10, impact=0.40, cost=8.0)
    r = rank(store.all(), sandbox, explore_fraction=0.25)
    picked = {s.entry_id: s for s in r.shortlist(3)}
    assert picked["Q91"].explore is True
    assert picked["Q11"].explore is False
    assert "explore" in r.explain()


def test_the_reserve_falls_back_to_exploit_when_there_is_nothing_to_explore(sandbox, store):
    _increments(store, n=2)
    r = rank(store.all(), sandbox, explore_fraction=0.5)
    ids = [s.entry_id for s in r.shortlist(4)]
    assert ids == ["Q11", "Q12"]                           # no gaps, no repeats


def test_an_explore_fraction_that_leaves_no_exploit_lane_is_refused(sandbox, store):
    with pytest.raises(ValueError, match="explore_fraction"):
        rank(store.all(), sandbox, explore_fraction=1.0)
