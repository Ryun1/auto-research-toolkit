"""The board's facts as data: one screen answering what is currently true.

`ar board` printed these as it computed them, so nothing else could read
them. The local UI must show exactly what `ar board` shows (#49), so the
facts are computed here once -- epoch segmentation, the frontier and the row
it left out, overruns, holdout status, other epochs' bests -- and both the
text board (`render.board_text`) and `ar board --json` are projections of the
same document. A second derivation is how a view comes to disagree with the
record it projects.

The document is plain data: numbers stay numbers (formatting is the
renderer's), and records are reduced to the fields a reader needs.
"""
from __future__ import annotations

import math

from . import budget as budget_mod
from . import epochs as epochs_mod
from . import holdout as holdout_mod
from . import runs as runs_mod
from . import skills as skills_mod
from .claims import Claims
from .entries import Store
from .errors import AutoresearchError


def snapshot(config, *, target=None, target_error=None, epoch=None) -> dict:
    """Everything `ar board` shows, for the current epoch or the one `epoch`
    names. The target is the caller's to resolve (probing runs the domain's
    command); `target_error` carries why it could not be.

    Raises `AutoresearchError` for an `epoch` the ledger does not have."""
    store = Store(config.paths.entries)
    entries = store.all()
    all_runs, skipped = runs_mod.read_with_skipped(config.paths.runs)
    # The frontier compares one instrument's rows (#40): the current epoch,
    # or the one `epoch` names. Every other epoch keeps its best row on the
    # board under its own heading, never on the frontier.
    listed = epochs_mod.segment(config, all_runs)
    label = None if epoch is None else str(epoch)
    picked = [e for e in listed if (e.current if label is None else e.label == label)]
    if label is not None and not picked:
        raise AutoresearchError(
            f"no epoch {label!r}; this ledger has "
            f"{', '.join(e.label for e in listed) or 'none'} (see `ar epochs`)")
    chosen = picked[0].rows if picked else []
    best, excluded = runs_mod.frontier(store, chosen, config.goal)
    doc = build(config, entries, all_runs, target,
                (best[0], best[1].entry or best[1].id) if best else None,
                skipped, best_metrics=best[1].metrics if best else None,
                unmet_required_gates=budget_mod.unmet_required_gates(
                    config.goal, store, best),
                excluded_best=excluded, overruns=overruns(config, store, entries))
    if best:
        doc["best"].update(entry=best[1].entry, run=best[1].id)
    doc["target_error"] = target_error
    doc["epoch"] = ({"label": picked[0].label, "current": picked[0].current}
                    if picked else None)
    doc["epoch_selected"] = label is not None
    doc["holdout"] = (holdout_mod.summary(config, best, target)
                      if holdout_mod.declared(config) else None)
    doc["other_epochs"] = []
    for other in listed:
        if not other.rows or (picked and other is picked[0]):
            continue
        top, _ = runs_mod.frontier(store, other.rows, config.goal)
        doc["other_epochs"].append({
            **describe_epoch(other),
            "best": ({"objective": top[0], "entry": top[1].entry,
                      "run": top[1].id} if top else None)})
    return doc


