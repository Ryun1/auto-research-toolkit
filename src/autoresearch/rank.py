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

**A share of the shortlist follows the risk dial.** The formula is expected
value per unit cost, which is risk-neutral, and risk-neutral EV/cost is pure
exploitation: an honest long shot (confidence 0.10, impact 0.40, cost 8 ->
0.005) loses to a safe increment (0.85, 0.02, 1 -> 0.017) by 3.4x, and would
need impact > 1.0 -- more than the whole objective -- to draw level. Since the
tree, the exploitation axis has a name: **the incumbent** -- an entry with a
`parent`, a branch off work already in the record. A novel branch is a root:
no parent, new territory. `[coordinator] risk` (default 0.5, meaning 50/50)
declares what share of each shortlist goes to novel branches; the remainder
goes to incumbent refinement. Both partitions are ordered by the same score,
and an unfilled share returns to the other partition, so a short queue is
never shortlisted below `k` for want of a long shot.

This replaces the older `explore_fraction`/`coverage_fraction` reserves, which
ranked on `impact` alone and on untested mechanism tags: with lineage a
structural field, novelty no longer needs a priced proxy, and one dial is one
decision instead of two quotas whose sum had to be policed.

**A domain may own the score itself.** `[commands] score = "bin/score"` hands
ranking to a domain command -- the same seam shape as `bin/measure`. The core
still applies the hard filters and the risk partition; the domain decides what
a branch is worth inside them. A multi-objective domain can make its scorer
Pareto-aware; the scalar-vs-frontier trade is a domain decision, not core
policy. A seam failure refuses the rank phase (dispatch is skipped, the
iteration records why) rather than silently falling back to the formula.

**The formula proposes; a judge may reorder within the shortlist.** The numbers
cannot encode everything -- that is why the human curator existed -- but a judge
that can also *resurrect* a hard-filtered entry can undo the exclusion above, so
it cannot. `apply_veto` refuses it, and refuses a veto with no justification.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .errors import ConfigError

#: Share of each shortlist aimed at novel branches (entries with no `parent`).
#: The default is the neutral stance -- half the attention on improving the
#: incumbent, half on pursuing new territory. A domain overrides it with
#: `[coordinator] risk`; it is an appetite, not a formula output.
RISK = 0.5


@dataclass
class Score:
    entry_id: str
    title: str
    score: float = 0.0
    terms: dict = field(default_factory=dict)
    excluded: str | None = None      # the hard filter that removed it, if any
    novel: bool = False              # a root branch: no parent, new territory

    def explain(self) -> str:
        if self.excluded:
            return f"{self.entry_id:6} EXCLUDED  {self.excluded}"
        terms = "  ".join(
            f"{k}={v:.4g}" if isinstance(v, (int, float))
            and not isinstance(v, bool) else f"{k}={v}"
            for k, v in self.terms.items())
        lane = "  [novel]" if self.novel else "  [branch]"
        return (f"{self.entry_id:6} {self.score:9.4f}  {terms}   "
                f"{self.title[:60]}{lane}")


@dataclass
class Ranking:
    scored: list[Score]
    excluded: list[Score]
    calibration: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    risk: float = 0.0

    def shortlist(self, k: int) -> list[Score]:
        """The top `k` under the risk dial.

        `scored` stays in score order -- that is the auditable ranking, and
        the judge reviews it. The dial decides only how the slots split
        between the two partitions: `int(k * risk + 0.5)` slots for novel
        branches (no `parent`), the rest for incumbent refinement. Rounding is
        deliberate, not a floor: at the fanouts this loop actually runs
        (k=3, risk=0.2 -> 0.6) a floor would reserve nothing and the stance
        would exist only on paper.

        A partition with no candidates gives its share back to the other, so
        an empty novel lane never starves dispatch; `risk=1.0` sends every
        slot it can to novel branches and backfills only what the record
        cannot support. The returned picks are re-sorted by score so dispatch
        and display stay deterministic.
        """
        if k <= 0:
            return []
        # A single slot is never spent on a lottery ticket: with k=1 the dial
        # has nothing to split, and applying it literally would send every
        # one-slot iteration to the same partition -- a permanent bias, not
        # the stance the dial declares.
        if k == 1:
            return self.scored[:1]
        if len(self.scored) <= k:
            return list(self.scored)
        novel = [c for c in self.scored if c.novel]
        incumbent = [c for c in self.scored if not c.novel]
        n_novel = min(int(k * self.risk + 0.5), len(novel))
        n_incumbent = max(0, min(k - n_novel, len(incumbent)))
        # Only the genuinely unfilled share returns to the other partition:
        # a dial the record cannot support is a stance, not a quota to pad.
        n_novel = min(n_novel + (k - n_novel - n_incumbent), len(novel))
        picks = novel[:n_novel] + incumbent[:n_incumbent]
        return sorted(picks, key=lambda s: -s.score)

    def explain(self) -> str:
        out = [f"ranked {len(self.scored)} candidate(s), "
               f"excluded {len(self.excluded)}"]
        out += ["", "score = confidence x impact / cost x staleness x overlap"]
        if self.risk:
            out += [f"risk {self.risk:.0%}: that share of the shortlist goes "
                    "to novel branches (no parent); the rest refines the "
                    "incumbent"]
        out += [""]
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


