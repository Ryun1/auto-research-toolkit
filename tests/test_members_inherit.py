"""A member inherits the umbrella's shared tables (#64).

The merge happens in `DomainConfig.load`, before any table is parsed, so
every check downstream sees the effective config. Tables merge key by key
with the member winning, except where a member winning would weaken a
safety rule: `human_only` and `never_push_remotes` accumulate,
`spend_ceiling` takes the smaller value and `min_core` the higher floor."""
import pytest

from autoresearch import cli, inherit, members
from autoresearch.config import DomainConfig
from autoresearch.errors import ConfigError
from conftest import MEMBERS, member_domain


def _workspace(tmp_path, member_extra="", member_drop=(), umbrella_extra="",
               umbrella_drop=()):
    """An umbrella (Q, H) over one member `domains/a` (A, B): the member's
    root, with `member_drop` tables removed so it inherits them."""
    root = member_domain(tmp_path / "umb", name="umb",
                         extra=MEMBERS + umbrella_extra,
                         drop_tables=umbrella_drop)
    return member_domain(root / "domains" / "a", ("A", "B"), name="a",
                         extra=member_extra, drop_tables=member_drop)


def test_inherited_tables_are_the_nine_shared_ones():
    assert set(inherit.INHERITED) == {
        "policy", "budgets", "upstream", "coordinator", "skills", "hardware",
        "remote", "brain", "session"}


def test_a_member_that_sets_one_budget_keeps_the_rest(tmp_path):
    member = _workspace(tmp_path, member_drop=("budgets",),
                        member_extra="[budgets]\niteration_fanout = 9\n")
    config = DomainConfig.load(member)
    umbrella = DomainConfig.load(member.parent.parent)
    assert config.budgets == {**umbrella.budgets, "iteration_fanout": 9.0}
    assert config.sources["budgets.iteration_fanout"] == "member"
    assert config.sources["budgets.domain_max_runs"] == "umbrella"
    assert config.umbrella_root == umbrella.paths.root


def test_a_member_cannot_drop_an_umbrella_human_only_rule(tmp_path, capsys):
    member = _workspace(tmp_path, member_drop=("policy",), member_extra=(
        '[[policy.human_only]]\npattern = "member publish"\n'
        'reason = "the member\'s own outward-facing action"\n'))
    config = DomainConfig.load(member)
    assert [r.pattern for r in config.policy.human_only] == [
        "toy publish", "member publish"]
    assert config.sources["policy.human_only[toy publish]"] == "umbrella"
    assert config.sources["policy.human_only[member publish]"] == "member"
    assert cli.main(["--domain", str(member), "policy"]) == 0
    assert "toy publish" in capsys.readouterr().out


def test_a_member_repeating_an_umbrella_rule_does_not_duplicate_it(tmp_path):
    member = _workspace(tmp_path)       # declares the toy's own policy again
    config = DomainConfig.load(member)
    assert [r.pattern for r in config.policy.human_only] == ["toy publish"]
    assert config.sources["policy.human_only[toy publish]"] == "umbrella"


def test_never_push_remotes_accumulate(tmp_path):
    rule = '[[policy.never_push_remotes]]\npattern = "{}"\nreason = "not ours"\n'
    member = _workspace(tmp_path, umbrella_extra=rule.format("origin"),
                        member_extra=rule.format("upstream"))
    config = DomainConfig.load(member)
    assert [r.pattern for r in config.policy.never_push_remotes] == [
        "origin", "upstream"]


def test_forbidden_paths_are_the_members_when_it_sets_them(tmp_path):
    """Their patterns are relative to each domain's root, so they merge
    member-wins like any other key rather than accumulating."""
    member = _workspace(tmp_path, member_drop=("policy",), member_extra=(
        '[[policy.forbidden_paths]]\npattern = "keys/*"\nreason = "member"\n'))
    config = DomainConfig.load(member)
    assert [r.pattern for r in config.policy.forbidden_paths] == ["keys/*"]
    assert config.sources["policy.forbidden_paths"] == "member"


