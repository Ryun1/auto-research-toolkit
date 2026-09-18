"""The agent-agnostic seam: a brain is `"typesafe"` or any command.

The loop must never learn a backend exists, and the config must refuse a
broken brain table before anything spends -- a misspelled placeholder is a
scout that runs without its prompt.
"""
import json
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


def test_build_brain_routes_per_role_and_shares_one_ceiling(sandbox, monkeypatch):
    from autoresearch.driver.brain import ProcessBrain, TypeSafeBrain, build_brain
    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    sandbox.brain = {"default": "typesafe", "curator": echo_command()}
    brain = build_brain(sandbox, max_budget_usd=5.0, allow_paid=True)
    assert isinstance(brain.routes["curator"], ProcessBrain)
    assert isinstance(brain.default, TypeSafeBrain)
    # Both backends read the same ledger, so a ceiling is campaign-wide
    # rather than a full-size copy in every backend.
    assert brain.routes["curator"].ledger is brain.default.ledger


def test_spend_through_one_backend_shrinks_the_other_backend_s_ceiling(sandbox, monkeypatch):
    from autoresearch.driver.brain import build_brain
    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    sandbox.brain = {"default": "typesafe", "curator": echo_command()}
    brain = build_brain(sandbox, max_budget_usd=1.0, allow_paid=True)
    reply = brain.ask("curator", "[]")
    assert reply.cost_usd == 0.25
    assert brain.default.ledger.remaining() == pytest.approx(0.75)


def test_an_absent_default_refuses_even_with_role_overrides(sandbox):
    """Roles that fall through to the default need somewhere to go: a table
    of overrides with no `default` is a refusal that names the remedy."""
    from autoresearch.driver.brain import build_brain
    sandbox.brain = {"curator": echo_command()}
    with pytest.raises(AutoresearchError, match="default"):
        build_brain(sandbox)


# -- config validation: refuse a broken table before anything spends ---------

def test_brain_table_accepts_typesafe_and_commands():
    assert _brain_spec({"default": "typesafe"}) == {"default": "typesafe"}
    spec = _brain_spec({"default": ["bin", "agent"], "curator": ["pi", "-p"]})
    assert spec["curator"] == ["pi", "-p"]


def test_brain_table_rejects_the_removed_sdk_backend():
    with pytest.raises(ConfigError, match='must be "typesafe"'):
        _brain_spec({"default": "claude"})


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


# -- the built-in brain is fail-closed: API money is authorized, not defaulted

def test_an_absent_brain_table_refuses_with_the_remedy(sandbox):
    """An absent table means no default brain is named -- a refusal that
    says how to route the roles to commands."""
    from autoresearch.driver.brain import build_brain
    with pytest.raises(AutoresearchError, match=r"\[brain\] default"):
        build_brain(sandbox)


def test_authorization_is_explicit_from_config_or_flag(sandbox, monkeypatch):
    from autoresearch.driver.brain import TypeSafeBrain, build_brain
    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    sandbox.brain = {"default": "typesafe", "authorize_spend": True}
    assert isinstance(build_brain(sandbox).default, TypeSafeBrain)
    sandbox.brain = {"default": "typesafe"}
    assert isinstance(build_brain(sandbox, allow_paid=True).default, TypeSafeBrain)


def test_the_flag_grants_and_never_revokes_domain_authorization(sandbox, monkeypatch):
    """argparse's store_true default is False, not None: a plain `ar loop`
    must not undo the domain's own `authorize_spend = true`."""
    from autoresearch.driver.brain import TypeSafeBrain, build_brain
    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    sandbox.brain = {"default": "typesafe", "authorize_spend": True}
    assert isinstance(build_brain(sandbox, allow_paid=False).default, TypeSafeBrain)


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


def test_one_typesafe_role_is_enough_to_require_authorization(sandbox, monkeypatch):
    from autoresearch.driver.brain import TypeSafeBrain, build_brain
    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    sandbox.brain = {"default": echo_command(), "judge": "typesafe"}
    with pytest.raises(AutoresearchError, match="judge"):
        build_brain(sandbox)
    assert isinstance(build_brain(sandbox, allow_paid=True).routes["judge"], TypeSafeBrain)


# -- the typesafe brain (Jev): typed questions over a state, no text ----------

def stub_typesafe(answers, usage=None, captured=None):
    """A TypeSafeBrain standing in for the wire: `_post` is the seam tests
    substitute."""
    from autoresearch.driver.brain import TypeSafeBrain
    brain = TypeSafeBrain(api_key="test",
                          input_per_mtok=1.0, output_per_mtok=2.0)

    def fake_post(payload):
        if captured is not None:
            captured.append(payload)
        return {"model": payload["model"], "answers": answers,
                "usage": usage if usage is not None
                else {"input_tokens": 0, "output_tokens": 0}}

    brain._post = fake_post
    return brain


