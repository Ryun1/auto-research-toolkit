import re

from autoresearch import render
from autoresearch.entries import Result
from autoresearch.states import default_machine
from conftest import make_entry


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
    text = render.board(sandbox, store.all(), [r], target=1000.0,
                        best=(20.0, "Q1"), best_metrics=r.metrics)
    assert "GOAL MET" in text


def test_board_goal_met_requires_stop_when(sandbox, store):
    from autoresearch.runs import RunRecord

    sandbox.goal.stop_when = "objective <= target * 0.99"
    r = RunRecord(metrics={"ops": 10.0, "peak": 9.95}, session="s",
                  provenance={"h": "x"}, entry="Q1")
    text = render.board(sandbox, store.all(), [r], target=100.0,
                        best=(99.5, "Q1"), best_metrics=r.metrics)
    assert "GOAL MET" not in text
    r.metrics["peak"] = 9.9
    text = render.board(sandbox, store.all(), [r], target=100.0,
                        best=(99.0, "Q1"), best_metrics=r.metrics)
    assert "GOAL MET" in text


def test_unadopted_domain_is_pointed_at_migrate_not_render(sandbox, store):
    """An empty store with a source document means "not adopted yet", not
    "forgot to re-render". Pointing the first case at `ar render` sends the
    reader the wrong way: rendering an empty store writes an empty queue and
    looks like it worked."""
    sandbox.tracks["research"].migrate_from = "docs/log/Legacy Queue.md"
    problems = render.check_views(sandbox, store.all())
    assert any("run `ar migrate` first" in p for p in problems)


def test_populated_track_missing_a_view_is_pointed_at_render(sandbox, store):
    sandbox.tracks["research"].migrate_from = "docs/log/Legacy Queue.md"
    make_entry(store, "Q1")
    problems = render.check_views(sandbox, store.all())
    assert any("has never been rendered" in p and "Hypothesis" in p
               for p in problems)


def _memo_files(sandbox, *names):
    for name in names:
        (sandbox.paths.memos / name).write_text("evidence")


def test_memos_index_reports_cited_and_uncited(sandbox, store):
    close(store, make_entry(store, "Q1"))
    make_entry(store, "Q2", sources=["inbox/extra.md; other/ref.py"])
    _memo_files(sandbox, "m.md", "extra.md", "orphan.md")
    text = render.memos_index_view(sandbox, store.all())
    assert "**3 files read: 2 cited by an entry, 1 not referenced by any entry.**" in text
    assert "| `m.md` | Q1 (confirmed) |" in text
    assert "| `extra.md` | Q2 (queued) |" in text
    assert text.rstrip().endswith("- `orphan.md`")


def test_memos_index_excludes_itself_from_its_own_listing(sandbox, store):
    _memo_files(sandbox, "a.md")
    render.write_views(sandbox, store.all())
    assert (sandbox.paths.memos / render.MEMOS_INDEX).exists()
    text = render.memos_index_view(sandbox, store.all())
    assert "INDEX.md" not in text and "**1 files read: 0 cited" in text


def test_check_views_flags_a_hand_edited_memos_index(sandbox, store):
    _memo_files(sandbox, "m.md")
    render.write_views(sandbox, store.all())
    assert render.check_views(sandbox, store.all()) == []
    path = sandbox.paths.memos / render.MEMOS_INDEX
    path.write_text(path.read_text() + "\nhand edit\n")
    problems = render.check_views(sandbox, store.all())
    assert any("inbox/INDEX.md" in p and "generated view" in p
               for p in problems)


def test_no_memos_means_no_index_and_no_problem(sandbox, store):
    """A domain with an empty memos directory gets no view at all: adoption
    must not turn into a validation failure (same reasoning as migrate)."""
    assert not (sandbox.paths.memos / render.MEMOS_INDEX).exists()
    render.write_views(sandbox, store.all())
    assert not (sandbox.paths.memos / render.MEMOS_INDEX).exists()
    assert not any("INDEX.md" in p for p in render.check_views(sandbox, store.all()))


def test_graph_view_draws_lineage_status_and_links(sandbox, store):
    """The graph view is the Obsidian promise: the picture is drawn from the
    links the records already carry -- parent, supersedes, related -- never
    maintained beside them."""
    root = make_entry(store, "Q1", title='the "quoted" title')
    make_entry(store, "Q2", parent="Q1", kind="debug")
    make_entry(store, "Q3", related=["Q1"], supersedes=["Q2"])
    close(store, root)
    text = render.graph_view(sandbox.tracks["research"], store.all())
    assert text.startswith(render.BANNER) and "flowchart TD" in text
    assert '    Q1["Q1 — the \'quoted\' title"]' in text
    assert '    Q1 -->|"debug"| Q2' in text
    assert '    Q2 ==>|"superseded by"| Q3' in text
    assert "    Q1 <-.->|related| Q3" in text
    assert "    classDef s_confirmed fill:#d3f0d3" in text
    assert "    classDef s_queued fill:#e8e8e8" in text


