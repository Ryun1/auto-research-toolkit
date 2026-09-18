"""Targeted research scouts: agents that answer one question and file their
ideas back into the record, where ranking prices them and the loop can claim
them.

The invariants under test: ideas land as ordinary entries through the same
path a generator's do; duplicates are refused (H60); a scout that raised is
attributed rather than silently dropped; spawn budget gates the fanout; the
record says which backend each scout ran on.
"""
import json

from autoresearch.driver.brain import Role, ScriptedBrain
from autoresearch.driver.loop import Coordinator, Iteration
from conftest import make_entry

PROPOSAL = {
    "title": "a scout-only idea",
    "hypothesis": "the question opens a mechanism the board has not tried",
    "prediction": "measuring it moves the objective",
    "bar": "at least 3% on the objective",
    "confidence": 0.4, "impact": 0.05, "cost": 2.0,
    "mechanisms": ["scout"],
    "sources": ["https://example.test/paper"],
    "why_filed": "the question opened this",
}

def _qc(b):
    return {"problems": [], "harness_debt": [], "verdict": "clean"}


IDLE = {role: (lambda b: []) for role in Role.ALL}
IDLE[Role.QC] = _qc


def coordinator_with(sandbox, scout_handler):
    return Coordinator(sandbox, ScriptedBrain({**IDLE, Role.SCOUT: scout_handler}))


def run_research(sandbox, handler, count=1):
    coordinator = coordinator_with(sandbox, handler)
    from autoresearch import budget as budget_mod
    it = Iteration(n=coordinator._last_recorded_n() + 1, kind="out-of-band")
    it.target = coordinator._target()
    phase = coordinator.research(it, budget_mod.iteration_budget(sandbox),
                                 question="does caching beat unrolling", count=count)
    return it, phase


def test_ideas_land_as_ordinary_entries(sandbox, store):
    it, phase = run_research(sandbox, lambda b: [dict(PROPOSAL)])
    entry = store.load(store.all()[0].id)
    assert entry.title == PROPOSAL["title"]
    assert entry.hypothesis == PROPOSAL["hypothesis"]
    assert entry.sources == PROPOSAL["sources"]
    assert entry.status == sandbox.track_for(entry.id).machine.initial
    assert phase.did == 1
    assert it.cost_usd == 0.0, "a scripted scout is free and the record says so"


def test_a_scout_that_raises_is_attributed_and_the_others_still_file(
        sandbox, store):
    """A failure filtered from the replies reads as silence; research must not."""
    import threading
    outcomes = [RuntimeError("the scout's backend fell over"), [dict(PROPOSAL)]]
    lock = threading.Lock()

    def handler(brief):
        with lock:
            outcome = outcomes.pop()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    it, phase = run_research(sandbox, handler, count=2)
    assert len(store.all()) == 1, "the healthy scout's ideas survive"
    assert any("raised" in line for line in phase.detail)
    assert any("1 idea(s), 1 filed" in line for line in phase.detail)


def test_duplicates_are_refused_against_the_board_and_each_other(sandbox, store):
    """H60: two agents proposing one idea must not file it twice."""
    make_entry(store, "Q1", title="A Board Idea")
    proposals = [dict(PROPOSAL, title="A Board Idea"),
                 dict(PROPOSAL, title="a board idea "),
                 dict(PROPOSAL, title="Scout Two's Idea")]
    it, phase = run_research(sandbox, lambda b: proposals, count=2)
    titles = {e.title for e in store.all()}
    assert titles == {"A Board Idea", "Scout Two's Idea"}
    assert phase.did == 1


def test_proposals_without_a_title_or_shape_are_dropped(sandbox, store):
    it, phase = run_research(
        sandbox, lambda b: ["not a dict", {"hypothesis": "no title"},
                            dict(PROPOSAL)])
    assert len(store.all()) == 1
    assert phase.did == 1


def test_the_record_names_the_backend_each_scout_ran_on(sandbox):
    it, phase = run_research(sandbox, lambda b: [dict(PROPOSAL)])
    assert any("[scripted]" in line for line in phase.detail)


