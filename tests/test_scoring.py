"""The domain score seam (`[commands] score`) and the tree the record carries.

The seam is the ranking judgement leaving core: the domain prices the
claimable branches, the core keeps the hard filters, the risk partition and
the shortlist arithmetic. A broken seam must refuse the iteration, not fall
back to the formula the domain replaced.
"""
import json
import shlex
import sys

import pytest

from autoresearch.driver.brain import Role, ScriptedBrain
from autoresearch.driver.loop import Coordinator
from autoresearch.errors import AutoresearchError
from autoresearch.scoring import domain_scores
from conftest import make_entry

IDLE = {Role.GENERATOR: lambda b: [],
        Role.JUDGE: lambda b: [],
        Role.WORKER: lambda b: {"verdict": "inconclusive", "summary": "n/a",
                                "verification": {"reread": True,
                                                 "claims_checked": ["bar"]}},
        Role.CURATOR: lambda b: {"reprice": [], "notes": []},
        Role.QC: lambda b: {"problems": [], "harness_debt": [],
                            "verdict": "clean"}}


def scorer(config, body):
    path = config.paths.root / "domain score.py"
    path.write_text(body)
    config.commands["score"] = shlex.join([sys.executable, path.name])
    return config.commands["score"]


def echo_scorer(config, scores):
    """A scorer that ignores stdin and prints fixed rows."""
    scorer(config, "import json\nprint(json.dumps({'scores': "
                  + json.dumps(scores) + "}))\n")


def test_the_seam_prices_the_claimable_candidates(sandbox, store):
    make_entry(store, "Q1", confidence=0.5, impact=0.4, cost=2.0)
    make_entry(store, "Q2", parent="Q1", confidence=0.5, impact=0.4, cost=2.0)
    echo_scorer(sandbox, [
        {"id": "Q1", "score": 0.25, "reason": "novel root"},
        {"id": "Q2", "score": 0.75, "reason": "branch off confirmed Q1"}])
    entries = store.all()
    rows = domain_scores(sandbox, entries, risk=0.5)
    assert [r["id"] for r in rows] == ["Q1", "Q2"]
    assert rows[1]["score"] == 0.75
    assert rows[1]["reason"] == "branch off confirmed Q1"


@pytest.mark.parametrize("scores", [
    [{"id": "Q1", "score": 1.0}],                       # a row missing
    [{"id": "Q1", "score": 1.0, "reason": ""},
     {"id": "Q9", "score": 1.0, "reason": ""}],          # an unranked id
    [{"id": "Q1", "score": "high", "reason": ""}],      # not a number
    [{"id": "Q1", "score": None, "reason": ""}],        # null
])
def test_a_seam_breaking_the_row_contract_is_refused(sandbox, store, scores):
    make_entry(store, "Q1")
    make_entry(store, "Q2", parent="Q1")
    echo_scorer(sandbox, scores)
    with pytest.raises(AutoresearchError):
        domain_scores(sandbox, store.all(), risk=0.5)


def test_a_failing_seam_command_is_refused(sandbox, store):
    make_entry(store, "Q1")
    scorer(sandbox, "import sys\nsys.exit(3)\n")
    with pytest.raises(AutoresearchError, match="exited 3"):
        domain_scores(sandbox, store.all(), risk=0.5)


def test_non_json_seam_output_is_refused(sandbox, store):
    make_entry(store, "Q1")
    scorer(sandbox, "print('the branch looks promising')\n")
    with pytest.raises(AutoresearchError, match="not JSON"):
        domain_scores(sandbox, store.all(), risk=0.5)


def test_the_seam_never_sees_an_excluded_entry(sandbox, store, tmp_path):
    """Hard filters run before the seam: a terminal entry never reaches the
    scorer, so no domain price can resurrect it."""
    from autoresearch import rank as rank_mod
    from autoresearch import scoring
    from conftest import close
    close(sandbox, store, make_entry(store, "Q1"), verdict="confirmed")
    make_entry(store, "Q2")
    seen = tmp_path / "seen.json"
    scorer(sandbox, (
        "import json, sys\n"
        "payload = json.load(sys.stdin)\n"
        f"open({str(seen)!r}, 'w').write(json.dumps(payload))\n"
        "ids = [e['id'] for e in payload['entries']]\n"
        "print(json.dumps({'scores': [{'id': i, 'score': 1.0, 'reason': ''}"
        " for i in ids]}))\n"))
    entries = store.all()
    ranking = rank_mod.rank(entries, sandbox)
    assert [s.entry_id for s in ranking.scored] == ["Q2"]
    scoring.seam_ranking(sandbox, ranking, entries, risk=0.5)
    payload = json.loads(seen.read_text())
    assert [e["id"] for e in payload["entries"]] == ["Q2"]


