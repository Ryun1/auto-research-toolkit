"""The coordinator, end to end, with no model in the loop."""
import json

import pytest

from autoresearch.budget import BUDGET, MET, recorded_usage
from autoresearch.driver.brain import Reply, Role, ScriptedBrain, extract_json
from autoresearch.driver.loop import Coordinator, Iteration
from autoresearch.errors import AutoresearchError
from conftest import make_entry
from toy_brain import ToyBrain


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
        "orient", "generate", "rank", "dispatch", "curate", "distil", "qc"]
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


VERIFIED = {"reread": True, "claims_checked": ["summary", "verdict"],
            "corrections": []}


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
                                "summary": "the measurement did not decide the bar",
                                "verification": dict(VERIFIED)},
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
                                "summary": "it worked, trust me",
                                "verification": dict(VERIFIED)},
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
        Role.WORKER: lambda b: {"verdict": "inconclusive", "summary": "n/a",
                                "verification": dict(VERIFIED)},
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
        Role.WORKER: lambda b: {"verdict": "inconclusive", "summary": "n/a",
                                "verification": dict(VERIFIED)},
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


def test_a_generator_that_raises_is_attributed(sandbox, store):
    """Filtering a failed generator out of the replies reads as a queue that
    starves; a failure goes on the record, the way a scout's does."""
    import re
    import threading
    proposal = {
        "title": "an unrolled idea",
        "hypothesis": "unrolling beats caching here",
        "prediction": "measuring it moves the objective",
        "bar": "at least 1% on the objective",
        "confidence": 0.4, "impact": 0.05, "cost": 2.0,
        "mechanisms": ["generated"],
        "why_filed": "the board has not tried it",
    }
    outcomes = [RuntimeError("the generator's backend fell over"), [proposal]]
    lock = threading.Lock()

    def handler(brief):
        with lock:
            outcome = outcomes.pop()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    coordinator = Coordinator(
        sandbox, ScriptedBrain({**IDLE, Role.GENERATOR: handler}))
    history = coordinator.run(max_iterations=1)
    phase = next(p for it in history for p in it.phases if p.name == "generate")
    assert len(store.all()) == 1, "the healthy generator's idea survives"
    assert any(re.search(r"generator \d+ raised", line) for line in phase.detail)


def test_a_failed_target_probe_is_recorded_not_silent(sandbox):
    """target=None must be distinguishable from 'no target configured' (H98):
    the orient record carries the reason the probe failed."""
    def broken_probe(command):
        raise RuntimeError("the probe exploded")

    coordinator = Coordinator(sandbox, ScriptedBrain(IDLE),
                              probe_target=broken_probe)
    it = Iteration(n=coordinator._last_recorded_n() + 1)
    phase = coordinator.orient(it)
    assert it.target is None
    assert any("target probe failed" in line and "exploded" in line
               for line in phase.detail)


def test_a_verdict_without_verification_is_refused(sandbox, store):
    """The re-review is the completion protocol, not advice: a verdict whose
    own agent did not re-verify it is a claim the record takes on faith."""
    make_entry(store, "Q1", impact=1.0)
    (sandbox.paths.root / "inbox" / "real.md").write_text("memo exists")
    brain = ScriptedBrain({
        Role.GENERATOR: lambda b: [],
        Role.JUDGE: lambda b: [],
        Role.WORKER: lambda b: {"verdict": "confirmed", "memo": "inbox/real.md",
                                "summary": "it worked", "verification": None},
        Role.CURATOR: lambda b: {"reprice": [], "notes": []},
        Role.QC: lambda b: {"problems": [], "harness_debt": [], "verdict": "clean"}})
    it = Coordinator(sandbox, brain).run_iteration(1)
    entry = store.load("Q1")
    assert it.verdicts["Q1"] == "refused"
    assert entry.status == "queued", "an unverified verdict must not close an entry"
    dispatch = next(p for p in it.phases if p.name == "dispatch")
    assert any("verification" in d for d in dispatch.detail)


