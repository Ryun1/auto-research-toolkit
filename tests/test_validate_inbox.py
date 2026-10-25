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


def test_a_closure_with_no_summary_warns_but_never_fails(sandbox, store, capsys):
    """sig.golf H18: the gate is at closure, so the ones that predate it (or
    arrived by a path that never had one) are a warning -- validate's contract
    is that a convention nudge never changes the exit code."""
    from autoresearch import cli, render
    from autoresearch.validate import closure_warnings
    from conftest import close
    memo(sandbox, "inbox/Q1.md")
    entry = close(sandbox, store, make_entry(store, "Q1"))
    assert entry.result.summary
    assert closure_warnings(sandbox) == []
    entry.result.summary = ""
    store.save(entry)
    warnings = closure_warnings(sandbox)
    assert len(warnings) == 1 and "Q1" in warnings[0] and "--summary" in warnings[0]
    render.write_views(sandbox, store.all())
    assert cli.main(["--domain", str(sandbox.paths.root), "validate"]) == 0
    out = capsys.readouterr().out
    assert "clean" in out and "Q1" in out


def test_a_row_whose_declared_output_is_gone_is_named(sandbox, store, capsys):
    """sig.golf H22: `RunRecord.outputs` is the only pointer from a number to
    what produced it, and nothing read that declaration back -- so a row whose
    evidence was never retained reads exactly like one whose evidence is
    present. A warning, because validate's contract is that a convention nudge
    never changes the exit code."""
    from autoresearch import cli, render
    from autoresearch.runs import OK, RunRecord, append
    from autoresearch.validate import output_warnings
    make_entry(store, "Q1")
    append(sandbox.paths.runs / "s.jsonl", RunRecord(
        id="ghost", session="s", entry="Q1", status=OK,
        metrics={"ops": 1.0, "peak": 1.0}, provenance={"host": "x"},
        outputs=["data/artifacts/never-written.bin"]))
    warnings = output_warnings(sandbox)
    assert len(warnings) == 1
    assert "ghost" in warnings[0] and "never-written.bin" in warnings[0]
    assert "evidence pack" in warnings[0]          # the remedy, named
    render.write_views(sandbox, store.all())
    assert cli.main(["--domain", str(sandbox.paths.root), "validate"]) == 0
    assert "clean" in capsys.readouterr().out


def test_a_row_whose_outputs_are_present_is_quiet(sandbox, store):
    from autoresearch.runs import OK, RunRecord, append
    from autoresearch.validate import output_warnings
    keep = sandbox.paths.root / "data" / "artifacts"
    keep.mkdir(parents=True, exist_ok=True)
    (keep / "proof.bin").write_bytes(b"evidence")
    append(sandbox.paths.runs / "s.jsonl", RunRecord(
        id="kept", session="s", entry="Q1", status=OK,
        metrics={"ops": 1.0, "peak": 1.0}, provenance={"host": "x"},
        outputs=["data/artifacts/proof.bin"]))
    assert output_warnings(sandbox) == []


def test_evidence_declared_outside_the_tree_is_named(sandbox, store, tmp_path):
    """sig.golf H26: existence is not containment. An absolute declaration, or
    one that escapes with `../`, resolves to a real file often enough that an
    existence test passes it -- while the bytes would not survive a clone, a
    bundle or `ar harness update`, which is the property the warning is for."""
    from autoresearch.runs import OK, RunRecord, append
    from autoresearch.validate import output_warnings
    make_entry(store, "Q1")
    outside = tmp_path / "kept-outside.bin"
    outside.write_bytes(b"x")
    append(sandbox.paths.runs / "s.jsonl", RunRecord(
        id="escaped", session="s", entry="Q1", status=OK,
        metrics={"ops": 1.0, "peak": 1.0}, provenance={"host": "x"},
        outputs=[str(outside), "../also-outside.bin"]))
    warnings = output_warnings(sandbox)
    # the present absolute path and the absent escape are both escapes
    assert len(warnings) == 2
    assert all("outside the domain tree" in w and "escaped" in w for w in warnings)
    assert "kept-outside.bin" in warnings[0] and "also-outside.bin" in warnings[1]
