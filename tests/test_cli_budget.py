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


def test_overrun_counts_only_current_claim_attribution(sandbox, store, capsys):
    import json

    make_entry(store, "Q1", claim=Claim(session="coordinator", at=NOW, why="",
                                        budget="", max_runs=2, max_hours=4.0))
    sandbox.paths.iterations.mkdir(parents=True, exist_ok=True)
    rows = [
        {"session": "coordinator", "claim_at": OLD, "runs": 3},
        {"session": "other", "claim_at": NOW, "runs": 3},
        {"session": "coordinator", "runs": 3},
        {"session": "coordinator", "claim_at": NOW, "runs": 2},
    ]
    for number, row in enumerate(rows):
        (sandbox.paths.iterations / f"{number:04}.json").write_text(json.dumps(
            {"runs": row["runs"], "runs_by_entry": {"Q1": row}}))
    append(sandbox.paths.runs / "old.jsonl", RunRecord(
        entry="Q1", session="coordinator", started=dt.datetime.fromisoformat(OLD).timestamp(),
        metrics={}))
    assert cli.main(["--domain", str(sandbox.paths.root), "budget"]) == 0
    assert "OVERRUN" not in capsys.readouterr().out
    (sandbox.paths.iterations / "0004.json").write_text(json.dumps(
        {"runs": 1, "runs_by_entry": {"Q1": {
            "session": "coordinator", "claim_at": NOW, "runs": 1}}}))
    assert cli.main(["--domain", str(sandbox.paths.root), "budget"]) == 0
    assert "OVERRUN: max_runs 3 > 2" in capsys.readouterr().out


def test_budget_shows_reservations_in_domain_and_claim(sandbox, store, capsys):
    from autoresearch import attempts
    from autoresearch.claims import Claims

    make_entry(store, "Q1")
    Claims(store, sandbox, "owner").claim("Q1", why="reservation accounting")
    reserved = attempts.reserve(sandbox, "Q1", "owner", 30, units=2)
    assert cli.main(["--domain", str(sandbox.paths.root), "budget"]) == 0
    output = capsys.readouterr().out
    domain, live_claim = output.split("1 live claim(s)")
    assert next(line for line in domain.splitlines() if line.strip().startswith("runs")).split()[1] == "2"
    assert next(line for line in live_claim.splitlines() if line.strip().startswith("runs")).split()[1] == "2"
    attempts.settle(sandbox, reserved["id"], "owner", "failed")
    assert cli.main(["--domain", str(sandbox.paths.root), "budget"]) == 0
    domain = capsys.readouterr().out.split("1 live claim(s)")[0]
    assert next(line for line in domain.splitlines() if line.strip().startswith("runs")).split()[1] == "2"


def test_rank_accounts_for_reserved_and_failed_execution(sandbox, store, capsys):
    from autoresearch import attempts
    from autoresearch.claims import Claims

    make_entry(store, "Q1")
    make_entry(store, "Q2", cost=399, impact=1)
    Claims(store, sandbox, "owner").claim("Q1", why="reservation accounting")
    reserved = attempts.reserve(sandbox, "Q1", "owner", 30, units=2)
    args = ["--domain", str(sandbox.paths.root), "rank", "--top", "1"]
    assert cli.main(args) == 0
    assert "Q2" not in capsys.readouterr().out.split("shortlist (top 1):")[1]
    attempts.settle(sandbox, reserved["id"], "owner", "failed")
    assert cli.main(args) == 0
    assert "Q2" not in capsys.readouterr().out.split("shortlist (top 1):")[1]
