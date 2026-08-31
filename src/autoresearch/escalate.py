"""When to stop buying local time, and what to buy instead.

A research loop on a laptop has three ways to fail that look identical from the
inside: the machine cannot hold the problem, the machine is too slow to finish
before the answer stops mattering, and the idea is wrong. Only the first two are
fixed by renting compute, and telling them apart is the whole job here.

The discipline is taken from a guide in the prior corpus that gated exactly this
decision, because renting on a hunch had already cost real money:

* **Gate 0 — the thing is already correct locally.** A rented hour spent finding
  a port bug is an hour that bought nothing. This gate is asserted by the
  domain, not inferred: core cannot know whether your kernel is right.
* **Gate A — the budget reaches a rung.** Compute has to change the *answer*, not
  just the wall clock. Being 10x faster at something that needs 10,000x is not
  a reason to spend.

And one rule of its own, which the corpus paid for twice: **core never invents a
speedup.** A ratio is a property of workload x machine x concurrency, and the
same corpus measured an Apple GPU at 0.82x on one machine and an NVIDIA 4090 at
11.1x -- 38.1x once the memory layout was coalesced -- on the *same* workload. No
plausible-looking multiplier is available from hardware specs. Either a ratio was
measured on that class for that workload, or the verdict is
`NEEDS_MEASUREMENT`, never a guess.

The output is a recommendation with its arithmetic shown. Nothing here spends
money: renting is a human gate.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .errors import ConfigError
from .hardware import Throughput

GO, REFUSE, NEEDS_MEASUREMENT, NOT_TRIGGERED = (
    "GO", "REFUSE", "NEEDS_MEASUREMENT", "NOT_TRIGGERED")

# Why we are even asking.
CAPACITY, TOO_SLOW, STALLED = "capacity", "too-slow", "stalled"


@dataclass
class RemoteClass:
    """A rentable machine class, as the domain declares it.

    `measured_ratio` is throughput on this class divided by throughput on the
    local host, **for this workload, at a stated concurrency**. Leave it None
    until someone has measured it; that is not a gap to fill with a spec sheet.
    """
    name: str
    usd_per_hour: float
    measured_ratio: float | None = None
    measured_at_concurrency: int | None = None
    measured_on: str = ""            # note: which run established it
    notes: str = ""

    @classmethod
    def from_dict(cls, spec: dict) -> "RemoteClass":
        try:
            return cls(name=spec["name"], usd_per_hour=float(spec["usd_per_hour"]),
                       measured_ratio=(None if spec.get("measured_ratio") is None
                                       else float(spec["measured_ratio"])),
                       measured_at_concurrency=spec.get("measured_at_concurrency"),
                       measured_on=spec.get("measured_on", ""),
                       notes=spec.get("notes", ""))
        except KeyError as exc:
            raise ConfigError(f"remote class is missing {exc}: {spec!r}") from exc


@dataclass
class Escalation:
    verdict: str
    trigger: str = ""
    detail: str = ""
    lines: list[str] = field(default_factory=list)
    best: RemoteClass | None = None
    usd: float | None = None
    hours: float | None = None

    @property
    def should_ask_human(self) -> bool:
        return self.verdict == GO

    def report(self) -> str:
        head = f"escalation: {self.verdict}"
        if self.trigger:
            head += f"  (trigger: {self.trigger})"
        out = [head]
        if self.detail:
            out.append(f"  {self.detail}")
        out += [f"  {line}" for line in self.lines]
        if self.verdict == GO:
            out.append("  Renting spends money, which is a human decision. This "
                       "is a recommendation with its arithmetic, not an action.")
        return "\n".join(out)


def triggered(*, capability=None, hours_needed: float | None = None,
              hours_available: float | None = None,
              stalled_iterations: int = 0, stall_threshold: int = 0) -> str | None:
    """Why local compute is not enough -- or None, which is the usual answer.

    Deliberately narrow. "The loop is not finding anything" is *not* by itself a
    compute problem; it is usually a hypothesis problem, and buying a GPU to run
    bad ideas faster is the most expensive way to learn that.  `STALLED` is only
    returned when the caller has established the stall is compute-bound.
    """
    if capability is not None and not capability.ok:
        return CAPACITY
    if (hours_needed is not None and hours_available is not None
            and hours_needed > hours_available):
        return TOO_SLOW
    if stall_threshold and stalled_iterations >= stall_threshold:
        return STALLED
    return None


def recommend(*, trigger: str | None, local: Throughput, units_needed: float,
              classes, hours_available: float | None = None,
              spend_ceiling: float | None = None,
              gate0_correct_locally: bool = False,
              gate0_note: str = "") -> Escalation:
    """Should we rent, which class, and what does the arithmetic say.

    `units_needed` is in the same unit as `local` -- candidates, shots, runs.
    """
    if trigger is None:
        return Escalation(NOT_TRIGGERED,
                          detail="local hardware is meeting the need")

    if local.value <= 0:
        raise ConfigError("local throughput must be positive to extrapolate")

    local_hours = units_needed / local.value / 3600.0
    lines = [f"local       {local.label()}",
             f"need        {units_needed:,.0f} {local.unit}"
             f"  ->  {local_hours:,.1f} h locally"]
    if hours_available is not None:
        lines.append(f"available   {hours_available:,.1f} h before this stops "
                     f"mattering")

    # Gate 0. Asserted by the domain, never inferred: core cannot know whether
    # your port is correct, and guessing in the permissive direction is how an
    # hour gets spent debugging on a rented box.
    if not gate0_correct_locally:
        return Escalation(
            REFUSE, trigger=trigger,
            detail="Gate 0 fails: the work is not yet proven correct on local "
                   "hardware." + (f" {gate0_note}" if gate0_note else ""),
            lines=lines + [
                "Renting to debug is the failure this gate exists to prevent. "
                "Get it correct locally -- at whatever concurrency the host "
                "allows -- then re-ask."])

    priced = [c for c in classes if c.measured_ratio]
    unmeasured = [c for c in classes if not c.measured_ratio]
    if not priced:
        return Escalation(
            NEEDS_MEASUREMENT, trigger=trigger,
            detail=f"no candidate class has a measured ratio for this workload "
                   f"({len(unmeasured)} declared, all unmeasured)",
            lines=lines + [
                f"unmeasured  {', '.join(c.name for c in unmeasured)}",
                "A speedup cannot be read off a spec sheet. The same workload "
                "measured 0.82x on one GPU and 38.1x on another; nothing about "
                "the hardware predicted either. Measure one hour on the "
                "cheapest candidate, then re-ask."])

    best, best_cost, best_hours = None, None, None
    for c in sorted(priced, key=lambda c: -c.measured_ratio):
        hours = local_hours / c.measured_ratio
        cost = hours * c.usd_per_hour
        note = (f"  [measured {c.measured_ratio:.2f}x"
                + (f" @ {c.measured_at_concurrency}-way" if c.measured_at_concurrency else "")
                + (f", {c.measured_on}" if c.measured_on else "") + "]")
        lines.append(f"  {c.name:22} {hours:8,.1f} h   ${cost:9,.2f}{note}")
        if best is None or cost < best_cost:
            best, best_cost, best_hours = c, cost, hours
    for c in unmeasured:
        lines.append(f"  {c.name:22} {'—':>8}   {'—':>10}  [no measured ratio "
                     f"for this workload; not costed]")

    # Gate A: does the spend reach a rung -- does it change the answer?
    if hours_available is not None and best_hours > hours_available:
        return Escalation(
            REFUSE, trigger=trigger, best=best, usd=best_cost, hours=best_hours,
            detail=f"Gate A fails: the best measured class still needs "
                   f"{best_hours:,.1f} h against {hours_available:,.1f} h "
                   f"available.",
            lines=lines + [
                f"Required ratio to fit: "
                f"{local_hours / hours_available:,.1f}x against the best "
                f"measured {best.measured_ratio:.2f}x. Renting buys wall clock, "
                "not the answer. Change the problem, not the machine."])

    if spend_ceiling is not None and best_cost > spend_ceiling:
        return Escalation(
            REFUSE, trigger=trigger, best=best, usd=best_cost, hours=best_hours,
            detail=f"the cheapest sufficient class costs ${best_cost:,.2f}, "
                   f"over the ${spend_ceiling:,.2f} ceiling",
            lines=lines + ["Raising the ceiling is a human decision; this stops "
                           "rather than assuming it."])

    return Escalation(
        GO, trigger=trigger, best=best, usd=best_cost, hours=best_hours,
        detail=f"rent {best.name}: {best_hours:,.1f} h at "
               f"${best.usd_per_hour:,.2f}/h = ${best_cost:,.2f}",
        lines=lines)
