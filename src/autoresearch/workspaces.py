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

import fcntl
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import uuid
from contextlib import contextmanager
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

    @contextmanager
    def _lock(self):
        # Keep the inode: unlinking an advisory lock lets different processes
        # lock different files. The kernel releases this hold on process death.
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / "operation.lock").open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _check_recovery(self, name: str) -> pathlib.Path:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
            raise ClaimError("workspace name must be a single safe path component")
        pending = self.root / f"{name}.recovery"
        if pending.is_symlink() or pending.exists():
            raise ClaimError(
                f"workspace recovery already started; original archive and receipt "
                f"are at {pending.resolve()}; reconcile that recovery before reusing the slot")
        return pending

    def inspect(self, name: str) -> dict:
        """Read a recovery receipt without inferring that its holder is dead."""
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
            raise ClaimError("workspace name must be a single safe path component")
        marker = self.root / f"{name}.json"
        try:
            if marker.is_symlink():
                raise ClaimError("workspace marker must not be a symlink")
            raw = marker.read_bytes()
            row = json.loads(raw)
            expected = self.root / name
            if (row.get("name") != name or row.get("kind") not in {"directory", "worktree"}
                    or pathlib.Path(row.get("path", "")).absolute() != expected.absolute()
                    or expected.is_symlink() or not row.get("holder")):
                raise ClaimError(f"invalid workspace marker: {marker}")
            return {**row, "token": hashlib.sha256(raw).hexdigest(),
                    "exists": expected.exists(), "owner_liveness": "unknown"}
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            raise ClaimError(f"cannot inspect workspace {name!r}: {exc}") from exc

    def recover(self, name: str, *, holder: str, token: str, confirm_inactive: bool) -> dict:
        """Archive an inspected inactive slot; never delete unharvested evidence.

        The human caller attests inactivity. Age, PID and mtime are not proof.
        A changed marker invalidates the inspection token.
        """
        if not confirm_inactive:
            raise ClaimError("recovery requires explicit confirmation that the holder is inactive")
        with self._lock():
            pending = self._check_recovery(name)
            row = self.inspect(name)
            if row["holder"] != holder or row["token"] != token:
                raise ClaimError("workspace changed since inspection; inspect it again")
            source = self.root / name
            recovery_id = uuid.uuid4().hex
            destination = self.root / "recovered" / recovery_id
            destination.mkdir(parents=True)
            receipt = {**row, "recovered_by": self.owner,
                       "archive": str(destination / "workspace"), "status": "prepared"}
            receipt_path = destination / "receipt.json"
            receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
            # Publish the archive location before moving evidence. Leave this
            # guard on any failure: retry must not create a second, empty archive.
            pending.symlink_to(destination.absolute(), target_is_directory=True)
            if source.exists():
                if row["kind"] == "worktree":
                    branch = row.get("branch")
                    if branch != f"ar/{name}":
                        raise ClaimError("workspace branch does not match its slot")
                    _git(self.config.paths.root, "worktree", "move", str(source), receipt["archive"])
                    _git(self.config.paths.root, "branch", "-m", branch, f"ar/recovered-{recovery_id}")
                else:
                    os.rename(source, receipt["archive"])
            receipt["status"] = "archived"
            completed = destination / "receipt.completed.json"
            completed.write_text(json.dumps(receipt, indent=2) + "\n")
            os.replace(completed, receipt_path)
            (self.root / f"{name}.json").unlink()
            pending.unlink()
            self.slots.pop(name, None)
            return receipt

    def _record(self, slot: Slot) -> None:
        marker = self.root / f"{slot.name}.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(
            {"name": slot.name, "holder": slot.holder, "kind": slot.kind,
             "branch": slot.branch, "path": str(slot.path),
             "retained": slot.retained}))

    def acquire(self, name: str) -> Slot:
        with self._lock():
            return self._acquire(name)

    def _acquire(self, name: str) -> Slot:
        """Create one slot. Refuses to reuse an existing one silently (H70)."""
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
            raise ClaimError("workspace name must be a single safe path component")
        self._check_recovery(name)
        path = self.root / name
        if path.exists() or (self.root / f"{name}.json").exists():
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
        with self._lock():
            self._retain(name, reason)

    def _retain(self, name: str, reason: str | None) -> None:
        """Keep unharvested evidence, including through the outer finally."""
        slot = self.slots[name]
        slot.retained = reason
        self._record(slot)

    def release(self, name: str, force: bool = False) -> None:
        with self._lock():
            self._release(name, force)

    def _release(self, name: str, force: bool = False) -> None:
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
        slot = self.slots.get(name)
        if slot is None and marker.exists():
            row = self.inspect(name)
            slot = Slot(name, self.root / name, row["holder"], row["kind"], row.get("branch"), row.get("retained"))
        path = slot.path if slot else self.root / name
        if slot and slot.kind == "worktree":
            if path.exists():
                _git(self.config.paths.root, "worktree", "remove", "--force", str(path))
            if slot.branch:
                _git(self.config.paths.root, "branch", "-D", slot.branch)
        elif path.exists():
            shutil.rmtree(path)
        marker.unlink(missing_ok=True)
        self.slots.pop(name, None)

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