def test_the_loop_dispatches_on_the_domain_prices(sandbox, store):
    """The seam reorders the shortlist: with the formula, Q10 (a cheap certain
    increment) outranks Q90 (an honest long shot); with the toy scorer, Q90's
    base is higher, so the dispatch order must flip."""
    make_entry(store, "Q10", confidence=0.85, impact=0.02, cost=1.0)
    make_entry(store, "Q90", confidence=0.10, impact=0.40, cost=8.0)
    it = Coordinator(sandbox, ScriptedBrain(dict(IDLE))).run_iteration(1)
    assert it.shortlist == ["Q90", "Q10"], it.shortlist
    assert any("scored by domain seam: bin/score"
               for d in next(p for p in it.phases if p.name == "rank").detail)


def test_a_seam_failure_skips_dispatch_and_records_why(sandbox, store):
    """No silent fallback: the formula the domain replaced must not quietly
    take over when the domain's scorer breaks."""
    make_entry(store, "Q1")
    scorer(sandbox, "import sys\nsys.exit(3)\n")
    it = Coordinator(sandbox, ScriptedBrain(dict(IDLE))).run_iteration(1)
    rank = next(p for p in it.phases if p.name == "rank")
    assert any("score seam failed" in d for d in rank.detail)
    assert it.shortlist == []
    assert it.verdicts == {}


# -- the tree the record carries --------------------------------------------


def generate_proposals(sandbox, store, proposals):
    handlers = dict(IDLE)
    handlers[Role.GENERATOR] = lambda b: proposals
    sandbox.budgets["iteration_fanout"] = 0      # no dispatch; generate only
    return Coordinator(sandbox, ScriptedBrain(handlers)).run_iteration(1)


def _branch_proposal(title, parent):
    """A mechanically valid branch proposal: the tree fields ride on top of
    the required scoring inputs."""
    return {"title": title, "hypothesis": "h", "prediction": "p",
            "bar": "confirmed if x; refuted if y", "confidence": 0.5,
            "impact": 0.1, "cost": 1, "mechanisms": ["m"], "parent": parent}


def test_a_generator_may_file_a_branch(sandbox, store):
    make_entry(store, "Q1")
    generate_proposals(sandbox, store, [_branch_proposal("a narrow re-run of Q1", "Q1")])
    child = next(e for e in store.all() if e.id != "Q1")
    assert child.parent == "Q1"


def test_an_unknown_parent_is_refused_at_filing(sandbox, store):
    it = generate_proposals(sandbox, store, [_branch_proposal("a branch of nothing", "Q99")])
    generate = next(p for p in it.phases if p.name == "generate")
    assert any("no such entry" in d for d in generate.detail)
    assert all(e.parent == "" for e in store.all())


def test_a_branch_past_the_depth_cap_is_refused(sandbox, store):
    """`tree_max_depth` counts lineage levels: Q2 is depth 1, so a cap of 1
    refuses a child of Q2 (depth 2) at filing."""
    make_entry(store, "Q1")
    make_entry(store, "Q2", parent="Q1")
    sandbox.tree_max_depth = 1
    it = generate_proposals(sandbox, store, [_branch_proposal("one too deep", "Q2")])
    generate = next(p for p in it.phases if p.name == "generate")
    assert any("tree_max_depth" in d for d in generate.detail)
    assert sorted(e.id for e in store.all()) == ["Q1", "Q2"]


def test_too_many_siblings_are_refused(sandbox, store):
    make_entry(store, "Q1")
    make_entry(store, "Q2", parent="Q1")
    make_entry(store, "Q3", parent="Q1")
    sandbox.tree_max_children = 2
    it = generate_proposals(sandbox, store, [_branch_proposal("one sibling too many", "Q1")])
    generate = next(p for p in it.phases if p.name == "generate")
    assert any("tree_max_children" in d for d in generate.detail)
    assert sorted(e.id for e in store.all()) == ["Q1", "Q2", "Q3"]


