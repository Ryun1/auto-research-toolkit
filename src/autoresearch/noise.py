"""Measurement noise: when a better number is not a better result (#37).

`best_run` took the single best `ok` row by strict comparison, so a 0.1% gain
from one noisy row read as a new best and `stop_when` could fire on it.
AIRA-dojo lost 9-13 medal points selecting on the searched metric, and
AIRA_2 puts most of that down to evaluation noise.

With a `noise` method declared in goal.yaml, rows group by config (a
canonical digest, so replications need no new row type) and each group
stands at a conservative centre. A group displaces the incumbent only when
its gain clears the floor; one that does not is named on the board as
"within noise of <incumbent>", never dropped silently (#9's precedent). A
`confirmed` closure must clear the same floor against the best rows of
every other entry.

Methods (computed, never stored -- invariant 4):

* `none` -- the default: today's behaviour, the single best row.
* `mad` -- pi-autoresearch's rule: the floor is k x 1.4826 x the median
  absolute deviation of every repeated config's rows from their own median,
  pooled over the epoch. It applies once `min_rows` such rows exist; before
  that there is no noise to measure and a gain is admitted as before.
* `bootstrap` -- a seeded bootstrap of the difference in means between the
  two groups; the gain is admitted when its `alpha` lower bound is above
  zero. Needs two rows a side.
* `declared` -- a domain-supplied sigma, for evaluators that cannot cheaply
  rerun; the floor is k x sigma.

Out of scope: correcting for the many comparisons a whole search makes.
That is a known limit; a held-out evaluation (#38) is the structural answer.
"""
from __future__ import annotations

import hashlib
import json
import random
import statistics
from dataclasses import dataclass

from .errors import AutoresearchError, GoalError

METHODS = ("none", "mad", "bootstrap", "declared")
MAD_SCALE = 1.4826            # MAD -> sigma for normally distributed noise


@dataclass(frozen=True)
class Noise:
    method: str = "none"
    #: how many sigmas a gain must clear (`mad`, `declared`)
    k: float = 2.0
    #: the declared measurement sigma, in objective units (`declared`)
    sigma: float | None = None
    #: the bootstrap's one-sided significance (`bootstrap`)
    alpha: float = 0.05
    #: repeated rows `mad` needs before its floor means anything
    min_rows: int = 3
    resamples: int = 2000

    @classmethod
    def from_dict(cls, raw, gid) -> Noise:
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise GoalError(f"goal {gid!r}: noise must be a mapping")
        known = {"method", "k", "sigma", "alpha", "min_rows", "resamples"}
        unknown = set(raw) - known
        if unknown:
            raise GoalError(f"goal {gid!r}: noise has unknown key(s) "
                            f"{sorted(unknown)}; known: {sorted(known)}")
        method = raw.get("method", "none")
        if method not in METHODS:
            raise GoalError(f"goal {gid!r}: noise.method must be one of "
                            f"{METHODS}, got {method!r}")
        try:
            noise = cls(method=method, k=float(raw.get("k", 2.0)),
                        sigma=(float(raw["sigma"]) if raw.get("sigma") is not None
                               else None),
                        alpha=float(raw.get("alpha", 0.05)),
                        min_rows=int(raw.get("min_rows", 3)),
                        resamples=int(raw.get("resamples", 2000)))
        except (TypeError, ValueError) as exc:
            raise GoalError(f"goal {gid!r}: noise: {exc}") from exc
        if noise.k <= 0:
            raise GoalError(f"goal {gid!r}: noise.k must be positive")
        if not 0 < noise.alpha < 0.5:
            raise GoalError(f"goal {gid!r}: noise.alpha must be in (0, 0.5)")
        if noise.min_rows < 2 or noise.resamples < 100:
            raise GoalError(f"goal {gid!r}: noise.min_rows must be >= 2 and "
                            "noise.resamples >= 100")
        if method == "declared" and (noise.sigma is None or noise.sigma < 0):
            raise GoalError(f"goal {gid!r}: noise.method declared needs a "
                            "non-negative noise.sigma")
        return noise


def config_digest(record) -> str:
    """Rows measuring one configuration share this; key order is not part of
    a configuration."""
    body = json.dumps(record.config or {}, sort_keys=True, default=str)
    return hashlib.sha256(body.encode()).hexdigest()[:12]


