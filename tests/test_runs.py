import json
import pathlib

import pytest

from autoresearch.errors import SchemaError
from autoresearch.goal import Goal
from autoresearch.runs import OK, RunRecord, append, best_run, frontier, read_all

GOAL = Goal.from_dict({"goal": {
    "id": "g", "objective": "ops * peak",
    "metrics": {"ops": {}, "peak": {}}, "target": {"value": 1}}})


def record(**over):
    data = dict(metrics={"ops": 100.0, "peak": 10.0}, session="s1",
                provenance={"host": "h"})
    data.update(over)
    return RunRecord(**data)


def test_valid_record_passes():
    record().validate(GOAL)


def test_passing_run_with_all_zero_metrics_is_refused():
    """H134: four committed rows claimed a passing status with zero of the
    metric being optimised, which would score as a perfect result."""
    with pytest.raises(SchemaError, match="H134"):
        record(metrics={"ops": 0.0, "peak": 0.0}).validate(GOAL)


def test_passing_run_missing_a_declared_metric_is_refused():
    with pytest.raises(SchemaError, match="absent"):
        record(metrics={"ops": 100.0}).validate(GOAL)


def test_invalid_run_may_omit_metrics():
    record(metrics={}, status="invalid").validate(GOAL)


def test_record_without_provenance_is_refused():
    with pytest.raises(SchemaError, match="provenance"):
        record(provenance={}).validate(GOAL)


def test_record_without_session_is_refused():
    with pytest.raises(SchemaError, match="session"):
        record(session="").validate(GOAL)


def test_unknown_field_is_refused_rather_than_dropped():
    with pytest.raises(SchemaError, match="unknown field"):
        RunRecord.from_dict({"metrics": {}, "session": "s", "surprise": 1})


def test_roundtrip_through_jsonl(tmp_path):
    path = tmp_path / "runs" / "s1.jsonl"
    append(path, record())
    append(path, record(status="invalid", metrics={}))
    back = read_all(path.parent)
    assert len(back) == 2 and back[0].status == OK


def test_malformed_line_names_file_and_line(tmp_path):
    path = tmp_path / "runs" / "s1.jsonl"
    append(path, record())
    path.write_text(path.read_text() + "{not json}\n")
    with pytest.raises(SchemaError, match=r"s1\.jsonl:2"):
        read_all(path.parent)


def test_schema_id_does_not_collide_with_a_domain_owned_schema():
    """The first domain to adopt this core already has its own
    `schemas/run-v1.schema.json` with entirely different required keys. Two
    schemas under one name invites appending a core record into that corpus and
    producing a row its own validator rejects."""
    from autoresearch.runs import SCHEMA
    assert SCHEMA == "ar-run-1"


def test_a_domain_schema_record_is_refused_with_a_pointed_message():
    with pytest.raises(SchemaError, match="different, domain-owned schema"):
        record(schema="run-v1").validate(GOAL)


def test_a_failed_run_still_carries_usable_metrics():
    """Three outcomes, not two. A run whose experiment failed its validity gates
    may still have measured the axes perfectly well -- in the first real domain
    that is most of the corpus, because the whole lambda lane is about
    configurations that fail. Reporting those as `invalid` would make every
    refutation measured from a failing config unusable as evidence."""
    r = record(status="failed")
    r.validate(GOAL)
    assert r.metrics["ops"] == 100.0


def test_failed_is_not_subject_to_the_all_zero_guard():
    """The H134 guard is about a run claiming success having measured nothing;
    a run that reports failure is not making that claim."""
    record(status="failed", metrics={"ops": 0.0, "peak": 0.0}).validate(GOAL)


MAXIMISE = Goal.from_dict({"goal": {
    "id": "g", "objective": "ops * peak", "direction": "maximise",
    "metrics": {"ops": {}, "peak": {}}, "target": {"value": 1}}})


def test_best_run_follows_the_goal_direction():
    """The board, `ar budget` and the coordinator each hardcoded `<`, so a
    `direction: maximise` domain was shown its worst row as its best -- and the
    coordinator handed that row to the stop decision, which could therefore
    never reach the goal-met exit."""
    rows = [record(metrics={"ops": 10.0, "peak": 1.0}, id="lo"),
            record(metrics={"ops": 100.0, "peak": 1.0}, id="hi")]
    assert best_run(rows, GOAL)[1].id == "lo"
    assert best_run(rows, MAXIMISE)[1].id == "hi"


def test_best_run_skips_rows_it_cannot_score():
    """A row that cannot be scored is skipped, never counted as zero (H134)."""
    rows = [record(metrics={}, status="invalid", id="bad"),
            record(metrics={"ops": 5.0}, id="partial"),
            record(metrics={"ops": 10.0, "peak": 1.0}, id="good")]
    value, best = best_run(rows, GOAL)
    assert (best.id, value) == ("good", 10.0)


