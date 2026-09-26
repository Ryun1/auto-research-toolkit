import time

import pytest

from autoresearch.claims import Claims, Lock
from autoresearch.entries import Event
from autoresearch.errors import ClaimError
from conftest import make_entry


def test_lock_names_its_holder(sandbox, tmp_path):
    """H32: a lock with no holder cannot be diagnosed and wedges every agent."""
    path = tmp_path / "lock"
    with Lock(path, "session-a"):
        with pytest.raises(ClaimError, match="held by session-a"):
            Lock(path, "session-b", timeout=0.1).acquire()


def test_lock_refuses_release_by_a_non_holder(tmp_path):
    """H57/H59: teardown with no ownership check removed other sessions' work."""
    path = tmp_path / "lock"
    Lock(path, "a").acquire()
    with pytest.raises(ClaimError, match="not by 'b'"):
        Lock(path, "b").release()


def test_stale_lock_is_stolen_and_says_so(tmp_path):
    path = tmp_path / "lock"
    Lock(path, "dead-session").acquire()
    time.sleep(0.02)
    lock = Lock(path, "live-session", stale_after=0.01).acquire()
    assert lock.stole is not None and lock.stole.holder == "dead-session"


def test_claim_gets_a_ceiling_even_when_none_is_named(sandbox, store):
    """H125: `--budget` was free text nothing read, so a claim could be worked
    indefinitely."""
    make_entry(store)
    entry = Claims(store, sandbox, "worker").claim("Q1", why="top of queue")
    assert entry.claim.max_runs and entry.claim.max_hours
    assert entry.status == "in-progress"


def test_second_claim_on_a_held_entry_is_refused(sandbox, store):
    make_entry(store)
    Claims(store, sandbox, "a").claim("Q1")
    with pytest.raises(ClaimError, match="already held by 'a'"):
        Claims(store, sandbox, "b").claim("Q1")


def test_release_requires_a_reason_and_only_by_the_holder(sandbox, store):
    make_entry(store)
    Claims(store, sandbox, "a").claim("Q1")
    with pytest.raises(ClaimError, match="requires a reason"):
        Claims(store, sandbox, "a").release("Q1", why="")
    with pytest.raises(ClaimError, match="not by 'b'"):
        Claims(store, sandbox, "b").release("Q1", why="mine now")
    entry = Claims(store, sandbox, "a").release("Q1", why="needs a measuring session")
    assert entry.status == "queued" and entry.claim is None
    assert any(ev.kind == "released" for ev in entry.history)


def test_reap_refuses_a_live_claim(sandbox, store):
    make_entry(store)
    Claims(store, sandbox, "a").claim("Q1")
    with pytest.raises(ClaimError, match="presumed alive"):
        Claims(store, sandbox, "reaper").reap("Q1")


def test_reap_repairs_an_orphan_without_touching_live_claims(sandbox, store):
    make_entry(store, "Q1")
    make_entry(store, "Q2")
    orphan = store.load("Q1")
    orphan.status = "in-progress"
    orphan.history.append(Event(
        "2026-09-25T12:00:00+00:00", "released", "old-session", "settled"))
    store.save(orphan)
    Claims(store, sandbox, "b").claim("Q2")
    reaper = Claims(store, sandbox, "reaper")

    candidates = dict((entry.id, age) for entry, age in
                      reaper.reapable(ttl_hours=0))
    assert candidates["Q1"] is None and candidates["Q2"] > 0
    before = [event.kind for event in orphan.history]
    _, former, _ = reaper.reap("Q1", ttl_hours=0)

    assert former is None
    repaired = store.load("Q1")
    assert repaired.status == "queued" and repaired.claim is None
    assert [event.kind for event in repaired.history[:len(before)]] == before
    repair = repaired.history[-1]
    assert repair.kind == "orphan-repaired" and repair.who == "reaper"
    assert "no live claim" in repair.detail
    live = store.load("Q2")
    assert live.status == "in-progress" and live.claim.session == "b"
    with pytest.raises(ClaimError, match="presumed alive"):
        reaper.reap("Q2")


