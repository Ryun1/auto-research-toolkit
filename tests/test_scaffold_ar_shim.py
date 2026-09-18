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


def test_arguments_pass_through_to_the_toolkit(root):
    stub(root, ".venv/bin/ar", "venv")
    done = run_shim(root, "--board", "x")
    assert done.returncode == 0
    assert json.loads(done.stdout) == ["venv", "--board", "x"]


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
