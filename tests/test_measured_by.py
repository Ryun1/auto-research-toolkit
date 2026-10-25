"""#43: a second measurement is a second *claim or slot*, not a second name.

Suspect release (#39) and `confirm_independently` both counted distinct
`record.session` strings, and `ar measure` stamps that from a free flag --
so one agent could re-measure under `--session other` and release its own
suspect row, or be its own independent confirmation. Core now stamps
`provenance.measured_by` itself: the slot a candidate was measured in, or
the claim the caller proved it holds. A row that can show neither is
`unclaimed` and never counts as a second source.
"""
import json

from autoresearch import budget as budget_mod
from autoresearch import cli, runs
from autoresearch.goal import Goal
from autoresearch.runs import RunRecord, read_all
from autoresearch.workspaces import Pool
from conftest import make_entry


def _ar(config, *args):
    return cli.main(["--domain", str(config.paths.root), *args])


def _token(capsys):
    return capsys.readouterr().out.split("token ")[1].split()[0]


def test_a_candidate_row_is_measured_by_its_slot(sandbox):
    with Pool(sandbox, "t") as pool:
        slot = pool.acquire("it1-Q1").path
        assert _ar(sandbox, "--session", "anything", "measure", "--candidate",
                   str(slot)) == 0
        [row] = read_all(slot / "data/runs")
    assert row.provenance["measured_by"] == "slot:it1-Q1"


def test_a_row_under_a_proven_claim_is_measured_by_the_claim(sandbox, store, capsys):
    make_entry(store, "Q1")
    assert _ar(sandbox, "--session", "a", "claim", "Q1", "--why", "t") == 0
    token = _token(capsys)
    claim_at = store.load("Q1").claim.at
    assert _ar(sandbox, "--session", "a", "--claim-token", token, "measure",
               "--entry", "Q1") == 0
    [row] = read_all(sandbox.paths.runs)
    assert row.provenance["measured_by"] == f"claim:a@{claim_at}"


def test_a_row_with_no_proof_is_unclaimed(sandbox, store, capsys):
    make_entry(store, "Q1")
    assert _ar(sandbox, "--session", "a", "claim", "Q1", "--why", "t") == 0
    capsys.readouterr()
    assert _ar(sandbox, "--session", "other", "measure", "--entry", "Q1") == 0
    assert _ar(sandbox, "--session", "other", "measure") == 0
    assert {r.provenance["measured_by"] for r in read_all(sandbox.paths.runs)} \
        == {"unclaimed"}
    assert "unclaimed" in capsys.readouterr().out


def _goal():
    return Goal.from_dict({"goal": {"id": "g", "objective": "ops",
                                    "metrics": {"ops": {}}, "target": {"value": 1},
                                    "suspect": {"k": 10}}})


def _row(run_id, ops, config, session, by, started):
    provenance = {"host": "x"}
    if by is not None:
        provenance["measured_by"] = by
    return RunRecord(id=run_id, metrics={"ops": float(ops)}, session=session,
                     started=started, config={"knobs": config},
                     provenance=provenance)


def _ledger(*extra):
    return [_row("a", 1000, "a", "s", "slot:it1-Q1", 1),
            _row("b", 990, "b", "s", "slot:it2-Q2", 2),
            _row("c", 20, "c", "lucky", "slot:it3-Q3", 3), *extra]


def test_a_renamed_session_with_no_claim_does_not_release_a_suspect(store):
    rows = _ledger(_row("c2", 21, "c", "other", "unclaimed", 4))
    best, _ = runs.frontier(store, rows, _goal())
    assert best[1].id == "b"


def test_the_same_slot_under_another_name_does_not_release_it(store):
    rows = _ledger(_row("c2", 21, "c", "other", "slot:it3-Q3", 4))
    best, _ = runs.frontier(store, rows, _goal())
    assert best[1].id == "b"


def test_another_slot_releases_it(store):
    rows = _ledger(_row("c2", 21, "c", "lucky", "slot:it4-Q3", 4))
    best, _ = runs.frontier(store, rows, _goal())
    assert best[1].id == "c"


def test_independent_confirmation_counts_sources_not_names():
    goal = Goal.from_dict({"goal": {"id": "g", "objective": "ops",
                                    "metrics": {"ops": {}}, "target": {"value": 100}}})
    best = _row("best", 50, "c", "a", "slot:it1-Q1", 1)
    same_slot = _row("x", 50, "c", "b", "slot:it1-Q1", 2)
    unclaimed = _row("y", 50, "c", "c", "unclaimed", 3)
    other = _row("z", 50, "c", "a", "slot:it2-Q1", 4)
    assert not budget_mod.independent_confirmation(goal, (50.0, best),
                                                   [best, same_slot, unclaimed], 100)
    assert budget_mod.independent_confirmation(goal, (50.0, best),
                                               [best, other], 100)


def test_rows_from_before_the_stamp_keep_their_session_and_entry(store):
    """Legacy rows carry no `measured_by`; they are told apart by session and
    entry, exactly as before, so an existing ledger reads the same."""
    rows = _ledger(_row("c2", 21, "c", "other", None, 4))
    rows = [_row(r.id, r.metrics["ops"], r.config["knobs"], r.session, None,
                 r.started) for r in rows]
    best, _ = runs.frontier(store, rows, _goal())
    assert best[1].id == "c"


def test_measured_by_is_in_the_receipted_bytes(sandbox):
    """Core stamps it before the receipt digest, so a slot cannot edit it."""
    from autoresearch import evaluator
    with Pool(sandbox, "t") as pool:
        slot = pool.acquire("it1-Q1").path
        assert _ar(sandbox, "measure", "--candidate", str(slot)) == 0
        path = slot / "data/runs/cli.jsonl"
        row = json.loads(path.read_text())
        row["provenance"]["measured_by"] = "slot:elsewhere"
        record = RunRecord.from_dict(row)
    assert "does not match its receipt" in evaluator.receipt_problem(
        sandbox.paths.receipts, record)
