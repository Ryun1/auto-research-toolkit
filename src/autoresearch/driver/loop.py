"""The coordinator: one iteration, eight phases, every phase metered.

    orient -> generate -> rank -> dispatch -> curate -> distil -> qc -> stop?

Shaped after auditician's coordinator, with three changes the research setting
forces.

**Generation runs every iteration, concurrently with the work.** The source
harness generated only when fewer than three entries remained queued, and its
own debt log records what that cost: the rule *forbade draining a short queue*,
so an entry filed when the queue was short was skipped by every iteration after
it (H95). A queue that starves is a loop that stops; a rule that blocks
draining is a loop that never finishes anything.

**The coordinator owns the pool.** Workers get isolated workspaces created and
destroyed by the coordinator's own `finally`, so liveness is observed rather
than inferred and teardown cannot be documented-but-unwired (H91).

**Distillation is a phase, not a side effect.** A closed entry is one record
among hundreds, and the next agent reads whatever the brief hands it -- so a
constraint that is not in a skill gets paid for twice. `distil` promotes closed
work into skills that cite it, on a cadence, and `check` refuses a skill whose
evidence has since moved. It runs after `curate` so that `qc` checks what it
wrote in the same iteration.

**QC is mechanical first, model second.** Auditician's quality controller asks
an agent whether anything was skipped. Here `ar validate` answers most of that
without a model -- every claim resolved, every closure backed by a memo that
exists, every view matching its records -- and the QC role is asked only about
what code cannot check. A check that is really a grep should not cost a token.

Every phase records what it read and what it did, into an iteration record. A
phase that did nothing is distinguishable from a phase that did not run, which
is the distinction H98 found missing everywhere.
"""
from __future__ import annotations

import concurrent.futures as futures
import dataclasses
import datetime as dt
import json
import math
import os
import pathlib
import shlex
import time
from dataclasses import dataclass, field

from .. import attempts, preflight, render, scoring
from .. import budget as budget_mod
from .. import hardware as hw
from .. import rank as rank_mod
from .. import runs as runs_mod
from .. import skills as skills_mod
from ..claims import Claims
from ..entries import KINDS, Applicability, Entry, Event, Result, Store
from ..errors import AutoresearchError, BudgetExceeded
from ..workspaces import Pool
from .brain import Role, cost_value


def _iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


@dataclass
class Phase:
    name: str
    read: int = 0
    did: int = 0
    seconds: float = 0.0
    detail: list = field(default_factory=list)
    error: str | None = None

    def line(self) -> str:
        state = f"read {self.read}, did {self.did}, {self.seconds:.1f}s"
        return (f"  {self.name:10} {state}"
                + (f"  ERROR: {self.error}" if self.error else ""))


@dataclass
class Iteration:
    n: int
    started: str = field(default_factory=_iso)
    finished: str | None = None
    phases: list[Phase] = field(default_factory=list)
    shortlist: list[str] = field(default_factory=list)
    verdicts: dict = field(default_factory=dict)
    stop: str = "running"
    stop_detail: str = ""
    objective: float | None = None
    target: float | None = None
    cost_usd: float | None = 0.0
    #: "iteration" for a full pass of the loop; "out-of-band" for a single phase
    #: run by hand (`ar skill distil`). Both are recorded, because a ceiling
    #: that refuses to record an overrun is a ceiling that hides one -- but only
    #: the first is a sample of what the queue yields, so an out-of-band record
    #: that counted would drag the yield floor down and stop a healthy loop.
    kind: str = "iteration"
    #: what this iteration consumed, so a later invocation can charge it to the
    #: campaign's meters. An iteration record that does not say what it spent
    #: leaves every ceiling but `runs` starting from zero at the next `ar loop`.
    runs: float = 0.0
    gpu_hours: float = 0.0
    #: Consumption for the original claim instance, including its timestamp.
    runs_by_entry: dict[str, dict] = field(default_factory=dict)
    #: Retained ledger IDs, used to reconcile consumption on restart.
    run_ids: list[str] = field(default_factory=list)
    attempt_ids: list[str] = field(default_factory=list)

    def phase(self, name: str) -> Phase:
        p = Phase(name)
        self.phases.append(p)
        return p

    @classmethod
    def from_dict(cls, data: dict) -> Iteration:
        data = dict(data)
        if "cost_usd" in data:
            data["cost_usd"] = cost_value(data["cost_usd"])
        data["phases"] = [Phase(**p) if isinstance(p, dict) else p
                          for p in data.get("phases", [])]
        return cls(**data)

    @property
    def confirmed(self) -> int:
        return sum(1 for v in self.verdicts.values() if v in ("confirmed", "fixed"))

    def report(self) -> str:
        out = [f"iteration {self.n}  ({self.started} -> {self.finished or '...'})"]
        out += [p.line() for p in self.phases]
        if self.shortlist:
            out.append(f"  shortlist: {', '.join(self.shortlist)}")
        if self.verdicts:
            out.append("  verdicts:  " + ", ".join(
                f"{k}={v}" for k, v in self.verdicts.items()))
        if self.objective is not None and self.target is not None:
            out.append(f"  objective: {self.objective:,.0f} vs target {self.target:,.0f}")
        out.append(f"  stop:      {self.stop}"
                   + (f" — {self.stop_detail}" if self.stop_detail else ""))
        return "\n".join(out)


def read_history(directory) -> list[Iteration]:
    """Every iteration already recorded, in order.

    This directory was write-only: nothing read it back, so every new `ar loop`
    process started numbering at 1 again. Two things broke, and both bite
    hardest in the workflow the records exist to serve -- stopping work on one
    machine and resuming it on another.

    The audit trail of the previous session was overwritten, silently, by the
    session that resumed it. And `yield_floor` -- the third exit, the one that
    stops a loop that has stopped repaying its machine time -- needs
    `over_iterations` samples of confirmed-per-iteration and only ever saw what
    the current process had run. Resume often enough, or run `--iterations 1`,
    and that stop condition never fires at all.

    Malformed records are reported with their filename, never skipped: a reader
    that quietly drops a row is how a corpus grows history nobody can explain.
    """
    directory = pathlib.Path(directory)
    if not directory.exists():
        return []
    out = []
    for path in sorted(directory.glob("*.json")):
        try:
            out.append(Iteration.from_dict(json.loads(path.read_text())))
        except (ValueError, TypeError) as exc:
            raise AutoresearchError(
                f"{path}: not a readable iteration record ({exc}). It was "
                f"written by this loop, so a record it cannot read back is a "
                f"corrupt history, not something to resume past.") from exc
    return sorted(out, key=lambda i: i.n)


