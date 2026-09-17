"""Impossible dispatches leave allocation, claims and worker workspaces untouched."""
import json
import shlex
import subprocess
import sys
from types import SimpleNamespace

import pytest

from autoresearch.budget import iteration_budget
from autoresearch.driver.brain import Reply, Role
from autoresearch.driver.loop import Coordinator, Iteration
from autoresearch.policy import Rule
from autoresearch.runs import read_all
from autoresearch.workspaces import Pool
from conftest import make_entry


def hook(config, body):
    path = config.paths.root / "dispatch preflight.py"
    path.write_text(body)
    config.commands["preflight"] = shlex.join([sys.executable, path.name])
    return config.commands["preflight"]


def setup_dispatch(config, store, ids=("Q1",)):
    for entry_id in ids:
        make_entry(store, entry_id, bar="domain-specific eligibility", cost=1)
    calls = []

    class Brain:
        def ask(self, role, brief, *, workspace=None):
            assert role == Role.WORKER
            entry_id = json.loads(brief)["entry"]["id"]
            calls.append(entry_id)
            assert store.load(entry_id).claim is not None
            assert workspace.is_dir()
            return Reply(role=role, data={
                "verdict": "inconclusive", "runs": 0,
                "summary": "static review only",
                "verification": {"reread": True, "claims_checked": ["bar"]},
            })

    coordinator = Coordinator(config, Brain())
    iteration = Iteration(n=1)
    budget = iteration_budget(config)
    cards = [SimpleNamespace(entry_id=entry_id, terms={"cost": 1}) for entry_id in ids]
    return coordinator, iteration, budget, cards, calls


def assert_blocked(config, store, monkeypatch):
    coordinator, iteration, budget, cards, calls = setup_dispatch(config, store)
    original = store.load("Q1")

    def forbidden(*args, **kwargs):
        pytest.fail("blocked dispatch attempted a claim or worker workspace")

    monkeypatch.setattr(coordinator.claims, "claim", forbidden)
    with Pool(config, "preflight-test") as pool:
        monkeypatch.setattr(pool, "acquire", forbidden)
        phase = coordinator.dispatch(iteration, cards, budget, pool)
    assert calls == []
    assert iteration.verdicts == {"Q1": "blocked"}
    assert phase.detail
    assert {key: budget[key].spent for key in ("fanout", "spawns", "runs")} == {
        "fanout": 0, "spawns": 0, "runs": 0,
    }
    assert iteration.runs == coordinator.domain_budget["runs"].spent == 0
    assert iteration.run_ids == []
    assert read_all(config.paths.runs) == []
    assert store.load("Q1") == original
    assert not config.paths.workspaces.exists()
    return phase


def test_infeasible_hook_cannot_allocate_claim_or_spawn(sandbox, store, monkeypatch):
    reason = "import-only adapter cannot produce a ranked eligible record"
    hook(sandbox, "import json, pathlib, sys\n"
         "request = json.load(sys.stdin)\n"
         "assert request['entry']['id'] == 'Q1'\n"
         "assert request['entry']['bar'] == 'domain-specific eligibility'\n"
         "assert pathlib.Path('domain.toml').is_file()\n"
         f"print(json.dumps({{'feasible': False, 'reason': {reason!r}}}))\n")
    phase = assert_blocked(sandbox, store, monkeypatch)
    assert f"Q1: {reason}" in phase.detail


@pytest.mark.parametrize("body", [
    "print('not JSON')",
    "print('[]')",
    "print('{}')",
    "print('{\"feasible\": 1, \"reason\": \"bad boolean\"}')",
    "print('{\"feasible\": true}')",
    "print('{\"feasible\": false, \"reason\": \"  \"}')",
    "print('{\"feasible\": false, \"reason\": null}')",
    "print('{\"feasible\": true, \"reason\": \"\"} trailing')",
    "import sys; print('{\"feasible\": true, \"reason\": \"\"}'); sys.exit(7)",
], ids=["invalid-json", "not-object", "missing-feasible", "not-boolean",
        "missing-reason", "empty-denial", "not-string", "extra-output", "nonzero"])
