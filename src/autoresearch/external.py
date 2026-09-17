"""Native coordinator handoff. No model dispatch and no sandbox promise.

Assignments reserve a finite run allocation before a workspace is exported.
Completion uses the same harvest and verdict path as core workers. Workspaces
remain retained until an explicit operator recovery; cancellation never deletes
unharvested evidence. Receipts survive interruption between entry and ledger IO.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
from pathlib import Path

from . import attempts, budget, runs
from .claims import Lock
from .driver.loop import Coordinator, Phase
from .entries import Event, Store
from .errors import AutoresearchError
from .workspaces import Pool

OUTPUT_CONTRACT = {
    "verdict": "confirmed|refuted|blocked|inconclusive",
    "memo": "workspace-relative findings path",
    "summary": "what was established against the entry bar",
    "closure_kind": "mechanism|slope|cell (refutations)",
    "reopen_condition": "required for slope/cell",
    "disposition": "optional experiment|superseded|already-shipped; default experiment",
    "applicability": "optional context object: baseline, source_revision, workload, hardware, parameters",
    "runs": "nonnegative integer; retained measurement evidence required",
    "gpu_hours": "finite nonnegative measured usage",
    "verification": {"reread": True, "claims_checked": ["each verified claim"],
                     "corrections": ["each correction"]},
}


def _lock(config, identity, session):
    return Lock(attempts.directory(config) / identity / "handoff.lock", session,
                stale_after=config.budgets.get("claim_lock_stale_seconds", 900))


def _workspace(config, row):
    pool = Pool(config, row["session"])
    receipt = pool.inspect(row["workspace_name"])
    if receipt["holder"] != row["session"] or not receipt["exists"]:
        raise AutoresearchError("assignment workspace is missing or belongs to another owner")
    path = Path(receipt["path"])
    if str(path) != row["workspace"]:
        raise AutoresearchError("assignment workspace identity changed")
    return path


def assign(config, entry_id, session, seconds, max_runs):
    row = attempts.reserve(config, entry_id, session, seconds, kind="external", units=max_runs)
    name = "external-" + row["id"]
    with _lock(config, row["id"], session):
        with attempts.claims(config, session)._lock():
            attempts.live_claim(config, row)
            row.update(workspace_name=name, workspace=str(config.paths.workspaces / name))
            attempts.save(config, row)
        pool = Pool(config, session)
        try:
            slot = pool.acquire(name)
            pool.retain(name, "external assignment evidence; recover only after reviewing receipt")
            relative = config.paths.runs.relative_to(config.paths.root)
            before = runs.snapshot(slot.path / relative)
            entry = Store(config.paths.entries).load(entry_id)
            with attempts.claims(config, session)._lock():
                attempts.live_claim(config, row)
                row.update(status="assigned",
                           snapshot={str(k): [v[0], v[1].hex()] for k, v in before.items()},
                           brief=dataclasses.asdict(entry),
                           scopes={"root": str(slot.path), "workspace": str(slot.path)},
                           ceiling={"runs": max_runs, "seconds": row["timeout_seconds"],
                                    "expires": (dt.datetime.fromisoformat(row["created"]) +
                                                dt.timedelta(seconds=row["timeout_seconds"])).isoformat()},
                           output_contract=OUTPUT_CONTRACT,
                           policy={"human_only": [dataclasses.asdict(rule) for rule in config.policy.human_only],
                                   "enforcement": "external coordinator must enforce policy; handoff is not a sandbox",
                                   "submissions": "human-gated; no submission is performed by this protocol"})
                attempts.save(config, row)
            return row
        except BaseException as exc:
            # The reservation and any created workspace stay available to cancel/recover.
            with attempts.claims(config, session)._lock():
                row["setup_error"] = str(exc)
                attempts.save(config, row)
            raise


def _receipt(entry, identity, kind):
    for event in reversed(entry.history):
        if event.kind != kind:
            continue
        try:
            data = json.loads(event.detail)
        except (ValueError, TypeError):
            continue
        if data.get("assignment") == identity:
            return data
    return None


def _appended(config, row, workspace, before):
    directory = workspace / config.paths.runs.relative_to(config.paths.root)
    records = []
    for path in sorted(directory.rglob("*.jsonl")):
        if path.is_symlink() or not path.resolve().is_relative_to(workspace.resolve()):
            raise AutoresearchError("run evidence escapes assignment workspace")
        size, digest = before.get(path.relative_to(directory), (0, None))
        if digest is not None and (path.stat().st_size < size or runs._digest(path, size) != digest):
            raise AutoresearchError("inherited run evidence changed")
        with path.open("rb") as stream:
            stream.seek(size)
            for line in stream:
                if not line.strip():
                    continue
                try:
                    record = runs.RunRecord.from_dict(json.loads(line))
                    record.validate(config.goal)
                except (ValueError, TypeError) as exc:
                    raise AutoresearchError(f"malformed assignment evidence: {path}") from exc
                if record.session != row["session"] or record.entry != row["entry"]:
                    raise AutoresearchError("evidence must match assignment entry and session")
                records.append(record)
    if len({r.id for r in records}) != len(records):
        raise AutoresearchError("duplicate run IDs in assignment evidence")
    return directory, records


def complete(config, identity, session, report):
    if not isinstance(report, dict):
        raise AutoresearchError("completion report must be a JSON object")
    with _lock(config, identity, session):
        row = attempts.load(config, identity, session)
        if row["kind"] != "external":
            raise AutoresearchError("not an external assignment")
        if row["status"] == "completed":
            if row.get("report") != report:
                raise AutoresearchError("assignment already completed with a different report")
            return row
        if row["status"] in attempts.TERMINAL or row["status"] == "cancelling":
            raise AutoresearchError("assignment is not completable")
        if row.get("report") is not None and row["report"] != report:
            raise AutoresearchError("completion already prepared with a different report")
        store = Store(config.paths.entries)
        committed = _receipt(store.load(row["entry"]), identity, "external-complete")
        if committed:
            return attempts.settle(config, identity, session, "completed",
                                   consumed=row["charged_runs"], run_ids=row["run_ids"],
                                   verdict=committed["verdict"])
        with attempts.claims(config, session)._lock():
            attempts.live_claim(config, row)
            if dt.datetime.now(dt.UTC) > dt.datetime.fromisoformat(row["ceiling"]["expires"]):
                raise AutoresearchError("assignment deadline exhausted; cancel and preserve evidence")
            row.update(status="completing", report=report)
            attempts.save(config, row)
        workspace = _workspace(config, row)
        before = {Path(k): (v[0], bytes.fromhex(v[1])) for k, v in row["snapshot"].items()}
        directory, observed = _appended(config, row, workspace, before)
        try:
            reported = budget.usage_number(report.get("runs", 0))
            gpu_hours = budget.usage_number(report.get("gpu_hours", 0))
        except (ValueError, TypeError) as exc:
            raise AutoresearchError(f"invalid completion usage: {exc}") from exc
        if reported != int(reported) or reported > len(observed) or (gpu_hours and not observed):
            raise AutoresearchError("reported usage lacks retained measurement evidence")
        measured_gpu_hours = 0.0
        for record in observed:
            try:
                measured_gpu_hours += budget.usage_number(record.cost.get("gpu_hours", 0))
            except (ValueError, TypeError, AttributeError) as exc:
                raise AutoresearchError(f"invalid measurement GPU usage for {record.id}: {exc}") from exc
        gpu_hours = max(gpu_hours, measured_gpu_hours)
        paths = config.paths
        protected = (paths.entries, paths.claims, paths.runs, paths.iterations, paths.workspaces,
                     attempts.directory(config))
        with attempts.claims(config, session)._lock():
            attempts.live_claim(config, row)
            harvest = runs.harvest(directory, before, paths.runs, workspace=workspace,
                                   root=paths.root, goal=config.goal, lanes=config.lanes,
                                   protected=protected)
            if harvest.problems:
                raise AutoresearchError("; ".join(harvest.problems))
            if report.get("memo"):
                runs.retain_output(workspace, paths.root, report["memo"], config.lanes, protected)
            row.update(run_ids=sorted(r.id for r in observed),
                       charged_runs=max(1, len(observed), reported), gpu_hours=gpu_hours)
            attempts.save(config, row)
            if row["charged_runs"] > row["reserved_runs"]:
                raise AutoresearchError("assignment exceeded its run allocation; evidence retained, cancel required")
            # No constructor/model invocation: reuse only the ordinary verdict application.
            coordinator = Coordinator.__new__(Coordinator)
            coordinator.config, coordinator.session = config, session
            coordinator.store = store
            coordinator.claims = attempts.claims(config, session)
            phase = Phase("external-complete")
            verdict = coordinator._apply_verdict_locked(row["entry"], report, phase, receipt_id=identity)
            row.update(verdict=verdict, detail=phase.detail)
            attempts.save(config, row)
        return attempts.settle(config, identity, session, "completed",
                               consumed=row["charged_runs"], run_ids=row["run_ids"], verdict=verdict)


def cancel(config, identity, session, reason, *, confirm_inactive=False):
    if not reason.strip() or not confirm_inactive:
        raise AutoresearchError("cancel requires a reason and explicit inactive-worker confirmation")
    with _lock(config, identity, session):
        with attempts.claims(config, session)._lock():
            row = attempts.load(config, identity, session)
            if row["kind"] != "external":
                raise AutoresearchError("not an external assignment")
            if row["status"] == "cancelled":
                return row
            if row["status"] in attempts.TERMINAL:
                raise AutoresearchError("assignment already settled")
            owner = attempts.claims(config, session)
            entry = owner.store.load(row["entry"])
            if _receipt(entry, identity, "external-complete"):
                raise AutoresearchError("completion already applied; retry complete with its prepared report")
            row.update(status="cancelling", reason=reason)
            attempts.save(config, row)
            if entry.claim and entry.claim.session == session and entry.claim.at == row["claim_at"]:
                owner.release_locked(row["entry"], reason, expected_claim_at=row["claim_at"],
                                     event=Event(attempts.now(), "external-cancel", session,
                                                 json.dumps({"assignment": identity})))
            # A stale assignment may settle its own ledger, never release a successor claim.
            row.update(status="cancelled", finished=attempts.now())
            attempts.save(config, row)
            return row


def _command(args):
    config = attempts._config(args)
    if args.external_action == "assign":
        row = assign(config, args.entry, args.session, args.timeout, args.max_runs)
    elif args.external_action == "complete":
        try:
            report = json.loads(Path(args.report).read_text())
        except (OSError, ValueError) as exc:
            raise AutoresearchError(f"unreadable completion report: {exc}") from exc
        row = complete(config, args.id, args.session, report)
    elif args.external_action == "cancel":
        row = cancel(config, args.id, args.session, args.reason,
                     confirm_inactive=args.confirm_inactive)
    else:
        row = attempts.load(config, args.id, args.session)
    print(json.dumps(row, indent=2))
    return 0


def register_parser(subparsers):
    parser = subparsers.add_parser("external", help="bounded native coordinator handoff; no model dispatch")
    subs = parser.add_subparsers(dest="external_action", required=True)
    for action in ("assign", "complete", "cancel", "show"):
        child = subs.add_parser(action)
        if action == "assign":
            child.add_argument("--entry", required=True)
            child.add_argument("--timeout", type=float, required=True)
            child.add_argument("--max-runs", type=int, required=True)
        else:
            child.add_argument("id")
        if action == "complete":
            child.add_argument("--report", required=True)
        if action == "cancel":
            child.add_argument("--reason", required=True)
            child.add_argument("--confirm-inactive", action="store_true")
        child.set_defaults(func=_command)
