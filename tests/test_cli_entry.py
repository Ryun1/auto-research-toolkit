"""`ar entry show` and `ar entry list` — the read surface agents script around.

`show` defaults to the current state because the append-only history is the
bulk of an old entry's JSON and a coordinator brief already excludes it;
`list --json` is the projection that lets an agent pick work without an N+1
of `entry show`.
"""
import json

from autoresearch import cli
from autoresearch.entries import Claim
from conftest import close, make_entry

NOW = "2026-01-01T00:00:00+00:00"


def _ar(sandbox, *args):
    return cli.main(["--domain", str(sandbox.paths.root), *args])


def test_show_defaults_to_state_without_history(sandbox, store, capsys):
    entry = close(sandbox, store, make_entry(store, "Q1"))
    assert _ar(sandbox, "entry", "show", "Q1") == 0
    doc = json.loads(capsys.readouterr().out)
    assert "history" not in doc
    assert doc["status"] == "confirmed"

    assert _ar(sandbox, "entry", "show", "Q1", "--history") == 0
    full = json.loads(capsys.readouterr().out)
    assert [e["kind"] for e in full["history"]] == [e.kind for e in entry.history]
    assert full["history"]


def test_list_json_is_the_pick_work_projection(sandbox, store, capsys):
    make_entry(store, "Q1", mechanisms=["alpha"], confidence=0.7)
    make_entry(store, "Q2", claim=Claim(session="s1", at=NOW, why="",
                                        budget="", max_runs=2, max_hours=4.0))
    assert _ar(sandbox, "entry", "list", "--json") == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["id"] for r in rows] == ["Q1", "Q2"]
    assert set(rows[0]) == {"id", "title", "status", "track", "kind", "parent",
                            "claim", "confidence", "impact", "cost",
                            "mechanisms", "gates", "hardware"}
    assert rows[0]["claim"] is None and rows[0]["mechanisms"] == ["alpha"]
    assert rows[1]["claim"] == {"session": "s1", "at": NOW}

    # The prose form is unchanged and still names what it read.
    assert _ar(sandbox, "entry", "list") == 0
    out = capsys.readouterr().out
    assert "2 entries read" in out and "[s1]" in out


def test_list_json_honours_the_filters(sandbox, store, capsys):
    make_entry(store, "Q1")
    make_entry(store, "Q2", status="in-progress")
    assert _ar(sandbox, "entry", "list", "--json",
               "--status", "in-progress") == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["id"] for r in rows] == ["Q2"]
