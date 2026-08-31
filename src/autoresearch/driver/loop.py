"""The coordinator: one iteration, seven phases, every phase metered.

    orient -> generate -> rank -> dispatch -> curate -> qc -> stop?

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
import os
import pathlib
import time
from dataclasses import dataclass, field

from .. import budget as budget_mod
from .. import hardware as hw
from .. import rank as rank_mod
from .. import render, runs as runs_mod
from ..claims import Claims
from ..entries import Entry, Event, Result, Store
from ..errors import AutoresearchError, BudgetExceeded
from ..workspaces import Pool
from .brain import Role


def _iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


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
    cost_usd: float = 0.0
    #: what this iteration consumed, so a later invocation can charge it to the
    #: campaign's meters. An iteration record that does not say what it spent
    #: leaves every ceiling but `runs` starting from zero at the next `ar loop`.
    runs: float = 0.0
    gpu_hours: float = 0.0

    def phase(self, name: str) -> Phase:
        p = Phase(name)
        self.phases.append(p)
        return p

    @classmethod
    def from_dict(cls, data: dict) -> "Iteration":
        data = dict(data)
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
            config, spent_runs=len(rows) + recorded["runs"],
            spent_money=recorded["money"],
            spent_gpu_hours=recorded["gpu_hours"])

    # -- helpers -----------------------------------------------------------

    def _target(self):
        try:
            return self.config.goal.target.resolve(self.probe_target)
        except Exception:
            return None

    def _best(self):
        """`(objective, record)` for the best measurement on disk, or None."""
        return runs_mod.best_run(runs_mod.read_all(self.config.paths.runs),
                                 self.config.goal)

    def _charge(self, it: Iteration, reply) -> None:
        """The one place model spend lands: this iteration's tally *and* the
        campaign's money meter.

        The meter was built from `policy.spend_ceiling` and then never spent, so
        `should_stop` could not reach the budget exit on money however long the
        loop ran. Recorded rather than `spend()`-ed: the money is already gone
        by the time we hear about it, and a ceiling that refuses to record an
        overrun is a ceiling that hides one.
        """
        cost = float(getattr(reply, "cost_usd", 0.0) or 0.0)
        it.cost_usd += cost
        self.domain_budget["money"].spent += cost

    def _charge_runs(self, it: Iteration, runs: float, gpu_hours: float = 0.0) -> None:
        """Charge measurements to the campaign, and record them on the iteration.

        The counts are the worker's own report because the rows it wrote landed
        in its workspace rather than in the coordinator's ledger -- so they are
        the only numbers available here, and they are charged rather than
        dropped. `domain_max_gpu_hours` had no feed at all before this, which
        made it a ceiling that could not be reached by any amount of spending.

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

    def _time_left(self, it: Iteration, budget, phase_name: str) -> bool:
        """Charge elapsed time and say whether this phase may start.

        `iteration_max_seconds` is scaffolded into every new domain, so a phase
        it skips has to be visible: a phase that did nothing and a phase that
        never ran are different facts, which is H98's whole subject.
        """
        if self._charge_seconds(budget) > 0:
            return True
        meter = budget["seconds"]
        it.phase(phase_name).error = (
            f"not run: the iteration's {meter.ceiling:g}s ceiling was spent "
            f"after {meter.spent:.1f}s")
        return False

    def _brief(self, role: str, iteration: Iteration, **extra) -> str:
        """Everything a role needs, assembled once. The knowledge guides are
        included by path rather than inlined: a worker has tools and can read
        them, and a generator that cannot see the closed-directions map will
        propose closed directions."""
        entries = self.store.all()
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
            "budget_remaining": {
                name: meter.remaining()
                for name, meter in self.domain_budget.meters.items()},
        }
        payload.update(extra)
        return json.dumps(payload, indent=2, default=str)

    # -- phases ------------------------------------------------------------

    def orient(self, it: Iteration) -> Phase:
        phase = it.phase("orient")
        start = time.time()
        entries = self.store.all()
        it.target = self._target()
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
                instruction=("Propose hypotheses this board has not tried. Return "
                             "a JSON list of entries with keys: title, hypothesis, "
                             "prediction, bar, confidence (0-1), impact (fractional "
                             "move on the objective), cost (in run-units), "
                             "mechanisms (list of tags), why_filed.")))
        replies = self._fan_out(Role.GENERATOR, briefs)
        phase.read = len(briefs)

        existing = {e.title.strip().lower() for e in self.store.all()}
        for reply in replies:
            for proposal in (reply.data or []):
                if not isinstance(proposal, dict) or not proposal.get("title"):
                    continue
                if proposal["title"].strip().lower() in existing:
                    continue          # two generators proposing one idea (H60)
                entry = self._file(proposal)
                existing.add(entry.title.strip().lower())
                phase.detail.append(entry.id)
                phase.did += 1
            self._charge(it, reply)
        phase.seconds = time.time() - start
        return phase

    def _file(self, proposal: dict) -> Entry:
        track = self.config.tracks[proposal.get("track", "research")] \
            if proposal.get("track") in self.config.tracks \
            else next(iter(self.config.tracks.values()))
        entry = Entry(
            id=self.store.next_id(track.prefix), track=track.id,
            title=str(proposal["title"])[:200],
            status=track.machine.initial,
            hypothesis=str(proposal.get("hypothesis", "")),
            prediction=str(proposal.get("prediction", "")),
            bar=str(proposal.get("bar", "")),
            why_filed=str(proposal.get("why_filed", "")),
            confidence=float(proposal.get("confidence", 0.5)),
            impact=float(proposal.get("impact", 0.0)),
            cost=float(proposal.get("cost", 1.0)),
            mechanisms=[str(m) for m in proposal.get("mechanisms", [])],
            sources=[str(s) for s in proposal.get("sources", [])])
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
                                explore_fraction=self.config.explore_fraction)
        phase.read = len(entries)
        k = int(budget["fanout"].remaining())
        # Computed before the judge so the brief can name the entries the
        # reserve would take. An explore pick sits low on score by
        # construction, and a judge shown it unlabelled reads the ranking as
        # broken and vetoes the one slot aimed at a big swing.
        reserve = [c.entry_id for c in ranking.shortlist(k) if c.explore]

        # The judge may reorder within the shortlist; it may not overrule a
        # hard filter, and apply_veto refuses that outright.
        try:
            budget.spend("spawns", note="judge")
            reply = self.brain.ask(Role.JUDGE, self._brief(
                Role.JUDGE, it,
                ranking=[{"id": s.entry_id, "score": s.score, "terms": s.terms,
                          "title": s.title} for s in ranking.scored],
                excluded=[{"id": s.entry_id, "why": s.excluded}
                          for s in ranking.excluded],
                explore_reserve=reserve,
                instruction=("Return a JSON list of vetoes, each {entry_id, "
                             "action: promote|demote|drop, justification}. "
                             "Return [] if the ordering is right. You may not "
                             "veto an excluded entry. `explore_reserve` names "
                             "the entries taking the reserved slots, ranked on "
                             "impact alone -- they sit low on score by design, "
                             "which is not a reason to veto them.")))
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
        taken = [c.entry_id for c in shortlist if c.explore]
        if taken:
            phase.detail.append(f"explore reserve: {', '.join(taken)}")
        phase.seconds = time.time() - start
        return phase, shortlist

    def dispatch(self, it: Iteration, shortlist, budget, pool) -> Phase:
        phase = it.phase("dispatch")
        start = time.time()
        jobs = []
        for card in shortlist:
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
                self.claims.claim(card.entry_id,
                                  why=f"ranked #{shortlist.index(card) + 1} "
                                      f"in iteration {it.n}")
            except AutoresearchError as exc:
                phase.detail.append(f"{card.entry_id}: {exc}")
                continue
            jobs.append(card.entry_id)
        phase.read = len(jobs)

        def work(entry_id):
            slot = pool.acquire(f"it{it.n}-{entry_id}")
            try:
                entry = self.store.load(entry_id)
                brief = self._brief(
                    Role.WORKER, it, entry=dataclasses.asdict(entry),
                    workspace=str(slot.path),
                    instruction=(
                        "Test this entry against its own pre-registered bar. "
                        "Measure with the domain's measure command; write a memo "
                        "under inbox/ containing the numbers; then return JSON: "
                        "{verdict: confirmed|refuted|blocked|inconclusive, memo: "
                        "path relative to the domain root, summary, closure_kind: "
                        "mechanism|slope|cell (refutations only), "
                        "reopen_condition (required for slope/cell), runs: int, "
                        "gpu_hours: float}."))
                return self.brain.ask(Role.WORKER, brief, workspace=slot.path)
            finally:
                pool.release(slot.name)

        for entry_id, ok, value in self._map(work, jobs):
            if not ok:
                phase.detail.append(f"{entry_id}: worker raised {value!r}")
                it.verdicts[entry_id] = "failed"
                try:
                    self.claims.release(entry_id, why=f"worker raised {value}")
                except AutoresearchError:
                    pass
                phase.did += 1
                continue
            self._charge(it, value)
            report = value.data if isinstance(value.data, dict) else {}
            verdict = self._apply_verdict(entry_id, report, phase)
            it.verdicts[entry_id] = verdict
            self._charge_runs(it, float(report.get("runs", 0) or 0),
                               float(report.get("gpu_hours", 0) or 0))
            phase.did += 1
        phase.seconds = time.time() - start
        return phase

    def _apply_verdict(self, entry_id: str, report: dict, phase: Phase) -> str:
        """Record a worker's verdict, enforcing the evidence rules.

        Idempotent on purpose: a worker with tool access may have run `ar close`
        itself, and finding the entry already terminal is a success, not a race.
        """
        entry = self.store.load(entry_id)
        track = self.config.track_for(entry_id)
        machine = track.machine
        if machine.status(entry.status).terminal:
            phase.detail.append(f"{entry_id}: already closed by the worker")
            return entry.status

        verdict = str(report.get("verdict", "inconclusive"))
        if verdict not in machine.terminal_names:
            # Inconclusive is a real outcome and must be sayable. H26: the loop
            # had no way to report "audited, found nothing", so agents reached
            # for a status that was not true.
            try:
                self.claims.release(
                    entry_id,
                    why=str(report.get("summary")
                            or f"{verdict}: no verdict against the registered bar"))
            except AutoresearchError as exc:
                phase.detail.append(f"{entry_id}: {exc}")
            return verdict

        result = Result(
            verdict=verdict, memo=str(report.get("memo", "")), at=_iso(),
            session=self.session, summary=str(report.get("summary", "")),
            closure_kind=report.get("closure_kind"),
            reopen_condition=str(report.get("reopen_condition", "")))
        try:
            entry.apply(machine, verdict, who=self.session,
                        why=result.summary, result=result,
                        memo_exists=lambda m: (self.config.paths.root / m).exists())
            entry.claim = None
            self.store.save(entry)
            return verdict
        except AutoresearchError as exc:
            # The close was refused -- missing evidence, missing closure kind.
            # The claim goes back rather than the entry being closed anyway.
            phase.detail.append(f"{entry_id}: close refused — {exc}")
            try:
                self.claims.release(entry_id, why=f"close refused: {exc}")
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
            reply = self.brain.ask(Role.CURATOR, self._brief(
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

    def qc(self, it: Iteration, budget) -> Phase:
        """Mechanical checks first. A check that is really a grep costs no token."""
        phase = it.phase("qc")
        start = time.time()
        entries = self.store.all()
        phase.read = len(entries)
        problems = list(self.config.check())
        problems += [f"duplicate id {d}" for d in self.store.duplicates()]
        problems += render.check_views(self.config, entries)
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
            reply = self.brain.ask(Role.QC, self._brief(
                Role.QC, it,
                mechanical_problems=problems,
                phases=[dataclasses.asdict(p) for p in it.phases],
                verdicts=it.verdicts,
                instruction=("Verify the iteration happened. Return JSON: "
                             "{problems: [str], harness_debt: [{title, "
                             "hypothesis}], verdict: clean|problems}.")))
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

    # -- fan-out -----------------------------------------------------------

    def _fan_out(self, role, briefs):
        return [value for _, ok, value in self._map(
            lambda b: self.brain.ask(role, b), briefs) if ok]

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
            self.curate(it, budget)
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
            verdicts_per_iteration=[i.confirmed for i in self.history] + [it.confirmed],
            budgets=[self.domain_budget])
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