def test_the_typesafe_brain_refuses_roles_that_need_text_or_tools():
    from autoresearch.driver.brain import TypeSafeBrain
    brain = TypeSafeBrain(api_key="test")
    with pytest.raises(AutoresearchError, match="cannot serve 'generator'"):
        brain.ask("generator", "{}")
    with pytest.raises(AutoresearchError, match="cannot serve 'worker'"):
        brain.ask("worker", "{}", workspace="/tmp")


def test_the_brief_travels_as_the_state_and_names_the_model():
    captured = []
    brain = stub_typesafe({"problem_0": {"type": "noul", "noul": 0.9}},
                          captured=captured)
    brain.ask("qc", json.dumps({"mechanical_problems": ["p"]}))
    assert captured[0]["model"] == "jev-latest"
    assert captured[0]["state"] == {"mechanical_problems": ["p"]}
    assert set(captured[0]["questions"]) == {"problem_0"}


def test_the_wire_state_is_trimmed_to_what_the_role_reads():
    """The coordinator's brief carries the whole record; a 258KB brief
    exceeds the System One token budget (HTTP 400 max_tokens_exceeded).
    The judge reviews the top `JUDGE_REVIEW_CAP` ranking rows -- each already
    carrying its `novel` flag from the risk dial -- so only those, plus the
    context allowlist, travel on the wire."""
    captured = []
    answers = {f"reorder_Q{i}": {"type": "choice", "choice": "none"}
               for i in range(12)}
    brain = stub_typesafe(answers, captured=captured)
    ranking = [{"id": f"Q{i}", "score": 1.0, "terms": {}, "title": f"t{i}",
                "novel": i % 2 == 0}
               for i in range(200)]
    brief = json.dumps({
        "role": "judge", "domain": "qsb",
        "instruction": "review the ordering",
        "budget_remaining": {"runs": 3, "gpu_hours": None, "money": 92.0},
        "ranking": ranking, "excluded": [{"id": "Qx", "why": "stale"}],
        "open_entries": [{"id": f"Q{i}", "hypothesis": "words" * 500}
                         for i in range(150)],
        "closed_directions": [{"id": "Q0", "verdict": "refuted"}],
        "risk": 0.5})
    brain.ask("judge", brief)
    wire = captured[0]["state"]
    assert len(wire["ranking"]) == brain.JUDGE_REVIEW_CAP
    assert wire["ranking"][0]["novel"] is True
    assert wire["instruction"] == "review the ordering"
    assert wire["budget_remaining"] == {"runs": 3, "gpu_hours": None,
                                        "money": 92.0}
    assert "open_entries" not in wire and "closed_directions" not in wire \
        and "excluded" not in wire
    assert len(json.dumps(wire)) < 8_000, "the wire state stays small"


def test_judge_answers_become_vetoes_carrying_their_probabilities():
    brain = stub_typesafe({
        "reorder_A1": {"type": "choice", "choice": "promote",
                       "probabilities": {"promote": 0.55, "demote": 0.1,
                                         "none": 0.35},
                       "confidence": 0.2},
        "reorder_A2": {"type": "choice", "choice": "demote",
                       "probabilities": {"promote": 0.05, "demote": 0.2,
                                         "none": 0.75},
                       "confidence": 0.55},
    })
    brief = json.dumps({"ranking": [
        {"id": "A1", "score": 1.0, "terms": {}, "title": "first"},
        {"id": "A2", "score": 0.5, "terms": {}, "title": "second"}]})
    reply = brain.ask("judge", brief)
    assert reply.backend == "typesafe"
    assert [v["entry_id"] for v in reply.data] == ["A1"], \
        "an action 'none' out-probabilities is not a veto"
    veto = reply.data[0]
    assert veto["action"] == "promote"
    assert "p=0.55" in veto["justification"], \
        "the justification is recorded evidence, apply_veto demands it"
    assert veto["justification"].strip()


def test_a_judge_with_nothing_to_review_spends_no_call():
    brain = stub_typesafe({})

    def boom(payload):
        raise AssertionError("no questions must mean no API call")

    brain._post = boom
    reply = brain.ask("judge", json.dumps({"ranking": []}))
    assert reply.data == []
    assert reply.cost_usd == 0.0


def test_qc_keeps_only_the_problems_jev_confirms():
    brain = stub_typesafe({
        "problem_0": {"type": "noul", "noul": 0.9},
        "problem_1": {"type": "noul", "noul": 0.1},
    })
    brief = json.dumps(
        {"mechanical_problems": ["stale claim", "slow render"]})
    reply = brain.ask("qc", brief)
    assert reply.data == {"problems": ["stale claim"], "harness_debt": [],
                          "verdict": "problems"}


def test_a_non_numeric_noul_is_refused_not_guessed():
    brain = stub_typesafe({"problem_0": {"type": "noul", "noul": "very"}})
    with pytest.raises(AutoresearchError, match="non-numeric noul"):
        brain.ask("qc", json.dumps({"mechanical_problems": ["p"]}))


