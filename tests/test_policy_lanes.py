import pytest

from autoresearch.errors import ConfigError, PolicyError
from autoresearch.lanes import FINDINGS, SCAFFOLDING, Lanes
from autoresearch.policy import Policy

SPEC = {
    "forbidden_paths": [{"pattern": "clone/*", "reason": "origin is a public fork"}],
    "never_push_remotes": [{"pattern": "*public-fork*", "reason": "never leaves the machine"}],
    "human_only": [{"pattern": "thing submit", "reason": "irreversible, person-only"}],
    "spend_ceiling": 20,
}


def test_every_declared_rule_refuses_something():
    """H89 directly: nothing asserted the hooks refused anything, so deleting
    the enforcement left the gate green."""
    assert Policy.from_dict(SPEC).selftest() == []


def test_selftest_catches_enforcement_that_was_deleted():
    """H89 exactly: a commit deleted rule 1's enforcement and the whole gate
    stayed green, because nothing asserted the guard refused anything. Here the
    enforcement is removed and the selftest must notice."""
    p = Policy.from_dict(SPEC)
    p.check_command = lambda command: None          # enforcement deleted
    p.check_paths = lambda paths: None
    failures = p.selftest()
    assert any("human_only rule refuses nothing" in f for f in failures)
    assert any("forbidden_paths rule refuses nothing" in f for f in failures)


def test_selftest_catches_deleted_spend_enforcement():
    """The selftest proved three rule kinds but never the spend ceiling, so
    deleting check_spend left `ar policy` green (the H89 class again)."""
    class Deleted(Policy):
        def check_spend(self, amount, note=""):
            return None

    failures = Deleted(spend_ceiling=5.0).selftest()
    assert any("spend_ceiling" in f for f in failures)


def test_regex_forbidden_path_rule_refuses_real_paths():
    """A regex-shaped forbidden_paths rule was inert in enforcement yet passed
    the selftest, because probes() offers the pattern itself and fnmatch
    matched it against itself. check_paths now honors regexes like
    check_command does (H24)."""
    p = Policy.from_dict({"forbidden_paths": [
        {"pattern": r"secrets/.*\.env", "reason": "keys never leave"}]})
    with pytest.raises(PolicyError, match="forbidden_paths"):
        p.check_paths(["secrets/prod.env"])
    assert p.selftest() == []


def test_empty_pattern_is_refused_because_it_matches_everything():
    """An empty pattern reads as "disabled" and behaves as "refuse everything":
    re.search("", x) always hits. It fails in the direction nobody tests for."""
    with pytest.raises(ConfigError, match="matches every input"):
        Policy.from_dict({"human_only": [{"pattern": "", "reason": "oops"}]})


@pytest.mark.parametrize("command", [
    "thing submit",
    "thing  submit",            # normalised token stream
    "thing submit --yes",
    ["thing", "submit"],
])
def test_human_only_refuses_the_command_and_its_variants(command):
    with pytest.raises(PolicyError, match="human-only"):
        Policy.from_dict(SPEC).check_command(command)


def test_human_only_allows_unrelated_commands():
    Policy.from_dict(SPEC).check_command("thing status")


def test_forbidden_path_refused():
    with pytest.raises(PolicyError, match="forbidden_paths"):
        Policy.from_dict(SPEC).check_paths(["clone/results.tsv"])


def test_push_to_never_push_remote_refused():
    with pytest.raises(PolicyError, match="refused"):
        Policy.from_dict(SPEC).check_push("origin", "git@github.com:x/public-fork.git")


def test_spend_ceiling():
    p = Policy.from_dict(SPEC)
    p.check_spend(19.99)
    with pytest.raises(PolicyError, match="ceiling"):
        p.check_spend(20.01)


def test_a_rule_without_a_reason_is_refused():
    """A never-rule with no stated reason is one the next agent deletes."""
    with pytest.raises(ConfigError, match="no reason"):
        Policy.from_dict({"forbidden_paths": ["clone/*"]})


def test_lanes_default_deny():
    lanes = Lanes("^(data/runs|inbox|docs|state)/")
    assert lanes.classify("inbox/memo.md") == FINDINGS
    assert lanes.classify("bin/close") == SCAFFOLDING
    assert lanes.classify("newtoplevel/x") == SCAFFOLDING     # default-deny


def test_mixed_branch_is_detected_and_explained():
    lanes = Lanes("^(inbox)/")
    assert lanes.is_mixed(["inbox/a.md", "bin/b"])
    assert "MIXED" in lanes.explain(["inbox/a.md", "bin/b"])
    assert not lanes.is_mixed(["inbox/a.md", "inbox/b.md"])


def test_bad_regex_is_refused_at_load():
    with pytest.raises(ConfigError, match="not a valid regex"):
        Lanes("^(unclosed")


def test_the_shipped_domain_does_not_publish_per_machine_state(toy):
    """The claim lock is a single-filesystem primitive: its holder and pid mean
    nothing on another machine, and committing it hands the next machine a lock
    it cannot diagnose. A `state/` prefix in the findings lane published it."""
    assert toy.lanes.classify("state/entries/Q1.yaml") == FINDINGS
    assert toy.lanes.classify("state/iterations/0001.json") == FINDINGS
    assert toy.lanes.classify("state/claims/lock") == SCAFFOLDING
