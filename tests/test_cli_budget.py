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


def test_exhausted_claim_is_not_overrun(sandbox, store, capsys):
    """Spending the allowance is legal; only exceeding it is an overrun."""
    make_entry(store, "Q1", claim=Claim(session="s1", at=NOW, why="",
                                        budget="", max_runs=2, max_hours=4.0))
    for _ in range(2):
        append(sandbox.paths.runs / "s1.jsonl",
               RunRecord(metrics={"ops": 5.0, "peak": 1.0}, session="s1",
                         entry="Q1", status=OK))
    assert cli.main(["--domain", str(sandbox.paths.root), "budget"]) == 0
    assert "OVERRUN" not in capsys.readouterr().out
    append(sandbox.paths.runs / "s1.jsonl",
           RunRecord(metrics={"ops": 5.0, "peak": 1.0}, session="s1",
                     entry="Q1", status=OK))
    assert cli.main(["--domain", str(sandbox.paths.root), "budget"]) == 0
    assert "OVERRUN: max_runs 3 > 2" in capsys.readouterr().out


def test_overrun_counts_recorded_iteration_consumption(sandbox, store, capsys, tmp_path):
    """H7: a claim can burn runs that leave no ledger row; the OVERRUN line
    must read the same consumption the campaign ceiling charges, not only
    rows that survived with their session field intact."""
    import json

    make_entry(store, "Q1", claim=Claim(session="coordinator", at=NOW, why="",
                                        budget="", max_runs=1, max_hours=4.0))
    sandbox.paths.iterations.mkdir(parents=True, exist_ok=True)
    (sandbox.paths.iterations / "0001.json").write_text(json.dumps(
        {"n": 1, "runs": 2, "runs_by_entry": {"Q1": {"session": "coordinator", "runs": 2}}}))
    assert cli.main(["--domain", str(sandbox.paths.root), "budget"]) == 0
    assert "OVERRUN: max_runs 2 > 1" in capsys.readouterr().out
    (sandbox.paths.iterations / "0001.json").write_text(json.dumps(
        {"runs": 2, "run_ids": ["retained", "retained"]}))
    append(sandbox.paths.runs / "retained.jsonl", RunRecord(
        id="retained", entry="Q1", session="it1-Q1", metrics={},
        provenance={"host": "test"}))
    assert cli.main(["--domain", str(sandbox.paths.root), "budget"]) == 0
    assert "OVERRUN" not in capsys.readouterr().out
