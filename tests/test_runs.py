import pytest

from autoresearch.errors import SchemaError
from autoresearch.goal import Goal
from autoresearch.runs import OK, RunRecord, append, read_all

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