def test_invalid_hooks_fail_closed(sandbox, store, monkeypatch, body):
    hook(sandbox, body)
    assert_blocked(sandbox, store, monkeypatch)


@pytest.mark.parametrize("command", ["", "'unterminated", "./missing-preflight"])
def test_unusable_hook_commands_fail_closed(sandbox, store, monkeypatch, command):
    sandbox.commands["preflight"] = command
    assert_blocked(sandbox, store, monkeypatch)


def test_hook_timeout_fails_closed(sandbox, store, monkeypatch):
    command = hook(sandbox, "raise AssertionError('timeout simulation must intercept me')")
    run = subprocess.run

    def timeout(args, **kwargs):
        if args == shlex.split(command):
            assert kwargs["timeout"] == 10
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])
        return run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", timeout)
    assert_blocked(sandbox, store, monkeypatch)


@pytest.mark.parametrize("restricted", ["measure", "preflight"])
def test_human_only_commands_block_before_hook_execution(
        sandbox, store, monkeypatch, restricted):
    hook(sandbox, "from pathlib import Path\nPath('hook-ran').touch()\n"
         "print('{\"feasible\": true, \"reason\": \"\"}')\n")
    measure = sandbox.paths.root / "measure.py"
    measure.write_text("from pathlib import Path\nPath('measure-ran').touch()\n")
    sandbox.commands["measure"] = shlex.join([sys.executable, measure.name])
    sandbox.policy.human_only.append(Rule(
        "human_only", sandbox.commands[restricted], "requires a human"))
    phase = assert_blocked(sandbox, store, monkeypatch)
    assert any("requires a human" in detail for detail in phase.detail)
    assert not (sandbox.paths.root / "hook-ran").exists()
    assert not (sandbox.paths.root / "measure-ran").exists()


def test_human_only_measure_blocks_without_a_hook(sandbox, store, monkeypatch):
    sandbox.commands.pop("preflight", None)
    sandbox.policy.human_only.append(Rule(
        "human_only", sandbox.commands["measure"], "requires a human"))
    assert_blocked(sandbox, store, monkeypatch)


@pytest.mark.parametrize("configured", [False, True], ids=["missing-hook", "feasible-hook"])
def test_feasible_or_missing_hook_preserves_dispatch(sandbox, store, configured):
    if configured:
        hook(sandbox, "print('{\"feasible\": true, \"reason\": \"\"}')")
    else:
        sandbox.commands.pop("preflight", None)
    coordinator, iteration, budget, cards, calls = setup_dispatch(sandbox, store)
    with Pool(sandbox, "preflight-test") as pool:
        coordinator.dispatch(iteration, cards, budget, pool)
    assert calls == ["Q1"]
    assert iteration.verdicts == {"Q1": "inconclusive"}
    assert all(budget[key].spent == 1 for key in ("fanout", "spawns", "runs"))
    assert store.load("Q1").claim is None
    assert iteration.runs == 1  # the attempt reservation is spent even for a static reply


def test_blocked_card_leaves_capacity_for_next_card(sandbox, store):
    sandbox.budgets.update(iteration_fanout=1, iteration_max_spawns=1, iteration_max_runs=1)
    hook(sandbox, "import json, sys\n"
         "entry = json.load(sys.stdin)['entry']\n"
         "print(json.dumps({'feasible': entry['id'] == 'Q2', 'reason': 'import only'}))\n")
    coordinator, iteration, budget, cards, calls = setup_dispatch(sandbox, store, ("Q1", "Q2"))
    original = store.load("Q1")
    with Pool(sandbox, "preflight-test") as pool:
        coordinator.dispatch(iteration, cards, budget, pool)
    assert calls == ["Q2"]
    assert iteration.verdicts == {"Q1": "blocked", "Q2": "inconclusive"}
    assert store.load("Q1") == original
    assert all(budget[key].spent == 1 for key in ("fanout", "spawns", "runs"))
