"""Pulling improvements: check, update, and the rollback that makes it safe.

The network is faked with local git repositories, so every gate here runs the
real git plumbing. The two properties under test are the ones that make an
automated upgrade honest: only NEW validation problems roll an upgrade back,
and what lands is always the resolved SHA, never a moving ref name.
"""
import subprocess

import pytest

from autoresearch import upstream as up
from autoresearch.cli import main as ar
from autoresearch.config import Upstream
from autoresearch.errors import AutoresearchError
from autoresearch.policy import PolicyError, Rule
from autoresearch.upstream import Install, UpdatePlan


def git(*args, cwd):
    proc = subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, check=True)
    return proc.stdout.strip()


@pytest.fixture
def upstream_repo(tmp_path):
    """A real git repository standing in for the remote."""
    repo = tmp_path / "core-repo"
    repo.mkdir()
    git("init", "-b", "main", cwd=repo)

    def commit(message):
        (repo / "f.txt").write_text(message + "\n")
        git("add", ".", cwd=repo)
        git("-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-m", message, cwd=repo)
        return git("rev-parse", "HEAD", cwd=repo)

    first = commit("first release")
    return repo, commit, first


@pytest.fixture
def upstream(toy, upstream_repo):
    repo, _, _ = upstream_repo
    toy.upstream = Upstream(url=str(repo), ref="main")
    return toy


def plan_for(toy, commit, monkeypatch=None, **install_kw):
    """A plan against the fixture's upstream, with `installed` pinned to a
    commit of our choosing rather than whatever the test venv really has."""
    def fake():
        return Install(version="0.1.0", commit=commit,
                       url="https://example.invalid/autoresearch", **install_kw)
    if monkeypatch is not None:
        monkeypatch.setattr(up, "installed", fake)
        return up.plan_update(toy)
    real, up.installed = up.installed, fake
    try:
        return up.plan_update(toy)
    finally:
        up.installed = real


# -- the plan ---------------------------------------------------------------

def test_a_commit_behind_head_is_behind_and_shows_what_changed(upstream, upstream_repo):
    repo, commit, first = upstream_repo
    second = commit("Ranking: fix the reserve ordering")
    plan = plan_for(upstream, first)
    assert plan.behind and plan.head == second
    assert plan.changelog and "Ranking: fix the reserve ordering" in plan.changelog


def test_the_head_commit_is_up_to_date(upstream, upstream_repo):
    repo, commit, first = upstream_repo
    plan = plan_for(upstream, first)
    assert not plan.behind and plan.changelog is None


def test_a_missing_ref_is_a_loud_error_not_an_up_to_date(upstream, toy):
    toy.upstream = Upstream(url=str(upstream.upstream.url), ref="nope")
    with pytest.raises(AutoresearchError, match="no branch or tag"):
        plan_for(toy, "0" * 40)


def test_a_version_only_install_compares_against_tags(upstream, upstream_repo,
                                                     monkeypatch):
    repo, _, _ = upstream_repo
    git("tag", "v0.1.0", cwd=repo)
    git("tag", "v0.2.0", cwd=repo)
    monkeypatch.setattr(up, "installed", lambda: Install(version="0.1.0"))
    assert up.plan_update(upstream).behind, "0.1.0 with v0.2.0 tagged must read as behind"
    monkeypatch.setattr(up, "installed", lambda: Install(version="0.2.0"))
    assert not up.plan_update(upstream).behind


def test_an_editable_install_says_so_instead_of_guessing(toy, monkeypatch):
    toy.upstream = Upstream()
    monkeypatch.setattr(up, "installed", lambda: Install(
        version="0.1.0", editable=True, url="file:///some/checkout"))
    with pytest.raises(AutoresearchError, match="editable"):
        up.upstream_url(toy)


def test_a_domain_with_no_source_at_all_is_told_how_to_fix_it(toy, monkeypatch):
    toy.upstream = Upstream()
    monkeypatch.setattr(up, "installed", lambda: Install(version="0.1.0"))
    with pytest.raises(AutoresearchError, match=r"\[upstream\]"):
        up.upstream_url(toy)


def test_pip_lands_on_the_resolved_sha_not_the_ref_name():
    cmd = up.pip_command("https://github.com/x/y", "abc123")
    assert cmd[-1] == "autoresearch @ git+https://github.com/x/y@abc123"
    local = up.pip_command("/some/local/repo", "abc123")
    assert local[-1] == "autoresearch @ git+file:///some/local/repo@abc123"


