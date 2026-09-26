from autoresearch.validate import INBOX_WARN_BYTES, inbox_warnings
from conftest import make_entry, memo

BIG = INBOX_WARN_BYTES + 1024


def test_a_oversized_inbox_file_warns_but_does_not_fail(sandbox, store, capsys):
    """The field corpora keep 60-1250 evidence files in inbox/ and must keep
    validating clean: the warning is a nudge, never a problem or an exit 1."""
    (sandbox.paths.memos / "kernel.cu").write_bytes(b"x" * BIG)

    warnings = inbox_warnings(sandbox)

    assert len(warnings) == 1
    assert "kernel.cu" in warnings[0]
    assert "data/artifacts/" in warnings[0]
    # Warnings are a separate channel: the config-level problems are untouched
    # and the CLI still exits 0 with the big file in place.
    assert sandbox.check() == []
    from autoresearch import cli, render
    render.write_views(sandbox, store.all())
    assert cli.main(["--domain", str(sandbox.paths.root), "validate"]) == 0
    out = capsys.readouterr().out
    assert "clean" in out
    assert "problem" not in out


def test_a_small_memo_draws_no_warning(sandbox):
    memo(sandbox, "inbox/Q1.md", "the result held\n")

    assert inbox_warnings(sandbox) == []


def test_validate_json_prints_only_ok_and_problems(sandbox, store, capsys):
    """A `--json` consumer parses stdout, not around prose: warnings and the
    read-counts summary stay off the document channel, and the exit code is
    unchanged."""
    import json

    from autoresearch import cli, render

    render.write_views(sandbox, store.all())
    assert cli.main(["--domain", str(sandbox.paths.root),
                     "validate", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc == {"ok": True, "problems": [], "skipped_runs": 0}

    make_entry(store, "Q1", hardware="typo")
    assert cli.main(["--domain", str(sandbox.paths.root),
                     "validate", "--json"]) == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["ok"] is False
    assert any("typo" in p for p in doc["problems"])


def test_validate_reports_in_progress_without_a_claim(sandbox, store, capsys):
    import json

    from autoresearch import cli, render
    from autoresearch.claims import Claims
    from autoresearch.entries import Event

    make_entry(store, "Q1", status="in-progress", history=[Event(
        "2026-09-25T12:00:00+00:00", "in-progress->queued",
        "old-session", "settled")])
    make_entry(store, "Q2")
    Claims(store, sandbox, "new-session").claim("Q2")
    make_entry(store, "Q3", history=[Event(
        "2026-09-25T12:00:00+00:00", "in-progress->queued",
        "old-session", "settled"), Event(
            "2026-09-25T12:00:01+00:00", "released",
            "old-session", "settled")])
    render.write_views(sandbox, store.all())

    assert cli.main(["--domain", str(sandbox.paths.root),
                     "validate", "--json"]) == 1
    problems = json.loads(capsys.readouterr().out)["problems"]
    orphan = [item for item in problems if item.startswith("Q1:")]
    assert len(orphan) == 1
    assert "in-progress with no live claim" in orphan[0]
    assert "status and history disagree" in orphan[0]
    assert "ar reap Q1" in orphan[0]
    q2 = [item for item in problems if item.startswith("Q2:")]
    assert all("no live claim" not in item for item in q2)
    q3 = [item for item in problems if item.startswith("Q3:")]
    assert all("no live claim" not in item for item in q3)


def test_an_empty_inbox_is_silent(sandbox):
    assert inbox_warnings(sandbox) == []
    assert sandbox.check() == []
