import pytest

from autoresearch.entries import Claim, Result
from autoresearch.rank import Veto, apply_veto, rank
from conftest import make_entry


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


# -- the risk dial --------------------------------------------------------
# score = confidence x impact / cost is expected value per unit cost, which is
# risk-neutral and therefore prefers cheap certain increments over large
# uncertain swings. The dial is structural, not a priced reserve: a share of
# every shortlist goes to novel branches (no parent), the rest to refining the
# incumbent (a branch off work already in the record).

def _increments(store, n=3):
    """Cheap, likely, small roots: exactly what the exploit formula loves."""
    for i in range(n):
        make_entry(store, f"Q1{i + 1}", confidence=0.85, impact=0.02, cost=1.0)


def _branches(store, n=2, parent="Q10"):
    """Refinements of an open root: the incumbent partition. The parent root
    itself scores terribly, so the novel partition's picks are the increments,
    not its parent."""
    make_entry(store, parent, confidence=0.01, impact=0.01, cost=8.0)
    for i in range(n):
        make_entry(store, f"Q9{i + 1}", parent=parent,
                   confidence=0.10, impact=0.40, cost=8.0)


def test_the_dial_splits_the_shortlist_between_root_and_branch(sandbox, store):
    _increments(store)
    _branches(store)
    r = rank(store.all(), sandbox, risk=0.5)
    picks = r.shortlist(3)
    # 50/50 of 3 rounds to 2 novel slots, 1 incumbent slot; within each
    # partition the score decides.
    assert [s.entry_id for s in picks] == ["Q11", "Q12", "Q91"]
    assert next(s for s in picks if s.entry_id == "Q91").novel is False


def test_risk_zero_spends_every_slot_on_the_incumbent(sandbox, store):
    _increments(store)
    _branches(store)
    r = rank(store.all(), sandbox, risk=0.0)
    picks = r.shortlist(2)
    assert [s.entry_id for s in picks] == ["Q91", "Q92"]
    assert all(not s.novel for s in picks)


def test_risk_one_spends_every_slot_on_novel_branches(sandbox, store):
    """Both ends of the dial are legitimate stances, not degenerate configs."""
    _increments(store)
    _branches(store)
    r = rank(store.all(), sandbox, risk=1.0)
    picks = r.shortlist(3)
    assert [s.entry_id for s in picks] == ["Q11", "Q12", "Q13"]
    assert all(s.novel for s in picks)


def test_an_unfillable_novel_share_returns_to_the_incumbent(sandbox, store):
    _branches(store)
    close(store, "Q10", "confirmed")     # the parent root leaves the queue
    r = rank(store.all(), sandbox, risk=1.0)   # wants only roots; none remain
    picks = r.shortlist(2)
    assert [s.entry_id for s in picks] == ["Q91", "Q92"]


def test_an_unfillable_incumbent_share_backfills_from_novel(sandbox, store):
    """A stance the record cannot support fills the remainder from the other
    partition: a short queue is never shortlisted below `k` for want of a
    branch."""
    _increments(store)
    _branches(store, n=1)
    r = rank(store.all(), sandbox, risk=0.0)   # wants only branches; one exists
    picks = r.shortlist(3)
    assert [s.entry_id for s in picks] == ["Q11", "Q12", "Q91"]


def test_a_single_slot_is_never_spent_on_a_partition(sandbox, store):
    """With k=1 the dial has nothing to split; applying it literally would send
    every one-slot iteration to the same partition -- a permanent bias, not a
    stance."""
    _increments(store)
    _branches(store)
    r = rank(store.all(), sandbox, risk=0.5)
    assert [s.entry_id for s in r.shortlist(1)] == ["Q11"]


def test_a_queue_no_larger_than_the_shortlist_has_nothing_to_split(sandbox, store):
    """Everything is dispatched either way, so marking partitions would tell
    the judge an entry 'sits low on score by design' about an entry that is
    simply third."""
    _increments(store)
    r = rank(store.all(), sandbox, risk=0.5)
    picks = r.shortlist(3)
    assert [c.entry_id for c in picks] == ["Q11", "Q12", "Q13"]


def test_the_dial_never_resurrects_a_hard_filtered_entry(sandbox, store):
    """The dial splits ranked entries; it is not a second chance at the
    filters. A mechanism-refuted direction is dead however novel it looks."""
    close(store, "Q1", "refuted", kind="mechanism", mechanisms=["dead"])
    _branches(store)
    make_entry(store, "Q93", confidence=0.1, impact=1.0, mechanisms=["dead"])
    r = rank(store.all(), sandbox, risk=0.5)
    assert "Q93" not in [s.entry_id for s in r.shortlist(2)]


def test_a_dropped_entry_does_not_return_through_the_dial(sandbox, store):
    """`drop` is a demotion out of the shortlist: it moves the card to the tail
    of `scored`, which is the tail of its partition, so the dial cannot pick it
    back."""
    _increments(store)
    _branches(store)
    r = rank(store.all(), sandbox, risk=0.5)
    assert "Q91" in [s.entry_id for s in r.shortlist(3)]
    out = apply_veto(r, [Veto("Q91", "drop", "measured last week under another id")])
    ids = [s.entry_id for s in out.shortlist(3)]
    assert "Q91" not in ids, ids
    assert ids == ["Q11", "Q12", "Q92"]


