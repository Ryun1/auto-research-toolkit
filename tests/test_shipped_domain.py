"""The domain we ship as the getting-started example must pass our own gate.

This exists because it did not. `domains/toy` accumulated the debris of a manual
CLI walkthrough -- three entries, two memos, a runs file -- and then a `--reopen`
left its generated view out of step with its records, so `ar validate` on the
shipped example exited 1. Every other test used the `sandbox` fixture, which
wipes exactly those directories, so nothing looked at what was actually
committed.

That is the same shape as H101 in the source corpus: a verification block that
reported OK having checked strictly less than the gate. The fix is to check the
artefact, not a cleaned copy of it.
"""
import subprocess
import sys

from conftest import ROOT, TOY


def ar(*args):
    return subprocess.run(
        [sys.executable, "-m", "autoresearch.cli", "--domain", str(TOY), *args],
        cwd=ROOT, capture_output=True, text=True)


def test_shipped_toy_domain_validates_clean():
    result = ar("validate")
    assert result.returncode == 0, result.stdout + result.stderr


def test_shipped_toy_domain_ships_no_walkthrough_debris():
    """It is a starting point, not someone's saved session."""
    from autoresearch.entries import Store
    assert Store(TOY / "state" / "entries").all() == []
    runs = TOY / "data" / "runs"
    assert not (runs.exists() and list(runs.glob("*.jsonl")))
    memos = TOY / "inbox"
    assert not (memos.exists() and list(memos.glob("*.md")))


def test_shipped_generated_views_match_their_records():
    """Invariant 1, checked against what is committed rather than a copy."""
    from autoresearch.config import DomainConfig
    from autoresearch.entries import Store
    from autoresearch import render
    config = DomainConfig.load(TOY)
    assert render.check_views(config, Store(config.paths.entries).all()) == []


def test_shipped_toy_domain_board_and_rank_run():
    for verb in ("board", "rank", "budget", "policy"):
        result = ar(verb)
        assert result.returncode == 0, f"ar {verb}: {result.stdout}{result.stderr}"


def test_shipped_toy_domain_reports_zero_skills_rather_than_staying_silent():
    """A reader that cannot say 'zero' is indistinguishable from one that read
    nothing -- the convention this core inherited verbatim."""
    result = ar("skill", "list")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 skill(s); 0 stale" in result.stdout


def test_shipped_toy_domain_ships_no_skills():
    """A committed skill would cite entries `state/entries/` does not hold."""
    assert not list((TOY / "docs" / "skills").glob("*/SKILL.md"))
