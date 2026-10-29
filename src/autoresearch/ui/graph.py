"""The knowledge graph's documents: nodes and links, and one entry's panel.

Links are `links.links`, the core's reading of the fields the record stores;
row standing is `runs.standing`, the frontier's own classification; overruns
are the board's. This module only shapes them for the page.
"""
from __future__ import annotations

import pathlib

from .. import board as board_mod
from .. import epochs as epochs_mod
from .. import links as links_mod
from .. import render
from .. import runs as runs_mod
from ..entries import Store

#: The largest memo the panel inlines; a bigger one is listed with its size.
MEMO_MAX = 256 * 1024


def graph(config) -> dict:
    store = Store(config.paths.entries)
    entries = store.all()
    links = links_mod.links(entries)
    linked = {link.src for link in links} | {link.dst for link in links}
    overruns = board_mod.overruns(config, store, entries)
    statuses = {}
    for track in config.tracks.values():
        for name in sorted(track.machine.reachable()):
            terminal = track.machine.status(name).terminal
            statuses.setdefault(name, {
                "terminal": terminal,
                # The mermaid view's meanings, so a status reads the same in
                # every picture of the record.
                "fill": render._MERMAID_FILL.get(
                    name, "#e0d5eb" if terminal else "#eeeeee")})
    return {
        "tracks": [{"id": t.id, "title": t.title, "prefix": t.prefix}
                   for t in config.tracks.values()],
        "statuses": statuses,
        "nodes": [{
            "id": e.id, "title": e.title, "track": e.track, "status": e.status,
            "hypothesis": e.hypothesis, "linked": e.id in linked,
            "claim": _claim(e, overruns.get(e.id)),
        } for e in sorted(entries, key=lambda e: e.id)],
        "links": [{"src": link.src, "dst": link.dst, "kind": link.kind}
                  for link in links],
    }


def standing(config) -> dict[str, dict]:
    """Every row by id: how the frontier of its own epoch treats it."""
    store = Store(config.paths.entries)
    out = {}
    for epoch in epochs_mod.segment(config, runs_mod.read_all(config.paths.runs)):
        kinds = runs_mod.standing(store, epoch.rows, config.goal)
        for record in epoch.rows:
            kind, reason = kinds[record.id]
            # Rows are agent-written and metric types are not checked on
            # read: only a number goes to the page as an objective.
            try:
                objective = config.goal.objective_value(record.metrics)
            except Exception:
                objective = None
            if isinstance(objective, bool) or not isinstance(objective, (int, float)):
                objective = None
            out[record.id] = {
                "id": record.id, "entry": record.entry, "standing": kind,
                "reason": reason, "epoch": epoch.label, "current": epoch.current,
                "objective": objective, "metrics": record.metrics,
                "session": str(record.session or ""),
                "started": runs_mod.started_at(record),
                "config": record.config}
    return out


def entry(config, entry_id: str, rows: dict) -> dict | None:
    """One entry's side panel, or None for an id the store does not have."""
    store = Store(config.paths.entries)
    entries = store.all()
    by_id = {e.id: e for e in entries}
    e = by_id.get(entry_id)
    if e is None:
        return None
    record = e.to_dict()
    history = record.pop("history", [])
    if record.get("claim"):
        record["claim"].pop("token_sha", None)   # a hash, but nobody's to read here
        over = board_mod.overruns(config, store, [e]).get(e.id)
        record["claim"]["overrun"] = _overrun(over)
    links = [link for link in links_mod.links(entries) if entry_id in (link.src, link.dst)]
    root = config.paths.root
    return {
        "entry": record,
        "terminal": config.track_for(e.id).machine.status(e.status).terminal,
        "memo": _memo(root, e.result.memo) if e.result and e.result.memo else None,
        "upstream": [{"id": link.src, "kind": link.kind, "title": by_id[link.src].title}
                     for link in links if link.dst == entry_id],
        "downstream": [{"id": link.dst, "kind": link.kind, "title": by_id[link.dst].title}
                       for link in links if link.src == entry_id],
        "paths": [{"path": p, "exists": _inside(root, p) is not None
                   and _inside(root, p).exists()}
                  for p in links_mod.source_paths(e, by_id)],
        "runs": sorted((row for row in rows.values() if row["entry"] == entry_id),
                       key=lambda row: row["started"]),
        "history": history,
    }


def _claim(e, over):
    if not e.claim:
        return None
    return {"session": e.claim.session, "at": e.claim.at, "overrun": _overrun(over)}


def _overrun(over):
    return None if not over else {"meter": over[0], "spent": over[1], "cap": over[2]}


def _inside(root: pathlib.Path, rel: str) -> pathlib.Path | None:
    """`root / rel` if it stays inside the domain; never a path that climbs
    out of it, whatever a record says."""
    try:
        path = (root / rel).resolve()
        path.relative_to(root.resolve())
    except (ValueError, OSError):
        return None
    return path


def _memo(root, rel: str) -> dict:
    path = _inside(root, rel)
    out = {"path": rel, "exists": bool(path and path.is_file()), "text": None, "size": None}
    if not out["exists"]:
        return out
    out["size"] = path.stat().st_size
    if out["size"] <= MEMO_MAX:
        out["text"] = path.read_text(errors="replace")
    return out
