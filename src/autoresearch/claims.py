"""Claiming work: the mechanism that lets many agents share one corpus.

Claim-before-work is the single reason 92 concurrent sessions could drain one
queue in the harness this generalises, and it is ported wholesale. What is *not*
ported is how that harness decided whether a claim holder was still alive. It
inferred liveness from artefacts, and the debt log is mostly that inference
going wrong:

* the lock recorded no holder, so a crashed session wedged claiming for every
  agent on the machine (H32);
* the reaper could never reap a session that committed once and died (H30);
* it read a reservation commit as proof of life, forever (H114);
* and it kept claims alive because prose merely *cited* a session (H115);
* reaping was all-or-nothing across both queues, so freeing one dead session's
  claim released every other claim past the same TTL (H83);
* two definitions of "dead session" lived 8x apart in two files, neither in
  config (H107);
* and `claim-next` failed instantly while the lock was held, so agents burned
  cycles on retries (H92).

The fix is not a better inference. Under a coordinator, liveness is
**observed**: the coordinator owns the workspace pool and knows when a worker
finished, so a claim that outlives its iteration is freeable on sight -- the
pool is torn down in the coordinator's own finally, and QC checks that no
claim survived dispatch. TTL reaping survives for sessions running outside a
coordinator, where a claim's age is the only liveness fact on disk: it targets
one entry at a time, and it says which claim it took and why.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import time
from dataclasses import dataclass

from .entries import Claim, Event
from .errors import ClaimError


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="microseconds")


def _age_hours(iso: str) -> float:
    then = dt.datetime.fromisoformat(iso)
    if then.tzinfo is None:
        then = then.replace(tzinfo=dt.UTC)
    return (dt.datetime.now(dt.UTC) - then).total_seconds() / 3600.0


@dataclass
class LockInfo:
    holder: str
    pid: int
    at: str

    @property
    def age_hours(self) -> float:
        return _age_hours(self.at)


class Lock:
    """A named lock that records who holds it.

    H32 is the whole design note: a lock with no holder cannot be diagnosed and
    cannot be safely broken, so one crashed session wedges every other agent on
    the machine. This one names its holder, is stealable once demonstrably
    stale, and **says so loudly when it steals** -- a silent steal is how two
    sessions end up believing they hold the same claim.

    It also waits. H92: failing instantly while the lock is held made every
    agent retry in a loop, turning a 50 ms wait into a publish cycle.
    """

    def __init__(self, path, holder: str, timeout=30.0, stale_after=900.0):
        self.path = pathlib.Path(path)
        self.holder, self.timeout, self.stale_after = holder, timeout, stale_after
        self.stole: LockInfo | None = None

    def _read(self) -> LockInfo | None:
        try:
            return LockInfo(**json.loads(self.path.read_text()))
        except (OSError, ValueError, TypeError):
            return None

    def acquire(self) -> Lock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + self.timeout
        payload = json.dumps({"holder": self.holder, "pid": os.getpid(),
                              "at": _now_iso()})
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w") as fh:
                    fh.write(payload)
                return self
            except FileExistsError:
                current = self._read()
                if current and current.age_hours * 3600 > self.stale_after:
                    self.stole = current
                    self.path.unlink(missing_ok=True)
                    continue
                if time.time() >= deadline:
                    held_by = current.holder if current else "an unreadable lock"
                    raise ClaimError(
                        f"lock {self.path.name} held by {held_by} for "
                        f"{current.age_hours * 3600:.0f}s; waited {self.timeout:.0f}s. "
                        f"It becomes stealable after {self.stale_after:.0f}s.") from None
                time.sleep(0.05)

    def release(self) -> None:
        current = self._read()
        if current and current.holder != self.holder:
            raise ClaimError(
                f"refusing to release lock {self.path.name}: held by "
                f"{current.holder!r}, not by {self.holder!r}. "
                "H57/H59: teardown with no ownership check removed other "
                "sessions' work.")
        self.path.unlink(missing_ok=True)

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
        return False



def claim_state_problems(entry, machine) -> list[str]:
    """Report active-work records that no session can currently own."""
    if entry.status != "in-progress" or entry.claim is not None:
        return []

    detail = ""
    lifecycle = [event for event in entry.history
                 if "->" in event.kind or event.kind in (
                     "released", "reaped", "orphan-repaired")]
    if lifecycle:
        event = max(lifecycle, key=lambda item: item.at)
        settled = (event.kind in ("released", "reaped", "orphan-repaired")
                   or event.kind.endswith(f"->{machine.initial}"))
        if settled:
            detail = (f"; history records {event.kind} by {event.who!r} at "
                      f"{event.at}, so status and history disagree")
    return [f"{entry.id}: in-progress with no live claim{detail}; repair with "
            f"`ar reap {entry.id}`"]


class Claims:
    """Claim, release and reap, over an entry store."""

    def __init__(self, store, config, session: str):
        self.store, self.config, self.session = store, config, session
        self.lock_path = config.paths.claims / "lock"

    def _lock(self) -> Lock:
        return Lock(self.lock_path, self.session,
                    timeout=float(self.config.budgets.get("claim_lock_timeout", 30)),
                    stale_after=float(self.config.budgets.get(
                        "claim_lock_stale_seconds", 900)))

    # -- taking work -------------------------------------------------------

    def claim(self, entry_id: str, why: str = "", budget: str = "",
              max_runs=None, max_hours=None):
        """Take one named entry. Serialised, and refused if already held.

        A ceiling is not optional here. H125: a claim could be worked
        indefinitely because `--budget` was free text nothing read. `max_runs`
        and `max_hours` default from the domain's budgets, so every claim has a
        ceiling even when the caller does not name one.
        """
        with self._lock():
            entry = self.store.load(entry_id)
            track = self.config.track_for(entry_id)
            if entry.claim and entry.status != track.machine.initial:
                raise ClaimError(
                    f"{entry_id} is already held by {entry.claim.session!r} "
                    f"(since {entry.claim.at}). If that session is gone, "
                    f"`ar reap {entry_id}` frees it on a TTL.")
            entry.claim = Claim(
                session=self.session, at=_now_iso(), why=why, budget=budget,
                max_runs=int(max_runs if max_runs is not None
                             else self.config.budgets.get("claim_max_runs", 20)),
                max_hours=float(max_hours if max_hours is not None
                                else self.config.budgets.get(
                                    "claim_wall_clock_hours", 4)))
            entry.apply(track.machine, "in-progress", who=self.session, why=why)
            self.store.save(entry)
            return entry

    def release(self, entry_id: str, why: str, expected_claim_at=None, event=None):
        """Hand back an owned claim, optionally pinned to its original lease."""
        with self._lock():
            return self.release_locked(entry_id, why, expected_claim_at, event=event)

    def release_locked(self, entry_id: str, why: str, expected_claim_at=None, event=None):
        """Release under a caller-held `_lock`, without reacquiring it."""
        if not why.strip():
            raise ClaimError("releasing a claim requires a reason")
        entry = self.store.load(entry_id)
        self.require_owner(entry, expected_claim_at)
        track = self.config.track_for(entry_id)
        entry.apply(track.machine, track.machine.initial,
                    who=self.session, why=why)
        entry.history.append(Event(_now_iso(), "released", self.session, why))
        if event is not None:
            entry.history.append(event)
        entry.claim = None
        self.store.save(entry)
        return entry

    def require_owner(self, entry, expected_claim_at=None):
        if not entry.claim or entry.claim.session != self.session:
            holder = entry.claim.session if entry.claim else "nobody"
            raise ClaimError(
                f"{entry.id} is held by {holder!r}, not by {self.session!r}; "
                "a session may change only its own claim")
        if expected_claim_at is not None and entry.claim.at != expected_claim_at:
            raise ClaimError(f"{entry.id}: claim changed since assignment")

    def mutate(self, entry_id: str, kind: str, why: str, change,
               expected_claim_at=None):
        """Apply a record change atomically, preserving ownership and history.

        Unclaimed records may be curated; an active claim is owner-only.
        `change` receives the freshly loaded entry and must not save it itself.
        """
        if not why.strip():
            raise ClaimError("record mutation requires a reason")
        with self._lock():
            entry = self.store.load(entry_id)
            track = self.config.track_for(entry_id)
            if expected_claim_at is not None or (
                    entry.claim and not track.machine.status(entry.status).terminal):
                self.require_owner(entry, expected_claim_at)
            before = entry.to_dict()
            change(entry)
            after = entry.to_dict()
            changed = {key: {"before": before.get(key), "after": after.get(key)}
                       for key in before.keys() | after.keys()
                       if key != "history" and before.get(key) != after.get(key)}
            detail = json.dumps({"why": why, "changes": changed}, sort_keys=True)
            entry.history.append(Event(_now_iso(), kind, self.session, detail))
            entry.updated = _now_iso()
            self.store.save(entry)
            return entry

    # -- freeing abandoned work --------------------------------------------

    def reapable(self, ttl_hours=None) -> list:
        """Orphaned active records, plus claims whose holder exceeded TTL.

        The second predicate is a **death test**, and it is deliberately not
        the same predicate as any settling window. H107 found those two numbers
        living 8x apart in two files, read as one thing; they are both in config
        here and neither is derived from the other. Orphan age is ``None``:
        there is no holder whose silence can be measured.
        """
        ttl = float(ttl_hours if ttl_hours is not None
                    else self.config.budgets.get("claim_ttl_hours", 6))
        out = []
        for entry in self.store.all():
            track = self.config.track_for(entry.id)
            if not entry.claim and entry.status == "in-progress":
                out.append((entry, None))
            elif (entry.claim and not track.machine.status(entry.status).terminal
                  and _age_hours(entry.claim.at) > ttl):
                out.append((entry, _age_hours(entry.claim.at)))
        return out

    def reap(self, entry_id: str, ttl_hours=None, why: str = ""):
        """Repair an unowned active record, or free ONE abandoned claim.

        Returns ``(entry, former, age)``. ``former`` is ``None`` for an orphan;
        an actual holder is still governed by the TTL and is never inferred
        dead from missing liveness data. H83 keeps reaping targeted to one
        claim; H67 adds the structurally distinct, claim-less active record.
        """
        with self._lock():
            entry = self.store.load(entry_id)
            track = self.config.track_for(entry_id)
            if not entry.claim:
                if entry.status != "in-progress":
                    raise ClaimError(f"{entry_id} holds no claim to reap")
                reason = why or (
                    "repaired orphan: in-progress status had no live claim")
                entry.apply(track.machine, track.machine.initial,
                            who=self.session, why=reason)
                entry.history.append(Event(
                    _now_iso(), "orphan-repaired", self.session, reason))
                entry.claim = None
                self.store.save(entry)
                return entry, None, 0.0
            age = _age_hours(entry.claim.at)
            ttl = float(ttl_hours if ttl_hours is not None
                        else self.config.budgets.get("claim_ttl_hours", 6))
            if age <= ttl:
                raise ClaimError(
                    f"{entry_id} was touched {age:.2f} h ago, inside the "
                    f"{ttl:.2f} h TTL; its holder {entry.claim.session!r} is "
                    "presumed alive. Reaping a live claim gives two sessions "
                    "the same work.")
            former = entry.claim.session
            reason = why or f"reaped: {former} silent for {age:.1f} h (TTL {ttl} h)"
            entry.apply(track.machine, track.machine.initial,
                        who=self.session, why=reason)
            entry.history.append(Event(_now_iso(), "reaped", self.session, reason))
            entry.claim = None
            self.store.save(entry)
            return entry, former, age

    # -- budget state ------------------------------------------------------

    def overrun(self, entry_id: str, runs_spent: int = 0):
        """Whether a held claim has passed a ceiling it registered. H125."""
        entry = self.store.load(entry_id)
        if not entry.claim:
            return None
        hours = _age_hours(entry.claim.at)
        if entry.claim.max_hours and hours > entry.claim.max_hours:
            return ("max_hours", hours, entry.claim.max_hours)
        if entry.claim.max_runs and runs_spent > entry.claim.max_runs:
            return ("max_runs", runs_spent, entry.claim.max_runs)
        return None
