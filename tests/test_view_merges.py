"""#24: generated views churn and conflict in multi-session trees.

Every `entry amend` / `close` re-renders the shared views, so two sessions'
branches both change them and the merge conflicts on files nobody edits by
hand -- once a conflict marker landed in a generated queue view, and "render
only on a clean tree" could not be satisfied alongside concurrent work.

`ar render --install-merge-driver` marks every generated view with a merge
driver that keeps the current side, so a merge never conflicts on (or writes
markers into) a view; `ar render` afterwards brings it up to date. A domain
that would rather not fail validate on that window sets
`[render] stale_views = "warning"`.
"""
import subprocess

from autoresearch import cli, merge, render
from autoresearch.config import DomainConfig
from conftest import close, make_entry


def _git(root, *args, check=True):
    return subprocess.run(["git", *args], cwd=root, check=check,
                          capture_output=True, text=True)


def _commit(root, message):
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=T", "-c", "user.email=t@example.invalid",
         "commit", "-qm", message)


def _ar(config, *args):
    return cli.main(["--domain", str(config.paths.root), *args])


def test_installing_the_driver_marks_every_view_once(sandbox, capsys):
    _git(sandbox.paths.root, "init", "-q")
    assert _ar(sandbox, "render", "--install-merge-driver") == 0
    assert _ar(sandbox, "render", "--install-merge-driver") == 0
    attributes = (sandbox.paths.root / ".gitattributes").read_text().splitlines()
    for track in sandbox.tracks.values():
        line = f"{track.view.replace(' ', '[[:space:]]')} merge=ar-view"
        assert attributes.count(line) == 1
    driver = _git(sandbox.paths.root, "config", "merge.ar-view.driver").stdout
    assert driver.strip() == "true"


def test_outside_git_the_driver_is_refused(sandbox, capsys):
    assert _ar(sandbox, "render", "--install-merge-driver") == 2
    assert "git" in capsys.readouterr().err


def _two_sessions(sandbox, store):
    root = sandbox.paths.root
    _git(root, "init", "-q", "-b", "main")
    make_entry(store, "Q1")
    make_entry(store, "Q2")
    render.write_views(sandbox, store.all())
    _commit(root, "base")
    _git(root, "checkout", "-q", "-b", "other")
    close(sandbox, store, store.load("Q2"), verdict="confirmed")
    render.write_views(sandbox, store.all())
    _commit(root, "other closes Q2")
    _git(root, "checkout", "-q", "main")
    from autoresearch.entries import _ENTRY_CACHE
    _ENTRY_CACHE.clear()
    close(sandbox, store, store.load("Q1"), verdict="confirmed")
    render.write_views(sandbox, store.all())
    _commit(root, "main closes Q1")


def test_with_the_driver_a_merge_never_conflicts_on_a_view(sandbox, store):
    _two_sessions(sandbox, store)
    assert _ar(sandbox, "render", "--install-merge-driver") == 0
    _commit(sandbox.paths.root, "merge driver")
    merged = _git(sandbox.paths.root, "-c", "user.name=T", "-c",
                  "user.email=t@example.invalid", "merge", "-q", "--no-edit",
                  "other", check=False)
    assert merged.returncode == 0, merged.stdout + merged.stderr
    for track in sandbox.tracks.values():
        text = (sandbox.paths.root / track.view).read_text()
        assert not merge.marker_lines(text)
    from autoresearch.entries import _ENTRY_CACHE, Store
    _ENTRY_CACHE.clear()
    entries = Store(sandbox.paths.entries).all()
    assert render.check_views(sandbox, entries)          # stale until rendered
    assert _ar(sandbox, "render") == 0
    assert render.check_views(sandbox, Store(sandbox.paths.entries).all()) == []


def test_without_the_driver_the_same_merge_conflicts(sandbox, store):
    _two_sessions(sandbox, store)
    merged = _git(sandbox.paths.root, "-c", "user.name=T", "-c",
                  "user.email=t@example.invalid", "merge", "-q", "--no-edit",
                  "other", check=False)
    assert merged.returncode != 0


def test_a_stale_view_is_a_problem_by_default(sandbox, store, capsys):
    make_entry(store, "Q1")
    render.write_views(sandbox, store.all())
    make_entry(store, "Q2")
    assert _ar(sandbox, "validate") == 1


def test_a_domain_may_take_stale_views_as_a_warning(sandbox, store, capsys):
    toml = sandbox.paths.root / "domain.toml"
    toml.write_text(toml.read_text() + '\n[render]\nstale_views = "warning"\n')
    config = DomainConfig.load(sandbox.paths.root)
    make_entry(store, "Q1")
    render.write_views(config, store.all())
    make_entry(store, "Q2")
    capsys.readouterr()
    assert _ar(config, "validate") == 0
    out = capsys.readouterr().out
    assert "differs from what the records render" in out


def test_an_unknown_stale_views_setting_is_refused(sandbox):
    import pytest

    from autoresearch.errors import ConfigError
    toml = sandbox.paths.root / "domain.toml"
    toml.write_text(toml.read_text() + '\n[render]\nstale_views = "ignore"\n')
    with pytest.raises(ConfigError, match="stale_views"):
        DomainConfig.load(sandbox.paths.root)