def test_reap_cli_lists_and_repairs_an_orphan(sandbox, store, capsys):
    from autoresearch import cli, render
    from autoresearch.entries import Event

    make_entry(store, status="in-progress", history=[Event(
        "2026-09-25T12:00:00+00:00", "released", "old-session", "settled")])
    render.write_views(sandbox, store.all())

    assert cli.main(["--domain", str(sandbox.paths.root), "reap"]) == 0
    output = capsys.readouterr().out
    assert "1 reapable record(s)" in output
    assert "orphan" in output and "no live claim" in output
    assert cli.main(["--domain", str(sandbox.paths.root), "reap", "Q1"]) == 0
    assert "orphan repaired to queued" in capsys.readouterr().out
    repaired = store.load("Q1")
    assert repaired.status == "queued" and repaired.claim is None
    assert [event.kind for event in repaired.history][-2:] == [
        "in-progress->queued", "orphan-repaired"]
    assert "no live claim" in repaired.history[-1].detail


def test_reap_frees_one_claim_only(sandbox, store):
    """H83: reaping was all-or-nothing, so freeing one dead session's claim
    released every other claim past the same TTL."""
    make_entry(store, "Q1")
    make_entry(store, "Q2")
    Claims(store, sandbox, "a").claim("Q1")
    Claims(store, sandbox, "b").claim("Q2")
    reaper = Claims(store, sandbox, "reaper")
    assert len(reaper.reapable(ttl_hours=0)) == 2
    entry, former, _ = reaper.reap("Q1", ttl_hours=0)
    assert former == "a" and entry.claim is None
    assert store.load("Q2").claim.session == "b"        # untouched


def test_overrun_reports_the_meter_that_stopped_the_work(sandbox, store):
    make_entry(store)
    claims = Claims(store, sandbox, "a")
    claims.claim("Q1", max_runs=2, max_hours=10)
    assert claims.overrun("Q1", runs_spent=1) is None
    meter, spent, ceiling = claims.overrun("Q1", runs_spent=3)
    assert (meter, spent, ceiling) == ("max_runs", 3, 2)


def test_gate_mutation_is_owner_only_atomic_and_recorded(sandbox, store):
    import json

    from autoresearch.gates import Gate, update
    make_entry(store, gates=[Gate("correctness")])
    owner = Claims(store, sandbox, "a")
    owner.claim("Q1")
    def change(e):
        return update(e, "correctness", "passed", ["evidence.json"])
    with pytest.raises(ClaimError):
        Claims(store, sandbox, "b").mutate("Q1", "gate-updated", "verified", change)
    assert store.load("Q1").readiness == "pending"
    owner.mutate("Q1", "gate-updated", "verified", change)
    back = store.load("Q1")
    assert back.readiness == "ready"
    audit = json.loads(back.history[-1].detail)
    assert audit["changes"]["gates"]["before"][0]["state"] == "pending"
    assert audit["changes"]["gates"]["after"][0]["evidence"] == ["evidence.json"]


def test_release_pinned_to_prior_claim_does_not_release_reassignment(sandbox, store):
    make_entry(store)
    owner = Claims(store, sandbox, "a")
    first = owner.claim("Q1")
    owner.release("Q1", "yielding")
    second = owner.claim("Q1")
    second.claim.at = "2099-01-01T00:00:00+00:00"
    store.save(second)
    with pytest.raises(ClaimError, match="changed since assignment"):
        owner.release("Q1", "late result", expected_claim_at=first.claim.at)
    assert store.load("Q1").claim.at == second.claim.at


def test_failed_gate_change_leaves_persisted_record_untouched(sandbox, store):
    from autoresearch.errors import SchemaError
    from autoresearch.gates import Gate, update
    make_entry(store, gates=[Gate("correctness")])
    before = store.path("Q1").read_bytes()
    with pytest.raises(SchemaError):
        Claims(store, sandbox, "curator").mutate(
            "Q1", "gate-updated", "bad evidence",
            lambda e: update(e, "correctness", "passed", []))
    assert store.path("Q1").read_bytes() == before
