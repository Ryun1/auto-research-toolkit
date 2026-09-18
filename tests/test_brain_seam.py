"""The agent-agnostic seam: a brain is `claude` or any command.

The loop must never learn a backend exists, and the config must refuse a
broken brain table before anything spends -- a misspelled placeholder is a
scout that runs without its prompt.
"""
import sys

import pytest

from autoresearch.config import _brain_spec
from autoresearch.errors import AutoresearchError, ConfigError

#: A stand-in agent: reads its brief on stdin, echoes it back as fenced JSON,
#: and meters itself by writing the cost file. Every contract clause in one
#: process.
ECHO_AGENT = (
    "import json, sys, pathlib\n"
    "brief = sys.stdin.read()\n"
    "args = sys.argv\n"
    "cost = pathlib.Path(args[args.index('--cost-file') + 1])\n"
    "cost.write_text('0.25')\n"
    "prompt = pathlib.Path(args[args.index('--prompt-file') + 1])\n"
    "role = args[args.index('--role') + 1]\n"
    "print('```json')\n"
    "print(json.dumps({'echo': json.loads(brief),\n"
    "                  'role': role,\n"
    "                  'prompt_chars': len(prompt.read_text()),\n"
    "                  'workspace': args[args.index('--workspace') + 1]}))\n"
    "print('```')\n"
)


# The flags every command in these tests passes through, carrying the four
# placeholders the ProcessBrain contract substitutes.
FLAGS = ["--role", "{role}", "--prompt-file", "{prompt_file}",
         "--workspace", "{workspace}", "--cost-file", "{cost_file}"]


def echo_command():
    return [sys.executable, "-c", ECHO_AGENT, *FLAGS]


# -- the process contract ----------------------------------------------------

def test_a_command_is_a_working_brain(sandbox):
    from autoresearch.driver.brain import ProcessBrain
    brain = ProcessBrain(echo_command(), root=sandbox.paths.root)
    reply = brain.ask("curator", '{"x": 1}')
    assert reply.data["echo"] == {"x": 1}
    assert reply.data["role"] == "curator"
    assert reply.data["workspace"] == str(sandbox.paths.root)
    assert reply.cost_usd == 0.25
    assert reply.backend == sys.executable.split("/")[-1]


def test_the_prompt_file_carries_the_role_prompt(sandbox):
    from autoresearch.driver.brain import ProcessBrain, role_prompt
    brain = ProcessBrain(echo_command(), root=sandbox.paths.root)
    reply = brain.ask("scout", "[]")
    assert reply.data["prompt_chars"] == len(role_prompt("scout"))


def test_workspace_overrides_the_root(sandbox, tmp_path):
    from autoresearch.driver.brain import ProcessBrain
    brain = ProcessBrain(echo_command(), root=sandbox.paths.root)
    reply = brain.ask("worker", "[]", workspace=tmp_path)
    assert reply.data["workspace"] == str(tmp_path)


def test_a_failing_command_is_an_attributed_error_not_a_silent_reply(sandbox):
    from autoresearch.driver.brain import ProcessBrain
    command = [sys.executable, "-c", "import sys; sys.stderr.write('boom\\n'); sys.exit(3)"]
    brain = ProcessBrain(command, root=sandbox.paths.root)
    with pytest.raises(AutoresearchError, match="exited 3.*boom"):
        brain.ask("scout", "[]")


def test_an_unparsable_reply_is_refused_not_guessed(sandbox):
    from autoresearch.driver.brain import ProcessBrain
    brain = ProcessBrain([sys.executable, "-c", "print('no json here')"],
                         root=sandbox.paths.root)
    with pytest.raises(AutoresearchError, match="no JSON found"):
        brain.ask("scout", "[]")