def test_best_run_of_nothing_is_none():
    assert best_run([], GOAL) is None


def test_non_object_rows_are_skipped_or_named_in_strict_mode(tmp_path):
    from autoresearch.runs import read_with_skipped

    path = tmp_path / "mixed.jsonl"
    path.write_text('[1, 2]\n')
    append(path, record(id="valid"))
    rows, skipped = read_with_skipped(tmp_path)
    assert [row.id for row in rows] == ["valid"]
    assert skipped == 1
    with pytest.raises(SchemaError, match=r"mixed\.jsonl:1:"):
        read_with_skipped(tmp_path, strict=True)


@pytest.mark.parametrize("strict", [False, True])
def test_invalid_utf8_names_file_and_line(tmp_path, strict):
    path = tmp_path / "broken.jsonl"
    append(path, record())
    with path.open("ab") as stream:
        stream.write(b"\xff\xfe\n")
    with pytest.raises(SchemaError, match=r"broken\.jsonl:2:"):
        read_all(tmp_path, strict=strict)


def test_invalid_rows_cannot_win_or_satisfy_goal():
    good = record(metrics={"ops": 10.0, "peak": 1.0}, id="good")
    invalid = [record(metrics={"ops": 0.0, "peak": 0.0}),
               record(metrics={"ops": 0.1, "peak": 1.0}, provenance={})]
    assert best_run(invalid, GOAL) is None
    value, winner = best_run([good, *invalid], GOAL)
    assert (value, winner.id) == (10.0, "good")


def test_read_all_picks_up_rows_appended_outside_the_reader(tmp_path):
    """Rows are append-only per session file, and workers append while the
    coordinator reads: a cached read must still see every new row on the next
    call, which the re-stat'ed (size, mtime_ns) signature guarantees."""
    path = tmp_path / "s1.jsonl"
    append(path, record(id="r1"))
    assert [r.id for r in read_all(tmp_path)] == ["r1"]
    appended = record(id="r2")
    with path.open("a") as stream:  # a raw append, outside `runs.append`
        stream.write(json.dumps(appended.to_dict(), sort_keys=True) + "\n")
    assert [r.id for r in read_all(tmp_path)] == ["r1", "r2"]


def test_skipped_count_is_recomputed_for_a_changed_file(tmp_path):
    from autoresearch.runs import read_with_skipped
    path = tmp_path / "mixed.jsonl"
    path.write_text("[1, 2]\n")
    append(path, record(id="valid"))
    rows, skipped = read_with_skipped(tmp_path)
    assert ([r.id for r in rows], skipped) == (["valid"], 1)
    path.write_text(path.read_text() + "[3]\n")  # one more foreign row
    rows, skipped = read_with_skipped(tmp_path)
    assert ([r.id for r in rows], skipped) == (["valid"], 2)


def test_snapshot_reuses_the_digest_of_unchanged_files(tmp_path, monkeypatch):
    """`snapshot` runs once per worker per iteration over inherited files whose
    bytes are identical across workers; only the hashing of unchanged bytes may
    be cached, and the returned (st_size, digest) structure is untouched."""
    from autoresearch import runs as runs_mod
    path = tmp_path / "s1.jsonl"
    append(path, record(id="r1"))
    first = runs_mod.snapshot(tmp_path)
    calls = []
    real_sha256 = runs_mod.hashlib.sha256

    def counting(*args, **kwargs):
        calls.append(1)
        return real_sha256(*args, **kwargs)

    monkeypatch.setattr(runs_mod.hashlib, "sha256", counting)
    assert runs_mod.snapshot(tmp_path) == first
    assert calls == []  # unchanged bytes: no re-hash
    with path.open("a") as stream:
        stream.write("\n")
    second = runs_mod.snapshot(tmp_path)
    assert len(calls) == 1  # the changed file was re-hashed
    assert second[pathlib.Path("s1.jsonl")][1] != first[pathlib.Path("s1.jsonl")][1]


# -- the frontier ignores wiring checks (sig.golf H4) ------------------------

class _Store:
    """The one method `frontier` asks of a store."""

    def __init__(self, **entries):
        self._entries = entries

    def load(self, entry_id):
        return self._entries[entry_id]      # KeyError: unreadable evidence


def _entry(scaffold=False):
    return type("Entry", (), {"scaffold": scaffold})()