@pytest.mark.parametrize("member_ceiling, effective, source",
                         [(2.0, 2.0, "member"), (9.0, 5.0, "umbrella")])
def test_spend_ceiling_takes_the_smaller(tmp_path, member_ceiling, effective,
                                         source):
    member = _workspace(tmp_path, member_drop=("policy",),
                        member_extra=f"[policy]\nspend_ceiling = {member_ceiling}\n")
    config = DomainConfig.load(member)
    assert config.policy.spend_ceiling == effective
    assert config.sources["policy.spend_ceiling"] == source


@pytest.mark.parametrize("umbrella_floor, member_floor",
                         [("0.9.0", "0.8.0"), ("0.8.0", "0.9.0")])
def test_min_core_takes_the_higher_floor(tmp_path, umbrella_floor, member_floor):
    member = _workspace(
        tmp_path, umbrella_drop=("upstream",), member_drop=("upstream",),
        umbrella_extra=f'[upstream]\nmin_core = "{umbrella_floor}"\n',
        member_extra=f'[upstream]\nmin_core = "{member_floor}"\n')
    assert DomainConfig.load(member).upstream.min_core == "0.9.0"


def test_an_unreadable_min_core_names_both_values():
    with pytest.raises(ConfigError, match=r"0\.9\.0.*0\.8\.dev1"):
        inherit.merge({"upstream": {"min_core": "0.9.0"}},
                      {"upstream": {"min_core": "0.8.dev1"}})


def test_identity_tables_are_never_inherited(tmp_path):
    """A member with no [commands] gets none, not the umbrella's, and its own
    validate says so."""
    member = _workspace(tmp_path, member_drop=("commands",))
    config = DomainConfig.load(member)
    assert config.commands == {}
    assert "commands.measure is required and missing" in config.check()
    assert not any(key.startswith("commands") for key in config.sources)


def test_inherited_skills_dir_resolves_in_the_member(tmp_path):
    member = _workspace(tmp_path, member_drop=("skills",))
    config = DomainConfig.load(member)
    assert config.skills.dir == "docs/skills"
    assert config.sources["skills.dir"] == "umbrella"
    assert (config.paths.root / config.skills.dir).is_relative_to(member)


def test_hardware_and_remote_merge_by_class_and_list(tmp_path):
    member = _workspace(tmp_path, member_drop=("hardware", "remote"), member_extra=(
        '[hardware.cheap-sweep]\nmin_threads = 8\n'
        '[[remote]]\nname = "member-box"\nusd_per_hour = 1.0\n'))
    config = DomainConfig.load(member)
    assert config.hardware["cheap-sweep"].min_threads == 8
    assert "wide-sweep" in config.hardware       # the umbrella's other class
    assert [r.name for r in config.remote] == ["member-box"]
    assert config.sources["hardware.cheap-sweep"] == "member"
    assert config.sources["hardware.wide-sweep"] == "umbrella"
    assert config.sources["remote"] == "member"


def test_a_plain_domain_has_no_sources(sandbox):
    assert sandbox.sources == {}
    assert sandbox.umbrella_root is None


def test_the_umbrella_itself_inherits_nothing(umbrella):
    config = DomainConfig.load(umbrella)
    assert config.sources == {} and config.umbrella_root is None


def test_a_malformed_umbrella_does_not_break_a_member_load(tmp_path):
    member = _workspace(tmp_path, member_drop=("budgets",))
    umbrella_toml = member.parent.parent / "domain.toml"
    umbrella_toml.write_text(umbrella_toml.read_text() + "x = [\n")
    assert members.umbrella_raw(member) is None
    config = DomainConfig.load(member)
    assert config.budgets == {} and config.sources == {}
    assert config.umbrella_root is None


