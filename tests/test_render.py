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


def test_a_scaffold_fixture_loses_the_headline_to_a_real_finding(sandbox, store,
                                                                capsys):
    """sig.golf H4: a wiring check scored 15x better than every real scheme and
    held the board's only headline number for the rest of the campaign, so the
    improvement never appeared."""
    from autoresearch import cli
    from autoresearch.runs import RunRecord, append
    make_entry(store, "Q1", scaffold=True)
    make_entry(store, "Q2")
    append(sandbox.paths.runs / "fixture.jsonl",
           RunRecord(metrics={"ops": 1.0, "peak": 1.0}, session="s",
                     provenance={"h": "x"}, entry="Q1", id="fix"))
    append(sandbox.paths.runs / "finding.jsonl",
           RunRecord(metrics={"ops": 10.0, "peak": 1.0}, session="s",
                     provenance={"h": "x"}, entry="Q2", id="find"))
    assert cli.main(["--domain", str(sandbox.paths.root), "board"]) == 0
    out = capsys.readouterr().out
    best = next(line for line in out.splitlines() if line.startswith("best "))
    assert "(Q2)" in best, best
    # named, not dropped: a reader must be able to tell a frontier that did not
    # move from one whose fixture was reclassified
    assert "excluded" in out and "fix" in out and "Q1" in out


def test_the_board_names_the_scaffold_row_it_left_out(sandbox, store):
    from autoresearch.runs import RunRecord
    r = RunRecord(metrics={"ops": 10.0, "peak": 2.0}, session="s",
                  provenance={"h": "x"}, entry="Q1")
    text = render.board(sandbox, store.all(), [r], target=1000.0,
                        best=(20.0, "Q2"), best_metrics=r.metrics,
                        excluded_best=(1.0, r, "scaffold entry Q1 -- a wiring check is not a finding"))
    line = next(line for line in text.splitlines() if line.startswith("excluded"))
    assert "Q1" in line and "not a finding" in line


def test_a_scaffold_entry_says_so_in_its_own_view(sandbox, store):
    make_entry(store, "Q1", scaffold=True)
    make_entry(store, "Q2")
    entries = store.all()
    assert "**Scaffold:**" in render.queue_view(sandbox.tracks["research"],
                                               entries)
    only_q2 = [e for e in entries if e.id == "Q2"]
    assert "Scaffold" not in render.queue_view(sandbox.tracks["research"],
                                               only_q2)


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


def test_graph_html_embeds_full_title_and_summary_for_hover(sandbox, store):
    """The diagram truncates labels; the snapshot page must not truncate the
    payload a reader hovers for. A closed entry summarises by its verdict, an
    open one by its hypothesis claim."""
    close(store, make_entry(store, "Q1",
                            title="x" * 200, hypothesis="the claim"))
    make_entry(store, "Q2", hypothesis="still open, this is why")
    # A summary containing a closing script tag must not break out of the
    # embedded JSON block.
    make_entry(store, "Q3", hypothesis="evil </script> payload")
    page = render.graph_html([sandbox.tracks["research"]], store.all())
    assert "x" * 200 in page            # the full title, untruncated
    assert "still open, this is why" in page
    assert "the claim" not in page      # closed: summary wins over hypothesis
    assert '"status": "confirmed"' in page or '"status":"confirmed"' in page
    assert "<\\/script>" in page and "evil <\\/script> payload" in page


def test_graph_html_covers_every_declared_track(sandbox, store):
    make_entry(store, "Q1")
    make_entry(store, "H1", track="harness")
    page = render.graph_html(list(sandbox.tracks.values()), store.all())
    assert "<h2>Hypothesis Queue</h2>" in page
    assert "<h2>Harness Debt</h2>" in page
    assert '"H1"' in page and '"Q1"' in page
    assert "flowchart LR" in page  # lineage reads left to right


def test_circles_mode_shrinks_nodes_to_ids(sandbox, store):
    """The HTML snapshot renders Obsidian-style: a small id-only circle per
    hypothesis, with the hover tooltip carrying the identity. The generated
    markdown view keeps titled rectangles -- Obsidian and GitHub have no
    hover payload, so the title must stay in the label there."""
    make_entry(store, "Q1", title="a long title that would bloat the node")
    flat = render.graph_view(sandbox.tracks["research"], store.all())
    assert 'Q1["Q1 — a long title' in flat and "((" not in flat
    round = render.graph_view(sandbox.tracks["research"], store.all(),
                              circles=True)
    assert "Q1((Q1))" in round and '["' not in round
    assert "a long title" not in round
    assert round == render.graph_view(sandbox.tracks["research"], store.all(),
                                      circles=True)


# -- what the loop last did (sig.golf H25) -----------------------------------

def _iteration(sandbox, n, **over):
    import json
    record = {"n": n, "started": "2026-01-01T00:00:00+00:00",
              "stop": "native-handoff",
              "stop_detail": "1 assignment(s) outstanding",
              "phases": [{"name": "rank", "did": 3, "error": None},
                         {"name": "qc", "did": 0, "error": "2 problem(s)"}]}
    record.update(over)
    path = sandbox.paths.iterations / f"{n:04d}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record))


def test_the_board_says_what_the_loop_last_did(sandbox, store):
    """Every `ar loop` writes an iteration record and nothing read it back, so a
    returning session could not see that the loop had stopped mid-handoff with an
    assignment outstanding without opening JSON by hand."""
    _iteration(sandbox, 7)
    text = render.board(sandbox, store.all(), [])
    assert "## Iterations" in text and "last 7" in text
    assert "native-handoff" in text
    assert "1 assignment(s) outstanding" in text
    assert "rank(3)" in text                      # only what did something
    assert "problem     qc: 2 problem(s)" in text  # and what went wrong


def test_the_board_says_when_the_loop_has_never_run(sandbox, store):
    text = render.board(sandbox, store.all(), [])
    assert "(none — the loop has not run in this domain)" in text


def test_a_broken_iteration_record_does_not_break_the_board(sandbox, store):
    """The board is the screen a cold session opens; an unreadable iteration
    file is a fact to report, not a reason to fail the whole projection."""
    path = sandbox.paths.iterations / "0003.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    text = render.board(sandbox, store.all(), [])
    assert "## Iterations" in text

def test_the_stop_line_is_the_last_loop_iteration_not_an_out_of_band_capture(
        sandbox, store):
    """A one-phase `skill distil` or `ar research` is recorded out of band with
    `stop: running`. Reporting the newest record as the coordinator's state says
    the loop is running when it stopped at a handoff two records ago."""
    _iteration(sandbox, 7)
    _iteration(sandbox, 8, kind="out-of-band", stop="running",
               stop_detail="", phases=[{"name": "distil", "did": 0, "error": None}])
    text = render.board(sandbox, store.all(), [])
    assert "last loop iteration 7" in text
    assert "out-of-band record(s) since" in text
    assert "native-handoff" in text          # the loop's own outcome
    assert text.count("stop        running") == 0


def test_out_of_band_records_are_counted_not_inferred(sandbox, store):
    """The arithmetic distance between two record numbers is not the number of
    out-of-band records: a deleted or renumbered record in between turned one
    capture into "2" (sig.golf H25)."""
    _iteration(sandbox, 7)
    # 8 is absent on purpose: the gap must not inflate the count
    _iteration(sandbox, 9, kind="out-of-band", stop="running", stop_detail="",
               phases=[{"name": "distil", "did": 0, "error": None}])
    text = render.board(sandbox, store.all(), [])
    assert "1 out-of-band record(s) since" in text
    assert "2 out-of-band" not in text