def test_a_missing_answer_is_refused_not_guessed():
    brain = stub_typesafe({"problem_0": {"type": "noul", "noul": 0.9}})
    with pytest.raises(AutoresearchError, match="did not answer"):
        brain.ask("qc", json.dumps({"mechanical_problems": ["a", "b"]}))


def test_cost_is_priced_from_usage_when_the_domain_priced_the_model():
    brain = stub_typesafe(
        {"problem_0": {"type": "noul", "noul": 0.9}},
        usage={"input_tokens": 1_000_000, "output_tokens": 50_000})
    reply = brain.ask("qc", json.dumps({"mechanical_problems": ["p"]}))
    assert reply.cost_usd == pytest.approx(1.0 + 0.1)


def test_an_unpriced_model_reports_unknown_cost_never_zero():
    from autoresearch.driver.brain import TypeSafeBrain
    brain = TypeSafeBrain(api_key="test")
    brain._post = lambda payload: {
        "answers": {"problem_0": {"type": "noul", "noul": 0.9}},
        "usage": {"input_tokens": 10, "output_tokens": 5}}
    reply = brain.ask("qc", json.dumps({"mechanical_problems": ["p"]}))
    assert reply.cost_usd is None


# -- the typesafe brain in the table and the router ---------------------------

def test_brain_table_accepts_typesafe_routes_and_options():
    spec = _brain_spec({"judge": "typesafe", "typesafe_model": "jev-latest",
                        "typesafe_input_per_mtok": 1.5})
    assert spec["judge"] == "typesafe"
    assert spec["typesafe_model"] == "jev-latest"


def test_brain_table_rejects_unknown_typesafe_options_and_bad_values():
    with pytest.raises(ConfigError, match="unknown typesafe option"):
        _brain_spec({"typesafe_temperture": 0.5})
    with pytest.raises(ConfigError, match="per_mtok"):
        _brain_spec({"typesafe_input_per_mtok": "cheap"})
    with pytest.raises(ConfigError, match='must be "typesafe"'):
        _brain_spec({"judge": "jev"})


def test_the_typesafe_brain_is_paid_and_fail_closed(sandbox, monkeypatch):
    from autoresearch.driver.brain import TypeSafeBrain, build_brain
    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    sandbox.brain = {"default": echo_command(), "judge": "typesafe"}
    with pytest.raises(AutoresearchError, match="authorize_spend"):
        build_brain(sandbox)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    sandbox.brain = {"default": echo_command(), "judge": "typesafe",
                     "authorize_spend": True}
    with pytest.raises(AutoresearchError, match="TYPESAFE_API_KEY"):
        build_brain(sandbox)
    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    assert isinstance(build_brain(sandbox).routes["judge"], TypeSafeBrain)


def test_typesafe_and_a_process_brain_share_one_campaign_ceiling(sandbox, monkeypatch):
    from autoresearch.driver.brain import TypeSafeBrain, build_brain
    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    sandbox.brain = {"default": echo_command(), "judge": "typesafe"}
    brain = build_brain(sandbox, max_budget_usd=5.0, allow_paid=True)
    assert isinstance(brain.routes["judge"], TypeSafeBrain)
    assert brain.routes["judge"].ledger is brain.default.ledger


def test_typesafe_options_are_settings_never_routes(sandbox, monkeypatch):
    """A regression: the routes comprehension once treated `typesafe_url` as a
    role name, and building the brain tried to run a float as a command."""
    from autoresearch.driver.brain import TypeSafeBrain, build_brain
    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    sandbox.brain = {"default": echo_command(), "judge": "typesafe",
                     "authorize_spend": True,
                     "typesafe_model": "jev-latest",
                     "typesafe_input_per_mtok": 3.0}
    brain = build_brain(sandbox)
    assert isinstance(brain.routes["judge"], TypeSafeBrain)
    assert brain.routes["judge"].model == "jev-latest"
    assert brain.routes["judge"].input_per_mtok == 3.0
    assert set(brain.routes) == {"judge"}, "options never become backends"


def test_a_process_brain_route_next_to_typesafe_needs_no_key(sandbox):
    """Authorization and the API key are demanded only of the backends that
    can spend. The default is a command here, so no gate applies to it."""
    from autoresearch.driver.brain import ProcessBrain, build_brain
    sandbox.brain = {"default": echo_command(), "curator": echo_command()}
    brain = build_brain(sandbox)
    assert isinstance(brain.routes["curator"], ProcessBrain)


def test_the_default_may_be_the_typesafe_brain(sandbox, monkeypatch):
    from autoresearch.driver.brain import TypeSafeBrain, build_brain
    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    sandbox.brain = {"default": "typesafe", "authorize_spend": True}
    brain = build_brain(sandbox)
    assert isinstance(brain.default, TypeSafeBrain)
    assert brain.routes == {}, "no routes named, every role falls to the default"
