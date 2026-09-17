"""Migration is the one place core parses prose, and it must not reconcile
silently: two encodings of one fact diverge, and the divergences are the audit."""
import pytest

from autoresearch.migrate import migrate

QUEUE = """# Hypothesis Queue

## Closed

| Entry | Status | Date | Evidence |
|---|---|---|---|
| Q01 | refuted | 2026-08-23 | `inbox/q01.md` |
| Q02 | confirmed | 2026-08-24 | `inbox/q02.md` |

## Qnn — <short name>

- **Status:** queued · YYYY-MM-DD

## Q01 — the first idea

- **Lane:** A (bit-exact)
- **Status:** refuted · 2026-08-23 · loop-otter
- **Hypothesis:** the codec has a dead path
- **Prediction:** dead ops fall. **Confirmed if** >100 drop; **refuted if** 0 drop.
- **Null-check requirement:** two. the census must be re-run at head
- **Source:** `inbox/prior.md`; Q00
- **Result:** Refuted: 0 of 949,081 sites are dead.

## Q02 — the second idea

- **Status:** confirmed · 2026-08-24 · loop-vole
- **Hypothesis:** folding is free
- **Known trap:** the fold window is seed-locked
- **Result:** Confirmed at -702 T.

## Q03 — still open

- **Status:** queued · 2026-08-25
- **Hypothesis:** unknown
"""


@pytest.fixture
def corpus(tmp_path):
    (tmp_path / "inbox").mkdir()
    (tmp_path / "inbox" / "q01.md").write_text("evidence")
    (tmp_path / "inbox" / "q02.md").write_text("evidence")
    path = tmp_path / "queue.md"
    path.write_text(QUEUE)
    return path


def result(corpus):
    return migrate(corpus, track="research", root=corpus.parent,
                   terminal=("confirmed", "refuted", "blocked"))


def test_template_section_is_skipped(corpus):
    ids = [e.id for e in result(corpus).entries]
    assert ids == ["Q01", "Q02", "Q03"]      # `Qnn — <short name>` is not an entry


def test_status_date_and_session_are_lifted(corpus):
    e = {x.id: x for x in result(corpus).entries}["Q01"]
    assert e.status == "refuted"
    assert e.result.session == "loop-otter" and e.result.at.startswith("2026-08-23")


def test_evidence_comes_from_the_closed_table(corpus):
    e = {x.id: x for x in result(corpus).entries}["Q02"]
    assert e.result.memo == "inbox/q02.md"


def test_sources_extract_references_not_prose_fragments(corpus):
    """Splitting the Source line on punctuation turned one sentence into eight
    fragments of English and no usable path. A typed field carries what a tool
    can follow; the raw line survives in `body`."""
    e = {x.id: x for x in result(corpus).entries}["Q01"]
    assert e.sources == ["inbox/prior.md", "Q00"]   # paths and entry ids, both followable


def test_registered_bar_is_lifted_out_of_the_prediction(corpus):
    """The bar was registered inside prose; lifting it into its own field is why
    a worker can be told not to move it."""
    e = {x.id: x for x in result(corpus).entries}["Q01"]
    assert "Confirmed if" in e.bar and "refuted if" in e.bar


def test_lane_becomes_a_tag_never_a_mechanism(corpus):
    """`mechanisms` is the EXCLUSION vocabulary: a `mechanism`-kind refutation
    sharing a tag hard-excludes an entry. Putting the lane there made a single
    refutation close an entire lane of the real corpus and left the research
    queue unrankable. A lane is a category; a refutation does not close one."""
    entries = {x.id: x for x in result(corpus).entries}
    assert entries["Q01"].tags == ["lane:a"]
    assert entries["Q01"].mechanisms == []
    assert entries["Q02"].tags == []                # no Lane line, no guess


def test_nothing_that_ranking_sorts_on_is_inferred(corpus):
    e = {x.id: x for x in result(corpus).entries}["Q01"]
    assert (e.confidence, e.impact, e.cost, e.mechanisms) == (0.5, 0.0, 1.0, [])


def test_the_whole_prose_section_is_preserved_verbatim(corpus):
    """56 entries in the source corpus carry a **Known trap:** and 8 a
    **Falsifier:**; a migration that dropped them would lose the reasoning the
    never-delete rule exists to preserve."""
    e = {x.id: x for x in result(corpus).entries}["Q02"]
    assert "Known trap" in e.body and "seed-locked" in e.body


