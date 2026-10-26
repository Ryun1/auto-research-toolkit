"""Reopening a closed entry names why, as a category the record can count.

The qsb corpus reopened 95 research verdicts with free text only, and it took
a hand-labelling pass to learn that 29 of them were verdicts that did not
survive and 48 were bookkeeping. A verdict's reliability cannot be studied
from a record that does not say which reopens were which.
"""
from autoresearch import cli
from conftest import close, make_entry


def _ar(sandbox, *args):
    return cli.main(["--domain", str(sandbox.paths.root), "--session", "s", *args])


def test_reopening_a_closed_entry_requires_a_reason_category(sandbox, store, capsys):
    close(sandbox, store, make_entry(store, "Q1"), "refuted", closure_kind="mechanism")
    assert _ar(sandbox, "close", "Q1", "--reopen", "--why", "read a stale tree") == 2
    err = capsys.readouterr().err
    assert "--reopen-reason" in err
    for reason in ("weak", "overclaim", "context", "clerical"):
        assert reason in err
    assert store.load("Q1").status == "refuted"


def test_the_reason_lands_on_the_reopen_event_and_survives_a_reload(sandbox, store):
    close(sandbox, store, make_entry(store, "Q1"), "refuted", closure_kind="mechanism")
    assert _ar(sandbox, "close", "Q1", "--reopen", "--reopen-reason", "weak",
               "--why", "the refutation read a stale tree") == 0
    entry = store.load("Q1")
    assert entry.status == "queued"
    event = entry.history[-1]
    assert event.kind == "refuted->queued"
    assert event.detail == "the refutation read a stale tree"
    assert event.snapshot == {"reopen_reason": "weak"}


def test_an_unknown_reason_is_refused(sandbox, store, capsys):
    close(sandbox, store, make_entry(store, "Q1"), "confirmed")
    try:
        _ar(sandbox, "close", "Q1", "--reopen", "--reopen-reason", "oops",
            "--why", "w")
    except SystemExit as exc:
        assert exc.code == 2
    assert store.load("Q1").status == "confirmed"


def test_a_blocked_entry_reopens_without_a_reason_and_refuses_one(sandbox, store):
    """`blocked` is not a verdict, so there is nothing to categorise; a
    category on it would be counted as a verdict that never existed."""
    make_entry(store, "Q1")
    assert _ar(sandbox, "claim", "Q1", "--why", "t") == 0
    assert _ar(sandbox, "close", "Q1", "blocked", "--why", "no host") == 0
    assert _ar(sandbox, "close", "Q1", "--reopen", "--reopen-reason", "weak",
               "--why", "host back") == 2
    assert store.load("Q1").status == "blocked"
    assert _ar(sandbox, "close", "Q1", "--reopen", "--why", "host back") == 0
    entry = store.load("Q1")
    assert entry.status == "queued" and entry.history[-1].snapshot == {}


def test_a_reason_without_reopen_is_refused_not_ignored(sandbox, store, capsys):
    close(sandbox, store, make_entry(store, "Q1"), "confirmed")
    assert _ar(sandbox, "close", "Q1", "--relabel", "mechanism", "--why", "w",
               "--reopen-reason", "weak") == 2
    assert "pass it with --reopen" in capsys.readouterr().err
