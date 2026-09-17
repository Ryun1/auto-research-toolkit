"""Persistent execution reservations. Policy checked argv, not a sandbox.

A crash never refunds a reservation. Checkpointing an abandoned attempt requires
an explicit inactive-owner attestation; PID age alone never proves inactivity.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import signal
import socket
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from . import budget
from .claims import Claims
from .entries import Store
from .errors import AutoresearchError

SCHEMA = "ar-attempt-1"
ACTIVE = {"reserved", "running", "assigned", "completing", "cancelling"}
TERMINAL = {"completed", "failed", "cancelled", "timed-out"}


def now():
    return dt.datetime.now(dt.UTC).isoformat()


def session_required(session):
    if not isinstance(session, str) or session in {"unknown", "cli"} or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", session):
        raise AutoresearchError("an explicit safe --session or AR_SESSION owner is required")
    return session


def directory(config):
    return config.paths.claims.parent / "attempts"


def claims(config, session):
    return Claims(Store(config.paths.entries), config, session)


def records(config):
    result = []
    for path in sorted(directory(config).glob("*/record.json")):
        try:
            row = json.loads(path.read_text())
            if (row["schema"] != SCHEMA or row["id"] != path.parent.name
                    or not re.fullmatch(r"[a-f0-9]{32}", row["id"])
                    or row["status"] not in ACTIVE | TERMINAL
                    or row["kind"] not in {"exec", "dispatch", "external"}
                    or not isinstance(row["entry"], str) or not row["entry"]
                    or not isinstance(row["run_ids"], list)
                    or any(not isinstance(i, str) or not i for i in row["run_ids"])
                    or len(set(row["run_ids"])) != len(row["run_ids"])):
                raise ValueError("invalid identity, state or links")
            session_required(row["session"])
            dt.datetime.fromisoformat(row["claim_at"])
            for key in ("charged_runs", "reserved_runs", "timeout_seconds"):
                if budget.usage_number(row[key]) <= 0:
                    raise ValueError(f"invalid {key}")
            result.append(row)
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            raise AutoresearchError(f"invalid attempt record {path}: {exc}; reconcile before spending") from exc
    return result


def load(config, attempt_id, session=None):
    if not isinstance(attempt_id, str) or not re.fullmatch(r"[a-f0-9]{32}", attempt_id):
        raise AutoresearchError("invalid attempt identity")
    row = next((r for r in records(config) if r["id"] == attempt_id), None)
    if row is None:
        raise AutoresearchError(f"unknown attempt {attempt_id}")
    if session is not None and row["session"] != session_required(session):
        raise AutoresearchError("attempt belongs to another session")
    return row


def save(config, row):
    target = directory(config) / row["id"] / "record.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".record-", dir=target.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(row, stream, sort_keys=True, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        Path(temporary).unlink(missing_ok=True)


def live_claim(config, row, *, check_time=True):
    entry = Store(config.paths.entries).load(row["entry"])
    claim = entry.claim
    if (not claim or entry.status != "in-progress" or claim.session != row["session"]
            or claim.at != row["claim_at"]):
        raise AutoresearchError("claim identity changed; operation refused")
    hours = claim.max_hours if claim.max_hours is not None else config.budgets.get("claim_wall_clock_hours")
    if hours is None or not math.isfinite(hours) or hours <= 0:
        raise AutoresearchError("claim requires a finite positive wall-clock ceiling")
    started = dt.datetime.fromisoformat(claim.at)
    if started.tzinfo is None:
        started = started.replace(tzinfo=dt.UTC)
    remaining = hours * 3600 - (dt.datetime.now(dt.UTC) - started).total_seconds()
    if check_time and remaining <= 0:
        raise AutoresearchError("claim wall-clock ceiling exhausted")
    return entry, remaining


def reserve(config, entry_id, session, seconds, command=(), *, kind="exec", units=1):
    session_required(session)
    if (not isinstance(seconds, (int, float)) or isinstance(seconds, bool)
            or not math.isfinite(seconds) or seconds <= 0):
        raise AutoresearchError("timeout must be finite and positive")
    if not isinstance(units, int) or isinstance(units, bool) or units <= 0:
        raise AutoresearchError("reservation runs must be a positive integer")
    with claims(config, session)._lock():
        entry = Store(config.paths.entries).load(entry_id)
        if not entry.claim:
            raise AutoresearchError("execution requires an existing live claim")
        row = {"entry": entry_id, "session": session, "claim_at": entry.claim.at}
        entry, remaining = live_claim(config, row)
        ceiling = entry.claim.max_runs
        if ceiling is None:
            ceiling = config.budgets.get("claim_max_runs")
        domain_ceiling = config.budgets.get("domain_max_runs")
        for name, value in (("claim runs", ceiling), ("domain runs", domain_ceiling)):
            if value is None or not math.isfinite(value) or value <= 0:
                raise AutoresearchError(f"{name} ceiling must be finite and positive")
        if budget.claim_runs(config, entry) + units > ceiling:
            raise AutoresearchError("claim run ceiling exhausted")
        if budget.total_runs(config) + units > domain_ceiling:
            raise AutoresearchError("domain run ceiling exhausted")
        budget.require_money(config, budget.recorded_usage(config)["money"])
        parallel = config.coordinator.get("max_parallel", 1)
        if not isinstance(parallel, int) or parallel <= 0:
            raise AutoresearchError("max_parallel must be a finite positive integer")
        if sum(r["status"] in ACTIVE for r in records(config)) >= parallel:
            raise AutoresearchError("execution concurrency ceiling exhausted; inspect attempts")
        row.update(schema=SCHEMA, id=uuid.uuid4().hex, status="reserved", kind=kind,
                   created=now(), command=list(command), reserved_runs=units,
                   charged_runs=units, timeout_seconds=min(seconds, remaining),
                   owner_pid=os.getpid(), host=socket.gethostname(), run_ids=[], checkpoints=[])
        save(config, row)
        return row


def settle(config, attempt_id, session, status, *, run_ids=(), consumed=None, **detail):
    if status not in TERMINAL:
        raise AutoresearchError("settlement requires a terminal status")
    with claims(config, session)._lock():
        row = load(config, attempt_id, session)
        amount = row["charged_runs"] if consumed is None else max(1, budget.usage_number(consumed))
        ids = sorted(set(run_ids))
        if row["status"] in TERMINAL:
            if row["status"] != status or row["run_ids"] != ids or row["charged_runs"] != amount:
                raise AutoresearchError("attempt already settled with a different receipt")
            return row
        from . import runs
        known = {r.id: r for r in runs.read_all(config.paths.runs)}
        evidence_session = detail.get("evidence_session", session) if row["kind"] == "dispatch" else session
        for run_id in ids:
            record = known.get(run_id)
            if record is None or record.entry != row["entry"] or record.session != evidence_session:
                raise AutoresearchError("linked run must be retained and owned by the attempt entry/session")
        row.update(detail)
        row.update(status=status, run_ids=ids, charged_runs=amount, finished=now())
        save(config, row)
        return row


def checkpoint(config, attempt_id, session, note, *, confirm_inactive=False):
    if not note.strip():
        raise AutoresearchError("checkpoint requires a nonempty note")
    with claims(config, session)._lock():
        row = load(config, attempt_id, session)
        if row["kind"] == "external" and confirm_inactive:
            raise AutoresearchError("use external cancel for assignment recovery")
        row["checkpoints"].append({"at": now(), "note": note})
        if confirm_inactive and row["status"] in ACTIVE:
            row.update(status="cancelled", finished=now(), reason=note)
        save(config, row)
        return row


def _terminate(proc):
    if proc is None or proc.poll() is not None:
        return
    os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


def execute(config, entry_id, session, seconds, command):
    command = list(command)
    if command[:1] == ["--"]:
        command = command[1:]
    if not command or any(not isinstance(v, str) or not v or "\0" in v for v in command):
        raise AutoresearchError("exec requires nonempty argv tokens")
    config.policy.check_command(command)
    from . import runs
    before = {record.id for record in runs.read_all(config.paths.runs)}
    row = reserve(config, entry_id, session, seconds, command)
    work = directory(config) / row["id"]
    proc = None

    def receipt():
        return [record.id for record in runs.read_all(config.paths.runs)
                if record.id not in before and record.entry == entry_id
                and record.session == session]

    try:
        with (work / "stdout.log").open("w") as stdout, (work / "stderr.log").open("w") as stderr:
            with claims(config, session)._lock():
                _, remaining = live_claim(config, row)
                config.policy.check_command(command)
                proc = subprocess.Popen(command, cwd=config.paths.root, stdout=stdout, stderr=stderr,
                                        start_new_session=True, env={**os.environ, "AR_ENTRY": entry_id,
                                        "AR_SESSION": session, "AR_ATTEMPT": row["id"]})
                row.update(status="running", pid=proc.pid, started=now())
                save(config, row)
            deadline = time.monotonic() + min(remaining, row["timeout_seconds"])
            while proc.poll() is None:
                live_claim(config, row)
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(command, row["timeout_seconds"])
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        run_ids = receipt()
        return settle(config, row["id"], session, "completed" if proc.returncode == 0 else "failed",
                      run_ids=run_ids, consumed=max(1, len(run_ids)), returncode=proc.returncode)
    except BaseException as exc:
        _terminate(proc)
        status = "timed-out" if isinstance(exc, subprocess.TimeoutExpired) else (
            "cancelled" if isinstance(exc, KeyboardInterrupt) else "failed")
        run_ids = receipt()
        result = settle(config, row["id"], session, status, reason=str(exc),
                        run_ids=run_ids, consumed=max(1, len(run_ids)),
                        returncode=proc.returncode if proc else None)
        if isinstance(exc, (KeyboardInterrupt, subprocess.TimeoutExpired)):
            return result
        raise


def _config(args):
    from .config import DomainConfig, discover
    return DomainConfig.load(args.domain or discover())


def _command(args):
    config = _config(args)
    if args.command == "exec":
        row = execute(config, args.entry, args.session, args.timeout, args.argv)
        print(json.dumps(row, indent=2))
        return 0 if row["status"] == "completed" else 1
    if args.attempt_action == "list":
        row = records(config)
    elif args.attempt_action == "show":
        row = load(config, args.id)
    else:
        row = checkpoint(config, args.id, args.session, args.note, confirm_inactive=args.confirm_inactive)
    print(json.dumps(row, indent=2))
    return 0


def register_parser(subparsers):
    parser = subparsers.add_parser("exec", help="bounded shell-free execution (not a sandbox)")
    parser.add_argument("--entry", required=True)
    parser.add_argument("--timeout", type=float, required=True)
    parser.add_argument("argv", nargs=argparse.REMAINDER)
    parser.set_defaults(func=_command, command="exec")
    parser = subparsers.add_parser("attempt", help="inspect persistent execution reservations")
    subs = parser.add_subparsers(dest="attempt_action", required=True)
    for name in ("list", "show", "checkpoint"):
        child = subs.add_parser(name)
        if name != "list":
            child.add_argument("id")
        if name == "checkpoint":
            child.add_argument("--note", required=True)
            child.add_argument("--confirm-inactive", action="store_true")
        child.set_defaults(func=_command, command="attempt")
