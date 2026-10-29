"""`ar` -- the command surface.

One entry point per verb, every verb reading the same records. There is no
command here that edits a queue document, because there is no queue document to
edit: the views are generated, and `ar validate` refuses a view that was edited
by hand. Every verb that saves an entry record re-renders the views itself
(`_render_views`), so a hand-driven agent never eats a failed validate between
a mutation and its next read; `ar render` remains the surface that narrates
what was written.

Unknown flags are refused by argparse, deliberately. In the source harness
**eight tools silently ignored an unrecognised flag and ran their default mode
instead, so a mistyped flag read as a successful run of what you asked for**
(H135). That is the worst available failure mode and it is free to avoid.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import math
import os
import pathlib
import subprocess
import sys
import textwrap
import time

from . import attempts as attempts_mod
from . import budget as budget_mod
from . import bundles as bundles_mod
from . import challenge as challenge_mod
from . import defects as defects_mod
from . import entries as entries_mod
from . import escalate as escalate_mod
from . import external as external_mod
from . import gates as gates_mod
from . import hardware as hw
from . import merge as merge_mod
from . import plugins as plugins_mod
from . import rank as rank_mod
from . import relocate as relocate_mod
from . import render, scoring
from . import runs as runs_mod
from . import skills as skills_mod
from . import upstream as upstream_mod
from . import usage as usage_mod
from . import validate as validate_mod
from .claims import Claims, claim_state_problems
from .config import DomainConfig, discover
from .entries import EVIDENCE_CLASSES, Entry, Event, Result, Store
from .errors import AutoresearchError, SchemaError
from .states import CLOSURE_KINDS, REOPEN_REASONS


def _load(args) -> DomainConfig:
    return DomainConfig.load(args.domain or discover())


def _store(config) -> Store:
    return Store(config.paths.entries)


def _make_probe(config, env=None):
    """The target probe as a callable: the domain's command, run, and refused
    by name when it fails. One derivation -- `ar loop`, `ar budget` and the
    board must not disagree about what a failed probe looks like."""
    def probe(command):
        result = subprocess.run(command.split(), cwd=config.paths.root,
                                capture_output=True, text=True, env=env)
        if result.returncode != 0:
            raise AutoresearchError(
                f"target probe {command!r} failed ({result.returncode}): "
                f"{result.stderr.strip()[:300]}")
        return float(result.stdout.strip().splitlines()[-1])
    return probe


def _probe_target(config):
    """Resolve the goal's target, running the domain's probe if it needs one."""
    return config.goal.target.resolve(_make_probe(config))


def _render_views(config) -> None:
    """Re-write the generated views after a mutation. `ar validate` refuses a
    view that disagrees with the records, and the queue view carries status and
    claim state -- so a verb that saves an entry record re-renders here, or
    every hand-driven mutation costs a failed validate before the next read.
    Silent by design: `ar render` is the surface that narrates what was written.

    Every caller has already saved, so a sibling file that will not load is not
    this command's failure: the write landed and only the views are stale. A
    refusal here made a successful close exit 2, the retry was refused as a
    no-op transition, and that refusal read as proof nothing happened (#29,
    QSB H300). It warns instead, and `main` exits VIEWS_STALE, not 2.
    """
    try:
        entries = _store(config).all()
    except SchemaError as exc:
        print(f"ar: warning: the write landed, but the views were not "
              f"re-rendered: {exc}\n  fix that file, then run `ar validate` "
              f"and `ar render`", file=sys.stderr)
        _VIEWS_STALE.append(str(exc))
        return
    render.write_views(config, entries)


# Exit status of a command whose write landed but whose view re-render could
# not read the store: distinct from 2, which means nothing was written.
VIEWS_STALE = 3
_VIEWS_STALE: list[str] = []


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

    validation = cmd_validate(argparse.Namespace(domain=str(root)))
    print(textwrap.dedent(f"""
        Next:
          1. edit  bin/measure   -- replace evaluate() with your real experiment
          2. edit  goal.yaml     -- the metrics it returns, and what winning means
          3. edit  guides/landscape.md -- what an agent needs to know to guess well
          4. run   ar --domain {root} board
          5. run   ar --domain {root} hardware   -- what this machine can do
          6. later ar --domain {root} skill list -- what the loop has distilled
        """).rstrip())
    return validation


def cmd_kit_list(args):
    """The kits this core ships, and which this domain declares."""
    from . import kits
    config = _load(args)
    for name in sorted(kits.KITS):
        declared = name in config.evaluator_kits
        copy = config.paths.root / kits.vendored(name)
        state = ("not declared" if not declared
                 else "missing" if not copy.is_file()
                 else "current" if copy.read_bytes() == kits.source(name).read_bytes()
                 else "differs from this core")
        print(f"{name:10} {kits.vendored(name):22} {state}")
    return 0


def cmd_kit_sync(args):
    """Vendor every declared kit into the domain, as this core ships it."""
    from . import kits
    config = _load(args)
    if not config.evaluator_kits:
        raise AutoresearchError(
            "this domain declares no kits; add `kits = [\"<name>\"]` under "
            f"[evaluator] in domain.toml (shipped: {sorted(kits.KITS)})")
    written = kits.sync(config)
    for path in written:
        print(f"wrote {path}")
    if not written:
        print("every declared kit is current")
    else:
        print("a kit is part of the evaluator: commit it, and re-measure "
              "before comparing runs across the change")
    return 0


def cmd_doctor(args):
    """Diagnose the environment this domain's records are produced in.

    Read-only. Reports which core answered (version, editable or copied,
    install source), which interpreter and module resolved, and whether the
    domain's declared `min_core` floor holds. Non-zero exit when a check
    fails, so a scheduled run can watch it.
    """
    config = _load(args)
    lines, problems = upstream_mod.doctor(config)
    for line in lines:
        print(line)
    for problem in problems:
        print(f"! {problem}")
    if not problems:
        print("environment consistent with this domain's declarations")
    return 1 if problems else 0


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


# -- challenge: the public challenge's shared intel --------------------------

def _challenge_params(config):
    c = config.challenge
    if not c.source:
        raise AutoresearchError(
            "this domain declares no [challenge] table; add `source = "
            "\"provablyfast\"` (or \"yukon\") to domain.toml to follow one")
    return (c.source, c.url or challenge_mod.DEFAULT_URLS.get(c.source, ""),
            c.benchmark, c.refresh_seconds, c.timeout_seconds)


def cmd_challenge_pull(args):
    """Fetch-if-stale, normalize, cache under state/challenge/. No record is
    touched: a leaderboard is input, not a verdict this campaign paid for."""
    config = _load(args)
    snapshot, action = challenge_mod.pull(
        *_challenge_params(config), config.paths.root,
        force=args.force)
    print(f"{snapshot.source}/{snapshot.challenge}"
          + (f"/{snapshot.benchmark}" if snapshot.benchmark else "")
          + f": {action}")
    if snapshot.record:
        print(f"  record  {snapshot.record.value:,.10g} {snapshot.record.unit}"
              + (f"  by {snapshot.record.by}" if snapshot.record.by else "")
              + (f"  {snapshot.record.at}" if snapshot.record.at else ""))
    if snapshot.bar:
        print(f"  bar     {snapshot.bar}")
    if snapshot.board:
        print(f"  board   {len(snapshot.board)} row(s)")
    if snapshot.truth_label:
        print(f"  label   {snapshot.truth_label}")
    return 0


def cmd_challenge_show(args):
    """Render the cached snapshot. Reads no network -- what `pull` last saw."""
    config = _load(args)
    source, *_ = _challenge_params(config)
    snapshot = challenge_mod.load_snapshot(config.paths.root)
    if snapshot is None:
        print("no snapshot cached; run `ar challenge pull` first",
              file=sys.stderr)
        return 1
    print(f"{snapshot.source}/{snapshot.challenge}"
          + (f"/{snapshot.benchmark}" if snapshot.benchmark else "")
          + f"  (fetched {snapshot.fetched_at}"
          + (f", published {snapshot.generated_at}" if snapshot.generated_at
             else "") + ")")
    # The page in the credit is the one this campaign fetched, never a page
    # pinned to the adapter: two challenges on one site are two different
    # boards, and citing the wrong one is a false attribution (sig.golf H1).
    print(f"credit: {challenge_mod.credit(snapshot)}")
    if snapshot.truth_label:
        print(f"truth: {snapshot.truth_label}")
    if snapshot.record:
        print(f"\nrecord: {snapshot.record.value:,.10g} {snapshot.record.unit}"
              + (f"  by {snapshot.record.by}" if snapshot.record.by else "")
              + (f"  {snapshot.record.at}" if snapshot.record.at else ""))
    target = snapshot.target_value()
    if target is not None:
        print(f"target (what bin/probe-target should chase): {target:,.10g}")
    if snapshot.bar:
        print(f"bar: {snapshot.bar}")
    saturation = snapshot.saturation()
    if saturation:
        print(f"saturation: top-5 spread {saturation['top5_spread_pct']}%, "
              f"{saturation['n_solvers']} solver(s)")
    if snapshot.board:
        print(f"\n{'rank':>4}  {'solver':<24} {'value':>16} {'delta':>12}  when")
        for row in snapshot.board:
            print(f"{row['rank']:>4}  {row['solver']:<24} {row['value']:>16,} "
                  f"+{row['delta']:>11,}  {row['at']}")
    else:
        print("\nboard: no rows parsed from this source -- "
              + challenge_mod.unreadable_reason(snapshot.source, snapshot.url))
    if snapshot.policy:
        policy = snapshot.policy
        print(f"\npolicy: {len(policy.get('allowed_paths') or [])} allowed, "
              f"{len(policy.get('forbidden_paths') or [])} forbidden path(s)")
        for path in (policy.get("allowed_paths") or []):
            print(f"  allowed: {path}")
        if policy.get("selection_basis"):
            print(f"  basis: {policy['selection_basis']}")
    if snapshot.upstream:
        print(f"upstream: {snapshot.upstream}")
    if snapshot.graph:
        print(f"graph: {snapshot.graph}")
    return 0


def cmd_challenge_check(args):
    """Comparability findings against the published policy. Findings are
    loud notes, not refusals -- but they are exit-1 findings, so a mismatch
    cannot scroll past unnoticed."""
    config = _load(args)
    findings = challenge_mod.check(config)
    for finding in findings:
        print(f"! {finding}")
    if not findings:
        print("published policy consistent with this host and [policy]")
    return 1 if findings else 0


def cmd_challenge_target(args):
    """Print the target number, one line. The domain's `bin/probe-target`
    wraps this: `exec ar challenge target` -- the probe reads the last stdout
    line as a float. Notes go to stderr, which the probe keeps for failures."""
    config = _load(args)
    if not config.challenge.source:
        value = _goal_target_without_challenge(config)
    else:
        value = challenge_mod.target_value(
            *_challenge_params(config), config.paths.root)
    print(f"{value:.10g}")
    return 0


#: Set while `ar challenge target` runs the goal's probe, so a probe that is
#: itself `exec ar challenge target` refuses instead of recursing.
_TARGET_FALLBACK_ENV = "AR_CHALLENGE_TARGET_FALLBACK"


def _goal_target_without_challenge(config) -> float:
    """With no [challenge] table, the goal's own target -- the number
    `ar board` and the loop already read. Refusing here while the board
    answered was a trap three curator passes fell into (#26)."""
    if os.environ.get(_TARGET_FALLBACK_ENV):
        raise AutoresearchError(
            "no [challenge] table, and goal.yaml's target source wraps "
            "`ar challenge target` itself, so there is no number to read: "
            "declare [challenge] in domain.toml, or point the goal's target "
            "at a probe that does not call this verb")
    target = config.goal.target
    print(f"challenge target: no [challenge] table; reading goal.yaml's "
          f"target ({target.source or f'value {target.value:g}'})",
          file=sys.stderr)
    return target.resolve(_make_probe(
        config, env={**os.environ, _TARGET_FALLBACK_ENV: "1"}))


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
    # The frontier compares one instrument's rows (#40): the current epoch,
    # or the one `--epoch` names. Every other epoch keeps its best row on the
    # board under its own heading, never on the frontier.
    from . import epochs as epochs_mod
    listed = epochs_mod.segment(config, all_runs)
    label = getattr(args, "epoch", None)
    picked = [e for e in listed if (e.current if label is None else e.label == str(label))]
    if label is not None and not picked:
        raise AutoresearchError(
            f"no epoch {label!r}; this ledger has "
            f"{', '.join(e.label for e in listed) or 'none'} (see `ar epochs`)")
    chosen = picked[0].rows if picked else []
    best, excluded = runs_mod.frontier(store, chosen, config.goal)
    print(render.board(config, entries, all_runs, target,
                       (best[0], best[1].entry or best[1].id) if best else None,
                       skipped, best_metrics=best[1].metrics if best else None,
                       unmet_required_gates=budget_mod.unmet_required_gates(
                           config.goal, store, best),
                       excluded_best=excluded, overruns=_overruns(config, store, entries)))
    from . import holdout as holdout_mod
    if holdout_mod.declared(config):
        print(_holdout_line(holdout_mod.summary(config, best, target)) + "\n")
    others = [e for e in listed if e.rows and not (picked and e is picked[0])]
    if others:
        print("## Previous epochs" if getattr(args, "epoch", None) is None
              else "## Other epochs")
        for epoch in others:
            top, _ = runs_mod.frontier(store, epoch.rows, config.goal)
            print("  " + _epoch_line(epoch)
                  + (f"  best {render._fmt(top[0])} ({top[1].entry or top[1].id})"
                     if top else "  no valid row"))
        print("  (`ar board --epoch N` reads one; `ar epochs` lists what a move "
              "may have invalidated)")
    return 0


def _overruns(config, store, entries) -> dict:
    """Live claims past a ceiling they registered, by entry id (#22) -- the
    same test `ar budget` prints per claim."""
    claims = Claims(store, config, "board")
    out = {}
    for entry in entries:
        if not entry.claim or config.track_for(entry.id).machine.status(
                entry.status).terminal:
            continue
        over = claims.overrun(entry.id, runs_spent=budget_mod.claim_runs(config, entry))
        if over:
            out[entry.id] = over
    return out


def _holdout_line(summary) -> str:
    """Pass/fail and the count: everything the search may know (#38)."""
    verdict = summary["last"] or "not consulted on the best config"
    cap = f" of {summary['max']}" if summary["max"] else ""
    return (f"holdout    {verdict}  ({summary['consults']}{cap} "
            f"consultation(s) this epoch)")


def cmd_holdout_check(args):
    """Consult the holdout on the current best config, explicitly (#38).

    For a hand-driven campaign with no loop to consult it at the stop
    decision. One consultation per config per epoch: a second check of the
    same config reads the recorded verdict. Prints pass/fail and the count,
    never a value."""
    config = _load(args)
    from . import epochs as epochs_mod
    from . import holdout as holdout_mod
    if not holdout_mod.declared(config):
        raise AutoresearchError("this domain declares no [commands] holdout")
    store = _store(config)
    best, _ = runs_mod.frontier(
        store, epochs_mod.current_rows(config, runs_mod.read_all(config.paths.runs)),
        config.goal)
    if best is None:
        raise AutoresearchError("no best row in the current epoch to check")
    target = _probe_target(config)
    verdict = holdout_mod.consult(config, best[1], target, args.session, retry=True)
    if verdict.passed is None:
        raise AutoresearchError(f"{best[1].id}: {verdict.reason}")
    print(f"{best[1].id}: {'pass' if verdict.passed else 'fail'} -- {verdict.reason}")
    print(_holdout_line(holdout_mod.summary(config, best, target)))
    return 0


def cmd_promote(args):
    """A person's release of a suspect row (#39).

    A row held as suspect waits for a clean re-measure from another session;
    a domain that wants a person to decide instead lists `ar promote` under
    `[[policy.human_only]]`, so an agent may prepare the case and never run
    it. The release is a file beside the entry store, read by every frontier
    reader -- the row itself is never edited."""
    config = _load(args)
    why = (args.why or "").strip()
    if not why:
        raise AutoresearchError("promoting a suspect row needs --why: what "
                                "made the gain believable")
    from .evaluator import _RUN_ID
    if not _RUN_ID.fullmatch(args.run or ""):
        raise AutoresearchError(f"{args.run!r} is not a run id")
    if args.run not in {r.id for r in runs_mod.read_all(config.paths.runs)}:
        raise AutoresearchError(f"no run {args.run!r} in the ledger")
    directory = runs_mod.promotions_dir(_store(config))
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{args.run}.json"
    if path.exists():
        raise AutoresearchError(f"{args.run} is already promoted ({path})")
    path.write_text(json.dumps({"run": args.run, "who": args.session,
                                "why": why, "at": _iso()}, sort_keys=True) + "\n")
    print(f"{args.run} promoted: it may stand on the frontier")
    return 0


def cmd_epochs(args):
    """The ledger's epochs, and the closures pinned to a superseded one.

    A closure decided under an evaluator or benchmark that has since moved
    may no longer hold; its stored reopen condition says what would reopen
    it. This lists those candidates for a person or curator to judge -- it
    never reopens anything (#40, #34)."""
    config = _load(args)
    from . import epochs as epochs_mod
    current = epochs_mod.current_key(config)
    listed = epochs_mod.segment(config, runs_mod.read_all(config.paths.runs),
                                current=current)
    print("epochs")
    for epoch in listed or [epochs_mod.Epoch("1", current, True)]:
        print("  " + _epoch_line(epoch))
    superseded, unpinned = [], 0
    for entry in _store(config).read_all()[0]:
        if entry.result is None:
            continue
        try:
            terminal = config.track_for(entry.id).machine.status(
                entry.result.verdict).terminal
        except AutoresearchError:
            continue
        if not terminal:
            continue
        key = epochs_mod.pin_key(entry.result.pin)
        if key is None:
            unpinned += 1
        elif key != current:
            superseded.append(entry)
    print()
    if superseded:
        print(f"closures pinned to a superseded epoch ({len(superseded)}) -- "
              "candidates to reopen, not reopened:")
        for entry in superseded:
            kind = f"/{entry.result.closure_kind}" if entry.result.closure_kind else ""
            reopen = entry.result.reopen_condition or "(no reopen condition stated)"
            print(f"  {entry.id}  {entry.result.verdict}{kind}  "
                  f"evaluator {entry.result.pin.get('evaluator_sha')}  "
                  f"reopens on: {reopen}")
    else:
        print("no closure is pinned to a superseded epoch")
    if unpinned:
        print(f"{unpinned} closure(s) carry no pin (closed before #40, or "
              "without a stamp); they cannot be placed in an epoch")
    return 0


def _epoch_line(epoch) -> str:
    if epoch.key is None:
        what = "rows from before the stamp"
    else:
        sha, benchmark = epoch.key
        what = f"evaluator {sha}" + (f", benchmark {benchmark}" if benchmark else "")
    return (f"epoch {epoch.label}{' (current)' if epoch.current else ''}: "
            f"{what}; {len(epoch.rows)} row(s)")


def cmd_render(args):
    config = _load(args)
    if getattr(args, "install_merge_driver", False):
        added = render.install_merge_driver(config)
        print(f"merge driver `{render.MERGE_DRIVER}` set in this clone's git "
              f"config; .gitattributes marks {len(render.view_paths(config))} "
              f"view(s) ({len(added)} new). Commit .gitattributes; run this "
              f"once in every other clone, and `ar render` after a merge.")
        return 0
    for path in render.write_views(config, _store(config).all()):
        print(f"wrote {path.relative_to(config.paths.root)}")
    return 0


def cmd_entry_collisions(args):
    """Entry ids both sides of a merge touched since they diverged (#35).

    Ids are filenames, so two branches filing the same number meet as an
    add/add on one path, and two branches changing one entry meet as a
    textual merge of a YAML record -- which git can conclude cleanly and
    wrongly, keeping one side's field (qsb H90, H107). #21 made filing
    exclusive inside one checkout; this is the cross-branch half, to run
    before the merge."""
    config = _load(args)
    root = config.paths.root
    if not config.paths.entries.is_relative_to(root):
        raise AutoresearchError("[state] entries is outside the domain root; "
                                "compare it from its own checkout")
    scope = str(config.paths.entries.relative_to(root))

    def git(*argv):
        try:
            proc = subprocess.run(["git", *argv], cwd=root, capture_output=True,
                                  text=True)
        except OSError as exc:
            raise AutoresearchError(f"git is not available: {exc}") from exc
        if proc.returncode != 0:
            raise AutoresearchError(f"git {' '.join(argv)}: {proc.stderr.strip()}")
        return proc.stdout

    base = git("merge-base", "HEAD", args.ref).strip()

    def touched(tip):
        out = {}
        for line in git("diff", "--name-status", "--no-renames", "--relative",
                        base, tip, "--", scope).splitlines():
            status, path = line.split("\t", 1)
            out[path] = status
        return out

    def blob(tip, path):
        proc = subprocess.run(["git", "rev-parse", "-q", "--verify",
                               f"{tip}:./{path}"], cwd=root,
                              capture_output=True, text=True)
        return proc.stdout.strip() if proc.returncode == 0 else None

    ours, theirs = touched("HEAD"), touched(args.ref)
    # Both sides ending at the same bytes -- a cherry-pick, an identical
    # filing, a delete on both -- merges cleanly and loses nothing.
    both = sorted(path for path in set(ours) & set(theirs)
                  if blob("HEAD", path) != blob(args.ref, path))
    if not both:
        print(f"no entry collides with {args.ref}")
        return 0
    print(f"{len(both)} entry id(s) touched on both sides since {base[:12]}:")
    for path in both:
        entry_id = pathlib.Path(path).stem
        what = ("filed on both sides -- two different entries, one id"
                if ours[path] == theirs[path] == "A"
                else "changed on both sides -- a clean merge may keep only one side")
        print(f"  {entry_id}  {what}")
    return 1


def cmd_entry_graph(args):
    """Print the graph view: the hypothesis tree as a mermaid diagram.

    Same markdown `ar render` writes to the track's `graph_view`, so an agent
    can paste it into a memo without a second renderer that could disagree
    with the generated one. `--html` writes a standalone snapshot page
    instead, with hover tooltips carrying the full title and summary."""
    config = _load(args)
    if args.track and args.track not in config.tracks:
        raise AutoresearchError(
            f"track {args.track!r} is not declared by this domain; "
            f"declared: {sorted(config.tracks)}")
    tracks = [t for t in config.tracks.values()
              if not args.track or t.id == args.track]
    if not args.track:
        # A track that has not declared graph_view has opted out of the
        # picture (harness debt, typically: "we don't want that graph").
        # An explicit --track overrides the opt-out; a domain where no
        # track declares it falls back to every track, so the verb still
        # works for an ad-hoc look.
        declared = [t for t in tracks if t.graph_view]
        tracks = declared or tracks
    entries = _store(config).all()
    if args.html:
        path = pathlib.Path(args.html)
        if not path.is_absolute():
            path = config.paths.root / path
        path.write_text(render.graph_html(tracks, entries))
        print(f"wrote {path}")
        return 0
    for track in tracks:
        print(render.graph_view(track, entries), end="")
    return 0


def _core_version(named: str | None) -> str:
    """`--core auto` names this core, which is what the documented filing
    command asks for.

    It was never implemented: `auto` was stored verbatim and `harness export`
    shipped it, so a defect filed the way AGENTS.md says carried no version at
    all -- and that field exists so upstream can reproduce what the reporter saw
    (sig.golf H20).
    """
    if (named or "").strip().lower() == "auto":
        return upstream_mod.installed().version
    return named or ""


def cmd_entry_new(args):
    config = _load(args)
    if args.track not in config.tracks:
        raise AutoresearchError(
            f"track {args.track!r} is not declared by this domain; "
            f"declared: {sorted(config.tracks)}")
    store = _store(config)
    track = config.tracks[args.track]
    if args.id:
        # An id no track owns was written, and that one file then stopped
        # every store-reading verb for the whole domain (#18).
        owner = config.track_for(args.id)
        if owner.id != track.id:
            raise AutoresearchError(
                f"id {args.id!r} belongs to track {owner.id!r} (prefix "
                f"{owner.prefix!r}), not {track.id!r} (prefix {track.prefix!r})")
    from . import tree
    # The same gate the coordinator's generate phase runs: a human or a
    # native-dispatch agent files through the identical tree rules, so the
    # CLI path and the loop path cannot diverge into different trees.
    # Check and create under one filing lock (#48): the sibling cap read
    # here must still hold when the entry lands, or two concurrent branch
    # filings both pass it.
    with store.filing_lock(f"entry new ({args.session})"):
        tree.check_derived(store, getattr(args, "derived_from", None) or "",
                           args.parent or "", track.id)
        tree.check_branch(config, store, args.parent or "", args.kind or "",
                          track.id)
        entry = Entry(
            id=args.id or store.next_id(track.prefix),
            track=track.id, title=args.title,
            status=track.machine.initial,
            parent=args.parent or "", kind=args.kind or "",
            derived_from=getattr(args, "derived_from", None) or "",
            hypothesis=args.hypothesis or "", prediction=args.prediction or "",
            bar=args.bar or "", why_filed=args.why or "",
            confidence=args.confidence, impact=args.impact, cost=args.cost,
            mechanisms=args.mechanism or [], sources=args.source or [],
            gate=args.gate or "", scaffold=bool(args.scaffold),
            core=_core_version(args.core), repro=args.repro or "",
            observed=args.observed or "", expected=args.expected or "")
        if track.gates:
            # Filed with the track's declared gates attached, so `ar gates update`
            # works on a fresh entry without a `gates configure` first (#20).
            from .gates import configure as configure_gates
            configure_gates(entry, track.gates)
        if track.requires_defect_evidence:
            # The track already declares this rule; before H15 only `harness
            # export` and `ar validate` read it, so a defect could sit in the
            # queue for a whole session with nothing a stranger could reproduce.
            # Refused here, before the record is written: the id this filing
            # computed is never reserved, so the next filing takes the same number
            # and a refused defect costs no id.
            from . import defects
            problems = defects.evidence_problems(entry, track)
            if problems:
                raise AutoresearchError("; ".join(problems))
        # Exclusive (#21): a number another session filed after this one read
        # the store is skipped rather than overwritten. A `--id` the caller chose
        # is never moved -- a taken one is refused (H79).
        print(store.create(entry, renumber=not args.id))
    _render_views(config)
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
    # getattr, not attribute access: the loop and older callers hand this a
    # namespace built from their own field list, and a correction surface
    # that only works for the CLI's parser is a correction surface with a
    # second, silent way to fail.
    changes = {name: value for name, value in (
        (name, getattr(args, name, None)) for name in (
            "title", "hypothesis", "core", "repro", "observed", "expected",
            "parent", "kind", "prediction", "bar", "why_filed", "scaffold"))
        if value is not None}
    # The three filing-time fields a wrong number hides in. They are corrected
    # in place like any other field, and like `reprice` and
    # `correct_result_text` they demand a reason: `prediction` and `bar` are
    # what the entry promised *before* it ran, so a silent rewrite is
    # indistinguishable from having predicted it all along (sig.golf H6 --
    # a Q6 predicted "at most 70,400 instructions" against a measured
    # 145,952, and the only writer was a hand edit of the YAML).
    if ({"prediction", "bar", "why_filed"} & set(changes)
            and not (getattr(args, "why", None) or "").strip()):
        raise AutoresearchError(
            "correcting --prediction/--bar/--why-filed changes what the entry "
            "promised before it ran; pass --why to say why the filed text was "
            "wrong")
    if changes.get("parent") is not None:
        # Re-parenting runs the same gate the filing path runs: a parent that
        # does not exist, or one on another track, is a broken lineage, and
        # `--kind`/`--parent` must move together (a kind on a parentless entry
        # is refused by the same rule).
        from . import tree
        tree.check_branch(config, store, changes["parent"],
                          changes.get("kind") or entry.kind,
                          config.track_for(entry.id).id)
        if changes["parent"] and entry.derived_from:
            # A branch is already lineage: the derivation it replaces goes,
            # or the entry is both on the tree and beside it (#23 review).
            changes["derived_from"] = ""
    # `sources` and `mechanisms` are typed fields (the exclusion vocabulary
    # and the record's citations), and a blind full-value write on a list is
    # how one session's list silently drops another's. So they get explicit
    # writers in both directions: `--source`/`--mechanism` add, and
    # `--drop-source`/`--drop-mechanism` remove. Either act on something that
    # is not there is a no-op the caller should hear about, not a silent
    # success that looks like a correction.
    added: dict[str, list[str]] = {}
    dropped: dict[str, list[str]] = {}
    for name, add_flag, drop_flag in (
            ("sources", "source", "drop_source"),
            ("mechanisms", "mechanism", "drop_mechanism")):
        current = list(getattr(entry, name))
        for value in getattr(args, drop_flag, None) or []:
            if value not in current:
                raise AutoresearchError(
                    f"{entry.id}: {value!r} is not in {name}; nothing to drop")
            current.remove(value)
            dropped.setdefault(name, []).append(value)
        for value in getattr(args, add_flag, None) or []:
            # Checked against the post-drop list, so a rename (drop the old,
            # add the new) in one amend is legal while a pure duplicate is
            # refused rather than silently succeeding.
            if value in current:
                raise AutoresearchError(
                    f"{entry.id}: {value!r} is already recorded in {name}; "
                    "nothing to add")
            current.append(value)
            added.setdefault(name, []).append(value)
        if added.get(name) or dropped.get(name):
            setattr(entry, name, current)
    # A closure snapshots the mechanisms it tested, and `rank` excludes other
    # entries by that snapshot, so an amend that reached only the entry's list
    # left the old tags excluding while the record read as corrected (mldsa87
    # H6). On a closed entry the amend is the correction of what the verdict
    # covers: it moves the snapshot too, and like a relabel it needs a reason.
    closure_was = None
    if entry.result is not None and (added.get("mechanisms")
                                     or dropped.get("mechanisms")):
        if not (getattr(args, "why", None) or "").strip():
            raise AutoresearchError(
                f"{entry.id} is closed, so its mechanisms are what the verdict "
                "excludes other entries by; pass --why to say why they change")
        closure_was = entry.closure_mechanisms()
        entry.result.mechanisms = list(entry.mechanisms)
    if args.summary is not None or args.reopen_condition is not None:
        # Correcting a closure's own text is a different act from editing an
        # open entry's fields: it is legal on a terminal entry, it leaves the
        # status and the verdict alone, and it demands a reason (QSB H82).
        if changes or added or dropped:
            raise AutoresearchError(
                "--summary/--reopen-condition correct a closure's text; pass "
                "them alone or run the two amends separately")
        entry.correct_result_text(args.summary, args.reopen_condition,
                                  who=args.session, why=args.why or "")
        store.save(entry)
        _render_views(config)
        print(f"{entry.id} closure text corrected")
        return 0
    if args.evidence_class is not None:
        # One writer for the class declaration, mirroring `close --relabel`:
        # the result is untouched apart from the typed field, and the reason
        # is mandatory (entries.set_evidence_class refuses without one).
        if changes or added or dropped:
            raise AutoresearchError(
                "--evidence-class is a reclassification, not a field edit; "
                "pass it alone or run the two amends separately")
        entry.set_evidence_class(args.evidence_class, who=args.session,
                                 why=args.why or "")
        store.save(entry)
        _render_views(config)
        print(f"{entry.id} evidence class set to {args.evidence_class}")
        return 0
    if not changes and not added and not dropped:
        raise AutoresearchError(
            "amend needs at least one field to change; nothing was written")
    # The previous value rides along in the event: an amend is a blind
    # full-value write, so two sessions correcting the same field inside a
    # minute can lose one text silently (H15). The record then carries enough
    # to see the collision and merge, which is what the loss needed.
    previous = {name: getattr(entry, name) for name in changes}
    for name, value in changes.items():
        setattr(entry, name, value)
    entry.history.append(Event(_iso(), "amend", args.session, "set " + ", ".join(
        [f"{name}={changes[name]!r} (was {previous[name]!r})"
         for name in sorted(changes)]
        + [f"{name} += {added[name]!r}" for name in sorted(added)]
        + [f"{name} -= {dropped[name]!r}" for name in sorted(dropped)]
        + ([f"result.mechanisms={entry.result.mechanisms!r} (was {closure_was!r})"]
           if closure_was is not None else []))
        + (f": {args.why}" if closure_was is not None else "")))
    entry.updated = _iso()
    store.save(entry)
    _render_views(config)
    touched = list(dict.fromkeys([*sorted(changes), *sorted(added),
                                  *sorted(dropped)]))
    print(f"{entry.id} amended: {', '.join(touched)}")
    return 0


def cmd_entry_reprice(args):
    """Re-price a filing-time estimate, out of band.

    Filing-time confidence/impact/cost are guesses; a result that settled
    later (an external assignment confirmed past the filing, a sibling
    closure moved a shared mechanism) can make them wrong while the entry
    stays open. The loop's curator phase has this writer; out-of-band
    sessions -- a hand-driven curator pass, a human correction -- had none,
    which left a hand-edit of the YAML as the only path (the exact move H8
    exists to forbid). One writer, both callers.
    """
    config = _load(args)
    store = _store(config)
    entry = store.load(args.id)
    moved = entries_mod.reprice(
        store, entry, machine=config.track_for(entry.id).machine,
        changes={"confidence": args.confidence, "impact": args.impact,
                 "cost": args.cost},
        session=args.session, why=args.why)
    _render_views(config)
    print(f"{entry.id} repriced: {', '.join(moved)}")
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

    Read-only, and safe to run on a schedule: exit 1 means an update is
    available, and anything else failed loudly rather than reporting a clean
    comparison it did not make. Exit 0 means there is nothing to pull -- which
    includes a checkout that has diverged from upstream, and the line printed
    says exactly that instead of claiming it is up to date (sig.golf H15).
    """
    config = _load(args)
    plan = upstream_mod.plan_update(config, ref=args.ref)
    print(plan.summary())
    if plan.behind:
        print("update available")
        if plan.changelog:
            print("\nwhat changed:")
            print(plan.changelog)
        else:
            print("\n(what changed could not be fetched; the update is still "
                  "there)")
        print("\ntake it with: ar harness update")
        return 1
    print(_relation_note(plan))
    return 0


def _relation_note(plan) -> str:
    """What the comparison found, for every case with nothing to pull.

    `same` is the only one of the four that is "up to date". A checkout that
    has diverged, or whose commit upstream has never seen, has nothing to pull
    -- and printing "up to date" for either is the claim the comparison did not
    earn, next to two different commits (sig.golf H15).
    """
    if plan.relation == "same":
        return "up to date"
    what = ("your commit is not in upstream at all -- it was never pushed"
            if plan.relation == "unpushed"
            else "your checkout and upstream have diverged")
    return (f"nothing to pull automatically: {what}. Reconcile by hand "
            f"(rebase, or reinstall from upstream) if you want their fixes.")


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
        print(_relation_note(plan))
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
    data = entry.to_dict()
    if not getattr(args, "history", False):
        # The default is the current state: the append-only history is the bulk
        # of an old entry's JSON, and a coordinator brief already excludes it --
        # so a state dump that inlines it is mostly bytes nobody reads. It is
        # data, never lost -- `--history` opts into the full record.
        data.pop("history", None)
    print(json.dumps(data, indent=2, default=str))
    return 0


def _entry_row(entry) -> dict:
    """The projection `entry list --json` hands an agent: enough to pick work
    without an N+1 of `entry show`. Deliberately not to_dict(): history and
    prose are the bulk of an old entry, and a picker needs none of it."""
    return {
        "id": entry.id, "title": entry.title, "status": entry.status,
        "track": entry.track, "kind": entry.kind, "parent": entry.parent,
        "claim": ({"session": entry.claim.session, "at": entry.claim.at}
                  if entry.claim else None),
        "confidence": entry.confidence, "impact": entry.impact,
        "cost": entry.cost,
        "mechanisms": list(entry.mechanisms),
        "gates": [dataclasses.asdict(g) for g in entry.gates],
        "hardware": entry.hardware,
    }


def cmd_entry_list(args):
    config = _load(args)
    entries = _store(config).all()
    if args.status:
        # A typo must not read as an empty queue: "0 entries read" is the same
        # screen for "nothing is filed" and for "you asked for a status nobody
        # declares", and a session that reads it stops looking (sig.golf H12).
        declared = set()
        for track in config.tracks.values():
            declared |= track.machine.reachable()
        unknown = [s for s in args.status if s not in declared]
        if unknown:
            raise AutoresearchError(
                f"unknown status {unknown[0]!r}; declared: {sorted(declared)}")
        entries = [e for e in entries if e.status in args.status]
    if args.track and args.track not in config.tracks:
        # The same argument as the status filter above, and the same remedy:
        # `entry new --track` and `entry graph --track` both refuse an
        # undeclared track, so the one verb that lists by track was the last
        # place a typo could read as an empty queue (sig.golf H12).
        raise AutoresearchError(
            f"unknown track {args.track!r}; declared: {sorted(config.tracks)}")
    if args.track:
        entries = [e for e in entries if e.track == args.track]
    if getattr(args, "json", False):
        print(json.dumps([_entry_row(e) for e in entries], indent=2))
        return 0
    print(f"{len(entries)} entr{'y' if len(entries) == 1 else 'ies'} read")
    if not args.status and not args.all:
        # The table is what a session reads to pick work, and on a mature
        # record nearly every row is closed: 1,542 rows (~167 KB) around a
        # few dozen open ones. A filter the reader asked for is honoured
        # as given; otherwise closed entries are counted, not listed.
        shown = [e for e in entries
                 if not config.track_for(e.id).machine.status(e.status).terminal]
        if len(shown) < len(entries):
            print(f"  ({len(entries) - len(shown)} terminal not listed -- "
                  "`--all` or `--status STATUS` lists them)")
        entries = shown
    for e in entries:
        held = f" [{e.claim.session}]" if e.claim else ""
        print(f"  {e.id:6} {e.status:12}{held:24} {e.title[:70]}")
    return 0


def cmd_claim(args):
    config = _load(args)
    claims = Claims(_store(config), config, args.session)
    entry = claims.claim(args.id, why=args.why or "", budget=args.budget or "",
                         max_runs=args.max_runs, max_hours=args.max_hours)
    _render_views(config)
    print(f"{entry.id} claimed by {args.session} "
          f"(ceiling: {entry.claim.max_runs} runs / {entry.claim.max_hours} h)")
    # Printed once and stored nowhere but its hash (#33): the token is what
    # proves this claim is yours, under any session name.
    token = claims.issued[entry.id]
    print(f"token {token}  (pass --claim-token, or add it to AR_CLAIM_TOKEN to "
          f"release or update this claim; several tokens are comma-separated)")
    print(f'  export AR_CLAIM_TOKEN="${{AR_CLAIM_TOKEN:+$AR_CLAIM_TOKEN,}}{token}"')
    if not sys.stdout.isatty():
        # The token is unrecoverable once lost, and `ar claim ... | head -1`
        # lost it, stranding the claim until its TTL (starks H8). Piped or
        # captured stdout may be cut short; stderr carries a copy.
        print(f"{entry.id} claim token {token}", file=sys.stderr)
    return 0


def cmd_release(args):
    config = _load(args)
    entry = Claims(_store(config), config, args.session,
                   token=getattr(args, "claim_token", None)).release(args.id, args.why)
    _render_views(config)
    print(f"{entry.id} released back to {entry.status}: {args.why}")
    return 0


def cmd_reap(args):
    config = _load(args)
    claims = Claims(_store(config), config, args.session)
    if args.id:
        entry, former, age = claims.reap(args.id, ttl_hours=args.ttl_hours)
        _render_views(config)
        if former is None:
            print(f"{entry.id} orphan repaired to {entry.status} (no live claim)")
        else:
            print(f"{entry.id} reaped from {former} (silent {age:.1f} h)")
        return 0
    candidates = claims.reapable(ttl_hours=args.ttl_hours)
    print(f"{len(candidates)} reapable record(s)")
    for entry, age in candidates:
        if age is None:
            print(f"  {entry.id:6} {'orphan':24} no live claim")
        else:
            print(f"  {entry.id:6} {entry.claim.session:24} silent {age:.1f} h")
    return 0


def cmd_close(args):
    config = _load(args)
    store = _store(config)
    entry = store.load(args.id)
    track = config.track_for(args.id)

    reopen_reason = getattr(args, "reopen_reason", None)
    if reopen_reason and not args.reopen:
        raise AutoresearchError(
            "--reopen-reason categorises a reopen; pass it with --reopen")

    if args.relabel:
        entry.relabel(args.relabel, who=args.session, why=args.why or "",
                      reopen_condition=args.reopen_condition)
        store.save(entry)
        _render_views(config)
        print(f"{entry.id} closure relabelled to {args.relabel}")
        return 0

    if args.reopen:
        if entry.claim and not track.machine.status(entry.status).terminal:
            # Reopening a live claim hands someone's work back to the queue;
            # only its holder may (#33 review). A closed entry is anyone's
            # to reopen, as before.
            Claims(store, config, args.session,
                   token=getattr(args, "claim_token", None)).require_owner(entry)
        entry.reopen(track.machine, who=args.session, why=args.why or "",
                     reason=reopen_reason)
        entry.claim = None
        store.save(entry)
        _render_views(config)
        print(f"{entry.id} reopened to {entry.status}")
        return 0

    # A verdict on an entry another session holds is the race the claim lock
    # exists to prevent: Q49 was closed by one session while a second held a
    # live claim on it (starks H4). Another holder's claim needs its token, or
    # `ar reap` once its TTL has run out; a closer under the holder's own
    # session name keeps the close it always had.
    claim_token = getattr(args, "claim_token", None)
    if (entry.claim and not track.machine.status(entry.status).terminal
            and (entry.claim.session != args.session or claim_token)):
        Claims(store, config, args.session,
               token=claim_token).require_owner(entry)

    # After the relabel/reopen returns: `--reopen` and `--relabel` carry no
    # destination, so a `superseded` closure rule must not fire on them.
    replacement = None
    superseded_by = getattr(args, "superseded_by", None)
    if superseded_by:
        if args.status != "superseded":
            raise AutoresearchError(
                "--superseded-by belongs to `close <id> superseded`; a "
                f"closure as {args.status or '(none)'!r} names no replacement")
        if superseded_by == args.id:
            raise AutoresearchError("an entry cannot supersede itself")
        # A replacement outside the store -- a hosted or public record -- is
        # named by a scheme-prefixed reference (`https://...`, `hosted:mk_1`),
        # so a mistyped entry id is still refused (mldsa87 H1, #11).
        external = "://" in superseded_by or (
            ":" in superseded_by and not superseded_by.startswith(":"))
        if external:
            replacement = superseded_by
        else:
            try:
                replacement = store.load(superseded_by)
            except Exception as exc:
                raise AutoresearchError(
                    f"cannot retire {args.id} as superseded: no entry "
                    f"{superseded_by!r} (an outside record is named with a "
                    f"scheme, e.g. https://... or hosted:<id>)") from exc
    if args.status == "superseded" and replacement is None:
        raise AutoresearchError(
            "closing as superseded requires --superseded-by: a retirement "
            "that does not name its replacement is how a duplicate outlives "
            "the entry it duplicated (H15)")

    # A closure states what it decided. `summary` is the record's own typed
    # sentence about the verdict: the closed queue row renders it, every brief
    # carries it, and the fabrication net reads its numbers. Without one the
    # row shows a dash -- the same glyph a lost statement would show -- and the
    # net has nothing to check, so a closure that says nothing passes cleanly.
    # Twenty-one of this domain's closures said nothing (sig.golf H18).
    if track.machine.status(args.status).terminal and not (
            args.summary or "").strip():
        raise AutoresearchError(
            f"closing {args.id} as {args.status} requires --summary: a "
            f"closure states what it decided. For a closure that already "
            f"exists, `ar entry amend {args.id} --summary ... --why ...` "
            f"completes it.")
    memo_root = config.paths.root
    result = Result(
        verdict=args.status, memo=args.memo or "", at=_iso(), session=args.session,
        summary=args.summary or "", closure_kind=args.closure,
        reopen_condition=args.reopen_condition or "",
        evidence_class=args.evidence_class,
        # `superseded` is already a disposition (entries.DISPOSITIONS), and a
        # retired entry was not an experiment: ranking and the stagnation
        # counters read disposition, so a retirement left as `experiment`
        # would count as evidence in a sibling tally it never belonged to.
        disposition=("superseded" if args.status == "superseded"
                     else "experiment"))
    # Same replication gate the coordinator applies: a close path that
    # bypassed it would make the loop's guard vacuous (invariant 3 -- a guard
    # that only refuses on one path is not a guard). An official/census
    # evidence class is deliberately measurement-free or externally owned;
    # demanding ledger rows of it would refuse honest work (pvfast H10).
    # A non-terminal close (`blocked`) stores no Result, so the evidence flags
    # used to be built and dropped while the command printed the memo path
    # (qsb H322, #28). The memo must exist all the same, and a closure kind --
    # how far a verdict reaches -- has no verdict here to label.
    terminal = track.machine.status(args.status).terminal
    if not terminal:
        if args.closure:
            raise AutoresearchError(
                f"--closure labels how far a verdict reaches; {args.status!r} "
                f"is not terminal on track {track.id!r}, so there is no "
                f"verdict to label")
        if args.memo and not (memo_root / args.memo).exists():
            raise AutoresearchError(
                f"{args.id}: evidence memo {args.memo!r} does not exist")
    if (args.status == "confirmed"
            and args.evidence_class not in ("official", "census")
            and config.confirm_runs > 1):
        evidence = runs_mod.closure_evidence(
            runs_mod.read_all(config.paths.runs), args.id, config.confirm_runs)
        if evidence:
            raise AutoresearchError("; ".join(evidence))
    if (args.status == "confirmed"
            and args.evidence_class not in ("official", "census")):
        # The count above says enough executions said so; this says they
        # said more than the noise does (#37). A no-op under `none`.
        noisy = runs_mod.noise_problems(
            config, store, runs_mod.read_all(config.paths.runs), args.id)
        if noisy:
            raise AutoresearchError("; ".join(noisy))
    tamper_hunks = None
    if args.status == "confirmed":
        # #39: hunks shaped like tampering, listed for QC and a person to read.
        # Data, not a decision -- the closure proceeds either way.
        from . import tamper as tamper_mod
        tamper_hunks = tamper_mod.scan_tree(
            config, config.paths.root, getattr(args, "diff_base", None) or "HEAD")
    if terminal:
        # Pinned to what its evidence was measured under, so a later move of
        # the evaluator or benchmark can list it as a reopen candidate (#40).
        from . import epochs as epochs_mod
        result.pin = epochs_mod.pin(
            config, runs_mod.read_all(config.paths.runs), args.id,
            source_revision_override=getattr(args, "source_revision", None) or "")
    entry.apply(track.machine, args.status, who=args.session, why=args.why or "",
                result=result,
                memo_exists=lambda m: (memo_root / m).exists())
    if not terminal:
        evidence = {"memo": result.memo, "summary": result.summary,
                    "reopen_condition": result.reopen_condition,
                    "evidence_class": result.evidence_class}
        if any(evidence.values()):
            entry.history.append(Event(
                _iso(), "evidence", args.session,
                " — ".join(v for v in (result.summary, result.memo) if v),
                snapshot={"verdict": args.status, **evidence}))
    # An entry settled outside the assignment that was cancelled on it said
    # nothing of that assignment: the row read `cancelled`, the entry
    # `confirmed`, and nothing tied the two (#17). Name them on the closure.
    try:
        from . import attempts as attempts_mod
        cancelled = [] if not track.machine.status(args.status).terminal else [
                     r["id"] for r in attempts_mod.records(config)
                     if r.get("kind") == "external" and r.get("entry") == entry.id
                     and r.get("status") == "cancelled"]
    except AutoresearchError:
        cancelled = []
    if cancelled:
        entry.history.append(Event(
            _iso(), "external-link", args.session,
            json.dumps({"cancelled_assignments": cancelled,
                        "settled_as": args.status})))
    if isinstance(replacement, str):
        entry.history.append(Event(
            _iso(), "superseded", args.session,
            f"retired as superseded by external record {replacement}"))
    elif replacement is not None:
        # The relation lands on the replacement's own typed field, so the
        # graph, the render and the next agent all see which entry absorbed
        # this one -- and the retired entry's own history says who replaced it.
        if entry.id not in replacement.supersedes:
            replacement.supersedes.append(entry.id)
        replacement.history.append(Event(
            _iso(), "superseded", args.session,
            f"{entry.id} retired as superseded by this entry"))
        replacement.updated = _iso()
        store.save(replacement)
    if tamper_hunks:
        from . import tamper as tamper_mod
        entry.history.append(Event(_iso(), "tamper-scan", args.session,
                                   tamper_mod.event_detail(tamper_hunks)))
    entry.claim = None
    store.save(entry)
    _render_views(config)
    print(f"{entry.id} closed {args.status}"
          + (f" ({args.closure})" if args.closure else "")
          + (f" — superseded by {getattr(replacement, 'id', replacement)}"
             if replacement else "")
          + (f" — evidence: {result.memo}" if terminal or result.memo
             else ""))
    if tamper_hunks:
        print(f"tamper-shaped hunks since {getattr(args, 'diff_base', None) or 'HEAD'}"
              " (for QC and a person to read; they decide nothing):")
        for hunk in tamper_hunks:
            print(f"  {hunk}")
    if args.status == "refuted" and args.closure == "mechanism":
        # A mechanism closure removes every open entry sharing the mechanism
        # from `ar rank`. Said nowhere, curators found it by hand five times in
        # a week and relabelled to `cell` (#13) -- so name them as it happens.
        dead = set(entry.closure_mechanisms())
        hidden = [e.id for e in store.all()
                  if e.id != entry.id
                  and not config.track_for(e.id).machine.status(e.status).terminal
                  and dead & set(e.mechanisms) and entry.result_applies_to(e)]
        if hidden:
            print(f"  now excluded from `ar rank` by this mechanism closure: "
                  f"{', '.join(hidden)} -- if the refutation does not reach "
                  f"them, `ar close {entry.id} --relabel cell --why ... "
                  f"--reopen-condition ...`")
    if not track.machine.status(entry.status).terminal:
        # Which statuses are terminal is per-track machine config (a defect
        # track closes `fixed`, not `confirmed`), and the first real domain
        # filed a harness defect with the research-track verb, watched the
        # entry bounce back to the queue, and only then learned the rule.
        # Only the initial status is ranked, so a `blocked` entry does not
        # return to the queue: it waits for its reopen edge (#28).
        if entry.status == track.machine.initial:
            fate = "stays actionable and returns to the queue"
        elif any(t.source == entry.status and t.dest == track.machine.initial
                 for t in track.machine.transitions):
            fate = (f"stays {entry.status}, off the queue, until "
                    f"`ar close {entry.id} --reopen`")
        else:
            fate = f"stays {entry.status}, off the queue"
        print(f"! {args.status!r} is not terminal on track {track.id!r}: "
              f"{entry.id} {fate} "
              f"(terminal here: {', '.join(sorted(track.machine.terminal_names))})")
    hint = skills_mod.skill_candidate_hint(entry, config)
    if hint:
        print(f"! {hint}")
    return 0


def cmd_measure(args):
    """Run the domain's measurement command and record the result.

    The domain decides how to measure; core decides where the row lands and
    what makes it valid. That split is the answer to a whole class of results
    escaping the ledger (H142, H64, H39).
    """
    config = _load(args)
    # Dispatch checks this through preflight; a direct invocation gets the
    # same gate. A human-only measurement command must never reach
    # subprocess.run just because a caller skipped the coordinator.
    config.policy.check_command(config.command("measure"))
    if args.entry:
        # A row on a closed entry lands on the board as that entry's evidence
        # -- an integration run on an already-confirmed entry printed as best,
        # GOAL MET, after its `ar claim` had been refused (mldsa87, #9).
        try:
            entry = _store(config).load(args.entry)
        except AutoresearchError:
            entry = None             # an unfiled id is not this check's call
        if entry is not None and config.track_for(entry.id).machine.status(
                entry.status).terminal:
            raise AutoresearchError(
                f"{entry.id} is closed ({entry.status}); a measurement filed "
                f"against it reads as its evidence. File the run under an "
                f"open entry, or reopen {entry.id} first")
    # #36: the command, the policy above and the evaluator all come from this
    # domain root. A candidate (a worker's slot) supplies only the tree being
    # measured -- its own domain.toml and its copy of the evaluator are files
    # the worker can edit, so they are never what scores it.
    from . import evaluator as evaluator_mod
    candidate = (evaluator_mod.candidate(config, args.candidate)
                 if getattr(args, "candidate", None) else None)
    _refuse_evaluator_drift(config, candidate, args.entry, "before")
    evaluator_sha = evaluator_mod.identity(config)
    command = evaluator_mod.command(config) + list(args.rest)
    env_note = f"entry={args.entry} " if args.entry else ""
    clock = time.monotonic()
    proc = subprocess.run(command, cwd=candidate or config.paths.root,
                          capture_output=True, text=True,
                          env={**_env(), "AR_SESSION": args.session,
                               **({"AR_ENTRY": args.entry} if args.entry else {}),
                               **({"AR_CANDIDATE": str(candidate)}
                                  if candidate else {})})
    wall_seconds = time.monotonic() - clock
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
    # Read again after the run: an evaluator edited while it ran scored this
    # row with something other than what was hashed.
    _refuse_evaluator_drift(config, candidate, args.entry, "during")
    if evaluator_mod.identity(config) != evaluator_sha:
        raise AutoresearchError(
            "the evaluator changed while it ran; no row recorded")
    # Core's numbers, not the script's: a script-written `measure_sha` hashes
    # whatever the script says it is, and a patched `time.time` inside the
    # candidate moves `started`/`finished` but not this clock.
    record.provenance["evaluator_sha"] = evaluator_sha
    if isinstance(record.cost, dict):
        record.cost["wall_seconds"] = round(wall_seconds, 3)
    # What the row was measured under (#40): the benchmark revision keys its
    # epoch with the evaluator hash; the source revision names the tree.
    from . import epochs as epochs_mod
    from .skills import core_version
    revision, dirty = epochs_mod.source_revision(
        config, candidate or config.paths.root)
    record.provenance.update(
        benchmark_revision=epochs_mod.benchmark_revision(config),
        source_revision=revision, core=core_version())
    if dirty is not None:
        record.provenance["source_dirty"] = dirty
    # Who measured it, in terms the measurer cannot choose (#43): the slot a
    # candidate tree was handed out as, or the claim the caller proved it
    # holds. `--session` is a free string, and counting it let one agent be
    # its own second session. Stamped before the receipt digest, so a slot
    # cannot edit it.
    record.provenance["measured_by"] = _measured_by(config, args, candidate)
    # A throughput figure that does not name its machine is not a measurement,
    # and two rows from different machines are not comparable without saying so.
    # The domain does not have to remember to record this; core does it.
    record.provenance.setdefault("host", hw.detect().to_dict())
    if args.entry:
        record.entry = args.entry
    record.validate(config.goal)
    runs_dir = (candidate / config.paths.runs.relative_to(config.paths.root)
                if candidate else config.paths.runs)
    if candidate:
        # The proof harvest asks for, kept where the slot cannot write it.
        # Taken before the row is appended: a run id is receipted once, so a
        # script that reuses one is refused here, not at harvest.
        try:
            evaluator_mod.write_receipt(config.paths.receipts, record, candidate)
        except FileExistsError:
            raise AutoresearchError(
                f"run id {record.id!r} was already recorded; a measure script "
                "must mint a fresh id per run") from None
    path = runs_mod.append(runs_dir / f"{args.session}.jsonl", record)
    value = (config.goal.objective_value(record.metrics)
             if record.status == "ok" else None)
    # H2: `,.0f` rounded a 0.701 objective to `1` -- a display 1000x off that
    # masks pass/fail against the target. The house format is render._fmt.
    shown = (path.relative_to(config.paths.root)
             if path.is_relative_to(config.paths.root) else path)
    print(f"{record.id} {record.status}"
          + (f"  objective={render._fmt(value)}" if value is not None else "")
          + f"  -> {shown}")
    if record.provenance["measured_by"] == "unclaimed":
        print("  (unclaimed: no slot and no proven claim, so this row never "
              "counts as a second, independent measurement; measure with "
              "--candidate, or --entry with the claim's --claim-token)")
    return 0


def _measured_by(config, args, candidate) -> str:
    if candidate is not None:
        return f"slot:{candidate.name}"
    if args.entry:
        try:
            entry = _store(config).load(args.entry)
        except AutoresearchError:
            entry = None
        if entry is not None and entry.claim:
            try:
                Claims(_store(config), config, args.session,
                       token=getattr(args, "claim_token", None)).require_owner(entry)
            except AutoresearchError:
                return "unclaimed"
            return f"claim:{entry.claim.session}@{entry.claim.at}"
    return "unclaimed"


def _refuse_evaluator_drift(config, candidate, entry_id, when):
    """A candidate whose copy of the evaluator differs from the primary's is
    not measured (#36) -- unless the entry is harness debt, whose work may be
    the evaluator itself. The primary's copy is what runs either way."""
    if candidate is None:
        return
    from . import evaluator as evaluator_mod
    drifted = evaluator_mod.drift(config, candidate)
    if drifted and not evaluator_mod.may_edit(config, entry_id):
        raise AutoresearchError(
            f"the candidate's evaluator differs from the primary's "
            f"({when} the run): {', '.join(drifted)}. A worker is not scored "
            "by an evaluator it edited; restore those files in the slot, or "
            "file the change as harness debt")


def _score_row(score) -> dict:
    """One row of `rank --json`: the same facts the prose table shows."""
    return {"id": score.entry_id, "score": score.score, "terms": score.terms,
            "title": score.title, "novel": score.novel,
            "excluded": score.excluded}


def cmd_rank(args):
    config = _load(args)
    entries = _store(config).all()
    domain = budget_mod.domain_budget(
        config, spent_runs=budget_mod.total_runs(config))
    remaining = domain["runs"].remaining()
    risk = config.risk if args.risk is None else args.risk
    ranking = rank_mod.rank(entries, config, risk=risk,
                            budget_ok=lambda e: e.cost <= remaining)
    if "score" in config.commands:
        # The human-correction surface shows what the loop would dispatch,
        # which is the domain's prices when the seam is installed.
        ranking = scoring.seam_ranking(config, ranking, entries, risk)
        if not getattr(args, "json", False):
            print(f"priced by domain seam: {config.commands['score']}\n")
    # Shortlisted first: `shortlist` is where the risk dial spends its slots,
    # and the table is the ranking a human corrects. A novel pick may sit low
    # in it by design, and one shown unmarked reads as the formula having
    # gone wrong.
    shortlist = ranking.shortlist(args.top) if args.top else []
    if getattr(args, "json", False):
        by_filter: dict[str, int] = {}
        for card in ranking.excluded:
            by_filter[card.excluded] = by_filter.get(card.excluded, 0) + 1
        # An unlimited meter's remaining is float infinity, which is not a
        # number any JSON consumer can parse -- it degrades to null.
        print(json.dumps({
            "ranking": [_score_row(s) for s in ranking.scored]
                       + [_score_row(s) for s in ranking.excluded],
            "shortlist": [_score_row(s) for s in shortlist],
            "excluded_summary": {"count": len(ranking.excluded),
                                 "by_filter": by_filter},
            "risk": ranking.risk,
            "remaining": remaining if math.isfinite(remaining) else None,
        }, indent=2))
        return 0
    print(ranking.explain(full=args.all))
    if args.top:
        print(f"\nshortlist (top {args.top}):")
        for s in shortlist:
            print(f"  {s.entry_id}  {s.title}"
                  + ("  [novel]" if s.novel else "  [branch]"))
    return 0


def _meter_json(meter) -> dict:
    """One meter for `budget --json`. An unlimited meter's remaining is float
    infinity, which no strict JSON consumer can parse -- it degrades to null,
    like an undeclared ceiling does."""
    remaining = meter.remaining()
    return {"spent": meter.spent,
            "remaining": remaining if math.isfinite(remaining) else None,
            "ceiling": meter.ceiling}


def cmd_budget(args):
    config = _load(args)
    store = _store(config)
    entries = store.all()
    all_runs = runs_mod.read_all(config.paths.runs)
    # Use the same durable reports and reservations as execution admission.
    recorded = budget_mod.recorded_usage(config)
    domain = budget_mod.domain_budget(
        config, spent_runs=budget_mod.total_runs(config),
        spent_money=recorded["money"],
        spent_gpu_hours=recorded["gpu_hours"])
    iteration = budget_mod.iteration_budget(config)
    meters = {name: _meter_json(meter) for name, meter in domain.meters.items()}
    # The iteration meters share names with the domain's (`runs` on both), so
    # they are namespaced rather than merged -- a meter silently overwriting
    # another is the H8 shape in JSON form.
    meters.update({"iteration." + name: _meter_json(meter)
                   for name, meter in iteration.meters.items()})
    claims = Claims(store, config, session=args.session)
    held = [e for e in entries if e.claim
            and not config.track_for(e.id).machine.status(e.status).terminal]
    claim_rows = []
    for entry in held:
        spent = budget_mod.claim_runs(config, entry)
        over = claims.overrun(entry.id, runs_spent=spent)
        claim_rows.append({
            "id": entry.id, "session": entry.claim.session,
            "runs_spent": spent, "max_runs": entry.claim.max_runs,
            "max_hours": entry.claim.max_hours,
            "overrun": (f"{over[0]} {over[1]:g} > {over[2]:g}"
                        if over else None),
        })

    try:
        target = _probe_target(config)
    except AutoresearchError:
        target = None
    from . import epochs as epochs_mod
    epoch_runs = epochs_mod.current_rows(config, all_runs)
    best, excluded = runs_mod.frontier(store, epoch_runs, config.goal)
    # The same yield series the loop feeds its stop decision; without it the
    # yield floor could never fire here, so budget and loop disagreed (#19).
    try:
        from .driver.loop import read_history
        yields = [i.confirmed for i in read_history(config.paths.iterations)
                  if i.kind == "iteration"]
    except AutoresearchError:
        yields = None            # a malformed record is validate's to report
    # Read, never consulted: a budget view that peeked at the holdout would
    # spend it on every glance (#38).
    from . import holdout as holdout_mod
    holdout_reason, holdout_passed = holdout_mod.stop_reason(config, best, target)
    decision = budget_mod.should_stop(
        config,
        measurements=best[1].metrics if best else None,
        target=target,
        verdicts_per_iteration=yields,
        budgets=[domain],
        unmet_required_gates=budget_mod.unmet_required_gates(
            config.goal, store, best),
        independent_confirmation=holdout_passed or budget_mod.independent_confirmation(
            config.goal, best, runs_mod.confirmation_rows(store, epoch_runs, config.goal),
            target),
        holdout=holdout_reason)

    if getattr(args, "json", False):
        # Non-finite objective/target values serialise as bare Infinity/NaN,
        # which spec-compliant parsers reject (the brief builder fights the
        # same failure at loop.py). "No number" rides as null, same contract.
        def finite(v):
            return v if isinstance(v, (int, float)) and math.isfinite(v) else None
        print(json.dumps({
            "meters": meters,
            "claims": claim_rows,
            "stop": {"reason": decision.reason, "detail": decision.detail},
            "target": finite(target),
            "best": finite(best[0]) if best else None,
            # Named, never dropped silently: a wiring check's number is real,
            # and a reader who cannot see what the frontier ignored cannot tell
            # a flat frontier from a moved one (sig.golf H4).
            "excluded_scaffold": (
                {"id": excluded[1].id, "entry": excluded[1].entry,
                 "objective": finite(excluded[0]), "reason": excluded[2]}
                if excluded else None),
            **({"holdout": holdout_mod.summary(config, best, target)}
               if holdout_mod.declared(config) else {}),
        }, indent=2))
        return 0

    print(domain.report())
    print()
    print(iteration.report())
    print(f"\n{len(held)} live claim(s)")
    for entry, row in zip(held, claim_rows, strict=True):
        claim = budget_mod.claim_budget(entry, config)
        claim["runs"].spent = row["runs_spent"]
        print(f"  {entry.id}  {entry.claim.session}")
        print("    " + claim.report().replace("\n", "\n    "))
        if row["overrun"]:
            print(f"    OVERRUN: {row['overrun']}")

    if holdout_mod.declared(config):
        print("\n" + _holdout_line(holdout_mod.summary(config, best, target)))
    print(f"\nstop decision: {decision}")
    # The last line repeats the decision above it in the numbers a reader is
    # looking for, so it is derived from that decision rather than from a
    # second, looser `is_met`: a budget that prints GOAL MET beside a
    # `running` stop decision is the sig.golf H2 record in one screen.
    # `target` is tested for truth, not for None: a zero target cannot be
    # normalised against, and `is_met` refuses it rather than dividing by it.
    if best is not None and target:
        met = decision.reason == budget_mod.MET
        print(f"best objective {render._fmt(best[0])} vs target {render._fmt(target)}"
              + ("  ** GOAL MET **" if met else ""))
    if excluded is not None:
        # Named, never dropped silently (sig.golf H4): a wiring check's number
        # is real, and a reader who cannot see what the frontier ignored cannot
        # tell a flat frontier from a moved one.
        value, record, reason = excluded
        print(f"excluded  {record.id} on {record.entry or '(no entry)'}: "
              f"objective {render._fmt(value)}: {reason}")
    return 0


def cmd_usage(args):
    """Out-of-band spend: record it, list it, price what was unknown."""
    config = _load(args)
    if args.usage_cmd == "record":
        cost = None if args.unknown else args.cost
        row = usage_mod.append(config, args.session, args.tool, args.kind,
                               cost, args.note)
        print(json.dumps(row, indent=2))
        if cost is None:
            print("unpriced: this spend is unknown, not free; a finite money "
                  "ceiling refuses further spend until `ar usage reconcile`")
        return 0
    if args.usage_cmd == "list":
        money, unpriced = usage_mod.effective(config)
        rows = usage_mod.rows(config)
        print(json.dumps({"rows": rows, "money": money,
                          "unpriced": [r["lineno"] for r in unpriced]}, indent=2))
        return 0
    row = usage_mod.reconcile(config, args.lineno, args.cost, args.session,
                              args.note)
    print(json.dumps(row, indent=2))
    return 0


def _print_assignments(config, iteration):
    """The native-handoff manifest: what the surrounding harness's subagents
    are to pick up, one line each, with the receipt verb for each."""
    from .attempts import load as load_attempt
    rows = [load_attempt(config, attempt_id)
            for attempt_id in iteration.attempt_ids]
    if not rows:
        return
    print("\nassignments for native agents (work happens outside this "
          "process; settle each with `ar --session <session> external complete "
          "<id> --report report.json`, then run `ar loop` again):")
    for row in rows:
        ceiling = row.get("ceiling") or {}
        print(f"  {row['id']}  entry {row['entry']}  "
              f"runs<={row.get('reserved_runs')}  "
              f"expires {ceiling.get('expires', '?')}  "
              f"workspace {row.get('workspace', '?')}\n"
              f"    brief: ar external show {row['id']}")


def cmd_loop(args):
    """Run the coordinator. The unattended entry point."""
    from .driver.brain import build_brain
    from .driver.loop import Coordinator

    config = _load(args)
    brain = build_brain(config, max_budget_usd=args.max_usd,
                        allow_paid=getattr(args, "allow_paid_brain", None))

    coordinator = Coordinator(config, brain, session=args.session,
                              probe_target=_make_probe(config))
    print(f"{config.name}: goal {config.goal.id} — {config.goal.objective} "
          f"({config.goal.direction})")
    history = coordinator.run(args.iterations, on_iteration=lambda it: print(it.report()))
    last = history[-1] if history else None
    print(f"\n{len(history)} iteration(s); "
          f"stopped: {last.stop if last else 'no iterations run'}"
          f"{(' — ' + last.stop_detail) if last and last.stop_detail else ''}")
    if last is not None and last.stop == "native-handoff":
        _print_assignments(config, last)
    spent = [i.cost_usd for i in history]
    print("cost: unknown (unmetered backend usage)" if None in spent
          else f"cost: ${sum(spent):.2f}")
    return 0


def cmd_migrate(args):
    """Convert an existing prose corpus into entry records. One way, once."""
    from .migrate import migrate as migrate_prose

    config = _load(args)
    if args.track is not None and args.track not in config.tracks:
        raise AutoresearchError(
            f"track {args.track!r} is not declared by this domain; "
            f"declared: {sorted(config.tracks)}")
    store = _store(config)
    total, problems = 0, []
    pending = {}
    for track in config.tracks.values():
        if args.track and track.id != args.track:
            continue
        source = pathlib.Path(args.source or config.paths.root) / (
            args.view or track.migrate_from or track.view)
        if not source.exists():
            if args.track:
                raise AutoresearchError(
                    f"track {track.id!r} was requested, but no source document "
                    f"exists at {source}")
            print(f"{track.id}: no source document at {source}, skipping "
                  "(set `migrate_from` on the track to name it)")
            continue
        try:
            result = migrate_prose(
                source, track=track.id,
                claims_root=config.paths.claims if config.paths.claims.exists() else None,
                root=config.paths.root,
                terminal=tuple(track.machine.terminal_names),
                machine=track.machine)
        except ValueError as exc:
            # A document that is not a prose queue is a wrong input, not a
            # crash: the common case is a domain whose track view is the
            # GENERATED markdown of records that already exist (sig.golf H11).
            # The parser's own diagnostic is kept -- it explains the format --
            # and the remedy joins it.
            reason = str(exc).rstrip()
            reason = reason if reason.endswith(".") else reason + "."
            raise AutoresearchError(
                f"{source} is not a prose queue: {reason} Name the document "
                f"to convert with `ar migrate --source <file>`, or set "
                f"`migrate_from` on the {track.id!r} track to name it."
            ) from exc
        print(f"\n=== {track.id} ({source.name}) ===")
        print(result.report())
        problems += [d.line() for d in result.disagreements]
        problems += result.missing_evidence
        for entry in result.entries:
            if store.exists(entry.id) or entry.id in pending:
                raise AutoresearchError(
                    f"migration would overwrite entry {entry.id!r}; "
                    "no entries written. Resolve duplicate sources or existing records first.")
            pending[entry.id] = entry
        total += len(result.entries)
    if not args.dry_run:
        for entry in pending.values():
            store.save(entry)
        # Every verb that saves entry records re-renders the views itself:
        # a migrated corpus whose queue view is born stale fails the next
        # validate for a reason the verb could have prevented.
        _render_views(config)
    print(f"\n{total} entries "
          + ("would be written (dry run)" if args.dry_run
             else f"written to {config.paths.entries}"))
    if problems:
        print(f"{len(problems)} item(s) need a human decision; nothing was "
              "reconciled silently.")
    return 0


def cmd_validate(args):
    config = _load(args)
    problems = list(config.check())
    # Read the merge residue before the entry store parses, and stop here. An
    # unfinished merge leaves no unmerged path for git to report, and the only
    # symptom downstream is a scanner error naming the wrong file (H83);
    # naming the cause, in every file at once, is the whole point.
    residue = merge_mod.residue_problems(config)
    if residue:
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "problems": residue, "skipped_runs": 0},
                             indent=2))
        else:
            print(f"\n{len(residue)} problem(s):")
            for p in residue:
                print(f"  - {p}")
        return 1
    store = _store(config)
    entries, read_problems = store.read_all()
    problems += read_problems
    problems += [f"duplicate entry id: {d}" for d in store.duplicates()]
    from . import kits as kits_mod
    problems += kits_mod.problems(config)
    # A stale generated view fails validate (invariant 1) unless the domain
    # has said its tree is multi-session and takes it as a warning (#24).
    stale_views = render.check_views(config, entries)
    if config.stale_views == "warning":
        view_warnings = [f"{p} (stale_views = \"warning\")" for p in stale_views]
    else:
        view_warnings = []
        problems += stale_views
    found, skill_problems = skills_mod.read_all(config)
    problems += skill_problems
    problems += skills_mod.check(config, found, entries, skills_mod.best_measurements(config))
    for entry in entries:
        try:
            track = config.track_for(entry.id)
        except AutoresearchError as exc:
            # One mis-prefixed file stopped validate for every entry (#18).
            problems.append(f"{entry.id}: {exc}")
            continue
        machine = track.machine
        if entry.status not in machine.statuses:
            problems.append(
                f"{entry.id}: status {entry.status!r} is not declared by track "
                f"{track.id!r}")
            continue
        status = machine.status(entry.status)
        problems += claim_state_problems(entry, machine)
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
    warnings = (view_warnings + validate_mod.inbox_warnings(config)
                + validate_mod.core_warnings(config)
                + validate_mod.closure_warnings(config)
                + validate_mod.output_warnings(config)
                + validate_mod.measurement_warnings(config)
                + validate_mod.revision_warnings(config)
                + validate_mod.epoch_warnings(config)
                + validate_mod.git_warnings(config)
                + validate_mod.duplicate_warnings(config)
                + validate_mod.holdout_warnings(config)
                + validate_mod.evaluator_warnings(config)
                + validate_mod.kit_warnings(config)
                + validate_mod.record_floor_warnings(config)
                + validate_mod.lineage_warnings(config)
                + validate_mod.citation_warnings(config))

    if getattr(args, "json", False):
        # Only the document: warnings and the read-counts summary are prose
        # channels, and a --json consumer parses stdout, not around it.
        # `skipped_runs` rides in the document because a domain of nothing but
        # foreign rows must not read as a clean board that read everything.
        print(json.dumps({"ok": not problems, "problems": problems,
                          "skipped_runs": skipped_runs}, indent=2))
        return 1 if problems else 0

    for w in warnings:
        print(f"! {w}")

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
    pending = skills_mod.undistilled(config, found, entries)
    if getattr(args, "json", False):
        # The document, because "is the debt map current?" is a question an
        # agent asks before it writes, and answering it must not mean parsing
        # the prose below (sig.golf H24).
        print(json.dumps({
            "skills": [{"name": s.name, "lines": s.lines, "stale": stale.get(s.name, []),
                        "cites": s.cites, "description": s.description}
                       for s in sorted(found, key=lambda s: s.name)],
            "undistilled": [{"track": e["track"], "id": e["id"],
                             "status": e["status"]} for e in pending],
            "body_lines": body_lines, "index_lines": index_lines,
        }, indent=2))
        return 0
    for skill in sorted(found, key=lambda s: s.name):
        mark = "STALE" if skill.name in stale else "ok"
        print(f"{skill.name:<32} {skill.lines:>4}L  {mark:<5} "
              f"cites {', '.join(skill.cites) or '(none)'}")
        print(f"    {textwrap.shorten(skill.description, 96)}")
        for reason in stale.get(skill.name, []):
            print(f"    stale: {reason}")
    print(f"\n{len(found)} skill(s); {len(stale)} stale; "
          f"{len(pending)} terminal entr(ies) cited by none")
    # Per track, because the flat number cannot be read: two uncited harness
    # entries say the debt map has drifted, seventeen uncited research closures
    # say nothing (sig.golf H24).
    for track in sorted({e["track"] for e in pending}):
        ids = [e["id"] for e in pending if e["track"] == track]
        shown = ", ".join(ids[:12]) + (f", +{len(ids) - 12} more" if len(ids) > 12 else "")
        print(f"  {track:<10} {len(ids):>4}: {shown}")
    if found:
        # The ratio is the design: a brief carries the descriptions, not the
        # bodies. Printing it is what keeps that an observation and not a claim.
        print(f"{body_lines} body lines held, {index_lines} index line(s) "
              f"carried into each brief")
    # An informational listing exits 0 even when it reports stale skills: the
    # staleness is the content, not a failure of the command, and a shell that
    # treats non-zero as fatal would stop on a perfectly fine listing.
    # `ar skill check` keeps its non-zero exit for real refusals.
    return 0


