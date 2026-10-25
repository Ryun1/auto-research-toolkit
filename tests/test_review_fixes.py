"""Defects an independent review found in #21-#40 and #22-#35, each pinned.

Grouped by the change they correct; the docstring of each test names what
went wrong.
"""
import os
import shutil
import subprocess
import time

import pytest

from autoresearch import cli, evaluator, validate
from autoresearch.claims import Claims
from autoresearch.config import DomainConfig
from autoresearch.entries import Event
from autoresearch.errors import ClaimError
from autoresearch.runs import RunRecord, append, read_all
from autoresearch.workspaces import Pool
from conftest import close, make_entry


def _ar(config, *args):
    return cli.main(["--domain", str(config.paths.root), *args])


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True,
                          capture_output=True, text=True).stdout


def _commit(root, message="fixture"):
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=T", "-c", "user.email=t@example.invalid",
         "commit", "-qm", message)


def _row(run_id, ops, sha=None, started=0.0, session="s", **over):
    provenance = {"host": "x"}
    if sha:
        provenance["evaluator_sha"] = sha
    return RunRecord(id=run_id, metrics={"ops": float(ops), "peak": 1.0},
                     session=session, started=started, provenance=provenance, **over)


# -- #40 epochs -------------------------------------------------------------

def test_the_board_does_not_list_the_current_epoch_as_previous(sandbox, capsys):
    current = evaluator.identity(sandbox)
    append(sandbox.paths.runs / "s.jsonl", _row("new", 900, current, started=2))
    assert _ar(sandbox, "board") == 0
    assert "Previous epochs" not in capsys.readouterr().out
    append(sandbox.paths.runs / "s.jsonl", _row("old", 10, "0ld", started=1))
    assert _ar(sandbox, "board") == 0
    out = capsys.readouterr().out
    previous = out.split("## Previous epochs")[1]
    assert "(current)" not in previous and "epoch 1" in previous


def test_a_row_with_a_non_numeric_start_does_not_crash_the_board(sandbox, capsys):
    current = evaluator.identity(sandbox)
    append(sandbox.paths.runs / "s.jsonl", _row("old", 10, "0ld", started=1))
    append(sandbox.paths.runs / "s.jsonl",
           _row("odd", 900, current, started="2026-10-01T00:00:00"))
    assert _ar(sandbox, "board") == 0


def test_ar_epochs_does_not_list_a_reopened_entry(sandbox, store, capsys):
    entry = close(sandbox, store, make_entry(store, "Q1"), verdict="refuted",
                  closure_kind="cell", reopen_condition="x")
    entry.result.pin = {"evaluator_sha": "0ld", "benchmark_revision": ""}
    machine = sandbox.track_for("Q1").machine
    entry.apply(machine, machine.initial, "test", why="reopened")
    store.save(entry)
    assert _ar(sandbox, "epochs") == 0
    assert "Q1" not in capsys.readouterr().out


def test_measure_outside_git_or_without_git_records_no_revision(sandbox, monkeypatch):
    monkeypatch.setenv("PATH", str(sandbox.paths.root / "nowhere"))
    from autoresearch import epochs
    assert epochs.source_revision(sandbox, sandbox.paths.root) == ("", None)


# -- #36 evaluator ----------------------------------------------------------

def test_an_evaluator_directory_is_hashed_not_a_crash(sandbox):
    (sandbox.paths.root / "lib").mkdir()
    (sandbox.paths.root / "lib/score.py").write_text("W = 1\n")
    toml = sandbox.paths.root / "domain.toml"
    toml.write_text(toml.read_text() + '\n[evaluator]\nfiles = ["bin/measure", "lib"]\n')
    config = DomainConfig.load(sandbox.paths.root)
    before = evaluator.identity(config)
    (sandbox.paths.root / "lib/score.py").write_text("W = 2\n")
    assert evaluator.identity(config) != before


M2 = """#!/usr/bin/env python3
import json, sys, time, uuid
ops = float(open(sys.argv[1]).read())
json.dump({"schema": "ar-run-1", "id": uuid.uuid4().hex[:12], "session": "x",
           "status": "ok", "started": time.time(), "finished": time.time(),
           "metrics": {"ops": ops, "peak": 1.0}, "config": {}, "provenance": {"h": 1},
           "outputs": [], "cost": {}}, sys.stdout)
print()
"""


