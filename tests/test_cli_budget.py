"""`ar budget`: the meters a human reads.

The OVERRUN line must be the derivation the record enforces -- `Claims.overrun`
-- counting only runs spent under the claim it reports on, and naming every
meter a claim has passed, not just the one a re-derivation remembered.
"""
import datetime as dt

from autoresearch import cli
from autoresearch.entries import Claim
from autoresearch.runs import OK, RunRecord, append
from conftest import make_entry

OLD = "2026-01-01T00:00:00+00:00"
NOW = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def test_overrun_counts_only_runs_spent_under_this_claim(sandbox, store, capsys):
    """Runs recorded under earlier claims are not this claim's spend."""
    make_entry(store, "Q1", claim=Claim(session="s1", at=NOW, why="",
                                        budget="", max_runs=2, max_hours=4.0))
    for session in ("s0", "s0", "s0", "s1"):
        append(sandbox.paths.runs / f"{session}.jsonl",
               RunRecord(metrics={"ops": 5.0, "peak": 1.0}, session=session,
                         entry="Q1", status=OK))
    assert cli.main(["--domain", str(sandbox.paths.root), "budget"]) == 0
    assert "OVERRUN" not in capsys.readouterr().out


def test_overrun_names_the_meter_and_the_ceiling(sandbox, store, capsys):
    """A claim past its wall clock is an overrun even with runs to spare."""
    make_entry(store, "Q1", claim=Claim(session="s1", at=OLD, why="",
                                        budget="", max_runs=2, max_hours=1.0))
    append(sandbox.paths.runs / "s1.jsonl",
           RunRecord(metrics={"ops": 5.0, "peak": 1.0}, session="s1",
                     entry="Q1", status=OK))
    assert cli.main(["--domain", str(sandbox.paths.root), "budget"]) == 0
    assert "OVERRUN: max_hours" in capsys.readouterr().out