def test_a_demoted_entry_does_not_return_through_the_dial(sandbox, store):
    _increments(store)
    _branches(store)
    r = rank(store.all(), sandbox, risk=0.5)
    out = apply_veto(r, [Veto("Q91", "demote", "the adder rewrite is Q92's premise")])
    assert "Q91" not in [s.entry_id for s in out.shortlist(3)]


def test_the_dial_is_marked_and_explained(sandbox, store):
    _increments(store)
    _branches(store)
    r = rank(store.all(), sandbox, risk=0.5)
    assert "[novel]" in r.explain() and "[branch]" in r.explain()
    assert "risk 50%" in r.explain()


def test_a_bad_risk_is_refused_as_an_autoresearch_error(sandbox, store):
    """`errors.py`: one error type per refusal reason, so no caller has to
    string-match. A bare ValueError escapes `ar`'s handler as a traceback."""
    from autoresearch.errors import ConfigError
    with pytest.raises(ConfigError, match=r"\brisk\b"):
        rank(store.all(), sandbox, risk=1.5)


def test_shortlist_never_returns_more_than_k_cards(sandbox, store):
    """The dial is clamped so the shortlist stays exactly k cards even when a
    stance is degenerate; a direct Ranking caller gets the same guarantee the
    validated config path gets."""
    _increments(store)
    _branches(store)
    r = rank(store.all(), sandbox, risk=0.5)
    r.risk = 2.0               # bypass rank()'s validation
    assert len(r.shortlist(3)) == 3


def test_hardware_qualified_candidate_still_passes_mechanism_filter(sandbox, store):
    from autoresearch.hardware import Host, Requirement
    sandbox.hardware["small"] = Requirement(name="small", min_memory_gb=1)
    close(store, "Q1", "refuted", kind="mechanism", mechanisms=["dead"])
    make_entry(store, "Q2", hardware="small", mechanisms=["dead"], impact=1.0)
    host = Host(os="Darwin", arch="arm64", chip="M5", vendor="apple",
                cpu_threads=10, memory_gb=16, memory_available_gb=8)
    ranking = rank(store.all(), sandbox, host=host)
    assert not ranking.scored
    assert "Q1" in next(c.excluded for c in ranking.excluded if c.entry_id == "Q2")


@pytest.mark.parametrize("kind", ["mechanism", "cell"])
def test_scoped_closures_require_all_dimensions_and_parameter_types(sandbox, store, kind):
    from autoresearch.entries import Applicability
    from autoresearch.states import default_machine
    scope = dict(baseline="pristine", source_revision="abc", hardware="RTX",
                 workload="uniform", parameters={"batch": 16})
    closed = make_entry(store, "Q1", status="in-progress", mechanisms=["m"],
                        context=Applicability(**scope))
    closed.apply(default_machine(), "refuted", "s", result=Result(
        "refuted", "memo", "2026-01-01", "s", closure_kind=kind,
        reopen_condition="new cell"))
    store.save(closed)
    make_entry(store, "Q2", mechanisms=["m"], context=Applicability(**scope), impact=1)
    for index, change in enumerate((
            {"hardware": "M5"}, {"source_revision": "def"}, {"workload": "mixed"},
            {"baseline": "shipped"}, {"parameters": {"batch": "16"}},
            {"parameters": {}}, {"hardware": ""}), start=3):
        make_entry(store, f"Q{index}", mechanisms=["m"],
                   context=Applicability(**(scope | change)), impact=1, confidence=0.7)
    ranking = rank(store.all(), sandbox)
    scored = {card.entry_id: card for card in ranking.scored}
    assert set(scored) >= {f"Q{i}" for i in range(3, 10)}
    for i in range(3, 10):
        assert scored[f"Q{i}"].terms["overlap"] == 1
        assert scored[f"Q{i}"].terms["confidence"] == 0.7
    if kind == "mechanism":
        assert "Q2" not in scored
    else:
        assert scored["Q2"].terms["overlap"] == 0.5


@pytest.mark.parametrize("disposition", ["superseded", "already-shipped"])
def test_nonexperiment_disposition_is_not_negative_evidence(sandbox, store, disposition):
    from autoresearch.states import default_machine
    entry = make_entry(store, "Q1", mechanisms=["m"], status="in-progress")
    entry.apply(default_machine(), "refuted", "s", result=Result(
        "refuted", "memo", "2026-01-01", "s", disposition=disposition))
    store.save(entry)
    make_entry(store, "Q2", mechanisms=["m"], impact=1, confidence=0.8)
    ranking = rank(store.all(), sandbox)
    assert [card.entry_id for card in ranking.scored] == ["Q2"]
    assert ranking.scored[0].terms["confidence"] == 0.8
    assert ranking.scored[0].terms["overlap"] == 1
    assert ranking.calibration == {}


def test_newly_scoped_closure_does_not_hide_later_matching_closure(sandbox, store):
    from autoresearch.entries import Applicability
    first = close(store, "Q1", "refuted", kind="mechanism", mechanisms=["m"])
    first.result.applicability = Applicability(hardware="M5")
    store.save(first)
    second = close(store, "Q2", "refuted", kind="mechanism", mechanisms=["m"])
    second.result.applicability = Applicability(hardware="RTX")
    store.save(second)
    make_entry(store, "Q3", mechanisms=["m"], context=Applicability(hardware="RTX"))
    ranking = rank(store.all(), sandbox)
    assert "Q2" in next(card.excluded for card in ranking.excluded if card.entry_id == "Q3")


