"""#40: measurement epochs. Every row says what it was measured under; the
frontier compares rows from one epoch only.

An epoch is a distinct `(evaluator_sha, benchmark_revision)` pair in the
ledger, derived and never stored. When the evaluator or the benchmark moves,
old rows stay on the board under "previous epochs" and stop competing for
best -- a refutation made under an old evaluator can be told from a current
one, and `ar epochs` lists the closures a move may have invalidated.
"""
import json
import subprocess

import pytest

from autoresearch import cli, epochs, evaluator
from autoresearch.config import DomainConfig
from autoresearch.runs import RunRecord, append, read_all
from conftest import close, make_entry


def _ar(sandbox, *args):
    return cli.main(["--domain", str(sandbox.paths.root), *args])


def _git_init(root):
    for args in (("init", "-q"), ("add", "."),
                 ("-c", "user.name=T", "-c", "user.email=t@example.invalid",
                  "commit", "-qm", "fixture")):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True,
                          capture_output=True, text=True).stdout.strip()


def _row(run_id, ops, sha, benchmark="", entry=None, started=100.0):
    provenance = {"host": "local"}
    if sha is not None:
        provenance.update(evaluator_sha=sha, benchmark_revision=benchmark)
    return RunRecord(id=run_id, metrics={"ops": ops, "peak": 1.0}, session="s",
                     entry=entry, started=started, provenance=provenance)


def _declare(sandbox, text):
    toml = sandbox.paths.root / "domain.toml"
    toml.write_text(toml.read_text().replace(
        '[commands]\n', '[commands]\n' + text + '\n'))
    return DomainConfig.load(sandbox.paths.root)


# -- stamping rows ---------------------------------------------------------

def test_a_row_names_the_source_revision_and_core_it_was_measured_under(sandbox):
    head = _git_init(sandbox.paths.root)
    assert _ar(sandbox, "measure", "--", "--knob", "unroll=4") == 0
    [row] = read_all(sandbox.paths.runs)
    assert row.provenance["source_revision"] == head
    assert row.provenance["source_dirty"] is False
    assert row.provenance["core"]
    assert row.provenance["benchmark_revision"] == ""


def test_a_dirty_tree_is_flagged_not_hidden(sandbox):
    _git_init(sandbox.paths.root)
    (sandbox.paths.root / "goal.yaml").write_text(
        (sandbox.paths.root / "goal.yaml").read_text() + "\n")
    assert _ar(sandbox, "measure") == 0
    assert read_all(sandbox.paths.runs)[0].provenance["source_dirty"] is True


def test_outside_git_the_revision_is_empty_not_guessed(sandbox):
    assert _ar(sandbox, "measure") == 0
    assert read_all(sandbox.paths.runs)[0].provenance["source_revision"] == ""


def test_a_domain_declared_revision_command_wins(sandbox):
    script = sandbox.paths.root / "bin/revs"
    script.write_text("#!/bin/sh\necho \"$1-rev\"\n")
    script.chmod(0o755)
    config = _declare(sandbox, 'source_revision = "bin/revs src"\n'
                               'benchmark_revision = "bin/revs bench"')
    assert _ar(config, "measure") == 0
    row = read_all(config.paths.runs)[0]
    assert row.provenance["source_revision"] == "src-rev"
    assert row.provenance["benchmark_revision"] == "bench-rev"


# -- the frontier is per epoch ---------------------------------------------

def test_an_old_epoch_row_does_not_stand_as_best(sandbox, store):
    """The negative test: an evaluator change makes an old row that beats
    every new one a number from a different instrument."""
    current = evaluator.identity(sandbox)
    append(sandbox.paths.runs / "s.jsonl", _row("old", 10.0, "0ld", started=1))
    append(sandbox.paths.runs / "s.jsonl", _row("new", 900.0, current, started=2))
    rows = epochs.current_rows(sandbox, read_all(sandbox.paths.runs))
    assert [r.id for r in rows] == ["new"]


def test_a_new_evaluator_with_no_rows_yet_has_no_best(sandbox, capsys):
    append(sandbox.paths.runs / "s.jsonl", _row("old", 10.0, "0ld"))
    assert epochs.current_rows(sandbox, read_all(sandbox.paths.runs)) == []
    assert _ar(sandbox, "budget", "--json") == 0
    assert json.loads(capsys.readouterr().out)["best"] is None