def test_a_scaffold_row_leaves_the_frontier_and_is_named():
    fixture = record(metrics={"ops": 1.0, "peak": 1.0}, id="fix", entry="Q1")
    finding = record(metrics={"ops": 10.0, "peak": 1.0}, id="find", entry="Q2")
    best, excluded = frontier(_Store(Q1=_entry(True), Q2=_entry()), [fixture, finding], GOAL)
    assert (best[0], best[1].id) == (10.0, "find")
    assert (excluded[0], excluded[1].id) == (1.0, "fix")


def test_a_domain_whose_rows_are_all_scaffolding_has_no_frontier():
    """Better no headline than a fixture's: the board says so instead of
    ranking a wiring check as a finding."""
    rows = [record(metrics={"ops": 1.0, "peak": 1.0}, id="a", entry="Q1"),
            record(metrics={"ops": 2.0, "peak": 1.0}, id="b", entry="Q2")]
    best, excluded = frontier(_Store(Q1=_entry(True), Q2=_entry(True)), rows, GOAL)
    assert best is None
    assert excluded[1].id == "a"      # the better of the two, for a minimise goal


def test_a_row_naming_no_entry_is_a_finding():
    """Default-deny runs one way: nothing is scaffolding unless the domain said
    so, so an unattributed row is never quietly dropped from the frontier."""
    rows = [record(metrics={"ops": 1.0, "peak": 1.0}, id="orphan"),
            record(metrics={"ops": 9.0, "peak": 1.0}, id="find", entry="Q2")]
    best, excluded = frontier(_Store(Q2=_entry()), rows, GOAL)
    assert (best[0], best[1].id) == (1.0, "orphan")
    assert excluded is None


def test_a_row_whose_entry_cannot_be_read_stays_in_the_frontier():
    """Nobody can prove an unreadable entry declared itself scaffolding."""
    rows = [record(metrics={"ops": 1.0, "peak": 1.0}, id="lost", entry="Q404"),
            record(metrics={"ops": 9.0, "peak": 1.0}, id="find", entry="Q2")]
    best, excluded = frontier(_Store(Q2=_entry()), rows, GOAL)
    assert (best[0], best[1].id) == (1.0, "lost")
    assert excluded is None


def test_a_zero_objective_from_one_zero_factor_is_flagged():
    """ecdsa H169: `round(T) * Q` with T=0 scored a perfect 0 because the H134
    guard only fired when *every* metric was zero."""
    with pytest.raises(SchemaError, match="H169"):
        record(metrics={"ops": 0.0, "peak": 7.0}).validate(GOAL)
    # maximise: zero is the worst score, not a perfect one
    record(metrics={"ops": 0.0, "peak": 7.0}).validate(MAXIMISE)
    # a goal whose target is legitimately zero opts out, as it does elsewhere
    zero_ok = Goal.from_dict({"goal": {
        "id": "g", "objective": "ops * peak", "metrics": {"ops": {}, "peak": {}},
        "target": {"value": 0}, "allow_degenerate_target": True}})
    record(metrics={"ops": 0.0, "peak": 7.0}).validate(zero_ok)


def test_frontier_leaves_out_rows_failing_a_constraint_or_marked_scaffold(sandbox, store):
    """#9: a row over the scorer's size cap (falcon H2), a reduced-width toy
    (ecdsa H168) and a finding's cost bench (sig.golf H41) each printed as
    best. The frontier skips them and names the best one it left out."""
    goal = Goal.from_dict({"goal": {
        "id": "g", "objective": "ops * peak", "metrics": {"ops": {}, "peak": {}},
        "target": {"value": 1}, "constraints": ["width == 256", "peak <= 50"]}})
    rows = [record(id="toy", metrics={"ops": 1.0, "peak": 1.0}, config={"width": 16}),
            record(id="big", metrics={"ops": 1.0, "peak": 60.0}, config={"width": 256}),
            record(id="bench", metrics={"ops": 1.0, "peak": 2.0},
                   config={"width": 256, "scaffold": True}),
            record(id="real", metrics={"ops": 1.0, "peak": 9.0}, config={"width": 256}),
            record(id="nowidth", metrics={"ops": 1.0, "peak": 3.0})]
    best, excluded = frontier(store, rows, goal)
    assert best[1].id == "real"
    value, row, reason = excluded
    assert row.id == "toy" and "width == 256" in reason
    with pytest.raises(Exception, match="constraints"):
        Goal.from_dict({"goal": {"id": "g", "objective": "ops", "metrics": {"ops": {}},
                                 "target": {"value": 1}, "constraints": "x > 1"}})


def test_measure_refuses_a_closed_entry(sandbox, store, capsys):
    from autoresearch import cli
    from conftest import close, make_entry
    close(sandbox, store, make_entry(store, "Q1"))
    assert cli.main(["--domain", str(sandbox.paths.root), "measure",
                     "--entry", "Q1"]) == 2
    assert "Q1 is closed" in capsys.readouterr().err
