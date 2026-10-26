"""scripts/calibrate-priors.py: priors are scored as they stood before the
verdict, from git history, so a prior rewritten after the fact cannot leak
the outcome into its own calibration."""
import json
import pathlib
import subprocess
import sys

from conftest import close, make_entry

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "calibrate-priors.py"


def commit(root, message):
    git = ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t",
           "-c", "commit.gpgsign=false"]
    subprocess.run(git + ["add", "-A"], check=True, capture_output=True)
    subprocess.run(git + ["commit", "-qm", message], check=True, capture_output=True)


def run(sandbox, tmp_path, *extra):
    out = tmp_path / "rows.jsonl"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(sandbox.paths.root), "--jsonl", str(out), *extra],
        capture_output=True, text=True)
    rows = {r["id"]: r for r in map(json.loads, out.read_text().splitlines())} \
        if out.exists() else {}
    return proc, rows


def test_priors_are_read_before_the_verdict_not_after(sandbox, store, tmp_path):
    subprocess.run(["git", "init", "-q", str(sandbox.paths.root)], check=True)
    q1 = make_entry(store, "Q1", confidence=0.9)
    q2 = make_entry(store, "Q2", confidence=0.1)
    commit(sandbox.paths.root, "file")
    close(sandbox, store, q1, "confirmed")
    close(sandbox, store, q2, "refuted", closure_kind="mechanism")
    commit(sandbox.paths.root, "close")
    # The leak: a prior rewritten once the outcome is known.
    q2 = store.load("Q2")
    q2.confidence = 0.95
    store.save(q2)
    # Filed and closed in one commit: nothing to score it on.
    close(sandbox, store, make_entry(store, "Q3", confidence=0.5), "confirmed")
    commit(sandbox.paths.root, "amend and file-closed")

    proc, rows = run(sandbox, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert rows["Q2"]["filed"]["confidence"] == 0.1
    assert rows["Q2"]["pre_verdict"]["confidence"] == 0.1
    assert rows["Q2"]["final"]["confidence"] == 0.95
    assert rows["Q1"]["first_verdict"] == "confirmed"
    assert rows["Q3"]["born_closed"] and rows["Q3"]["pre_verdict"] is None
    # Before the verdict confidence separates perfectly; the leaked final
    # value points the wrong way. The report must show both.
    assert "filed 1.00" in proc.stdout and "final 0.00" in proc.stdout
    assert "changed after verdict 1/2" in proc.stdout
    assert "1 born closed" in proc.stdout


def test_a_domain_outside_git_is_refused_by_name(sandbox, store, tmp_path):
    make_entry(store, "Q1")
    proc, _ = run(sandbox, tmp_path)
    assert proc.returncode == 2
    assert "not readable as git history" in proc.stderr


def test_an_unknown_track_is_refused(sandbox, tmp_path):
    proc, _ = run(sandbox, tmp_path, "--track", "nope")
    assert proc.returncode == 2
    assert "unknown track 'nope'" in proc.stderr
