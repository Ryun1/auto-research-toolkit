"""`ar init` -- what a new domain arrives with."""
import pytest

from autoresearch.config import DomainConfig
from autoresearch.errors import ConfigError
from autoresearch.lanes import SCAFFOLDING
from autoresearch.scaffold import init


@pytest.fixture
def scaffolded(tmp_path):
    init(tmp_path / "d", name="d")
    return DomainConfig.load(tmp_path / "d")


def test_a_scaffolded_domain_loads_and_validates(scaffolded):
    assert list(scaffolded.check()) == []


def test_it_ignores_the_state_that_must_not_cross_machines(scaffolded):
    """Stopping on one machine and resuming on another is the supported
    workflow; the claim lock and the workspace pool are the two things that
    must never travel, because both name a filesystem that is not there."""
    ignored = (scaffolded.paths.root / ".gitignore").read_text().split()
    assert "state/claims/" in ignored
    assert ".ar/" in ignored


def test_its_lane_boundary_keeps_the_lock_out_of_the_findings_lane(scaffolded):
    assert scaffolded.lanes.classify("state/claims/lock") == SCAFFOLDING


def test_it_refuses_to_scaffold_over_a_live_domain(tmp_path):
    init(tmp_path / "d", name="d")
    with pytest.raises(ConfigError, match="refuses to scaffold over"):
        init(tmp_path / "d", name="d")


def test_it_does_not_destroy_a_gitignore_that_is_already_there(tmp_path):
    """`ar init` refuses to scaffold over a live domain because it would destroy
    the records. `.gitignore` is the one scaffolded file that routinely exists
    already in a directory being adopted -- and what it protects is exactly the
    class of path `[policy] forbidden_paths` exists for."""
    root = tmp_path / "d"
    root.mkdir()
    (root / ".gitignore").write_text(".venv/\nsecrets/\n")

    init(root, name="d")
    kept = (root / ".gitignore").read_text()
    assert ".venv/" in kept and "secrets/" in kept, "pre-existing rules lost"
    assert "state/claims/" in kept and ".ar/" in kept, "ours were not added"


def test_scaffolding_twice_does_not_duplicate_the_gitignore_block(tmp_path):
    init(tmp_path / "d", name="d")
    init(tmp_path / "d", name="d", force=True)
    lines = (tmp_path / "d" / ".gitignore").read_text().splitlines()
    assert lines.count("state/claims/") == 1
    assert lines.count(".ar/") == 1


def test_a_scaffolded_domain_has_a_skills_directory_that_starts_empty(tmp_path):
    """The scaffold's first act must not be to fail its own validator, and a
    skill cites entries -- of which a new domain has none. So it ships the
    format, not an example."""
    from autoresearch import skills as skills_mod
    init(tmp_path / "d", name="d")
    config = DomainConfig.load(tmp_path / "d")
    assert (config.paths.root / config.skills.dir).is_dir()
    assert (config.paths.root / config.skills.dir / "README.md").exists()
    assert skills_mod.read_all(config) == ([], [])
    assert list(config.check()) == []


def test_a_scaffolded_domain_declares_a_distil_cadence(tmp_path):
    init(tmp_path / "d", name="d")
    config = DomainConfig.load(tmp_path / "d")
    assert config.distil_every == 5