def test_a_benchmark_move_starts_a_new_epoch(sandbox):
    script = sandbox.paths.root / "bin/bench"
    script.write_text("#!/bin/sh\necho v2\n")
    script.chmod(0o755)
    config = _declare(sandbox, 'benchmark_revision = "bin/bench"')
    current = evaluator.identity(config)
    append(config.paths.runs / "s.jsonl", _row("v1", 10.0, current, "v1", started=1))
    append(config.paths.runs / "s.jsonl", _row("v2", 900.0, current, "v2", started=2))
    assert [r.id for r in epochs.current_rows(config, read_all(config.paths.runs))] \
        == ["v2"]


def test_a_ledger_with_no_stamped_rows_is_one_epoch(sandbox):
    """Migration: a domain upgrading with only unstamped rows keeps its
    frontier until the first stamped row arrives."""
    append(sandbox.paths.runs / "s.jsonl", _row("legacy", 10.0, None))
    assert [r.id for r in epochs.current_rows(
        sandbox, read_all(sandbox.paths.runs))] == ["legacy"]


def test_unpinned_rows_are_never_merged_into_a_pinned_epoch(sandbox):
    current = evaluator.identity(sandbox)
    append(sandbox.paths.runs / "s.jsonl", _row("legacy", 10.0, None, started=1))
    append(sandbox.paths.runs / "s.jsonl", _row("new", 900.0, current, started=2))
    listed = epochs.segment(sandbox, read_all(sandbox.paths.runs))
    assert [e.label for e in listed] == ["unpinned", "1"]
    assert [e.current for e in listed] == [False, True]
    assert [r.id for r in epochs.current_rows(
        sandbox, read_all(sandbox.paths.runs))] == ["new"]


def test_the_board_shows_previous_epochs_and_can_select_one(sandbox, capsys):
    current = evaluator.identity(sandbox)
    append(sandbox.paths.runs / "s.jsonl", _row("old", 10.0, "0ld", started=1))
    append(sandbox.paths.runs / "s.jsonl", _row("new", 900.0, current, started=2))
    assert _ar(sandbox, "board") == 0
    out = capsys.readouterr().out
    assert "best       900" in out
    assert "## Previous epochs" in out and "old" in out
    assert _ar(sandbox, "board", "--epoch", "1") == 0
    assert "best       10" in capsys.readouterr().out


def test_an_unknown_epoch_is_refused(sandbox, capsys):
    assert _ar(sandbox, "board", "--epoch", "7") == 2
    assert "no epoch" in capsys.readouterr().err


def test_independent_confirmation_counts_only_this_epoch(sandbox):
    from autoresearch import budget as budget_mod
    current = evaluator.identity(sandbox)
    best_row = _row("best", 900.0, current, entry="Q1")
    rows = [best_row, RunRecord(id="old-confirm", metrics={"ops": 900.0, "peak": 1.0},
                                session="other", entry="Q2",
                                provenance={"evaluator_sha": "0ld"})]
    epoch_rows = epochs.current_rows(sandbox, rows)
    assert not budget_mod.independent_confirmation(
        sandbox.goal, (900.0, best_row), epoch_rows, 1.0)


# -- stamping closures -----------------------------------------------------

def test_a_closure_is_pinned_to_the_rows_it_was_measured_under(sandbox, store):
    head = _git_init(sandbox.paths.root)
    make_entry(store, "Q1")
    assert _ar(sandbox, "--session", "w", "claim", "Q1", "--why", "t") == 0
    assert _ar(sandbox, "--session", "w", "measure", "--entry", "Q1") == 0
    (sandbox.paths.root / "inbox").mkdir(exist_ok=True)
    (sandbox.paths.root / "inbox/q1.md").write_text("memo")
    assert _ar(sandbox, "--session", "w", "close", "Q1", "refuted",
               "--memo", "inbox/q1.md", "--summary", "no", "--closure", "cell",
               "--reopen-condition", "a new width", "--why", "done") == 0
    pin = store.load("Q1").result.pin
    assert pin["evaluator_sha"] == evaluator.identity(sandbox)
    assert pin["source_revision"] == head and pin["core"]


