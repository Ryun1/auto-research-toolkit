"""#39: an anomalously large win is held, not crowned.

METR's most effective reward-hacking detector was the cheapest: flag
unusually high scores, then look. Sakana's CUDA Engineer exploit was found
from outside because its speedups were implausible. With a `suspect` rule in
goal.yaml, a gain far past the noise floor (or, with no noise method, far
past the largest step accepted so far) stays off the frontier until a clean
re-measure from another session releases it, or a person runs `ar promote`.
At a `confirmed` close, diff hunks shaped like tampering are listed for QC
and a person to read. They decide nothing.
"""
import json
import subprocess

import pytest

from autoresearch import cli, epochs, evaluator, runs, tamper
from autoresearch.config import DomainConfig
from autoresearch.errors import GoalError
from autoresearch.goal import Goal
from autoresearch.runs import RunRecord, append, read_all
from conftest import make_entry


def _goal(**extra):
    data = {"id": "g", "objective": "ops", "metrics": {"ops": {}},
            "target": {"value": 1}}
    data.update(extra)
    return Goal.from_dict({"goal": data})


def _row(run_id, ops, config, session=None, started=0.0, sha=None):
    provenance = {"host": "local"}
    if sha:
        provenance["evaluator_sha"] = sha
    return RunRecord(id=run_id, metrics={"ops": float(ops), "peak": 1.0},
                     session=session or f"s-{run_id}", started=started,
                     config={"knobs": config}, provenance=provenance)


def _ledger():
    """1000 -> 990 (a step of 10) -> 20: a 97x larger step."""
    return [_row("a", 1000, "a", started=1), _row("b", 990, "b", started=2),
            _row("c", 20, "c", session="lucky", started=3)]


def _domain(sandbox, block):
    path = sandbox.paths.root / "goal.yaml"
    path.write_text(path.read_text().replace(
        "  yield_floor:", block + "\n  yield_floor:"))
    return DomainConfig.load(sandbox.paths.root)


# -- the goal block ---------------------------------------------------------

def test_suspect_is_off_unless_declared():
    assert _goal().suspect.k is None
    assert _goal(suspect={}).suspect.k == 10.0


@pytest.mark.parametrize("block", [{"k": 0}, {"k": "big"}, {"factor": 3}, "on"])
def test_a_malformed_suspect_block_is_refused(block):
    with pytest.raises(GoalError, match="suspect"):
        _goal(suspect=block)


# -- holding a suspect row --------------------------------------------------

def test_a_huge_step_is_held_off_the_frontier(store):
    best, excluded = runs.frontier(store, _ledger(), _goal(suspect={"k": 10}))
    assert best[1].id == "b"
    assert excluded[1].id == "c" and excluded[2].startswith("suspect")


def test_without_the_rule_the_huge_step_stands(store):
    best, _ = runs.frontier(store, _ledger(), _goal())
    assert best[1].id == "c"


def test_the_first_improvement_has_no_step_to_compare_and_stands(store):
    rows = [_row("a", 1000, "a", started=1), _row("b", 20, "b", started=2)]
    best, _ = runs.frontier(store, rows, _goal(suspect={"k": 10}))
    assert best[1].id == "b"


def test_a_clean_remeasure_from_another_session_releases_it(store):
    rows = _ledger() + [_row("c2", 22, "c", session="other", started=4)]
    best, excluded = runs.frontier(store, rows, _goal(suspect={"k": 10}))
    assert best[1].id == "c" and excluded is None


def test_a_remeasure_from_the_same_session_does_not(store):
    rows = _ledger() + [_row("c2", 22, "c", session="lucky", started=4)]
    best, excluded = runs.frontier(store, rows, _goal(suspect={"k": 10}))
    assert best[1].id == "b" and excluded[2].startswith("suspect")