# -- applying it --------------------------------------------------------------

class FakeInstaller:
    def __init__(self):
        self.commands = []

    def __call__(self, cmd):
        self.commands.append(cmd)
        return "9.9.9"


def make_plan(upstream_repo, old_commit, behind=True):
    repo, _, _ = upstream_repo
    return UpdatePlan(
        install=Install(version="0.1.0", commit=old_commit,
                        url=str(repo), ref="main"),
        url=str(repo), ref="main", head="f" * 40, behind=behind)


def test_a_clean_upgrade_records_itself_in_the_inbox(sandbox, upstream_repo):
    repo, _, first = upstream_repo
    installer = FakeInstaller()

    def verifier(config):
        return []

    result = up.apply_update(sandbox, make_plan(upstream_repo, first),
                             installer=installer, verifier=verifier)
    assert result.ok and not result.rolled_back
    assert len(installer.commands) == 1
    assert installer.commands[0][-1].endswith("@" + "f" * 40), \
        "what lands is the resolved SHA, not a moving ref name"
    memos = list(sandbox.paths.memos.glob("core-update-*.md"))
    assert len(memos) == 1, "an upgrade the record does not know about is H39"
    body = memos[0].read_text()
    assert "0.1.0" in body and "9.9.9" in body and str(repo) in body


def test_only_NEW_problems_roll_an_upgrade_back(sandbox, upstream_repo):
    """A project with an incomplete defect entry pre-dating the upgrade must
    still be able to take a core fix: the same problems before and after are
    not the upgrade's fault."""
    repo, _, first = upstream_repo
    installer = FakeInstaller()

    def verifier(config):
        return ["H1: missing repro -- an opinion, not a record"]

    result = up.apply_update(sandbox, make_plan(upstream_repo, first),
                             installer=installer, verifier=verifier)
    assert result.ok
    assert len(installer.commands) == 1, "a pre-existing problem must not roll back"
    assert list(sandbox.paths.memos.glob("core-update-*.md")), \
        "the upgrade happened, so the record says so"


def test_a_new_problem_rolls_back_to_the_recorded_commit(sandbox, upstream_repo):
    repo, _, first = upstream_repo
    installer = FakeInstaller()
    calls = []

    def verifier(config):
        calls.append(1)
        return [] if len(calls) == 1 else ["Ranking: the rank formula crashed"]

    result = up.apply_update(sandbox, make_plan(upstream_repo, first),
                             installer=installer, verifier=verifier,
                             )
    assert not result.ok and result.rolled_back
    assert len(installer.commands) == 2, "upgrade, then rollback"
    assert installer.commands[1][-1].endswith(f"@{first}"), \
        "rollback lands on the commit the project had, exactly"
    assert not list(sandbox.paths.memos.glob("core-update-*.md")), \
        "a rolled-back upgrade must not read as one that happened"
    assert any("rolled back" in d for d in result.detail)


def test_a_rolled_back_upgrade_re_renders_with_the_old_core(sandbox, upstream_repo):
    """The views were just re-rendered by the new core; the rollback owns the
    views again, so the verifier must run a third time or the project is left
    with a generated document its own validator refuses."""
    repo, _, first = upstream_repo
    calls = []

    def verifier(config):
        calls.append(1)
        return []

    up.apply_update(sandbox, make_plan(upstream_repo, first),
                    installer=FakeInstaller(), verifier=verifier)
    assert len(calls) == 2
    calls.clear()

    def failing_verifier(config):
        calls.append(1)
        return [] if len(calls) == 1 else ["new problem after upgrade"]

    up.apply_update(sandbox, make_plan(upstream_repo, first),
                    installer=FakeInstaller(), verifier=failing_verifier)
    assert len(calls) == 3, "rollback re-renders before handing back"


def test_the_upgrade_respects_human_only_policy(sandbox, upstream_repo):
    """`ar harness update` runs a pip install, so a domain that gates pip
    behind a human gates upgrades behind a human -- one engine, no special
    case for the update path."""
    repo, _, first = upstream_repo
    sandbox.policy.human_only.append(Rule(
        kind="human_only", pattern="pip install",
        reason="this project upgrades its core only when a person decides to",
        example="pip install autoresearch"))
    installer = FakeInstaller()
    with pytest.raises(PolicyError, match="human-only"):
        up.apply_update(sandbox, make_plan(upstream_repo, first),
                        installer=installer, verifier=lambda config: [])
    assert installer.commands == [], "the gate fires before anything is installed"


