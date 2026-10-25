"""Merge residue: state that looks committed and is not (QSB H83).

A `git merge` can be concluded without the conflict ever being resolved, in two
different ways, and git blesses both:

1. **`git add` on files that still carry markers.** `git add` satisfies the
   merge, so `git status` reports "All conflicts fixed but you are still
   merging" with no unmerged path left to look for. The marker lines are still
   in the file. This is the state a real domain hit: the whole `ar` verb
   surface -- board, claim, entry, close, render, validate, budget, reap --
   died on a YAML scanner error naming an unrelated file, and nothing in the
   toolkit recognised the cause.
2. **Deleting only the marker lines, keeping both sides' text.** The result
   parses, usually: PyYAML takes the last of a duplicated key and says nothing.
   The committed record is then the *interleaving* of two records, and the field
   a reader sees is whichever side happened to be written last.

Neither is an unmerged path, so `git status` is clean and `git merge --abort`
has nothing to abort. Both are invisible to every reader, and both outlive the
session that made them. This module is the single place that recognises them.

Generated views are scanned too. They self-heal through `ar render`, so the
remedy differs, and a view committed with markers is the visible half of an
entry file committed with markers.
"""

from __future__ import annotations

import collections.abc
import pathlib

import yaml

#: Git's three conflict-marker lines, as runs so a source file, a test fixture
#: or a memo can talk about the defect without itself tripping the scan.
MARKER_START = "<" * 7
MARKER_MIDDLE = "=" * 7
MARKER_END = ">" * 7

#: The lanes whose files are committed state rather than evidence. A duplicate
#: key in a JSON corpus is a data quirk; in an entry record it is a lost field.
_STATE_SUFFIXES = (".yaml", ".yml", ".md")


class DuplicateKey(yaml.YAMLError):
    """A mapping key appears twice in one YAML document."""


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that refuses a repeated mapping key instead of picking one."""


def _no_duplicates(loader, node, deep=False):
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, collections.abc.Hashable):
            continue
        if key in seen:
            raise DuplicateKey(
                f"duplicate key {key!r} at line {key_node.start_mark.line + 1}")
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep)



_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicates)


def marker_lines(text: str) -> list[int]:
    """1-based line numbers of each complete start/middle/end marker triple.

    All three are required, in order. A lone angle run is prose -- a memo may
    legitimately quote a diff -- and must not close a domain's validation.
    """
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines, 1) if line.startswith(MARKER_START)]
    middles = [i for i, line in enumerate(lines, 1) if line.startswith(MARKER_MIDDLE)]
    ends = [i for i, line in enumerate(lines, 1) if line.startswith(MARKER_END)]
    found = []
    for start in starts:
        middle = next((m for m in middles if m > start), None)
        end = next((e for e in ends if middle is not None and e > middle), None)
        if middle is not None and end is not None:
            found.append(start)
    return found


def file_problem(path: pathlib.Path, remedy: str,
                root: pathlib.Path | None = None) -> str | None:
    """The one-line problem for one file, or None when it is clean.

    A file can carry both residues; they are reported together, because both
    come from the same unfinished merge and one report is one thing to fix.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    reasons = []
    marks = marker_lines(text)
    if marks:
        where = ", ".join(str(n) for n in marks[:5])
        reasons.append(
            f"unresolved merge conflict markers at line(s) {where} "
            f"({len(marks)} block(s))")
    if not reasons and path.suffix in (".yaml", ".yml"):
        # Only when the file is at least parseable: a marker-bearing file is
        # not YAML at all, and its problem is the markers, already reported.
        # A file that is simply broken is the store's own error to name -- this
        # check is about a merge nobody finished, not about every bad file.
        try:
            yaml.load(text, Loader=_StrictLoader)
        except DuplicateKey as exc:
            reasons.append(f"both sides of a resolved conflict left behind: {exc}")
        except yaml.YAMLError:
            pass
    if not reasons:
        return None
    rel = path.relative_to(root) if root is not None else path
    return f"{rel}: {'; '.join(reasons)} -- {remedy}"


def merge_in_progress(root: pathlib.Path) -> bool:
    """True while git still has an open merge or rebase in this checkout.

    The distinction matters because the ADVICE differs. Markers with a merge
    still open are what a merge looks like halfway through: the right move is to
    finish or abort it. Markers with no merge open are the H83 state -- git
    believes the merge is concluded and there is nothing to abort. Telling an
    operator to "resolve the conflict in the file" while `git merge --continue`
    is the actual command is advice that sends them to hand-edit YAML during a
    merge they did not start. Learned the hard way: this check fired on a live
    merge in a shared checkout and named the wrong remedy.
    """
    git = root / ".git"
    if git.is_file():                     # a worktree: .git is a pointer file
        try:
            pointer = git.read_text().strip()
        except OSError:
            return False
        if not pointer.startswith("gitdir:"):
            return False
        git = pathlib.Path(pointer.split(":", 1)[1].strip())
        if not git.is_absolute():
            git = (root / git).resolve()
    return any((git / name).exists() for name in (
        "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge",
        "rebase-apply"))


def _scan_targets(config, in_merge: bool) -> list[tuple[pathlib.Path, str]]:
    root = pathlib.Path(config.paths.root)
    tail = ("a merge is OPEN: finish it (`git merge --continue`) or abort it "
            "(`git merge --abort`) -- do not hand-edit the files"
            if in_merge else
            "git has NO open merge, so these are residue from a merge that was "
            "concluded without resolving it (QSB H83)")
    return [
        (pathlib.Path(config.paths.entries), f"entry file (hand-owned; {tail})"),
        (root / "docs" / "log",
         f"generated view (regenerate with `ar render`; {tail})"),
        (pathlib.Path(config.paths.memos) / "INDEX.md",
         f"generated view (regenerate with `ar render`; {tail})"),
    ]


def residue_problems(config) -> list[str]:
    """Every committed-state file that carries an unfinished merge."""
    root = pathlib.Path(config.paths.root)
    problems = []
    for base, remedy in _scan_targets(config, merge_in_progress(root)):
        if base.is_file():
            candidates = [base]
        elif base.is_dir():
            candidates = sorted(p for p in base.rglob("*")
                                if p.is_file() and p.suffix in _STATE_SUFFIXES)
        else:
            continue
        for path in candidates:
            problem = file_problem(path, remedy, root)
            if problem:
                problems.append(problem)
    return problems