def test_a_cost_file_that_is_not_a_number_is_refused(sandbox):
    from autoresearch.driver.brain import ProcessBrain
    command = [sys.executable, "-c",
               "import sys, pathlib\n"
               "pathlib.Path(sys.argv[sys.argv.index('--cost-file') + 1])"
               ".write_text('lots')\n"
               "print('[]')\n", *FLAGS]
    brain = ProcessBrain(command, root=sandbox.paths.root)
    with pytest.raises(AutoresearchError):
        brain.ask("scout", "[]")


def test_an_absent_cost_file_blocks_further_spend_under_a_ceiling(sandbox):
    from autoresearch.driver.brain import CostLedger, ProcessBrain
    ledger = CostLedger(1.0)
    brain = ProcessBrain([sys.executable, "-c", "print('[]')"],
                         root=sandbox.paths.root, ledger=ledger)
    assert brain.ask("scout", "[]").cost_usd is None
    with pytest.raises(AutoresearchError):
        brain.ask("scout", "[]")


# -- routing -----------------------------------------------------------------

def test_roles_route_to_the_backend_the_domain_named(sandbox):
    from autoresearch.driver.brain import ProcessBrain, RoutingBrain, ScriptedBrain
    curator = ProcessBrain(echo_command(), root=sandbox.paths.root)
    default = ScriptedBrain({})
    brain = RoutingBrain({"curator": curator}, default)
    assert brain.ask("curator", "[]").backend == curator.name
    assert brain.ask("judge", "[]").backend == "scripted"


def test_build_brain_routes_per_role_and_shares_one_ceiling(sandbox):
    from autoresearch.driver.brain import ProcessBrain, SDKBrain, build_brain
    sandbox.brain = {"default": "claude", "curator": echo_command()}
    brain = build_brain(sandbox, max_budget_usd=5.0, allow_paid=True)
    assert isinstance(brain.routes["curator"], ProcessBrain)
    assert isinstance(brain.default, SDKBrain)
    # Both backends read the same ledger, so a ceiling is campaign-wide
    # rather than a full-size copy in every backend.
    assert brain.routes["curator"].ledger is brain.default.ledger


def test_spend_through_one_backend_shrinks_the_other_backend_s_ceiling(sandbox):
    from autoresearch.driver.brain import build_brain
    sandbox.brain = {"default": "claude", "curator": echo_command()}
    brain = build_brain(sandbox, max_budget_usd=1.0, allow_paid=True)
    reply = brain.ask("curator", "[]")
    assert reply.cost_usd == 0.25
    assert brain.default.ledger.remaining() == pytest.approx(0.75)


def test_overrides_only_table_still_gets_a_default(sandbox):
    from autoresearch.driver.brain import ProcessBrain, RoutingBrain, build_brain
    sandbox.brain = {"curator": echo_command()}
    brain = build_brain(sandbox, allow_paid=True)
    assert isinstance(brain, RoutingBrain)
    assert isinstance(brain.routes["curator"], ProcessBrain)
    assert brain.routes.get("judge") is None, "judge falls to the default brain"


def test_sdkbrain_without_a_ledger_still_caps_itself_against_its_own_spend(sandbox):
    """The SDK brain is metered twice on purpose; a ledger-less construction
    must keep the second meter -- the ceiling it hands the SDK shrinks by
    everything it has already spent."""
    from autoresearch.driver.brain import SDKBrain
    brain = SDKBrain(sandbox, max_budget_usd=1.0)
    brain.spent_usd = 0.6
    assert brain._ceiling() == pytest.approx(0.4)
    brain.spent_usd = 1.4          # past the ceiling: hand the SDK zero, not debt
    assert brain._ceiling() == 0.0
    no_cap = SDKBrain(sandbox)     # None becomes the policy ceiling
    assert no_cap.max_budget_usd == sandbox.policy.spend_ceiling
    assert no_cap._ceiling() == sandbox.policy.spend_ceiling


