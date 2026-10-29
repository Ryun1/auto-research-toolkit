"""The JSON the page reads: thin projections over the loaders the CLI uses.

Nothing here decides a verdict. The board is `board.snapshot`, `ar board
--json`'s own document; an entry row is `ar entry list --json`'s. Results are
cached until the watcher says a resource they read has changed, so a page
refetching on every event costs one recomputation per change, not one per
tab. Some facts move with the clock and no file -- a claim passing its
ceiling, a probed target -- so those documents also read `clock`, which the
watcher ticks every minute.
"""
from __future__ import annotations

import threading

from .. import board as board_mod
from ..entries import Store
from ..errors import AutoresearchError

#: Which resources each endpoint reads. A change to any of them drops the
#: cached document; `state` covers holdout verdicts, promotions and
#: everything else under it.
READS = {
    "board": {"entries", "claims", "iterations", "runs", "inbox", "skills", "state",
              "holdout", "clock"},
    "summary": {"entries", "claims", "iterations", "runs", "inbox", "skills", "state",
                "holdout", "clock"},
    "entries": {"entries", "claims", "state"},
    "graph": {"entries", "claims", "state", "runs", "clock"},
    # The current epoch moves with the evaluator's files and the benchmark
    # revision, which no watched directory holds: the clock re-reads it.
    "standing": {"entries", "runs", "state", "clock"},
    "entry": {"entries", "claims", "state", "runs", "inbox", "guides", "skills", "clock"},
}


class Api:
    def __init__(self, config):
        self.config = config
        self._cache: dict[str, object] = {}
        self._lock = threading.Lock()
        #: bumped by every invalidation: a document built across one is
        #: already stale and is returned but never cached.
        self._generation = 0

    def invalidate(self, changed) -> None:
        with self._lock:
            self._generation += 1
            for key in list(self._cache):
                if READS[key.split("/", 1)[0]] & set(changed):
                    del self._cache[key]

    def get(self, name: str, arg: str | None = None):
        """The document `name` (with `arg`, e.g. `entry/Q12`), from cache
        until a resource it reads changes."""
        key = name if arg is None else f"{name}/{arg}"
        with self._lock:
            if key in self._cache:
                return self._cache[key]
            started = self._generation
        build = getattr(self, "_" + name)
        doc = board_mod.finite(build() if arg is None else build(arg))
        with self._lock:
            # An unknown id is not worth remembering, and a build the record
            # moved under would pin the old document until the next change.
            if doc is not None and self._generation == started:
                self._cache[key] = doc
        return doc

    # -- endpoints --------------------------------------------------------

    def _board(self) -> dict:
        from ..cli import _probe_target
        try:
            target, error = _probe_target(self.config), None
        except AutoresearchError as exc:
            target, error = None, str(exc)
        return board_mod.snapshot(self.config, target=target, target_error=error)

    def _summary(self) -> dict:
        """The header: goal, best and distance to target."""
        doc = self.get("board")
        return {"domain": doc["domain"], "goal": doc["goal"], "target": doc["target"],
                "target_error": doc["target_error"], "best": doc["best"],
                "epoch": doc["epoch"]}

    def _entries(self) -> list[dict]:
        from ..cli import _entry_row
        return [_entry_row(e) for e in Store(self.config.paths.entries).all()]

    def _graph(self) -> dict:
        from . import graph
        return graph.graph(self.config)

    def _standing(self) -> dict:
        from . import graph
        return graph.standing(self.config)

    def _entry(self, entry_id: str) -> dict | None:
        from . import graph
        return graph.entry(self.config, entry_id, self.get("standing"))