def test_a_remeasure_under_another_evaluator_does_not(sandbox, store):
    current = evaluator.identity(sandbox)
    rows = [_row("a", 1000, "a", started=1, sha=current),
            _row("b", 990, "b", started=2, sha=current),
            _row("c", 20, "c", session="lucky", started=3, sha=current),
            _row("c2", 22, "c", session="other", started=4, sha="0ld")]
    epoch_rows = epochs.current_rows(sandbox, rows)
    best, _ = runs.frontier(store, epoch_rows, _goal(suspect={"k": 10}))
    assert best[1].id == "b"


def test_under_a_noise_method_the_floor_sets_the_bar(store):
    goal = _goal(noise={"method": "mad"}, suspect={"k": 10})
    rows = [_row("a1", 1000, "a", started=1), _row("a2", 1010, "a", started=2),
            _row("a3", 990, "a", started=3), _row("b", 500, "b", started=4)]
    best, excluded = runs.frontier(store, rows, goal)
    assert best[1].config["knobs"] == "a" and excluded[1].id == "b"
    assert "noise floor" in excluded[2]


def test_a_fifty_fold_gain_does_not_trigger_goal_met(sandbox, capsys):
    """The issue's negative test, through the real stop decision."""
    config = _domain(sandbox, "  suspect: {k: 10}")
    target = sandbox.paths.root / "bin/probe-target"
    target.write_text("#!/bin/sh\necho 500\n")
    target.chmod(0o755)
    for row in [_row("a", 1000, "a", started=1), _row("b", 990, "b", started=2),
                _row("c", 20, "c", session="lucky", started=3)]:
        append(config.paths.runs / "s.jsonl", row)
    assert cli.main(["--domain", str(config.paths.root), "budget", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["best"] == 990.0 and doc["stop"]["reason"] != "goal-met"


# -- a person can release it ------------------------------------------------

def test_ar_promote_releases_a_suspect_row(sandbox, store, capsys):
    config = _domain(sandbox, "  suspect: {k: 10}")
    for row in _ledger():
        append(config.paths.runs / "s.jsonl", row)
    root = str(config.paths.root)
    assert cli.main(["--domain", root, "promote", "c", "--why",
                     "re-ran it by hand on the bench"]) == 0
    best, _ = runs.frontier(store, read_all(config.paths.runs), config.goal)
    assert best[1].id == "c"


def test_promote_needs_a_known_row_and_a_reason(sandbox, capsys):
    root = str(sandbox.paths.root)
    append(sandbox.paths.runs / "s.jsonl", _row("c", 20, "c"))
    assert cli.main(["--domain", root, "promote", "nope", "--why", "x"]) == 2
    assert "no run" in capsys.readouterr().err
    assert cli.main(["--domain", root, "promote", "c", "--why", " "]) == 2


# -- tamper-shaped hunks ----------------------------------------------------

DIFF = """\
diff --git a/bin/measure b/bin/measure
--- a/bin/measure
+++ b/bin/measure
@@ -10,0 +11,1 @@
+    return 1.0
diff --git a/tests/test_speed.py b/tests/test_speed.py
--- a/tests/test_speed.py
+++ b/tests/test_speed.py
@@ -1,0 +2,1 @@
+    assert True
diff --git a/src/kernel.py b/src/kernel.py
--- a/src/kernel.py
+++ b/src/kernel.py
@@ -5,0 +6,5 @@
+import time
+time.time = lambda: 0.0
+start = time.perf_counter()
+    def __eq__(self, other):
+self.width = 3
"""


def test_the_scan_types_each_tamper_shape(sandbox):
    hunks = tamper.scan(sandbox, DIFF)
    kinds = {(h.kind, h.path, h.line) for h in hunks}
    assert ("evaluator", "bin/measure", 11) in kinds
    assert ("test", "tests/test_speed.py", 2) in kinds
    assert ("monkeypatch", "src/kernel.py", 7) in kinds
    assert ("timing", "src/kernel.py", 8) in kinds
    assert ("equality", "src/kernel.py", 9) in kinds
    # an ordinary attribute write on self is not a monkey-patch
    assert not any(h.line == 10 for h in hunks)


def test_findings_lane_files_are_not_scanned(sandbox):
    memo = DIFF.replace("src/kernel.py", "inbox/memo.md")
    assert not any(h.path == "inbox/memo.md" for h in tamper.scan(sandbox, memo))


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True,
                          capture_output=True, text=True).stdout


def _commit(root):
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "-c", "user.name=T", "-c", "user.email=t@example.invalid",
         "commit", "-qm", "fixture")