@dataclass
class Group:
    digest: str
    scored: list                     # [(objective, record)], in ledger order

    @property
    def values(self) -> list[float]:
        return [v for v, _ in self.scored]

    @property
    def first_seen(self) -> float:
        return min(r.started for _, r in self.scored)

    def representative(self, goal):
        """The row the group stands on: its median, and for an even count
        the worse of the two middle rows -- conservative, and a real row, so
        `is_met` reads metrics somebody actually measured."""
        ordered = sorted(self.scored, key=lambda pair: pair[0],
                         reverse=goal.direction != "minimise")
        return ordered[len(ordered) // 2]


def groups(scored) -> list[Group]:
    """`[(objective, record)]` -> config groups, by first appearance."""
    by = {}
    for value, record in scored:
        by.setdefault(config_digest(record), []).append((value, record))
    return sorted((Group(d, rows) for d, rows in by.items()),
                  key=lambda g: g.first_seen)


@dataclass
class Verdict:
    admitted: bool | None            # None: the floor cannot be computed yet
    why: str
    needed: int = 0


def _gain(goal, incumbent: float, candidate: float) -> float:
    return incumbent - candidate if goal.direction == "minimise" \
        else candidate - incumbent


def pooled_sigma(all_groups, noise) -> tuple[float | None, int]:
    """`(sigma, rows)` from every repeated config's spread about its median."""
    residuals = []
    for group in all_groups:
        if len(group.values) < 2:
            continue
        centre = statistics.median(group.values)
        residuals += [abs(v - centre) for v in group.values]
    if len(residuals) < noise.min_rows:
        return None, len(residuals)
    return MAD_SCALE * statistics.median(residuals), len(residuals)


def compare(goal, incumbent: Group, candidate: Group, all_groups) -> Verdict:
    """Does `candidate`'s gain over `incumbent` clear the floor?"""
    noise = goal.noise
    gain = _gain(goal, incumbent.representative(goal)[0],
                 candidate.representative(goal)[0])
    if noise.method in ("mad", "declared"):
        if noise.method == "declared":
            sigma = noise.sigma
        else:
            sigma, have = pooled_sigma(all_groups, noise)
            if sigma is None:
                need = noise.min_rows - have
                return Verdict(None, f"the noise floor needs {need} more "
                                     f"repeated run(s) of one config", need)
        floor = noise.k * sigma
        return Verdict(gain > floor, f"gain {gain:.6g} vs floor {floor:.6g}")
    if noise.method == "bootstrap":
        short = max(0, 2 - len(incumbent.values)) + max(0, 2 - len(candidate.values))
        if short:
            return Verdict(None, f"the bootstrap needs {short} more run(s) "
                                 f"(two per config compared)", short)
        seed = int(hashlib.sha256((incumbent.digest + candidate.digest)
                                  .encode()).hexdigest()[:12], 16)
        rng = random.Random(seed)
        a, b = incumbent.values, candidate.values
        diffs = sorted(_gain(goal, statistics.fmean(rng.choices(a, k=len(a))),
                             statistics.fmean(rng.choices(b, k=len(b))))
                       for _ in range(noise.resamples))
        lower = diffs[int(noise.alpha * len(diffs))]
        return Verdict(lower > 0, f"bootstrap lower bound {lower:.6g}")
    raise AutoresearchError(f"no noise comparison for method {noise.method!r}")


def admit(goal, scored):
    """The incumbent group after admission, and the groups held within noise
    of it: `(incumbent | None, [(group, reason)])`. Groups are taken in order
    of first appearance; a better group displaces the incumbent only when its
    gain clears the floor, or when there is no floor yet to clear."""
    all_groups = groups(scored)
    incumbent = None
    for group in all_groups:
        if incumbent is None:
            incumbent = group
            continue
        if not goal.better(group.representative(goal)[0],
                           incumbent.representative(goal)[0]):
            continue
        if compare(goal, incumbent, group, all_groups).admitted is not False:
            incumbent = group
    held = []
    if incumbent is not None:
        top = incumbent.representative(goal)
        for group in all_groups:
            if group is incumbent or not goal.better(
                    group.representative(goal)[0], top[0]):
                continue
            verdict = compare(goal, incumbent, group, all_groups)
            held.append((group, f"within noise of {top[1].id} "
                                f"({verdict.why})"))
    return incumbent, held


def confirmation_problems(goal, scored, entry_id) -> list[str]:
    """Why a `confirmed` closure of `entry_id` does not clear the floor.

    The baseline is the frontier the other entries hold; the entry's best
    config group must beat it beyond noise. With no baseline -- the first
    finding -- there is nothing to clear."""
    if goal.noise.method == "none":
        return []
    own = [(v, r) for v, r in scored if r.entry == entry_id]
    if not own:
        return [f"confirmed under noise.method {goal.noise.method} needs a "
                f"valid row of {entry_id} in the current epoch to test"]
    others = [(v, r) for v, r in scored if r.entry != entry_id]
    baseline, _ = admit(goal, others)
    if baseline is None:
        return []
    mine = groups(own)
    best = mine[0]
    for group in mine[1:]:
        if goal.better(group.representative(goal)[0], best.representative(goal)[0]):
            best = group
    base_value, base_row = baseline.representative(goal)
    if not goal.better(best.representative(goal)[0], base_value):
        return [f"{entry_id}'s rows do not beat the baseline {base_row.id} "
                f"({base_value:.6g}) at their median"]
    verdict = compare(goal, baseline, best, groups(scored))
    if verdict.admitted is None:
        return [f"{entry_id}: {verdict.why} before a confirmation can be "
                f"told from noise -- {verdict.needed} more run(s)"]
    if not verdict.admitted:
        return [f"{entry_id}'s gain over {base_row.id} is within noise "
                f"({verdict.why}); measure again or do not confirm"]
    return []
