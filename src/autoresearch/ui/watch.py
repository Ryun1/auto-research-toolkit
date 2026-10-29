"""Which parts of the record changed, by polling mtimes.

Polling rather than fsevents/inotify keeps `ar ui` dependency-free, and a
campaign's record is small enough to walk every second: ECDSA Fail's 470
entries, 245 run files and 1,270 inbox items stat in milliseconds.

A change is reported once the tree has held still for one more poll, so a
verb that writes an entry and then re-renders its views is one event, not
two, and a half-written file is never what the page refetches.
"""
from __future__ import annotations

import os
import pathlib
import threading


def resources(config) -> dict[str, pathlib.Path]:
    """Resource name -> directory, for everything the page shows. A file is
    named by the most specific directory holding it, so `state/entries/Q1.yaml`
    is an `entries` change, not a `state` one."""
    root = config.paths.root
    return {
        "entries": config.paths.entries,
        "claims": config.paths.claims,
        "iterations": config.paths.iterations,
        "runs": config.paths.runs,
        "inbox": config.paths.memos,
        "skills": root / config.skills.dir,
        "guides": root / "guides",
        "state": root / "state",
    }


def signature(directory: pathlib.Path, skip=()) -> frozenset:
    """`(path, mtime_ns, size)` for every file under `directory`, leaving out
    the subtrees in `skip`. A missing directory is an empty one."""
    out = []
    skip = {str(p) for p in skip}
    for base, dirs, files in os.walk(directory):
        if base in skip:
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs if os.path.join(base, d) not in skip
                   and not d.startswith(".")]
        for name in files:
            path = os.path.join(base, name)
            try:
                st = os.stat(path)
            except OSError:
                continue                  # deleted between listing and stat
            out.append((path, st.st_mtime_ns, st.st_size))
    return frozenset(out)


class Watcher:
    """Polls every `interval` seconds and publishes the names of the
    resources that changed. Readers block in `wait(seen)` for a generation
    newer than the one they last saw."""

    def __init__(self, config, interval: float = 1.0):
        self.dirs = resources(config)
        self.interval = interval
        self.generation = 0
        self._log: list[tuple[int, frozenset]] = []
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._thread = None
        self._last = self._scan()

    def _scan(self) -> dict[str, frozenset]:
        out = {}
        for name, directory in self.dirs.items():
            # Each directory nested inside this one is its own resource.
            inner = [d for other, d in self.dirs.items()
                     if other != name and d != directory and directory in d.parents]
            out[name] = signature(directory, skip=inner)
        return out

    def poll(self) -> set[str]:
        """One scan; the resources whose files differ from the last scan.
        A tree still moving is scanned again until it holds still."""
        now = self._scan()
        changed = {k for k in now if now[k] != self._last.get(k)}
        while changed and not self._stop.is_set():
            self._stop.wait(self.interval / 2)
            settled = self._scan()
            moving = {k for k in settled if settled[k] != now[k]}
            now = settled
            if not moving:
                break
            changed |= moving
        self._last = now
        if changed:
            self.publish(changed)
        return changed

    def publish(self, names) -> None:
        with self._cond:
            self.generation += 1
            self._log.append((self.generation, frozenset(names)))
            del self._log[:-256]
            self._cond.notify_all()

    def wait(self, seen: int, timeout: float) -> tuple[int, set[str]]:
        """Block until a generation newer than `seen` (or `timeout`); return
        the newest generation and every resource changed since `seen`."""
        with self._cond:
            self._cond.wait_for(lambda: self.generation > seen or self._stop.is_set(),
                                timeout=timeout)
            names = set()
            for gen, changed in self._log:
                if gen > seen:
                    names |= changed
            if self._log and self._log[0][0] > seen + 1:
                names = set(self.dirs)    # fell out of the log: say everything
            return self.generation, names

    def start(self) -> None:
        def run():
            while not self._stop.wait(self.interval):
                try:
                    self.poll()
                except OSError:
                    pass                  # a directory mid-rename; next poll
        self._thread = threading.Thread(target=run, name="ar-ui-watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
