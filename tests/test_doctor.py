"""`ar doctor` — the installed core vs what the domain's records assume.

The first field domain ran a copied (non-editable) install whose staleness
was only discoverable by md5-comparing site-packages against the checkout,
and pinned its reopen conditions to "reinstall >= the fix commit" by hand.
`min_core` in `[upstream]` makes that floor a declaration; `ar doctor` makes
it enforced, `ar validate` names it without failing.
"""
import pytest

from autoresearch import cli, validate
from autoresearch.upstream import Install, doctor, min_core_problem, version_tuple


@pytest.fixture
def install(monkeypatch):
    """A deterministic stand-in for pip's metadata."""
    def set_version(version, editable=False):
        inst = Install(version=version, url="file:///core", editable=editable)
        monkeypatch.setattr("autoresearch.upstream.installed", lambda: inst)
        return inst
    return set_version


def test_version_tuple_reads_numeric_components_only():
    assert version_tuple("0.2.0") == (0, 2, 0)
    assert version_tuple("1.0.10") > version_tuple("1.0.9")
    # A dev suffix or a hash cannot be compared; that must read as None,
    # never as the oldest possible version.
    assert version_tuple("0.2.0.dev1") is None
    assert version_tuple("unknown") is None


def test_a_floor_at_or_below_the_install_passes(sandbox, install):
    install("0.2.0")
    sandbox.upstream.min_core = "0.1.9"
    assert min_core_problem(sandbox) is None
    sandbox.upstream.min_core = "0.2.0"
    assert min_core_problem(sandbox) is None


def test_a_floor_above_the_install_names_the_remedy(sandbox, install):
    install("0.1.0")
    sandbox.upstream.min_core = "0.2.0"
    problem = min_core_problem(sandbox)
    assert problem and "reinstall" in problem and "0.2.0" in problem


def test_an_incomparable_install_is_a_problem_not_a_pass(sandbox, install):
    install("unknown")
    sandbox.upstream.min_core = "0.2.0"
    assert "cannot be compared" in min_core_problem(sandbox)


def test_no_floor_declared_means_no_check(sandbox, install):
    install("0.0.1")
    assert min_core_problem(sandbox) is None
    lines, problems = doctor(sandbox)
    assert any("not declared" in ln for ln in lines)
    assert problems == []


def test_doctor_reports_identity_and_enforces_the_floor(sandbox, install, capsys):
    install("0.1.0", editable=False)
    toml = sandbox.paths.root / "domain.toml"
    toml.write_text(toml.read_text().replace(
        '[upstream]', '[upstream]\nmin_core = "0.2.0"'))
    sandbox.upstream.min_core = "0.2.0"
    lines, problems = doctor(sandbox)
    assert any("0.1.0" in ln and "copied" in ln for ln in lines), lines
    assert any("min_core:  0.2.0" in ln for ln in lines)
    assert len(problems) == 1

    assert cli.main(["--domain", str(sandbox.paths.root), "doctor"]) == 1
    out = capsys.readouterr().out
    assert "below this domain's min_core 0.2.0" in out


def test_validate_names_a_stale_core_without_failing(sandbox, install, capsys):
    install("0.1.0")
    toml = sandbox.paths.root / "domain.toml"
    toml.write_text(toml.read_text().replace(
        '[upstream]', '[upstream]\nmin_core = "0.2.0"'))
    from autoresearch.config import DomainConfig
    reloaded = DomainConfig.load(sandbox.paths.root)
    assert validate.core_warnings(reloaded)
    assert cli.main(["--domain", str(sandbox.paths.root), "render"]) == 0
    assert cli.main(["--domain", str(sandbox.paths.root), "validate"]) == 0
    assert "reinstall" in capsys.readouterr().out
