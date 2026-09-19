"""`ar close` and the per-track terminal set.

Which statuses are terminal is per-track machine config — a defect track
closes `fixed`, not `confirmed` — and the first real domain filed a harness
defect with the research-track verb, watched the entry bounce back to the
queue, and only then learned the rule. A close that leaves the entry
actionable must say so at the moment it happens.
"""
from autoresearch import cli
from conftest import make_entry, memo


def _claim_and_close(sandbox, entry_id, status, *extra):
    root = ["--domain", str(sandbox.paths.root), "--session", "s"]
    assert cli.main([*root, "claim", entry_id, "--why", "taking it"]) == 0
    capsys_code = cli.main([*root, "close", entry_id, status, *extra])
    return capsys_code


def test_a_non_terminal_close_says_the_entry_returns(sandbox, store, capsys):
    """`blocked` is a legal close target that leaves the entry actionable."""
    make_entry(store, "Q1")
    code = _claim_and_close(sandbox, "Q1", "blocked",
                            "--why", "waiting on a quiet host")
    assert code == 0
    out = capsys.readouterr().out
    assert "'blocked' is not terminal on track 'research'" in out, out
    assert "Q1 stays actionable and returns to the queue" in out
    assert "terminal here: confirmed, refuted" in out


def test_a_terminal_close_prints_no_warning(sandbox, store, capsys):
    memo(sandbox, "inbox/Q1.md")
    make_entry(store, "Q1")
    code = _claim_and_close(sandbox, "Q1", "confirmed", "--memo", "inbox/Q1.md")
    assert code == 0
    assert "not terminal" not in capsys.readouterr().out


def test_a_close_leaves_the_views_valid_so_validate_passes(sandbox, capsys):
    """The queue view carries status and claim state; a close that left it
    stale made every hand-driven mutation eat a failed `ar validate` before
    the next read. Mutating verbs re-render, so validate follows close with
    no manual `ar render` in between."""
    root = ["--domain", str(sandbox.paths.root), "--session", "s"]
    assert cli.main([*root, "entry", "new", "Q1 title",
                     "--track", "research", "--id", "Q1"]) == 0
    assert cli.main([*root, "claim", "Q1", "--why", "taking it"]) == 0
    assert cli.main([*root, "close", "Q1", "confirmed",
                     "--memo", memo(sandbox, "inbox/Q1.md")]) == 0
    assert cli.main([*root, "validate"]) == 0
    assert "problem" not in capsys.readouterr().out
