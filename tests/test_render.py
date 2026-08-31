from conftest import make_entry

from autoresearch import render
from autoresearch.entries import Result
from autoresearch.states import default_machine


def close(store, entry, **over):
    m = default_machine()
    entry.apply(m, "in-progress", who="s", why="c")
    data = dict(verdict="confirmed", memo="inbox/m.md", at="2026-08-31T00:00:00+00:00",
                session="s", summary="it moved 15%")
    data.update(over)
    entry.apply(m, data["verdict"], who="s", result=Result(**data),
                memo_exists=lambda p: True)
    store.save(entry)
    return entry


def test_view_reports_counts_including_zero(sandbox, store):
    """H81/H96: a board saying 'no live claims' must not be indistinguishable
    from a board that read nothing."""
    text = render.queue_view(sandbox.tracks["research"], store.all())
    assert "**0 entries read: 0 open, 0 closed.**" in text
    assert "_No closed entries._" in text and "_No open entries._" in text


def test_closed_table_is_pointers_only(sandbox, store):
    close(store, make_entry(store, "Q1"))
    text = render.queue_view(sandbox.tracks["research"], store.all())
    assert "| Q1 | confirmed |" in text and "`inbox/m.md`" in text
    assert "**1 entries read: 0 open, 1 closed.**" in text


def test_view_carries_the_do_not_edit_banner(sandbox, store):
    text = render.queue_view(sandbox.tracks["research"], store.all())
    assert text.startswith(render.BANNER)


def test_check_views_detects_a_hand_edit(sandbox, store):
    """Invariant 1: a generated view that someone edited is a build failure, not
    a divergence discovered four hours later (H72, H140)."""
    make_entry(store, "Q1")
    render.write_views(sandbox, store.all())
    assert render.check_views(sandbox, store.all()) == []
    path = sandbox.paths.root / sandbox.tracks["research"].view
    path.write_text(path.read_text() + "\nhand edit\n")
    problems = render.check_views(sandbox, store.all())
    assert len(problems) == 1 and "generated view" in problems[0]


def test_check_views_detects_a_never_rendered_view(sandbox, store):
    problems = render.check_views(sandbox, store.all())
    assert any("never been rendered" in p for p in problems)


def test_board_distinguishes_empty_from_all_held(sandbox, store):
    make_entry(store, "Q1")
    text = render.board(sandbox, store.all(), [], target=1000.0)
    assert "1 entries read" in text and "queued heads (1 queued)" in text
    text = render.board(sandbox, [], [], target=1000.0)
    assert "the queue is empty, not merely all held" in text


def test_board_shows_goal_met(sandbox, store):
    from autoresearch.runs import RunRecord
    r = RunRecord(metrics={"ops": 10.0, "peak": 2.0}, session="s",
                  provenance={"h": "x"}, entry="Q1")
    text = render.board(sandbox, store.all(), [r], target=1000.0, best=(20.0, "Q1"))
    assert "GOAL MET" in text