def cmd_skill_find(args):
    """Case-insensitive AND-match over skill name and description.

    Routing should not cost a full-index read: an agent with a term in hand
    gets one line per hit -- name, description, path -- against the same
    in-memory read every other skill verb uses. No INDEX.json cache file sits
    under this: `read_all`'s stat-signature cache already makes re-reading the
    directory cheap, and a second copy of the index would be a staleness class
    of bug in exchange for nothing.
    """
    config = _load(args)
    found = _skills(config)
    terms = [t.lower() for t in args.terms]
    hits = [s for s in found
            if all(t in s.name.lower() or t in s.description.lower()
                   for t in terms)]
    for skill in sorted(hits, key=lambda s: s.name):
        print(skill.name)
        print(f"    {textwrap.shorten(skill.description, 96)}")
        print("    " + (str(skill.path.relative_to(config.paths.root))
                        if skill.path else "(path unknown)"))
    return 0


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


def cmd_skill_link(args):
    config = _load(args)
    for path, state in skills_mod.link(config):
        print(f"{path.relative_to(config.paths.root)} -> "
              f"{config.skills.dir}: {state}")
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
    brain = build_brain(config, max_budget_usd=args.max_usd,
                        allow_paid=getattr(args, "allow_paid_brain", None))
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
    print("cost: unknown (unmetered backend usage)" if it.cost_usd is None
          else f"cost: ${it.cost_usd:.2f}  (recorded as iteration {it.n:04d}, "
               "out-of-band)")
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
    brain = build_brain(config, max_budget_usd=args.max_usd,
                        allow_paid=getattr(args, "allow_paid_brain", None))
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
    print("cost: unknown (unmetered backend usage)" if it.cost_usd is None
          else f"cost: ${it.cost_usd:.2f}  (recorded as iteration {it.n:04d}, "
               "out-of-band)")
    return 0