class Coordinator:
    """Runs iterations until the goal is met, a budget runs out, or the queue
    stops repaying the machine time."""

    def __init__(self, config, brain, session: str = "coordinator",
                 probe_target=None):
        self.config, self.brain, self.session = config, brain, session
        self.store = Store(config.paths.entries)
        self.claims = Claims(self.store, config, session)
        self.probe_target = probe_target
        # Resumed from disk, so stopping here and picking the work up on
        # another machine continues the numbering and the yield floor's
        # window rather than restarting both at one.
        self.history: list[Iteration] = read_history(config.paths.iterations)
        # Fixed here, not read off `history` later: that list grows as this
        # process runs, so a length read per-iteration reports work this
        # process did as work it resumed -- in the record whose only job is to
        # say what happened.
        self.resumed = len(self.history)
        self.resumed_through = self.history[-1].n if self.history else 0
        # A domain adopting this core arrives with a corpus in its own older
        # schema. Those rows are not counted against this campaign's run budget
        # -- the budget is for work this loop does -- but the count is kept and
        # reported rather than silently dropped, because "0 rows" and "0 rows we
        # can read, of 9,443" are different facts (H81/H96).
        # Detected once per coordinator: every measurement this iteration takes
        # is on this machine, and a figure that does not name its machine is not
        # a measurement.
        self.host = hw.detect()
        rows, self.foreign_rows = runs_mod.read_with_skipped(config.paths.runs)
        recorded = budget_mod.recorded_usage(config)
        self.domain_budget = budget_mod.domain_budget(
            config, spent_runs=budget_mod.total_runs(config),
            spent_money=recorded["money"],
            spent_gpu_hours=recorded["gpu_hours"])

    # -- helpers -----------------------------------------------------------

    def _target(self, detail=None):
        """The goal's target, or None with the reason on `detail`.

        A failed probe is recorded, never silent: a target that reads as None
        is indistinguishable from a domain that configured none (H98), and the
        goal-met exit is dead either way."""
        try:
            return self.config.goal.target.resolve(self.probe_target)
        except Exception as exc:      # the probe is domain input; attributed
            if detail is not None:
                detail.append(f"target probe failed: {exc}")
            return None

    def _best(self):
        """`(objective, record)` for the best measurement on disk, or None."""
        return runs_mod.best_run(runs_mod.read_all(self.config.paths.runs),
                                 self.config.goal)

    def _unmet_required_gates(self, best) -> tuple[str, ...]:
        """Required gate names the best run's owning entry has not passed.

        `best` carries the run record, and the record names its entry; the
        entry carries the gate outcomes. A run that names no entry, or names
        one the store cannot produce, counts as every gate unmet: a
        measurement nobody can trace to its evidence cannot inherit a proof.

        Empty when the goal declares no `required_gates`, so a goal without
        the linkage stops exactly as before."""
        required = self.config.goal.required_gates
        if not required or best is None:
            return ()
        record = best[1]
        if not record.entry:
            return required
        try:
            entry = self.store.load(record.entry)
        except Exception:            # unreadable evidence is no evidence
            return required
        states = {g.name: g.state for g in entry.gates}
        return tuple(name for name in required if states.get(name) != "passed")

    @staticmethod
    def _norm_title(title) -> str:
        return str(title).strip().lower()

    def _seen_titles(self) -> set[str]:
        """Every title already in the record, normalised: the H60 dedup key.
        One derivation -- generate and research must refuse the same
        duplicates, or the two paths file different ones."""
        return {self._norm_title(e.title) for e in self.store.all()}

    def _charge(self, it: Iteration, reply) -> None:
        """The one place model spend lands: this iteration's tally *and* the
        campaign's money meter.

        The meter was built from `policy.spend_ceiling` and then never spent, so
        `should_stop` could not reach the budget exit on money however long the
        loop ran. Recorded rather than `spend()`-ed: the money is already gone
        by the time we hear about it, and a ceiling that refuses to record an
        overrun is a ceiling that hides one.
        """
        cost = cost_value(getattr(reply, "cost_usd", None))
        it.cost_usd = None if cost is None or it.cost_usd is None else it.cost_usd + cost
        if cost is None:
            self.domain_budget["money"].unknown = True
        else:
            self.domain_budget["money"].spent += cost

    def _charge_runs(self, it: Iteration, runs: float, gpu_hours: float = 0.0) -> None:
        """Charge measurements to the campaign, and record them on the iteration.

        Reports and observed rows are reconciled before charging. Missing or
        malformed evidence does not refund consumption: the larger count wins.

        The iteration's *pool* of runs is allocated at dispatch, not here; this
        is consumption, which is a campaign-level fact.
        """
        self.domain_budget["runs"].spent += runs
        self.domain_budget["gpu_hours"].spent += gpu_hours
        it.runs += runs
        it.gpu_hours += gpu_hours

    def _charge_seconds(self, budget) -> float:
        """Wall clock is spent whether or not anyone spends it. Returns what is
        left of this iteration's seconds ceiling."""
        budget["seconds"].spent = time.time() - budget.started
        return budget["seconds"].remaining()

    def _require_money(self) -> None:
        money = self.domain_budget["money"]
        budget_mod.require_money(self.config, None if money.unknown else money.spent)

    def _ask(self, role, brief, *, workspace=None):
        """Keep historical campaign costs authoritative across backend restarts."""
        self._require_money()
        return self.brain.ask(role, brief, workspace=workspace)

    def _time_left(self, it: Iteration, budget, phase_name: str) -> bool:
        """Charge elapsed time and say whether this phase may start.

        `iteration_max_seconds` is scaffolded into every new domain, so a phase
        it skips has to be visible: a phase that did nothing and a phase that
        never ran are different facts, which is H98's whole subject.
        """
        try:
            self._require_money()
        except AutoresearchError as exc:
            it.phase(phase_name).error = f"not run: {exc}"
            return False
        if self._charge_seconds(budget) > 0:
            return True
        meter = budget["seconds"]
        it.phase(phase_name).error = (
            f"not run: the iteration's {meter.ceiling:g}s ceiling was spent "
            f"after {meter.spent:.1f}s")
        return False

    def _skill_index(self) -> tuple[list[dict], list[str]]:
        """Name, description and path for every readable skill, and what could
        not be read.

        By index and not by body, which is the entire point: thirteen skills at
        the source corpus's median are 2,730 lines, and a brief that inlines
        them has re-created the problem. A role routes on the description and
        reads the one that matches.

        The problems travel with it rather than being dropped. A skill that
        cannot be parsed is missing from this brief, and a role given no signal
        reads a short index as the whole of what is known -- which is the
        silent-drop failure `skills.read_all` exists to prevent.
        """
        found, problems = skills_mod.read_all(self.config)
        return skills_mod.index(self.config, found), problems

    def _brief(self, role: str, iteration: Iteration, **extra) -> str:
        """Everything a role needs, assembled once. The knowledge guides and the
        skills are included by path rather than inlined: a worker has tools and
        can read them, and a generator that cannot see the closed-directions map
        will propose closed directions."""
        entries = self.store.all()
        skill_index, skill_problems = self._skill_index()
        payload = {
            "role": role,
            "iteration": iteration.n,
            "domain": self.config.name,
            "goal": {
                "id": self.config.goal.id,
                "objective": self.config.goal.objective,
                "direction": self.config.goal.direction,
                "derived": self.config.goal.derived,
                "target": iteration.target,
                "best_so_far": iteration.objective,
            },
            "knowledge_paths": self.config.knowledge,
            "skills": skill_index,
            "skills_unreadable": skill_problems,
            "host": {
                "fingerprint": self.host.fingerprint,
                "chip": self.host.chip,
                "cpu_threads": self.host.cpu_threads,
                "memory_gb": round(self.host.memory_gb, 1),
                "gpu": self.host.gpu.describe(),
                "on_battery": self.host.on_battery,
                "note": "every throughput figure you report must name this "
                        "machine and the concurrency it was taken at",
            },
            "measure_command": self.config.commands.get("measure"),
            "open_entries": [
                {"id": e.id, "title": e.title, "status": e.status,
                 "mechanisms": e.mechanisms, "hypothesis": e.hypothesis}
                for e in entries
                if not self.config.track_for(e.id).machine.status(e.status).terminal],
            "closed_directions": [
                {"id": e.id, "verdict": e.result.verdict,
                 "closure_kind": e.result.closure_kind,
                 "mechanisms": e.mechanisms,
                 "reopen_condition": e.result.reopen_condition,
                 "summary": e.result.summary}
                for e in entries
                if e.result is not None],
            # Coverage map: how many terminal experiment entries have tested
            # each mechanism tag. A tag missing from this map, or with
            # `"tested": 0`, is untested -- the coverage reserve's lane, and
            # the one kind of proposal this board is shortest of.
            "mechanism_coverage": self._mechanism_coverage(entries),
            # Scoped sibling memory (AIRA arXiv 2507.02554 §4.1): what each
            # branchable parent's children already concluded, plus the
            # complexity cue a next child should aim at. A generator branching
            # off P reads P's family, not the whole record -- siblings that
            # differ push diversity; siblings that repeat are mode collapse.
            "branch_families": self._branch_families(entries),
            "budget_remaining": {
                # float("inf") serializes as bare `Infinity`, which is not
                # valid JSON: spec-compliant parsers reject it (the TypeSafe
                # System One endpoint answers HTTP 500). An unlimited meter
                # is "no number", so it rides as null.
                # float("inf") serializes as bare `Infinity`, which is not
                # valid JSON: spec-compliant parsers reject it (the TypeSafe
                # System One endpoint answers HTTP 500). An unlimited meter
                # is "no number", so it rides as null.
                name: (None if math.isinf(v := meter.remaining()) else v)
                for name, meter in self.domain_budget.meters.items()},
        }
        payload.update(extra)
        return json.dumps(payload, indent=2, default=str)

    # -- phases ------------------------------------------------------------

    def _mechanism_coverage(self, entries) -> dict:
        """Terminal experiment verdicts per mechanism tag, for the briefs.

        Generator, scout and judge all need the same fact -- which mechanism
        tags the record has actually tested and which it has only named -- so
        it is computed once here. Untested is the interesting row: a tag the
        board keeps proposing but never measuring is exactly the failure the
        coverage reserve exists to stop."""
        out: dict[str, dict] = {}
        for e in entries:
            if e.result is None:
                continue
            machine = self.config.track_for(e.id).machine
            if not machine.status(e.status).terminal:
                continue
            if e.result.disposition != "experiment":
                continue
            for m in e.closure_mechanisms():
                row = out.setdefault(m, {"tested": 0, "confirmed": 0,
                                         "refuted": 0})
                row["tested"] += 1
                if e.result.verdict in ("confirmed", "fixed"):
                    row["confirmed"] += 1
                elif e.result.verdict == "refuted":
                    row["refuted"] += 1
        return out

    def _lineage(self, entry) -> list[dict]:
        """The entry's ancestral chain, root first, the entry itself last.

        Handed to the worker brief. For a `debug` branch this is the prior
        fix attempts -- without it a debug step re-undoes its parent's
        repair (the oscillation AIRA §4.1 scoped memory exists to stop). For
        any branch it is the context the branch's premise rests on. Bounded
        by the tree caps, so it never grows without the record growing."""
        chain = []
        walked, guard = entry, {entry.id}
        while True:
            chain.append({
                "id": walked.id, "kind": walked.kind,
                "status": walked.status,
                "verdict": walked.result.verdict if walked.result else None,
                "closure_kind": walked.result.closure_kind if walked.result else None,
                "summary": walked.result.summary if walked.result else ""})
            if walked.parent == "" or walked.parent in guard:
                break
            guard.add(walked.parent)
            walked = self.store.load(walked.parent)
        chain.reverse()
        return chain

    def _branch_families(self, entries) -> dict:
        """Sibling verdicts for every branchable parent, plus a complexity cue.

        Scoped sibling memory (AIRA §4.1): a generator branching off P should
        see what P's *children* already concluded -- not the whole record --
        because siblings that differ are what pushes diversity, and siblings
        that repeat are mode collapse. The cue is AIRA's prompt-adaptive
        complexity: the more children a parent already has, the more advanced
        a premise the next child should exchange."""
        by_parent: dict[str, list] = {}
        for e in entries:
            if e.parent:
                by_parent.setdefault(e.parent, []).append(e)
        rows = {}
        for e in entries:
            machine = self.config.track_for(e.id).machine
            if machine.status(e.status).terminal or e.claim is not None:
                continue        # branchable: claimable, at any lineage depth
            children = by_parent.get(e.id) or []
            if not children:
                continue
            rows[e.id] = {
                "title": e.title,
                "children_count": len(children),
                "complexity": (
                    "minimal" if len(children) < 2
                    else "moderate" if len(children) < 5 else "advanced"),
                "siblings": [
                    {"id": c.id, "kind": c.kind, "status": c.status,
                     "verdict": c.result.verdict if c.result else None,
                     "summary": c.result.summary if c.result else ""}
                    for c in sorted(children, key=lambda c: c.id)],
            }
        return rows

    @staticmethod
    def _proposal_problem(proposal: dict) -> str | None:
        """The mechanical check on a filed proposal, or None if it passes.

        The scoring inputs are not optional. On the corpus this core is
        derived from, the filing path defaulted `impact` to 0 and `mechanisms`
        to empty, 298 entries shipped with those defaults, and the ranking --
        which is only as good as the numbers under it -- ranked on noise while
        looking like mathematics. A proposal missing any of them is refused at
        the door with the reason named, so the gap is visible in the phase
        record instead of invisible in the prices."""
        title = str(proposal.get("title", ""))[:60]
        mechanisms = proposal.get("mechanisms")
        if not isinstance(mechanisms, list) or not mechanisms \
                or any(not str(m).strip() for m in mechanisms):
            return (f"{title!r}: mechanisms must be a non-empty list of tags; "
                    "without them the board cannot track coverage or exclude "
                    "dead directions")
        for name, what in (("confidence", "a probability in [0, 1]"),
                           ("impact", "a fractional move on the objective"),
                           ("cost", "a cost in run-units")):
            value = proposal.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return f"{title!r}: {name} must be {what}, got {value!r}"
        return None

    def orient(self, it: Iteration) -> Phase:
        phase = it.phase("orient")
        start = time.time()
        entries = self.store.all()
        it.target = self._target(phase.detail)
        best = self._best()
        it.objective = best[0] if best else None
        phase.read = len(entries)
        phase.did = 1
        if self.resumed:
            phase.detail.append(
                f"resumed {self.resumed} prior iteration(s), "
                f"through {self.resumed_through}")
        phase.detail += [f"host={self.host.fingerprint}",
                        f"target={it.target}", f"best={it.objective}",
                        f"runs charged to this campaign="
                        f"{self.domain_budget['runs'].spent:g}"]
        if self.foreign_rows:
            phase.detail.append(
                f"{self.foreign_rows} pre-adoption run row(s) present and not "
                f"charged to this campaign's budget")
        # Free any claim whose holder has gone silent. Targeted, one at a time,
        # never all-or-nothing (H83).
        for entry, age in self.claims.reapable():
            try:
                _, former, _ = self.claims.reap(entry.id)
                phase.detail.append(f"reaped {entry.id} from {former} ({age:.1f}h)")
            except AutoresearchError:
                pass
        phase.seconds = time.time() - start
        return phase

    def generate(self, it: Iteration, budget) -> Phase:
        phase = it.phase("generate")
        start = time.time()
        n = int(self.config.coordinator.get("generators_per_iteration", 1))
        briefs = []
        for i in range(n):
            try:
                budget.spend("spawns", note="generator")
            except BudgetExceeded:
                break
            briefs.append(self._brief(
                Role.GENERATOR, it, generator_index=i, generators=n,
                instruction=("Propose hypotheses this board has not tried. "
                             "Every entry MUST carry: title, hypothesis, "
                             "prediction, bar, confidence (0-1), impact "
                             "(fractional move on the objective), cost (in "
                             "run-units), mechanisms (non-empty list of tags), "
                             "why_filed -- a proposal missing any of these is "
                             "refused mechanically. Check `mechanism_coverage` "
                             "in the brief: at least one entry in your batch "
                             "must probe a mechanism tag with `tested: 0` (or "
                             "absent), naming a NEW tag if no existing one "
                             "fits. A novel mechanism whose confirmation does "
                             "not immediately move the objective is still "
                             "worth filing at its honest numbers -- its "
                             "refutation or confirmation prices the whole "
                             "family, and the risk dial is how it gets "
                             "attempted. Set `parent` to an existing entry id "
                             "when your proposal refines or narrows that "
                             "entry's work -- a branch off the incumbent; "
                             "leave `parent` out for new territory (a novel "
                             "root), which is what the dial's share is spent "
                             "on. On a branch, set `kind`: `improve` refines "
                             "what held, `debug` repairs what failed or was "
                             "inconclusive, `probe` narrows a boundary. Read "
                             "`branch_families` in the brief: it carries only "
                             "what a parent's children already concluded, and "
                             "its `complexity` cue says how advanced the next "
                             "child's premise should be -- a parent with many "
                             "siblings has had the simple exchanges tried. "
                             "Depth and sibling caps are enforced at "
                             "filing; a refused branch is a refused branch, "
                             "not a suggestion.")))
        numbered = list(enumerate(briefs))
        phase.read = len(briefs)

        existing = self._seen_titles()
        for (i, _), ok, reply in self._map(
                lambda nb: self._ask(Role.GENERATOR, nb[1]), numbered):
            if not ok:
                phase.detail.append(f"generator {i} raised {reply!r}")
                continue
            for proposal in (reply.data or []):
                if not isinstance(proposal, dict) or not proposal.get("title"):
                    continue
                problem = self._proposal_problem(proposal)
                if problem is not None:
                    phase.detail.append(f"refused: {problem}")
                    continue
                if self._norm_title(proposal["title"]) in existing:
                    continue          # two generators proposing one idea (H60)
                try:
                    entry = self._file(proposal)
                except AutoresearchError as exc:
                    # A refused branch (unknown parent, depth or sibling cap)
                    # is a distinguishable line, not a crashed phase.
                    phase.detail.append(f"refused: {exc}")
                    continue
                existing.add(self._norm_title(entry.title))
                phase.detail.append(entry.id)
                phase.did += 1
            self._charge(it, reply)
        phase.seconds = time.time() - start
        return phase

    def _file(self, proposal: dict) -> Entry:
        track = self.config.tracks[proposal.get("track", "research")] \
            if proposal.get("track") in self.config.tracks \
            else next(iter(self.config.tracks.values()))
        from ..skills import core_version
        # The tree. A proposal may name the entry it branches from; the
        # coordinator -- not the proposing agent -- owns the tree structure,
        # so the lineage is validated here: an unknown parent, a depth past
        # `tree_max_depth`, or more siblings against one parent than
        # `tree_max_children` is refused at filing, with a reason, rather
        # than discovered at dispatch.
        parent = str(proposal.get("parent", "") or "")
        if parent:
            try:
                parent_entry = self.store.load(parent)
            except Exception as exc:
                raise AutoresearchError(
                    f"cannot file a branch of {parent!r}: no such entry") from exc
            if parent_entry.track != track.id:
                raise AutoresearchError(
                    f"cannot file a branch of {parent!r} on track "
                    f"{track.id!r}: a branch stays on its parent's track")
            depth, walked, lineage = 1, parent, {parent}
            while walked:
                walked = self.store.load(walked).parent
                if walked:
                    if walked in lineage:
                        raise AutoresearchError(
                            f"lineage of {parent!r} is cyclic at {walked!r}")
                    lineage.add(walked)
                    depth += 1
            max_depth = self.config.tree_max_depth
            if max_depth and depth > max_depth:
                raise AutoresearchError(
                    f"branch depth {depth} exceeds tree_max_depth={max_depth}; "
                    "deepen the record by closing work, or raise the cap")
            max_children = self.config.tree_max_children
            if max_children:
                siblings = sum(1 for e in self.store.all()
                               if e.parent == parent)
                if siblings + 1 > max_children:
                    raise AutoresearchError(
                        f"parent {parent!r} already has {siblings} branch(es); "
                        f"tree_max_children={max_children}")
        kind = str(proposal.get("kind", "") or "")
        if kind:
            if kind not in KINDS:
                raise AutoresearchError(
                    f"unknown branch kind {kind!r}; one of "
                    f"{KINDS}, or omit it")
            if not parent:
                raise AutoresearchError(
                    f"kind {kind!r} is branch intent; a proposal with no "
                    "parent is a novel root and carries no kind")
        entry = Entry(
            id=self.store.next_id(track.prefix), track=track.id,
            title=str(proposal["title"])[:200],
            status=track.machine.initial,
            parent=parent,
            kind=kind,
            hypothesis=str(proposal.get("hypothesis", "")),
            prediction=str(proposal.get("prediction", "")),
            bar=str(proposal.get("bar", "")),
            why_filed=str(proposal.get("why_filed", "")),
            confidence=float(proposal.get("confidence", 0.5)),
            impact=float(proposal.get("impact", 0.0)),
            cost=float(proposal.get("cost", 1.0)),
            mechanisms=[str(m) for m in proposal.get("mechanisms", [])],
            sources=[str(s) for s in proposal.get("sources", [])],
            repro=str(proposal.get("repro", "")),
            observed=str(proposal.get("observed", "")),
            expected=str(proposal.get("expected", "")),
            # Core stamps itself on a defect entry: the reporter is the one
            # place the running version is known for certain, and a defect
            # report without it cannot be reproduced upstream. An entry on an
            # ordinary track stays unstamped -- run provenance deliberately
            # omits the core version, and so does the entry unless it IS the
            # defect report.
            core=str(proposal.get("core", ""))
            or (core_version() if track.requires_defect_evidence else ""))
        entry.gates = [dataclasses.replace(gate) for gate in track.gates]
        self.store.save(entry)
        return entry

    def rank(self, it: Iteration, budget) -> tuple[Phase, list]:
        phase = it.phase("rank")
        start = time.time()
        entries = self.store.all()
        # An entry costing more runs than the campaign has left is unaffordable;
        # so is one costing more than a single iteration may spend. Both meters
        # were declared, only the first was ever read.
        remaining = min(self.domain_budget["runs"].remaining(),
                        budget["runs"].remaining())
        ranking = rank_mod.rank(entries, self.config, host=self.host,
                                budget_ok=lambda e: e.cost <= remaining,
                                risk=self.config.risk)
        phase.read = len(entries)
        k = int(budget["fanout"].remaining())

        # The domain's own scorer, if it declared one, replaces the formula's
        # pricing of the claimable candidates. Hard filters have already run,
        # so the seam can price only what survived them; a seam failure is
        # recorded and the iteration dispatches nothing -- there is no
        # fallback to the formula the domain replaced.
        if "score" in self.config.commands:
            try:
                ranking = scoring.seam_ranking(
                    self.config, ranking, entries, self.config.risk)
                phase.detail.append(
                    f"scored by domain seam: {self.config.commands['score']}")
            except AutoresearchError as exc:
                phase.detail.append(f"score seam failed: {exc}")
                phase.detail.append("dispatch skipped: no ranking to spend")
                phase.seconds = time.time() - start
                it.shortlist = []
                return phase, []

        # The judge may reorder within the shortlist; it may not overrule a
        # hard filter, and apply_veto refuses that outright.
        try:
            budget.spend("spawns", note="judge")
            reply = self._ask(Role.JUDGE, self._brief(
                Role.JUDGE, it,
                ranking=[{"id": s.entry_id, "score": s.score, "terms": s.terms,
                          "title": s.title, "novel": s.novel}
                         for s in ranking.scored],
                excluded=[{"id": s.entry_id, "why": s.excluded}
                          for s in ranking.excluded],
                instruction=(
                    f"The risk dial is {self.config.risk:.0%}: that share of "
                    "the shortlist goes to novel branches (`novel: true`, no "
                    "parent -- new territory); the rest refines the incumbent. "
                    "Return a JSON list of vetoes, each {entry_id, "
                    "action: promote|demote|drop, justification}. "
                    "Return [] if the ordering is right. You may not "
                    "veto an excluded entry. A novel pick may sit low on "
                    "score by design, which is not a reason to veto it.")))
            self._charge(it, reply)
            vetoes = [rank_mod.Veto(v["entry_id"], v["action"], v.get("justification", ""))
                      for v in (reply.data or []) if isinstance(v, dict)]
            if vetoes:
                ranking = rank_mod.apply_veto(ranking, vetoes)
                phase.detail.append(f"{len(vetoes)} veto(es) applied")
        except (BudgetExceeded, AutoresearchError, ValueError, KeyError) as exc:
            phase.detail.append(f"judge skipped: {exc}")

        shortlist = ranking.shortlist(k)
        it.shortlist = [s.entry_id for s in shortlist]
        phase.did = len(shortlist)
        phase.detail.append(f"{len(ranking.excluded)} excluded by hard filters")
        taken = [c.entry_id for c in shortlist if c.novel]
        if taken:
            phase.detail.append(f"novel branches: {', '.join(taken)}")
        phase.seconds = time.time() - start
        return phase, shortlist

    def _dispatch_native(self, it: Iteration, shortlist, budget, phase,
                         start: float) -> Phase:
        """Hand each shortlisted card to the surrounding harness's subagents.

        No model runs in this process: the phase reserves a bounded external
        assignment per card (claim, run ceiling, wall clock, workspace,
        output contract) and leaves the work to native agents, which settle
        through `ar external complete`. The run allocation is charged by
        `attempts.reserve` against the claim and campaign ceilings at reserve
        time -- a crash never refunds it -- so the iteration's runs meter is
        deliberately not charged here a second time.

        The iteration ends at dispatch: nothing it can curate or distil has
        come back yet. `run_iteration` stops the loop on the outstanding
        assignments; the next invocation's orient sees the applied verdicts.
        """
        from .. import external as external_mod

        def max_runs_for(card) -> int:
            """The card's declared cost, in run units, at least one."""
            return max(1, int(card.terms.get("cost", 1.0)))

        assigned = []
        for card in shortlist:
            reason = preflight.blocked_reason(
                self.config, self.store.load(card.entry_id))
            if reason is not None:
                phase.detail.append(f"{card.entry_id}: {reason}")
                it.verdicts[card.entry_id] = "blocked"
                continue
            try:
                budget.spend("fanout", note=card.entry_id)
                budget.spend("spawns", note=f"native worker {card.entry_id}")
                # Parity with worker dispatch: the iteration's runs meter is
                # charged for the allocation, while attempts.reserve charges
                # the same units against the claim and campaign ceilings.
                budget.spend("runs", max_runs_for(card), note=card.entry_id)
            except BudgetExceeded as exc:
                phase.detail.append(str(exc))
                break
            try:
                claimed = self.claims.claim(
                    card.entry_id,
                    why=f"ranked #{shortlist.index(card) + 1} in iteration {it.n}")
            except AutoresearchError as exc:
                phase.detail.append(f"{card.entry_id}: {exc}")
                continue
            max_runs = max_runs_for(card)
            try:
                row = external_mod.assign(
                    self.config, card.entry_id, self.session,
                    claimed.claim.max_hours * 3600, max_runs)
            except AutoresearchError as exc:
                self.claims.release(card.entry_id, why=str(exc))
                phase.detail.append(f"{card.entry_id}: {exc}")
                continue
            it.attempt_ids.append(row["id"])
            it.verdicts[card.entry_id] = "assigned"
            phase.did += 1
            assigned.append(row)
            ceiling = row.get("ceiling") or {}
            phase.detail.append(
                f"{card.entry_id}: assignment {row['id']} reserved "
                f"{max_runs} run(s) until {ceiling.get('expires', '?')}; "
                f"brief: ar external show {row['id']}")
        phase.read = len(shortlist)
        phase.seconds = time.time() - start
        return phase

    def dispatch(self, it: Iteration, shortlist, budget, pool) -> Phase:
        phase = it.phase("dispatch")
        start = time.time()
        if self.config.dispatch == "native":
            return self._dispatch_native(it, shortlist, budget, phase, start)
        jobs = []
        dispatched_claims = {}
        dispatch_attempts = {}
        for card in shortlist:
            reason = preflight.blocked_reason(
                self.config, self.store.load(card.entry_id))
            if reason is not None:
                phase.detail.append(f"{card.entry_id}: {reason}")
                it.verdicts[card.entry_id] = "blocked"
                continue
            try:
                budget.spend("fanout", note=card.entry_id)
                budget.spend("spawns", note=f"worker {card.entry_id}")
                # Allocated from the pool before the worker starts, on the
                # entry's declared cost. Charged after the fact it would bound
                # nothing: every card is dispatched before any of them reports.
                budget.spend("runs", card.terms.get("cost", 1.0),
                             note=card.entry_id)
            except BudgetExceeded as exc:
                phase.detail.append(str(exc))
                break
            try:
                claimed = self.claims.claim(
                    card.entry_id,
                    why=f"ranked #{shortlist.index(card) + 1} in iteration {it.n}")
            except AutoresearchError as exc:
                phase.detail.append(f"{card.entry_id}: {exc}")
                continue
            dispatched_claims[card.entry_id] = claimed.claim
            try:
                reservation = attempts.reserve(
                    self.config, card.entry_id, self.session,
                    claimed.claim.max_hours * 3600, kind="dispatch")
            except AutoresearchError as exc:
                self.claims.release(card.entry_id, why=str(exc))
                phase.detail.append(f"{card.entry_id}: {exc}")
                continue
            dispatch_attempts[card.entry_id] = reservation
            it.attempt_ids.append(reservation["id"])
            jobs.append(card.entry_id)
        phase.read = len(jobs)

        paths = self.config.paths
        protected = (paths.entries, paths.claims, paths.runs,
                     paths.iterations, paths.workspaces)

        def work(entry_id):
            slot = pool.acquire(f"it{it.n}-{entry_id}")
            pool.retain(slot.name, "worker evidence has not been harvested")
            before, reply, error = None, None, None
            try:
                relative = self.config.paths.runs.relative_to(self.config.paths.root)
                directory = slot.path / relative
                if not directory.resolve().is_relative_to(slot.path.resolve()):
                    raise AutoresearchError("configured runs directory escapes workspace")
                before = runs_mod.snapshot(directory)
                entry = self.store.load(entry_id)
                command = ["ar", "--domain", str(slot.path), "--session",
                           slot.name, "measure", "--entry", entry_id, "--"]
                brief = self._brief(
                    Role.WORKER, it, entry=dataclasses.asdict(entry),
                    workspace=str(slot.path),
                    # The ancestral chain, root first, this entry last. For a
                    # `debug` branch this IS the assignment's memory -- the
                    # prior fix attempts, so the step does not undo its
                    # parent's repair. For any branch it is the context the
                    # pre-registered bar rests on.
                    lineage=self._lineage(entry),
                    record_command=shlex.join(command),
                    memos_directory=str(self.config.paths.memos.relative_to(
                        self.config.paths.root)),
                    instruction=(
                        "Test this entry against its own pre-registered bar. "
                        "Run measurements through record_command (append domain "
                        "arguments after --), not the raw measure_command; only "
                        "toolkit-recorded rows are measurement evidence. Declare "
                        "all audit outputs as workspace-relative findings paths "
                        "in each record's outputs. Write a memo under the "
                        "configured memos directory; then re-read your own memo "
                        "and verify every claim before returning. Zero-run static "
                        "work must report runs: 0 and make no measured claims. "
                        "Return JSON: {verdict: "
                        "confirmed|refuted|blocked|inconclusive, memo: "
                        "path relative to the domain root, summary, closure_kind: "
                        "mechanism|slope|cell (refutations only), "
                        "reopen_condition (required for slope/cell), runs: int, "
                        "disposition: experiment|superseded|already-shipped (optional, default experiment), "
                        "applicability: {baseline, source_revision, workload, hardware, parameters} (optional), "
                        "gpu_hours: float, verification: {reread: true, "
                        "claims_checked: [what you re-verified, one item each], "
                        "corrections: [what the re-review changed]}}. A reply "
                        "without a verification block is refused. `lineage` "
                        "carries this branch's ancestors, root first, the "
                        "entry itself last -- on a `debug` branch it names "
                        "every prior fix attempt, so never re-undo a repair "
                        "an ancestor already made; on any branch it is the "
                        "context the registered bar rests on."))
                reply = self._ask(Role.WORKER, brief, workspace=slot.path)
            except Exception as exc:
                error = exc
            return slot, before, reply, error

        for entry_id, ok, value in self._map(work, jobs):
            if not ok:
                slot, before, reply, error = None, None, None, value
            else:
                slot, before, reply, error = value
            report = reply.data if reply and isinstance(reply.data, dict) else {}
            if reply is not None:
                self._charge(it, reply)
            evidence = runs_mod.Harvest()
            if slot is not None and before is not None:
                try:
                    evidence = runs_mod.harvest(
                        slot.path / self.config.paths.runs.relative_to(self.config.paths.root),
                        before, self.config.paths.runs, workspace=slot.path,
                        root=self.config.paths.root, goal=self.config.goal,
                        lanes=self.config.lanes, protected=protected)
                    if report.get("memo"):
                        runs_mod.retain_output(slot.path, self.config.paths.root,
                                               report["memo"], self.config.lanes,
                                               protected)
                except Exception as exc:
                    evidence.problems.append(f"evidence retention failed: {exc}")
            amounts = {}
            for key in ("runs", "gpu_hours"):
                try:
                    amount = float(report.get(key, 0) or 0)
                    if not math.isfinite(amount) or amount < 0:
                        raise ValueError("must be finite and nonnegative")
                    amounts[key] = amount
                except (ValueError, TypeError) as exc:
                    evidence.problems.append(f"invalid reported {key}: {exc}")
                    amounts[key] = 0
            verified = sum(r.entry == entry_id for r in evidence.records)
            if amounts["runs"] > verified or (amounts["gpu_hours"] > 0 and not verified):
                evidence.problems.append(
                    f"missing measurement evidence: reported {amounts['runs']:g} runs, "
                    f"retained {verified} new record(s) for {entry_id}")
            gpu_hours = 0.0
            for record in evidence.records:
                try:
                    amount = float(record.cost.get("gpu_hours", 0) or 0)
                    if math.isfinite(amount) and amount > 0:
                        gpu_hours += amount
                except (ValueError, TypeError, AttributeError):
                    pass
            attributed = max(1, amounts["runs"], evidence.consumed)
            self._charge_runs(it, attributed,
                              max(amounts["gpu_hours"], gpu_hours))
            claim = dispatched_claims[entry_id]
            it.runs_by_entry[entry_id] = {
                "session": claim.session, "claim_at": claim.at, "runs": attributed}
            it.run_ids.extend(r.id for r in evidence.records)
            try:
                attempts.settle(self.config, dispatch_attempts[entry_id]["id"],
                                self.session, "failed" if error is not None else "completed",
                                consumed=attributed, run_ids=[r.id for r in evidence.records],
                                evidence_session=f"it{it.n}-{entry_id}")
            except AutoresearchError as exc:
                evidence.problems.append(f"settlement refused: {exc}")
                attempts.settle(self.config, dispatch_attempts[entry_id]["id"],
                                self.session, "failed", consumed=attributed,
                                reason=f"settlement refused: {exc}")
            if error is not None:
                evidence.problems.append(f"worker raised {error!r}")
            if evidence.problems:
                phase.detail.extend(f"{entry_id}: {p}" for p in evidence.problems)
                if slot is not None:
                    pool.retain(slot.name, "; ".join(evidence.problems))
                    phase.detail.append(f"{entry_id}: workspace retained at {slot.path}")
                verdict = "failed" if error is not None else "refused"
                try:
                    self.claims.release(entry_id, why="; ".join(evidence.problems))
                except AutoresearchError as exc:
                    phase.detail.append(f"{entry_id}: {exc}")
            else:
                verdict = self._apply_verdict(entry_id, report, phase,
                                              expected_claim_at=claim.at)
                if slot is not None:
                    pool.retain(slot.name, None)
                    pool.release(slot.name)
            it.verdicts[entry_id] = verdict
            phase.did += 1
        phase.seconds = time.time() - start
        return phase

    def _apply_verdict(self, entry_id: str, report: dict, phase: Phase,
                       expected_claim_at=None) -> str:
        with self.claims._lock():
            entry = self.store.load(entry_id)
            if expected_claim_at is not None and (
                    not entry.claim or entry.claim.session != self.session
                    or entry.claim.at != expected_claim_at):
                raise AutoresearchError("claim identity changed; verdict refused")
            return self._apply_verdict_locked(entry_id, report, phase)

    def _apply_verdict_locked(self, entry_id: str, report: dict, phase: Phase,
                              receipt_id=None) -> str:
        """Record a worker's verdict, enforcing the evidence rules.

        Idempotent on purpose: a worker with tool access may have run `ar close`
        itself, and finding the entry already terminal is a success, not a race.
        """
        entry = self.store.load(entry_id)
        def receipt(verdict):
            return Event(_iso(), "external-complete", self.session,
                         json.dumps({"assignment": receipt_id, "verdict": verdict})) if receipt_id else None
        track = self.config.track_for(entry_id)
        machine = track.machine
        if machine.status(entry.status).terminal:
            phase.detail.append(f"{entry_id}: already closed by the worker")
            return entry.status
        # The re-review is the completion protocol, not advice: a verdict the
        # worker did not re-check against its own deliverable is a claim the
        # record would take on faith. Refused loudly, claim handed back. The
        # flag must be a real True and the checks non-empty (H101: a
        # verification that reported OK having checked strictly less than the
        # gate is the vacuous-pass shape this record does not survive).
        verification = report.get("verification")
        claims = verification.get("claims_checked") if isinstance(
            verification, dict) else None
        if not (isinstance(verification, dict)
                and verification.get("reread") is True
                and isinstance(claims, list) and claims):
            phase.detail.append(
                f"{entry_id}: reply refused — no verification block; the work "
                "was not re-reviewed before it was shared")
            try:
                self.claims.release_locked(entry_id,
                                    why="reply refused: no verification block", event=receipt("refused"))
            except AutoresearchError as exc:
                phase.detail.append(f"{entry_id}: {exc}")
            return "refused"

        verdict = str(report.get("verdict", "inconclusive"))
        if verdict not in machine.terminal_names:
            # Inconclusive is a real outcome and must be sayable. H26: the loop
            # had no way to report "audited, found nothing", so agents reached
            # for a status that was not true.
            try:
                self.claims.release_locked(
                    entry_id,
                    why=str(report.get("summary")
                            or f"{verdict}: no verdict against the registered bar"), event=receipt(verdict))
            except AutoresearchError as exc:
                phase.detail.append(f"{entry_id}: {exc}")
            return verdict

        try:
            applicability = report.get("applicability")
            result = Result(
                verdict=verdict, memo=str(report.get("memo", "")), at=_iso(),
                session=self.session, summary=str(report.get("summary", "")),
                closure_kind=report.get("closure_kind"),
                reopen_condition=str(report.get("reopen_condition", "")),
                verification=verification,
                applicability=(Applicability.from_dict(applicability)
                               if applicability is not None else None),
                disposition=report.get("disposition", "experiment"))
            entry.apply(machine, verdict, who=self.session,
                        why=result.summary, result=result,
                        memo_exists=lambda m: (self.config.paths.root / m).exists())
            entry.claim = None
            if receipt_id:
                entry.history.append(receipt(verdict))
            self.store.save(entry)
            return verdict
        except AutoresearchError as exc:
            # The close was refused -- missing evidence, missing closure kind.
            # The claim goes back rather than the entry being closed anyway.
            phase.detail.append(f"{entry_id}: close refused — {exc}")
            try:
                self.claims.release_locked(entry_id, why=f"close refused: {exc}", event=receipt("refused"))
            except AutoresearchError:
                pass
            return "refused"

    def curate(self, it: Iteration, budget) -> Phase:
        phase = it.phase("curate")
        start = time.time()
        entries = self.store.all()
        phase.read = len(entries)
        render.write_views(self.config, entries)
        phase.did += 1
        try:
            budget.spend("spawns", note="curator")
            reply = self._ask(Role.CURATOR, self._brief(
                Role.CURATOR, it, verdicts=it.verdicts,
                instruction=("Fold this iteration's verdicts into the record. "
                             "Return JSON: {reprice: [{entry_id, confidence, "
                             "impact, cost, why}], notes: [str]}. Reprice only "
                             "entries this iteration's results actually move.")))
            self._charge(it, reply)
            data = reply.data if isinstance(reply.data, dict) else {}
            for change in data.get("reprice", []):
                try:
                    entry = self.store.load(change["entry_id"])
                except AutoresearchError:
                    continue
                if self.config.track_for(entry.id).machine.status(
                        entry.status).terminal:
                    continue          # H140: never reprice a closed entry
                for numeric in ("confidence", "impact", "cost"):
                    if numeric in change:
                        setattr(entry, numeric, float(change[numeric]))
                entry.history.append(Event(_iso(), "repriced", self.session,
                                           str(change.get("why", ""))))
                self.store.save(entry)
                phase.did += 1
            phase.detail += [str(n) for n in data.get("notes", [])]
        except (BudgetExceeded, AutoresearchError, KeyError, TypeError) as exc:
            phase.detail.append(f"curator skipped: {exc}")
        render.write_views(self.config, self.store.all())
        phase.seconds = time.time() - start
        return phase

    def research(self, it: Iteration, budget, question: str,
                 count: int = 1) -> Phase:
        """Spin up targeted research scouts and file what they bring back.

        Scouts answer one question the loop did not ask, looking outside the
        board; their ideas land through the same `_file` path a generator's
        do, so the next `rank` prices them and the next loop can claim them.
        The record keeps which backend each scout ran on, and a scout that
        raised is attributed rather than dropped -- a failure filtered from
        the replies would read as silence, indistinguishable from a question
        that opened nothing.
        """
        phase = it.phase("research")
        start = time.time()
        briefs = []
        for i in range(max(0, count)):
            try:
                budget.spend("spawns", note=f"scout {i}")
            except BudgetExceeded as exc:
                phase.detail.append(str(exc))
                break
            briefs.append(self._brief(
                Role.SCOUT, it, scout_index=i, scouts=count,
                question=str(question),
                instruction=("Research this question against the board and "
                             "return a JSON list of proposals with keys: "
                             "title, hypothesis, prediction, bar, confidence "
                             "(0-1), impact (fractional move on the "
                             "objective), cost (in run-units), mechanisms "
                             "(non-empty list of tags), sources (list of "
                             "citations or URLs), why_filed -- a proposal "
                             "missing any of these is refused mechanically. "
                             "Favour proposals that open a mechanism tag "
                             "`mechanism_coverage` shows untested. File only "
                             "what the question opens; never re-propose an "
                             "open entry or a closed direction. Return [] if "
                             "it opens nothing.")))
        phase.read = len(briefs)
        existing = self._seen_titles()

        def scout(numbered_brief):
            i, brief = numbered_brief
            return i, self._ask(Role.SCOUT, brief)

        for item, ok, value in self._map(scout, list(enumerate(briefs))):
            i = item[0]
            if not ok:
                phase.detail.append(f"scout {i} raised {value!r}")
                continue
            reply = value[1]
            self._charge(it, reply)
            proposals = reply.data if isinstance(reply.data, list) else []
            filed = 0
            for proposal in proposals:
                if not isinstance(proposal, dict) or not proposal.get("title"):
                    continue
                problem = self._proposal_problem(proposal)
                if problem is not None:
                    phase.detail.append(f"refused: {problem}")
                    continue
                if self._norm_title(proposal["title"]) in existing:
                    continue          # two agents proposing one idea (H60)
                try:
                    entry = self._file(proposal)
                except AutoresearchError as exc:
                    phase.detail.append(f"refused: {exc}")
                    continue
                existing.add(self._norm_title(entry.title))
                filed += 1
                phase.did += 1
            phase.detail.append(
                f"scout {i} [{reply.backend or 'unlisted'}]: "
                f"{len(proposals)} idea(s), {filed} filed")
        phase.seconds = time.time() - start
        return phase

    def distil(self, it: Iteration, budget, force: bool = False) -> Phase:
        """Promote closed work into skills the next agent reads.

        Runs on a cadence rather than every iteration: a librarian asked to
        distil after a single verdict writes a skill that says what one entry
        already says, and the record says it better. A cadence-skipped phase
        still records *why*, because a phase that did nothing and a phase that
        did not run are different facts (H98).

        The role returns a body; this writes it. `skills.write` re-runs the full
        check against the store before anything reaches disk, so a skill citing
        an open entry is refused with its reason on the record rather than
        landing and failing validation later.
        """
        phase = it.phase("distil")
        start = time.time()
        every = self.config.distil_every
        existing, read_problems = skills_mod.read_all(self.config)
        phase.read = len(existing)
        phase.detail += read_problems
        if existing:
            # The efficiency claim, recorded rather than asserted: what every
            # brief this iteration actually carried, against what it did not.
            index_chars = sum(len(s.name) + len(s.description)
                              for s in existing)
            phase.detail.append(
                f"index: {len(existing)} description(s), {index_chars} chars in "
                f"every brief; {sum(s.lines for s in existing)} body lines held "
                f"back")
        if not every and not force:
            phase.detail.append(
                "not run: coordinator.distil_every is 0, so this domain has "
                "turned distillation off")
            phase.seconds = time.time() - start
            return phase
        if not force and it.n % every:
            phase.detail.append(
                f"not run: cadence is every {every} iterations; next at "
                f"{(it.n // every + 1) * every}")
            phase.seconds = time.time() - start
            return phase

        entries = self.store.all()
        pending = skills_mod.undistilled(self.config, existing, entries)
        stale = skills_mod.stale_report(self.config, existing, entries)
        if not pending and not stale:
            phase.detail.append(
                "nothing to distil: every terminal entry is cited and no "
                "citation has moved")
            phase.seconds = time.time() - start
            return phase
        try:
            budget.spend("spawns", note="librarian")
            reply = self._ask(Role.LIBRARIAN, self._brief(
                Role.LIBRARIAN, it,
                undistilled=pending,
                stale=stale,
                instruction=("Distil what is worth keeping. Return JSON: "
                             "{write: [{name, description, cites, body}], "
                             "retire: [{name, why}], notes: [str]}. Cite only "
                             "terminal entries, and cite every one you name in "
                             "the body. Return empty lists when nothing this "
                             "iteration closed is worth a skill.")))
            self._charge(it, reply)
            data = reply.data if isinstance(reply.data, dict) else {}
            measurements = skills_mod.best_measurements(self.config)
            for spec in data.get("write", []):
                if not isinstance(spec, dict) or not spec.get("name"):
                    continue
                try:
                    path = skills_mod.write(
                        self.config, spec["name"], spec.get("description", ""),
                        spec.get("cites", []), spec.get("body", ""),
                        iteration=it.n, entries=entries,
                        measurements=measurements)
                except AutoresearchError as exc:
                    phase.detail.append(f"{spec['name']}: refused - {exc}")
                    continue
                phase.did += 1
                phase.detail.append(
                    f"wrote {path.relative_to(self.config.paths.root)}")
            for spec in data.get("retire", []):
                if not isinstance(spec, dict) or not spec.get("name"):
                    continue
                try:
                    skills_mod.retire(self.config, spec["name"])
                except AutoresearchError as exc:
                    phase.detail.append(f"retire refused - {exc}")
                    continue
                phase.did += 1
                phase.detail.append(
                    f"retired {spec['name']}: {spec.get('why', 'no reason given')}")
            phase.detail += [str(n) for n in data.get("notes", [])]
        except (BudgetExceeded, AutoresearchError, KeyError, TypeError) as exc:
            phase.detail.append(f"librarian skipped: {exc}")
        phase.seconds = time.time() - start
        return phase

    def qc(self, it: Iteration, budget) -> Phase:
        """Mechanical checks first. A check that is really a grep costs no token."""
        phase = it.phase("qc")
        start = time.time()
        entries = self.store.all()
        phase.read = len(entries)
        problems = list(self.config.check())
        problems += [f"duplicate id {d}" for d in self.store.duplicates()]
        problems += render.check_views(self.config, entries)
        found, skill_problems = skills_mod.read_all(self.config)
        problems += skill_problems
        problems += skills_mod.check(self.config, found, entries,
                                     skills_mod.best_measurements(self.config))
        for entry in entries:
            machine = self.config.track_for(entry.id).machine
            status = machine.status(entry.status)
            if status.terminal and entry.result is None:
                problems.append(f"{entry.id}: closed with no result")
            elif status.terminal and status.requires_evidence and not (
                    self.config.paths.root / entry.result.memo).exists():
                problems.append(
                    f"{entry.id}: evidence memo {entry.result.memo!r} missing")
            if not status.terminal and entry.result is not None:
                problems.append(f"{entry.id}: open entry carries a stale result")
        # Nothing dispatched this iteration may still be held: a claim that
        # outlives its iteration is the squat H78/H125 describe.
        for entry_id in it.shortlist:
            entry = self.store.load(entry_id)
            if entry.claim and entry.claim.session == self.session:
                problems.append(
                    f"{entry_id}: still claimed by the coordinator after dispatch")
        # Only now is a model asked, and only about what code cannot check --
        # and that ask comes out of the same spawn pool as every other role. It
        # did not, so the meter that makes a runaway iteration structurally
        # impossible undercounted by one every iteration.
        try:
            budget.spend("spawns", note="qc")
            reply = self._ask(Role.QC, self._brief(
                Role.QC, it,
                mechanical_problems=problems,
                phases=[dataclasses.asdict(p) for p in it.phases],
                verdicts=it.verdicts,
                instruction=("Verify the iteration happened. Return JSON: "
                             "{problems: [str], harness_debt: [{title, "
                             "hypothesis, repro, observed}], verdict: "
                             "clean|problems}. Every harness_debt item must "
                             "carry a repro -- a command or path demonstrating "
                             "the defect -- and what you observed; a defect "
                             "report nobody can run is an opinion, and "
                             "validation refuses it until a human completes it.")))
            self._charge(it, reply)
            data = reply.data if isinstance(reply.data, dict) else {}
            problems += [f"qc: {p}" for p in data.get("problems", [])]
            # A defect in the scaffolding is filed against the harness track, not
            # against the research queue -- an agent booting into research work
            # should never read harness debt as a lead on the score.
            harness_track = next(
                (t for t in self.config.tracks.values() if t.id == "harness"), None)
            for debt in data.get("harness_debt", []):
                if harness_track and isinstance(debt, dict) and debt.get("title"):
                    entry = self._file({**debt, "track": harness_track.id})
                    phase.detail.append(f"filed {entry.id} (harness debt)")
        except (BudgetExceeded, AutoresearchError, KeyError, TypeError) as exc:
            problems.append(f"qc role skipped: {exc}")

        phase.did = len(problems)
        phase.detail += problems
        if problems:
            phase.error = f"{len(problems)} problem(s)"
        phase.seconds = time.time() - start
        return phase

    def _map(self, fn, items):
        """Run `fn` over `items` in parallel.

        Returns `(item, ok, value)` per item, where a failure carries the
        exception rather than a stand-in. An earlier version substituted a
        generic "worker failed" reply and dropped which item it belonged to --
        which is precisely the shape this harness keeps filing defects about: a
        failure that reads as a result. The item is part of the answer.
        """
        if not items:
            return []
        workers = max(1, min(len(items),
                             int(self.config.coordinator.get("max_parallel", 5))))
        out = []
        with futures.ThreadPoolExecutor(max_workers=workers) as pool:
            submitted = [(item, pool.submit(fn, item)) for item in items]
            for item, future in submitted:
                try:
                    out.append((item, True, future.result()))
                except Exception as exc:      # noqa: BLE001 -- attributed, not swallowed
                    out.append((item, False, exc))
        return out

    # -- the loop ----------------------------------------------------------

    def run_iteration(self, n: int) -> Iteration:
        # Before any phase runs, not after: a collision discovered at record
        # time has already spent the fanout, and the cheapest moment to refuse
        # is the one where nothing has been claimed yet.
        record = self.config.paths.iterations / f"{n:04d}.json"
        if record.exists():
            raise AutoresearchError(
                f"iteration {n} is already recorded at {record}. Overwriting it "
                f"would destroy the earlier session's audit trail; `run()` "
                f"continues from {self._last_recorded_n()}.")
        it = Iteration(n=n)
        budget = budget_mod.iteration_budget(self.config)
        pool = Pool(self.config, f"{self.session}-it{n}")
        try:
            self.orient(it)
            shortlist = []
            # Wall clock gates the phases that start new work; curate and qc
            # always run, because an iteration that is not recorded did not
            # happen as far as the next agent is concerned.
            if self._time_left(it, budget, "generate"):
                self.generate(it, budget)
            if self._time_left(it, budget, "rank"):
                _, shortlist = self.rank(it, budget)
            if self._time_left(it, budget, "dispatch"):
                self.dispatch(it, shortlist, budget, pool)
            native = self.config.dispatch == "native"
            outstanding = [e for e, v in it.verdicts.items() if v == "assigned"]
            if native and outstanding:
                # Nothing this iteration dispatched has come back: work is
                # happening in native agents outside this process, so there is
                # nothing to close out yet. Skipped, never silent -- the
                # record names why, which is what makes this distinguishable
                # from a phase that did not run.
                for name, why in (
                        ("curate", f"{len(outstanding)} assignment(s) outstanding"),
                        ("distil", f"{len(outstanding)} assignment(s) outstanding"),
                        ("qc", "settle assignments first; qc runs with the verdicts")):
                    skipped = it.phase(name)
                    skipped.detail.append(f"native dispatch: {why}")
            else:
                self.curate(it, budget)
                # Not clock-gated: like curate, it closes out work already paid for,
                # and a verdict that never became knowledge is the run charged twice.
                self.distil(it, budget)
                self.qc(it, budget)
        finally:
            # H91: teardown documented in a runbook and wired into no loop left
            # 64 of 64 merged worktrees on disk.
            pool.release_all()

        self._charge_seconds(budget)
        best = self._best()
        decision = budget_mod.should_stop(
            self.config,
            measurements=best[1].metrics if best else None,
            target=it.target,
            verdicts_per_iteration=[i.confirmed for i in self.history
                                    if i.kind == "iteration"] + [it.confirmed],
            budgets=[self.domain_budget],
            unmet_required_gates=self._unmet_required_gates(best),
            independent_confirmation=budget_mod.independent_confirmation(
                self.config.goal, best,
                runs_mod.read_all(self.config.paths.runs), it.target))
        outstanding = [e for e, v in it.verdicts.items() if v == "assigned"]
        if self.config.dispatch == "native" and outstanding:
            # The work this iteration dispatched is happening in native
            # agents outside this process. Looping now would re-rank a queue
            # whose claims are still held; the honest stop names the handoff
            # and the verb that resumes it.
            decision = budget_mod.StopDecision(
                "native-handoff",
                detail=f"{len(outstanding)} assignment(s) outstanding: "
                       "settle with `ar external complete`, then run `ar loop` again")
        it.stop, it.stop_detail = decision.reason, decision.detail
        it.objective = best[0] if best else None
        it.finished = _iso()
        self.history.append(it)
        self._record(it)
        return it

    def _record(self, it: Iteration) -> None:
        """Write one iteration record, atomically.

        `read_history` refuses an unreadable record rather than skipping it, so
        a half-written file does not degrade the next run -- it stops it, at
        construction, with no way past. Truncate-then-write leaves exactly that
        window open on the crash this directory exists to survive, so the
        record is built beside its name and moved onto it in one step.
        """
        path = self.config.paths.iterations / f"{it.n:04d}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(dataclasses.asdict(it), indent=2, default=str)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(body)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    def _last_recorded_n(self) -> int:
        """The highest iteration number already on disk.

        Numbering from zero in each process overwrote `0001.json` on every new
        `ar loop`, which threw away that record's spend -- so the money ceiling
        it feeds silently reset, which is the whole failure `recorded_usage`
        exists to close.
        """
        highest = 0
        for path in self.config.paths.iterations.glob("*.json"):
            if path.stem.isdigit():
                highest = max(highest, int(path.stem))
        return highest

    def run(self, max_iterations: int = 10, on_iteration=None) -> list[Iteration]:
        """Iterations this call ran. `self.history` holds those plus whatever
        was resumed -- the caller reports a count and sums a cost over what
        comes back, and neither is true of a previous machine's work."""
        already = len(self.history)
        start = max(len(self.history), self._last_recorded_n())
        for i in range(max_iterations):
            it = self.run_iteration(start + i + 1)
            if on_iteration:
                on_iteration(it)
            if it.stop != budget_mod.RUNNING:
                break
        return self.history[already:]