def build(config, entries, runs, target=None, best=None, skipped_runs=0,
          best_metrics=None, unmet_required_gates=(),
          excluded_best=None, overruns=None) -> dict:
    """The board's body from records already read: goal, best, tracks,
    iterations, measurements, skills. `best` is `(objective, entry or run
    id)`; `excluded_best` is `(objective, record, reason)` as
    `runs.frontier` returns it. `snapshot` fills `best.run` and leaves
    `best.entry` empty for a row filed against no entry."""
    goal = config.goal
    doc = {
        "domain": config.name,
        "goal": {"id": goal.id, "objective": goal.objective,
                 "direction": goal.direction, "derived": dict(goal.derived or {}),
                 "moving": bool(goal.target.moving)},
        "target": target,
        "best": None,
        "excluded_best": None,
    }
    if best is not None:
        value, label = best
        # GOAL MET is the stop decision's own answer: a stop_when clause is
        # the goal's definition of "met" (qsb requires +1% over a moving
        # target), so the board must not derive a second, looser one from
        # raw distance -- and the required gates the decision also honours
        # keep an objective-level win from reading as a win (sig.golf H2).
        # A zero target cannot be normalised against, so it has no distance.
        doc["best"] = {
            "objective": value, "entry": label, "run": None,
            "metrics": dict(best_metrics or {}),
            "distance": goal.distance_to(value, target) if target else None,
            "met": bool(goal.is_met(best_metrics or {}, target)) if target else None,
            "unmet_required_gates": list(unmet_required_gates)}
    if excluded_best is not None:
        # Named, never dropped silently (sig.golf H4): a reader has to be able
        # to tell a frontier that did not move from one whose fixture was
        # reclassified.
        value, record, reason = excluded_best
        doc["excluded_best"] = {"run": record.id, "entry": record.entry,
                                "objective": value, "reason": reason}

    doc["tracks"] = []
    for name, track in config.tracks.items():
        mine = [e for e in entries if track.is_id(e.id)]
        machine = track.machine
        counts, readiness = {}, {}
        for e in mine:
            counts[e.status] = counts.get(e.status, 0) + 1
            value = e.gate_readiness(machine.required_gates)
            readiness[value] = readiness.get(value, 0) + 1
        held = [e for e in mine if e.claim and not machine.status(e.status).terminal]
        queued = sorted((e for e in mine if e.status == machine.initial),
                        key=lambda e: -getattr(e, "_score", 0))
        doc["tracks"].append({
            "name": name, "title": track.title, "entries": len(mine),
            "counts": dict(sorted(counts.items())),
            "readiness": dict(sorted(readiness.items())),
            # #22: claims ran 27-47 h past a 2 h budget and only a curator's
            # manual check noticed. The board names the overrun on the line.
            "live_claims": [
                {"id": e.id, "session": e.claim.session, "at": e.claim.at,
                 "readiness": e.gate_readiness(machine.required_gates),
                 "overrun": _overrun((overruns or {}).get(e.id))}
                for e in held],
            "queued": len(queued),
            "queued_heads": [
                {"id": e.id, "title": e.title,
                 "readiness": e.gate_readiness(machine.required_gates)}
                for e in queued[:3]],
        })

    doc["iterations"] = _iterations(config)

    from .runs import started_at
    doc["measurements"] = {
        "rows": len(runs), "valid": sum(1 for r in runs if r.status == "ok"),
        "skipped": skipped_runs,
        "newest": [{"id": r.id, "status": r.status, "entry": r.entry,
                    "metrics": dict(sorted(r.metrics.items()))}
                   for r in sorted(runs, key=started_at)[-3:]],
    }

    # The skills line is here so the dominant mode -- domains driving work by
    # hand through claim/close -- can see what the distil mechanism has left
    # to do without running the coordinator. `outstanding` is the same pair
    # the `ar skill distil --dry-run` prints; an absent skills directory reads
    # as zero (the config.py Skills contract), so the line prints even on a
    # domain that has never distilled anything.
    found, _problems = skills_mod.read_all(config)
    skills_state = skills_mod.outstanding(config, entries)
    doc["skills"] = {"read": len(found), "stale": len(skills_state["stale"]),
                     "undistilled": len(skills_state["undistilled"])}
    return doc


def overruns(config, store, entries) -> dict:
    """Live claims past a ceiling they registered, by entry id (#22) -- the
    same test `ar budget` prints per claim."""
    claims = Claims(store, config, "board")
    out = {}
    for entry in entries:
        if not entry.claim or config.track_for(entry.id).machine.status(
                entry.status).terminal:
            continue
        over = claims.overrun(entry.id, runs_spent=budget_mod.claim_runs(config, entry))
        if over:
            out[entry.id] = over
    return out


def finite(doc):
    """`doc` with every non-finite number as None. Bare Infinity/NaN is not
    JSON, and spec-compliant parsers reject it; "no number" rides as null,
    the `ar budget --json` contract."""
    if isinstance(doc, float):
        return doc if math.isfinite(doc) else None
    if isinstance(doc, dict):
        return {k: finite(v) for k, v in doc.items()}
    if isinstance(doc, (list, tuple)):
        return [finite(v) for v in doc]
    return doc


def _overrun(over):
    return None if not over else {"meter": over[0], "spent": over[1], "cap": over[2]}


def describe_epoch(epoch) -> dict:
    sha, benchmark = epoch.key if epoch.key is not None else (None, None)
    return {"label": epoch.label, "current": epoch.current,
            "evaluator": sha, "benchmark": benchmark, "rows": len(epoch.rows)}


def _iterations(config) -> dict:
    """What the loop last did, from the record it wrote. The board answers
    what is true now, and "the coordinator stopped at a native handoff with
    one assignment outstanding" is a fact about now (sig.golf H25)."""
    from .render import last_iteration
    recorded = last_iteration(config)
    total = 0
    try:
        total = sum(1 for p in config.paths.iterations.glob("*.json")
                    if p.stem.isdigit())
    except OSError:
        pass
    last = None
    if recorded:
        number, it, out_of_band = recorded
        last = {"n": it.get("n", number), "out_of_band": out_of_band,
                "stop": it.get("stop"), "stop_detail": it.get("stop_detail"),
                "phases": [{"name": ph["name"], "did": ph.get("did", 0),
                            "error": ph.get("error")}
                           for ph in (it.get("phases") or [])]}
    return {"total": total, "last": last}
