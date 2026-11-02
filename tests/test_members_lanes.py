"""A member's lanes are written against the member's own root (#64), but git
reports paths from the repository top. Tamper scanning rebases each path onto
the domain before it reaches the lanes, so a member's `state/entries/` is its
findings and a path outside the member is never matched by accident."""
import subprocess

from autoresearch import lanes, tamper
from autoresearch.config import DomainConfig

CLOCK = "t = time.perf_counter()\n"


def _git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _commit(root, rel, text="x = 1\n"):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    _git(root, "add", rel)
    _git(root, "commit", "-q", "-m", f"add {rel}")
    return path


def _scan(root):
    member = root / "domains" / "a"
    return tamper.scan_tree(DomainConfig.load(member), member)


def test_rebase_inside_and_outside_a_domain():
    assert lanes.rebase("domains/a/bin/x.py", "domains/a/") == "bin/x.py"
    assert lanes.rebase("tools/y.py", "domains/a/") is None
    assert lanes.rebase("domains/ab/x.py", "domains/a/") is None
    assert lanes.rebase("bin/x.py", "") == "bin/x.py"


def test_a_tracked_member_findings_file_is_findings(git_umbrella):
    path = _commit(git_umbrella, "domains/a/state/entries/A9.yaml")
    path.write_text("x = 1\n" + CLOCK)
    assert _scan(git_umbrella) == []


def test_an_untracked_member_findings_file_is_findings(git_umbrella):
    path = git_umbrella / "domains/a/state/entries/A9.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CLOCK)
    assert _scan(git_umbrella) == []


def test_a_member_scaffolding_file_is_reported_member_relative(git_umbrella):
    path = _commit(git_umbrella, "domains/a/bin/x.py")
    path.write_text("x = 1\n" + CLOCK)
    hunks = _scan(git_umbrella)
    assert [(h.kind, h.path) for h in hunks] == [("timing", "bin/x.py")]


def test_a_path_outside_the_member_is_scaffolding(git_umbrella):
    toml = git_umbrella / "domains/a/domain.toml"
    toml.write_text(toml.read_text().replace(
        "state/iterations)/", "state/iterations|tools)/"))
    _git(git_umbrella, "commit", "-qam", "member lanes cover tools/")
    path = _commit(git_umbrella, "tools/y.py")
    path.write_text("x = 1\n" + CLOCK)
    hunks = _scan(git_umbrella)
    assert [(h.kind, h.path) for h in hunks] == [("timing", "../../tools/y.py")]


def test_the_evaluator_matches_a_member_relative_path(git_umbrella):
    path = git_umbrella / "domains/a/bin/measure"
    path.write_text(path.read_text() + "echo changed\n")
    assert "evaluator" in {h.kind for h in _scan(git_umbrella)
                           if h.path == "bin/measure"}


def test_a_domain_at_the_repo_top_is_unchanged(tmp_path):
    from conftest import member_domain
    root = member_domain(tmp_path / "solo")
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "T")
    _git(root, "config", "user.email", "t@e.x")
    _git(root, "config", "commit.gpgsign", "false")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "c")
    path = root / "bin/x.py"
    path.write_text(CLOCK)
    config = DomainConfig.load(root)
    assert [(h.kind, h.path) for h in tamper.scan_tree(config, root)] == \
        [("timing", "bin/x.py")]
