"""#35: store tools every multi-session domain needs, in core.

qsb-research grew `bin/entry-collision` (entry ids two branches both changed
before a merge silently keeps one -- H90, H107) and `bin/dupe_sweep` /
`bin/integrity_check` (54 duplicate claim pairs, 10 with contradictory
terminals -- H303, H4054). Both are store questions, not domain ones.
"""
import subprocess

from autoresearch import cli, validate
from conftest import close, make_entry


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True,
                          capture_output=True, text=True).stdout


def _commit(root, message):
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=T", "-c", "user.email=t@example.invalid",
         "commit", "-qm", message)


def _branches(sandbox, store):
    root = sandbox.paths.root
    _git(root, "init", "-q", "-b", "main")
    make_entry(store, "Q1")
    make_entry(store, "Q2")
    _commit(root, "base")
    _git(root, "checkout", "-q", "-b", "other")
    entry = store.load("Q1")
    entry.title = "theirs"
    store.save(entry)
    make_entry(store, "Q3", title="their new lead")
    make_entry(store, "Q4", title="only theirs")
    _commit(root, "other side")
    _git(root, "checkout", "-q", "main")
    from autoresearch.entries import _ENTRY_CACHE
    _ENTRY_CACHE.clear()
    entry = store.load("Q1")
    entry.title = "ours"
    store.save(entry)
    make_entry(store, "Q3", title="our new lead")
    _commit(root, "this side")


def test_collisions_name_ids_both_sides_touched(sandbox, store, capsys):
    _branches(sandbox, store)
    code = cli.main(["--domain", str(sandbox.paths.root), "entry", "collisions",
                     "other"])
    out = capsys.readouterr().out
    assert code == 1
    assert "Q3  filed on both sides" in out
    assert "Q1  changed on both sides" in out
    assert "Q2" not in out and "Q4" not in out


def test_no_collision_exits_clean(sandbox, store, capsys):
    root = sandbox.paths.root
    _git(root, "init", "-q", "-b", "main")
    make_entry(store, "Q1")
    _commit(root, "base")
    _git(root, "checkout", "-q", "-b", "other")
    make_entry(store, "Q2")
    _commit(root, "other")
    _git(root, "checkout", "-q", "main")
    assert cli.main(["--domain", str(root), "entry", "collisions", "other"]) == 0
    assert "no entry collides" in capsys.readouterr().out


def test_duplicate_closures_are_warned_and_contradictions_marked(sandbox, store):
    close(sandbox, store, make_entry(store, "Q1", title="Fold the cache"),
          verdict="confirmed")
    close(sandbox, store, make_entry(store, "Q2", title="fold  the cache."),
          verdict="refuted", closure_kind="mechanism")
    close(sandbox, store, make_entry(store, "Q3", hypothesis="wider is cheaper"),
          verdict="refuted", closure_kind="mechanism")
    close(sandbox, store, make_entry(store, "Q4", hypothesis="Wider is cheaper"),
          verdict="refuted", closure_kind="mechanism")
    make_entry(store, "Q5", title="fold the cache")           # open: not a closure
    [warning] = validate.duplicate_warnings(sandbox)
    assert "2 duplicate closure pair(s)" in warning
    assert "Q1/Q2 (contradictory: confirmed vs refuted)" in warning
    assert "Q3/Q4" in warning and "Q5" not in warning


def test_distinct_closures_draw_no_warning(sandbox, store):
    close(sandbox, store, make_entry(store, "Q1", title="a"), verdict="confirmed")
    close(sandbox, store, make_entry(store, "Q2", title="b"), verdict="confirmed")
    assert validate.duplicate_warnings(sandbox) == []
