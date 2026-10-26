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
    from autoresearch import render
    from autoresearch.config import DomainConfig
    from autoresearch.entries import Store
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


def test_shipped_toy_domain_ships_the_hand_driven_protocol_in_sync():
    """The scaffold writes AGENTS.md into new domains; the shipped example
    carries the same file. It must stay identical to the scaffold's constant:
    a drifted copy is two protocols, and agents read whichever repo they are
    opened in."""
    from autoresearch.scaffold import AGENTS_MD
    assert (TOY / "AGENTS.md").read_text() == AGENTS_MD


def test_shipped_toy_domain_carries_what_the_scaffold_writes():
    """AGENTS.md says `bin/ar ...` on every line and points at
    `docs/skills/README.md`, and for a while the example shipped neither: the
    protocol named a shim the repo it was read in did not have. Every
    machine-independent file `ar init` writes verbatim is checked here.
    `bin/autoresearch` is not: its shebang is the scaffolding interpreter's
    absolute path. `bin/measure`, `guides/`, `domain.toml` and `goal.yaml` are
    not either: those are the domain's own, and the toy's are meant to differ."""
    import os

    from autoresearch.scaffold import AR, GITIGNORE, SKILLS_README
    assert (TOY / "bin" / "ar").read_text() == AR
    assert os.access(TOY / "bin" / "ar", os.X_OK)
    assert (TOY / "docs" / "skills" / "README.md").read_text() == SKILLS_README
    assert (TOY / ".gitignore").read_text() == GITIGNORE


def test_the_package_runs_as_a_module():
    """The fallback every `bin/ar` shim advertises (sig.golf H7). A machine
    whose toolkit lives in its default python3 has no `ar` on PATH, and this is
    the path the shim is left with -- so the probe that selects it has to be
    followed by a command that works, not an ImportError naming __main__."""
    result = subprocess.run([sys.executable, "-m", "autoresearch", "--help"],
                            cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "usage: ar" in result.stdout


def test_the_module_carries_a_refusal_not_a_crash_to_the_shim():
    """The shim propagates this return code, and a probe that cannot tell a
    refusal from a crash reports a broken domain as a broken install -- which
    is what the missing `__main__` did: rc 1 with an ImportError on stderr."""
    result = subprocess.run(
        [sys.executable, "-m", "autoresearch", "--domain", str(ROOT), "board"],
        cwd=ROOT, capture_output=True, text=True)
    assert result.returncode != 0, result.stdout
    assert result.stderr.startswith("ar:"), result.stderr


def test_the_scaffolded_shim_names_the_fallback_that_works():
    """The constant and the package are one contract: if the shim stops naming
    `python3 -m autoresearch`, this fails rather than letting the two drift."""
    from autoresearch.scaffold import AR
    assert '"-m", "autoresearch"' in AR
