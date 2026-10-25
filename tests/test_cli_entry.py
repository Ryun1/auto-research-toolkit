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
    assert entry.result.evidence_class is None  # unlabelled until declared

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
    assert entry.result.evidence_class is None  # unlabelled until declared
    assert entry.title != "renamed"


# -- the pre-registration has a writer (sig.golf H6) --------------------------
# `entry new` takes --prediction/--bar/--why and amend could correct neither:
# a number found wrong at filing time could only be fixed by hand-editing the
# YAML, beside the record, where nothing renders or validates it.

def test_amend_corrects_a_prediction_that_was_filed_wrong(sandbox, store):
    make_entry(store, "Q1", prediction="at most 70,400 instructions")
    assert _ar(sandbox, "entry", "amend", "Q1",
               "--prediction", "at most 145,952 instructions",
               "--why", "the image census measured 145,952") == 0
    assert store.load("Q1").prediction == "at most 145,952 instructions"


def test_the_superseded_prediction_stays_in_the_history(sandbox, store):
    """An amend is a blind full-value write (H15), so the previous value has to
    ride along -- otherwise the correction destroys the evidence that the
    entry once claimed something else."""
    make_entry(store, "Q1", prediction="at most 70,400 instructions")
    _ar(sandbox, "entry", "amend", "Q1",
       "--prediction", "at most 145,952 instructions", "--why", "measured")
    event = store.load("Q1").history[-1]
    assert event.kind == "amend"
    assert "at most 70,400 instructions" in event.detail


def test_amend_corrects_the_bar_and_the_reason_it_was_filed(sandbox, store):
    make_entry(store, "Q1", bar="confirmed if C < 5000", why_filed="a hunch")
    assert _ar(sandbox, "entry", "amend", "Q1",
               "--bar", "confirmed if C < 5400",
               "--why-filed", "derived from the Q6 census",
               "--why", "both were transcribed from a stale note") == 0
    entry = store.load("Q1")
    assert entry.bar == "confirmed if C < 5400"
    assert entry.why_filed == "derived from the Q6 census"


def test_amend_will_not_silently_rewrite_the_pre_registration(sandbox, store,
                                                              capsys):
    """No --why, no write: a prediction that changes with no stated reason is
    indistinguishable from one that was right all along."""
    make_entry(store, "Q1", prediction="at most 70,400 instructions")
    assert _ar(sandbox, "entry", "amend", "Q1",
               "--prediction", "at most 145,952 instructions") == 2
    assert "--why" in capsys.readouterr().err
    assert store.load("Q1").prediction == "at most 70,400 instructions"


def test_a_closed_entry_s_prediction_can_still_be_corrected(sandbox, store):
    """The same stance `close --summary` takes (QSB H82): a terminal verdict is
    not the last word on prose that is wrong, only on the verdict."""
    close(sandbox, store, make_entry(store, "Q1", prediction="70,400"))
    assert _ar(sandbox, "entry", "amend", "Q1",
               "--prediction", "145,952",
               "--why", "the census measured the real image") == 0
    assert store.load("Q1").prediction == "145,952"


def test_an_entry_can_be_filed_as_a_wiring_check_and_unmarked(sandbox, store):
    """The mark is a filing-time decision (sig.golf H4): the entry that checks
    the harness end to end is filed knowing it will out-score every real
    scheme, and can be unmarked when the thing it checked becomes a finding."""
    assert _ar(sandbox, "entry", "new", "wiring check", "--track", "research",
               "--scaffold") == 0
    assert store.load("Q1").scaffold is True
    assert _ar(sandbox, "entry", "amend", "Q1", "--no-scaffold") == 0
    assert store.load("Q1").scaffold is False
    assert _ar(sandbox, "entry", "amend", "Q1", "--scaffold") == 0
    assert store.load("Q1").scaffold is True


def test_an_unknown_status_filter_is_refused_rather_than_reading_as_empty(
        sandbox, store, capsys):
    """sig.golf H12: `--status confirmedd` printed "0 entries read" and exited
    0 -- the same screen an empty queue prints, which is how a session decides
    there is nothing to do."""
    make_entry(store, "Q1")
    assert _ar(sandbox, "entry", "list", "--status", "confirmedd") == 2
    err = capsys.readouterr().err
    assert "unknown status" in err and "confirmed" in err


def test_a_declared_status_matching_nothing_is_still_an_answer(sandbox, store,
                                                                capsys):
    """The refusal is about the typo, not about emptiness."""
    make_entry(store, "Q1")
    assert _ar(sandbox, "entry", "list", "--status", "confirmed") == 0
    assert "0 entries read" in capsys.readouterr().out


def test_an_unknown_track_filter_is_refused_too(sandbox, store, capsys):
    """The same argument as the status filter, in the verb that filters by
    track: `entry new --track` and `entry graph --track` already refused an
    undeclared track, so listing by one was the last place a typo read as an
    empty queue."""
    make_entry(store, "Q1")
    assert _ar(sandbox, "entry", "list", "--track", "nope") == 2
    assert "unknown track" in capsys.readouterr().err
    assert _ar(sandbox, "entry", "list", "--track", "research") == 0


def test_entry_new_refuses_an_id_no_track_owns(sandbox, store, capsys):
    """#18: `--id QT1` was written, and that one file then stopped `ar
    validate` and every store-reading verb for the whole domain."""
    assert _ar(sandbox, "entry", "new", "x", "--track", "research",
               "--id", "QT1") == 2
    assert "no track owns entry id 'QT1'" in capsys.readouterr().err
    assert not store.exists("QT1")


def test_entry_new_refuses_an_id_another_track_owns(sandbox, store, capsys):
    assert _ar(sandbox, "entry", "new", "x", "--track", "research",
               "--id", "H7") == 2
    err = capsys.readouterr().err
    assert "'H7' belongs to track" in err and "not 'research'" in err
    assert not store.exists("H7")


def test_entry_new_accepts_an_id_its_track_owns(sandbox, store):
    assert _ar(sandbox, "entry", "new", "x", "--track", "research",
               "--id", "Q7") == 0
    assert store.load("Q7").track == "research"
