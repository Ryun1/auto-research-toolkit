from autoresearch.validate import INBOX_WARN_BYTES, inbox_warnings
from conftest import memo

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


def test_an_empty_inbox_is_silent(sandbox):
    assert inbox_warnings(sandbox) == []
    assert sandbox.check() == []