def test_a_verified_verdict_records_the_rereview(sandbox, store):
    """The verification block is provenance: the closure says not only what was
    claimed but that the agent that made it re-checked it."""
    from autoresearch.entries import Store
    make_entry(store, "Q1", impact=1.0)
    (sandbox.paths.root / "inbox" / "real.md").write_text("numbers")
    verification = {"reread": True, "claims_checked": ["objective"],
                    "corrections": []}
    brain = ScriptedBrain({
        Role.GENERATOR: lambda b: [],
        Role.JUDGE: lambda b: [],
        Role.WORKER: lambda b: {"verdict": "confirmed", "memo": "inbox/real.md",
                                "summary": "it worked", "verification": verification},
        Role.CURATOR: lambda b: {"reprice": [], "notes": []},
        Role.QC: lambda b: {"problems": [], "harness_debt": [], "verdict": "clean"}})
    Coordinator(sandbox, brain).run_iteration(1)
    entry = Store(sandbox.paths.entries).load("Q1")
    assert entry.status == "confirmed"
    assert entry.result.verification == verification


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
        "orient", "generate", "rank", "dispatch", "curate", "distil", "qc"], \
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
        "runs": 3, "gpu_hours": 1.5, "verification": dict(VERIFIED)}})
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
        "verdict": "inconclusive", "summary": "n/a", "runs": 3, "gpu_hours": 1.5,
        "verification": dict(VERIFIED)}})
    Coordinator(sandbox, brain).run(max_iterations=1)

    fresh = Coordinator(sandbox, ScriptedBrain(dict(IDLE)))
    assert fresh.domain_budget["runs"].spent == 3
    assert fresh.domain_budget["gpu_hours"].spent == 1.5


def test_retained_measurements_are_not_charged_again_on_restart(coordinator):
    coordinator.run_iteration(1)
    spent = coordinator.domain_budget["runs"].spent
    fresh = Coordinator(coordinator.config, ScriptedBrain(dict(IDLE)))
    assert spent > 0
    assert fresh.domain_budget["runs"].spent == spent


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
# -- resuming on another machine ------------------------------------------
#
# `state/iterations/` was write-only. Nothing read it back, so every new
# `ar loop` process numbered from 1 again. These are the two things that broke.

def test_a_new_coordinator_resumes_the_iteration_number(sandbox):
    first = Coordinator(sandbox, ToyBrain(sandbox))
    first.run_iteration(1)
    first.run_iteration(2)

    second = Coordinator(sandbox, ToyBrain(sandbox))
    assert [i.n for i in second.history] == [1, 2], "history was not read back"
    assert second.run(max_iterations=1)[-1].n == 3


def test_resuming_does_not_clobber_the_earlier_records(sandbox):
    first = Coordinator(sandbox, ToyBrain(sandbox))
    first.run_iteration(1)
    written = (sandbox.paths.iterations / "0001.json").read_text()

    Coordinator(sandbox, ToyBrain(sandbox)).run(max_iterations=1)
    assert (sandbox.paths.iterations / "0001.json").read_text() == written
    assert (sandbox.paths.iterations / "0002.json").exists()


def test_reusing_an_iteration_number_is_refused_before_any_work(sandbox):
    """Silently overwriting is the failure this whole directory exists to
    prevent, so the collision is loud and it happens before the phases run."""
    coordinator = Coordinator(sandbox, ToyBrain(sandbox))
    coordinator.run_iteration(1)
    with pytest.raises(AutoresearchError, match="already recorded"):
        Coordinator(sandbox, ToyBrain(sandbox)).run_iteration(1)


def test_the_yield_floor_counts_iterations_from_earlier_sessions(sandbox):
    """The stop condition needs `over_iterations` samples of
    confirmed-per-iteration. Run one iteration per process -- which is what
    stopping and resuming looks like -- and it never used to see more than one.
    """
    from autoresearch.driver import loop as loop_mod

    for _ in range(1, 4):
        Coordinator(sandbox, ToyBrain(sandbox)).run(max_iterations=1)

    seen = []
    real = loop_mod.budget_mod.should_stop

    def spy(config, **kw):
        seen.append(kw["verdicts_per_iteration"])
        return real(config, **kw)

    loop_mod.budget_mod.should_stop = spy
    try:
        Coordinator(sandbox, ToyBrain(sandbox)).run(max_iterations=1)
    finally:
        loop_mod.budget_mod.should_stop = real
    assert len(seen[-1]) == 4, "the yield floor saw only this process's work"


def test_a_malformed_iteration_record_is_reported_not_skipped(sandbox):
    (sandbox.paths.iterations).mkdir(parents=True, exist_ok=True)
    (sandbox.paths.iterations / "0001.json").write_text("{not json")
    with pytest.raises(AutoresearchError, match="0001.json"):
        Coordinator(sandbox, ToyBrain(sandbox))


def test_run_returns_only_the_iterations_this_process_ran(sandbox):
    """`cmd_loop` prints `len(history)` and sums `cost_usd` over it. Returning
    the resumed history too makes every resume report iterations it did not run
    and money it did not spend -- and under `--iterations 1`, the workflow the
    README recommends, that figure grows monotonically forever."""
    Coordinator(sandbox, ToyBrain(sandbox)).run(max_iterations=1)

    second = Coordinator(sandbox, ToyBrain(sandbox))
    ran = second.run(max_iterations=1)
    assert [i.n for i in ran] == [2]
    assert len(second.history) == 2, "the full history is still available"


