"""The three guards against a result the record cannot back.

* `branch_stagnation` -- a lineage whose children all refute stops costing
  full dispatch price: excluded from ranking, refused new children at filing.
* `confirm_runs` -- a `confirmed` experiment closure must leave enough valid
  run rows in the ledger; one noisy or fabricated row closes nothing.
* faithfulness -- a closed experiment's typed summary must trace to the run
  rows it claims to summarise.

Every guard here ships with a negative test proving it refuses something
(invariant 3) and an off-switch test proving the default changes nothing.
"""
import pytest

from autoresearch import rank as rank_mod
from autoresearch import runs as runs_mod
from autoresearch.errors import AutoresearchError
from autoresearch.runs import RunRecord
from autoresearch.tree import check_branch
from conftest import close, make_entry


def row(config, entry_id, metrics, **over):
    """One valid run row attributed to an entry, in the ledger."""
    record = RunRecord(
        metrics=metrics, session=over.pop("session", "worker-1"),
        entry=entry_id, provenance={"host": "test"}, **over)
    runs_mod.append(config.paths.runs / f"{record.session}.jsonl", record)
    return record


def set_stagnation(sandbox, n):
    sandbox.branch_stagnation = n


# -- branch stagnation -------------------------------------------------------


def _stagnant_family(store, config, confirmed=False, kinds=("cell", "cell")):
    """A parent with two refuted children (and optionally one confirmed)."""
    close(config, store, make_entry(store, "Q1"), verdict="confirmed")
    for i, kind in enumerate(kinds):
        close(config, store, make_entry(store, f"Q2{i}", parent="Q1"),
              verdict="refuted", closure_kind=kind,
              reopen_condition="re-run at 10x the band")
    if confirmed:
        close(config, store, make_entry(store, "Q23", parent="Q1"),
              verdict="confirmed")


def test_a_stagnant_parents_children_are_excluded_from_ranking(sandbox, store):
    set_stagnation(sandbox, 2)
    _stagnant_family(store, sandbox)
    make_entry(store, "Q24", parent="Q1")
    ranking = rank_mod.rank(store.all(), sandbox)
    excluded = {s.entry_id: s.excluded for s in ranking.excluded}
    assert "parent Q1 stagnant: 2 refuted branch(es), none confirmed" in \
        excluded.get("Q24", "")
    assert ranking.scored == []


def test_one_confirmed_sibling_clears_the_parent(sandbox, store):
    """Self-healing by construction: stagnation is recomputed every rank, so a
    confirmed child lifts the exclusion without anyone clearing a flag."""
    set_stagnation(sandbox, 2)
    _stagnant_family(store, sandbox, confirmed=True)
    make_entry(store, "Q24", parent="Q1")
    ranking = rank_mod.rank(store.all(), sandbox)
    assert all(s.entry_id != "Q24" for s in ranking.excluded)
    assert any(s.entry_id == "Q24" for s in ranking.scored)


def test_blocked_siblings_do_not_stagnate_a_parent(sandbox, store):
    """A blocked or inconclusive sibling is not evidence against a direction."""
    set_stagnation(sandbox, 2)
    close(sandbox, store, make_entry(store, "Q1"), verdict="confirmed")
    close(sandbox, store, make_entry(store, "Q20", parent="Q1"),
          verdict="refuted", closure_kind="cell",
          reopen_condition="the band moving")
    # Q21 stays queued: one refuted branch, one unmeasured -- under threshold.
    make_entry(store, "Q21", parent="Q1")
    make_entry(store, "Q24", parent="Q1")
    ranking = rank_mod.rank(store.all(), sandbox)
    assert not any("stagnant" in s.excluded for s in ranking.excluded)
    assert any(s.entry_id == "Q24" for s in ranking.scored)


def test_mechanism_refutations_alone_do_not_stagnate(sandbox, store):
    """A `mechanism`-kind refutation already hard-excludes through tags; it
    must not ALSO read as stagnation evidence, or the two filters double-count
    in the explanation."""
    set_stagnation(sandbox, 2)
    close(sandbox, store, make_entry(store, "Q1"), verdict="confirmed")
    close(sandbox, store, make_entry(store, "Q20", parent="Q1"),
          verdict="refuted", closure_kind="mechanism",
          reopen_condition="a new mechanism")
    make_entry(store, "Q24", parent="Q1")
    ranking = rank_mod.rank(store.all(), sandbox)
    excluded = {s.entry_id: s.excluded for s in ranking.excluded}
    if "Q24" in excluded:
        assert "stagnant" not in excluded["Q24"]


def test_zero_stagnation_changes_nothing(sandbox, store):
    set_stagnation(sandbox, 0)
    _stagnant_family(store, sandbox)
    make_entry(store, "Q24", parent="Q1")
    ranking = rank_mod.rank(store.all(), sandbox)
    assert ranking.scored and not any(
        "stagnant" in s.excluded for s in ranking.excluded)