def test_close_source_revision_names_an_external_closure(sandbox, store):
    make_entry(store, "Q1")
    assert _ar(sandbox, "--session", "w", "claim", "Q1", "--why", "t") == 0
    (sandbox.paths.root / "inbox").mkdir(exist_ok=True)
    (sandbox.paths.root / "inbox/q1.md").write_text("memo")
    assert _ar(sandbox, "--session", "w", "close", "Q1", "refuted",
               "--memo", "inbox/q1.md", "--summary", "no", "--closure", "cell",
               "--reopen-condition", "a new width", "--why", "done",
               "--source-revision", "abc123") == 0
    pin = store.load("Q1").result.pin
    assert pin["source_revision"] == "abc123"
    assert pin["evaluator_sha"] == evaluator.identity(sandbox)


def test_a_loop_closure_carries_the_workers_declared_revision(sandbox, store):
    from types import SimpleNamespace

    from autoresearch.budget import iteration_budget
    from autoresearch.driver.brain import Reply
    from autoresearch.driver.loop import Coordinator, Iteration
    from autoresearch.workspaces import Pool
    make_entry(store, "Q1", impact=1.0)

    class Brain:
        def ask(self, role, brief, *, workspace=None):
            (workspace / "inbox/r.md").write_text("static")
            return Reply(role=role, data={
                "verdict": "refuted", "memo": "inbox/r.md", "runs": 0,
                "summary": "no", "closure_kind": "mechanism",
                "applicability": {"source_revision": "deadbeef"},
                "verification": {"reread": True, "claims_checked": ["x"]}})

    coordinator = Coordinator(sandbox, Brain())
    with Pool(sandbox, "pin") as pool:
        coordinator.dispatch(Iteration(n=1), [SimpleNamespace(entry_id="Q1", terms={})],
                             iteration_budget(sandbox), pool)
    pin = store.load("Q1").result.pin
    assert pin["source_revision"] == "deadbeef"
    assert pin["evaluator_sha"] == evaluator.identity(sandbox)


def test_validate_warns_on_cell_and_slope_closures_with_no_revision(sandbox, store):
    from autoresearch import validate
    close(sandbox, store, make_entry(store, "Q1"), verdict="refuted",
          closure_kind="cell", reopen_condition="another width")
    close(sandbox, store, make_entry(store, "Q2"), verdict="refuted",
          closure_kind="mechanism")
    [warning] = validate.revision_warnings(sandbox)
    assert "Q1" in warning and "Q2" not in warning


def test_ar_epochs_lists_closures_pinned_to_a_superseded_epoch(sandbox, store, capsys):
    entry = close(sandbox, store, make_entry(store, "Q1"), verdict="refuted",
                  closure_kind="cell", reopen_condition="re-measure at v2")
    entry.result.pin = {"evaluator_sha": "0ld", "benchmark_revision": "",
                        "source_revision": "abc", "core": "0.9"}
    store.save(entry)
    append(sandbox.paths.runs / "s.jsonl", _row("old", 10.0, "0ld", started=1))
    append(sandbox.paths.runs / "s.jsonl",
           _row("new", 900.0, evaluator.identity(sandbox), started=2))
    assert _ar(sandbox, "epochs") == 0
    out = capsys.readouterr().out
    assert "Q1" in out and "re-measure at v2" in out
    assert "(current)" in out
    # It lists; it never reopens.
    assert store.load("Q1").status == "refuted"


@pytest.mark.parametrize("pin", [{}, {"evaluator_sha": None}])
def test_an_unpinned_closure_is_counted_not_listed_as_superseded(sandbox, store,
                                                                capsys, pin):
    entry = close(sandbox, store, make_entry(store, "Q1"), verdict="refuted",
                  closure_kind="cell", reopen_condition="x")
    entry.result.pin = pin
    store.save(entry)
    assert _ar(sandbox, "epochs") == 0
    out = capsys.readouterr().out
    assert "1 closure(s) carry no pin" in out


def test_validate_names_rows_from_a_superseded_epoch_once(sandbox):
    from autoresearch import validate
    append(sandbox.paths.runs / "s.jsonl", _row("old", 10.0, "0ld", started=1))
    append(sandbox.paths.runs / "s.jsonl", _row("old2", 11.0, "0ld", started=2))
    assert validate.epoch_warnings(sandbox) == [
        "2 row(s) were measured under a superseded evaluator or benchmark "
        "(1 earlier epoch(s)); they no longer compete for best -- `ar epochs` "
        "lists closures that may need reopening"]
    append(sandbox.paths.runs / "t.jsonl", _row("legacy", 9.0, None))
    sandbox_only_legacy = [r for r in read_all(sandbox.paths.runs)
                           if r.id == "legacy"]
    assert epochs.segment(sandbox, sandbox_only_legacy)[0].current
