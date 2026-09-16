"""Isolated workspaces for parallel workers, owned by the coordinator.

This is the one place the source harness's design is deliberately *changed*
rather than ported. There, every session created its own worktree and nothing
watched it, so the harness had to infer whether a tree was still in use -- and
the debt log is that inference failing:

* `destroy` had no ownership check, so one session's teardown removed every
  other session's slots (H57);
* `init` took no hold, so a slot being measured in read as unheld and was
  destroyed under its owner (H59);
* `create` silently reused a worktree when the session name already existed, so
  two ticks shared one index (H70);
* worktrees were named relative to wherever the command was run (H19);
* and 64 of 64 merged worktrees were never destroyed, because teardown was
  documented in a runbook and wired into no loop (H91).

Here the coordinator creates the pool, hands out slots, and destroys them in its
own `finally`. Lifecycle is owned by the thing that knows when a worker
finished, so it is observed rather than guessed. Every slot records its holder
and `release` refuses a non-holder -- H57's fix, kept.

Slots are git worktrees when the domain is a git repository (so a worker has a
real index of its own and its commits are separable) and plain directories
otherwise, so the toy domain and any non-git domain still work.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass

from .errors import ClaimError


def _git(root, *args, check=True):
    return subprocess.run(["git", *args], cwd=root, capture_output=True,
                          text=True, check=check)


def is_git_repo(root) -> bool:
    try:
        return _git(root, "rev-parse", "--is-inside-work-tree").stdout.strip() == "true"
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


@dataclass
class Slot:
    name: str
    path: object
    holder: str
    kind: str            # "worktree" | "directory"
    branch: str | None = None
    retained: str | None = None


class Pool:
    """A set of workspaces the coordinator owns for the length of one iteration."""

    def __init__(self, config, owner: str):
        self.config, self.owner = config, owner
        self.root = config.paths.workspaces
        self.slots: dict[str, Slot] = {}
        self.git = is_git_repo(config.paths.root)

    def _record(self, slot: Slot) -> None:
        marker = self.root / f"{slot.name}.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(
            {"name": slot.name, "holder": slot.holder, "kind": slot.kind,
             "branch": slot.branch, "path": str(slot.path),
             "retained": slot.retained}))

    def acquire(self, name: str) -> Slot:
        """Create one slot. Refuses to reuse an existing one silently (H70)."""
        path = self.root / name
        if path.exists():
            raise ClaimError(
                f"workspace {name!r} already exists at {path}. Reusing it would "
                "give two workers one index (H70); pick another name or release "
                "the existing slot.")
        self.root.mkdir(parents=True, exist_ok=True)
        if self.git:
            branch = f"ar/{name}"
            _git(self.config.paths.root, "worktree", "add", "-q", "-b", branch,
                 str(path), "HEAD")
            slot = Slot(name, path, self.owner, "worktree", branch)
        else:
            shutil.copytree(self.config.paths.root, path,
                            ignore=shutil.ignore_patterns(
                                ".git", "__pycache__", ".ar", ".venv"),
                            symlinks=True)
            slot = Slot(name, path, self.owner, "directory")
        self.slots[name] = slot
        self._record(slot)
        return slot

    def retain(self, name: str, reason: str | None) -> None:
        """Keep unharvested evidence, including through the outer finally."""
        slot = self.slots[name]
        slot.retained = reason
        self._record(slot)

    def release(self, name: str, force: bool = False) -> None:
        """Destroy one slot. Refuses if this pool does not hold it (H57/H59)."""
        marker = self.root / f"{name}.json"
        if marker.exists():
            recorded = json.loads(marker.read_text())
            if recorded.get("holder") != self.owner and not force:
                raise ClaimError(
                    f"refusing to release workspace {name!r}: held by "
                    f"{recorded.get('holder')!r}, not by {self.owner!r}. "
                    "Teardown without an ownership check removed other "
                    "sessions' work (H57).")
        held = self.slots.get(name)
        reason = held.retained if held else (
            recorded.get("retained") if marker.exists() else None)
        if reason and not force:
            raise ClaimError(f"workspace {name!r} retained at "
                             f"{held.path if held else self.root / name}: {reason}")
        slot = self.slots.pop(name, None)
        path = slot.path if slot else self.root / name
        if slot and slot.kind == "worktree":
            _git(self.config.paths.root, "worktree", "remove", "--force",
                 str(path), check=False)
            if slot.branch:
                _git(self.config.paths.root, "branch", "-D", slot.branch, check=False)
        elif path.exists():
            shutil.rmtree(path, ignore_errors=True)
        marker.unlink(missing_ok=True)

    def release_all(self) -> int:
        """Every slot this pool created. Called from the coordinator's `finally`,
        which is the wiring H91 found missing: teardown documented in a runbook
        and present in no loop leaves 64 of 64 worktrees behind. A release that
        fails is named on stderr, never swallowed -- teardown that silently
        leaks a slot is indistinguishable from a clean shutdown."""
        released = 0
        for name in list(self.slots):
            try:
                self.release(name)
                released += 1
            except Exception as exc:      # reported, not swallowed
                print(f"workspace release failed for {name!r}: {exc}",
                      file=sys.stderr)
        return released

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release_all()
        return False
