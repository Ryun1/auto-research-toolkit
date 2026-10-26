"""scripts/audit-fast-closures.py: verdicts closed soon after filing are
queued for audit, and the rule is back-tested on reopens that say why."""
import pathlib
import subprocess
import sys

from autoresearch.entries import Entry, Event

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "audit-fast-closures.py"


def filed(store, entry_id, created, *events):
    """An entry with a hand-written history: (at, kind, snapshot) triples."""
    store.save(Entry(id=entry_id, track="research", title=f"{entry_id} title",
                     status=events[-1][1].split("->")[1], created=created,
                     history=[Event(at, kind, "s", "", snapshot=snap)
                              for at, kind, snap in events]))


def run(sandbox, *extra):
    return subprocess.run([sys.executable, str(SCRIPT), str(sandbox.paths.root), *extra],
                          capture_output=True, text=True)


def test_fast_unreopened_verdicts_are_queued_and_the_rule_is_backtested(sandbox, store):
    day = "2026-01-01T"
    # closed in 5 minutes, never reopened: queued
    filed(store, "Q1", day + "10:00:00+00:00",
          (day + "10:01:00+00:00", "queued->in-progress", {}),
          (day + "10:05:00+00:00", "in-progress->refuted", {}))
    # closed a day later: not queued
    filed(store, "Q2", day + "10:00:00+00:00",
          (day + "10:01:00+00:00", "queued->in-progress", {}),
          ("2026-01-02T12:00:00+00:00", "in-progress->confirmed", {}))
    # fast, then reopened as weak: already looked at, so not queued, but it
    # is the back-test's one weak verdict
    filed(store, "Q3", day + "10:00:00+00:00",
          (day + "10:01:00+00:00", "queued->in-progress", {}),
          (day + "10:02:00+00:00", "in-progress->refuted", {}),
          (day + "11:00:00+00:00", "refuted->queued", {"reopen_reason": "weak"}))
    # closed before it was created: untimed, not queued
    filed(store, "Q4", day + "10:00:00+00:00",
          (day + "09:00:00+00:00", "in-progress->refuted", {}))

    proc = run(sandbox)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "holds 1 of 1 weak" in out
    assert "flags 2 (67%)" in out
    assert "queue: 1 verdict(s)" in out and "1 untimed" in out
    queue = out.split("queue:")[1]
    assert "Q1 " in queue
    for other in ("Q2 ", "Q3 ", "Q4 "):
        assert other not in queue


def test_without_reasons_there_is_nothing_to_backtest(sandbox, store):
    filed(store, "Q1", "2026-01-01T10:00:00+00:00",
          ("2026-01-01T10:05:00+00:00", "in-progress->refuted", {}))
    proc = run(sandbox)
    assert proc.returncode == 0, proc.stderr
    assert "no reopen in this record says why yet" in proc.stdout


def test_hand_labels_stand_in_for_reasons_the_record_predates(sandbox, store, tmp_path):
    filed(store, "Q1", "2026-01-01T10:00:00+00:00",
          ("2026-01-01T10:05:00+00:00", "in-progress->refuted", {}),
          ("2026-01-01T11:00:00+00:00", "refuted->queued", {}))
    labels = tmp_path / "labels.tsv"
    labels.write_text("id\thistory_index\tlabel\nQ1\t1\tweak\n")
    proc = run(sandbox, "--labels", str(labels))
    assert proc.returncode == 0, proc.stderr
    assert "weak 1" in proc.stdout and "holds 1 of 1 weak" in proc.stdout