def test_filing_a_child_of_a_stagnant_parent_is_refused(sandbox, store):
    """The filing gate -- shared by the loop's generate phase and
    `ar entry new --parent` -- refuses the same parent rank excludes."""
    set_stagnation(sandbox, 2)
    _stagnant_family(store, sandbox)
    with pytest.raises(AutoresearchError, match="stagnant"):
        check_branch(sandbox, store, "Q1", "", "research")


def test_filing_a_child_of_a_healthy_parent_passes(sandbox, store):
    set_stagnation(sandbox, 2)
    _stagnant_family(store, sandbox, confirmed=True)
    check_branch(sandbox, store, "Q1", "", "research")   # must not raise


# -- confirm_runs ------------------------------------------------------------


def _confirm_rows(sandbox, n, entry="Q1"):
    for i in range(n):
        row(sandbox, entry, {"T": 3.0}, session=f"worker-{i}")


def test_closure_evidence_passes_with_enough_rows(sandbox):
    _confirm_rows(sandbox, 2)
    assert runs_mod.closure_evidence(
        runs_mod.read_all(sandbox.paths.runs), "Q1", 2) == []


def test_closure_evidence_names_the_shortfall(sandbox):
    _confirm_rows(sandbox, 1)
    problems = runs_mod.closure_evidence(
        runs_mod.read_all(sandbox.paths.runs), "Q1", 2)
    assert problems and "requires 2 valid run row(s)" in problems[0]
    assert "holds 1" in problems[0]


def test_failed_rows_do_not_count_as_confirmation(sandbox):
    row(sandbox, "Q1", {"T": 3.0}, session="a")
    bad = RunRecord(metrics={"T": 3.0}, session="b", entry="Q1", status="failed",
                    provenance={"host": "test"})
    runs_mod.append(sandbox.paths.runs / "b.jsonl", bad)
    assert runs_mod.closure_evidence(
        runs_mod.read_all(sandbox.paths.runs), "Q1", 2)


def test_a_confirmed_verdict_without_replication_is_refused(sandbox, store):
    """The CodeScientist shape: one run in the ledger closing a discovery.
    Refused, claim released, entry back in the queue -- the rows stay charged
    and the next worker adds rows. Rows here pre-exist from a prior claim, so
    the worker reports 0 new runs; the gate counts the ledger, not the reply."""
    from autoresearch.driver.brain import Role, ScriptedBrain
    from autoresearch.driver.loop import Coordinator
    from conftest import make_entry

    sandbox.confirm_runs = 2
    _confirm_rows(sandbox, 1)
    worker = {"verdict": "confirmed", "memo": "inbox/m.md",
              "summary": "measured T=3.0", "verification": {
                  "reread": True, "claims_checked": ["bar"]},
              "runs": 0, "gpu_hours": 0}
    handlers = dict(IDLE, **{Role.WORKER: lambda b: worker})
    sandbox.budgets["iteration_fanout"] = 1
    memo(sandbox)
    make_entry(store, "Q1")
    it = Coordinator(sandbox, ScriptedBrain(handlers)).run_iteration(1)
    assert it.verdicts.get("Q1") == "refused"
    reloaded = store.load("Q1")
    assert not sandbox.track_for("Q1").machine.status(reloaded.status).terminal


def test_a_confirmed_verdict_with_replication_closes(sandbox, store):
    from autoresearch.driver.brain import Role, ScriptedBrain
    from autoresearch.driver.loop import Coordinator
    from conftest import make_entry

    sandbox.confirm_runs = 2
    _confirm_rows(sandbox, 2)
    worker = {"verdict": "confirmed", "memo": "inbox/m.md",
              "summary": "measured T=3.0", "verification": {
                  "reread": True, "claims_checked": ["bar"]},
              "runs": 0, "gpu_hours": 0}
    handlers = dict(IDLE, **{Role.WORKER: lambda b: worker})
    sandbox.budgets["iteration_fanout"] = 1
    memo(sandbox)
    make_entry(store, "Q1")
    Coordinator(sandbox, ScriptedBrain(handlers)).run_iteration(1)
    assert store.load("Q1").status == "confirmed"


