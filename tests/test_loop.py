"""The coordinator, end to end, with no model in the loop."""
import json

import pytest
from conftest import make_entry
from toy_brain import ToyBrain

from autoresearch.budget import BUDGET, MET, recorded_usage
from autoresearch.driver.brain import Reply, Role, ScriptedBrain, extract_json
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


# -- the meters that were declared and never spent -------------------------

IDLE = {Role.GENERATOR: lambda b: [], Role.JUDGE: lambda b: [],
        Role.WORKER: lambda b: {"verdict": "inconclusive", "summary": "n/a"},
        Role.CURATOR: lambda b: {"reprice": [], "notes": []},
        Role.QC: lambda b: {"problems": [], "harness_debt": [], "verdict": "clean"}}


class CostlyBrain(ScriptedBrain):
    """A brain that bills. Every role costs the same, so the arithmetic in a
    test is the number of asks."""

    def __init__(self, per_ask=0.05, handlers=None):
        super().__init__(handlers or dict(IDLE))
        self.per_ask = per_ask

    def ask(self, role, brief, *, workspace=None, max_turns=None) -> Reply:
        reply = super().ask(role, brief, workspace=workspace, max_turns=max_turns)
        return Reply(role=reply.role, data=reply.data, raw=reply.raw,
                     cost_usd=self.per_ask)


def test_model_spend_is_charged_to_the_campaign_money_meter(sandbox):
    """The money meter was built from `policy.spend_ceiling` and then never
    spent, so the loop could not reach the budget exit on money."""
    c = Coordinator(sandbox, CostlyBrain(0.05))
    it = c.run_iteration(1)
    assert it.cost_usd == pytest.approx(0.25), "five roles, one ask each"
    assert c.domain_budget["money"].spent == pytest.approx(it.cost_usd)


def test_the_money_ceiling_survives_a_restart(sandbox):
    """A ceiling re-read as zero at every `ar loop` is not a ceiling; it is a
    per-invocation allowance anyone can renew with the up-arrow."""
    Coordinator(sandbox, CostlyBrain(0.05)).run(max_iterations=1)
    assert recorded_usage(sandbox)["money"] == pytest.approx(0.25)
    fresh = Coordinator(sandbox, CostlyBrain(0.05))
    assert fresh.domain_budget["money"].spent == pytest.approx(0.25)


def test_a_restart_does_not_overwrite_the_records_it_charges_against(sandbox):
    """Numbering from zero in each process rewrote `0001.json`, throwing away
    the spend it held -- so the ceiling reset anyway, one iteration at a time."""
    Coordinator(sandbox, CostlyBrain(0.05)).run(max_iterations=1)
    second = Coordinator(sandbox, CostlyBrain(0.05))
    second.run(max_iterations=1)

    written = sorted(p.name for p in sandbox.paths.iterations.glob("*.json"))
    assert written == ["0001.json", "0002.json"]
    assert recorded_usage(sandbox)["money"] == pytest.approx(0.50)
    assert Coordinator(sandbox, CostlyBrain(0.05)) \
        .domain_budget["money"].spent == pytest.approx(0.50)


def test_the_loop_stops_when_the_spend_ceiling_is_reached(sandbox):
    sandbox.policy.spend_ceiling = 0.10
    it = Coordinator(sandbox, CostlyBrain(0.05)).run_iteration(1)
    assert it.stop == BUDGET and "money" in it.stop_detail


def test_qc_comes_out_of_the_same_spawn_pool_as_every_other_role(sandbox):
    """Generators, judge and curator each spend a spawn; QC asked a model
    without spending one, so the meter that bounds an iteration undercounted by
    one every iteration."""
    sandbox.budgets["iteration_max_spawns"] = 5       # 2 generators, judge, curator, qc
    ran = Coordinator(sandbox, ScriptedBrain(dict(IDLE))).run_iteration(1)
    assert not any("qc role skipped" in d for d in _phase(ran, "qc").detail)

    sandbox.budgets["iteration_max_spawns"] = 4       # one short: QC is the fifth
    starved = Coordinator(sandbox, ScriptedBrain(dict(IDLE))).run_iteration(2)
    skipped = [d for d in _phase(starved, "qc").detail if "qc role skipped" in d]
    assert skipped and "spawns exhausted" in skipped[0], _phase(starved, "qc").detail


