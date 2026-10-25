"""#23: a follow-up past the depth cap keeps its lineage.

`ar entry new --parent Q656` was refused by `tree_max_depth = 3`, so both
follow-ups were filed as roots citing Q656 only in prose -- and ranking and
the graph read them as novel branches. The cap still governs branch filing;
`--derived-from` records the lineage beside it, without counting toward
depth: the graph draws it and ranking reads the entry as not novel.
"""
from autoresearch import cli, rank, render
from conftest import make_entry


def _ar(sandbox, *args):
    return cli.main(["--domain", str(sandbox.paths.root), *args])


def _deep_chain(store):
    make_entry(store, "Q1")
    make_entry(store, "Q2", parent="Q1", kind="improve")
    make_entry(store, "Q3", parent="Q2", kind="improve")
    make_entry(store, "Q4", parent="Q3", kind="improve")   # at the cap of 3 below


def test_the_cap_still_refuses_a_deep_branch_and_names_the_way_out(sandbox, store, capsys):
    _deep_chain(store)
    assert _ar(sandbox, "entry", "new", "deeper", "--track", "research",
               "--parent", "Q4", "--kind", "improve", "--why", "x") == 2
    assert "--derived-from" in capsys.readouterr().err


def test_derived_from_files_past_the_cap_and_records_the_lineage(sandbox, store):
    _deep_chain(store)
    assert _ar(sandbox, "entry", "new", "deeper", "--track", "research",
               "--derived-from", "Q4", "--why", "x") == 0
    entry = store.load("Q5")
    assert entry.derived_from == "Q4" and entry.parent == ""


def test_a_derived_entry_is_not_novel(sandbox, store):
    _deep_chain(store)
    make_entry(store, "Q5", derived_from="Q4")
    make_entry(store, "Q6")
    ranking = rank.rank(store.all(), sandbox, budget_ok=lambda e: True)
    novel = {c.entry_id: c.novel for c in ranking.scored + ranking.excluded}
    assert novel["Q5"] is False and novel["Q6"] is True


def test_the_graph_draws_the_derivation(sandbox, store):
    _deep_chain(store)
    make_entry(store, "Q5", derived_from="Q4")
    view = render.graph_view(sandbox.tracks["research"], store.all())
    assert 'Q4 -.->|"derived"| Q5' in view


def test_derived_from_must_name_an_entry_on_the_same_track(sandbox, store, capsys):
    make_entry(store, "H1", track="harness")
    assert _ar(sandbox, "entry", "new", "x", "--track", "research",
               "--derived-from", "Q9", "--why", "x") == 2
    assert "no such entry" in capsys.readouterr().err
    assert _ar(sandbox, "entry", "new", "x", "--track", "research",
               "--derived-from", "H1", "--why", "x") == 2
    assert "track" in capsys.readouterr().err


def test_parent_and_derived_from_are_one_or_the_other(sandbox, store, capsys):
    make_entry(store, "Q1")
    assert _ar(sandbox, "entry", "new", "x", "--track", "research", "--parent",
               "Q1", "--derived-from", "Q1", "--why", "x") == 2
    assert "one or the other" in capsys.readouterr().err


def test_a_loop_proposal_may_be_derived(sandbox, store):
    from autoresearch.driver.brain import Role, ScriptedBrain
    from autoresearch.driver.loop import Coordinator
    from test_loop import IDLE
    _deep_chain(store)
    proposal = {"title": "deeper still", "hypothesis": "h", "prediction": "p",
                "bar": "b", "confidence": 0.4, "impact": 0.05, "cost": 2.0,
                "mechanisms": ["m"], "why_filed": "w", "derived_from": "Q4"}
    Coordinator(sandbox, ScriptedBrain(
        {**IDLE, Role.GENERATOR: {"proposals": [proposal]}})).run(max_iterations=1)
    filed = [e for e in store.all() if e.title == "deeper still"]
    assert filed and filed[0].derived_from == "Q4"
