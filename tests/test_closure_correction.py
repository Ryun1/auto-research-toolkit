"""A closure's own text must be correctable from a terminal state (QSB H82).

The entry this reproduces is real: a closure published a gzip figure 4.3x off,
and the figure could not be corrected through any sanctioned verb. `close`
refused a terminal-to-terminal transition, `claim` refused a terminal entry,
`release` needed a claim to release, and `amend` reached every field of the
entry except the two that carry the verdict's meaning. The only routes left were
a hand-edit of the YAML -- which the domain rules forbid and `ar validate`
rejects -- or hoping a reader looked at `observed` instead of the rendered row,
which is built from the summary.

So a wrong figure outlived the session that measured it. These tests pin the
route that fixes that: amend the closure's text, keep the status terminal, and
leave the verdict alone.

The CLI's own refusal exit is 2, not 1: a refused command is not a clean run.
"""
import json

import pytest

from autoresearch import cli
from autoresearch.entries import Store
from conftest import close, make_entry

#: `cli.main`'s exit code for a refused command.
REFUSED = 2


def _ar(sandbox, *args) -> int:
    return cli.main(["--domain", str(sandbox.paths.root), "--session", "s",
                     *args])


def _record(sandbox, capsys, entry_id="Q1") -> dict:
    capsys.readouterr()
    assert _ar(sandbox, "entry", "show", entry_id) == 0
    return json.loads(capsys.readouterr().out)


@pytest.fixture
def closed(sandbox, store):
    close(sandbox, store, make_entry(store, "Q1"), verdict="refuted",
          closure_kind="cell", reopen_condition="a re-measurement at n>=3",
          summary="the tree gzips to 2,078,766 B")
    return sandbox


def test_re_closing_a_terminal_entry_under_another_verdict_is_still_refused(closed):
    """The fix opens the PROSE, not the outcome."""
    assert _ar(closed, "close", "Q1", "confirmed", "--memo", "inbox/a.md",
               "--summary", "s") == REFUSED
    assert _record_state(closed) == "refuted"


def _record_state(sandbox) -> str:
    from autoresearch.entries import Store
    return Store(sandbox.paths.entries).load("Q1").status


def test_a_closed_summary_is_correctable_and_the_status_stays_terminal(closed,
                                                                       capsys):
    assert _ar(closed, "entry", "amend", "Q1", "--summary",
               "the tree gzips to 491,753 B measured; 2,134,615 B modelled",
               "--why", "the published figure was the research/-free tree, "
                        "4.3x off the shipped package") == 0
    data = _record(closed, capsys)
    assert data["status"] == "refuted"
    assert data["result"]["verdict"] == "refuted"
    assert data["result"]["summary"].startswith("the tree gzips to 491,753 B")


def test_the_correction_survives_a_re_read_from_disk(closed, capsys):
    """The point of the entry: not just the in-memory object, the file."""
    _ar(closed, "entry", "amend", "Q1", "--summary", "corrected", "--why", "r")
    assert "corrected" in (closed.paths.entries / "Q1.yaml").read_text()


def test_the_correction_is_in_history_with_its_reason(closed, capsys):
    _ar(closed, "entry", "amend", "Q1", "--summary", "corrected", "--why", "the figure was wrong")
    capsys.readouterr()
    _ar(closed, "entry", "show", "Q1", "--history")
    event = json.loads(capsys.readouterr().out)["history"][-1]
    assert event["kind"] == "correction"
    assert "the figure was wrong" in event["detail"]


def test_a_correction_without_a_reason_is_refused(closed, capsys):
    assert _ar(closed, "entry", "amend", "Q1", "--summary", "corrected") == REFUSED
    assert _record(closed, capsys)["result"]["summary"].startswith("the tree")


def test_the_reopen_condition_is_correctable_too(closed, capsys):
    assert _ar(closed, "entry", "amend", "Q1", "--reopen-condition",
               "reopens at n>=3 replicates on a second host", "--why",
               "the original band named no replicate count") == 0
    result = _record(closed, capsys)["result"]
    assert result["reopen_condition"] == "reopens at n>=3 replicates on a second host"
    assert result["summary"].startswith("the tree")


def test_a_correction_cannot_be_smuggled_in_with_a_field_edit(closed):
    assert _ar(closed, "entry", "amend", "Q1", "--observed", "x", "--summary",
               "y", "--why", "r") == REFUSED


def test_correcting_an_open_entry_is_refused(sandbox, store):
    """An open entry has no closure text to correct; the ordinary field amend
    is the route there."""
    make_entry(store, "Q1")
    assert _ar(sandbox, "entry", "amend", "Q1", "--summary", "y",
               "--why", "r") == REFUSED


def test_the_rendered_row_shows_the_corrected_summary(closed):
    from autoresearch.entries import Store
    from autoresearch.render import write_views
    _ar(closed, "entry", "amend", "Q1", "--summary", "491,753 B measured",
        "--why", "r")
    store = Store(closed.paths.entries)
    write_views(closed, store.all())
    view = (closed.paths.root / "docs" / "log" / "Hypothesis Queue.md").read_text()
    row = next(line for line in view.splitlines() if line.startswith("| Q1 "))
    assert "491,753 B measured" in row
    assert "2,078,766" not in row


def _other_session_writes(path, title):
    """Another process writing the same file: bypass this Store entirely, the
    way a second session in the same checkout would."""
    import yaml
    raw = yaml.safe_load(path.read_text())
    raw["title"] = title
    path.write_text(yaml.safe_dump(raw, sort_keys=False))


def test_a_concurrent_write_to_the_same_entry_is_refused(sandbox, store):
    """The silent-revert class (QSB H71): two sessions editing one checkout, and
    the second write destroying the first with a success message. The guard
    names the file and both signatures."""
    from autoresearch.errors import SchemaError
    make_entry(store, "Q1")
    mine = store.load("Q1")
    _other_session_writes(sandbox.paths.entries / "Q1.yaml", "theirs")
    mine.title = "mine"
    with pytest.raises(SchemaError) as caught:
        store.save(mine)
    assert "changed on disk since this session read it" in str(caught.value)
    assert "Q1.yaml" in str(caught.value)


def test_the_refused_write_left_the_other_sessions_record_intact(sandbox, store):
    """The point of the guard: the other session's committed record survives."""
    from autoresearch.errors import SchemaError
    make_entry(store, "Q1")
    mine = store.load("Q1")
    _other_session_writes(sandbox.paths.entries / "Q1.yaml", "theirs")
    mine.title = "mine"
    with pytest.raises(SchemaError):
        store.save(mine)
    assert Store(sandbox.paths.entries).load("Q1").title == "theirs"


def test_force_is_the_deliberate_override(sandbox, store):
    make_entry(store, "Q1")
    mine = store.load("Q1")
    _other_session_writes(sandbox.paths.entries / "Q1.yaml", "theirs")
    mine.title = "mine"
    store.save(mine, force=True)
    assert Store(sandbox.paths.entries).load("Q1").title == "mine"
