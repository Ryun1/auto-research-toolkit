"""Meters. The thing that makes an unattended loop *bounded* rather than merely
unsupervised.

The source harness registered a ceiling with every claim -- and it was free
text. Its own note is honest about it: "Nothing meters it; its whole job is to
make a third exit sayable." That is a real function, but it left H125 open and
unfixed: **a claim can be worked indefinitely; nothing in the queue or the claim
record says when to stop.**

Four meters, because the four things that run out are different things:

* **claim** -- one agent on one entry: wall-clock and runs. Stops a worker that
  has stopped converging.
* **iteration** -- one coordinator pass: fan-out, subagent spawns, seconds. This
  is what makes a runaway iteration *structurally* impossible rather than
  merely discouraged: the coordinator allocates from a fixed pool, so it cannot
  spawn its way out of it.
* **domain** -- the campaign: total runs, GPU-hours, money. Money is a human
  gate, so its ceiling refuses rather than warns.
* **goal** -- not a resource at all, but the third exit: met, or yielding below
  a floor. Without it a loop can only be stopped by someone noticing.

Every meter answers `remaining()` and `report()`. A meter you cannot read is one
nobody checks, and a warning nobody can act on is worse than nothing (H138).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from .errors import AutoresearchError, BudgetExceeded


@dataclass
class Meter:
    """One resource with a ceiling. Spending past it raises; nothing warns."""
    name: str
    ceiling: float | None
    spent: float = 0.0
    unit: str = ""

    @property
    def unlimited(self) -> bool:
        return self.ceiling is None

    def remaining(self) -> float:
        return float("inf") if self.unlimited else max(0.0, self.ceiling - self.spent)

    def would_exceed(self, amount: float = 1.0) -> bool:
        return not self.unlimited and (self.spent + amount) > self.ceiling

    def spend(self, amount: float = 1.0, note: str = "") -> float:
        if self.would_exceed(amount):
            raise BudgetExceeded(
                self.name, self.spent + amount, self.ceiling,
                f"{self.name} exhausted: {self.spent + amount:g} of "
                f"{self.ceiling:g} {self.unit}".rstrip()
                + (f" ({note})" if note else "")
                + ". Checkpoint: report what you have, then release or renew "
                  "the claim with a reason.")
        self.spent += amount
        return self.remaining()

    def report(self) -> str:
        if self.unlimited:
            return f"{self.name:24} {self.spent:>10,.4g} spent   (no ceiling)"
        pct = 100.0 * self.spent / self.ceiling if self.ceiling else 0.0
        return (f"{self.name:24} {self.spent:>10,.4g} / {self.ceiling:<10,.4g}"
                f" {self.unit:12} {pct:5.1f}%")


class Budget:
    """A named set of meters, spent together and reported together."""

    def __init__(self, label: str, meters: dict[str, Meter]):
        self.label, self.meters = label, meters
        self.started = time.time()

    def __getitem__(self, name: str) -> Meter:
        try:
            return self.meters[name]
        except KeyError:
            raise BudgetExceeded(
                name, 0, 0,
                f"no meter {name!r} in budget {self.label!r}; "
                f"declared: {sorted(self.meters)}") from None

    def spend(self, name: str, amount: float = 1.0, note: str = "") -> float:
        return self[name].spend(amount, note)

    def exhausted(self) -> list[str]:
        return [n for n, m in self.meters.items()
                if not m.unlimited and m.remaining() <= 0]

    def report(self) -> str:
        lines = [f"budget: {self.label}"]
        lines += ["  " + m.report() for m in self.meters.values()]
        out = self.exhausted()
        if out:
            lines.append(f"  EXHAUSTED: {', '.join(out)}")
        return "\n".join(lines)


def claim_budget(entry, config) -> Budget:
    """What one worker may spend on one entry."""
    claim = entry.claim
    return Budget(f"claim {entry.id}", {
        "hours": Meter("hours", claim.max_hours if claim else
                       config.budgets.get("claim_wall_clock_hours"), unit="h"),
        "runs": Meter("runs", claim.max_runs if claim else
                      config.budgets.get("claim_max_runs"), unit="runs"),
    })


def iteration_budget(config) -> Budget:
    """What one coordinator pass may spend.

    `fanout` and `spawns` are the two that make a runaway impossible: the
    coordinator allocates workers and generators from this pool, so it cannot
    dispatch more than it was given no matter what it decides.
    """
    return Budget("iteration", {
        "fanout": Meter("fanout", config.budgets.get("iteration_fanout", 4),
                        unit="workers"),
        "spawns": Meter("spawns", config.budgets.get("iteration_max_spawns", 12),
                        unit="subagents"),
        "seconds": Meter("seconds", config.budgets.get("iteration_max_seconds"),
                         unit="s"),
        "runs": Meter("runs", config.budgets.get("iteration_max_runs"), unit="runs"),
    })


def domain_budget(config, spent_runs: float = 0.0, spent_money: float = 0.0,
                  spent_gpu_hours: float = 0.0) -> Budget:
    """What the whole campaign may spend. Money is a human gate."""
    budget = Budget("domain", {
        "runs": Meter("runs", config.budgets.get("domain_max_runs"),
                      spent=spent_runs, unit="runs"),
        "gpu_hours": Meter("gpu_hours", config.budgets.get("domain_max_gpu_hours"),
                           spent=spent_gpu_hours, unit="gpu-h"),
        "money": Meter("money", config.policy.spend_ceiling, spent=spent_money,
                       unit=config.policy.currency),
    })
    return budget


#: what one iteration's record says it consumed, keyed by the meter it feeds
USAGE_FIELDS = {"money": "cost_usd", "runs": "runs", "gpu_hours": "gpu_hours"}


def recorded_usage(config) -> dict[str, float]:
    """What this campaign has already consumed, read back from the iteration
    records on disk.

    A campaign meter has to survive a restart. Rebuilt from `spent=0.0` at every
    `ar loop`, a campaign ceiling is not a ceiling -- it is a per-invocation
    allowance anyone can renew by pressing up-arrow.

    The run ledger cannot answer this on its own: workers can report consumed
    runs without supplying records, and GPU-hours also live in iteration
    reports. Consumers adding ledger counts must subtract
    `recorded_run_overlap` so retained measurements are charged only once.
    Historical usage without explicit run IDs remains conservatively charged.
    """
    usage = dict.fromkeys(USAGE_FIELDS, 0.0)
    directory = config.paths.iterations
    if not directory.exists():
        return usage
    for path in sorted(directory.glob("*.json")):
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise AutoresearchError(
                f"{path}: not a readable iteration record ({exc}). The "
                "campaign budget is rebuilt from these records, so an "
                "unreadable one under-counts the ceiling -- and a budget that "
                "under-counts does not stop. Fix or remove it deliberately.") \
            from exc
        for meter, field_name in USAGE_FIELDS.items():
            try:
                usage[meter] += float(record.get(field_name) or 0.0)
            except (TypeError, ValueError, AttributeError) as exc:
                # The malformed-figure branch used to `continue`, so a figure
                # recorded_run_overlap refuses came back as 0.0 here -- the
                # under-count the comment below refuses to accept. Same rule,
                # one verdict.
                raise AutoresearchError(
                    f"{path}: meter {meter!r} figure "
                    f"{record.get(field_name)!r} is not a number. The campaign "
                    "budget is rebuilt from these records, so a malformed one "
                    "under-counts the ceiling -- and a budget that "
                    "under-counts does not stop. Fix or remove it "
                    "deliberately.") from exc
    return usage


def recorded_runs_by_entry(config) -> dict[str, dict]:
    """Per-entry consumption the coordinator attributed to a claim at charge
    time ({entry: {"session": ..., "runs": ...}}).

    Harvested rows carry the worker-slot session, so a held claim's overrun
    needs this attribution; it is the same consumption the campaign ceiling
    already charges, not an extra meter.
    """
    out: dict[str, dict] = {}
    directory = config.paths.iterations
    if not directory.exists():
        return out
    for path in sorted(directory.glob("*.json")):
        try:
            record = json.loads(path.read_text())
            attribution = record.get("runs_by_entry") or {}
            if not isinstance(attribution, dict):
                raise ValueError("runs_by_entry must be an object")
            for entry_id, row in attribution.items():
                if (not isinstance(entry_id, str) or not isinstance(row, dict)
                        or not isinstance(row.get("session"), str)):
                    raise ValueError(f"runs_by_entry[{entry_id!r}] is malformed")
                runs = float(row.get("runs") or 0.0)
                prior = out.get(entry_id) or {"session": row["session"], "runs": 0.0}
                out[entry_id] = {"session": prior["session"],
                                 "runs": prior["runs"] + runs}
        except (OSError, ValueError, TypeError) as exc:
            raise AutoresearchError(
                f"{path}: cannot read run attribution ({exc}). A claim's "
                "overrun is read from the same records as the campaign "
                "ceiling; a malformed one must not silently hide a "
                "breach.") from exc
    return out


def recorded_run_overlap(config, records) -> float:
    """Runs already counted by both the ledger and iteration consumption.

    Historical iterations without run IDs remain conservatively charged. A
    missing ledger row is never discounted, and repeated IDs cannot discount
    the same measurement twice.
    """
    remaining = {record.id for record in records}
    overlap = 0.0
    for path in sorted(config.paths.iterations.glob("*.json")):
        try:
            record = json.loads(path.read_text())
            ids = record.get("run_ids", [])
            consumed = float(record.get("runs") or 0.0)
            if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids):
                raise ValueError("run_ids must be a list of strings")
            matching = remaining.intersection(ids)
            overlap += min(len(matching), max(0.0, consumed))
            remaining.difference_update(matching)
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            raise AutoresearchError(f"{path}: cannot reconcile run accounting ({exc})") from exc
    return overlap


# -- the third exit --------------------------------------------------------

MET, YIELD, BUDGET, RUNNING = "goal-met", "yield-floor", "budget", "running"


@dataclass
class StopDecision:
    reason: str
    detail: str = ""
    meters: list[str] = field(default_factory=list)

    @property
    def should_stop(self) -> bool:
        return self.reason != RUNNING

    def __str__(self) -> str:
        return (f"{self.reason}: {self.detail}" if self.detail else self.reason)


def should_stop(config, *, measurements=None, target=None,
                verdicts_per_iteration=None, budgets=()) -> StopDecision:
    """Met, yielding below the floor, or out of budget -- otherwise keep going.

    This is deliberately the only place the loop can decide to end, so "when do
    we stop" has one answer rather than one per caller.
    """
    goal = config.goal
    if measurements is not None and target is not None:
        try:
            if goal.is_met(measurements, target):
                value = goal.objective_value(measurements)
                return StopDecision(
                    MET, f"objective {value:,.0f} meets target {target:,.0f}")
        except Exception:            # a malformed measurement is not a stop
            pass

    for budget in budgets:
        out = budget.exhausted()
        if out:
            return StopDecision(BUDGET, f"{budget.label}: {', '.join(out)}",
                                meters=out)

    if verdicts_per_iteration and goal.yield_floor.breached(verdicts_per_iteration):
        window = verdicts_per_iteration[-goal.yield_floor.over_iterations:]
        return StopDecision(
            YIELD,
            f"{sum(window) / len(window):.2f} confirmed/iteration over the last "
            f"{len(window)}, below the floor of "
            f"{goal.yield_floor.confirmed_per_iteration:.2f} — the queue is not "
            f"repaying the machine time")

    return StopDecision(RUNNING)
