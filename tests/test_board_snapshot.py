"""`board.snapshot`: the board's facts as data (#49).

`ar board` and `ar board --json` are two projections of one document, so the
local UI that reads the document cannot show a different board."""
import json

from autoresearch import board, cli, evaluator, render
from autoresearch.claims import Claims
from autoresearch.runs import RunRecord, append
from conftest import make_entry


def _ar(sandbox, *args):
    return cli.main(["--domain", str(sandbox.paths.root), *args])


def _row(run_id, ops, sha, entry=None, started=1.0, status="ok"):
    return RunRecord(id=run_id, metrics={"ops": ops, "peak": 1.0}, session="s",
                     entry=entry, started=started, status=status,
                     provenance={"host": "local", "evaluator_sha": sha,
                                 "benchmark_revision": ""})


def _populate(sandbox, store):
    make_entry(store, "Q1")
    make_entry(store, "Q2", scaffold=True)
    make_entry(store, "Q3")
    entry = Claims(store, sandbox, "alice").claim("Q3", why="t", max_hours=2)
    entry.claim.at = "2026-01-01T00:00:00+00:00"
    store.save(entry)
    current = evaluator.identity(sandbox)
    path = sandbox.paths.runs / "s.jsonl"
    append(path, _row("old", 5.0, "0ld", "Q1", started=1))
    append(path, _row("fix", 1.0, current, "Q2", started=2))
    append(path, _row("new", 900.0, current, "Q1", started=3))
    append(path, _row("bad", 0.0, current, None, started=4, status="invalid"))


def test_the_json_board_carries_what_the_text_board_shows(sandbox, store, capsys):
    _populate(sandbox, store)
    assert _ar(sandbox, "board", "--json") == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["best"]["objective"] == 900.0
    assert doc["best"]["entry"] == "Q1" and doc["best"]["run"] == "new"
    assert doc["best"]["met"] is True and doc["best"]["unmet_required_gates"] == []
    assert doc["excluded_best"]["run"] == "fix"
    assert "scaffold" in doc["excluded_best"]["reason"]
    assert doc["epoch"]["current"] is True and doc["epoch_selected"] is False
    [old] = doc["other_epochs"]
    assert old["evaluator"] == "0ld" and old["best"]["run"] == "old"
    research = next(t for t in doc["tracks"] if t["name"] == "research")
    [claim] = research["live_claims"]
    assert claim["id"] == "Q3" and claim["overrun"]["meter"] == "max_hours"
    assert [h["id"] for h in research["queued_heads"]] == ["Q1", "Q2"]
    assert doc["measurements"]["valid"] == 3 and doc["measurements"]["rows"] == 4
    assert doc["measurements"]["newest"][-1]["status"] == "invalid"
    assert doc["holdout"] is None and doc["target_error"] is None


def test_the_text_board_is_the_snapshot_rendered(sandbox, store, capsys):
    _populate(sandbox, store)
    assert _ar(sandbox, "board") == 0
    printed = capsys.readouterr().out
    target = sandbox.goal.target.resolve(cli._make_probe(sandbox))
    assert printed == render.board_document(board.snapshot(sandbox, target=target))


def test_the_json_board_selects_an_epoch_and_refuses_an_unknown_one(sandbox, store,
                                                                   capsys):
    _populate(sandbox, store)
    assert _ar(sandbox, "board", "--json", "--epoch", "1") == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["best"]["run"] == "old" and doc["epoch_selected"] is True
    assert _ar(sandbox, "board", "--json", "--epoch", "7") == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "no epoch" in captured.err


def test_a_non_finite_number_rides_as_null():
    assert board.finite({"a": float("inf"), "b": [float("nan"), 1.5]}) == {
        "a": None, "b": [None, 1.5]}
