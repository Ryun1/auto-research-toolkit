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

from . import budget as budget_mod, rank as rank_mod, render, runs as runs_mod
from .claims import Claims
from .config import DomainConfig, discover
from .entries import Entry, Result, Store
from .errors import AutoresearchError
from .states import CLOSURE_KINDS


def _load(args) -> DomainConfig:
    return DomainConfig.load(args.domain or discover())


def _store(config) -> Store:
    return Store(config.paths.entries)


def _probe_target(config):
    """Resolve the goal's target, running the domain's probe if it needs one."""
    def probe(command):
        result = subprocess.run(command.split(), cwd=config.paths.root,
                                capture_output=True, text=True)
        if result.returncode != 0:
            raise AutoresearchError(
                f"target probe {command!r} failed ({result.returncode}): "
                f"{result.stderr.strip()[:300]}")
        return float(result.stdout.strip().splitlines()[-1])
    return config.goal.target.resolve(probe)


# -- verbs ----------------------------------------------------------------

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
    best = None
    for record in all_runs:
        if record.status != "ok":
            continue
        try:
            value = config.goal.objective_value(record.metrics)
        except AutoresearchError:
            continue
        if best is None or value < best[0]:
            best = (value, record.entry or record.id)
    print(render.board(config, entries, all_runs, target, best, skipped))
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
        gate=args.gate or "")
    if store.exists(entry.id):
        raise AutoresearchError(
            f"{entry.id} already exists at {store.path(entry.id)}. Ids are "
            "filenames, so a collision is loud here rather than a silent "
            "renumber after every citation was written (H79).")
    print(store.save(entry))
    return 0


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
    domain = budget_mod.domain_budget(config, spent_runs=len(all_runs))
    remaining = domain["runs"].remaining()
    ranking = rank_mod.rank(entries, config,
                            budget_ok=lambda e: e.cost <= remaining)
    print(ranking.explain())
    if args.top:
        print(f"\nshortlist (top {args.top}):")
        for s in ranking.shortlist(args.top):
            print(f"  {s.entry_id}  {s.title}")
    return 0


def cmd_budget(args):
    config = _load(args)
    entries = _store(config).all()
    all_runs = runs_mod.read_all(config.paths.runs)
    domain = budget_mod.domain_budget(config, spent_runs=len(all_runs))
    iteration = budget_mod.iteration_budget(config)
    print(domain.report())
    print()
    print(iteration.report())
    held = [e for e in entries if e.claim
            and not config.track_for(e.id).machine.status(e.status).terminal]
    print(f"\n{len(held)} live claim(s)")
    for entry in held:
        spent = sum(1 for r in all_runs if r.entry == entry.id)
        print(f"  {entry.id}  {entry.claim.session}")
        print("    " + budget_mod.claim_budget(entry, config).report()
              .replace("\n", "\n    "))
        over = None
        if entry.claim.max_runs and spent > entry.claim.max_runs:
            over = f"max_runs {spent} > {entry.claim.max_runs}"
        if over:
            print(f"    OVERRUN: {over}")

    try:
        target = _probe_target(config)
    except AutoresearchError:
        target = None
    best = None
    for record in all_runs:
        if record.status != "ok":
            continue
        try:
            value = config.goal.objective_value(record.metrics)
        except AutoresearchError:
            continue
        if best is None or value < best:
            best = value
    decision = budget_mod.should_stop(
        config,
        measurements=None,
        target=target,
        budgets=[domain])
    print(f"\nstop decision: {decision}")
    if best is not None and target is not None:
        met = config.goal.direction == "minimise" and best < target
        print(f"best objective {best:,.0f} vs target {target:,.0f}"
              + ("  ** GOAL MET **" if met else ""))
    return 0


def cmd_loop(args):
    """Run the coordinator. The unattended entry point."""
    from .driver.brain import SDKBrain
    from .driver.loop import Coordinator

    config = _load(args)
    brain = SDKBrain(config, model=args.model, max_budget_usd=args.max_usd)

    def probe(command):
        result = subprocess.run(command.split(), cwd=config.paths.root,
                                capture_output=True, text=True)
        return float(result.stdout.strip().splitlines()[-1])

    coordinator = Coordinator(config, brain, session=args.session,
                              probe_target=probe)
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
    print(f"checked {len(entries)} entries, {len(config.tracks)} tracks, "
          f"{len(config.policy.forbidden_paths + config.policy.human_only + config.policy.never_push_remotes)} "
          f"policy rules")
    if problems:
        print(f"\n{len(problems)} problem(s):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("clean")
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
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


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
    new.set_defaults(func=cmd_entry_new)
    show = esub.add_parser("show"); show.add_argument("id"); show.set_defaults(func=cmd_entry_show)
    lst = esub.add_parser("list")
    lst.add_argument("--status", action="append")
    lst.add_argument("--track")
    lst.set_defaults(func=cmd_entry_list)

    claim = sub.add_parser("claim", help="take one entry (serialised)")
    claim.add_argument("id")
    claim.add_argument("--why")
    claim.add_argument("--budget", help="human-readable ceiling, recorded beside the numbers")
    claim.add_argument("--max-runs", type=int, dest="max_runs")
    claim.add_argument("--max-hours", type=float, dest="max_hours")
    claim.set_defaults(func=cmd_claim)

    rel = sub.add_parser("release", help="hand a claim back, with a reason")
    rel.add_argument("id"); rel.add_argument("--why", required=True)
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
