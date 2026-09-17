"""Worker-owned records survive teardown without trusting reported measurements."""
import json
import subprocess
from types import SimpleNamespace

import pytest

from autoresearch.budget import iteration_budget
from autoresearch.driver.brain import Reply, Role
from autoresearch.driver.loop import Coordinator, Iteration
from autoresearch.runs import RunRecord, append, read_all
from autoresearch.workspaces import Pool
from conftest import make_entry

VERIFIED = {"reread": True, "claims_checked": ["audited result"]}


def record(**overrides):
    return RunRecord(**{
        "id": "worker-row", "metrics": {"ops": 30, "peak": 2},
        "session": "it1-Q1", "entry": "Q1", "started": 123,
        "provenance": {"source": "original-revision", "host": "local"},
        **overrides,
    })


def dispatch(sandbox, store, worker):
    make_entry(store, "Q1", impact=1.0)

    class Brain:
        def ask(self, role, brief, *, workspace=None):
            assert role == Role.WORKER
            return Reply(role=role, data=worker(workspace, json.loads(brief)))

    coordinator = Coordinator(sandbox, Brain())
    iteration = Iteration(n=1)
    with Pool(sandbox, "evidence-test") as pool:
        coordinator.dispatch(iteration, [SimpleNamespace(entry_id="Q1", terms={})],
                             iteration_budget(sandbox), pool)
    return coordinator, iteration


def report(**overrides):
    return {"verdict": "confirmed", "memo": "inbox/result.md", "runs": 1,
            "summary": "audited", "verification": VERIFIED, **overrides}


