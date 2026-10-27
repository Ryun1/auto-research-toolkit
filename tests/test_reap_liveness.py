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


def _commit(root, message):
    for args in (("init", "-q"), ("add", "-A"),
                 ("-c", "user.name=T", "-c", "user.email=t@example.invalid",
                  "commit", "-q", "--allow-empty", "-m", message)):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def test_a_commit_naming_the_holder_keeps_it_alive(sandbox, store):
    _stale_claim(sandbox, store, session="scout-3")
    _commit(sandbox.paths.root, "Q1 memo [scout-3]")
    with pytest.raises(ClaimError, match=r"commit \w+ marking \[scout-3\]"):
        Claims(store, sandbox, "reaper").reap("Q1")


@pytest.mark.parametrize("message, matched", [
    ("B10 audit\n\nSession: moon", "marking Session: moon"),
    ("moon closes in on Q1", "naming Q1 and moon"),
    ("Q1 closed by moon.", "naming Q1 and moon"),      # a sentence ends in
    ("moon closes Q1.", "naming Q1 and moon"),         # a full stop
])
def test_a_marked_commit_or_one_naming_holder_and_entry_keeps_it_alive(
        sandbox, store, message, matched):
    _stale_claim(sandbox, store, session="moon")
    _commit(sandbox.paths.root, message)
    with pytest.raises(ClaimError, match=matched):
        Claims(store, sandbox, "reaper").reap("Q1")


@pytest.mark.parametrize("message", [
    "B10 withstood; moon memo corrected",       # the holder's name as prose
    "Q1 closed superseded",                     # the entry, not the holder
    "moon notes on Q10",                        # Q10 is not Q1
    "moon.v2 notes on Q1",                      # moon.v2 is not moon
])
def test_a_commit_that_only_mentions_the_name_is_not_a_sign_of_life(
        sandbox, store, message):
    """SLH-DSA-128 H6: a session named `moon` was kept alive by "moon memo
    corrected", a commit that never touched its claim, and the message said
    the commit named the entry."""
    _stale_claim(sandbox, store, session="moon")
    _commit(sandbox.paths.root, message)
    _, former, _ = Claims(store, sandbox, "reaper").reap("Q1")
    assert former == "moon"


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
    line = next(text for text in capsys.readouterr().out.splitlines()
                if "Q1" in text and "a" in text and "since" in text)
    assert "OVERRUN" in line and "max_hours" in line
