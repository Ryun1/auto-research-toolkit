"""The knowledge graph's documents (#51): nodes and links, and one entry's
side panel, as the `ar ui` server hands them to the page."""
import json

from autoresearch import evaluator, runs
from autoresearch.claims import Claims
from autoresearch.runs import RunRecord, append
from autoresearch.ui import graph
from autoresearch.ui.api import Api
from conftest import close, make_entry, memo


def _row(run_id, ops, entry, started, status="ok", sha=None):
    return RunRecord(id=run_id, metrics={"ops": ops, "peak": 1.0}, session="s",
                     entry=entry, started=started, status=status,
                     provenance={"host": "local", "evaluator_sha": sha or "x",
                                 "benchmark_revision": ""})


def test_the_graph_carries_every_entry_its_links_and_claims(sandbox, store):
    make_entry(store, "Q1", hypothesis="the first")
    make_entry(store, "Q2", sources=["Q1", "inbox/x.md"])
    make_entry(store, "H1", track="harness", title="loose")
    Claims(store, sandbox, "alice").claim("Q2", why="t")
    doc = graph.graph(sandbox)
    by_id = {n["id"]: n for n in doc["nodes"]}
    assert set(by_id) == {"Q1", "Q2", "H1"}
    assert doc["links"] == [{"src": "Q1", "dst": "Q2", "kind": "cites"}]
    assert by_id["Q1"]["linked"] and not by_id["H1"]["linked"]
    assert by_id["Q2"]["claim"]["session"] == "alice"
    assert by_id["Q1"]["hypothesis"] == "the first"
    assert doc["statuses"]["queued"]["fill"] and doc["statuses"]["confirmed"]["terminal"]
    json.dumps(doc)


def test_the_panel_has_the_memo_links_paths_runs_and_history(sandbox, store):
    make_entry(store, "Q1")
    make_entry(store, "Q2", hypothesis="h", sources=["Q1", "bin/tool", "inbox/gone.md"])
    make_entry(store, "Q3", parent="Q2")
    close(sandbox, store, store.load("Q2"))
    current = evaluator.identity(sandbox)
    path = sandbox.paths.runs / "s.jsonl"
    append(path, _row("old", 9.0, "Q2", 1, sha="0ld"))
    append(path, _row("good", 5.0, "Q2", 2, sha=current))
    append(path, _row("bad", 0.0, "Q2", 3, status="invalid", sha=current))
    append(path, _row("other", 4.0, "Q1", 4, sha=current))
    (sandbox.paths.root / "bin" / "tool").write_text("x")
    doc = Api(sandbox).get("entry", "Q2")
    assert doc["entry"]["hypothesis"] == "h" and doc["terminal"] is True
    assert doc["memo"] == {"path": "inbox/Q2.md", "exists": True,
                           "text": "evidence", "size": 8}
    assert [u["id"] for u in doc["upstream"]] == ["Q1"]
    assert doc["downstream"] == [{"id": "Q3", "kind": "parent", "title": "Q3 title"}]
    assert doc["paths"] == [{"path": "bin/tool", "exists": True},
                            {"path": "inbox/gone.md", "exists": False}]
    rows = {r["id"]: r for r in doc["runs"]}
    assert set(rows) == {"old", "good", "bad"}
    assert rows["good"]["standing"] == "ok" and rows["good"]["current"]
    assert rows["bad"]["standing"] == "invalid"
    assert rows["old"]["current"] is False
    assert [h["kind"] for h in doc["history"]][-1]
    assert "token_sha" not in json.dumps(doc)
    assert Api(sandbox).get("entry", "Q9") is None


def test_a_memo_path_never_climbs_out_of_the_domain(sandbox, store, tmp_path):
    secret = tmp_path / "secret.md"
    secret.write_text("not yours")
    make_entry(store, "Q1")
    entry = close(sandbox, store, store.load("Q1"))
    entry.result.memo = "../secret.md"
    store.save(entry)
    doc = graph.entry(sandbox, "Q1", {})
    assert doc["memo"]["exists"] is False and doc["memo"]["text"] is None


def test_row_standing_keeps_excluded_apart_from_ok(sandbox, store):
    make_entry(store, "Q1", scaffold=True)
    make_entry(store, "Q2")
    rows = [_row("fix", 1.0, "Q1", 1), _row("real", 9.0, "Q2", 2),
            _row("broke", 3.0, "Q2", 3, status="failed")]
    kinds = runs.standing(store, rows, sandbox.goal)
    assert kinds["real"] == ("ok", "")
    assert kinds["fix"][0] == "excluded" and "scaffold" in kinds["fix"][1]
    assert kinds["broke"][0] == "failed"


def test_a_long_memo_is_listed_not_inlined(sandbox, store):
    make_entry(store, "Q1")
    close(sandbox, store, store.load("Q1"))
    memo(sandbox, "inbox/Q1.md", "x" * (graph.MEMO_MAX + 1))
    doc = graph.entry(sandbox, "Q1", {})
    assert doc["memo"]["text"] is None and doc["memo"]["size"] == graph.MEMO_MAX + 1