def test_refuted_verdicts_are_exempt_from_replication(sandbox, store):
    """Requiring re-runs to say 'no' doubles the price of the result that is
    most often already cheap to trust; the guard is against a fabricated win."""
    from autoresearch.driver.brain import Role, ScriptedBrain
    from autoresearch.driver.loop import Coordinator
    from conftest import make_entry

    sandbox.confirm_runs = 2          # no rows at all in the ledger
    worker = {"verdict": "refuted", "memo": "inbox/m.md",
              "closure_kind": "cell", "reopen_condition": "the band moving",
              "summary": "no effect", "verification": {
                  "reread": True, "claims_checked": ["bar"]},
              "runs": 0, "gpu_hours": 0}
    handlers = dict(IDLE, **{Role.WORKER: lambda b: worker})
    sandbox.budgets["iteration_fanout"] = 1
    memo(sandbox)
    make_entry(store, "Q1")
    Coordinator(sandbox, ScriptedBrain(handlers)).run_iteration(1)
    assert store.load("Q1").status == "refuted"


def test_ar_close_applies_the_same_replication_gate(sandbox, store, capsys):
    """A guard honoured only on the loop path is vacuous: the manual close
    path refuses identically (cli.main reports AutoresearchError as rc 2)."""
    from autoresearch import cli
    from conftest import make_entry

    sandbox.confirm_runs = 2
    # cmd_close loads its own config from disk: the gate must be a real config
    # value, not a test-only attribute mutation.
    toml = sandbox.paths.root / "domain.toml"
    toml.write_text(toml.read_text().replace(
        "[coordinator]", "[coordinator]\nconfirm_runs = 2"))
    _confirm_rows(sandbox, 1)
    memo(sandbox)
    entry = make_entry(store, "Q1")
    # `close` moves in-progress -> confirmed; the claim is the legitimate path.
    machine = sandbox.track_for("Q1").machine
    entry.apply(machine, "in-progress", "test", why="claiming")
    store.save(entry)
    rc = cli.main(["--domain", str(sandbox.paths.root), "close", "Q1",
                   "confirmed", "--memo", "inbox/m.md",
                   "--summary", "it held"])
    assert rc == 2
    assert "requires 2 valid run row" in capsys.readouterr().err
    assert store.load("Q1").status == "in-progress"


# -- faithfulness ------------------------------------------------------------


def test_a_summary_number_matching_nothing_is_named(sandbox, store):
    close(sandbox, store, make_entry(store, "Q1"),
          summary="T measured at 0.417, decisive")
    _confirm_rows(sandbox, 1)             # row says T=3.0, not 0.417
    problems = runs_mod.faithfulness_problems(
        runs_mod.read_all(sandbox.paths.runs), store.load("Q1"), sandbox.goal)
    assert problems and "0.417" in problems[0]


def test_a_summary_tracing_to_the_ledger_is_clean(sandbox, store):
    row(sandbox, "Q1", {"T": 3.0}, session="a")
    row(sandbox, "Q1", {"T": 3.5}, session="b")
    close(sandbox, store, make_entry(store, "Q1"),
          summary="T moved from 3.0 to 3.5, a 0.5 delta over 2 rows")
    assert runs_mod.faithfulness_problems(
        runs_mod.read_all(sandbox.paths.runs), store.load("Q1"),
        sandbox.goal) == []


def test_a_verdict_with_no_rows_at_all_is_named(sandbox, store):
    close(sandbox, store, make_entry(store, "Q1"), summary="held")
    problems = runs_mod.faithfulness_problems([], store.load("Q1"),
                                              sandbox.goal)
    assert problems and "no valid run row" in problems[0]


def test_refuted_and_non_experiment_closures_are_not_checked(sandbox, store):
    """A legitimate refutation may hold only invalid/failed rows -- a
    configuration that measured but failed its validity gates still decided
    the bar -- so demanding ok rows for a `no` would cry wolf. Refuted and
    non-experiment closures are left to the QC model."""
    close(sandbox, store, make_entry(store, "Q1"), verdict="refuted",
          closure_kind="cell", reopen_condition="the band moving",
          summary="a 0.123 number that matches nothing")
    assert runs_mod.faithfulness_problems([], store.load("Q1"),
                                          sandbox.goal) == []


def test_qc_reports_a_faithfulness_problem_mechanically(sandbox, store):
    """The check feeds the mechanical problems the QC role sees -- it must
    fire from the loop's qc phase, before any model is asked."""
    from autoresearch.driver.brain import Role, ScriptedBrain
    from autoresearch.driver.loop import Coordinator
    from conftest import make_entry

    close(sandbox, store, make_entry(store, "Q1"),
          summary="T measured at 0.417, decisive")
    _confirm_rows(sandbox, 1)
    handlers = dict(IDLE, **{Role.WORKER: lambda b: {
        "verdict": "inconclusive", "summary": "n/a",
        "verification": {"reread": True, "claims_checked": ["bar"]}}})
    sandbox.budgets["iteration_fanout"] = 1
    it = Coordinator(sandbox, ScriptedBrain(handlers)).run_iteration(1)
    qc = next(p for p in it.phases if p.name == "qc")
    assert any("0.417" in d for d in qc.detail)


