"""#22: a claim's silence is its holder's silence, not its age.

`ar reap` judged a claim silent from the claim's own timestamp, so it reaped
a provably.fast session that had committed a memo two hours earlier: "its
claim was silent but the session was not". And claims ran 27-47 h past a
2 h budget with only a curator's manual check noticing. The holder's last
sign of life -- an event it wrote on any entry, a run row it recorded, a
commit naming it -- now counts, and the board names overrun claims.
"""
import datetime as dt
import subprocess
import time

import pytest

from autoresearch import cli
from autoresearch.claims import Claims
from autoresearch.entries import Event
from autoresearch.errors import ClaimError
from autoresearch.runs import RunRecord, append
from conftest import make_entry

NOW = dt.datetime.now(dt.UTC)


def _iso(hours_ago):
    return (NOW - dt.timedelta(hours=hours_ago)).isoformat()


def _stale_claim(sandbox, store, entry_id="Q1", session="a", hours=10):
    make_entry(store, entry_id)
    entry = Claims(store, sandbox, session).claim(entry_id, why="t")
    entry.claim.at = _iso(hours)
    for event in entry.history:          # the claim was taken back then too
        event.at = _iso(hours)
    store.save(entry)
    return entry


def test_a_silent_holder_is_reaped(sandbox, store):
    _stale_claim(sandbox, store)
    entry, former, age = Claims(store, sandbox, "reaper").reap("Q1")
    assert former == "a" and age > 6


def test_an_event_the_holder_wrote_elsewhere_keeps_it_alive(sandbox, store):
    _stale_claim(sandbox, store)
    other = make_entry(store, "Q2")
    other.history.append(Event(_iso(0.5), "amended", "a", "still working"))
    store.save(other)
    with pytest.raises(ClaimError, match="Q2"):
        Claims(store, sandbox, "reaper").reap("Q1")
    assert [e.id for e, _ in Claims(store, sandbox, "r").reapable()] == []


def test_a_run_row_the_holder_recorded_keeps_it_alive(sandbox, store):
    _stale_claim(sandbox, store)
    append(sandbox.paths.runs / "a.jsonl", RunRecord(
        id="recent", metrics={"ops": 1.0, "peak": 1.0}, session="a",
        started=time.time() - 600, finished=time.time() - 590,
        provenance={"host": "x"}))
    with pytest.raises(ClaimError, match="run row recent"):
        Claims(store, sandbox, "reaper").reap("Q1")


def test_a_commit_naming_the_holder_keeps_it_alive(sandbox, store):
    root = sandbox.paths.root
    _stale_claim(sandbox, store, session="scout-3")
    for args in (("init", "-q"), ("add", "-A"),
                 ("-c", "user.name=T", "-c", "user.email=t@example.invalid",
                  "commit", "-qm", "Q1 memo [scout-3]")):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    with pytest.raises(ClaimError, match="commit"):
        Claims(store, sandbox, "reaper").reap("Q1")


def test_an_old_sign_of_life_does_not_count(sandbox, store):
    _stale_claim(sandbox, store)
    other = make_entry(store, "Q2")
    other.history.append(Event(_iso(9), "amended", "a", "long ago"))
    store.save(other)
    _, former, _ = Claims(store, sandbox, "reaper").reap("Q1")
    assert former == "a"


def test_the_board_names_an_overrun_claim(sandbox, store, capsys):
    make_entry(store, "Q1")
    entry = Claims(store, sandbox, "a").claim("Q1", why="t", max_hours=2)
    entry.claim.at = _iso(30)
    store.save(entry)
    assert cli.main(["--domain", str(sandbox.paths.root), "board"]) == 0
    line = next(l for l in capsys.readouterr().out.splitlines()
                if "Q1" in l and "a" in l and "since" in l)
    assert "OVERRUN" in line and "max_hours" in line