def test_already_at_head_is_a_no_op_without_an_install(sandbox, upstream_repo):
    repo, _, first = upstream_repo
    plan = make_plan(upstream_repo, first, behind=False)
    plan.head = first
    installer = FakeInstaller()
    result = up.apply_update(sandbox, plan, installer=installer,
                             verifier=lambda config: [])
    assert result.ok and installer.commands == []


# -- the command surface ------------------------------------------------------

def test_check_exit_codes_are_machine_readable(toy, monkeypatch, capsys):
    class FakePlan:
        install = Install(version="0.1.0", commit="a" * 40)
        url = "https://example.invalid/x"
        ref = "main"
        head = "b" * 40
        behind = True
        changelog = "  abc123 Ranking: something"

        def summary(self):
            return "installed   core 0.1.0 @ aaaaaaaaaaaa\nupstream    x main @ bbbbbbbbbbbb"

    monkeypatch.setattr(up, "plan_update", lambda config, ref=None: FakePlan())
    assert ar(["--domain", str(toy.paths.root), "harness", "check"]) == 1
    assert "update available" in capsys.readouterr().out

    FakePlan.behind = False
    assert ar(["--domain", str(toy.paths.root), "harness", "check"]) == 0
    assert "up to date" in capsys.readouterr().out


def test_an_editable_checkout_is_compared_by_its_own_head(upstream, upstream_repo,
                                                         monkeypatch, tmp_path):
    """A source checkout (this repository, for one) has a git HEAD, so the
    honest comparison is that commit against upstream -- not a shrug."""
    repo, commit, first = upstream_repo
    second = commit("Budgets: spend a meter that was declared")
    # the project's checkout: a clone left one commit behind upstream
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "clone", "--quiet", str(repo), str(checkout)],
                   check=True, capture_output=True)
    git("checkout", "--quiet", first, cwd=checkout)
    monkeypatch.setattr(up, "installed", lambda: Install(
        version="0.1.0", editable=True, url="file://" + str(checkout)))
    plan = up.plan_update(upstream)
    assert plan.behind and plan.head == second
    assert plan.changelog and "Budgets: spend a meter" in plan.changelog


def test_a_checkout_ahead_of_origin_has_nothing_to_pull(upstream, upstream_repo,
                                                       monkeypatch, tmp_path):
    """The inverse false alarm: a checkout ahead of origin must not read as
    'update available' pointing backwards. This repository, mid-development,
    is exactly that case."""
    repo, commit, first = upstream_repo
    commit("Budgets: spend a meter that was declared")   # the checkout takes this; origin does not
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "--quiet", "--bare", str(repo),
                    str(origin)], check=True, capture_output=True)
    git("update-ref", "refs/heads/main", first, cwd=origin)
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "clone", "--quiet", str(repo), str(checkout)],
                   check=True, capture_output=True)
    upstream.upstream = Upstream(url=str(origin), ref="main")
    monkeypatch.setattr(up, "installed", lambda: Install(
        version="0.1.0", editable=True, url="file://" + str(checkout)))
    plan = up.plan_update(upstream)
    assert plan.head == first
    assert not plan.behind


def test_update_refuses_to_pip_replace_an_editable_checkout(sandbox, upstream_repo):
    repo, _, first = upstream_repo

    def verifier(config):
        return []

    plan = UpdatePlan(
        install=Install(version="0.1.0", editable=True,
                        url="file://" + str(repo)),
        url=str(repo), ref="main", head="f" * 40, behind=True)
    with pytest.raises(AutoresearchError, match="editable"):
        up.apply_update(sandbox, plan, installer=FakeInstaller(),
                        verifier=verifier)


def test_the_scaffold_ships_an_upstream_table(tmp_path):
    from autoresearch.config import DomainConfig
    from autoresearch.scaffold import init
    init(tmp_path / "d", name="d")
    config = DomainConfig.load(tmp_path / "d")
    assert config.upstream.url.startswith("https://")
    assert config.upstream.ref == "main"
