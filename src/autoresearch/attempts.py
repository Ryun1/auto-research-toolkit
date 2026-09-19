"""Persistent execution reservations. Policy checked argv, not a sandbox.

A crash never refunds a reservation. Checkpointing an abandoned attempt requires
an explicit inactive-owner attestation; PID age alone never proves inactivity.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
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

# The declared concurrency ceiling when a domain.toml omits `max_parallel` --
# the value the scaffold writes. One source for both this module's reserve
# check and the coordinator's thread pool (`loop._map`): two defaults that
# disagreed made a hand-written domain silently serialize its dispatches while
# its parallel workers thought they had room.
DEFAULT_MAX_PARALLEL = 3


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


def _problem(path: Path, row) -> str | None:
    """Why this record cannot be trusted to feed a meter, or None."""
    try:
        if (row["schema"] != SCHEMA or row["id"] != path.parent.name
                or not re.fullmatch(r"[a-f0-9]{32}", row["id"])
                or row["status"] not in ACTIVE | TERMINAL
                or row["kind"] not in {"exec", "dispatch", "external"}
                or not isinstance(row["entry"], str) or not row["entry"]
                or not isinstance(row["run_ids"], list)
                or any(not isinstance(i, str) or not i for i in row["run_ids"])
                or len(set(row["run_ids"])) != len(row["run_ids"])):
            return "invalid identity, state or links"
        session_required(row["session"])
        dt.datetime.fromisoformat(row["claim_at"])
        for key in ("charged_runs", "reserved_runs", "timeout_seconds"):
            if budget.usage_number(row[key]) <= 0:
                return f"invalid {key}"
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        return str(exc)
    return None


# Parsed attempt records, keyed by file path -> (size, mtime_ns, row). The
# cache is transparent to callers: a file changed underneath us -- another
# process settling, a legacy row dropped in by hand -- has a different
# (size, mtime_ns) signature and is re-read.
_RECORD_CACHE: dict[Path, tuple[int, int, dict]] = {}


def _cached_row(path: Path) -> dict | None:
    """The parsed record at `path`, re-parsed only when its bytes may have
    changed, or None when it cannot be read.

    reserve() re-reads every active record under the claim lock for each
    shortlisted card, so re-globbing and re-parsing identical bytes per call
    was the hot path this cache exists for. Unreadable files are never cached:
    `problems()` must report them on every call.
    """
    try:
        stat = path.stat()
    except OSError:
        _RECORD_CACHE.pop(path, None)
        return None
    key = (stat.st_size, stat.st_mtime_ns)
    cached = _RECORD_CACHE.get(path)
    if cached is not None and (cached[0], cached[1]) == key:
        return cached[2]
    try:
        row = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    _RECORD_CACHE[path] = (stat.st_size, stat.st_mtime_ns, row)
    return row


def problems(config) -> list[dict]:
    """Every attempt record that cannot feed a meter, with its reason.

    Inspection is deliberately separate from metering: `ar attempt list`
    reports these instead of dying on the first one, because a ledger one
    legacy row bricks is a ledger nobody can even enumerate to fix.

    Quarantined and unreadable rows are reported on every call -- the cache
    only skips the re-parse of bytes it has already seen unchanged.
    """
    out = []
    for path in sorted(directory(config).glob("*/record.json")):
        try:
            row = _cached_row(path)
            if row is None:
                row = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            out.append({"path": str(path), "id": None, "reason": f"unreadable: {exc}"})
            continue
        reason = _problem(path, row)
        if reason is not None:
            out.append({"path": str(path), "id": row.get("id"),
                        "reason": reason})
    return out


def records(config):
    found = problems(config)
    if found:
        raise AutoresearchError(
            f"invalid attempt record {found[0]['path']}: {found[0]['reason']}; "
            f"reconcile before spending ({len(found)} invalid record(s)): "
            "`ar attempt reconcile <id> --charged-runs N --reason ...`")
    live = set(directory(config).glob("*/record.json"))
    # list() first: reserve() under the claim lock may insert a fresh row
    # while a lock-free reader prunes here, and a dict that changes size
    # during comprehension iteration raises.
    for path in [p for p in list(_RECORD_CACHE) if p not in live]:
        _RECORD_CACHE.pop(path)
    return [_cached_row(path) for path in sorted(live)]


TOMBSTONE_SCHEMA = "ar-attempt-tombstone-1"


def tombstones(config) -> list[dict]:
    """Reconciled-away records whose asserted consumption feeds the ceiling.

    A tombstone is an operator's statement, not a measurement: its
    `charged_runs` is asserted, never inferred, and it is counted against the
    campaign exactly as stated. The quarantined record's bytes must still hash
    to the digest the tombstone recorded -- a tombstone whose evidence moved
    proves nothing.
    """
    out = []
    for path in sorted(directory(config).glob("*/tombstone.json")):
        try:
            row = json.loads(path.read_text())
            if (row["schema"] != TOMBSTONE_SCHEMA or row["id"] != path.parent.name
                    or not re.fullmatch(r"[a-f0-9]{32}", row["id"])
                    or isinstance(row["charged_runs"], bool)
                    or not isinstance(row["charged_runs"], int)
                    or row["charged_runs"] < 0
                    or not str(row["reason"]).strip()):
                raise ValueError("invalid identity, consumption or reason")
            original = path.parent / "quarantined" / "record.json"
            if hashlib.sha256(original.read_bytes()).hexdigest() != row["record_sha256"]:
                raise ValueError("quarantined record bytes do not match the tombstone digest")
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise AutoresearchError(
                f"invalid attempt tombstone {path}: {exc}; a tombstone feeds "
                "the campaign ceiling, so a corrupt one must be fixed or "
                "removed deliberately") from exc
        out.append(row)
    return out


def reconcile(config, attempt_id, session, charged_runs, reason):
    """Archive one invalid attempt record behind a tombstone naming its spend.

    The record's bytes are preserved under `quarantined/`; the operator states
    the consumption the record itself cannot declare. Truthful zero-spend is a
    real answer -- a legacy-schema record that predates metering may genuinely
    have consumed nothing -- but zero must be stated, not defaulted.
    """
    session_required(session)
    if isinstance(charged_runs, bool) or not isinstance(charged_runs, int) or charged_runs < 0:
        raise AutoresearchError("--charged-runs must be a nonnegative integer")
    if not reason.strip():
        raise AutoresearchError("reconcile requires a nonempty --reason")
    if not re.fullmatch(r"[a-f0-9]{32}", str(attempt_id) or ""):
        raise AutoresearchError("invalid attempt identity")
    path = directory(config) / attempt_id / "record.json"
    if not path.exists():
        raise AutoresearchError(f"no attempt record at {path}; nothing to reconcile")
    try:
        row = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise AutoresearchError(
            f"{path}: unreadable ({exc}); fix it or remove it deliberately "
            "-- reconcile archives records, never deletes them") from exc
    if not isinstance(row, dict) or row.get("id") != attempt_id:
        raise AutoresearchError("attempt id does not match the record's identity")
    if _problem(path, row) is None:
        raise AutoresearchError(
            f"{attempt_id} is a valid attempt record; nothing to reconcile "
            "(settle or cancel it instead)")
    data = path.read_bytes()
    quarantine = path.parent / "quarantined"
    quarantine.mkdir(exist_ok=True)
    (quarantine / "record.json").write_bytes(data)
    tomb = {"schema": TOMBSTONE_SCHEMA, "id": attempt_id,
            "quarantined_at": now(), "by": session,
            "charged_runs": charged_runs, "reason": reason,
            "record_sha256": hashlib.sha256(data).hexdigest()}
    target = path.parent / "tombstone.json"
    fd, temporary = tempfile.mkstemp(prefix=".tombstone-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(tomb, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    path.unlink()
    return tomb


def load(config, attempt_id, session=None):
    if not isinstance(attempt_id, str) or not re.fullmatch(r"[a-f0-9]{32}", attempt_id):
        raise AutoresearchError("invalid attempt identity")
    row = next((r for r in records(config) if r["id"] == attempt_id), None)
    if row is None:
        raise AutoresearchError(f"unknown attempt {attempt_id}")
    if session is not None and row["session"] != session_required(session):
        raise AutoresearchError("attempt belongs to another session")
    # A deep copy: the row came from _RECORD_CACHE, and settle()/checkpoint()
    # mutate what they load. save() pops the cache entry before writing, so
    # the copy is what reaches disk -- but a caller that mutates and then
    # refuses (an exception between load and save) must not leave a state in
    # the cache that never existed on disk.
    return copy.deepcopy(row)


def save(config, row):
    target = directory(config) / row["id"] / "record.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    # Drop the cache entry before writing (load() hands out a deep copy since
    # the cache landed, but reserve() still reads rows by reference): a save
    # that fails before the rename must not leave the previous bytes looking
    # like this row, and the re-read below repopulates it from what landed.
    _RECORD_CACHE.pop(target, None)
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
        # Re-read the bytes we just put on disk, so the next records() call
        # serves what a cold read would -- the cache is an accelerator, never
        # a second copy of the truth.
        _cached_row(target)
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
        # Declared concurrency ceiling; loop._map shares DEFAULT_MAX_PARALLEL.
        parallel = config.coordinator.get("max_parallel", DEFAULT_MAX_PARALLEL)
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
        out = {"records": records(config), "problems": problems(config),
               "tombstones": []}
        try:
            out["tombstones"] = tombstones(config)
        except AutoresearchError as exc:
            # A corrupt tombstone feeds the ceiling, so metered commands must
            # still refuse -- but inspection names it rather than dying.
            out["tombstone_problems"] = [str(exc)]
        row = out
    elif args.attempt_action == "show":
        row = load(config, args.id)
    elif args.attempt_action == "reconcile":
        row = reconcile(config, args.id, args.session, args.charged_runs,
                        args.reason)
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
    for name in ("list", "show", "checkpoint", "reconcile"):
        child = subs.add_parser(name)
        if name not in ("list",):
            child.add_argument("id")
        if name == "checkpoint":
            child.add_argument("--note", required=True)
            child.add_argument("--confirm-inactive", action="store_true")
        if name == "reconcile":
            child.add_argument("--charged-runs", type=int, required=True,
                               help="runs this attempt consumed and is not counted "
                                    "anywhere else; 0 is a real answer when you can "
                                    "confirm it, never a default")
            child.add_argument("--reason", required=True)
        child.set_defaults(func=_command, command="attempt")
