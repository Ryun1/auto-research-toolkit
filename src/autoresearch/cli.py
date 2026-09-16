"""`ar` -- the command surface.

One entry point per verb, every verb reading the same records. There is no
command here that edits a queue document, because there is no queue document to
edit: `ar render` writes the views and `ar validate` refuses a view that was
edited by hand.

Unknown flags are refused by argparse, deliberately. In the source harness
**eight tools silently ignored an unrecognised flag and ran their default mode
instead, so a mistyped flag read as a successful run of what you asked for**
(H135). That is the worst available failure mode and it is free to avoid.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import textwrap

from . import budget as budget_mod
from . import defects as defects_mod
from . import escalate as escalate_mod
from . import hardware as hw
from . import rank as rank_mod
from . import render
from . import runs as runs_mod
from . import skills as skills_mod
from . import upstream as upstream_mod
from .claims import Claims
from .config import DomainConfig, discover
from .entries import Entry, Event, Result, Store
from .errors import AutoresearchError
from .states import CLOSURE_KINDS


def _load(args) -> DomainConfig:
    return DomainConfig.load(args.domain or discover())


def _store(config) -> Store:
    return Store(config.paths.entries)


def _make_probe(config):
    """The target probe as a callable: the domain's command, run, and refused
    by name when it fails. One derivation -- `ar loop`, `ar budget` and the
    board must not disagree about what a failed probe looks like."""
    def probe(command):
        result = subprocess.run(command.split(), cwd=config.paths.root,
                                capture_output=True, text=True)
        if result.returncode != 0:
            raise AutoresearchError(
                f"target probe {command!r} failed ({result.returncode}): "
                f"{result.stderr.strip()[:300]}")
        return float(result.stdout.strip().splitlines()[-1])
    return probe


def _probe_target(config):
    """Resolve the goal's target, running the domain's probe if it needs one."""
    return config.goal.target.resolve(_make_probe(config))


# -- verbs ----------------------------------------------------------------

def cmd_init(args):
    """Scaffold a new domain that is valid and runnable before you edit it."""
    from .scaffold import init

    root = pathlib.Path(args.path).resolve()
    written = init(root, name=args.name or root.name,
                   objective=args.objective, metrics=args.metric or ["cost"],
                   target=args.target, force=args.force)
    print(f"scaffolded domain {args.name or root.name!r} in {root}")
    for path in written:
        print(f"  {path.relative_to(root)}")

    config = DomainConfig.load(root)
    problems = config.check()
    print(f"\nvalidate: {'clean' if not problems else problems}")
    print(textwrap.dedent(f"""
        Next:
          1. edit  bin/measure   -- replace evaluate() with your real experiment
          2. edit  goal.yaml     -- the metrics it returns, and what winning means
          3. edit  guides/landscape.md -- what an agent needs to know to guess well
          4. run   ar --domain {root} board
          5. run   ar --domain {root} hardware   -- what this machine can do
          6. later ar --domain {root} skill list -- what the loop has distilled
        """).rstrip())
    return 0


def cmd_hardware(args):
    """What this machine is, what it can run, and what it cannot."""
    config = _load(args)
    host = hw.detect()
    print(host.describe())

    print(f"\nrequirements declared: {len(config.hardware)}")
    blocked = 0
    for requirement in config.hardware.values():
        capability = hw.check(host, requirement)
        blocked += 0 if capability.ok else 1
        print("  " + capability.report().replace("\n", "\n  "))
    if not config.hardware:
        print("  (none — declare [hardware.<class>] so a capacity limit "
              "surfaces as a refusal rather than as a truncated measurement)")

    print(f"\nrentable classes declared: {len(config.remote)}")
    for cls in config.remote:
        if cls.measured_ratio:
            print(f"  {cls.name:22} ${cls.usd_per_hour:.2f}/h   "
                  f"{cls.measured_ratio:.2f}x"
                  + (f" @ {cls.measured_at_concurrency}-way"
                     if cls.measured_at_concurrency else "")
                  + (f"   {cls.measured_on}" if cls.measured_on else ""))
        else:
            print(f"  {cls.name:22} ${cls.usd_per_hour:.2f}/h   "
                  f"NO MEASURED RATIO — cannot be costed, and a spec sheet is "
                  f"not a measurement")
    if not config.remote:
        print("  (none declared)")
    return 1 if blocked else 0


def cmd_escalate(args):
    """Should this stop running locally, and what should be rented instead.

    Every input core can know, core supplies: the host, the declared hardware
    classes, the rentable classes and their measured ratios, the spend ceiling.
    The four it cannot are flags, because none of them has an honest source
    here: how fast this workload runs locally, how many units it needs, how long
    the answer stays worth having, and whether the work is already correct. In
    particular Gate 0 is a flag and defaults to false -- core cannot know whether
    your port is right, and guessing in the permissive direction is how an hour
    gets spent debugging on a rented box.
    """
    config = _load(args)
    host = hw.detect()

    capability = None
    if args.hardware:
        if args.hardware not in config.hardware:
            raise AutoresearchError(
                f"hardware class {args.hardware!r} is not declared by this "
                f"domain; declared: {sorted(config.hardware) or '(none)'}")
        capability = hw.check(host, config.hardware[args.hardware])

    if args.rate <= 0:
        # `recommend` refuses this too, but only once something has triggered --
        # and a zero rate reported as "local hardware is meeting the need" is
        # the failure-that-reads-as-a-result shape this harness keeps filing.
        raise AutoresearchError(
            f"--rate is {args.rate:g}; a local throughput must be positive to "
            "extrapolate from. A run that made no progress is not a rate.")
    local = hw.Throughput(
        value=args.rate, unit=args.unit,
        # A rate that does not name its machine is not a measurement. The
        # default is the machine we are standing on, never a blank.
        machine=args.machine or host.fingerprint,
        concurrency=args.concurrency, workload=args.workload or "",
        lower_bound=args.lower_bound)
    hours_needed = (args.need / local.value / 3600.0) if local.value > 0 else None

    trigger = escalate_mod.triggered(
        capability=capability,
        hours_needed=hours_needed, hours_available=args.hours_available,
        stalled_iterations=args.stalled_iterations,
        stall_threshold=args.stall_threshold)

    escalation = escalate_mod.recommend(
        trigger=trigger, local=local, units_needed=args.need,
        classes=config.remote, hours_available=args.hours_available,
        spend_ceiling=config.policy.spend_ceiling,
        gate0_correct_locally=args.correct_locally,
        gate0_note=args.gate0_note or "")

    print(f"host        {host.fingerprint}")
    if capability is not None:
        print("  " + capability.report().replace("\n", "\n  "))
    if not config.remote:
        print("  (no [[remote]] classes declared; there is nothing to cost)")
    print(escalation.report())
    # A verdict is not an error, but REFUSE and NEEDS_MEASUREMENT each name
    # something that has to happen before money is worth spending, so they are
    # distinguishable from "go" without reading the prose.
    return 0 if escalation.verdict in (escalate_mod.GO,
                                       escalate_mod.NOT_TRIGGERED) else 1


def cmd_board(args):
    config = _load(args)
    store = _store(config)
    entries = store.all()
    all_runs, skipped = runs_mod.read_with_skipped(config.paths.runs)
    try:
        target = _probe_target(config)
    except AutoresearchError as exc:
        print(f"(target unavailable: {exc})", file=sys.stderr)
        target = None
    best = runs_mod.best_run(all_runs, config.goal)
    print(render.board(config, entries, all_runs, target,
                       (best[0], best[1].entry or best[1].id) if best else None,
                       skipped))
    return 0


def cmd_render(args):
    config = _load(args)
    for path in render.write_views(config, _store(config).all()):
        print(f"wrote {path.relative_to(config.paths.root)}")
    return 0


def cmd_entry_new(args):
    config = _load(args)
    store = _store(config)
    track = config.tracks[args.track]
    entry = Entry(
        id=args.id or store.next_id(track.prefix),
        track=track.id, title=args.title,
        status=track.machine.initial,
        hypothesis=args.hypothesis or "", prediction=args.prediction or "",
        bar=args.bar or "", why_filed=args.why or "",
        confidence=args.confidence, impact=args.impact, cost=args.cost,
        mechanisms=args.mechanism or [], sources=args.source or [],
        gate=args.gate or "",
        core=args.core or "", repro=args.repro or "",
        observed=args.observed or "", expected=args.expected or "")
    if store.exists(entry.id):
        raise AutoresearchError(
            f"{entry.id} already exists at {store.path(entry.id)}. Ids are "
            "filenames, so a collision is loud here rather than a silent "
            "renumber after every citation was written (H79).")
    print(store.save(entry))
    return 0


def cmd_entry_amend(args):
    """Complete or correct evidence fields on an existing entry.

    The honest completion path for a defect the QC role filed without a repro:
    validation and `ar harness export` both refuse the entry until this has
    been run. Every amendment appends a history event, like every other
    transition -- history is append-only.
    """
    config = _load(args)
    store = _store(config)
    entry = store.load(args.id)
    changes = {name: value for name, value in (
        ("title", args.title), ("hypothesis", args.hypothesis),
        ("core", args.core), ("repro", args.repro),
        ("observed", args.observed), ("expected", args.expected))
        if value is not None}
    if not changes:
        raise AutoresearchError(
            "amend needs at least one field to change; nothing was written")
    for name, value in changes.items():
        setattr(entry, name, value)
    entry.history.append(Event(
        _iso(), "amend", args.session,
        "set " + ", ".join(f"{name}={changes[name]!r}" for name in sorted(changes))))
    entry.updated = _iso()
    store.save(entry)
    print(f"{entry.id} amended: {', '.join(sorted(changes))}")
    return 0


def cmd_harness_export(args):
    """Export this project's defects against the core as one upstream bundle.

    Defects live here, as entries, for as long as they live anywhere. This
    command writes the bundle a person publishes; it never publishes itself,
    and the scaffold's policy makes the publishing commands human-only.
    """
    config = _load(args)
    entries = _store(config).all()
    bundle, refused, stats = defects_mod.export_bundle(
        config, entries, all_status=args.all)
    print(f"read {stats['read']} entr(ies) on defect track {stats['track']!r}: "
          f"exported {len(bundle['defects'])}, refused {len(refused)}, "
          f"skipped {stats['closed_skipped']} closed"
          + ("" if args.all else " (--all includes them)"))
    for entry_id, reason in refused:
        print(f"  refused {entry_id}: {reason}")
        print(f"          complete it with: ar entry amend {entry_id} --repro ... --observed ...")
    if not bundle["defects"]:
        print("nothing to export")
        return 1 if refused else 0
    text = json.dumps(bundle, indent=2, sort_keys=True)
    if args.out:
        path = pathlib.Path(args.out)
        path.write_text(text + "\n")
        print(f"wrote {path} ({len(bundle['defects'])} defect(s), "
              f"{bundle['schema']})")
    else:
        print(text)
    # Refused entries are named above; a bundle that left them behind must not
    # read as a clean pass, or the incomplete defect never gets completed.
    return 1 if refused else 0


def cmd_harness_check(args):
    """What core is installed, what upstream has, and what changed between.

    Read-only, and safe to run on a schedule: exit 0 means up to date, 1 means
    an update is available, and anything else failed loudly rather than
    reporting a clean comparison it did not make.
    """
    config = _load(args)
    plan = upstream_mod.plan_update(config, ref=args.ref)
    print(plan.summary())
    if not plan.behind:
        print("up to date")
        return 0
    print("update available")
    if plan.changelog:
        print("\nwhat changed:")
        print(plan.changelog)
    else:
        print("\n(what changed could not be fetched; the update is still there)")
    print("\ntake it with: ar harness update")
    return 1


def cmd_harness_update(args):
    """Pull the latest core, re-render, validate -- and undo itself if the
    upgrade made validation worse.

    Lands on the resolved head SHA, never a moving ref name. A memo recording
    the move lands in inbox/, so the record knows its own core moved. Existing
    validation problems do not block an upgrade and are not caused by it; only
    new ones roll it back.
    """
    config = _load(args)
    plan = upstream_mod.plan_update(config, ref=args.ref)
    print(plan.summary())
    if not plan.behind:
        print("up to date; nothing to do")
        return 0
    if plan.changelog:
        print("\nwhat changed:")
        print(plan.changelog)
    if args.dry_run:
        print(f"\nwould run: {' '.join(upstream_mod.pip_command(plan.url, plan.head))}")
        print("nothing was written (--dry-run)")
        return 0
    result = upstream_mod.apply_update(config, plan)
    for line in result.detail:
        print(line)
    if result.ok:
        print("\nnext: commit the memo, and keep going:")
        print("  git add inbox/ && git commit -m 'Core: pulled upstream update'")
        print("  ar loop")
    return 0 if result.ok else 1


def cmd_entry_show(args):
    config = _load(args)
    entry = _store(config).load(args.id)
    print(json.dumps(entry.to_dict(), indent=2, default=str))
    return 0


def cmd_entry_list(args):
    config = _load(args)
    entries = _store(config).all()
    if args.status:
        entries = [e for e in entries if e.status in args.status]
    if args.track:
        entries = [e for e in entries if e.track == args.track]
    print(f"{len(entries)} entr{'y' if len(entries) == 1 else 'ies'} read")
    for e in entries:
        held = f" [{e.claim.session}]" if e.claim else ""
        print(f"  {e.id:6} {e.status:12}{held:24} {e.title[:70]}")
    return 0


def cmd_claim(args):
    config = _load(args)
    claims = Claims(_store(config), config, args.session)
    entry = claims.claim(args.id, why=args.why or "", budget=args.budget or "",
                         max_runs=args.max_runs, max_hours=args.max_hours)
    print(f"{entry.id} claimed by {args.session} "
          f"(ceiling: {entry.claim.max_runs} runs / {entry.claim.max_hours} h)")
    return 0


def cmd_release(args):
    config = _load(args)
    entry = Claims(_store(config), config, args.session).release(args.id, args.why)
    print(f"{entry.id} released back to {entry.status}: {args.why}")
    return 0


def cmd_reap(args):
    config = _load(args)
    claims = Claims(_store(config), config, args.session)
    if args.id:
        entry, former, age = claims.reap(args.id, ttl_hours=args.ttl_hours)
        print(f"{entry.id} reaped from {former} (silent {age:.1f} h)")
        return 0
    candidates = claims.reapable(ttl_hours=args.ttl_hours)
    print(f"{len(candidates)} reapable claim(s)")
    for entry, age in candidates:
        print(f"  {entry.id:6} {entry.claim.session:24} silent {age:.1f} h")
    return 0


def cmd_close(args):
    config = _load(args)
    store = _store(config)
    entry = store.load(args.id)
    track = config.track_for(args.id)

    if args.relabel:
        entry.relabel(args.relabel, who=args.session, why=args.why or "")
        store.save(entry)
        print(f"{entry.id} closure relabelled to {args.relabel}")
        return 0

    if args.reopen:
        entry.apply(track.machine, track.machine.initial, who=args.session,
                    why=args.why or "")
        entry.claim = None
        store.save(entry)
        print(f"{entry.id} reopened to {entry.status}")
        return 0

    memo_root = config.paths.root
    result = Result(
        verdict=args.status, memo=args.memo or "", at=_iso(), session=args.session,
        summary=args.summary or "", closure_kind=args.closure,
        reopen_condition=args.reopen_condition or "")
    entry.apply(track.machine, args.status, who=args.session, why=args.why or "",
                result=result,
                memo_exists=lambda m: (memo_root / m).exists())
    entry.claim = None
    store.save(entry)
    print(f"{entry.id} closed {args.status}"
          + (f" ({args.closure})" if args.closure else "")
          + f" — evidence: {result.memo}")
    return 0


def cmd_measure(args):
    """Run the domain's measurement command and record the result.

    The domain decides how to measure; core decides where the row lands and
    what makes it valid. That split is the answer to a whole class of results
    escaping the ledger (H142, H64, H39).
    """
    config = _load(args)
    command = config.command("measure").split() + list(args.rest)
    env_note = f"entry={args.entry} " if args.entry else ""
    proc = subprocess.run(command, cwd=config.paths.root, capture_output=True,
                          text=True,
                          env={**_env(), "AR_SESSION": args.session,
                               **({"AR_ENTRY": args.entry} if args.entry else {})})
    if proc.returncode != 0:
        raise AutoresearchError(
            f"measure failed ({proc.returncode}) {env_note}: {proc.stderr.strip()[:500]}")
    try:
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise AutoresearchError(
            f"measure did not emit a JSON run record on stdout: {exc}\n"
            f"got: {proc.stdout[:300]!r}") from exc
    record = runs_mod.RunRecord.from_dict(payload)
    record.session = args.session
    # A throughput figure that does not name its machine is not a measurement,
    # and two rows from different machines are not comparable without saying so.
    # The domain does not have to remember to record this; core does it.
    record.provenance.setdefault("host", hw.detect().to_dict())
    if args.entry:
        record.entry = args.entry
    record.validate(config.goal)
    path = runs_mod.append(config.paths.runs / f"{args.session}.jsonl", record)
    value = (config.goal.objective_value(record.metrics)
             if record.status == "ok" else None)
    print(f"{record.id} {record.status}"
          + (f"  objective={value:,.0f}" if value is not None else "")
          + f"  -> {path.relative_to(config.paths.root)}")
    return 0


def cmd_rank(args):
    config = _load(args)
    entries = _store(config).all()
    all_runs = runs_mod.read_all(config.paths.runs)
    recorded = budget_mod.recorded_usage(config)
    domain = budget_mod.domain_budget(
        config, spent_runs=len(all_runs) + recorded["runs"]
        - budget_mod.recorded_run_overlap(config, all_runs))
    remaining = domain["runs"].remaining()
    explore = config.explore_fraction if args.explore is None else args.explore
    ranking = rank_mod.rank(entries, config, explore_fraction=explore,
                            budget_ok=lambda e: e.cost <= remaining)
    # Shortlisted first: `shortlist` is what marks the reserve, and the table
    # is the ranking a human corrects. An explore pick sits low in it by design,
    # and one shown unmarked reads as the formula having gone wrong.
    shortlist = ranking.shortlist(args.top) if args.top else []
    print(ranking.explain())
    if args.top:
        print(f"\nshortlist (top {args.top}):")
        for s in shortlist:
            print(f"  {s.entry_id}  {s.title}"
                  + ("  [explore]" if s.explore else ""))
    return 0


def cmd_budget(args):
    config = _load(args)
    store = _store(config)
    entries = store.all()
    all_runs = runs_mod.read_all(config.paths.runs)
    # Consumption is read back from the iteration records, the same source the
    # coordinator charges against, so `ar budget` and the loop cannot disagree
    # about how much of a ceiling is left.
    recorded = budget_mod.recorded_usage(config)
    domain = budget_mod.domain_budget(
        config, spent_runs=len(all_runs) + recorded["runs"]
        - budget_mod.recorded_run_overlap(config, all_runs),
        spent_money=recorded["money"],
        spent_gpu_hours=recorded["gpu_hours"])
    iteration = budget_mod.iteration_budget(config)
    print(domain.report())
    print()
    print(iteration.report())
    held = [e for e in entries if e.claim
            and not config.track_for(e.id).machine.status(e.status).terminal]
    print(f"\n{len(held)} live claim(s)")
    claims = Claims(store, config, session=args.session)
    for entry in held:
        # Runs spent under THIS claim: ledger rows under the claim's own
        # session, plus unrecorded consumption the coordinator attributed
        # per entry (harvested rows carry the worker-slot session, so the
        # campaign ceiling and this line read the same attribution).
        spent = sum(1 for r in all_runs
                    if r.entry == entry.id and r.session == entry.claim.session)
        attribution = budget_mod.recorded_runs_by_entry(config).get(entry.id) or {}
        if attribution.get("session") == entry.claim.session:
            spent += attribution.get("runs", 0.0)
        print(f"  {entry.id}  {entry.claim.session}")
        print("    " + budget_mod.claim_budget(entry, config).report()
              .replace("\n", "\n    "))
        over = claims.overrun(entry.id, runs_spent=spent)
        if over:
            print(f"    OVERRUN: {over[0]} {over[1]:g} > {over[2]:g}")

    try:
        target = _probe_target(config)
    except AutoresearchError:
        target = None
    best = runs_mod.best_run(all_runs, config.goal)
    decision = budget_mod.should_stop(
        config,
        measurements=best[1].metrics if best else None,
        target=target,
        budgets=[domain])
    print(f"\nstop decision: {decision}")
    # `target` is tested for truth, not for None: a zero target cannot be
    # normalised against, and `is_met` refuses it rather than dividing by it.
    if best is not None and target:
        # The goal decides what "met" means -- its direction, and its own
        # `stop_when` if it declares one. This line used to test `<` against the
        # target itself, which said "not met" for every maximise domain and
        # disagreed with the stop decision printed directly above it.
        met = config.goal.is_met(best[1].metrics, target)
        print(f"best objective {best[0]:,.0f} vs target {target:,.0f}"
              + ("  ** GOAL MET **" if met else ""))
    return 0


def cmd_loop(args):
    """Run the coordinator. The unattended entry point."""
    from .driver.brain import build_brain
    from .driver.loop import Coordinator

    config = _load(args)
    brain = build_brain(config, model=args.model, max_budget_usd=args.max_usd)

    coordinator = Coordinator(config, brain, session=args.session,
                              probe_target=_make_probe(config))
    print(f"{config.name}: goal {config.goal.id} — {config.goal.objective} "
          f"({config.goal.direction})")
    history = coordinator.run(args.iterations, on_iteration=lambda it: print(it.report()))
    last = history[-1] if history else None
    print(f"\n{len(history)} iteration(s); "
          f"stopped: {last.stop if last else 'no iterations run'}"
          f"{(' — ' + last.stop_detail) if last and last.stop_detail else ''}")
    print(f"cost: ${sum(i.cost_usd for i in history):.2f}")
    return 0


def cmd_migrate(args):
    """Convert an existing prose corpus into entry records. One way, once."""
    from .migrate import migrate as migrate_prose

    config = _load(args)
    store = _store(config)
    total, problems = 0, []
    for track in config.tracks.values():
        if args.track and track.id != args.track:
            continue
        source = pathlib.Path(args.source or config.paths.root) / (
            args.view or track.migrate_from or track.view)
        if not source.exists():
            print(f"{track.id}: no source document at {source}, skipping "
                  "(set `migrate_from` on the track to name it)")
            continue
        result = migrate_prose(
            source, track=track.id,
            claims_root=config.paths.claims if config.paths.claims.exists() else None,
            root=config.paths.root,
            terminal=tuple(track.machine.terminal_names))
        print(f"\n=== {track.id} ({source.name}) ===")
        print(result.report())
        problems += [d.line() for d in result.disagreements]
        problems += result.missing_evidence
        if not args.dry_run:
            for entry in result.entries:
                store.save(entry)
        total += len(result.entries)
    print(f"\n{total} entries "
          + ("would be written (dry run)" if args.dry_run
             else f"written to {config.paths.entries}"))
    if problems:
        print(f"{len(problems)} item(s) need a human decision; nothing was "
              "reconciled silently.")
    return 0


def cmd_validate(args):
    config = _load(args)
    store = _store(config)
    problems = list(config.check())
    entries = store.all()
    problems += [f"duplicate entry id: {d}" for d in store.duplicates()]
    problems += render.check_views(config, entries)
    found, skill_problems = skills_mod.read_all(config)
    problems += skill_problems
    problems += skills_mod.check(config, found, entries, skills_mod.best_measurements(config))
    for entry in entries:
        track = config.track_for(entry.id)
        machine = track.machine
        if entry.status not in machine.statuses:
            problems.append(
                f"{entry.id}: status {entry.status!r} is not declared by track "
                f"{track.id!r}")
            continue
        status = machine.status(entry.status)
        if status.terminal and entry.result is None:
            problems.append(f"{entry.id}: {entry.status} with no result recorded")
        elif status.terminal and status.requires_evidence:
            if not (config.paths.root / entry.result.memo).exists():
                problems.append(
                    f"{entry.id}: evidence memo {entry.result.memo!r} does not "
                    "exist -- a Closed row pointing at nothing (H74/H117)")
        if not status.terminal and entry.result is not None:
            problems.append(
                f"{entry.id}: open status {entry.status!r} carries a stale "
                f"result ({entry.result.verdict}) -- H137")
        if entry.hardware and entry.hardware not in config.hardware:
            # Ranking treats an unknown class as "no requirement", which is the
            # permissive direction -- so a typo must be caught here or it means
            # the gate silently does not exist.
            problems.append(
                f"{entry.id}: hardware class {entry.hardware!r} is not declared; "
                f"declared: {sorted(config.hardware) or '(none)'}")
        if track.requires_defect_evidence:
            problems += defects_mod.evidence_problems(entry, track)
    skipped_runs = 0
    try:
        rows, skipped_runs = runs_mod.read_with_skipped(config.paths.runs)
        for record in rows:
            problems += [f"run {record.id}: {p}"
                         for p in record.problems(config.goal)]
    except AutoresearchError as exc:
        problems.append(str(exc))

    if skipped_runs:
        print(f"note: {skipped_runs} run row(s) predate adoption of this schema "
              f"and were not checked")
    print(f"checked {len(entries)} entries, {len(found)} skills, "
          f"{len(config.tracks)} tracks, "
          f"{len(config.policy.forbidden_paths + config.policy.human_only + config.policy.never_push_remotes)} "
          f"policy rules")
    if problems:
        print(f"\n{len(problems)} problem(s):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("clean")
    return 0


def _skills(config):
    """Read, and print what could not be read. Every reader in this harness
    reports how many things it read: "no skills" and "one unparseable skill"
    must not be the same screen."""
    found, problems = skills_mod.read_all(config)
    for p in problems:
        print(f"! {p}")
    return found


def cmd_skill_list(args):
    config = _load(args)
    found = _skills(config)
    entries = _store(config).all()
    stale = skills_mod.stale_report(config, found, entries)
    body_lines = sum(s.lines for s in found)
    index_lines = len(found)
    for skill in sorted(found, key=lambda s: s.name):
        mark = "STALE" if skill.name in stale else "ok"
        print(f"{skill.name:<32} {skill.lines:>4}L  {mark:<5} "
              f"cites {', '.join(skill.cites) or '(none)'}")
        print(f"    {textwrap.shorten(skill.description, 96)}")
        for reason in stale.get(skill.name, []):
            print(f"    stale: {reason}")
    pending = skills_mod.undistilled(config, found, entries)
    print(f"\n{len(found)} skill(s); {len(stale)} stale; "
          f"{len(pending)} terminal entr(ies) cited by none")
    if found:
        # The ratio is the design: a brief carries the descriptions, not the
        # bodies. Printing it is what keeps that an observation and not a claim.
        print(f"{body_lines} body lines held, {index_lines} index line(s) "
              f"carried into each brief")
    return 1 if stale else 0


def cmd_skill_show(args):
    config = _load(args)
    for skill in _skills(config):
        if skill.name == args.name:
            print(skills_mod.render(skill))
            return 0
    raise AutoresearchError(f"no skill named {args.name!r}")


def cmd_skill_check(args):
    config = _load(args)
    entries = _store(config).all()
    found, problems = skills_mod.read_all(config)
    problems += skills_mod.check(config, found, entries, skills_mod.best_measurements(config))
    measured = skills_mod.best_measurements(config) is not None
    print(f"checked {len(found)} skill(s) against {len(entries)} entries")
    if not measured and config.goal.derived:
        print("note: no scored run yet, so the derived-constant lint did not "
              "run. That is a gap, not a pass.")
    if problems:
        print(f"\n{len(problems)} problem(s):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("clean")
    return 0


def cmd_skill_retire(args):
    config = _load(args)
    path = skills_mod.retire(config, args.name)
    print(f"retired {path.relative_to(config.paths.root)}: {args.why}")
    return 0


def cmd_skill_distil(args):
    """Run the distil phase once, out of band.

    The same phase the loop runs, so there is one implementation of what a
    distillation is -- a second one here would be the copy that drifts.
    """
    from . import budget as _budget
    from .driver.brain import build_brain
    from .driver.loop import Coordinator, Iteration, _iso

    config = _load(args)
    if args.dry_run:
        found = _skills(config)
        entries = _store(config).all()
        pending = skills_mod.undistilled(config, found, entries)
        stale = skills_mod.stale_report(config, found, entries)
        print(f"{len(found)} skill(s) on disk")
        print(f"{len(pending)} terminal entr(ies) no skill cites:")
        for item in pending:
            print(f"  {item['id']:<6} {item['status']:<10} {item['title']}")
        print(f"{len(stale)} stale skill(s):")
        for name, reasons in stale.items():
            print(f"  {name}: {'; '.join(reasons)}")
        print("\nnothing was written (--dry-run)")
        return 0
    brain = build_brain(config, model=args.model, max_budget_usd=args.max_usd)
    coordinator = Coordinator(config, brain, session=args.session)
    it = Iteration(n=coordinator._last_recorded_n() + 1, kind="out-of-band")
    it.target = coordinator._target()
    try:
        phase = coordinator.distil(it, _budget.iteration_budget(config), force=True)
    finally:
        # Recorded even when the phase raised. The campaign money meter is
        # rebuilt from these records and from nothing else, so a librarian ask
        # that spends and is not written here is spend the next `ar loop` cannot
        # see -- a ceiling that hides an overrun rather than refusing it. The
        # record is marked `out-of-band` so it charges the meters without
        # counting as a sample of what the queue yields.
        it.finished = _iso()
        coordinator._record(it)
    print(phase.name, f"read {phase.read}, did {phase.did}")
    for line in phase.detail:
        print(f"  {line}")
    print(f"cost: ${it.cost_usd:.2f}  "
          f"(recorded as iteration {it.n:04d}, out-of-band)")
    return 0


def cmd_research(args):
    """Spin up targeted research scouts; their ideas land in the record.

    Out-of-band like `ar skill distil`: one phase, run by hand, recorded and
    charged -- a scout ask that spends and is not written here is spend the
    next `ar loop` cannot see. Ideas are filed as ordinary entries, so the
    next `ar rank` prices them and the next `ar loop` can claim them.
    """
    from . import budget as _budget
    from .driver.brain import build_brain
    from .driver.loop import Coordinator, Iteration, _iso

    config = _load(args)
    brain = build_brain(config, model=args.model, max_budget_usd=args.max_usd)
    coordinator = Coordinator(config, brain, session=args.session)
    it = Iteration(n=coordinator._last_recorded_n() + 1, kind="out-of-band")
    it.target = coordinator._target()
    try:
        phase = coordinator.research(it, _budget.iteration_budget(config),
                                     question=" ".join(args.question),
                                     count=args.count)
    finally:
        # Recorded even when the phase raised, for the same reason the distil
        # record is: spend the record does not show is a ceiling that hides it.
        it.finished = _iso()
        coordinator._record(it)
    print(phase.name, f"scouts {phase.read}, filed {phase.did}")
    for line in phase.detail:
        print(f"  {line}")
    print(f"cost: ${it.cost_usd:.2f}  "
          f"(recorded as iteration {it.n:04d}, out-of-band)")
    return 0


def cmd_policy(args):
    config = _load(args)
    print(config.policy.describe())
    failures = config.policy.selftest()
    print(f"\nselftest: {len(failures) or 'every rule refuses something'}")
    for f in failures:
        print(f"  - {f}")
    return 1 if failures else 0


def _iso():
    import datetime as dt
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def _env():
    import os
    return dict(os.environ)


# -- parser ---------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="ar", description=__doc__.splitlines()[0])
    ap.add_argument("--domain", type=pathlib.Path,
                    help="domain root (default: nearest enclosing domain.toml)")
    ap.add_argument("--session", default="cli",
                    help="who is acting; recorded on every state change")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ini = sub.add_parser("init", help="scaffold a new domain")
    ini.add_argument("path", help="directory for the new domain")
    ini.add_argument("--name", help="domain name (default: the directory name)")
    ini.add_argument("--objective", default="cost",
                     help="expression over your metrics, e.g. \"round(T) * Q\"")
    ini.add_argument("--metric", action="append",
                     help="a metric name; repeatable (default: cost)")
    ini.add_argument("--target", type=float, default=100.0)
    ini.add_argument("--force", action="store_true",
                     help="scaffold over an existing domain.toml (destructive)")
    ini.set_defaults(func=cmd_init)

    sub.add_parser("hardware", help="what this machine is and what it can run"
                   ).set_defaults(func=cmd_hardware)

    esc = sub.add_parser(
        "escalate", help="whether to rent compute, which class, and the arithmetic",
        description="Recommends rented compute under two gates, and never "
                    "invents a speedup: a class with no measured ratio for this "
                    "workload is not costed. Nothing here spends money.")
    esc.add_argument("--need", type=float, required=True,
                     help="how many units the work needs, in --unit")
    esc.add_argument("--rate", type=float, required=True,
                     help="measured local throughput, units per second")
    esc.add_argument("--unit", default="units",
                     help="what --need and --rate count (candidates, shots, ops)")
    esc.add_argument("--concurrency", type=int, required=True,
                     help="the concurrency --rate was measured at; a ratio is a "
                          "function of concurrency as well as machine")
    esc.add_argument("--workload", help="what was being run; rates for different "
                                        "workloads never compare")
    esc.add_argument("--machine", help="where --rate was measured "
                                       "(default: this host's fingerprint)")
    esc.add_argument("--lower-bound", action="store_true", dest="lower_bound",
                     help="the rate was still climbing when it was capped")
    esc.add_argument("--hours-available", type=float, dest="hours_available",
                     help="wall clock before the answer stops mattering")
    esc.add_argument("--hardware", help="declared hardware class to check the "
                                        "host against")
    esc.add_argument("--stalled-iterations", type=int, default=0,
                     dest="stalled_iterations",
                     help="iterations stalled for a reason you have established "
                          "is compute-bound")
    esc.add_argument("--stall-threshold", type=int, default=0,
                     dest="stall_threshold",
                     help="how many such iterations count as a trigger")
    esc.add_argument("--correct-locally", action="store_true",
                     dest="correct_locally",
                     help="Gate 0: assert the work is already proven correct on "
                          "local hardware. Asserted, never inferred")
    esc.add_argument("--gate0-note", dest="gate0_note",
                     help="what backs the Gate 0 assertion, or what is missing")
    esc.set_defaults(func=cmd_escalate)

    sub.add_parser("board", help="one screen: goal, queues, claims, measurements"
                   ).set_defaults(func=cmd_board)
    sub.add_parser("render", help="write the generated queue views"
                   ).set_defaults(func=cmd_render)
    sub.add_parser("validate", help="check records, views, runs and policy"
                   ).set_defaults(func=cmd_validate)
    sub.add_parser("policy", help="show never-rules and prove each refuses something"
                   ).set_defaults(func=cmd_policy)

    rankp = sub.add_parser("rank", help="score the queue and explain the numbers")
    rankp.add_argument("--top", type=int, default=3)
    rankp.add_argument("--explore", type=float, default=None, metavar="F",
                       help="share of the shortlist reserved for the largest "
                            "impact, ignoring confidence and cost; overrides "
                            "the domain's coordinator.explore_fraction "
                            f"(core default {rank_mod.EXPLORE_FRACTION}, "
                            "0 disables)")
    rankp.set_defaults(func=cmd_rank)

    sub.add_parser("budget", help="every meter, and the stop decision"
                   ).set_defaults(func=cmd_budget)

    mig = sub.add_parser("migrate", help="convert a prose corpus into records (one way)")
    mig.add_argument("--source", help="root the documents live under")
    mig.add_argument("--track", help="migrate only this track")
    mig.add_argument("--view", help="one document, overriding the track's migrate_from")
    mig.add_argument("--dry-run", action="store_true", dest="dry_run")
    mig.set_defaults(func=cmd_migrate)

    loop = sub.add_parser("loop", help="run the coordinator until it stops")
    loop.add_argument("--iterations", type=int, default=1)
    loop.add_argument("--model")
    loop.add_argument("--max-usd", type=float, dest="max_usd",
                      help="hard ceiling on model spend; defaults to policy.spend_ceiling")
    loop.set_defaults(func=cmd_loop)

    research = sub.add_parser(
        "research", help="spin up targeted research agents; "
                         "their ideas land in the record as entries")
    research.add_argument("question", nargs="+",
                          help="the targeted question the scouts answer")
    research.add_argument("--count", type=int, default=1,
                          help="how many scouts to run in parallel (default 1)")
    research.add_argument("--model")
    research.add_argument("--max-usd", type=float, dest="max_usd",
                          help="hard ceiling on model spend; defaults to policy.spend_ceiling")
    research.set_defaults(func=cmd_research)

    entry = sub.add_parser("entry", help="file, show and list entries")
    esub = entry.add_subparsers(dest="entry_cmd", required=True)
    new = esub.add_parser("new", help="file a new entry")
    new.add_argument("title")
    new.add_argument("--track", default="research")
    new.add_argument("--id")
    new.add_argument("--hypothesis")
    new.add_argument("--prediction")
    new.add_argument("--bar", help="pre-registered confirm/refute criteria")
    new.add_argument("--why", help="why it is filed")
    new.add_argument("--gate", help="precondition on shippability, not on investigating")
    new.add_argument("--confidence", type=float, default=0.5)
    new.add_argument("--impact", type=float, default=0.0)
    new.add_argument("--cost", type=float, default=1.0)
    new.add_argument("--mechanism", action="append")
    new.add_argument("--source", action="append")
    new.add_argument("--core", help="core version the defect was observed on "
                                    "(stamped automatically when the loop files it)")
    new.add_argument("--repro", help="command or path that demonstrates the defect")
    new.add_argument("--observed", help="what actually happened")
    new.add_argument("--expected", help="what should have happened instead")
    new.set_defaults(func=cmd_entry_new)
    show = esub.add_parser("show")
    show.add_argument("id")
    show.set_defaults(func=cmd_entry_show)
    amend = esub.add_parser("amend", help="complete or correct evidence fields")
    amend.add_argument("id")
    amend.add_argument("--title")
    amend.add_argument("--hypothesis")
    amend.add_argument("--core", help="core version the defect was observed on")
    amend.add_argument("--repro", help="command or path that demonstrates it")
    amend.add_argument("--observed", help="what actually happened")
    amend.add_argument("--expected", help="what should have happened instead")
    amend.set_defaults(func=cmd_entry_amend)
    lst = esub.add_parser("list")
    lst.add_argument("--status", action="append")
    lst.add_argument("--track")
    lst.set_defaults(func=cmd_entry_list)

    skill = sub.add_parser("skill", help="the domain's distilled, cited skills")
    ssub = skill.add_subparsers(dest="skill_cmd", required=True)
    ssub.add_parser("list", help="every skill, its citations and its staleness"
                    ).set_defaults(func=cmd_skill_list)
    sshow = ssub.add_parser("show")
    sshow.add_argument("name")
    sshow.set_defaults(func=cmd_skill_show)
    ssub.add_parser("check", help="validate every skill against the record"
                    ).set_defaults(func=cmd_skill_check)
    sdistil = ssub.add_parser("distil", help="run the distil phase once")
    sdistil.add_argument("--dry-run", action="store_true",
                         help="show what would be distilled; write nothing")
    sdistil.add_argument("--model")
    sdistil.add_argument("--max-usd", type=float, dest="max_usd")
    sdistil.set_defaults(func=cmd_skill_distil)
    sretire = ssub.add_parser("retire")
    sretire.add_argument("name")
    sretire.add_argument("--why", required=True,
                         help="a skill removed without a reason is a skill that "
                              "will be written again")
    sretire.set_defaults(func=cmd_skill_retire)

    claim = sub.add_parser("claim", help="take one entry (serialised)")
    claim.add_argument("id")
    claim.add_argument("--why")
    claim.add_argument("--budget", help="human-readable ceiling, recorded beside the numbers")
    claim.add_argument("--max-runs", type=int, dest="max_runs")
    claim.add_argument("--max-hours", type=float, dest="max_hours")
    claim.set_defaults(func=cmd_claim)

    rel = sub.add_parser("release", help="hand a claim back, with a reason")
    rel.add_argument("id")
    rel.add_argument("--why", required=True)
    rel.set_defaults(func=cmd_release)

    reap = sub.add_parser("reap", help="free ONE abandoned claim past the TTL")
    reap.add_argument("id", nargs="?")
    reap.add_argument("--ttl-hours", type=float, dest="ttl_hours")
    reap.set_defaults(func=cmd_reap)

    close = sub.add_parser("close", help="close, reopen or relabel an entry")
    close.add_argument("id")
    close.add_argument("status", nargs="?", help="terminal status for this track")
    close.add_argument("--memo", help="path to the evidence memo; must exist")
    close.add_argument("--summary")
    close.add_argument("--closure", choices=sorted(CLOSURE_KINDS),
                       help="how far a refutation reaches")
    close.add_argument("--reopen-condition", dest="reopen_condition",
                       help="required for slope/cell closures")
    close.add_argument("--why")
    close.add_argument("--reopen", action="store_true")
    close.add_argument("--relabel", choices=sorted(CLOSURE_KINDS))
    close.set_defaults(func=cmd_close)

    meas = sub.add_parser("measure", help="run the domain's measurement, record the row")
    meas.add_argument("--entry")
    meas.add_argument("rest", nargs="*", help="passed through to the domain command")
    meas.set_defaults(func=cmd_measure)

    harness = sub.add_parser(
        "harness", help="this project's defects on the core, packaged for upstream")
    hsub = harness.add_subparsers(dest="harness_cmd", required=True)
    exp = hsub.add_parser(
        "export", help="write the upstream defect bundle",
        description="Collects every defect entry carrying its evidence -- core, "
                    "repro, observed -- into one JSON bundle a person publishes "
                    "upstream. Incomplete entries are refused by name, never "
                    "silently dropped. Publishing is human-only.")
    exp.add_argument("--out", help="write the bundle to this path (default: stdout)")
    exp.add_argument("--all", action="store_true", dest="all",
                     help="include closed defects too")
    exp.set_defaults(func=cmd_harness_export)

    chk = hsub.add_parser(
        "check", help="installed core vs upstream, and what changed",
        description="Read-only. Exit 0 up to date, 1 update available, and "
                    "anything else means the comparison itself failed.")
    chk.add_argument("--ref", help="branch or tag to compare against "
                                   "(default: the [upstream] table, or main)")
    chk.set_defaults(func=cmd_harness_check)

    upd = hsub.add_parser(
        "update", help="pull the latest core; re-render, validate, roll back "
                        "if the upgrade made things worse",
        description="Lands on the resolved head SHA, never a moving ref. "
                    "Only validation problems that are NEW after the upgrade "
                    "trigger a rollback; pre-existing ones ride along.")
    upd.add_argument("--ref", help="branch or tag to pull "
                                   "(default: the [upstream] table, or main)")
    upd.add_argument("--dry-run", action="store_true", dest="dry_run",
                     help="show the plan and the pip command; change nothing")
    upd.set_defaults(func=cmd_harness_update)

    return ap


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except AutoresearchError as exc:
        print(f"ar: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