def test_a_confirmed_close_lists_tamper_shaped_hunks_and_records_them(
        sandbox, store, capsys):
    _commit(sandbox.paths.root)
    make_entry(store, "Q1")
    (sandbox.paths.root / "src").mkdir()
    (sandbox.paths.root / "src/kernel.py").write_text(
        "import time\ntime.perf_counter = lambda: 0.0\n")
    root = str(sandbox.paths.root)
    assert cli.main(["--domain", root, "--session", "w", "claim", "Q1",
                     "--why", "t"]) == 0
    (sandbox.paths.root / "inbox/q1.md").write_text("memo")
    capsys.readouterr()
    assert cli.main(["--domain", root, "--session", "w", "close", "Q1",
                     "confirmed", "--memo", "inbox/q1.md", "--summary", "fast",
                     "--why", "done"]) == 0
    out = capsys.readouterr().out
    assert "monkeypatch  src/kernel.py:2" in out
    event = [e for e in store.load("Q1").history if e.kind == "tamper-scan"]
    assert event and "src/kernel.py" in event[0].detail
    assert store.load("Q1").status == "confirmed"      # data, not a decision


def test_a_clean_close_records_no_tamper_event(sandbox, store, capsys):
    _commit(sandbox.paths.root)
    make_entry(store, "Q1")
    root = str(sandbox.paths.root)
    assert cli.main(["--domain", root, "--session", "w", "claim", "Q1",
                     "--why", "t"]) == 0
    (sandbox.paths.root / "inbox/q1.md").write_text("memo")
    assert cli.main(["--domain", root, "--session", "w", "close", "Q1",
                     "confirmed", "--memo", "inbox/q1.md", "--summary", "fast",
                     "--why", "done"]) == 0
    assert not [e for e in store.load("Q1").history if e.kind == "tamper-scan"]


def test_the_loop_lists_a_workers_tamper_shaped_hunks(sandbox, store):
    from types import SimpleNamespace

    from autoresearch.budget import iteration_budget
    from autoresearch.driver.brain import Reply
    from autoresearch.driver.loop import Coordinator, Iteration
    from autoresearch.workspaces import Pool
    _commit(sandbox.paths.root)
    make_entry(store, "Q1", impact=1.0)

    class Brain:
        def ask(self, role, brief, *, workspace=None):
            (workspace / "src").mkdir(exist_ok=True)
            (workspace / "src/kernel.py").write_text(
                "class K:\n    def __hash__(self):\n        return 0\n")
            (workspace / "inbox").mkdir(exist_ok=True)
            (workspace / "inbox/r.md").write_text("fast")
            return Reply(role=role, data={
                "verdict": "confirmed", "memo": "inbox/r.md", "runs": 0,
                "summary": "fast", "evidence_class": "census",
                "verification": {"reread": True, "claims_checked": ["x"]}})

    iteration = Iteration(n=1)
    with Pool(sandbox, "tamper") as pool:
        Coordinator(sandbox, Brain()).dispatch(
            iteration, [SimpleNamespace(entry_id="Q1", terms={})],
            iteration_budget(sandbox), pool)
    assert store.load("Q1").status == "confirmed"
    assert any("equality  src/kernel.py:2" in line
               for line in iteration.phases[0].detail)
    assert [e for e in store.load("Q1").history if e.kind == "tamper-scan"]