def cmd_policy(args):
    config = _load(args)
    print(config.policy.describe())
    failures = config.policy.selftest()
    print(f"\nselftest: {len(failures) or 'every rule refuses something'}")
    for f in failures:
        print(f"  - {f}")
    return 1 if failures else 0


def cmd_workspace(args):
    from .workspaces import Pool

    config = _load(args)
    pool = Pool(config, args.session)
    if args.workspace_action == "inspect":
        result = pool.inspect(args.name)
    else:
        if args.session in {"cli", "unknown", ""}:
            raise AutoresearchError("workspace recovery requires an explicit --session")
        result = pool.recover(args.name, holder=args.holder, token=args.token,
                              confirm_inactive=args.confirm_inactive)
    print(json.dumps(result, indent=2))
    return 0


def cmd_session(args):
    from . import session as session_mod

    config = _load(args)
    if args.session_action == "create":
        result = session_mod.create(config, args.name)
    elif args.session_action == "destroy":
        result = session_mod.destroy(config, args.name, harvest=args.harvest,
                                     accept_drift=args.accept_drift,
                                     discard_unreceipted=getattr(
                                         args, "discard_unreceipted", False))
    else:
        result = session_mod.prune(config)
        if getattr(args, "destroy", False):
            eligible = [row for row in result if row["eligible"]]
            if not eligible:
                print("no eligible sessions to destroy")
                return 0
            for row in eligible:
                outcome = session_mod.destroy(config, row["name"])
                print(json.dumps(outcome, indent=2))
            return 0
    print(json.dumps(result, indent=2))
    return 0


