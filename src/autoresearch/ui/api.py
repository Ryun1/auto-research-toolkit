"""The JSON the page reads: thin projections over the loaders the CLI uses.

Nothing here decides a verdict. The board is `board.snapshot`, `ar board
--json`'s own document; an entry row is `ar entry list --json`'s. Results are
cached until the watcher says a resource they read has changed, so a page
refetching on every event costs one recomputation per change, not one per
tab.
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
    "board": {"entries", "claims", "iterations", "runs", "inbox", "skills", "state"},
    "summary": {"entries", "claims", "iterations", "runs", "inbox", "skills", "state"},
    "entries": {"entries", "claims", "state"},
}


class Api:
    def __init__(self, config):
        self.config = config
        self._cache: dict[str, object] = {}
        self._lock = threading.Lock()

    def invalidate(self, changed) -> None:
        with self._lock:
            for name in list(self._cache):
                if READS.get(name, set()) & set(changed):
                    del self._cache[name]

    def get(self, name: str):
        with self._lock:
            if name in self._cache:
                return self._cache[name]
        doc = board_mod.finite(getattr(self, "_" + name)())
        with self._lock:
            self._cache[name] = doc
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