def test_a_broken_umbrella_that_parses_is_inherited_and_named(tmp_path):
    """The merge reads the umbrella's raw TOML and never loads it, so an
    umbrella whose goal is missing still lends its tables -- and #63's check
    names its problem from the member."""
    member = _workspace(tmp_path, member_drop=("budgets",))
    umbrella = member.parent.parent
    (umbrella / "goal.yaml").unlink()
    config = DomainConfig.load(member)
    assert config.sources["budgets.iteration_fanout"] == "umbrella"
    assert [p for p in config.check()
            if p.startswith(f"domains: umbrella at {umbrella}: domain.goal")]


# -- a member cannot weaken what it inherits (#64 review) ---------------------

@pytest.mark.parametrize("member_policy", [
    {"human_only": False}, {"human_only": {}}, {"human_only": ""},
    {"never_push_remotes": False}])
def test_a_member_cannot_blank_an_accumulated_rule_list(member_policy):
    key = next(iter(member_policy))
    umbrella = {"policy": {key: [{"pattern": "toy publish", "reason": "r"}]}}
    with pytest.raises(ConfigError, match=rf"policy\.{key}.*cannot be removed"):
        inherit.merge(umbrella, {"policy": member_policy})


@pytest.mark.parametrize("member_policy", [False, "", []])
def test_a_member_cannot_blank_the_umbrella_policy(member_policy):
    umbrella = {"policy": {"spend_ceiling": 5.0}}
    with pytest.raises(ConfigError, match=r"\[policy\].*cannot be removed"):
        inherit.merge(umbrella, {"policy": member_policy})


def test_a_member_blanking_human_only_in_toml_is_refused_at_load(tmp_path):
    member = _workspace(tmp_path, member_drop=("policy",),
                        member_extra="[policy]\nhuman_only = false\n")
    with pytest.raises(ConfigError, match="cannot be removed"):
        DomainConfig.load(member)


@pytest.mark.parametrize("ceiling", ["100", "inf", True, float("nan")])
def test_a_member_ceiling_that_is_not_a_number_is_refused(ceiling):
    with pytest.raises(ConfigError, match="spend_ceiling"):
        inherit.merge({"policy": {"spend_ceiling": 5.0}},
                      {"policy": {"spend_ceiling": ceiling}})


def test_a_nan_umbrella_ceiling_is_refused():
    with pytest.raises(ConfigError, match="spend_ceiling"):
        inherit.merge({"policy": {"spend_ceiling": float("nan")}},
                      {"policy": {"spend_ceiling": 2.0}})


def test_an_infinite_member_ceiling_still_takes_the_smaller():
    data, sources = inherit.merge({"policy": {"spend_ceiling": 5.0}},
                                  {"policy": {"spend_ceiling": float("inf")}})
    assert data["policy"]["spend_ceiling"] == 5.0
    assert sources["policy.spend_ceiling"] == "umbrella"


def test_an_empty_member_min_core_keeps_the_umbrella_floor():
    data, sources = inherit.merge({"upstream": {"min_core": "0.9.0"}},
                                  {"upstream": {"min_core": ""}})
    assert data["upstream"]["min_core"] == "0.9.0"
    assert sources["upstream.min_core"] == "umbrella"


def test_merge_shares_nothing_with_its_inputs():
    umbrella = {"budgets": {"x": 1}, "hardware": {"c": {"min_threads": 2}}}
    data, _ = inherit.merge(umbrella, {})
    data["budgets"]["x"] = 2
    data["hardware"]["c"]["min_threads"] = 9
    assert umbrella == {"budgets": {"x": 1}, "hardware": {"c": {"min_threads": 2}}}


def test_an_unparseable_umbrella_is_a_problem_in_the_member(tmp_path):
    """Load still succeeds -- an unrelated outer domain.toml must not block a
    domain -- but validate must say the shared safety rules are not applied."""
    member = _workspace(tmp_path)
    umbrella_toml = member.parent.parent / "domain.toml"
    umbrella_toml.write_text(umbrella_toml.read_text() + "x = [\n")
    problems = DomainConfig.load(member).check()
    assert [p for p in problems
            if p.startswith(f"domains: {umbrella_toml} is not valid TOML")
            and "NOT applied" in p]