def _iso():
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def _env():
    import os
    return dict(os.environ)


# -- parser ---------------------------------------------------------------

def build_parser(plugins_spec: tuple[str, ...] = (), root=None, config=None) -> argparse.ArgumentParser:
    import os
    ap = argparse.ArgumentParser(prog="ar", description=__doc__.splitlines()[0])
    ap.add_argument("--domain", type=pathlib.Path,
                    help="domain root (default: nearest enclosing domain.toml)")
    # AR_SESSION is the documented alternative to --session (attempts.py's
    # refusal names it); a default that ignored it recorded an exporting
    # session's claims as `cli` (#30, QSB H328).
    ap.add_argument("--session", default=os.environ.get("AR_SESSION", "cli"),
                    help="who is acting; recorded on every state change "
                         "(default: $AR_SESSION, else 'cli')")
    ap.add_argument("--claim-token", dest="claim_token",
                    default=os.environ.get("AR_CLAIM_TOKEN"),
                    help="the token `ar claim` printed; proves ownership of "
                         "that claim under any session name (default: "
                         "$AR_CLAIM_TOKEN)")
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

    boardp = sub.add_parser("board", help="one screen: goal, queues, claims, measurements")
    boardp.add_argument("--epoch", help="read the frontier of this epoch (a number "
                                        "from `ar epochs`, or `unpinned`) "
                                        "instead of the current one")
    boardp.set_defaults(func=cmd_board)
    holdoutp = sub.add_parser("holdout", help="the held-out check a win must pass")
    hsub = holdoutp.add_subparsers(dest="holdout_cmd", required=True)
    hsub.add_parser("check", help="consult the holdout on the current best "
                                  "config (once per config per epoch)"
                    ).set_defaults(func=cmd_holdout_check)
    promotep = sub.add_parser(
        "promote", help="release a suspect row onto the frontier (a person's "
                        "call; list it under [[policy.human_only]])")
    promotep.add_argument("run")
    promotep.add_argument("--why")
    promotep.set_defaults(func=cmd_promote)
    sub.add_parser("epochs", help="the ledger's measurement epochs, and closures "
                                  "pinned to a superseded one"
                   ).set_defaults(func=cmd_epochs)
    renderp = sub.add_parser("render", help="write the generated queue views")
    renderp.add_argument("--install-merge-driver", action="store_true",
                         help="make merges keep the current side of every "
                              "generated view instead of conflicting on it (#24)")
    renderp.set_defaults(func=cmd_render)
    validatep = sub.add_parser("validate", help="check records, views, runs and policy")
    validatep.add_argument("--json", action="store_true",
                           help="print {ok, problems} as JSON and nothing else")
    validatep.set_defaults(func=cmd_validate)
    sub.add_parser("policy", help="show never-rules and prove each refuses something"
                   ).set_defaults(func=cmd_policy)

    rankp = sub.add_parser("rank", help="score the queue and explain the numbers")
    rankp.add_argument("--top", type=int, default=3,
                       help="size of the shortlist printed last (default 3)")
    rankp.add_argument("--all", action="store_true",
                       help="list every terminal exclusion and every calibration "
                            "row; by default closed entries are counted and "
                            "calibration shows only the candidates' tags")
    rankp.add_argument("--json", action="store_true",
                       help="print the ranking, shortlist and exclusions as JSON")
    rankp.add_argument("--risk", type=float, default=None, metavar="R",
                       help="share of the shortlist aimed at novel branches "
                            "(entries with no parent); overrides the domain's "
                            f"coordinator.risk (core default {rank_mod.RISK})")
    rankp.set_defaults(func=cmd_rank)

    bud = sub.add_parser("budget", help="every meter, and the stop decision")
    bud.add_argument("--json", action="store_true",
                     help="print meters, claims and the stop decision as JSON")
    bud.set_defaults(func=cmd_budget)

    mig = sub.add_parser("migrate", help="convert a prose corpus into records (one way)")
    mig.add_argument("--source", help="root the documents live under")
    mig.add_argument("--track", help="migrate only this track")
    mig.add_argument("--view", help="one document, overriding the track's migrate_from")
    mig.add_argument("--dry-run", action="store_true", dest="dry_run")
    mig.set_defaults(func=cmd_migrate)

    loop = sub.add_parser("loop", help="run the coordinator until it stops")
    loop.add_argument("--iterations", type=int, default=1)
    loop.add_argument("--max-usd", type=float, dest="max_usd",
                      help="hard ceiling on model spend; defaults to policy.spend_ceiling")
    loop.add_argument("--allow-paid-brain", action="store_true", dest="allow_paid_brain",
                      help="authorize the built-in TypeSafe brain to spend API money "
                           "for this run (otherwise domain.toml must set [brain] authorize_spend)")
    loop.set_defaults(func=cmd_loop)

    research = sub.add_parser(
        "research", help="spin up targeted research agents; "
                         "their ideas land in the record as entries")
    research.add_argument("question", nargs="+",
                          help="the targeted question the scouts answer")
    research.add_argument("--count", type=int, default=1,
                          help="how many scouts to run in parallel (default 1)")
    research.add_argument("--max-usd", type=float, dest="max_usd",
                          help="hard ceiling on model spend; defaults to policy.spend_ceiling")
    research.add_argument("--allow-paid-brain", action="store_true", dest="allow_paid_brain",
                          help="authorize the built-in TypeSafe brain to spend API money "
                               "for this run (otherwise domain.toml must set [brain] authorize_spend)")
    research.set_defaults(func=cmd_research)

    entry = sub.add_parser("entry", help="file, show and list entries")
    esub = entry.add_subparsers(dest="entry_cmd", required=True)
    new = esub.add_parser("new", help="file a new entry")
    new.add_argument("title")
    # No default: a filing that forgot --track used to land on the research
    # track, where a harness defect reads to the next agent as a lead on the
    # score (H15). The track is a decision; make it one at the filing.
    new.add_argument("--track", required=True,
                     help="which queue this entry belongs to; no default, "
                          "because a forgotten track misfiles scaffolding as "
                          "research (H15)")
    new.add_argument("--id", help="explicit id (default: the track's next number)")
    new.add_argument("--hypothesis", help="the claim that could be wrong")
    new.add_argument("--prediction", help="what the measurement should show if "
                                          "the hypothesis holds")
    new.add_argument("--bar", help="pre-registered confirm/refute criteria")
    new.add_argument("--why", help="why it is filed")
    new.add_argument("--gate", help="precondition on shippability, not on investigating")
    new.add_argument("--scaffold", action="store_true",
                     help="this entry measures the harness, not a hypothesis "
                          "(a wiring check, a placeholder). Its run rows are "
                          "excluded from the frontier and named on the board "
                          "(sig.golf H4)")
    new.add_argument("--confidence", type=float, default=0.5,
                     help="prior that it confirms, 0-1 (default 0.5); "
                          "calibration pulls it toward the observed rate "
                          "once a tag has 5 closures")
    new.add_argument("--impact", type=float, default=0.0,
                     help="expected gain on the objective if it confirms "
                          "(default 0)")
    new.add_argument("--cost", type=float, default=1.0,
                     help="expected runs to decide it (default 1); ranking "
                          "divides by it and the run ceiling charges it")
    new.add_argument("--mechanism", action="append",
                     help="mechanism tag; repeat for several. Ranking, "
                          "dead-direction exclusion and calibration route on them")
    new.add_argument("--source", action="append",
                     help="where the idea came from (path, URL, entry id); repeatable")
    new.add_argument("--parent", help="the entry this branches from; omit "
                                      "for a novel root")
    new.add_argument("--kind", help="branch intent: improve, debug or probe "
                                    "(branches only)")
    new.add_argument("--derived-from", dest="derived_from",
                     help="the entry this follows up when a --parent branch "
                          "would pass tree_max_depth: lineage the graph draws "
                          "and ranking reads, outside the depth count (#23)")
    new.add_argument("--core", help="core version the defect was observed on "
                                    "(stamped automatically when the loop files it)")
    new.add_argument("--repro", help="command or path that demonstrates the defect")
    new.add_argument("--observed", help="what actually happened")
    new.add_argument("--expected", help="what should have happened instead")
    new.set_defaults(func=cmd_entry_new)
    show = esub.add_parser("show", help="one entry as JSON: bar, claim, "
                                        "result, gates")
    show.add_argument("id")
    show.add_argument("--history", action="store_true",
                      help="include the append-only history (default: the "
                           "current state only)")
    show.set_defaults(func=cmd_entry_show)
    amend = esub.add_parser("amend", help="complete or correct evidence fields")
    amend.add_argument("id")
    amend.add_argument("--title", help="correct the title")
    amend.add_argument("--hypothesis", help="correct the hypothesis text")
    amend.add_argument("--core", help="core version the defect was observed on")
    amend.add_argument("--repro", help="command or path that demonstrates it")
    amend.add_argument("--observed", help="what actually happened")
    amend.add_argument("--expected", help="what should have happened instead")
    amend.add_argument("--scaffold", dest="scaffold", action="store_true",
                       default=None,
                       help="mark the entry as measuring the harness rather "
                            "than a hypothesis; its run rows leave the frontier")
    amend.add_argument("--no-scaffold", dest="scaffold", action="store_false",
                       help="undo --scaffold: the entry's rows are findings again")
    amend.add_argument("--prediction",
                       help="correct the pre-registered prediction in place; "
                            "requires --why, because it is what the entry "
                            "promised before it ran (sig.golf H6)")
    amend.add_argument("--bar",
                       help="correct the pre-registered confirm/refute bar in "
                            "place; requires --why")
    amend.add_argument("--why-filed", dest="why_filed",
                       help="correct why this entry was filed in place; "
                            "requires --why")
    amend.add_argument("--parent", dest="parent",
                       help="correct the parent this entry branches from "
                            "(H15: a mis-parented entry had no writer but "
                            "prose)")
    amend.add_argument("--source", dest="source", action="append",
                       help="add a source; repeatable, and never drops one "
                            "already recorded (H15)")
    amend.add_argument("--mechanism", dest="mechanism", action="append",
                       help="add a mechanism tag; repeatable, additive (H15). "
                            "On a closed entry it also changes what the "
                            "closure excludes, and needs --why")
    amend.add_argument("--drop-source", dest="drop_source", action="append",
                       help="remove a source that is recorded but wrong "
                            "(repeatable; a value that is not there is "
                            "refused, not ignored)")
    amend.add_argument("--drop-mechanism", dest="drop_mechanism",
                       action="append",
                       help="remove a mechanism tag (repeatable; refused if "
                            "it is not there)")
    amend.add_argument("--summary", dest="summary",
                       help="correct a CLOSED entry's result.summary in place; "
                            "the status and the verdict are untouched and "
                            "--why is required (QSB H82)")
    amend.add_argument("--reopen-condition", dest="reopen_condition",
                       help="correct a CLOSED entry's reopen condition in "
                            "place; --why is required (QSB H82)")
    amend.add_argument("--evidence-class", dest="evidence_class",
                       choices=sorted(EVIDENCE_CLASSES),
                       help="declare which evidence class backs the result "
                            "(ledger = run-ledger backed, official = "
                            "evaluator-owned numbers, census = "
                            "measurement-free work); requires --why")
    amend.add_argument("--why",
                       help="required when --evidence-class names the "
                            "reclassification reason")
    amend.set_defaults(func=cmd_entry_amend)
    reprice = esub.add_parser(
        "reprice", help="correct confidence/impact/cost on an open entry")
    reprice.add_argument("id")
    reprice.add_argument("--confidence", type=float,
                         help="P(confirm); a probability in [0, 1]")
    reprice.add_argument("--impact", type=float,
                         help="expected fractional move on the objective")
    reprice.add_argument("--cost", type=float, help="cost in run-units")
    reprice.add_argument("--why", required=True,
                         help="a re-price without a reason is a guess moving "
                              "the ranking")
    reprice.set_defaults(func=cmd_entry_reprice)
    lst = esub.add_parser("list", help="one line per entry: id, status, "
                                        "holder, title (open entries by default)")
    lst.add_argument("--status", action="append",
                     help="only this status; repeat for several")
    lst.add_argument("--track", help="only this track")
    lst.add_argument("--all", action="store_true",
                     help="list terminal entries in the table too (--json "
                          "always carries every entry)")
    lst.add_argument("--json", action="store_true",
                     help="print the pick-work projection (no history, no "
                          "prose) as JSON")
    lst.set_defaults(func=cmd_entry_list)
    collisions = esub.add_parser(
        "collisions", help="entry ids this branch and REF both filed or "
                           "changed since they diverged (exit 1 if any)")
    collisions.add_argument("ref", help="the branch or commit to merge")
    collisions.set_defaults(func=cmd_entry_collisions)
    graph = esub.add_parser(
        "graph", help="the hypothesis tree as a mermaid diagram")
    graph.add_argument("--track", help="one track (default: every track)")
    graph.add_argument("--html", metavar="PATH",
                       help="write a standalone HTML snapshot (hover a node "
                            "for the full title and summary) instead of "
                            "printing markdown")
    graph.set_defaults(func=cmd_entry_graph)

    skill = sub.add_parser("skill", help="the domain's distilled, cited skills")
    ssub = skill.add_subparsers(dest="skill_cmd", required=True)
    slist = ssub.add_parser("list",
                         help="every skill, its citations and its staleness")
    slist.add_argument("--json", action="store_true",
                       help="print the listing as JSON, so an agent can ask "
                            "whether the map is current without parsing prose")
    slist.set_defaults(func=cmd_skill_list)
    sfind = ssub.add_parser(
        "find", help="route on a term: AND-match over skill names and descriptions")
    sfind.add_argument("terms", nargs="+",
                       help="every term must appear in a skill's name or "
                            "description (case-insensitive)")
    sfind.set_defaults(func=cmd_skill_find)
    sshow = ssub.add_parser("show", help="print one skill's body")
    sshow.add_argument("name")
    sshow.set_defaults(func=cmd_skill_show)
    ssub.add_parser("check", help="validate every skill against the record"
                    ).set_defaults(func=cmd_skill_check)
    sdistil = ssub.add_parser("distil", help="run the distil phase once")
    sdistil.add_argument("--dry-run", action="store_true",
                         help="show what would be distilled; write nothing")
    sdistil.add_argument("--max-usd", type=float, dest="max_usd",
                         help="hard ceiling on model spend; defaults to policy.spend_ceiling")
    sdistil.add_argument("--allow-paid-brain", action="store_true", dest="allow_paid_brain",
                         help="authorize the built-in TypeSafe brain to spend API money "
                              "for this run (otherwise domain.toml must set [brain] authorize_spend)")
    sdistil.set_defaults(func=cmd_skill_distil)
    ssub.add_parser("link", help="expose the skills to a coding harness "
                    "opened at the domain root (.claude/skills)"
                    ).set_defaults(func=cmd_skill_link)
    sretire = ssub.add_parser("retire", help="delete one skill, with a reason")
    sretire.add_argument("name")
    sretire.add_argument("--why", required=True,
                         help="a skill removed without a reason is a skill that "
                              "will be written again")
    sretire.set_defaults(func=cmd_skill_retire)

    claim = sub.add_parser("claim", help="take one entry (serialised)")
    claim.add_argument("id")
    claim.add_argument("--why", help="what you intend to do; recorded in history")
    claim.add_argument("--budget", help="human-readable ceiling, recorded beside the numbers")
    claim.add_argument("--max-runs", type=int, dest="max_runs",
                       help="run ceiling for this claim (default budgets.claim_max_runs)")
    claim.add_argument("--max-hours", type=float, dest="max_hours",
                       help="wall-clock ceiling for this claim "
                            "(default budgets.claim_wall_clock_hours)")
    claim.set_defaults(func=cmd_claim)

    rel = sub.add_parser("release", help="hand a claim back, with a reason")
    rel.add_argument("id")
    rel.add_argument("--why", required=True,
                     help="why it goes back; the next claimant reads it")
    rel.set_defaults(func=cmd_release)

    reap = sub.add_parser(
        "reap",
        help="repair an unowned in-progress record, or free ONE expired claim")
    reap.add_argument("id", nargs="?",
                      help="the entry to free; omit to list what is reapable")
    reap.add_argument("--ttl-hours", type=float, dest="ttl_hours",
                      help="silence after which a claim is reapable "
                           "(default budgets.claim_ttl_hours)")
    reap.set_defaults(func=cmd_reap)

    close = sub.add_parser("close", help="close, reopen or relabel an entry")
    close.add_argument("id")
    close.add_argument("status", nargs="?", help="terminal status for this track")
    close.add_argument("--memo", help="path to the evidence memo; must exist")
    close.add_argument("--superseded-by", dest="superseded_by",
                       help="required when closing `superseded`: the entry "
                            "that covers this one, or an outside record "
                            "named with a scheme (https://..., hosted:<id>). The relation is recorded "
                            "in that entry's `supersedes`, so retirement "
                            "always names its replacement (H15)")
    close.add_argument("--summary", help="the numbers and the arithmetic to the "
                                         "verdict, in a sentence or two")
    close.add_argument("--closure", choices=sorted(CLOSURE_KINDS),
                       help="how far a refutation reaches")
    close.add_argument("--reopen-condition", dest="reopen_condition",
                       help="required for slope/cell closures")
    close.add_argument("--source-revision", dest="source_revision",
                       help="the revision this closure was decided at, for a "
                            "closure with no stamped rows of its own (an "
                            "external or static one); recorded in its pin (#40)")
    close.add_argument("--diff-base", dest="diff_base",
                       help="for a confirmed close, list tamper-shaped hunks "
                            "in the diff since this revision (default HEAD: "
                            "uncommitted changes) (#39)")
    close.add_argument("--why", help="reason recorded in history (needed to "
                                     "reopen, relabel or correct a closure)")
    close.add_argument("--reopen", action="store_true",
                       help="put a closed entry back in the queue")
    close.add_argument("--reopen-reason", dest="reopen_reason",
                       choices=list(REOPEN_REASONS),
                       help="required to reopen a verdict: weak (it did not "
                            "survive), overclaim (it stands, its claim is "
                            "narrowed), context (sound then, changed since) "
                            "or clerical (bookkeeping)")
    close.add_argument("--relabel", choices=sorted(CLOSURE_KINDS),
                       help="change a refutation's closure kind without reopening")
    close.add_argument("--evidence-class", dest="evidence_class",
                       choices=sorted(EVIDENCE_CLASSES),
                       help="which evidence class backs this closure. No "
                            "default: an unlabelled closure is recorded as "
                            "unlabelled rather than stamped with the "
                            "run-ledger net it never got (QSB H87). Only an "
                            "explicit official/census opts out of that net")
    close.set_defaults(func=cmd_close)

    meas = sub.add_parser("measure", help="run the domain's measurement, record the row")
    meas.add_argument("--entry", help="the claimed entry this run is charged to")
    meas.add_argument("--candidate", type=pathlib.Path,
                      help="a workspace slot to measure: the command, policy "
                           "and evaluator come from this domain, the tree "
                           "from the slot, and the row lands in the slot "
                           "with a receipt harvest checks (#36)")
    meas.add_argument("rest", nargs="*",
                      help="passed through to the domain command, flags included")
    meas.set_defaults(func=cmd_measure)

    challenge = sub.add_parser(
        "challenge",
        help="the public challenge's shared intel: record, board, published policy")
    csub = challenge.add_subparsers(dest="challenge_cmd", required=True)
    cpull = csub.add_parser("pull", help="fetch-if-stale, normalize, cache the snapshot")
    cpull.add_argument("--force", action="store_true",
                       help="refetch even if the cache is inside refresh_seconds")
    cpull.set_defaults(func=cmd_challenge_pull)
    cshow = csub.add_parser("show", help="render the cached snapshot (no network)")
    cshow.set_defaults(func=cmd_challenge_show)
    ccheck = csub.add_parser(
        "check", help="compare the published policy against this host and [policy]")
    ccheck.set_defaults(func=cmd_challenge_check)
    ctgt = csub.add_parser(
        "target", help="print the target number, one line (fetch-if-stale); "
                       "bin/probe-target wraps this")
    ctgt.set_defaults(func=cmd_challenge_target)

    sub.add_parser("doctor", help="diagnose the installed core against this "
                   "domain's declarations").set_defaults(func=cmd_doctor)

    kit = sub.add_parser(
        "kit", help="shared measurement code vendored into the domain "
                    "([evaluator] kits)")
    ksub = kit.add_subparsers(dest="kit_cmd", required=True)
    ksub.add_parser("list", help="shipped kits and this domain's copies"
                    ).set_defaults(func=cmd_kit_list)
    ksub.add_parser("sync", help="write each declared kit as this core ships it"
                    ).set_defaults(func=cmd_kit_sync)

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

    session = sub.add_parser(
        "session", help="persistent worktree sessions for hand-driven work")
    ssn = session.add_subparsers(dest="session_action", required=True)
    ssn_create = ssn.add_parser(
        "create", help="cut a session worktree beside this domain")
    ssn_create.add_argument("name", help="slug naming the session")
    ssn_destroy = ssn.add_parser(
        "destroy", help="remove a session worktree; refuses drift and unharvested rows")
    ssn_destroy.add_argument("name")
    ssn_destroy.add_argument(
        "--harvest", action="store_true",
        help="copy worktree-only run rows and entries into the primary before removing")
    ssn_destroy.add_argument(
        "--discard-unreceipted", action="store_true",
        help="with --harvest: archive run rows no `ar measure --candidate` "
             "receipted beside the session record, instead of refusing (#41)")
    ssn_destroy.add_argument(
        "--accept-drift", action="store_true",
        help="proceed even though domain.toml changed since the session was cut")
    ssn_prune = ssn.add_parser(
        "prune", help="report every session, its age against the settle window "
                      "and whether it is eligible; destroys only with --destroy")
    ssn_prune.add_argument(
        "--destroy", action="store_true",
        help="destroy the sessions prune reports as eligible (clean, settled, "
             "nothing unharvested), each through destroy's own refusals")
    ssn_create.set_defaults(func=cmd_session, session_action="create")
    ssn_destroy.set_defaults(func=cmd_session, session_action="destroy")
    ssn_prune.set_defaults(func=cmd_session, session_action="prune")
    workspace = sub.add_parser("workspace", help="inspect or archive an abandoned workspace")
    wsub = workspace.add_subparsers(dest="workspace_action", required=True)
    inspect = wsub.add_parser("inspect", help="inspect holder and obtain a recovery token")
    inspect.add_argument("name")
    inspect.set_defaults(func=cmd_workspace)
    recover = wsub.add_parser("recover", help="archive evidence after confirming holder inactivity")
    recover.add_argument("name")
    recover.add_argument("--holder", required=True)
    recover.add_argument("--token", required=True, help="token returned by workspace inspect")
    recover.add_argument("--confirm-inactive", action="store_true", required=True)
    recover.set_defaults(func=cmd_workspace)

    bundles_mod.register_parser(sub)
    gates_mod.register_parser(sub)
    relocate_mod.register_parser(sub)
    attempts_mod.register_parser(sub)
    external_mod.register_parser(sub)
    usage = sub.add_parser("usage", help="out-of-band spend, metered like any other")
    usub = usage.add_subparsers(dest="usage_cmd", required=True)
    urec = usub.add_parser("record", help="append one spend row")
    urec.add_argument("--tool", required=True, help="what spent it, e.g. jev or vast")
    urec.add_argument("--kind", required=True, choices=sorted(usage_mod.KINDS))
    urec.add_argument("--cost", type=float, help="what it cost in the domain's currency")
    urec.add_argument("--unknown", action="store_true",
                      help="the price is not knowable yet; the money ceiling goes unknown, not free")
    urec.add_argument("--session", required=True)
    urec.add_argument("--note", required=True)
    urec.set_defaults(func=cmd_usage, usage_cmd="record")
    usub.add_parser("list", help="every row, and what is still unpriced"
                    ).set_defaults(func=cmd_usage, usage_cmd="list")
    urec_ = usub.add_parser("reconcile", help="price one unpriced row")
    urec_.add_argument("lineno", type=int, help="the row's line number, from `ar usage list`")
    urec_.add_argument("--cost", type=float, required=True)
    urec_.add_argument("--session", required=True)
    urec_.add_argument("--note", required=True)
    urec_.set_defaults(func=cmd_usage, usage_cmd="reconcile")

    plugins_mod.register(sub, plugins_spec, root, config)
    _accept_token_after_verb(ap)
    return ap