def test_orient_counts_only_genuinely_resumed_iterations(sandbox):
    """`self.history` grows as the process runs, so reading its length fresh
    each iteration attributes this process's own work to a previous machine --
    in the record whose whole purpose is the audit trail."""
    Coordinator(sandbox, ToyBrain(sandbox)).run(max_iterations=1)

    second = Coordinator(sandbox, ToyBrain(sandbox))
    second.run(max_iterations=2)
    for it in second.history[1:]:
        detail = next(p for p in it.phases if p.name == "orient").detail
        assert "resumed 1 prior iteration(s), through 1" in detail, detail


def test_an_iteration_record_is_written_atomically(sandbox, monkeypatch):
    """`read_history` hard-fails on an unreadable record at construction, so a
    torn write bricks every later `ar loop` -- and losing power mid-write is
    the crash the README documents as recoverable. Truncate-then-write leaves a
    window where the file on disk is neither the old record nor the new one, so
    the record is built beside its destination and moved onto it in one step.
    """
    from autoresearch.driver import loop as loop_mod

    moves, real = [], loop_mod.os.replace

    def spy(src, dst):
        moves.append((str(src), str(dst)))
        return real(src, dst)

    monkeypatch.setattr(loop_mod.os, "replace", spy)
    Coordinator(sandbox, ToyBrain(sandbox)).run_iteration(1)

    assert moves, "the record was written in place, not moved onto its name"
    src, dst = moves[-1]
    assert dst.endswith("0001.json") and src != dst
    assert [p.name for p in sandbox.paths.iterations.iterdir()] == ["0001.json"], \
        "a temporary file was left beside the record"




def test_the_loop_reserves_a_shortlist_slot_for_amplitude(sandbox):
    """The score is EV per unit cost, so a cheap certain increment always beats
    an honest long shot. The coordinator hands part of every shortlist to the
    largest `impact` instead, or the loop never attempts a big swing."""
    from autoresearch.entries import Store
    store = Store(sandbox.paths.entries)
    sandbox.budgets["iteration_fanout"] = 3
    for i in range(4):                    # cheap, likely, small
        make_entry(store, f"Q1{i}", confidence=0.85, impact=0.02, cost=1.0)
    make_entry(store, "Q90", confidence=0.10, impact=0.40, cost=8.0)
    brain = ScriptedBrain({
        Role.GENERATOR: lambda b: [], Role.JUDGE: lambda b: [],
        Role.WORKER: lambda b: {"verdict": "inconclusive", "summary": "no decision",
                                "verification": dict(VERIFIED)},
        Role.CURATOR: lambda b: {"reprice": [], "notes": []},
        Role.QC: lambda b: {"problems": [], "harness_debt": [], "verdict": "clean"}})
    it = Coordinator(sandbox, brain).run_iteration(1)
    assert "Q90" in it.shortlist, it.shortlist
    assert len(it.shortlist) == 3


def test_the_judge_sees_which_entries_the_reserve_would_take(sandbox):
    """A veto is reviewed against the ranking; an unlabelled explore pick reads
    to the judge as the formula having gone wrong."""
    from autoresearch.entries import Store
    store = Store(sandbox.paths.entries)
    sandbox.budgets["iteration_fanout"] = 3
    for i in range(4):
        make_entry(store, f"Q1{i}", confidence=0.85, impact=0.02, cost=1.0)
    make_entry(store, "Q90", confidence=0.10, impact=0.40, cost=8.0)
    seen = {}
    def judge(brief):
        seen.update(json.loads(brief))
        return []
    brain = ScriptedBrain({
        Role.GENERATOR: lambda b: [], Role.JUDGE: judge,
        Role.WORKER: lambda b: {"verdict": "inconclusive", "summary": "no decision",
                                "verification": dict(VERIFIED)},
        Role.CURATOR: lambda b: {"reprice": [], "notes": []},
        Role.QC: lambda b: {"problems": [], "harness_debt": [], "verdict": "clean"}})
    Coordinator(sandbox, brain).run_iteration(1)
    assert seen["explore_reserve"] == ["Q90"]


