"""Domain-owned branch scoring: the seam where ranking judgement leaves core.

`[commands] score = "bin/score"` hands the *pricing* of claimable branches to a
domain command, the same seam shape as `bin/measure`. The core keeps what is
structural -- the hard filters, the risk partition, the shortlist arithmetic --
and the domain decides what a branch is worth inside them. A multi-objective
domain may make its scorer Pareto-aware; whether the goal is one scalar or a
frontier is a domain decision, not core policy.

Contract (mirrors `preflight`): the command runs as shell-free argv with the
domain root as cwd. Stdin is one JSON object:

    {"risk": <float>, "entries": [<entry dict>, ...]}

`entries` are the claimable candidates only -- hard filters have already run,
so the scorer never sees a terminal entry or a dead mechanism, and can never
resurrect one. Stdout is one JSON object:

    {"scores": [{"id": <entry id>, "score": <finite number>,
                 "reason": <string>}]}

Exactly one row per input entry, same id set. A nonzero exit, a malformed
object, a missing id or a non-finite score raises `ScoringError`: the rank
phase records the failure and the iteration skips dispatch. There is no
fallback to the formula -- a domain that installed a scorer must see it break,
not be silently rescued by the arithmetic it replaced.
"""
from __future__ import annotations

import dataclasses
import json
import math
import shlex
import subprocess

from .errors import AutoresearchError, PolicyError

#: The scorer may read the record, so it can outlast `preflight`'s 10s. A
#: branch score that needs longer than this is a scorer to fix, not a phase to
#: widen.
SCORE_TIMEOUT_SECONDS = 30


def seam_ranking(config, ranking, entries, risk: float):
    """Rebuild `ranking` with the domain's prices for its claimable entries.

    Hard filters and calibration are kept; only pricing changes. Raises
    `AutoresearchError` on any contract breach -- the caller records the
    reason and skips dispatch rather than guessing a substitute ranking.
    """
    from . import rank as rank_mod
    command = config.commands.get("score")
    if not command:
        return ranking
    by_entry_id = {s.entry_id for s in ranking.scored}
    claimable = [e for e in entries if e.id in by_entry_id]
    rows = domain_scores(config, claimable, risk)
    by_id = {r["id"]: r for r in rows}
    scored = [
        rank_mod.Score(entry_id=e.id, title=e.title, score=by_id[e.id]["score"],
                       # The declared cost rides with the score: dispatch and
                       # the run ceiling charge `card.terms["cost"]`, and a
                       # seam that dropped it would silently meter every card
                       # at the 1-run default.
                       terms={"domain": by_id[e.id]["reason"], "cost": e.cost},
                       novel=e.parent == "")
        for e in claimable]
    scored.sort(key=lambda s: -s.score)
    return rank_mod.Ranking(
        scored=scored, excluded=ranking.excluded,
        calibration=ranking.calibration, risk=risk,
        notes=[f"priced by {command}"])


def domain_scores(config, entries: list, risk: float) -> list[dict]:
    """Price the candidates through the domain's `score` command.

    Returns the scorer's rows, validated, in input-entry order. Raises
    `AutoresearchError` on any contract breach -- the caller records the
    reason and skips dispatch rather than guessing a substitute ranking.
    """
    command = config.commands.get("score")
    if not command:
        raise AutoresearchError("domain_scores called without a score command")
    try:
        config.policy.check_command(command)
        argv = shlex.split(command)
        if not argv:
            raise AutoresearchError("score command is empty")
        payload = {"risk": risk,
                   "entries": [dataclasses.asdict(e) for e in entries]}
        result = subprocess.run(
            argv, cwd=config.paths.root,
            input=json.dumps(payload),
            capture_output=True, text=True, timeout=SCORE_TIMEOUT_SECONDS)
        if result.returncode != 0:
            raise AutoresearchError(
                f"score command exited {result.returncode}: "
                f"{result.stderr.strip()}")
        verdict = json.loads(result.stdout)
        rows = (verdict or {}).get("scores")
        if not isinstance(rows, list):
            raise AutoresearchError(
                "score command must output one JSON object with "
                "scores: [{id, score, reason}]")
        by_id = {row.get("id"): row for row in rows
                 if isinstance(row, dict)}
        expected = {e.id for e in entries}
        missing = expected - set(by_id)
        if missing:
            raise AutoresearchError(
                f"score command returned no row for {sorted(missing)}")
        unknown = set(by_id) - expected
        if unknown:
            raise AutoresearchError(
                f"score command returned rows for unranked ids {sorted(unknown)}")
        out = []
        for entry in entries:
            row = by_id[entry.id]
            score = row.get("score")
            if isinstance(score, bool) or not isinstance(score, (int, float)) \
                    or not math.isfinite(score):
                raise AutoresearchError(
                    f"score for {entry.id} is not a finite number: {score!r}")
            reason = row.get("reason", "")
            if not isinstance(reason, str):
                raise AutoresearchError(
                    f"score reason for {entry.id} must be a string, "
                    f"got {reason!r}")
            out.append({"id": entry.id, "score": float(score),
                        "reason": reason})
        return out
    except PolicyError as exc:
        raise AutoresearchError(str(exc)) from exc
    except subprocess.TimeoutExpired as exc:
        raise AutoresearchError(
            f"score command timed out after {SCORE_TIMEOUT_SECONDS}s") from exc
    except json.JSONDecodeError as exc:
        raise AutoresearchError(
            f"score command output is not JSON: {exc}") from exc