def test_an_external_completion_lists_tamper_shaped_hunks(sandbox, store):
    import pathlib

    from autoresearch import external
    from autoresearch.claims import Claims
    _commit(sandbox.paths.root)
    make_entry(store, "Q1")
    owner = Claims(store, sandbox, "owner")
    owner.claim("Q1", why="bounded")
    assigned = external.assign(sandbox, "Q1", "owner", 30, 1,
                               token=owner.issued["Q1"])
    workspace = pathlib.Path(assigned["workspace"])
    (workspace / "src").mkdir()
    (workspace / "src/kernel.py").write_text("import time\nt = time.perf_counter()\n")
    (workspace / "inbox").mkdir(exist_ok=True)
    (workspace / "inbox/m.md").write_text("fast")
    done = external.complete(sandbox, assigned["id"], "owner", {
        "runs": 0, "verdict": "confirmed", "memo": "inbox/m.md",
        "summary": "fast", "evidence_class": "census",
        "verification": {"reread": True, "claims_checked": ["x"]}})
    assert done["status"] == "completed"
    assert any(e.kind == "tamper-scan" for e in store.load("Q1").history)


def test_a_suspect_row_cannot_release_itself(store):
    """Review: release compared the re-measure's session with the config's
    *first* row's session, so a lucky row whose config an honest session had
    measured earlier (and worse) counted as its own second session."""
    rows = [_row("a", 1000, "a", started=1), _row("b", 990, "b", started=2),
            _row("c0", 5000, "c", session="honest", started=3),
            _row("c1", 20, "c", session="lucky", started=4)]
    best, excluded = runs.frontier(store, rows, _goal(suspect={"k": 10}))
    assert best[1].id == "b" and excluded[2].startswith("suspect")


def test_a_suspect_row_does_not_confirm_the_best(sandbox, store):
    """A row held as suspect is not evidence; it may not be the second
    measurement `confirm_independently` asks for either."""
    from autoresearch import budget as budget_mod
    goal = _goal(suspect={"k": 10})
    rows = [_row("a", 1000, "a", started=1), _row("b", 990, "b", started=2),
            _row("b2", 991, "b", session="other", started=3),
            _row("c", 20, "c", session="lucky", started=4)]
    best, _ = runs.frontier(store, rows, goal)
    usable = runs.confirmation_rows(store, rows, goal)
    assert "c" not in {r.id for r in usable}
    assert budget_mod.independent_confirmation(goal, best, usable, 995.0)


def test_an_added_line_that_looks_like_a_header_does_not_steer_the_scan(sandbox):
    """Review: an added line `++ docs/notes.md` shows in the diff as
    `+++ docs/notes.md` and was read as a new file, so later added lines were
    attributed to a findings-lane path and skipped."""
    diff = ("diff --git a/src/x.py b/src/x.py\n--- a/src/x.py\n+++ b/src/x.py\n"
            "@@ -0,0 +1,3 @@\n+'''\n+++ docs/notes.md\n+time.time = lambda: 0\n")
    hunks = tamper.scan(sandbox, diff)
    assert [(h.kind, h.path, h.line) for h in hunks if h.kind == "monkeypatch"] \
        == [("monkeypatch", "src/x.py", 3)]


def test_the_scan_reads_paths_whatever_the_users_diff_prefix(sandbox, store, capsys):
    """`diff.mnemonicPrefix` makes git print `w/path`; the scan asks for
    a/ b/ explicitly, so the evaluator and findings lanes still classify."""
    _commit(sandbox.paths.root)
    _git(sandbox.paths.root, "config", "diff.mnemonicPrefix", "true")
    (sandbox.paths.root / "bin/measure").write_text(
        (sandbox.paths.root / "bin/measure").read_text() + "\n# tuned\n")
    hunks = tamper.scan_tree(sandbox, sandbox.paths.root)
    assert ("evaluator", "bin/measure") in {(h.kind, h.path) for h in hunks}