def test_the_loop_honours_a_domain_that_disables_the_reserve(sandbox):
    """A converged domain may want every slot on the score. The coordinator
    reads the domain's appetite; it does not carry its own."""
    from autoresearch.entries import Store
    store = Store(sandbox.paths.entries)
    sandbox.explore_fraction = 0.0
    sandbox.budgets["iteration_fanout"] = 3
    for i in range(4):
        make_entry(store, f"Q1{i}", confidence=0.85, impact=0.02, cost=1.0)
    make_entry(store, "Q90", confidence=0.10, impact=0.40, cost=8.0)
    brain = ScriptedBrain({
        Role.GENERATOR: lambda b: [], Role.JUDGE: lambda b: [],
        Role.WORKER: lambda b: {"verdict": "inconclusive", "summary": "no decision",
                                "verification": dict(VERIFIED)},
        Role.CURATOR: lambda b: {"reprice": [], "notes": []},
        Role.QC: lambda b: {"problems": [], "harness_debt": [], "verdict": "clean"}})
    it = Coordinator(sandbox, brain).run_iteration(1)
    assert "Q90" not in it.shortlist, it.shortlist


# -- distil: closed work becomes knowledge ---------------------------------

def _librarian(write=(), retire=()):
    handlers = dict(IDLE)
    handlers[Role.LIBRARIAN] = lambda b: {"write": list(write),
                                          "retire": list(retire), "notes": []}
    return ScriptedBrain(handlers)


def test_distil_does_not_run_off_cadence_but_says_so(sandbox):
    """H98 again: a phase the cadence skipped and a phase that did not run are
    different facts, and only one of them is fine."""
    sandbox.distil_every = 5
    it = Coordinator(sandbox, ScriptedBrain(dict(IDLE))).run_iteration(1)
    phase = _phase(it, "distil")
    assert phase.did == 0
    assert any("cadence is every 5" in d and "next at 5" in d for d in phase.detail)


def test_distil_off_is_a_decision_and_is_recorded(sandbox):
    sandbox.distil_every = 0
    it = Coordinator(sandbox, ScriptedBrain(dict(IDLE))).run_iteration(1)
    assert any("turned distillation off" in d
               for d in _phase(it, "distil").detail)


def test_distil_writes_a_skill_the_next_brief_carries(sandbox, store):
    """The loop's own output becomes the next agent's index -- which is the
    whole reason the phase exists."""
    from conftest import close, make_entry
    close(sandbox, store, make_entry(store, "Q1"))
    sandbox.distil_every = 1
    brain = _librarian(write=[{
        "name": "one-lesson",
        "description": "Use when a proposal shares Q1's mechanism.",
        "cites": ["Q1"],
        "body": "# It held\n\nMeasured and closed [Q1].\n"}])
    coordinator = Coordinator(sandbox, brain)
    it = coordinator.run_iteration(1)

    phase = _phase(it, "distil")
    assert phase.did == 1, phase.detail
    assert (sandbox.paths.root / "docs/skills/one-lesson/SKILL.md").exists()
    brief = json.loads(coordinator._brief(Role.GENERATOR, it))
    assert brief["skills"] == [{
        "name": "one-lesson",
        "description": "Use when a proposal shares Q1's mechanism.",
        "path": "docs/skills/one-lesson/SKILL.md",
        "cites": ["Q1"]}]
    assert "Measured and closed" not in json.dumps(brief), \
        "the brief carries the index, not the bodies"


def test_distil_refuses_a_skill_citing_an_open_entry_and_says_why(sandbox, store):
    from conftest import close, make_entry
    make_entry(store, "Q1")                       # queued, never closed
    close(sandbox, store, make_entry(store, "Q2"))   # gives the phase work to do
    sandbox.distil_every = 1
    brain = _librarian(write=[{
        "name": "premature", "description": "Use when it applies.",
        "cites": ["Q1"], "body": "# claim [Q1]\n"}])
    it = Coordinator(sandbox, brain).run_iteration(1)

    phase = _phase(it, "distil")
    assert phase.did == 0
    assert any("premature: refused" in d and "not terminal" in d
               for d in phase.detail), phase.detail
    assert not (sandbox.paths.root / "docs/skills/premature").exists()


def test_distil_retires_a_skill_and_records_the_reason(sandbox, store):
    from conftest import close, make_entry, make_skill
    close(sandbox, store, make_entry(store, "Q1"))
    close(sandbox, store, make_entry(store, "Q2"))   # undistilled: the phase runs
    make_skill(sandbox, "going", cites=["Q1"])
    sandbox.distil_every = 1
    it = Coordinator(sandbox, _librarian(retire=[
        {"name": "going", "why": "superseded"}])).run_iteration(1)
    assert any("retired going: superseded" in d
               for d in _phase(it, "distil").detail)
    assert not (sandbox.paths.root / "docs/skills/going").exists()


