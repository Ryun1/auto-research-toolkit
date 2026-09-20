"""`ar entry show` and `ar entry list` — the read surface agents script around.

`show` defaults to the current state because the append-only history is the
bulk of an old entry's JSON and a coordinator brief already excludes it;
`list --json` is the projection that lets an agent pick work without an N+1
of `entry show`.
"""
import json

from autoresearch import cli, render
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


def test_entry_graph_prints_the_view_markdown(sandbox, store, capsys):
    """`ar entry graph` prints the same markdown `ar render` writes, so an
    agent pasting it into a memo cannot produce a second, disagreeing copy."""
    make_entry(store, "Q1", parent="Q2", kind="improve")
    make_entry(store, "Q2")
    assert _ar(sandbox, "entry", "graph", "--track", "research") == 0
    out = capsys.readouterr().out
    assert out == render.graph_view(sandbox.tracks["research"], store.all())
    assert '    Q2 -->|"improve"| Q1' in out

    assert _ar(sandbox, "entry", "graph") == 0
    only = capsys.readouterr().out
    # The harness track never declared graph_view: it has opted out of the
    # picture, so the default scope is the declared tracks only.
    assert only == out and "Harness Debt" not in only
    assert _ar(sandbox, "entry", "graph", "--track", "harness") == 0
    assert "Harness Debt" in capsys.readouterr().out

    assert _ar(sandbox, "entry", "graph", "--track", "nope") != 0
    assert "not declared" in capsys.readouterr().err


def test_entry_graph_html_writes_the_snapshot(sandbox, store, capsys):
    make_entry(store, "Q1", hypothesis="the claim behind Q1")
    assert _ar(sandbox, "entry", "graph", "--html", "docs/graph.html") == 0
    assert "wrote" in capsys.readouterr().out
    page = (sandbox.paths.root / "docs/graph.html").read_text()
    assert "the claim behind Q1" in page and "flowchart LR" in page

    # Relative paths resolve against the domain root; --track narrows it.
    assert _ar(sandbox, "entry", "graph", "--html", "narrow.html",
               "--track", "research") == 0
    assert (sandbox.paths.root / "narrow.html").exists()


def test_list_json_honours_the_filters(sandbox, store, capsys):
    make_entry(store, "Q1")
    make_entry(store, "Q2", status="in-progress")
    assert _ar(sandbox, "entry", "list", "--json",
               "--status", "in-progress") == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["id"] for r in rows] == ["Q2"]


def test_amend_declares_the_evidence_class(sandbox, store, capsys):
    """The out-of-band path for closures accepted before the strictness
    landed: one command, one typed field, --why mandatory, views re-rendered
    so validate follows without a manual render (the H32-class hygiene)."""
    entry = close(sandbox, store, make_entry(store, "Q1"))
    assert entry.result.evidence_class == "ledger"

    assert _ar(sandbox, "entry", "amend", "Q1",
               "--evidence-class", "official") == 2
    assert "reason" in capsys.readouterr().err

    assert _ar(sandbox, "entry", "amend", "Q1",
               "--evidence-class", "official",
               "--why", "evaluator-owned numbers") == 0
    reread = store.load("Q1")
    assert reread.result.evidence_class == "official"
    assert reread.history[-1].kind == "evidence-class"


def test_amend_refuses_a_reclassification_combined_with_field_edits(sandbox,
                                                                    store,
                                                                    capsys):
    """A silent-ignore that ran only one of two requested changes reads as
    success (the H135 shape): combining --evidence-class with a field edit is
    refused, so neither half applies by accident."""
    close(sandbox, store, make_entry(store, "Q1"))
    assert _ar(sandbox, "entry", "amend", "Q1",
               "--evidence-class", "census", "--why", "a census",
               "--title", "renamed") == 2
    assert "pass it alone" in capsys.readouterr().err
    entry = store.load("Q1")
    assert entry.result.evidence_class == "ledger"
    assert entry.title != "renamed"