def test_open_entries_are_reported_as_unpriced(corpus):
    assert result(corpus).unpriced == ["Q03"]


def test_missing_evidence_is_reported_not_swallowed(tmp_path):
    path = tmp_path / "queue.md"
    path.write_text(QUEUE)                       # inbox/ not created
    out = migrate(path, track="research", root=tmp_path,
                  terminal=("confirmed", "refuted", "blocked"))
    assert len(out.missing_evidence) == 2


def test_a_status_disagreement_is_reported(tmp_path):
    (tmp_path / "inbox").mkdir()
    (tmp_path / "inbox" / "q01.md").write_text("e")
    (tmp_path / "inbox" / "q02.md").write_text("e")
    path = tmp_path / "queue.md"
    path.write_text(QUEUE.replace("| Q01 | refuted |", "| Q01 | confirmed |"))
    out = migrate(path, track="research", root=tmp_path,
                  terminal=("confirmed", "refuted", "blocked"))
    assert any(d.field == "status" and d.entry_id == "Q01" for d in out.disagreements)


def test_a_parse_that_finds_nothing_is_an_error_not_an_empty_queue(tmp_path):
    """H81: swapping the em dash took every reader from 80 sections to zero, and
    zero matches reported as a clean pass."""
    path = tmp_path / "queue.md"
    path.write_text(QUEUE.replace("—", "-"))     # en/hyphen instead of em dash
    with pytest.raises(ValueError, match="zero matches is a parse failure"):
        migrate(path, track="research", root=tmp_path, terminal=("refuted",))


def test_reopened_claim_does_not_manufacture_a_disagreement(tmp_path):
    (tmp_path / "inbox").mkdir()
    for n in ("q01", "q02"):
        (tmp_path / "inbox" / f"{n}.md").write_text("e")
    claims = tmp_path / "claims"
    claims.mkdir()
    (claims / "Q01.json").write_text(
        '{"hypothesis": "Q01", "session": "s", "claimed_at": "2026-08-23T00:00:00+00:00",'
        ' "verdict": "confirmed", "reopened_at": "2026-08-24T00:00:00+00:00",'
        ' "reopened_from": "confirmed", "reopened_why": "premise discharged"}')
    path = tmp_path / "queue.md"
    path.write_text(QUEUE)
    out = migrate(path, track="research", root=tmp_path, claims_root=claims,
                  terminal=("confirmed", "refuted", "blocked"))
    assert out.disagreements == []
    e = {x.id: x for x in out.entries}["Q01"]
    assert any(ev.kind == "reopened" and "premise discharged" in ev.detail
               for ev in e.history)


def test_unknown_track_is_refused_before_migration(sandbox, capsys):
    from autoresearch import cli

    source = sandbox.paths.root / "migration-source.md"
    source.write_text("## Q01 — imported idea\n\n- **Status:** queued\n")
    args = ["--domain", str(sandbox.paths.root), "migrate",
            "--view", str(source), "--track", "bogus"]
    assert cli.main(args) == 2
    assert "bogus" in capsys.readouterr().err
    assert not list(sandbox.paths.entries.glob("*.yaml"))
    assert cli.main([*args[:-1], "research"]) == 0
    from autoresearch.entries import Store
    assert Store(sandbox.paths.entries).load("Q01").title == "imported idea"


@pytest.mark.parametrize("dry_run", [False, True])
def test_migration_collision_preserves_all_existing_records(sandbox, dry_run):
    from autoresearch import cli
    from autoresearch.entries import Entry, Store

    store = Store(sandbox.paths.entries)
    path = store.save(Entry(id="Q2", track="research", title="Current finding"))
    before = path.read_bytes()
    source = sandbox.paths.root / "migration-source.txt"
    source.write_text("## Q1 — new idea\n\n- **Status:** queued\n\n"
                      "## Q2 — obsolete idea\n\n- **Status:** queued\n")
    args = ["--domain", str(sandbox.paths.root), "migrate",
            "--track", "research", "--view", str(source)]
    if dry_run:
        args.append("--dry-run")
    assert cli.main(args) == 2
    assert path.read_bytes() == before
    assert not store.exists("Q1")
