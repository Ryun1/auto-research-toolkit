"""`ar relocate` -- pointer-preserving evidence relocation (QSB H32).

`ar validate` warns on every oversized inbox file, but a bare `mv` dangles the
recorded pointers (closed entries' `result.memo`, open entries' `sources`),
and a dangling memo is the H74/H117 failure class the close gate refuses. The
command moves the file AND rewrites every recorded pointer, atomically per
record, then re-renders the views so validate follows clean.
"""
import json

import pytest

from autoresearch import cli
from conftest import close, make_entry


def _ar(sandbox, *args):
    return cli.main(["--domain", str(sandbox.paths.root), "--session", "s",
                     *args])


@pytest.fixture
def corpus(sandbox, store):
    """One oversized inbox file, cited by an open entry's sources and a closed
    entry's result memo."""
    (sandbox.paths.memos / "q24-swarm.json").write_text(
        json.dumps({"hits": 1, "blob": "x" * 128 * 1024}))
    make_entry(store, "Q1", sources=["inbox/q24-swarm.json"])
    q2 = close(sandbox, store, make_entry(store, "Q2"))
    # conftest.close writes its own default memo; the corpus under test cites
    # the oversized file from the closed entry's result memo.
    q2.result.memo = "inbox/q24-swarm.json"
    store.save(q2)
    return sandbox


def test_relocation_moves_the_file_and_rewrites_every_pointer(corpus, store,
                                                              capsys):
    assert _ar(corpus, "relocate", "inbox/q24-swarm.json",
               "--why", "oversized terminal evidence") == 0

    assert not (corpus.paths.memos / "q24-swarm.json").exists()
    moved = corpus.paths.root / "data/artifacts/inbox/q24-swarm.json"
    assert moved.exists()
    assert json.loads(moved.read_text())["hits"] == 1

    open_entry = store.load("Q1")
    assert open_entry.sources == ["data/artifacts/inbox/q24-swarm.json"]
    closed_entry = store.load("Q2")
    assert closed_entry.result.memo == "data/artifacts/inbox/q24-swarm.json"
    event = closed_entry.history[-1]
    assert event.kind == "relocate"
    assert "result.memo" in event.detail
    assert "oversized" in event.detail

    out = capsys.readouterr().out
    assert "rewrote 2 pointer(s)" in out


def test_relocated_pointers_validate_clean_with_no_inbox_warning(corpus):
    """The H32 bar: validate runs with zero inbox-size warnings AND every
    recorded memo pointer still resolves."""
    from autoresearch.validate import inbox_warnings

    assert len(inbox_warnings(corpus)) == 1
    assert _ar(corpus, "relocate", "inbox/q24-swarm.json",
               "--why", "oversized") == 0
    assert inbox_warnings(corpus) == []
    assert _ar(corpus, "validate") == 0


def test_relocation_refuses_a_missing_reason_or_missing_file(corpus, capsys):
    # argparse owns the required flag: a missing --why never reaches the
    # command, and the in-function reason guard is belt-and-braces.
    with pytest.raises(SystemExit):
        _ar(corpus, "relocate", "inbox/q24-swarm.json")
    assert _ar(corpus, "relocate", "inbox/nope.json", "--why", "why") == 2
    assert "no such file" in capsys.readouterr().err
    assert (corpus.paths.memos / "q24-swarm.json").exists()


def test_relocation_refuses_paths_outside_the_inbox(corpus, capsys):
    artifacts = corpus.paths.root / "data/artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "kept.json").write_text("{}")
    assert _ar(corpus, "relocate", "data/artifacts/kept.json",
               "--why", "why") == 2
    assert "not under the inbox" in capsys.readouterr().err


def test_relocation_refuses_a_destination_still_in_the_inbox(corpus, capsys):
    assert _ar(corpus, "relocate", "inbox/q24-swarm.json",
               "--to", "inbox/renamed.json", "--why", "why") == 2
    assert "still inside the inbox" in capsys.readouterr().err
    assert (corpus.paths.memos / "q24-swarm.json").exists()


def test_relocation_refuses_an_existing_destination(corpus, capsys):
    (corpus.paths.root / "data/artifacts/inbox").mkdir(parents=True)
    (corpus.paths.root / "data/artifacts/inbox/q24-swarm.json").write_text("{}")
    assert _ar(corpus, "relocate", "inbox/q24-swarm.json",
               "--why", "why") == 2
    assert "destination exists" in capsys.readouterr().err
    assert (corpus.paths.memos / "q24-swarm.json").exists()


def test_relocation_refuses_a_destination_outside_the_root(corpus, capsys):
    assert _ar(corpus, "relocate", "inbox/q24-swarm.json",
               "--to", "../outside.json", "--why", "why") == 2
    assert "escapes the domain root" in capsys.readouterr().err
    assert (corpus.paths.memos / "q24-swarm.json").exists()