def test_a_branch_cannot_cross_tracks(sandbox, store):
    make_entry(store, "Q1")
    proposal = _branch_proposal("a defect branching off research", "Q1")
    proposal["track"] = "harness"
    it = generate_proposals(sandbox, store, [proposal])
    generate = next(p for p in it.phases if p.name == "generate")
    assert any("stays on its parent's track" in d for d in generate.detail)
    assert all(e.parent == "" for e in store.all())


# -- branch intent and scoped memory (AIRA arXiv 2507.02554 §4.1) ------------


def test_a_generator_may_name_a_branch_intent(sandbox, store):
    make_entry(store, "Q1")
    proposal = _branch_proposal("a repair of Q1", "Q1")
    proposal["kind"] = "debug"
    generate_proposals(sandbox, store, [proposal])
    child = next(e for e in store.all() if e.id != "Q1")
    assert child.kind == "debug"


def test_an_unknown_kind_is_refused_at_filing(sandbox, store):
    make_entry(store, "Q1")
    proposal = _branch_proposal("a mutation", "Q1")
    proposal["kind"] = "mutate"
    it = generate_proposals(sandbox, store, [proposal])
    generate = next(p for p in it.phases if p.name == "generate")
    assert any("unknown branch kind" in d for d in generate.detail)
    assert sorted(e.id for e in store.all()) == ["Q1"]


def test_a_kind_without_a_parent_is_refused_at_filing(sandbox, store):
    proposal = _branch_proposal("a root with intent", "")
    proposal["kind"] = "improve"
    it = generate_proposals(sandbox, store, [proposal])
    generate = next(p for p in it.phases if p.name == "generate")
    assert any("novel root and carries no kind" in d for d in generate.detail)
    assert store.all() == []


def test_the_lineage_walks_root_first_entry_last(sandbox, store):
    from conftest import close
    make_entry(store, "Q1")
    close(sandbox, store, make_entry(store, "Q2", parent="Q1"))
    Q3 = make_entry(store, "Q3", parent="Q2", kind="debug",
                    status="in-progress")
    chain = Coordinator(sandbox, ScriptedBrain(dict(IDLE)))._lineage(Q3)
    assert [row["id"] for row in chain] == ["Q1", "Q2", "Q3"]
    assert chain[0]["verdict"] is None            # the root is still open
    assert chain[1]["verdict"] == "confirmed"     # the parent's verdict rides
    assert chain[2]["kind"] == "debug"


def test_branch_families_carry_sibling_verdicts_and_the_complexity_cue(
        sandbox, store):
    from conftest import close
    make_entry(store, "Q1")
    close(sandbox, store, make_entry(store, "Q2", parent="Q1"))
    close(sandbox, store, make_entry(store, "Q3", parent="Q1"),
          verdict="refuted", closure_kind="cell", reopen_condition="new cell")
    make_entry(store, "Q4", parent="Q1")
    make_entry(store, "Q9", parent="Q2")   # Q2 is terminal: not branchable
    families = Coordinator(sandbox, ScriptedBrain(dict(IDLE)))._branch_families(
        store.all())
    assert set(families) == {"Q1"}
    fam = families["Q1"]
    assert fam["children_count"] == 3
    assert fam["complexity"] == "moderate"          # 3 children: 2-4
    verdicts = {s["id"]: s["verdict"] for s in fam["siblings"]}
    assert verdicts == {"Q2": "confirmed", "Q3": "refuted", "Q4": None}
    # The AIRA cue, swept: 1 child is minimal, 5+ is advanced.
    make_entry(store, "Q5", parent="Q1")
    make_entry(store, "Q6", parent="Q1")
    make_entry(store, "Q7", parent="Q1")
    fam = Coordinator(sandbox, ScriptedBrain(dict(IDLE)))._branch_families(
        store.all())["Q1"]
    assert fam["complexity"] == "advanced"          # 6 children: >=5


