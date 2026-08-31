import pytest

from autoresearch.config import CONFIG_NAME, DomainConfig, discover
from autoresearch.errors import ConfigError


def test_toy_domain_loads_and_validates(toy):
    assert toy.name == "toy"
    assert toy.check() == []


def test_tracks_have_their_own_terminal_sets(toy):
    assert toy.tracks["research"].machine.terminal_names == {"confirmed", "refuted"}
    assert toy.tracks["harness"].machine.terminal_names == {"fixed", "wontfix", "refuted"}


def test_track_for_id(toy):
    assert toy.track_for("Q12").id == "research"
    assert toy.track_for("H7").id == "harness"
    with pytest.raises(ConfigError, match="no track owns"):
        toy.track_for("Z1")


def test_unknown_top_level_table_is_refused(tmp_path):
    """H135 applied to config: an unrecognised key must not read as a
    successful run of what you meant."""
    (tmp_path / CONFIG_NAME).write_text(
        '[domain]\nname="x"\n[lanes]\nfindings="^a/"\n[[tracks]]\nid="r"\nprefix="Q"\n'
        '[surprise]\nk=1\n')
    with pytest.raises(ConfigError, match="unknown top-level"):
        DomainConfig.load(tmp_path)


def test_missing_lanes_findings_is_refused(tmp_path):
    (tmp_path / "goal.yaml").write_text(
        "goal:\n  id: g\n  objective: T\n  metrics: {T: {}}\n  target: {value: 1}\n")
    (tmp_path / CONFIG_NAME).write_text(
        '[domain]\nname="x"\n[[tracks]]\nid="r"\nprefix="Q"\n')
    with pytest.raises(ConfigError, match="lanes.findings is required"):
        DomainConfig.load(tmp_path)


def test_missing_config_says_how_to_fix_it(tmp_path):
    with pytest.raises(ConfigError, match="ar init"):
        DomainConfig.load(tmp_path)


def test_discover_walks_up(toy, tmp_path):
    deep = toy.paths.root / "guides"
    assert discover(deep) == toy.paths.root
    with pytest.raises(ConfigError, match="or any parent"):
        discover(tmp_path)


def test_check_reports_a_missing_command(toy, monkeypatch):
    toy.commands["measure"] = "bin/does-not-exist"
    assert any("does not exist" in p for p in toy.check())


def test_check_reports_duplicate_track_prefixes(toy):
    toy.tracks["harness"].prefix = "Q"
    assert any("share an id prefix" in p for p in toy.check())


def test_ar_budget_survives_a_zero_target(sandbox, capsys):
    """`is_met` refuses to normalise against a zero target, so the goal-met line
    has to test the target for truth rather than for None -- driving a metric to
    zero is a legitimate goal and it must not end in a traceback."""
    from autoresearch import cli
    from autoresearch.runs import RunRecord, append

    goal = sandbox.paths.root / "goal.yaml"
    goal.write_text(goal.read_text().replace(
        "  target:\n    source: bin/probe-target\n    moving: true\n"
        "    refresh_seconds: 300",
        "  target:\n    value: 0.0").replace(
        '  stop_when: "objective < target"', ""))
    append(sandbox.paths.runs / "seed.jsonl",
           RunRecord(metrics={"ops": 10.0, "peak": 1.0}, session="seed",
                     provenance={"host": "test"}))

    assert cli.main(["--domain", str(sandbox.paths.root), "budget"]) == 0
    out = capsys.readouterr().out
    assert "stop decision" in out and "best objective" not in out


# -- the explore reserve, as a domain decision ---------------------------


def _minimal(tmp_path, coordinator=""):
    (tmp_path / "goal.yaml").write_text(
        "goal:\n  id: g\n  objective: T\n  metrics: {T: {}}\n  target: {value: 1}\n")
    (tmp_path / CONFIG_NAME).write_text(
        '[domain]\nname="x"\n[lanes]\nfindings="^a/"\n'
        '[[tracks]]\nid="r"\nprefix="Q"\n' + coordinator)
    return tmp_path


def test_a_domain_may_set_its_own_explore_fraction(tmp_path):
    """How much of a shortlist to spend on amplitude is a risk appetite, and
    risk appetite belongs to the domain, not to the core."""
    config = DomainConfig.load(_minimal(
        tmp_path, "[coordinator]\nexplore_fraction = 0.5\n"))
    assert config.explore_fraction == 0.5


def test_a_domain_that_says_nothing_gets_the_core_default(tmp_path):
    from autoresearch.rank import EXPLORE_FRACTION
    config = DomainConfig.load(_minimal(tmp_path))
    assert config.explore_fraction == EXPLORE_FRACTION


def test_a_domain_may_disable_the_reserve_outright(tmp_path):
    """Zero is a real answer, and must not read as 'unset' and be defaulted."""
    config = DomainConfig.load(_minimal(
        tmp_path, "[coordinator]\nexplore_fraction = 0\n"))
    assert config.explore_fraction == 0.0


def test_an_out_of_range_explore_fraction_is_refused_at_load(tmp_path):
    with pytest.raises(ConfigError, match="coordinator.explore_fraction"):
        DomainConfig.load(_minimal(
            tmp_path, "[coordinator]\nexplore_fraction = 1.0\n"))


def test_a_non_numeric_explore_fraction_is_refused_at_load(tmp_path):
    """It reaches a float division either way; failing here costs no iteration."""
    with pytest.raises(ConfigError, match="coordinator.explore_fraction"):
        DomainConfig.load(_minimal(
            tmp_path, '[coordinator]\nexplore_fraction = "a fifth"\n'))
