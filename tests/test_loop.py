"""The coordinator, end to end, with no model in the loop."""
import json

import pytest
from conftest import make_entry
from toy_brain import ToyBrain

from autoresearch.budget import MET
from autoresearch.driver.brain import Role, ScriptedBrain, extract_json
from autoresearch.driver.loop import Coordinator
from autoresearch.errors import AutoresearchError


@pytest.fixture
def coordinator(sandbox):
    def probe(command):
        import subprocess
        r = subprocess.run(command.split(), cwd=sandbox.paths.root,
                           capture_output=True, text=True)
        return float(r.stdout.strip())
    return Coordinator(sandbox, ToyBrain(sandbox), probe_target=probe)


def test_one_iteration_runs_every_phase(coordinator):
    it = coordinator.run_iteration(1)
    assert [p.name for p in it.phases] == [
        "orient", "generate", "rank", "dispatch", "curate", "qc"]
    assert it.phases[1].did >= 1, "generation filed nothing"
    assert it.shortlist, "nothing was dispatched"
    assert it.verdicts, "no verdicts recorded"


def test_qc_is_clean_after_a_normal_iteration(coordinator):
    it = coordinator.run_iteration(1)
    qc = next(p for p in it.phases if p.name == "qc")
    assert qc.error is None, f"QC found: {qc.detail}"


def test_no_claim_outlives_its_iteration(coordinator):
    """A claim that survives dispatch is the squat H78/H125 describe."""
    coordinator.run_iteration(1)
    held = [e for e in coordinator.store.all()
            if e.claim and not coordinator.config.track_for(
                e.id).machine.status(e.status).terminal]
    assert held == []


def test_every_closure_has_a_memo_that_exists(coordinator):
    coordinator.run_iteration(1)
    for entry in coordinator.store.all():
        if entry.result:
            assert (coordinator.config.paths.root / entry.result.memo).exists()


def test_loop_reaches_the_goal_and_stops(coordinator):
    """Verification 1: the loop generates, ranks, dispatches in parallel,
    curates, QCs, and stops on its own when the target is met."""
    history = coordinator.run(max_iterations=6)
    assert history[-1].stop == MET, history[-1].report()
    assert len(history) < 6, "should have stopped before exhausting iterations"
    assert history[-1].objective < history[0].objective


def test_generation_runs_every_iteration_not_only_when_starved(coordinator):
    """H95: 'fewer than three queued, audit instead' forbade draining a short
    queue, so an entry filed when the queue was short was skipped forever."""
    coordinator.run(max_iterations=2)
    generated = [p.did for it in coordinator.history
                 for p in it.phases if p.name == "generate"]
    assert all(g >= 1 for g in generated[:2]), generated


def test_iteration_records_are_written(coordinator):
    coordinator.run_iteration(1)
    path = coordinator.config.paths.iterations / "0001.json"
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["n"] == 1 and data["phases"]


def test_fanout_budget_caps_dispatch(sandbox):
    """The coordinator allocates workers from a fixed pool, so it cannot
    dispatch its way past its own budget."""
    sandbox.budgets["iteration_fanout"] = 1
    for i in range(5):
        make_entry(__import__("autoresearch.entries", fromlist=["Store"]).Store(
            sandbox.paths.entries), f"Q{i + 1}", impact=1.0,
            sources=[json.dumps({"unroll": 1, "width": 32, "fold": 1, "cache": 0})])
    c = Coordinator(sandbox, ToyBrain(sandbox))
    it = c.run_iteration(1)
    assert len(it.shortlist) == 1


def test_a_worker_returning_inconclusive_releases_the_claim(sandbox):
    """H26: the loop had no way to report 'measured, found nothing', so agents
    reached for a status that was not true."""
    from autoresearch.entries import Store
    store = Store(sandbox.paths.entries)
    make_entry(store, "Q1", impact=1.0)
    brain = ScriptedBrain({
        Role.GENERATOR: lambda b: [],
        Role.JUDGE: lambda b: [],
        Role.WORKER: lambda b: {"verdict": "inconclusive",
                                "summary": "the measurement did not decide the bar"},
        Role.CURATOR: lambda b: {"reprice": [], "notes": []},
        Role.QC: lambda b: {"problems": [], "harness_debt": [], "verdict": "clean"}})
    it = Coordinator(sandbox, brain).run_iteration(1)
    entry = store.load("Q1")
    assert it.verdicts["Q1"] == "inconclusive"
    assert entry.status == "queued" and entry.claim is None
    assert any(ev.kind == "released" for ev in entry.history)


def test_a_close_without_evidence_is_refused_and_the_claim_returned(sandbox):
    from autoresearch.entries import Store
    store = Store(sandbox.paths.entries)
    make_entry(store, "Q1", impact=1.0)
    brain = ScriptedBrain({
        Role.GENERATOR: lambda b: [],
        Role.JUDGE: lambda b: [],
        Role.WORKER: lambda b: {"verdict": "confirmed", "memo": "inbox/ghost.md",
                                "summary": "it worked, trust me"},
        Role.CURATOR: lambda b: {"reprice": [], "notes": []},
        Role.QC: lambda b: {"problems": [], "harness_debt": [], "verdict": "clean"}})
    it = Coordinator(sandbox, brain).run_iteration(1)
    entry = store.load("Q1")
    assert it.verdicts["Q1"] == "refused"
    assert entry.status == "queued", "an unevidenced verdict must not close an entry"


def test_qc_files_harness_debt_against_the_harness_track(sandbox):
    from autoresearch.entries import Store
    store = Store(sandbox.paths.entries)
    make_entry(store, "Q1", impact=1.0)
    brain = ScriptedBrain({
        Role.GENERATOR: lambda b: [],
        Role.JUDGE: lambda b: [],
        Role.WORKER: lambda b: {"verdict": "inconclusive", "summary": "n/a"},
        Role.CURATOR: lambda b: {"reprice": [], "notes": []},
        Role.QC: lambda b: {"problems": [], "verdict": "problems", "harness_debt": [
            {"title": "measure command has no timeout", "hypothesis": "a hung run stalls the pool"}]}})
    Coordinator(sandbox, brain).run_iteration(1)
    assert any(e.id.startswith("H") for e in store.all())


def test_extract_json_handles_fenced_and_bare_replies():
    assert extract_json('prose\n```json\n[{"a": 1}]\n```\n') == [{"a": 1}]
    assert extract_json('here you go: {"a": 2} thanks') == {"a": 2}
    with pytest.raises(AutoresearchError, match="no JSON found"):
        extract_json("I could not comply.")
