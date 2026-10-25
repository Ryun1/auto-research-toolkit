"""One unreadable entry file must not misreport work on every other entry.

A sibling file with conflict markers or broken YAML is a real problem, and
`ar validate` names it. What it must not do is turn a write that landed into a
failed command (#29), or hide every malformed file but the first (#32).
"""
from autoresearch import cli
from conftest import make_entry


def _root(sandbox, session="s2"):
    return ["--domain", str(sandbox.paths.root), "--session", session]


def _break(store, entry_id, text="<<<<<<< ours\nx: 1\n=======\nx: 2\n>>>>>>> theirs\n"):
    path = store.path(entry_id)
    path.write_text(path.read_text() + text)


def test_a_write_that_landed_is_not_reported_as_failed(sandbox, store, capsys):
    """#29 (QSB H300): the claim was saved, then re-rendering the views read a
    sibling's conflict markers and exited 2. The retry was refused as a no-op
    transition, and that refusal was read as proof nothing had happened."""
    make_entry(store, "Q1")
    make_entry(store, "Q2")
    _break(store, "Q1")
    code = cli.main([*_root(sandbox), "claim", "Q2", "--why", "x"])
    assert store.load("Q2").status == "in-progress"
    err = capsys.readouterr().err
    assert code == 3, err
    assert "landed" in err and "views were not re-rendered" in err
    assert "Q1.yaml" in err and "ar validate" in err


def test_a_clean_store_still_exits_zero(sandbox, store):
    make_entry(store, "Q2")
    assert cli.main([*_root(sandbox), "claim", "Q2", "--why", "x"]) == 0


def test_a_refused_write_is_still_exit_two(sandbox, store, capsys):
    """The distinct status is only for a write that landed."""
    make_entry(store, "Q1")
    make_entry(store, "Q2")
    _break(store, "Q1")
    assert cli.main([*_root(sandbox), "claim", "Q2", "--why", "x"]) == 3
    assert cli.main([*_root(sandbox, "s9"), "claim", "Q2", "--why", "x"]) == 2


def test_validate_names_every_malformed_entry_in_one_pass(sandbox, store, capsys):
    """#32 (QSB H320): validate died on the first malformed file, so a
    multi-session afternoon was fix one, re-run, find the next."""
    for entry_id in ("Q1", "Q2", "Q3"):
        make_entry(store, entry_id)
    _break(store, "Q1", "  : : not yaml [\n")
    _break(store, "Q2", "  : : not yaml [\n")
    code = cli.main(["--domain", str(sandbox.paths.root), "validate"])
    out = capsys.readouterr().out
    assert code == 1, out
    assert "Q1.yaml" in out and "Q2.yaml" in out
    # The store-wide checks still ran over the entry that loaded.
    assert "checked 1 entries" in out


def test_validate_names_an_entry_no_track_owns(sandbox, store, capsys):
    """#18: one mis-prefixed file stopped validate for all 298 entries."""
    make_entry(store, "Q1")
    store.path("Q1").rename(store.path("QT1"))
    text = store.path("QT1").read_text().replace("id: Q1", "id: QT1")
    store.path("QT1").write_text(text)
    code = cli.main(["--domain", str(sandbox.paths.root), "validate"])
    out = capsys.readouterr().out
    assert code == 1, out
    assert "no track owns entry id 'QT1'" in out