@pytest.mark.parametrize("declare", [True, False], ids=["declared", "default"])
def test_a_command_argument_naming_a_candidate_file_reads_the_slots_copy(
        sandbox, declare):
    """Every token naming a file was rewritten to the primary's copy, so the
    slot's tuned `candidate.txt` was never measured; and by default the
    argument was taken for part of the evaluator, refusing the edit."""
    root = sandbox.paths.root
    (root / "bin/m2").write_text(M2)
    (root / "bin/m2").chmod(0o755)
    (root / "candidate.txt").write_text("100\n")
    toml = root / "domain.toml"
    text = toml.read_text().replace('measure      = "bin/measure"',
                                    'measure      = "bin/m2 candidate.txt"')
    toml.write_text(text + ('\n[evaluator]\nfiles = ["bin/m2"]\n' if declare else ""))
    config = DomainConfig.load(root)
    assert evaluator.files(config) == ["bin/m2"]
    with Pool(config, "t") as pool:
        slot = pool.acquire("it1-Q1").path
        (slot / "candidate.txt").write_text("555\n")
        assert _ar(config, "--session", "it1-Q1", "measure", "--candidate", str(slot)) == 0
        [row] = read_all(slot / "data/runs")
        assert row.metrics["ops"] == 555.0


FAKE = """#!/usr/bin/env python3
import json, sys, time
json.dump({"schema": "ar-run-1", "id": "fabricated", "session": "x",
           "status": "ok", "started": time.time(), "finished": time.time(),
           "metrics": {"ops": 1.0, "peak": 1.0}, "config": {}, "provenance": {"h": 1},
           "outputs": [], "cost": {}}, sys.stdout)
print()
"""


def test_a_symlinked_measure_script_is_still_the_primarys(sandbox, tmp_path):
    """`_inside` resolved symlinks, so a script linked from a shared location
    was not the evaluator and ran from the slot -- the worker's copy."""
    root = sandbox.paths.root
    shared = tmp_path / "shared"
    shared.mkdir()
    shutil.move(root / "bin/measure", shared / "measure")
    os.symlink(shared / "measure", root / "bin/measure")
    config = DomainConfig.load(root)
    assert evaluator.files(config) == ["bin/measure"]
    with Pool(config, "t") as pool:
        slot = pool.acquire("it1-Q1").path
        (slot / "bin/measure").unlink()
        (slot / "bin/measure").write_text(FAKE)
        (slot / "bin/measure").chmod(0o755)
        assert _ar(config, "--session", "it1-Q1", "measure", "--candidate",
                   str(slot)) == 2
        assert read_all(slot / "data/runs") == []


def test_validate_says_when_nothing_protects_the_evaluator(sandbox):
    toml = sandbox.paths.root / "domain.toml"
    toml.write_text(toml.read_text().replace('measure      = "bin/measure"',
                                             'measure      = "python -m bench"'))
    config = DomainConfig.load(sandbox.paths.root)
    [warning] = [w for w in validate.evaluator_warnings(config)]
    assert "[evaluator] files" in warning


def test_a_receipt_admits_its_row_only_from_the_slot_it_was_measured_in(sandbox):
    from autoresearch import runs as runs_mod
    with Pool(sandbox, "t") as pool:
        first = pool.acquire("it1-Q1").path
        second = pool.acquire("it1-Q2").path
        assert _ar(sandbox, "--session", "it1-Q1", "measure", "--candidate",
                   str(first)) == 0
        line = (first / "data/runs/it1-Q1.jsonl").read_text()
        before = runs_mod.snapshot(second / "data/runs")
        (second / "data/runs/it1-Q1.jsonl").write_text(line)
        result = runs_mod.harvest(second / "data/runs", before, sandbox.paths.runs,
                                  workspace=second, root=sandbox.paths.root,
                                  goal=sandbox.goal, lanes=sandbox.lanes,
                                  receipts=sandbox.paths.receipts)
    assert result.records == []
    assert any("another workspace" in p for p in result.problems)


# -- #33 claim tokens -------------------------------------------------------

def _token(capsys):
    return capsys.readouterr().out.split("token ")[1].split()[0]


def test_exec_needs_the_claim_token(sandbox, store, capsys):
    make_entry(store, "Q1")
    assert _ar(sandbox, "--session", "alice", "claim", "Q1", "--why", "t") == 0
    token = _token(capsys)
    assert _ar(sandbox, "--session", "alice", "exec", "--entry", "Q1",
               "--timeout", "5", "--", "true") == 2
    assert "token" in capsys.readouterr().err
    assert _ar(sandbox, "--session", "alice", "--claim-token", token, "exec",
               "--entry", "Q1", "--timeout", "5", "--", "true") == 0


def test_external_assign_needs_the_claim_token(sandbox, store, capsys):
    make_entry(store, "Q1")
    assert _ar(sandbox, "--session", "alice", "claim", "Q1", "--why", "t") == 0
    token = _token(capsys)
    assert _ar(sandbox, "--session", "alice", "external", "assign", "--entry",
               "Q1", "--timeout", "600", "--max-runs", "1") == 2
    assert _ar(sandbox, "--session", "alice", "--claim-token", token, "external",
               "assign", "--entry", "Q1", "--timeout", "600", "--max-runs", "1") == 0


