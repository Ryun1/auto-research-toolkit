"""Spend that no run row and no brain cost file records.

The campaign money meter is rebuilt from disk at startup -- but only from
iteration records and attempts, which see exactly two spenders. Any other
spender (a curator's decision API, a rented GPU paid by hand, a model call the
domain routed around the brain) is invisible to a finite ceiling, and a ceiling
that cannot see a spender is not a ceiling. This ledger is where out-of-band
spend lands, on disk, next to the rest of the campaign's state.

The metering rule is the brain seam's: an unpriced row is unknown, never zero.
Unknown money refuses further spend until it is reconciled with a correction
row, because a ceiling that under-counts does not stop.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import budget
from .errors import AutoresearchError

SCHEMA = "ar-usage-1"
KINDS = {"api", "rental", "other"}
CORRECTION = "correction"


def path(config) -> Path:
    """`state/usage.jsonl`: committed with the corpus, like every meter."""
    return config.paths.claims.parent / "usage.jsonl"


def _cost(value):
    """None means unpriced; a number means finite and nonnegative."""
    if value is None:
        return None
    number = budget.usage_number(value)
    if number < 0:
        raise ValueError("cost must be nonnegative")
    return number


def _tool(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
        raise ValueError("tool must be a short name")
    return value


def append(config, session, tool, kind, cost, note, *, correction=None):
    """One row. `cost=None` is an unpriced row: unknown, never zero."""
    from .attempts import session_required
    session_required(session)
    _tool(tool)
    if kind not in KINDS and kind != CORRECTION:
        raise AutoresearchError(
            f"kind must be one of {sorted(KINDS) | {CORRECTION}}, got {kind!r}")
    if not str(note).strip():
        raise AutoresearchError("a usage row requires a nonempty note")
    try:
        cost = _cost(cost)
    except (ValueError, TypeError) as exc:
        raise AutoresearchError(f"invalid usage cost: {exc}") from exc
    row = {"schema": SCHEMA, "at": _now(), "session": session, "tool": tool,
           "kind": kind, "cost_usd": cost, "currency": config.policy.currency,
           "note": str(note).strip()}
    if kind == CORRECTION:
        if not isinstance(correction, int) or isinstance(correction, bool) or correction < 1:
            raise AutoresearchError("a correction names the line it corrects")
        row["target"] = correction
    elif correction is not None:
        raise AutoresearchError("only a correction row names a target line")
    target = path(config)
    target.parent.mkdir(parents=True, exist_ok=True)
    number = 1
    if target.exists():
        number = len(target.read_text().splitlines()) + 1
    with target.open("a") as stream:
        stream.write(json.dumps({"lineno": number, **row}, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return {**row, "lineno": number}


def _now():
    import datetime as dt
    return dt.datetime.now(dt.UTC).isoformat()


def rows(config) -> list[dict]:
    """Every row, validated in position order. A malformed row refuses rather
    than reading as absent: these rows feed the money ceiling, and a ledger
    that under-counts does not stop."""
    target = path(config)
    if not target.exists():
        return []
    out = []
    for number, line in enumerate(target.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if row["schema"] != SCHEMA or row["lineno"] != number:
                raise ValueError("schema or line position mismatch")
            _tool(row["tool"])
            from .attempts import session_required
            session_required(row["session"])
            if row["kind"] not in KINDS and row["kind"] != CORRECTION:
                raise ValueError(f"unknown kind {row['kind']!r}")
            row["cost_usd"] = _cost(row["cost_usd"])
            if not str(row["note"]).strip():
                raise ValueError("empty note")
            if row["currency"] != config.policy.currency:
                raise ValueError(
                    f"currency {row['currency']!r} is not the domain's "
                    f"{config.policy.currency!r}; money cannot be summed across currencies")
            if row["kind"] == CORRECTION:
                if not isinstance(row.get("target"), int) or row["target"] >= number:
                    raise ValueError("correction must name an earlier row")
                target_kind = next((r["kind"] for r in out
                                    if r["lineno"] == row["target"]), None)
                if target_kind == CORRECTION:
                    raise ValueError(
                        f"correction targets line {row['target']}, which is "
                        "itself a correction; price the original row")
        except (ValueError, TypeError, KeyError) as exc:
            raise AutoresearchError(
                f"{target}:{number}: invalid usage row ({exc}). These rows "
                "feed the campaign money ceiling, so a malformed one must be "
                "fixed or removed deliberately.") from exc
        out.append(row)
    return out


def effective(config) -> tuple[float | None, list[dict]]:
    """`(money, unpriced)` -- what the ledger says was spent, and the rows
    still waiting for a price. A correction replaces its target's cost."""
    ledger = rows(config)
    priced = {row["target"]: row for row in ledger if row["kind"] == CORRECTION}
    money = 0.0
    unpriced = []
    for row in ledger:
        if row["kind"] == CORRECTION:
            continue
        cost = priced[row["lineno"]]["cost_usd"] if row["lineno"] in priced else row["cost_usd"]
        if cost is None:
            unpriced.append(row)
        else:
            money += cost
    return (None if unpriced else money), unpriced


def reconcile(config, lineno, cost, session, note):
    """Price one unpriced row after the fact, by appending a correction."""
    if lineno not in {row["lineno"] for row in rows(config)}:
        raise AutoresearchError(f"no usage row at line {lineno}")
    from .attempts import session_required
    session_required(session)
    if cost is None:
        raise AutoresearchError("a correction states a price; an unpriced row is what it fixes")
    return append(config, session, "usage", CORRECTION, cost, note, correction=lineno)
