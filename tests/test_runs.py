import pytest

from autoresearch.errors import SchemaError
from autoresearch.goal import Goal
from autoresearch.runs import OK, RunRecord, append, best_run, read_all

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