def _accept_token_after_verb(parser) -> None:
    """Every refusal says "pass `--claim-token TOKEN`", and an agent reads that
    as a flag of the verb it just ran: `ar release Q1 --claim-token T` was an
    unrecognized argument (starks H6). Each leaf verb accepts it too. SUPPRESS
    keeps a leaf that was not given it from overwriting the global value."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for child in set(action.choices.values()):
                _accept_token_after_verb(child)
            return
    if "--claim-token" not in parser._option_string_actions:
        parser.add_argument("--claim-token", dest="claim_token",
                            default=argparse.SUPPRESS,
                            help="the token `ar claim` printed (also accepted "
                                 "before the verb, or as $AR_CLAIM_TOKEN)")


def _plugins_config(argv) -> DomainConfig | None:
    """The domain's config when it declares plugins, else None.

    Plugin subcommands cannot parse until they are registered, and they can
    only be registered once the domain is known -- so argv is pre-scanned for
    `--domain`, the config is loaded early, and a parser carrying the plugins'
    subparsers is built for the real parse. Loading the config twice is one
    extra TOML read; it keeps every other command's failure modes unchanged,
    and a plugin that fails its contract is refused before anything parses --
    a mistyped plugin name is a config error, not an invalid-choice error
    (H135's shape: a command that runs the wrong thing reads as success)."""
    rest = list(argv if argv is not None else sys.argv[1:])
    domain = None
    for i, a in enumerate(rest):
        if a == "--domain" and i + 1 < len(rest):
            domain = rest[i + 1]
        elif a.startswith("--domain="):
            domain = a.split("=", 1)[1]
    try:
        root = pathlib.Path(domain) if domain else discover()
        config = DomainConfig.load(root)
    except AutoresearchError:
        return None
    if not config.plugins:
        return None
    return config


def _measure_passthrough(argv):
    """`ar measure --entry Q55 --patch X` refused `--patch` as unknown: the
    domain's flags only got through after a `--` the help never mentioned
    (falcon H1). Everything after measure's own `--entry` is the domain's, so
    the separator is supplied here."""
    # The subcommand is the first token past the global options; "measure"
    # appearing later is some other verb's argument (a title, a summary).
    i = 0
    while i < len(argv) and argv[i].startswith("-"):
        i += 2 if argv[i] in ("--domain", "--session", "--claim-token") else 1
    if i >= len(argv) or argv[i] != "measure":
        return argv
    j = i + 1
    while j < len(argv):
        if argv[j] in ("--entry", "--candidate", "--claim-token"):
            j += 2
        elif argv[j].startswith(("--entry=", "--candidate=", "--claim-token=")):
            j += 1
        else:
            break
    if j < len(argv) and argv[j] not in ("--", "-h", "--help"):
        return argv[:j] + ["--"] + argv[j:]
    return argv


def main(argv=None) -> int:
    argv = _measure_passthrough(list(argv if argv is not None else sys.argv[1:]))
    spec = _plugins_config(argv)
    if spec is not None:
        try:
            parser = build_parser(spec.plugins, spec.paths.root, spec)
        except AutoresearchError as exc:
            print(f"ar: {exc}", file=sys.stderr)
            return 2
        args = parser.parse_args(argv)
    else:
        parser = build_parser()
        args = parser.parse_args(argv)
    _VIEWS_STALE.clear()
    code = None
    try:
        code = args.func(args)
        # Inside the try: buffered output that only meets the closed pipe at
        # the interpreter's exit flush would otherwise print its own traceback.
        sys.stdout.flush()
        return VIEWS_STALE if code == 0 and _VIEWS_STALE else code
    except AutoresearchError as exc:
        print(f"ar: {exc}", file=sys.stderr)
        return 2
    except BrokenPipeError:
        # `ar rank | head`: the reader stopped, which is not a defect, and a
        # traceback reads to an agent as the harness crashing. stdout goes to
        # devnull so nothing else tries the dead pipe. The verb's own code
        # stands if it finished; one cut off mid-print exits 1.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 1 if code is None else code


if __name__ == "__main__":
    sys.exit(main())
