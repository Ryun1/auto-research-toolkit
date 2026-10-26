"""`ar init` writes `bin/ar` (H149): on macOS bare `ar` is /usr/bin/ar, the BSD
archiver, which answers plausibly instead of failing. The shim probes the
domain's venvs for the real toolkit and never falls through to the archiver."""
import json
import os
import stat
import subprocess
import sys

import pytest

from autoresearch.scaffold import init


@pytest.fixture
def root(tmp_path):
    init(tmp_path / "d", name="d")
    return tmp_path / "d"


def stub(root, candidate, marker):
    """A fake toolkit `ar` that prints its argv (and a marker) as JSON."""
    ar = root / candidate
    ar.parent.mkdir(parents=True)
    ar.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"print(json.dumps([{marker!r}] + sys.argv[1:]))\n")
    ar.chmod(0o755)
    return ar


def run_shim(root, *args, env=None, timeout=10):
    return subprocess.run(
        [str(root / "bin" / "ar"), *args], capture_output=True, text=True,
        env=env, timeout=timeout)


def test_init_writes_bin_ar_executable(root):
    ar = root / "bin" / "ar"
    assert ar.is_file()
    assert stat.S_IXUSR & ar.stat().st_mode
    first = ar.read_text().splitlines()[0]
    assert first == "#!/usr/bin/env python3"


def test_the_shim_probes_the_venv312_first(root):
    stub(root, ".venv312/bin/ar", "venv312")
    stub(root, ".venv/bin/ar", "venv")
    done = run_shim(root, "board")
    assert done.returncode == 0
    assert json.loads(done.stdout)[0] == "venv312"


def test_the_shim_probes_the_venv_when_venv312_is_absent(root):
    stub(root, ".venv/bin/ar", "venv")
    done = run_shim(root, "board")
    assert done.returncode == 0
    assert json.loads(done.stdout)[0] == "venv"


def test_the_shim_finds_an_enclosing_projects_venv(tmp_path):
    """A domain in a subdirectory of its project -- `domains/toy` inside the
    toolkit is one -- finds the venv at the repository root; the domain's own
    venv still wins over it."""
    init(tmp_path / "project" / "domains" / "d", name="d")
    domain = tmp_path / "project" / "domains" / "d"
    (tmp_path / "project" / ".git").mkdir()
    stub(tmp_path / "project", ".venv/bin/ar", "project")
    done = run_shim(domain, "board")
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)[:3] == ["project", "--domain",
                                           str(domain.resolve())]
    stub(domain, ".venv/bin/ar", "domain")
    assert json.loads(run_shim(domain, "board").stdout)[0] == "domain"


def no_toolkit_env(tmp_path):
    """A PATH whose python3 lacks the module, so only the venv probes count."""
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir(exist_ok=True)
    python3 = fakebin / "python3"
    python3.write_text("#!/bin/sh\nexit 1\n")
    python3.chmod(0o755)
    return {"PATH": str(fakebin), "HOME": os.environ.get("HOME", "")}


def run_shim_isolated(domain, env):
    return subprocess.run(
        [sys.executable, str(domain / "bin" / "ar"), "board"],
        capture_output=True, text=True, timeout=10, env=env)


def test_the_walk_stops_at_the_repository_root(tmp_path):
    """A venv above the enclosing repository -- a shared /tmp, say -- is never
    run, and a domain in no repository walks nowhere."""
    init(tmp_path / "shared" / "repo" / "d", name="d")
    domain = tmp_path / "shared" / "repo" / "d"
    planted = stub(tmp_path / "shared", ".venv/bin/ar", "planted")
    env = no_toolkit_env(tmp_path)
    assert run_shim_isolated(domain, env).returncode != 0, "no repo: no walk"
    (tmp_path / "shared" / "repo" / ".git").mkdir()
    done = run_shim_isolated(domain, env)
    assert done.returncode != 0 and "planted" not in done.stdout
    assert planted.exists()


def test_a_venv_others_can_write_is_not_run(tmp_path):
    init(tmp_path / "repo" / "d", name="d")
    domain = tmp_path / "repo" / "d"
    (tmp_path / "repo" / ".git").mkdir()
    ar = stub(tmp_path / "repo", ".venv/bin/ar", "writable")
    ar.parent.chmod(0o777)
    done = run_shim_isolated(domain, no_toolkit_env(tmp_path))
    assert done.returncode != 0 and "writable" not in done.stdout
    ar.parent.chmod(0o755)
    # The real PATH: the stub's own `env python3` shebang needs it.
    assert json.loads(run_shim(domain, "board").stdout)[0] == "writable"


def test_arguments_pass_through_to_the_toolkit(root):
    stub(root, ".venv/bin/ar", "venv")
    done = run_shim(root, "--board", "x")
    assert done.returncode == 0
    argv = json.loads(done.stdout)
    # the shim's own argument first, then the user's, in order
    assert argv[:3] == ["venv", "--domain", str(root.resolve())]
    assert argv[3:] == ["--board", "x"]


def test_the_shim_names_the_domain_it_lives_in(root):
    """A shim knows which domain it belongs to; the toolkit does not. Without
    `--domain`, running another domain's shim answers for whatever domain the
    caller is standing in (sig.golf H17)."""
    stub(root, ".venv/bin/ar", "venv")
    argv = json.loads(run_shim(root, "board").stdout)
    assert argv[argv.index("--domain") + 1] == str(root.resolve())


def test_a_shim_run_from_elsewhere_still_answers_for_its_own_domain(
        root, tmp_path):
    """The reproduction: two scaffolded domains, the shim of one run with the
    other as the working directory. It used to print the other domain's board."""
    elsewhere = tmp_path / "elsewhere"
    init(elsewhere, name="elsewhere")
    for domain in (root, elsewhere):
        stub(domain, ".venv/bin/ar", "venv")
    done = subprocess.run([str(root / "bin" / "ar"), "board"], cwd=elsewhere,
                         capture_output=True, text=True, timeout=10)
    assert done.returncode == 0
    assert json.loads(done.stdout)[2] == str(root.resolve())


def test_without_any_toolkit_the_failure_names_the_remedy(root, tmp_path):
    """No venv, and a `python3` on PATH without the module: the shim must exit
    nonzero with a loud message naming the remedy -- never run, and never be
    mistaken for, the BSD archiver."""
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    python3 = fakebin / "python3"
    python3.write_text("#!/bin/sh\nexit 1\n")
    python3.chmod(0o755)

    # Invoked via `python3 bin/ar` so the shebang's PATH lookup cannot pick up
    # the fake interpreter; PATH then governs only the shim's own probes.
    done = subprocess.run(
        [sys.executable, str(root / "bin" / "ar"), "board"],
        capture_output=True, text=True, timeout=10,
        env={"PATH": str(fakebin), "HOME": os.environ.get("HOME", "")})
    assert done.returncode != 0
    assert "autoresearch" in done.stderr
    assert ".venv312/bin/ar" in done.stderr
    assert "archiver" in done.stderr