def test_the_librarian_comes_out_of_the_same_spawn_pool(sandbox, store):
    """Every model ask spends a spawn, or the meter that bounds an iteration
    undercounts -- the exact defect QC's ask was."""
    from conftest import close, make_entry
    close(sandbox, store, make_entry(store, "Q1"))
    sandbox.distil_every = 1
    sandbox.budgets["iteration_max_spawns"] = 4   # 2 generators, judge, curator
    it = Coordinator(sandbox, _librarian()).run_iteration(1)
    skipped = [d for d in _phase(it, "distil").detail if "librarian skipped" in d]
    assert skipped and "spawns exhausted" in skipped[0], _phase(it, "distil").detail


def test_qc_reports_a_skill_whose_evidence_moved(sandbox, store):
    """Mechanical first: the reopen is caught by code, in the same iteration,
    with no model asked about it."""
    from conftest import close, make_entry, make_skill
    entry = close(sandbox, store, make_entry(store, "Q1"))
    make_skill(sandbox, "was-true", cites=["Q1"])
    machine = sandbox.track_for("Q1").machine
    entry.apply(machine, machine.initial, "test", why="new evidence")
    store.save(entry)

    it = Coordinator(sandbox, ScriptedBrain(dict(IDLE))).run_iteration(1)
    assert any("was-true" in d and "result-archived" in d
               for d in _phase(it, "qc").detail), _phase(it, "qc").detail


def test_the_brief_says_which_skills_it_could_not_read(sandbox, store):
    """A role given a short index and no signal reads it as the whole of what is
    known -- the silent-drop failure `read_all` exists to prevent."""
    broken = sandbox.paths.root / "docs/skills/broken/SKILL.md"
    broken.parent.mkdir(parents=True)
    broken.write_text("# no frontmatter at all\n")
    coordinator = Coordinator(sandbox, ScriptedBrain(dict(IDLE)))
    brief = json.loads(coordinator._brief(Role.GENERATOR, Iteration(n=1)))
    assert brief["skills"] == []
    assert any("frontmatter" in p for p in brief["skills_unreadable"])


def test_an_out_of_band_distil_does_not_drag_the_yield_floor(sandbox):
    """Its record charges the money meter, but it is not a sample of what the
    queue yields -- counting it would stop a healthy loop."""
    from autoresearch.driver.loop import Iteration as It
    coordinator = Coordinator(sandbox, ScriptedBrain(dict(IDLE)))
    for n, kind in ((1, "iteration"), (2, "out-of-band")):
        it = It(n=n, kind=kind)
        it.verdicts = {"Q1": "confirmed"} if kind == "iteration" else {}
        coordinator._record(it)
    fresh = Coordinator(sandbox, ScriptedBrain(dict(IDLE)))
    counted = [i.confirmed for i in fresh.history if i.kind == "iteration"]
    assert counted == [1], "only real iterations are samples"


def test_force_overrides_distillation_being_turned_off(sandbox, store):
    """An explicitly invoked out-of-band command that silently does nothing is
    the failure shape this harness refuses everywhere else."""
    from conftest import close, make_entry
    close(sandbox, store, make_entry(store, "Q1"))
    sandbox.distil_every = 0
    coordinator = Coordinator(sandbox, _librarian(write=[{
        "name": "forced", "description": "Use when it applies.",
        "cites": ["Q1"], "body": "# claim [Q1]\n"}]))
    it = Iteration(n=1)
    from autoresearch import budget as budget_mod
    phase = coordinator.distil(it, budget_mod.iteration_budget(sandbox),
                               force=True)
    assert phase.did == 1, phase.detail
    assert (sandbox.paths.root / "docs/skills/forced/SKILL.md").exists()


def test_the_prompts_only_name_brief_keys_the_brief_carries(sandbox):
    """A prompt telling a role to read `skills_unreadable` when the brief calls
    it something else is a rule that silently does not exist. Cheap to check,
    and the two halves of the contract live in different files."""
    from autoresearch.driver.brain import Role, role_prompt
    brief = json.loads(Coordinator(sandbox, ScriptedBrain(dict(IDLE)))
                       ._brief(Role.GENERATOR, Iteration(n=1)))
    for role in (Role.GENERATOR, Role.WORKER):
        prompt = role_prompt(role)
        for key in ("skills", "skills_unreadable", "knowledge_paths"):
            if f"`{key}`" in prompt:
                assert key in brief, f"{role}.md names `{key}`, which no brief carries"
    assert "`skills`" in role_prompt(Role.GENERATOR), \
        "the generator must be told the index exists, or it reads every body"
    assert "`skills`" in role_prompt(Role.WORKER)
