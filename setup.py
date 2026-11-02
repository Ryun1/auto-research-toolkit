"""Build-time hook: record which commit a core was built from (#62).

pip records a commit only for a `git+` install. A core installed from a local
checkout (`pip install /path/to/auto-research-toolkit`, not editable) carried
nothing but the pyproject version. Between releases that is the *last*
release's number, so every `core:` stamp it wrote named code it did not run.

`build_py` therefore writes `autoresearch/_build.py` into the build tree:
the checkout's HEAD, whether src/ had uncommitted changes, and the tags at
HEAD. It writes nothing when there is no git checkout (an sdist build), and
nothing for an editable install, which reads the live checkout at run time.
Everything else lives in pyproject.toml.
"""
import pathlib
import subprocess

from setuptools import setup
from setuptools.command.build_py import build_py

ROOT = pathlib.Path(__file__).resolve().parent


def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True,
                          text=True, check=True, timeout=30).stdout.strip()


def git_state() -> dict | None:
    """HEAD, dirtiness and tags of the checkout this file sits at the top of;
    None when it is not one. A tree vendored inside some other repository is
    not a checkout of this project, and that repository's HEAD would lie."""
    try:
        if pathlib.Path(_git("rev-parse", "--show-toplevel")).resolve() != ROOT:
            return None
        commit = _git("rev-parse", "HEAD")
        dirty = bool(_git("status", "--porcelain", "--", "src",
                          "pyproject.toml", "setup.py"))
        tags = tuple(_git("tag", "--points-at", "HEAD").split())
    except (OSError, subprocess.SubprocessError):
        return None
    return {"commit": commit, "dirty": dirty, "tags": tags}


class BuildPyWithCommit(build_py):
    def run(self):
        super().run()
        if getattr(self, "editable_mode", False):
            return
        state = git_state()
        if state is None:
            return
        target = pathlib.Path(self.build_lib) / "autoresearch" / "_build.py"
        target.write_text(
            '"""Written by setup.py at build time; not in version control."""\n'
            f"COMMIT = {state['commit']!r}\n"
            f"DIRTY = {state['dirty']!r}\n"
            f"TAGS = {state['tags']!r}\n")


setup(cmdclass={"build_py": BuildPyWithCommit})
