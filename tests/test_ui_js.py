"""The page's JavaScript, under node's built-in test runner (tests/js/).

The page's logic -- escaping, the side panel's HTML, what the graph draws,
search, and which documents a change makes stale -- lives in DOM-free
modules so it can be tested without a browser or any npm package. These
run wherever `node` is on PATH (GitHub's runners have it) and skip where it
is not.
"""
import json
import pathlib
import shutil
import subprocess

import pytest

from autoresearch.ui.api import READS

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATIC = ROOT / "src" / "autoresearch" / "ui" / "static"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not on PATH")


def test_the_node_suite_passes():
    files = sorted(str(p) for p in (ROOT / "tests" / "js").glob("*.test.mjs"))
    assert files
    result = subprocess.run([NODE, "--test", *files], cwd=ROOT,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]


def test_the_page_refetches_what_the_server_invalidates():
    """The page decides what to refetch from its own copy of READS; a copy
    that drifts from api.READS draws stale documents with no error."""
    script = (f"import({json.dumps((STATIC / 'live.js').as_uri())})"
              ".then(m => console.log(JSON.stringify(m.READS)))")
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True,
                         timeout=60, check=True).stdout
    page = json.loads(out)
    for name, reads in page.items():
        assert set(reads) == READS[name], name