def test_sdkbrain_with_a_ledger_hands_the_sdk_what_the_ledger_has_left(sandbox):
    from autoresearch.driver.brain import CostLedger, SDKBrain
    ledger = CostLedger(2.0)
    ledger.spend(1.5)
    brain = SDKBrain(sandbox, max_budget_usd=99.0, ledger=ledger)
    assert brain._ceiling() == pytest.approx(0.5), \
        "the ledger wins; the brain's own ceiling is not doubled on top of it"


# -- config validation: refuse a broken table before anything spends ---------

def test_brain_table_accepts_claude_and_commands():
    assert _brain_spec({"default": "claude"}) == {"default": "claude"}
    spec = _brain_spec({"default": ["bin", "agent"], "curator": ["pi", "-p"]})
    assert spec["curator"] == ["pi", "-p"]


def test_brain_table_rejects_an_unknown_role():
    with pytest.raises(ConfigError, match="not a role"):
        _brain_spec({"curater": ["pi"]})


def test_brain_table_rejects_a_malformed_command():
    with pytest.raises(ConfigError, match="non-empty list of strings"):
        _brain_spec({"curator": "pi -p"})
    with pytest.raises(ConfigError, match="non-empty list of strings"):
        _brain_spec({"curator": []})


def test_brain_table_rejects_an_unknown_placeholder():
    with pytest.raises(ConfigError, match="unknown placeholder.*promt_file"):
        _brain_spec({"curator": ["agent", "--prompt", "{promt_file}"]})


# -- the SDK brain is fail-closed: API money is authorized, not defaulted ----

def test_an_absent_brain_table_refuses_the_sdk_brain(sandbox):
    """The incumbent default was one SDK brain for every role -- which meant
    `ar loop` could start spending API money without anyone saying so. Now the
    absence is a refusal that names both remedies."""
    from autoresearch.driver.brain import build_brain
    with pytest.raises(AutoresearchError, match="authorize_spend"):
        build_brain(sandbox)
    with pytest.raises(AutoresearchError, match="allow-paid-brain"):
        build_brain(sandbox)


def test_authorization_is_explicit_from_config_or_flag(sandbox):
    from autoresearch.driver.brain import SDKBrain, build_brain
    sandbox.brain = {"authorize_spend": True}
    assert isinstance(build_brain(sandbox).default, SDKBrain)
    sandbox.brain = {}
    assert isinstance(build_brain(sandbox, allow_paid=True).default, SDKBrain)


def test_the_flag_grants_and_never_revokes_domain_authorization(sandbox):
    """argparse's store_true default is False, not None: a plain `ar loop`
    must not undo the domain's own `authorize_spend = true`."""
    from autoresearch.driver.brain import SDKBrain, build_brain
    sandbox.brain = {"authorize_spend": True}
    assert isinstance(build_brain(sandbox, allow_paid=False).default, SDKBrain)


def test_authorize_spend_must_be_a_boolean():
    from autoresearch.config import ConfigError, _brain_spec
    with pytest.raises(ConfigError, match="authorize_spend"):
        _brain_spec({"authorize_spend": "yes"})
    assert _brain_spec({"authorize_spend": True}) == {"authorize_spend": True}


def test_a_process_brain_domain_needs_no_authorization(sandbox):
    """A domain that routes every role to commands never approaches the API
    meter, so the fail-closed gate must not be in its way."""
    from autoresearch.driver.brain import ProcessBrain, RoutingBrain, build_brain
    sandbox.brain = {"default": echo_command()}
    brain = build_brain(sandbox)
    assert isinstance(brain, RoutingBrain)
    assert isinstance(brain.default, ProcessBrain)


def test_one_sdk_role_is_enough_to_require_authorization(sandbox):
    from autoresearch.driver.brain import build_brain
    sandbox.brain = {"default": echo_command(), "judge": "claude"}
    with pytest.raises(AutoresearchError, match="judge"):
        build_brain(sandbox)
    assert build_brain(sandbox, allow_paid=True).routes["judge"].backend == "claude-sdk"
