"""A merge concluded without the conflict being resolved (QSB H83).

The incident is real: a `git merge` of `origin/main` into a domain root was
concluded while `state/entries/H70.yaml` and `state/entries/Q2039.yaml` still
carried markers, and the four generated views were committed with markers too.
Every `ar` verb that reads the entry set -- board, claim, entry, close, render,
validate, budget, reap -- died on a YAML scanner error naming an unrelated file,
and the domain lost all verdict recording until a human repaired the tree.

Git blesses two different ways of concluding a merge that was never resolved,
and neither leaves an unmerged path:

1. `git add` on files that still carry the marker lines (form 1);
2. deleting only the marker lines, leaving both sides' text, so the file parses
   and PyYAML silently takes the last of a duplicated key (form 2).

These tests perform the real merges in real git repositories and require the
failure to be NAMED. The defect was never the parse error; it was that nothing
in the toolkit recognised the cause.
"""
import json
import pathlib
import subprocess

import pytest
import yaml

from autoresearch import cli
from autoresearch import merge as merge_mod
from conftest import make_entry


def _git(repo, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, check=True).stdout


def _ar(sandbox, *args) -> int:
    return cli.main(["--domain", str(sandbox.paths.root), "--session", "s",
                     *args])


def _diverge(sandbox, store) -> pathlib.Path:
    """Two branches, each editing the title of both entry files, merged."""
    for name in ("Q1", "Q2"):
        make_entry(store, name, title="original")
    root = pathlib.Path(sandbox.paths.root)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "test")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    _git(root, "checkout", "-q", "-b", "theirs")
    # git rewrote the files underneath us; re-read before writing over them,
    # which is exactly what the H71 guard exists to make necessary.
    store.all()
    for name in ("Q1", "Q2"):
        make_entry(store, name, title="theirs")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "theirs")
    _git(root, "checkout", "-q", "main")
    store.all()
    for name in ("Q1", "Q2"):
        make_entry(store, name, title="ours")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "ours")
    merged = subprocess.run(["git", "merge", "theirs"], cwd=root,
                            capture_output=True, text=True)
    # git merge exits 1 when it leaves a conflict behind. That is the state
    # under test, not a failure of the fixture.
    assert merged.returncode == 1, merged.stdout + merged.stderr
    return root


@pytest.fixture
def markers_kept(sandbox, store):
    """Form 1: `git add` run on files that still carry the marker lines."""
    root = _diverge(sandbox, store)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "resolved")
    for name in ("Q1", "Q2"):
        body = (root / "state" / "entries" / f"{name}.yaml").read_text()
        assert merge_mod.marker_lines(body), "fixture lost its markers"
    return sandbox


