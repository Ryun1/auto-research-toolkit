"""The session lifecycle: create, destroy, prune.

Every test runs against a real git repository with a scaffolded, committed
domain, because the whole point of the module is what git worktrees do with
real indexes. The semantics under test are the field harness's, paid for in
its debt log: harvest before destroy (a vanished worktree once took its run
rows with it), config drift refuses destruction (a drifted config split a
ledger across two run paths in the hr-iter-0902 clone), and prune proposes
while only destroy disposes.
"""
import json
import os
import pathlib
import subprocess

import pytest

from autoresearch import runs, scaffold
from autoresearch import session as session_mod
from autoresearch.config import DomainConfig
from autoresearch.entries import Entry, Store
from autoresearch.errors import ConfigError
from autoresearch.runs import RunRecord

GIT_ENV = {
    "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.com",
}


def git(*args, cwd):
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True,
        check=True, env={**os.environ, **GIT_ENV})
    return proc.stdout.strip()


def scaffolded(tmp_path, name="demo-domain"):
    """A scaffolded (not yet git-committed) domain."""
    scaffold.init(tmp_path / name, name.removesuffix("-domain"))
    return tmp_path / name


def committed_domain(tmp_path, name="demo-domain"):
    """A real git repo holding a committed, scaffolded domain."""
    root = scaffolded(tmp_path, name)
    store = Store(root / "state" / "entries")
    store.save(Entry(id="Q1", track="research", title="seed"))
    git("init", "-q", "-b", "main", cwd=root)
    git("add", "-A", cwd=root)
    git("commit", "-qm", "seed", cwd=root)
    return DomainConfig.load(root)


@pytest.fixture
def domain(tmp_path):
    return committed_domain(tmp_path)


def a_row(session, cost=3.0):
    return RunRecord(metrics={"cost": cost}, session=session,
                     provenance={"who": "test"})


def worktree_of(domain, name):
    return pathlib.Path(json.loads(
        (domain.paths.root / ".ar" / "sessions" / f"{name}.json").read_text()
    )["path"])


def record_path(domain, name):
    return domain.paths.root / ".ar" / "sessions" / f"{name}.json"


# -- create ----------------------------------------------------------------

def test_create_makes_worktree_and_record(domain):
    record = session_mod.create(domain, "loop-a")
    expected = domain.paths.root.parent / "demo-domain-session-loop-a"
    assert pathlib.Path(record["path"]) == expected
    assert expected.is_dir() and (expected / "domain.toml").is_file()
    stored = json.loads(record_path(domain, "loop-a").read_text())
    assert stored["name"] == "loop-a"
    assert stored["base"] == git("rev-parse", "HEAD", cwd=domain.paths.root)
    assert len(stored["config_sha256"]) == 64
    assert stored["settle_hours"] == domain.session_settle_hours
    assert stored["created_at"]


def test_duplicate_create_is_refused(domain):
    session_mod.create(domain, "loop-a")
    with pytest.raises(ConfigError, match="loop-a"):
        session_mod.create(domain, "loop-a")


def test_create_refuses_an_existing_worktree_without_a_record(domain):
    """A path present but unrecorded is still reuse of one index (H70)."""
    stray = domain.paths.root.parent / "demo-domain-session-loop-a"
    stray.mkdir()
    with pytest.raises(ConfigError) as refused:
        session_mod.create(domain, "loop-a")
    assert str(stray) in str(refused.value)


@pytest.mark.parametrize("bad", ["Big", "-lead", "under_score", "a b",
                                 "x" * 49, ""])
def test_create_refuses_a_non_slug_name(domain, bad):
    with pytest.raises(ConfigError, match="slug"):
        session_mod.create(domain, bad)


def test_create_refuses_a_non_git_root(tmp_path):
    root = scaffolded(tmp_path, "plain-domain")
    config = DomainConfig.load(root)
    with pytest.raises(ConfigError) as refused:
        session_mod.create(config, "loop-a")
    assert "not a git repository" in str(refused.value)
    assert str(root) in str(refused.value)


def test_create_resolves_a_relative_session_parent_against_the_root(domain):
    domain.session_parent = "sessions"
    record = session_mod.create(domain, "loop-a")
    assert pathlib.Path(record["path"]) == \
        domain.paths.root / "sessions" / "demo-domain-session-loop-a"