def memo(config, name="inbox/m.md"):
    from conftest import memo as _memo
    return _memo(config, name)


IDLE = {
    "generator": lambda b: [],
    "judge": lambda b: [],
    "curator": lambda b: {"reprice": [], "notes": []},
    "qc": lambda b: {"problems": [], "harness_debt": [], "verdict": "clean"},
}


# -- evidence classes and run_detail anchors (pvfast-stwo-simd H10/H11) ------


def test_official_and_census_results_skip_the_ledger_checks(sandbox, store):
    """Evaluator-owned numbers and measurement-free censuses are legitimate
    evidence no local row can back; a strictness that permanently flags them
    is a permanently-red QC phase where real regressions hide (H10). The
    class is declared on the result, so the skip is visible, not silent."""
    close(sandbox, store, make_entry(store, "Q1"),
          summary="officially 1.6469x on the pinned host, decisive")
    entry = store.load("Q1")
    entry.set_evidence_class("official", who="test", why="evaluator-owned numbers")
    store.save(entry)
    assert runs_mod.faithfulness_problems([], store.load("Q1"), sandbox.goal) == []

    close(sandbox, store, make_entry(store, "Q2"),
          summary="census: 17 of 18 sites match, a 0.123 loose end")
    entry = store.load("Q2")
    entry.set_evidence_class("census", who="test", why="differential census")
    store.save(entry)
    assert runs_mod.faithfulness_problems([], store.load("Q2"), sandbox.goal) == []


def test_the_ledger_default_keeps_the_fabrication_net(sandbox, store):
    """The off-switch test: without a declared class the strictness is
    unchanged -- a summary number matching nothing is still named."""
    close(sandbox, store, make_entry(store, "Q1"),
          summary="T measured at 0.417, decisive")
    _confirm_rows(sandbox, 1)
    entry = store.load("Q1")
    assert entry.result.evidence_class == "ledger"
    problems = runs_mod.faithfulness_problems(
        runs_mod.read_all(sandbox.paths.runs), entry, sandbox.goal)
    assert problems and "0.417" in problems[0]


def test_settled_run_detail_anchors_waive_the_no_row_problem(sandbox, store):
    """A scratch bench retains its evidence as run_detail legs on the settled
    external report, not as ledger rows (H11): the no-row problem must be
    waived by retained legs, and summary numbers must trace to the figures
    those legs retain."""
    close(sandbox, store, make_entry(store, "Q1"),
          summary="pack stage 5.49x faster, end to end 45.3%")
    assert runs_mod.faithfulness_problems(
        [], store.load("Q1"), sandbox.goal,
        detail_anchors=[5.49, 45.3]) == []


def test_a_summary_number_outside_the_run_detail_anchors_is_named(sandbox, store):
    """The anchors are a second evidence pool, not an amnesty: a figure the
    settled legs nowhere retain is still named."""
    close(sandbox, store, make_entry(store, "Q1"),
          summary="pack stage 7.77x faster")
    problems = runs_mod.faithfulness_problems(
        [], store.load("Q1"), sandbox.goal, detail_anchors=[5.49])
    assert problems and "7.77" in problems[0]
    assert "run_detail" in problems[0]


def test_no_rows_and_no_legs_is_still_named(sandbox, store):
    close(sandbox, store, make_entry(store, "Q1"), summary="held")
    problems = runs_mod.faithfulness_problems([], store.load("Q1"), sandbox.goal)
    assert problems and "no valid run row" in problems[0]
    assert "run_detail" in problems[0]


def test_detail_anchors_read_from_settled_external_reports(sandbox):
    """Anchors come from settled external completions only: an assigned row's
    prepared report is not evidence yet, and legs without a retained result
    string contribute nothing. Only the leg's `result` field is parsed."""
    import json
    import uuid

    from autoresearch import attempts as attempts_mod

    def record(status, entry="Q1", legs=None):
        d = attempts_mod.directory(sandbox) / uuid.uuid4().hex
        d.mkdir(parents=True)
        report = {"run_detail": legs} if legs is not None else {}
        (d / "record.json").write_text(json.dumps(
            {"schema": "ar-attempt-1", "id": d.name, "kind": "external",
             "status": status, "entry": entry, "session": "s",
             "report": report}))

    record("completed", legs=[
        {"run": "pack", "what": "tiled pack at 2^20, 7/7 cycles",
         "result": "5.49x vs pristine, byte-identical roots"},
        {"run": "identity", "result": "no number retained"}])
    record("assigned", legs=[{"result": "9.99x prepared, not settled"}])
    record("completed", entry="Q2", legs=[{"result": "0.0498 s baseline"}])

    assert attempts_mod.detail_anchors_by_entry(sandbox) == {
        "Q1": [5.49], "Q2": [0.0498]}