def test_graph_view_is_deterministic_and_skips_cross_track_edges(sandbox, store):
    """Same records, byte-identical markdown (check_views compares bytes); a
    cross-track parent is skipped rather than silently reaching into another
    track's records."""
    make_entry(store, "Q1", parent="H1")
    make_entry(store, "H1", track="harness")
    first = render.graph_view(sandbox.tracks["research"], store.all())
    assert first == render.graph_view(sandbox.tracks["research"], store.all())
    assert "H1" not in first and "Q1" in first
    harness = render.graph_view(sandbox.tracks["harness"], store.all())
    assert "H1" in harness and "Q1" not in harness


def test_graph_view_writes_and_checks_like_any_view(sandbox, store):
    sandbox.tracks["research"].graph_view = "docs/log/Hypothesis Graph.md"
    make_entry(store, "Q1")
    written = render.write_views(sandbox, store.all())
    assert sandbox.paths.root / "docs/log/Hypothesis Graph.md" in written
    assert render.check_views(sandbox, store.all()) == []
    path = sandbox.paths.root / sandbox.tracks["research"].graph_view
    path.write_text(path.read_text() + "\nhand edit\n")
    problems = render.check_views(sandbox, store.all())
    assert len(problems) == 1 and "Hypothesis Graph.md" in problems[0]


def test_graph_view_never_rendered_is_reported(sandbox, store):
    sandbox.tracks["research"].graph_view = "docs/log/Hypothesis Graph.md"
    make_entry(store, "Q1")
    problems = render.check_views(sandbox, store.all())
    assert any("Hypothesis Graph.md" in p and "never been rendered" in p
               for p in problems)


def test_no_graph_view_declared_means_no_graph_and_no_problem(sandbox, store):
    sandbox.tracks["research"].graph_view = ""
    make_entry(store, "Q1")
    assert not any("Hypothesis Graph" in p
                   for p in render.check_views(sandbox, store.all()))


def test_many_unlinked_entries_are_bucketed_by_status(sandbox, store):
    """dagre lays unlinked nodes in one horizontal line, which at corpus
    scale is a diagram thousands of screens wide (measured on the QSB
    record: 157,000px for 256 unlinked entries). Past BUCKET_MIN the loose
    entries go into status subgraphs, chained vertically -- and nothing is
    dropped or drawn twice."""
    make_entry(store, "Q1", parent="Q2", kind="improve")
    make_entry(store, "Q2")
    close(store, make_entry(store, "Q10"))
    for i in range(20, 40):
        make_entry(store, f"Q{i}", status="refuted")
    for i in range(40, 60):
        make_entry(store, f"Q{i}")
    text = render.graph_view(sandbox.tracks["research"], store.all())
    assert 'subgraph s_queued_bucket["queued — 20 unlinked"]' in text
    assert 'subgraph s_refuted_bucket["refuted — 20 unlinked"]' in text
    assert 'subgraph s_confirmed_bucket["confirmed — 1 unlinked"]' in text
    assert "Q40 ~~~ Q41" in text and "Q20 ~~~ Q21" in text
    # The linked pair stays in the main flow, outside every bucket.
    assert '    Q2 -->|"improve"| Q1' in text
    node_ids = re.findall(r"^\s+(Q\d+)\[", text, re.M)
    assert sorted(node_ids) == sorted(
        [f"Q{i}" for i in (list(range(20, 40)) + list(range(40, 60)))]
        + ["Q1", "Q2", "Q10"])
    again = render.graph_view(sandbox.tracks["research"], store.all())
    assert again == text


def test_a_referenced_entry_is_not_bucketed_twice(sandbox, store):
    """Q4 appears only as the target of Q5's `supersedes` -- its own record
    carries no link -- so it must land in the main flow, never in a bucket
    (which would draw the node twice)."""
    for i in range(20, 40):
        make_entry(store, f"Q{i}")
    make_entry(store, "Q4", status="refuted")
    make_entry(store, "Q5", supersedes=["Q4"])
    text = render.graph_view(sandbox.tracks["research"], store.all())
    assert 'Q4 ==>|"superseded by"| Q5' in text
    assert text.count('    Q4["Q4 —') == 1
    assert "s_refuted_bucket" not in text  # one refuted entry is below BUCKET_MIN
