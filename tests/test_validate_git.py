"""#25: `ar validate` read clean over a record git did not hold.

provably.fast found 13 entry files from a night's measurements untracked
while `validate` read clean -- the record it validated was not the one a
clone would get. Entries and run rows that git reports untracked, modified
or staged-but-uncommitted are named in a warning (never a problem: the
working tree mid-session is legitimately ahead of the last commit).
"""
import subprocess

from autoresearch import validate
from autoresearch.runs import RunRecord, append
from conftest import make_entry


def _git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _commit(root):
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=T", "-c", "user.email=t@example.invalid",
         "commit", "-qm", "record", "--allow-empty")


def _row(run_id):
    return RunRecord(id=run_id, metrics={"ops": 1.0, "peak": 1.0}, session="s",
                     provenance={"host": "x"})


def test_uncommitted_record_files_are_named(sandbox, store):
    root = sandbox.paths.root
    _git(root, "init", "-q")
    make_entry(store, "Q1")
    append(sandbox.paths.runs / "s.jsonl", _row("r1"))
    _commit(root)
    make_entry(store, "Q2")                                  # untracked
    append(sandbox.paths.runs / "s.jsonl", _row("r2"))       # modified
    make_entry(store, "Q3")
    _git(root, "add", "state/entries/Q3.yaml")              # staged only
    [warning] = validate.git_warnings(sandbox)
    for name in ("state/entries/Q2.yaml", "state/entries/Q3.yaml",
                 "data/runs/s.jsonl"):
        assert name in warning
    assert "state/entries/Q1.yaml" not in warning


def test_a_committed_record_draws_no_warning(sandbox, store):
    _git(sandbox.paths.root, "init", "-q")
    make_entry(store, "Q1")
    _commit(sandbox.paths.root)
    assert validate.git_warnings(sandbox) == []


def test_outside_git_there_is_nothing_to_say(sandbox, store):
    make_entry(store, "Q1")
    assert validate.git_warnings(sandbox) == []


def test_the_warning_reaches_ar_validate_without_failing_it(sandbox, store, capsys):
    from autoresearch import cli, render
    _git(sandbox.paths.root, "init", "-q")
    make_entry(store, "Q1")
    render.write_views(sandbox, store.all())
    assert cli.main(["--domain", str(sandbox.paths.root), "validate"]) == 0
    assert "state/entries/Q1.yaml" in capsys.readouterr().out