@pytest.mark.parametrize("git", [False, True], ids=["directory", "worktree"])
def test_new_records_outputs_and_memo_survive_cleanup(sandbox, store, git):
    # Non-default configured directory, inherited rows and appended rows in the
    # same file exercise the boundary that an ID-only post-scan misses.
    sandbox.paths.runs = sandbox.paths.root / "data/custom-runs"
    inherited = record(id="inherited")
    append(sandbox.paths.runs / "session.jsonl", inherited)
    if git:
        for args in (("init", "-q"), ("add", "."),
                     ("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                      "commit", "-qm", "fixture")):
            subprocess.run(["git", *args], cwd=sandbox.paths.root, check=True,
                           capture_output=True)
    produced = record(outputs=["data/artifacts/raw.txt"])
    slots = []

    def worker(workspace, brief):
        slots.append(workspace)
        (workspace / "data/artifacts").mkdir(parents=True, exist_ok=True)
        (workspace / "data/artifacts/raw.txt").write_text("raw audit output")
        (workspace / "inbox").mkdir(exist_ok=True)
        (workspace / "inbox/result.md").write_text("measured 30 ops, peak 2")
        append(workspace / "data/custom-runs/session.jsonl", produced)
        return report()

    coordinator, iteration = dispatch(sandbox, store, worker)
    assert not slots[0].exists()
    assert sorted(r.id for r in read_all(sandbox.paths.runs)) == ["inherited", "worker-row"]
    retained = next(r for r in read_all(sandbox.paths.runs) if r.id == produced.id)
    assert retained.to_dict() == produced.to_dict()
    assert (sandbox.paths.root / produced.outputs[0]).read_text() == "raw audit output"
    assert (sandbox.paths.root / "inbox/result.md").read_text() == "measured 30 ops, peak 2"
    assert store.load("Q1").status == "confirmed"
    assert iteration.run_ids == [produced.id]
    coordinator._record(iteration)
    restarted = Coordinator(sandbox, coordinator.brain)
    assert restarted.domain_budget["runs"].spent == coordinator.domain_budget["runs"].spent == 2
    assert restarted._best()[1].id in {produced.id, inherited.id}


def test_missing_measurements_refuse_closure_without_refunding_budget(sandbox, store):
    def worker(workspace, brief):
        (workspace / "inbox/result.md").write_text("claimed measurements without rows")
        return report(runs=3, gpu_hours=1.5)

    coordinator, iteration = dispatch(sandbox, store, worker)
    assert iteration.verdicts["Q1"] == "refused"
    assert store.load("Q1").status == "queued"
    assert store.load("Q1").claim is None
    assert read_all(sandbox.paths.runs) == []
    assert iteration.runs == coordinator.domain_budget["runs"].spent == 3
    assert iteration.gpu_hours == 1.5
    assert (sandbox.paths.workspaces / "it1-Q1/inbox/result.md").exists()
    assert any("missing measurement evidence" in line for line in iteration.phases[0].detail)


def test_zero_run_static_closure_needs_no_measurement_record(sandbox, store):
    def worker(workspace, brief):
        (workspace / "inbox/result.md").write_text("static proof of mechanism")
        return report(runs=0, verdict="refuted", closure_kind="mechanism")

    _, iteration = dispatch(sandbox, store, worker)
    assert iteration.verdicts["Q1"] == "refuted"
    assert read_all(sandbox.paths.runs) == []
    assert not (sandbox.paths.workspaces / "it1-Q1").exists()


@pytest.mark.parametrize("failure", ["id-collision", "output-collision", "malformed", "escape", "state-output"])
def test_unsafe_evidence_is_retained_not_published(sandbox, store, failure):
    original = record(id="existing")
    append(sandbox.paths.runs / "seed.jsonl", original)
    output = sandbox.paths.root / "data/artifacts/raw.txt"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("original artifact")

    def worker(workspace, brief):
        (workspace / "inbox/result.md").write_text("attempted closure")
        row = record()
        if failure == "id-collision":
            row.id = original.id
        elif failure == "output-collision":
            (workspace / "data/artifacts/raw.txt").write_text("different artifact")
            row.outputs = ["data/artifacts/raw.txt"]
        elif failure == "escape":
            row.outputs = ["../outside.txt"]
        elif failure == "state-output":
            row.outputs = ["state/entries/Q1.yaml"]
        if failure == "malformed":
            (workspace / "data/runs/broken.jsonl").write_text('{"schema":')
        else:
            append(workspace / "data/runs/new.jsonl", row)
        return report()

    _, iteration = dispatch(sandbox, store, worker)
    assert iteration.verdicts["Q1"] == "refused"
    assert [r.to_dict() for r in read_all(sandbox.paths.runs)] == [original.to_dict()]
    assert output.read_text() == "original artifact"
    assert store.load("Q1").status == "queued"
    assert (sandbox.paths.workspaces / "it1-Q1").exists()
    assert iteration.run_ids == []
    assert iteration.runs == 1


def test_worker_exception_retains_valid_rows_and_workspace(sandbox, store):
    produced = record(cost={"gpu_hours": 0.25})

    def worker(workspace, brief):
        append(workspace / "data/runs/new.jsonl", produced)
        (workspace / "inbox/unreported.md").write_text("unfinished audit")
        raise RuntimeError("worker crashed after writing its row")

    _, iteration = dispatch(sandbox, store, worker)
    assert iteration.verdicts["Q1"] == "failed"
    assert [r.to_dict() for r in read_all(sandbox.paths.runs)] == [produced.to_dict()]
    assert iteration.runs == 1
    assert iteration.gpu_hours == 0.25
    assert (sandbox.paths.workspaces / "it1-Q1/inbox/unreported.md").exists()
    assert store.load("Q1").claim is None


@pytest.mark.parametrize("change", ["reuse", "rewrite", "partial"])
def test_inherited_rows_cannot_support_new_measurement_claims(sandbox, store, change):
    original = record(id="inherited")
    append(sandbox.paths.runs / "seed.jsonl", original)

    def worker(workspace, brief):
        (workspace / "inbox/result.md").write_text("claimed fresh evidence")
        if change == "rewrite":
            (workspace / "data/runs/seed.jsonl").write_text("")
            append(workspace / "data/runs/seed.jsonl", record(id="rewritten"))
        elif change == "partial":
            append(workspace / "data/runs/new.jsonl", record())
        return report(runs=2 if change == "partial" else 1)

    _, iteration = dispatch(sandbox, store, worker)
    assert iteration.verdicts["Q1"] == "refused"
    assert store.load("Q1").status == "queued"
    expected = {"inherited", "worker-row"} if change == "partial" else {"inherited"}
    assert {r.id for r in read_all(sandbox.paths.runs)} == expected
    assert iteration.runs == (2 if change == "partial" else 1)

def test_settlement_refuses_rows_written_under_another_session(sandbox, store):
    """A run row that does not carry the dispatch slot's session is not evidence
    this attempt produced; settling over it would launder foreign measurement."""
    from autoresearch import attempts

    def worker(workspace, brief):
        append(workspace / "data/runs/new.jsonl", record(session="some-other-worker"))
        return report()

    coordinator, iteration = dispatch(sandbox, store, worker)
    assert iteration.verdicts["Q1"] == "refused"
    assert any("settlement refused" in line for line in iteration.phases[0].detail)
    assert [r["status"] for r in attempts.records(sandbox)] == ["failed"]


def test_restarted_budget_counts_reserved_but_unsettled_attempts(sandbox, store):
    """Restart-safe counting: a crash after reserve-but-before-settle still
    spends its reservation, and the same attempt is never counted twice."""
    from autoresearch import attempts
    from autoresearch.budget import total_runs
    make_entry(store, "Q1", impact=1.0)
    claims = attempts.claims(sandbox, "coordinator")
    claims.claim("Q1", why="crash test")
    assert total_runs(sandbox) == 0
    attempts.reserve(sandbox, "Q1", "coordinator", 60.0, kind="dispatch")
    assert total_runs(sandbox) == 1
    assert total_runs(sandbox) == 1, "reading the ledger again must not charge again"