@pytest.fixture
def both_sides_kept(sandbox, store):
    """Form 2: only the marker lines deleted, both sides' text left behind."""
    root = _diverge(sandbox, store)
    for name in ("Q1", "Q2"):
        path = root / "state" / "entries" / f"{name}.yaml"
        kept = [line for line in path.read_text().splitlines()
                if not line.startswith((merge_mod.MARKER_START,
                                        merge_mod.MARKER_MIDDLE,
                                        merge_mod.MARKER_END))]
        path.write_text("\n".join(kept) + "\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "resolved")
    return sandbox


@pytest.mark.parametrize("residue", ["markers_kept", "both_sides_kept"])
def test_git_reports_the_merged_tree_as_clean(residue, request):
    """The precondition of the whole defect: git has nothing left to say, and
    `git merge --abort` has nothing to abort."""
    sandbox = request.getfixturevalue(residue)
    root = pathlib.Path(sandbox.paths.root)
    assert _git(root, "status", "--porcelain").strip() == ""


def test_validate_names_every_marker_bearing_entry_file(markers_kept, capsys):
    assert _ar(markers_kept, "validate") == 1
    out = capsys.readouterr().out
    for name in ("Q1", "Q2"):
        assert f"{name}.yaml: unresolved merge conflict markers" in out
    # The remedy differs per lane, and the message has to say which.
    assert "hand-owned" in out


def test_validate_names_both_sides_of_a_kept_conflict(both_sides_kept, capsys):
    assert _ar(both_sides_kept, "validate") == 1
    out = capsys.readouterr().out
    for name in ("Q1", "Q2"):
        assert f"{name}.yaml: both sides of a resolved conflict left behind" in out
    assert "duplicate key 'title'" in out


def test_validate_json_reports_the_markers(markers_kept, capsys):
    assert _ar(markers_kept, "validate", "--json") == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["ok"] is False
    assert any("conflict markers" in p for p in doc["problems"])


def test_validate_names_a_marker_bearing_generated_view(markers_kept, capsys):
    """A view self-heals through `ar render`; the entry files do not. The check
    must still name it, with the render remedy."""
    root = pathlib.Path(markers_kept.paths.root)
    view = root / "docs" / "log" / "Hypothesis Queue.md"
    view.parent.mkdir(parents=True, exist_ok=True)
    view.write_text(f"# view\n\n{merge_mod.MARKER_START} HEAD\nboth sides\n"
                    f"{merge_mod.MARKER_MIDDLE}\n{merge_mod.MARKER_END} theirs\n")
    assert _ar(markers_kept, "validate") == 1
    out = capsys.readouterr().out
    assert "Hypothesis Queue.md: unresolved merge conflict markers" in out
    assert "ar render" in out


def test_board_names_the_conflict_instead_of_a_scanner_error(markers_kept, capsys):
    """`ar board` never reaches validate. Before the fix it died with a PyYAML
    scanner error naming an unrelated line; it must now name the cause."""
    try:
        _ar(markers_kept, "board")
    except SystemExit as exit_code:
        assert exit_code.code != 0
    message = capsys.readouterr().err
    assert "unresolved merge conflict markers" in message
    assert "Q1.yaml" in message
    assert "scanner" not in message.lower()


def test_prose_that_merely_quotes_a_diff_is_not_flagged(sandbox, store, capsys):
    """A memo may legitimately show a diff. Only a complete in-order triple is
    a merge artifact, so a lone marker line must not close a domain's
    validation."""
    entry = make_entry(store, "Q1", hypothesis="see the " +
                       merge_mod.MARKER_START + " HEAD run of that diff")
    store.save(entry)
    _ar(sandbox, "validate")
    assert "unresolved merge conflict markers" not in capsys.readouterr().out


def test_repeated_keys_in_nested_mappings_are_caught_too():
    """A result block merged from both sides lands as a repeated key inside
    a nested mapping, and the rule has to reach it."""
    body = ("id: Q1\n"
            "result:\n"
            "  verdict: confirmed\n"
            "  memo: inbox/a.md\n"
            "  memo: inbox/b.md\n")
    assert yaml.safe_load(body)["result"]["memo"] == "inbox/b.md"  # silent
    with pytest.raises(merge_mod.DuplicateKey):
        yaml.load(body, Loader=merge_mod._StrictLoader)


def test_an_open_merge_gets_the_conclude_or_abort_advice(both_sides_kept,
                                                           tmp_path, capsys):
    """Markers with a merge still open are what a merge looks like halfway
    through. The right command is `git merge --continue`, not a hand edit, and
    this check fired on a live merge in a shared checkout before it said so."""
    from autoresearch.merge import _scan_targets, merge_in_progress

    repo = tmp_path / "open"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    (repo / "f").write_text("a\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "checkout", "-q", "-b", "side")
    (repo / "f").write_text("b\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "side")
    _git(repo, "checkout", "-q", "main")
    (repo / "f").write_text("c\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "main")
    assert subprocess.run(["git", "merge", "side"], cwd=repo,
                          capture_output=True).returncode == 1
    assert merge_in_progress(repo)
    assert not merge_in_progress(pathlib.Path(both_sides_kept.paths.root))

    advice = " ".join(remedy for _, remedy in _scan_targets(both_sides_kept, True))
    assert "a merge is OPEN" in advice
    assert "git merge --continue" in advice
    assert "do not hand-edit" in advice
    # And the closed reading is the other one, in the same words.
    closed = " ".join(remedy for _, remedy in _scan_targets(both_sides_kept, False))
    assert "git has NO open merge" in closed and "QSB H83" in closed

    assert _ar(both_sides_kept, "validate") == 1
    out = capsys.readouterr().out
    assert "git has NO open merge" in out
