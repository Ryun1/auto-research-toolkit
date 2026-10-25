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


@dataclass(frozen=True)
class Suspect:
    """#39: how much larger than expected a gain may be before it is held.

    A gain past `k` x the noise floor -- or, with no floor to read, past `k`
    x the largest step the frontier has accepted -- stays off the frontier
    until a clean re-measure from another session (same config, same epoch)
    reproduces it, or a person releases it with `ar promote`. `k` None is
    off, the default; a declared block without `k` takes a conservative 10.
    """
    k: float | None = None

    @classmethod
    def from_dict(cls, raw, gid) -> Suspect:
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise GoalError(f"goal {gid!r}: suspect must be a mapping like {{k: 10}}")
        unknown = set(raw) - {"k"}
        if unknown:
            raise GoalError(f"goal {gid!r}: suspect has unknown key(s) "
                            f"{sorted(unknown)}; known: ['k']")
        try:
            k = float(raw.get("k", 10.0))
        except (TypeError, ValueError) as exc:
            raise GoalError(f"goal {gid!r}: suspect.k: {exc}") from exc
        if k <= 1:
            raise GoalError(f"goal {gid!r}: suspect.k must be above 1")
        return cls(k)


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
        from .runs import started_at
        return min(started_at(r) for _, r in self.scored)

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


def _floor(goal, all_groups) -> float | None:
    """The noise floor in objective units, when there is one to read."""
    noise = goal.noise
    if noise.method == "declared":
        return noise.k * noise.sigma
    if noise.method in ("mad", "bootstrap"):
        sigma, _ = pooled_sigma(all_groups, noise)
        return noise.k * sigma if sigma is not None else None
    return None


def _suspicion(goal, gain, steps, all_groups) -> str | None:
    """Why a gain is too large to take on one execution's word, or None."""
    k = goal.suspect.k
    if k is None:
        return None
    floor = _floor(goal, all_groups)
    if floor:
        if gain > k * floor:
            return (f"suspect: gain {gain:.6g} is over {k:g}x the noise floor "
                    f"{floor:.6g}")
        return None
    if steps and gain > k * max(steps):
        return (f"suspect: gain {gain:.6g} is over {k:g}x the largest accepted "
                f"step {max(steps):.6g}")
    return None


def _released(goal, unit, beaten, by_config, promoted) -> bool:
    """A suspect unit stands once two sessions have each measured its config
    beating the incumbent it was held against -- the original and a clean
    re-measure -- or a person promoted one of its rows. Counting only rows
    that beat it means an earlier, worse row of the same config from another
    session is not a reproduction. The caller passes one epoch's rows, so the
    re-measure ran under the same evaluator."""
    if any(r.id in promoted for _, r in unit.scored):
        return True
    from .runs import source_of
    rows = by_config[config_digest(unit.scored[0][1])]
    # Sources, not session names (#43): a slot or a proven claim each; an
    # unclaimed row is nobody's second measurement.
    sources = {source_of(r) for v, r in rows if goal.better(v, beaten)}
    return len(sources - {None}) >= 2


def admit(goal, scored, promoted=frozenset()):
    """`(incumbent, held, suspects)` over one epoch's scored rows.

    Units are taken in order of first appearance: config groups under a noise
    method, single rows under `none`. A better unit displaces the incumbent
    only when its gain clears the noise floor (or there is no floor yet) and
    is not suspect (#39) -- or is suspect but released. `held` names the
    better groups left within noise; `suspects` the units held as suspect,
    each `(unit, reason)`.
    """
    from .runs import started_at
    rows_mode = goal.noise.method == "none"
    all_groups = groups(scored)
    units = ([Group(r.id, [(v, r)]) for v, r in
              sorted(scored, key=lambda pair: started_at(pair[1]))]
             if rows_mode else all_groups)
    by_config = {}
    for value, record in scored:
        by_config.setdefault(config_digest(record), []).append((value, record))
    incumbent, steps, suspects = None, [], []
    for unit in units:
        if incumbent is None:
            incumbent = unit
            continue
        beaten = incumbent.representative(goal)[0]
        value = unit.representative(goal)[0]
        if not goal.better(value, beaten):
            continue
        if not rows_mode and compare(goal, incumbent, unit,
                                     all_groups).admitted is False:
            continue
        gain = _gain(goal, beaten, value)
        why = _suspicion(goal, gain, steps, all_groups)
        if why and not _released(goal, unit, beaten, by_config, promoted):
            suspects.append((unit, why + "; held until another session "
                                         "re-measures this config, or `ar promote`"))
            continue
        steps.append(gain)
        incumbent = unit
    held = []
    if incumbent is not None and not rows_mode:
        top = incumbent.representative(goal)
        suspect_ids = {id(unit) for unit, _ in suspects}
        for group in all_groups:
            if (group is incumbent or id(group) in suspect_ids
                    or not goal.better(group.representative(goal)[0], top[0])):
                continue
            verdict = compare(goal, incumbent, group, all_groups)
            held.append((group, f"within noise of {top[1].id} "
                                f"({verdict.why})"))
    return incumbent, held, suspects


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
    baseline, _, _ = admit(goal, others)
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
