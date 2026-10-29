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


# -- pinned against the pre-#49 board --------------------------------------

def _golden_domain(sandbox, store):
    """Every section of the board, with nothing that moves with the clock or
    the evaluator: an overrun on runs, not hours, and a frontier whose other
    epochs are named by a fixed sha or as unpinned."""
    from conftest import close, make_skill
    root = sandbox.paths.root
    toml = root / "domain.toml"
    toml.write_text(toml.read_text().replace(
        "[commands]\n", '[commands]\nholdout = "holdout/bin"\n')
        + '\n[holdout]\nfiles = ["holdout/factor"]\n')
    (root / "holdout").mkdir()
    (root / "holdout/bin").write_text("#!/bin/sh\nexit 1\n")
    (root / "holdout/factor").write_text("1")
    from autoresearch.config import DomainConfig
    sandbox = DomainConfig.load(root)
    make_entry(store, "Q1", title="a confirmed lead " + "x" * 90)
    make_entry(store, "Q2", parent="Q1")
    make_entry(store, "Q3", scaffold=True)
    make_entry(store, "Q4")
    make_entry(store, "H1", track="harness", title="a harness defect")
    make_entry(store, "H2", track="harness", title="another")
    close(sandbox, store, store.load("Q1"))
    close(sandbox, store, store.load("H1"), verdict="fixed")
    entry = Claims(store, sandbox, "alice").claim("Q2", why="t", max_runs=1)
    entry.claim.at = "2026-01-01T00:00:00+00:00"
    entry.claim.max_hours = None
    store.save(entry)
    current = evaluator.identity(sandbox)
    path = sandbox.paths.runs / "s.jsonl"
    append(path, _row("old", 5.0, "0ld", "Q1", started=1))
    append(path, RunRecord(id="pre", metrics={"ops": 3.0, "peak": 1.0}, session="s",
                           entry="Q1", started=2, provenance={"host": "local"}))
    append(path, _row("fix", 1.0, current, "Q3", started=3))
    append(path, _row("n1", 900.0, current, "Q2", started=4))
    append(path, _row("n2", 700.5, current, "Q2", started=5))
    append(path, _row("n3", 650.0, current, "Q2", started=6, status="failed"))
    append(path, _row("n4", 0.0, current, None, started=7, status="invalid"))
    (sandbox.paths.runs / "foreign.jsonl").write_text('{"weird": 1}\n')
    sandbox.paths.iterations.mkdir(parents=True, exist_ok=True)
    (sandbox.paths.iterations / "1.json").write_text(json.dumps(
        {"n": 1, "stop": "budget", "stop_detail": "out of runs",
         "phases": [{"name": "rank", "did": 2}, {"name": "qc", "error": "boom"}]}))
    (sandbox.paths.iterations / "2.json").write_text(json.dumps(
        {"n": 2, "kind": "capture"}))
    make_skill(sandbox, "a-skill", cites=["Q1"])
    return sandbox


GOLDEN = """\
# toy — board

## Goal: beat-toy-target
objective  round(ops) * peak  (minimise)
derived    break_even = ops / peak
target     6,000,000  [moving]
best       700  (Q2)   distance -99.99%  ** GOAL MET **
excluded  fix on Q3  objective 1: scaffold entry Q3 -- a wiring check is not a finding

## Hypothesis Queue — 4 entries read
  confirmed:1  in-progress:1  queued:2
  readiness: unconfigured:4
  live claims: 1
    Q2  alice  since 2026-01-01T00:00  [unconfigured]
  queued heads (2 queued):
    Q3  Q3 title  [unconfigured]
    Q4  Q4 title  [unconfigured]

## Harness Debt — 2 entries read
  fixed:1  queued:1
  readiness: unconfigured:2
  live claims: 0
  queued heads (1 queued):
    H2  another  [unconfigured]

## Iterations — 2 recorded, last loop iteration 1 (1 out-of-band record(s) since)
  stop        budget — out of runs
  phases      rank(2) qc(0)
  problem     qc: boom

## Measurements — 7 rows read, 5 valid, 1 foreign rows skipped (pre-adoption schema)
  n2  ok       Q2     ops=700.5 peak=1
  n3  failed   Q2     ops=650 peak=1
  n4  invalid  —      ops=0 peak=1

## Skills — 1 skill(s) read, 0 stale, 1 undistilled

holdout    not consulted on the best config  (0 consultation(s) this epoch)

## Previous epochs
  epoch unpinned: rows from before the stamp; 1 row(s)  best 3 (Q1)
  epoch 1: evaluator 0ld; 1 row(s)  best 5 (Q1)
  (`ar board --epoch N` reads one; `ar epochs` lists what a move may have invalidated)
"""


def test_the_board_is_byte_identical_to_the_pre_snapshot_board(sandbox, store, capsys):
    """#49's done-when: `ar board` printed exactly this before the facts
    became a document (generated by the 0.13.0 cmd_board on this fixture)."""
    config = _golden_domain(sandbox, store)
    assert _ar(config, "board") == 0
    assert capsys.readouterr().out == GOLDEN
