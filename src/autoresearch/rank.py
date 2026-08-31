"""Deciding what to work on next, as a computation rather than an essay.

In the harness this core is derived from, `claim-next` handed out the **topmost
queued entry, regardless of fit** (H92), and the only re-ranking was a human
curator appending prose: roughly thirty passes, about 1,500 lines. Nothing could
sort, because there was no priority to sort on; and nothing checked a ranking
against the queue, so one curation ranked a terminal entry first, four hours
after it closed (H140).

Two design choices matter more than the formula.

**Hard filters run before scoring, and they are the closure taxonomy made
operational.** The source harness invented `mechanism | slope | cell` to say how
far a refutation reaches, and then used it only as documentation. Here it
decides:

* a `mechanism` refutation holds outside the range measured, so a queued entry
  sharing that mechanism is **excluded** -- this is what stops the fleet
  re-running dead work, which is the failure the whole never-delete rule exists
  to prevent;
* a `slope` or `cell` refutation holds only inside the band measured, so a
  sharing entry is **penalised, not excluded** -- it re-opens outside the band,
  and excluding it would be exactly the over-claim the taxonomy was invented to
  stop.

**The formula proposes; a judge may reorder within the shortlist.** The numbers
cannot encode everything -- that is why the human curator existed -- but a judge
that can also *resurrect* a hard-filtered entry can undo the exclusion above, so
it cannot. `apply_veto` refuses it, and refuses a veto with no justification.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class Score:
    entry_id: str
    title: str
    score: float = 0.0
    terms: dict = field(default_factory=dict)
    excluded: str | None = None      # the hard filter that removed it, if any

    def explain(self) -> str:
        if self.excluded:
            return f"{self.entry_id:6} EXCLUDED  {self.excluded}"
        terms = "  ".join(f"{k}={v:.4g}" for k, v in self.terms.items())
        return f"{self.entry_id:6} {self.score:9.4f}  {terms}   {self.title[:60]}"


@dataclass
class Ranking:
    scored: list[Score]
    excluded: list[Score]
    calibration: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def shortlist(self, k: int) -> list[Score]:
        return self.scored[:k]

    def explain(self) -> str:
        out = [f"ranked {len(self.scored)} candidate(s), "
               f"excluded {len(self.excluded)}"]
        out += ["", "score = confidence x impact / cost x staleness x overlap", ""]
        out += ["  " + s.explain() for s in self.scored]
        if self.excluded:
            out += ["", "excluded by hard filter:"]
            out += ["  " + s.explain() for s in self.excluded]
        if self.calibration:
            out += ["", "calibration (confirm rate by mechanism, from history):"]
            out += [f"  {m:28} {v['rate']:.2f}  (n={v['n']})"
                    for m, v in sorted(self.calibration.items())]
        out += ["  " + n for n in self.notes] if self.notes else []
        return "\n".join(out)


def calibrate(entries, machine_for) -> dict:
    """Observed confirm-rate per mechanism tag, from closed entries.

    This is why migrating the historical corpus matters: 257 reached verdicts is
    a real prior. Without it, `confidence` is whatever the filing agent guessed,
    and an agent's guess about its own idea is the least reliable number in the
    system.
    """
    tally: dict[str, list[int]] = {}
    for entry in entries:
        machine = machine_for(entry.id)
        if not machine.status(entry.status).terminal or not entry.result:
            continue
        good = entry.result.verdict in ("confirmed", "fixed")
        for mechanism in entry.mechanisms:
            tally.setdefault(mechanism, []).append(1 if good else 0)
    return {m: {"rate": sum(v) / len(v), "n": len(v)} for m, v in tally.items()}


def _dead_mechanisms(entries, machine_for):
    """Mechanisms closed by a refutation, split by how far the refutation reaches."""
    hard, soft = {}, {}
    for entry in entries:
        machine = machine_for(entry.id)
        if not machine.status(entry.status).terminal or not entry.result:
            continue
        if entry.result.verdict != "refuted":
            continue
        bucket = hard if entry.result.closure_kind == "mechanism" else soft
        for mechanism in entry.mechanisms:
            bucket.setdefault(mechanism, entry.id)
    return hard, soft


def rank(entries, config, *, prior_weight: float = 3.0,
         verdicts_since=None, budget_ok=None) -> Ranking:
    """Score every claimable entry. Returns the ranking and why each entry sits
    where it does -- an unexplained ranking is one nobody can correct."""
    machine_for = lambda eid: config.track_for(eid).machine   # noqa: E731
    calibration = calibrate(entries, machine_for)
    hard_dead, soft_dead = _dead_mechanisms(entries, machine_for)
    # Closure dates, so staleness can ask "what has the board learned since this
    # entry was last priced" rather than "how much has it ever learned". The
    # first version compared against the total closed count, which on a corpus
    # with 400 verdicts crushed every entry's staleness to 0.05 and made the
    # term meaningless -- a penalty that applies equally to everything is not a
    # penalty, it is a constant factor.
    closure_dates = sorted(
        e.result.at for e in entries
        if e.result is not None and getattr(e.result, "at", None))

    scored, excluded = [], []
    for entry in entries:
        machine = machine_for(entry.id)
        card = Score(entry_id=entry.id, title=entry.title)

        # -- hard filters, in the order a human would apply them ----------
        if machine.status(entry.status).terminal:
            card.excluded = f"terminal ({entry.status}) — H140"
        elif entry.claim is not None:
            card.excluded = f"held by {entry.claim.session}"
        elif entry.status != machine.initial:
            card.excluded = f"not claimable from {entry.status!r}"
        elif budget_ok is not None and not budget_ok(entry):
            card.excluded = f"cost {entry.cost:g} exceeds the remaining budget"
        else:
            dead = [m for m in entry.mechanisms if m in hard_dead]
            if dead:
                card.excluded = (
                    f"mechanism {dead[0]!r} was refuted by {hard_dead[dead[0]]} "
                    "with closure_kind=mechanism, which holds outside the range "
                    "measured")
        if card.excluded:
            excluded.append(card)
            continue

        # -- the formula ---------------------------------------------------
        # Confidence is the filing agent's number, pulled toward the observed
        # rate for its mechanisms. With no history it is unchanged; with a lot
        # of history the prior dominates. Bayesian in spirit, deliberately
        # simple, and auditable by hand -- which a fitted model would not be.
        confidence = entry.confidence
        observed = [calibration[m] for m in entry.mechanisms if m in calibration]
        if observed:
            n = sum(o["n"] for o in observed)
            rate = sum(o["rate"] * o["n"] for o in observed) / n
            confidence = ((entry.confidence * prior_weight + rate * n)
                          / (prior_weight + n))

        impact = max(entry.impact, 1e-9)
        cost = max(entry.cost, 1e-9)

        # Staleness: how much the board has learned since this entry was priced.
        # Measured in verdicts rather than in wall-clock, because a queue that
        # sat still for a week is not stale and one that saw twenty refutations
        # in an hour is. `verdicts_since` is injected so the caller decides how
        # to count them.
        learned = (verdicts_since(entry) if verdicts_since
                   else sum(1 for at in closure_dates if at > entry.updated))
        staleness = 1.0 / (1.0 + 0.05 * max(0, learned))

        # Soft overlap: a slope/cell refutation touching this mechanism means
        # the band matters, not that the direction is dead.
        touching = [m for m in entry.mechanisms if m in soft_dead]
        overlap = 1.0 / (1.0 + len(touching))

        card.terms = {"confidence": confidence, "impact": impact, "cost": cost,
                      "staleness": staleness, "overlap": overlap}
        card.score = confidence * impact / cost * staleness * overlap
        if touching:
            card.terms["soft_dead"] = float(len(touching))
        scored.append(card)

    scored.sort(key=lambda s: -s.score)
    notes = []
    if hard_dead:
        notes.append(f"{len(hard_dead)} mechanism(s) closed by a "
                     f"closure_kind=mechanism refutation and hard-excluded")
    if soft_dead:
        notes.append(f"{len(soft_dead)} mechanism(s) closed by slope/cell and "
                     f"penalised rather than excluded — they re-open outside "
                     f"the band measured")
    return Ranking(scored=scored, excluded=excluded, calibration=calibration,
                   notes=notes)


@dataclass
class Veto:
    entry_id: str
    action: str          # "promote" | "demote" | "drop"
    justification: str


def apply_veto(ranking: Ranking, vetoes: list[Veto]) -> Ranking:
    """Let a judge reorder within the shortlist, under three rules.

    1. A veto must carry a justification. An unexplained reorder is the prose
       re-rank this module replaced.
    2. A judge may not resurrect a hard-filtered entry. The filters encode
       "this direction is closed by a mechanism" and "this entry is terminal",
       and an LLM overruling those reintroduces exactly the failure the
       never-delete record exists to prevent (H140).
    3. A drop is a demotion out of the shortlist, not a closure. Only `ar close`
       closes anything, and only against evidence.
    """
    excluded_ids = {s.entry_id for s in ranking.excluded}
    order = {s.entry_id: i for i, s in enumerate(ranking.scored)}
    scored = list(ranking.scored)
    notes = list(ranking.notes)

    for veto in vetoes:
        if not veto.justification.strip():
            raise ValueError(
                f"veto on {veto.entry_id} has no justification; an unexplained "
                "reorder is the prose re-rank this replaced")
        if veto.entry_id in excluded_ids:
            raise ValueError(
                f"veto on {veto.entry_id} would resurrect a hard-filtered entry. "
                "The filters encode closed mechanisms and terminal status; a "
                "judge may reorder within the shortlist, never overrule them.")
        if veto.entry_id not in order:
            raise ValueError(f"veto names {veto.entry_id}, which was not ranked")

        card = next(s for s in scored if s.entry_id == veto.entry_id)
        scored.remove(card)
        if veto.action == "promote":
            scored.insert(0, card)
        elif veto.action in ("demote", "drop"):
            scored.append(card)
        else:
            raise ValueError(f"unknown veto action {veto.action!r}")
        card.terms["veto"] = veto.action
        notes.append(f"veto: {veto.action} {veto.entry_id} — {veto.justification}")

    return Ranking(scored=scored, excluded=ranking.excluded,
                   calibration=ranking.calibration, notes=notes)