def test_spawn_budget_gates_the_fanout(sandbox, store):
    """Scouts spend the same spawn meter generators do: a domain cannot be
    flooded with researchers any more than with generators."""
    sandbox.budgets["iteration_max_spawns"] = 1
    it, phase = run_research(sandbox, lambda b: [dict(PROPOSAL)], count=3)
    assert phase.read == 1, "one spawn left, so one scout runs"
    assert any("spawns" in line for line in phase.detail)
    assert len(store.all()) == 1


def test_zero_scouts_run_zero_scouts(sandbox, store):
    """`--count 0` means none: a phase that did not run is a different fact
    from a phase that ran and filed nothing (H98)."""
    it, phase = run_research(sandbox, lambda b: [dict(PROPOSAL)], count=0)
    assert phase.read == 0 and phase.did == 0
    assert not store.all()
    assert not phase.error


def test_an_empty_answer_is_recorded_not_treated_as_failure(sandbox, store):
    it, phase = run_research(sandbox, lambda b: [])
    assert phase.did == 0
    assert any("0 idea(s), 0 filed" in line for line in phase.detail)
    assert not phase.error


# -- the out-of-band entry point --------------------------------------------

def test_cli_research_records_an_out_of_band_iteration(sandbox, store,
                                                       monkeypatch, capsys):
    """`ar research` goes through the same brain factory as the loop, files
    the ideas, and writes an out-of-band record charging what was spent."""
    import autoresearch.cli as cli
    from autoresearch.driver import brain as brain_mod

    def fake_build(config, max_budget_usd=None, allow_paid=None):
        scouts = [lambda b: [dict(PROPOSAL)],
                  lambda b: [dict(PROPOSAL, title="a second scout's idea")]]
        return ScriptedBrain({**IDLE, Role.SCOUT: lambda b: scouts.pop()(b)})

    monkeypatch.setattr(brain_mod, "build_brain", fake_build)
    rc = cli.main(["--domain", str(sandbox.paths.root),
                   "research", "does caching beat unrolling", "--count", "2"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "filed 2" in out
    assert "out-of-band" in out
    assert len(store.all()) == 2
    records = sorted((sandbox.paths.iterations).glob("*.json"))
    assert len(records) == 1
    record = json.loads(records[0].read_text())
    assert record["kind"] == "out-of-band"
    assert any(p["name"] == "research" for p in record["phases"])


def test_cli_research_prints_unknown_for_an_unmetered_backend(sandbox, store,
                                                             monkeypatch,
                                                             capsys):
    """A brain that writes no cost file is usage the ceiling cannot see.
    Printing `$0.00` would say it was free; the summary must say unknown."""
    import autoresearch.cli as cli
    from autoresearch.driver import brain as brain_mod

    class Unmetered:
        def ask(self, role, brief, *, workspace=None, max_turns=None):
            from autoresearch.driver.brain import Reply
            return Reply(role=role, data=[dict(PROPOSAL)], cost_usd=None,
                         backend="unmetered")

    monkeypatch.setattr(brain_mod, "build_brain",
                        lambda config, max_budget_usd=None, allow_paid=None:
                        Unmetered())
    rc = cli.main(["--domain", str(sandbox.paths.root),
                   "research", "any question"])
    assert rc == 0
    assert "cost: unknown (unmetered backend usage)" in capsys.readouterr().out


def test_the_record_is_written_even_when_the_phase_raises(sandbox, monkeypatch):
    """A scout ask that spends and is not recorded is spend the next loop
    cannot see -- a ceiling that hides an overrun rather than refusing it."""

    import autoresearch.cli as cli
    from autoresearch.driver import brain as brain_mod
    from autoresearch.driver import loop as loop_mod
    from autoresearch.errors import AutoresearchError

    monkeypatch.setattr(brain_mod, "build_brain",
                        lambda config, max_budget_usd=None, allow_paid=None:
                        ScriptedBrain(IDLE))

    def explode(self, it, budget, question, count=1):
        raise AutoresearchError("the scout backend vanished mid-ask")

    monkeypatch.setattr(loop_mod.Coordinator, "research", explode)
    # main translates AutoresearchError into exit code 2, but the finally in
    # cmd_research must still have recorded the spend.
    assert cli.main(["--domain", str(sandbox.paths.root), "research",
                     "anything"]) == 2
    records = sorted((sandbox.paths.iterations).glob("*.json"))
    assert len(records) == 1