def test_the_generator_brief_carries_the_families(sandbox, store):
    from conftest import close
    make_entry(store, "Q1")
    close(sandbox, store, make_entry(store, "Q2", parent="Q1"))
    seen = {}

    def generator(brief):
        seen.update(json.loads(brief))
        return []

    handlers = dict(IDLE)
    handlers[Role.GENERATOR] = generator
    sandbox.budgets["iteration_fanout"] = 0
    Coordinator(sandbox, ScriptedBrain(handlers)).run_iteration(1)
    fam = seen["branch_families"]["Q1"]
    assert fam["children_count"] == 1
    assert fam["siblings"][0]["verdict"] == "confirmed"


def test_the_worker_brief_carries_the_lineage(sandbox, store):
    """The wiring, not just the computation: a dispatched debug branch's
    worker brief names every prior attempt, root first, the entry last."""
    from types import SimpleNamespace

    from autoresearch.budget import iteration_budget
    from autoresearch.driver.brain import Reply
    from autoresearch.driver.loop import Iteration
    from autoresearch.workspaces import Pool
    from conftest import close

    make_entry(store, "Q1")
    make_entry(store, "Q2", parent="Q1")
    close(sandbox, store, make_entry(store, "Q3", parent="Q2"),
          verdict="refuted", closure_kind="cell", reopen_condition="moved")
    make_entry(store, "Q4", parent="Q3", kind="debug")
    captured = []

    class Brain:
        def ask(self, role, brief, *, workspace=None):
            assert role == Role.WORKER
            captured.append(json.loads(brief))
            return Reply(role=role, data={
                "verdict": "inconclusive", "runs": 0, "summary": "static",
                "verification": {"reread": True, "claims_checked": ["bar"]}})

    coordinator = Coordinator(sandbox, Brain())
    iteration = Iteration(n=1)
    with Pool(sandbox, "lineage-test") as pool:
        coordinator.dispatch(iteration, [SimpleNamespace(
            entry_id="Q4", terms={"cost": 1})],
            iteration_budget(sandbox), pool)
    chain = captured[0]["lineage"]
    assert [row["id"] for row in chain] == ["Q1", "Q2", "Q3", "Q4"]
    assert chain[2]["verdict"] == "refuted" and chain[2]["closure_kind"] == "cell"
    assert chain[3]["kind"] == "debug"


def test_a_branch_itself_is_a_branchable_family(sandbox, store):
    """The tree's depth exists so children have children: a claimable branch
    with its own children gets the same scoped sibling memory a root gets."""
    from conftest import close
    make_entry(store, "Q1")
    make_entry(store, "Q2", parent="Q1")
    close(sandbox, store, make_entry(store, "Q3", parent="Q2"),
          verdict="refuted", closure_kind="cell", reopen_condition="moved")
    make_entry(store, "Q4", parent="Q2")
    families = Coordinator(sandbox, ScriptedBrain(dict(IDLE)))._branch_families(
        store.all())
    assert set(families) == {"Q1", "Q2"}
    # Two children (one closed, one open) is the moderate band: the simple
    # exchange has been tried.
    assert families["Q2"]["complexity"] == "moderate"
    assert {s["id"]: s["verdict"] for s in families["Q2"]["siblings"]} \
        == {"Q3": "refuted", "Q4": None}


# -- the human filing path: `ar entry new --parent` --------------------------


def test_entry_new_files_a_branch_through_the_same_gate(sandbox, store, capsys):
    from autoresearch import cli
    make_entry(store, "Q1")
    assert cli.main(["--domain", str(sandbox.paths.root), "entry", "new",
                     "a human-filed branch", "--parent", "Q1",
                     "--kind", "debug", "--confidence", "0.5",
                     "--impact", "0.1", "--cost", "1",
                     "--mechanism", "m"]) == 0
    assert store.load("Q2").parent == "Q1"
    assert store.load("Q2").kind == "debug"


def test_entry_new_refuses_an_unknown_parent(sandbox, store, capsys):
    from autoresearch import cli
    assert cli.main(["--domain", str(sandbox.paths.root), "entry", "new",
                     "a branch of nothing", "--parent", "Q99",
                     "--confidence", "0.5", "--impact", "0.1",
                     "--cost", "1", "--mechanism", "m"]) == 2
    assert "no such entry" in capsys.readouterr().err
    assert store.all() == []
