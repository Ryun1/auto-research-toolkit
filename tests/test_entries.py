import pytest

from autoresearch.entries import Entry, Result
from autoresearch.errors import SchemaError, TransitionError
from autoresearch.states import default_machine
from conftest import make_entry


def result(**over):
    data = dict(verdict="confirmed", memo="inbox/m.md", at="2026-08-31T00:00:00+00:00",
                session="s")
    data.update(over)
    return Result(**data)


def test_ids_are_filenames_so_duplicates_cannot_exist(store):
    """H79: ids allocated by reading a shared document meant two branches took
    the same id and the loser was renumbered after every citation was written."""
    make_entry(store, "Q1")
    make_entry(store, "Q2")
    assert store.duplicates() == []
    assert store.next_id("Q") == "Q3"


def test_transition_requires_a_reason_where_the_machine_says_so(store):
    m = default_machine()
    e = make_entry(store)
    e.apply(m, "in-progress", who="s", why="claimed")
    with pytest.raises(TransitionError, match="requires a reason"):
        e.apply(m, "queued", who="s", why="   ")


def test_closing_requires_evidence_that_exists(store, sandbox):
    m = default_machine()
    e = make_entry(store)
    e.apply(m, "in-progress", who="s", why="c")
    with pytest.raises(TransitionError, match="does not exist"):
        e.apply(m, "confirmed", who="s", result=result(),
                memo_exists=lambda p: False)
    e.apply(m, "confirmed", who="s", result=result(), memo_exists=lambda p: True)
    assert e.status == "confirmed"


def test_refutation_requires_a_closure_kind(store):
    m = default_machine()
    e = make_entry(store)
    e.apply(m, "in-progress", who="s", why="c")
    with pytest.raises(TransitionError, match="requires closure_kind"):
        e.apply(m, "refuted", who="s", result=result(verdict="refuted"),
                memo_exists=lambda p: True)


def test_slope_closure_requires_a_reopen_condition(store):
    """H136: a `slope` closure with no re-open condition is a permanent closure
    wearing a temporary label, and 7 of 18 were."""
    m = default_machine()
    e = make_entry(store)
    e.apply(m, "in-progress", who="s", why="c")
    with pytest.raises(TransitionError, match="re-open condition"):
        e.apply(m, "refuted", who="s", memo_exists=lambda p: True,
                result=result(verdict="refuted", closure_kind="slope"))
    e.apply(m, "refuted", who="s", memo_exists=lambda p: True,
            result=result(verdict="refuted", closure_kind="slope",
                          reopen_condition="measured 30-45 peak only"))
    assert e.result.closure_kind == "slope"


def test_mechanism_closure_needs_no_reopen_condition(store):
    m = default_machine()
    e = make_entry(store)
    e.apply(m, "in-progress", who="s", why="c")
    e.apply(m, "refuted", who="s", memo_exists=lambda p: True,
            result=result(verdict="refuted", closure_kind="mechanism"))
    assert e.status == "refuted"


def test_reopen_archives_the_old_result_rather_than_leaving_it(store):
    """H137: reopen left the old Result in place and the linter then refused the
    entry it had just created."""
    m = default_machine()
    e = make_entry(store)
    e.apply(m, "in-progress", who="s", why="c")
    e.apply(m, "confirmed", who="s", result=result(summary="it worked"),
            memo_exists=lambda p: True)
    e.apply(m, "queued", who="curator", why="premise discharged")
    assert e.result is None
    assert any(ev.kind == "result-archived" and "it worked" in ev.detail
               for ev in e.history)


def test_relabel_keeps_the_verdict(store):
    """H133: correcting a label used to require reopening an entry whose verdict
    was not in doubt."""
    m = default_machine()
    e = make_entry(store)
    e.apply(m, "in-progress", who="s", why="c")
    e.apply(m, "refuted", who="s", memo_exists=lambda p: True,
            result=result(verdict="refuted", closure_kind="mechanism"))
    e.relabel("cell", who="curator", why="one point, not a range")
    assert e.status == "refuted" and e.result.closure_kind == "cell"
    assert any(ev.kind == "relabel" for ev in e.history)


def test_relabel_requires_a_reason(store):
    m = default_machine()
    e = make_entry(store)
    e.apply(m, "in-progress", who="s", why="c")
    e.apply(m, "refuted", who="s", memo_exists=lambda p: True,
            result=result(verdict="refuted", closure_kind="mechanism"))
    with pytest.raises(TransitionError, match="requires a reason"):
        e.relabel("cell", who="c", why="")


def test_history_is_append_only_across_a_save_load_cycle(store):
    m = default_machine()
    e = make_entry(store)
    e.apply(m, "in-progress", who="s", why="c")
    store.save(e)
    back = store.load(e.id)
    back.apply(m, "queued", who="s", why="released")
    store.save(back)
    assert [ev.kind for ev in store.load(e.id).history] == [
        "queued->in-progress", "in-progress->queued"]


def test_unknown_field_is_refused(store):
    with pytest.raises(SchemaError, match="unknown field"):
        Entry.from_dict({"id": "Q1", "track": "r", "title": "t", "surprise": 1})


def test_malformed_id_is_reported(store):
    with pytest.raises(SchemaError, match="not <PREFIX><digits>"):
        _ = Entry(id="nope", track="r", title="t").number  # noqa: B018 -- property raises


def test_entry_creation_rejects_unknown_track(sandbox, store, capsys):
    from autoresearch import cli

    args = ["--domain", str(sandbox.paths.root), "entry", "new",
            "--track", "bogus", "Example"]
    assert cli.main(args) == 2
    assert "bogus" in capsys.readouterr().err
    assert store.all() == []
    assert cli.main([*args[:-2], "research", "Example"]) == 0
    assert [(entry.track, entry.title) for entry in store.all()] == [
        ("research", "Example")]
