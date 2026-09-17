"""`ar skill` -- the human's view of the distilled corpus.

Exit codes matter here: `check` is a gate, and `list` is the screen someone
looks at before deciding whether to spend an iteration distilling.
"""
from autoresearch import cli
from conftest import close, make_entry, make_skill


def _ar(sandbox, *args):
    return cli.main(["--domain", str(sandbox.paths.root), *args])


def test_list_reports_zero_rather_than_printing_nothing(sandbox, capsys):
    """Every reader in this harness reports how many things it read: a board
    that says 'no skills' must not be indistinguishable from one that read
    nothing."""
    assert _ar(sandbox, "skill", "list") == 0
    assert "0 skill(s); 0 stale" in capsys.readouterr().out


def test_list_names_the_ratio_the_design_turns_on(sandbox, store, capsys):
    close(sandbox, store, make_entry(store, "Q1"))
    make_skill(sandbox, "a-lesson", cites=["Q1"],
               body="# A claim [Q1]\n\n" + "body line\n" * 40)
    assert _ar(sandbox, "skill", "list") == 0
    out = capsys.readouterr().out
    assert "a-lesson" in out and "Use when" in out
    assert "body lines held" in out and "index line(s) carried" in out


def test_list_exits_nonzero_when_a_skill_is_stale(sandbox, store, capsys):
    entry = close(sandbox, store, make_entry(store, "Q1"))
    make_skill(sandbox, "was-true", cites=["Q1"])
    machine = sandbox.track_for("Q1").machine
    entry.apply(machine, machine.initial, "test", why="new evidence")
    store.save(entry)

    assert _ar(sandbox, "skill", "list") == 1
    assert "STALE" in capsys.readouterr().out


def test_check_is_a_gate(sandbox, store, capsys):
    make_entry(store, "Q1")                          # open
    make_skill(sandbox, "premature", cites=["Q1"])
    assert _ar(sandbox, "skill", "check") == 1
    assert "not terminal" in capsys.readouterr().out


def test_check_says_the_constant_lint_did_not_run_rather_than_passing_silently(
        sandbox, store, capsys):
    close(sandbox, store, make_entry(store, "Q1"))
    make_skill(sandbox, "fine", cites=["Q1"])
    assert _ar(sandbox, "skill", "check") == 0
    out = capsys.readouterr().out
    assert "did not run" in out and "gap, not a pass" in out


def test_validate_refuses_a_domain_whose_skill_outlived_its_evidence(
        sandbox, store, capsys):
    """The check has to be in `ar validate` too, or it is enforced only where
    someone remembers to run it."""
    entry = close(sandbox, store, make_entry(store, "Q1"))
    make_skill(sandbox, "was-true", cites=["Q1"])
    _ar(sandbox, "render")
    assert _ar(sandbox, "validate") == 0
    machine = sandbox.track_for("Q1").machine
    entry.apply(machine, machine.initial, "test", why="new evidence")
    store.save(entry)
    capsys.readouterr()

    assert _ar(sandbox, "validate") == 1
    assert "was-true" in capsys.readouterr().out


def test_validate_counts_the_skills_it_read(sandbox, store, capsys):
    close(sandbox, store, make_entry(store, "Q1"))
    make_skill(sandbox, "counted", cites=["Q1"])
    _ar(sandbox, "validate")
    assert "1 skills" in capsys.readouterr().out


def test_show_prints_the_whole_skill(sandbox, store, capsys):
    close(sandbox, store, make_entry(store, "Q1"))
    make_skill(sandbox, "shown", cites=["Q1"])
    assert _ar(sandbox, "skill", "show", "shown") == 0
    out = capsys.readouterr().out
    assert "name: shown" in out and "[Q1]" in out


def test_show_refuses_a_name_that_is_not_there(sandbox):
    assert cli.main(["--domain", str(sandbox.paths.root),
                     "skill", "show", "absent"]) == 2


def test_retire_requires_a_reason(sandbox, store, capsys):
    """A skill removed without a reason is a skill that gets written again."""
    close(sandbox, store, make_entry(store, "Q1"))
    make_skill(sandbox, "going", cites=["Q1"])
    import pytest
    with pytest.raises(SystemExit):
        _ar(sandbox, "skill", "retire", "going")
    assert (sandbox.paths.root / "docs/skills/going/SKILL.md").exists()

    assert _ar(sandbox, "skill", "retire", "going", "--why", "superseded") == 0
    assert not (sandbox.paths.root / "docs/skills/going").exists()


def test_distil_dry_run_writes_nothing(sandbox, store, capsys):
    close(sandbox, store, make_entry(store, "Q1"))
    assert _ar(sandbox, "skill", "distil", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "Q1" in out and "nothing was written" in out
    assert not any((sandbox.paths.root / "docs/skills").glob("*/SKILL.md"))


def test_distil_preserves_usage_across_restart(sandbox, store, monkeypatch):
    from autoresearch.budget import recorded_usage
    from autoresearch.driver import brain as brain_mod
    from autoresearch.driver.brain import Reply

    close(sandbox, store, make_entry(store, "Q1"))

    class Librarian:
        def ask(self, role, brief, **kwargs):
            return Reply(role=role, data={}, cost_usd=0.75, backend="local-fixture")

    monkeypatch.setattr(brain_mod, "build_brain", lambda *a, **kw: Librarian())
    result = _ar(sandbox, "skill", "distil")
    assert recorded_usage(sandbox)["money"] == 0.75
    assert result == 0


def test_distil_retains_unknown_usage_on_failure(sandbox, monkeypatch):
    import pytest

    from autoresearch.budget import recorded_usage
    from autoresearch.driver import brain as brain_mod
    from autoresearch.driver.loop import Coordinator

    monkeypatch.setattr(brain_mod, "build_brain", lambda *a, **kw: object())

    def interrupted(self, iteration, budget, force=False):
        iteration.cost_usd = None
        raise RuntimeError("interrupted after unmetered work")

    monkeypatch.setattr(Coordinator, "distil", interrupted)
    with pytest.raises(RuntimeError, match="interrupted"):
        _ar(sandbox, "skill", "distil")
    assert recorded_usage(sandbox)["money"] is None