def test_reopen_cannot_take_another_sessions_live_claim(sandbox, store, capsys):
    make_entry(store, "Q1")
    assert _ar(sandbox, "--session", "alice", "claim", "Q1", "--why", "t") == 0
    assert _ar(sandbox, "--session", "mallory", "close", "Q1", "--reopen",
               "--why", "steal") == 2
    assert store.load("Q1").claim.session == "alice"


def test_a_pinned_claim_must_be_the_claim_it_pinned(sandbox, store):
    make_entry(store, "Q1")
    entry = Claims(store, sandbox, "a").claim("Q1", why="t")
    stale = Claims(store, sandbox, "a", pinned={"Q1": "2000-01-01T00:00:00+00:00"})
    with pytest.raises(ClaimError):
        stale.release("Q1", why="x")
    fresh = Claims(store, sandbox, "a", pinned={"Q1": entry.claim.at})
    fresh.release("Q1", why="x")


# -- #22 reap liveness ------------------------------------------------------

def _stale(sandbox, store, session, entry_id="Q1", hours=10):
    import datetime as dt
    make_entry(store, entry_id)
    entry = Claims(store, sandbox, session).claim(entry_id, why="t")
    then = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=hours)).isoformat()
    entry.claim.at = then
    for event in entry.history:
        event.at = then
    store.save(entry)


def test_a_commit_that_only_contains_the_name_is_not_a_sign_of_life(sandbox, store):
    _stale(sandbox, store, "scout")
    _git(sandbox.paths.root, "init", "-q")
    _commit(sandbox.paths.root, "teach the scouting report to wrap")
    _, former, _ = Claims(store, sandbox, "reaper").reap("Q1")
    assert former == "scout"


def test_the_shared_default_name_is_no_sign_of_life(sandbox, store):
    """`cli` is everyone's default: an amend by any `cli` user, or the next
    `ar loop` run, kept a dead `cli` claim alive for ever."""
    import datetime as dt
    _stale(sandbox, store, "cli")
    other = make_entry(store, "Q2")
    other.history.append(Event(dt.datetime.now(dt.UTC).isoformat(), "amended",
                               "cli", "someone else"))
    store.save(other)
    _, former, _ = Claims(store, sandbox, "reaper").reap("Q1")
    assert former == "cli"


def test_a_run_row_from_before_the_claim_is_no_sign_of_life(sandbox, store):
    _stale(sandbox, store, "a")
    append(sandbox.paths.runs / "a.jsonl", _row("old", 1, session="a",
                                                started=time.time() - 20 * 3600))
    _, former, _ = Claims(store, sandbox, "reaper").reap("Q1")
    assert former == "a"


def test_reap_and_validate_work_without_git(sandbox, store, monkeypatch):
    _stale(sandbox, store, "a")
    monkeypatch.setenv("PATH", str(sandbox.paths.root / "nowhere"))
    assert validate.git_warnings(sandbox) == []
    _, former, _ = Claims(store, sandbox, "reaper").reap("Q1")
    assert former == "a"


# -- #35 store tools --------------------------------------------------------

def test_a_superseded_duplicate_no_longer_warns(sandbox, store):
    close(sandbox, store, make_entry(store, "Q1", title="fold the cache"),
          verdict="confirmed")
    retired = make_entry(store, "Q2", title="fold the cache")
    close(sandbox, store, retired, verdict="superseded")
    assert validate.duplicate_warnings(sandbox) == []


def test_identical_changes_on_both_sides_are_not_a_collision(sandbox, store, capsys):
    root = sandbox.paths.root
    _git(root, "init", "-q", "-b", "main")
    make_entry(store, "Q1")
    _commit(root, "base")
    _git(root, "checkout", "-q", "-b", "other")
    entry = store.load("Q1")
    entry.title = "same"
    store.save(entry)
    _commit(root, "other")
    _git(root, "checkout", "-q", "main")
    _git(root, "cherry-pick", "other")
    assert _ar(sandbox, "entry", "collisions", "other") == 0


# -- #23 derived_from -------------------------------------------------------

def test_amending_a_parent_onto_a_derived_entry_clears_the_derivation(sandbox, store):
    make_entry(store, "Q1")
    make_entry(store, "Q2")
    make_entry(store, "Q3", derived_from="Q1")
    assert _ar(sandbox, "entry", "amend", "Q3", "--parent", "Q2", "--why", "x") == 0
    entry = store.load("Q3")
    assert entry.parent == "Q2" and entry.derived_from == ""


# -- #21 filing -------------------------------------------------------------

def test_create_and_save_never_share_a_temporary_file(sandbox, store, monkeypatch):
    from autoresearch.entries import Entry
    seen = []
    real = os.link

    def spy(src, dst):
        seen.append(os.path.basename(src))
        return real(src, dst)

    monkeypatch.setattr(os, "link", spy)
    store.create(Entry(id="Q1", track="research", title="t"))
    assert seen and seen[0] != f".Q1.yaml.{os.getpid()}.tmp"