def calibrate(entries, machine_for, candidate=None) -> dict:
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
        if entry.result.disposition != "experiment":
            continue
        if candidate is not None and not entry.result_applies_to(candidate):
            continue
        good = entry.result.verdict in ("confirmed", "fixed")
        for mechanism in entry.closure_mechanisms():
            tally.setdefault(mechanism, []).append(1 if good else 0)
    return {m: {"rate": sum(v) / len(v), "n": len(v)} for m, v in tally.items()}


def _dead_mechanisms(entries, machine_for):
    """Mechanisms closed by a refutation, split by how far the refutation reaches."""
    hard, soft = {}, {}
    for entry in entries:
        machine = machine_for(entry.id)
        if not machine.status(entry.status).terminal or not entry.result:
            continue
        if entry.result.verdict != "refuted" or entry.result.disposition != "experiment":
            continue
        bucket = hard if entry.result.closure_kind == "mechanism" else soft
        for mechanism in entry.closure_mechanisms():
            bucket.setdefault(mechanism, []).append(entry)
    return hard, soft


def rank(entries, config, *, prior_weight: float = 3.0,
         verdicts_since=None, budget_ok=None, host=None,
         risk: float = 0.0) -> Ranking:
    """Score every claimable entry. Returns the ranking and why each entry sits
    where it does -- an unexplained ranking is one nobody can correct.

    `risk` is the share of the shortlist `Ranking.shortlist` aims at novel
    branches (entries with no `parent`). Off by default so a library caller
    gets the score and nothing else; the loop and the CLI pass the domain's
    dial. Hard filters above the formula are unchanged by it."""
    if not 0.0 <= risk <= 1.0:
        raise ConfigError(
            f"risk must be in [0, 1], got {risk!r}; it is a share of the "
            "shortlist, not a probability of anything")
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
        elif host is not None and entry.hardware and entry.hardware in config.hardware:
            # Refuse before claiming, not after measuring. A run that silently
            # truncates to what fits reports a number for a configuration
            # nobody chose.
            from .hardware import check as _check
            capability = _check(host, config.hardware[entry.hardware])
            if not capability.ok:
                card.excluded = (f"host cannot run {entry.hardware!r}: "
                                 + "; ".join(capability.problems))
        if not card.excluded:
            dead = next(((m, closed) for m in entry.mechanisms
                         for closed in hard_dead.get(m, [])
                         if closed.result_applies_to(entry)), None)
            if dead:
                mechanism, closed = dead
                card.excluded = (
                    f"mechanism {mechanism!r} was refuted by {closed.id} "
                    "with closure_kind=mechanism within matching applicability")
        if card.excluded:
            excluded.append(card)
            continue

        # -- the formula ---------------------------------------------------
        # Confidence is the filing agent's number, pulled toward the observed
        # rate for its mechanisms. With no history it is unchanged; with a lot
        # of history the prior dominates. Bayesian in spirit, deliberately
        # simple, and auditable by hand -- which a fitted model would not be.
        confidence = entry.confidence
        applicable_calibration = calibrate(entries, machine_for, candidate=entry)
        observed = [applicable_calibration[m] for m in entry.mechanisms
                    if m in applicable_calibration]
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
        touching = [m for m in entry.mechanisms
                    if any(closed.result_applies_to(entry)
                           for closed in soft_dead.get(m, []))]
        overlap = 1.0 / (1.0 + len(touching))

        card.terms = {"confidence": confidence, "impact": impact, "cost": cost,
                      "staleness": staleness, "overlap": overlap}
        card.score = confidence * impact / cost * staleness * overlap
        if touching:
            card.terms["soft_dead"] = float(len(touching))
        card.novel = entry.parent == ""
        scored.append(card)

    scored.sort(key=lambda s: -s.score)
    notes = []
    if hard_dead:
        notes.append(f"{len(hard_dead)} mechanism(s) have hard refutations; "
                     "exclusion requires matching applicability (legacy unscoped "
                     "closures apply everywhere)")
    if soft_dead:
        notes.append(f"{len(soft_dead)} mechanism(s) have slope/cell refutations; "
                     "overlap penalties require matching applicability")
    return Ranking(scored=scored, excluded=excluded, calibration=calibration,
                   notes=notes, risk=risk)


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
                   calibration=ranking.calibration, notes=notes,
                   risk=ranking.risk)