def test_the_iteration_seconds_ceiling_stops_new_work(sandbox):
    """`iteration_max_seconds` is scaffolded into every new domain and nothing
    ever spent it -- a ceiling that could not fire."""
    sandbox.budgets["iteration_max_seconds"] = 0
    it = Coordinator(sandbox, ScriptedBrain(dict(IDLE))).run_iteration(1)
    assert [p.name for p in it.phases] == [
        "orient", "generate", "rank", "dispatch", "curate", "qc"], \
        "a phase that did not run must still be visible (H98)"
    for name in ("generate", "rank", "dispatch"):
        phase = _phase(it, name)
        assert phase.error and "ceiling was spent" in phase.error
        assert phase.did == 0
    assert _phase(it, "qc").seconds >= 0, "the record still gets written"


def test_a_workers_runs_and_gpu_hours_are_charged_to_the_campaign(sandbox):
    from autoresearch.entries import Store
    make_entry(Store(sandbox.paths.entries), "Q1", impact=1.0)
    brain = ScriptedBrain({**IDLE, Role.WORKER: lambda b: {
        "verdict": "inconclusive", "summary": "measured, decided nothing",
        "runs": 3, "gpu_hours": 1.5}})
    c = Coordinator(sandbox, brain)
    c.run_iteration(1)
    assert c.domain_budget["runs"].spent == 3
    assert c.domain_budget["gpu_hours"].spent == 1.5


def test_a_workers_measurements_survive_a_restart(sandbox):
    """The rows a worker wrote are in its workspace, not the coordinator's
    ledger, and its GPU-hours are written nowhere else at all -- so a ceiling
    rebuilt from the ledger alone starts every invocation back at zero."""
    from autoresearch.entries import Store
    make_entry(Store(sandbox.paths.entries), "Q1", impact=1.0)
    brain = ScriptedBrain({**IDLE, Role.WORKER: lambda b: {
        "verdict": "inconclusive", "summary": "n/a", "runs": 3, "gpu_hours": 1.5}})
    Coordinator(sandbox, brain).run(max_iterations=1)

    fresh = Coordinator(sandbox, ScriptedBrain(dict(IDLE)))
    assert fresh.domain_budget["runs"].spent == 3
    assert fresh.domain_budget["gpu_hours"].spent == 1.5


def test_the_iteration_run_pool_bounds_the_whole_iteration(sandbox):
    """Charged after the fact the meter bounds nothing: every card is dispatched
    before any of them reports. It is allocated at dispatch instead."""
    from autoresearch.entries import Store
    store = Store(sandbox.paths.entries)
    sandbox.budgets["iteration_max_runs"] = 3
    make_entry(store, "Q1", impact=1.0, cost=2.0)
    make_entry(store, "Q2", impact=1.0, cost=2.0)
    it = Coordinator(sandbox, ScriptedBrain(dict(IDLE))).run_iteration(1)
    assert sorted(it.shortlist) == ["Q1", "Q2"], "both are individually affordable"
    assert len(it.verdicts) == 1, "two of them are not"
    assert any("runs exhausted" in d for d in _phase(it, "dispatch").detail)


def test_the_iteration_run_ceiling_gates_the_shortlist(sandbox):
    """An entry costing more runs than one iteration may spend is unaffordable,
    exactly as one costing more than the campaign has left."""
    from autoresearch.entries import Store
    sandbox.budgets["iteration_max_runs"] = 1
    make_entry(Store(sandbox.paths.entries), "Q1", impact=1.0, cost=2.0)
    it = Coordinator(sandbox, ScriptedBrain(dict(IDLE))).run_iteration(1)
    assert it.shortlist == []


def _phase(it, name):
    return next(p for p in it.phases if p.name == name)


# -- the direction the loop optimises in -----------------------------------

def test_a_maximise_goal_stops_on_its_best_row_not_its_worst(sandbox):
    """`_best` hardcoded `<`, so a maximise domain fed its *worst* row to the
    stop decision and could never reach the goal-met exit."""
    from autoresearch.goal import Goal
    from autoresearch.runs import RunRecord, append

    sandbox.goal = Goal.from_dict({"goal": {
        "id": "climb", "objective": "ops", "direction": "maximise",
        "metrics": {"ops": {}, "peak": {}}, "target": {"value": 50.0}}})
    for ops in (10.0, 100.0):
        append(sandbox.paths.runs / "seed.jsonl",
               RunRecord(metrics={"ops": ops, "peak": 1.0}, session="seed",
                         provenance={"host": "test"}))

    it = Coordinator(sandbox, ScriptedBrain(dict(IDLE))).run_iteration(1)
    assert it.objective == 100.0, "the best row of a maximise goal is the largest"
    assert it.stop == MET, it.report()