def test_create_uses_an_absolute_session_parent_as_is(domain, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    domain.session_parent = str(elsewhere)
    record = session_mod.create(domain, "loop-a")
    assert pathlib.Path(record["path"]) == \
        elsewhere / "demo-domain-session-loop-a"


# -- destroy: config drift --------------------------------------------------

def test_destroy_refuses_config_drift_then_accepts_it(domain):
    session_mod.create(domain, "loop-a")
    wt = worktree_of(domain, "loop-a")
    config_path = domain.paths.root / "domain.toml"
    config_path.write_text(config_path.read_text() + "\n# drifted\n")
    with pytest.raises(ConfigError, match="changed"):
        session_mod.destroy(domain, "loop-a")
    out = session_mod.destroy(domain, "loop-a", accept_drift=True)
    assert out["destroyed"]
    assert not record_path(domain, "loop-a").exists()
    assert not wt.exists()
    archives = list((domain.paths.root / ".ar" / "sessions"
                     / "archived").glob("loop-a.*.json"))
    assert len(archives) == 1
    assert json.loads(archives[0].read_text())["name"] == "loop-a"


# -- destroy: run rows -------------------------------------------------------

def test_destroy_refuses_unharvested_rows_then_harvests_them(domain):
    session_mod.create(domain, "loop-a")
    wt = worktree_of(domain, "loop-a")
    row = a_row("loop-a")
    runs.append(wt / "data" / "runs" / "loop-a.jsonl", row)
    with pytest.raises(ConfigError) as refused:
        session_mod.destroy(domain, "loop-a")
    message = str(refused.value)
    assert "loop-a.jsonl" in message and "1 run row" in message
    assert not list(domain.paths.runs.glob("*.jsonl"))  # refused untouched

    out = session_mod.destroy(domain, "loop-a", harvest=True)
    assert out["harvested_runs"] == [{"file": "data/runs/loop-a.jsonl",
                                      "rows": 1}]
    harvested = runs.read_all(domain.paths.runs)
    assert [r.id for r in harvested] == [row.id]
    assert not wt.exists()
    assert not record_path(domain, "loop-a").exists()


def test_harvest_is_idempotent_across_two_sessions(domain):
    """A destroy that harvests rows then refuses on a later check must not
    duplicate them when the harvest re-runs (dedupe by run id)."""
    session_mod.create(domain, "loop-a")
    wt = worktree_of(domain, "loop-a")
    row = a_row("loop-a")
    runs.append(wt / "data" / "runs" / "loop-a.jsonl", row)
    # Same id already in the primary: the scan must not call it unharvested.
    runs.append(domain.paths.runs / "loop-a.jsonl", row)
    out = session_mod.destroy(domain, "loop-a", harvest=True)
    assert out["harvested_runs"] == []
    assert len(runs.read_all(domain.paths.runs)) == 1


# -- destroy: entries --------------------------------------------------------

def test_destroy_refuses_a_divergent_entry_even_with_harvest(domain):
    session_mod.create(domain, "loop-a")
    wt = worktree_of(domain, "loop-a")
    entry = wt / "state" / "entries" / "Q1.yaml"
    entry.write_text(entry.read_text() + "# amended in session\n")
    before = (domain.paths.entries / "Q1.yaml").read_bytes()
    with pytest.raises(ConfigError, match="Q1.yaml"):
        session_mod.destroy(domain, "loop-a", harvest=True)
    assert (domain.paths.entries / "Q1.yaml").read_bytes() == before


def test_destroy_harvests_an_entry_the_primary_lacks(domain):
    session_mod.create(domain, "loop-a")
    wt = worktree_of(domain, "loop-a")
    Store(wt / "state" / "entries").save(
        Entry(id="Q2", track="research", title="from session"))
    with pytest.raises(ConfigError, match="Q2.yaml"):
        session_mod.destroy(domain, "loop-a")
    out = session_mod.destroy(domain, "loop-a", harvest=True)
    assert out["harvested_entries"] == ["state/entries/Q2.yaml"]
    assert Store(domain.paths.entries).load("Q2").title == "from session"
    assert not wt.exists()


# -- destroy: memos ----------------------------------------------------------

def test_destroy_refuses_a_divergent_memo_and_never_harvests_it(domain):
    session_mod.create(domain, "loop-a")
    wt = worktree_of(domain, "loop-a")
    # an empty directory is not committed, so the worktree has no inbox yet
    (wt / "inbox").mkdir()
    memo = wt / "inbox" / "loop-a-finding.md"
    memo.write_text("a raw finding\n")
    with pytest.raises(ConfigError, match="loop-a-finding.md"):
        session_mod.destroy(domain, "loop-a", harvest=True)
    assert memo.exists()            # refused untouched
    assert not (domain.paths.memos / "loop-a-finding.md").exists()


# -- prune -------------------------------------------------------------------

def test_prune_reports_and_destroys_nothing(domain):
    session_mod.create(domain, "loop-a")
    wt = worktree_of(domain, "loop-a")
    runs.append(wt / "data" / "runs" / "loop-a.jsonl", a_row("loop-a"))
    report = session_mod.prune(domain)
    assert len(report) == 1
    row = report[0]
    assert row["name"] == "loop-a"
    assert row["worktree_exists"] is True
    assert row["clean"] is False            # the appended row is uncommitted
    assert row["unharvested"] == 1
    assert row["idle_hours"] < row["settle_hours"]
    assert row["eligible"] is False
    assert wt.exists() and record_path(domain, "loop-a").exists()


def test_prune_proposes_a_settled_clean_session_without_destroying(domain):
    session_mod.create(domain, "loop-a")
    record_path(domain, "loop-a").write_text(json.dumps({
        **json.loads(record_path(domain, "loop-a").read_text()),
        "created_at": "2026-01-01T00:00:00+00:00",
    }))
    row = session_mod.prune(domain)[0]
    assert row["idle_hours"] > row["settle_hours"]
    assert row["clean"] is True and row["unharvested"] == 0
    assert row["eligible"] is True
    # prune proposes; destroy is the only destructive path
    assert worktree_of(domain, "loop-a").exists()
    assert record_path(domain, "loop-a").exists()


def test_prune_with_no_sessions_is_empty(domain):
    assert session_mod.prune(domain) == []


def test_prune_destroy_destroys_an_eligible_session(domain):
    """The CLI's prune --destroy path: eligibility comes from the record's
    created_at against its settle window, and destruction goes through the
    same guards as an explicit destroy."""
    from autoresearch import cli

    session_mod.create(domain, "old")
    record_path = domain.paths.root / ".ar" / "sessions" / "old.json"
    record = json.loads(record_path.read_text())
    record["created_at"] = "2026-01-01T00:00:00+00:00"
    record_path.write_text(json.dumps(record))

    wt = worktree_of(domain, "old")
    assert wt.exists()
    rc = cli.main(["--domain", str(domain.paths.root),
                   "session", "prune", "--destroy"])
    assert rc == 0
    assert not wt.exists()
    assert not record_path.exists()
    archived = list((domain.paths.root / ".ar" / "sessions" / "archived").glob(
        "old.*.json"))
    assert len(archived) == 1
