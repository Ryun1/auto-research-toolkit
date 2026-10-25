"""An unlabelled closure must not read as a ledger-backed one (QSB H87).

`ar close --evidence-class` defaulted to `ledger`, described in its own help as
"the run-ledger faithfulness net applies". So a closure resting on a ptxas
register count, and a closure resting on a full measured A/B, were stamped
SASS delta came to sit in the refuted column reading exactly like the measured
closures beside it.

All three halves of the fix are asserted here: the flag has no default, so an
the rendered queue shows the class per row, so a blank is visible as a blank;
and an unlabelled `confirmed` closure still faces the full faithfulness net,
because only an explicit `official`/`census` opts out.
"""
import json

import pytest

from autoresearch import cli
from autoresearch.entries import Result
from autoresearch.errors import SchemaError
from autoresearch.runs import closure_evidence, faithfulness_problems
from conftest import close, make_entry

FIELDS = dict(verdict="refuted", memo="inbox/a.md",
              at="2026-01-01T00:00:00+00:00", session="s", summary="",
              closure_kind="cell", reopen_condition="a re-measurement at n>=3")

def _ar(sandbox, *args) -> int:
    return cli.main(["--domain", str(sandbox.paths.root), "--session", "s",
                     *args])

def _result_json(sandbox, capsys, entry_id="Q1") -> dict:
    capsys.readouterr()          # drain whatever the mutating verb printed
    assert _ar(sandbox, "entry", "show", entry_id) == 0
    return json.loads(capsys.readouterr().out)["result"]

def _close(sandbox, store, entry_id, evidence_class=None):
    entry = close(sandbox, store, make_entry(store, entry_id), verdict="refuted",
                  closure_kind="cell", reopen_condition="a re-measurement at n>=3")
    if evidence_class is not None:
        entry.result.evidence_class = evidence_class
        store.save(entry)
    return entry

def test_close_without_the_flag_records_no_class(sandbox, store, capsys):
    _close(sandbox, store, "Q1")
    assert _result_json(sandbox, capsys)["evidence_class"] is None

def test_an_explicit_class_is_recorded(sandbox, store, capsys):
    _close(sandbox, store, "Q1", evidence_class="census")
    assert _result_json(sandbox, capsys)["evidence_class"] == "census"

def test_a_historical_ledger_stamp_survives_the_round_trip(sandbox, store, capsys):
    """Entries closed before this change carry `evidence_class: ledger` in their
    YAML. They must keep saying so -- the fix changes new closures, not the
    meaning of a recorded one."""
    _close(sandbox, store, "Q1", evidence_class="ledger")
    assert _result_json(sandbox, capsys)["evidence_class"] == "ledger"

def test_unlabelled_does_not_become_a_free_pass():
    """Only an explicit official/census skips the mechanical faithfulness net,
    so dropping the default must not have loosened it: an unlabelled result
    faces exactly the checks a `ledger` one does."""
    class _Goal:
        def objective_value(self, metrics):
            return metrics.get("x", 0)

    goal = _Goal()
    problems = {}
    for evidence_class in (None, "ledger", "census", "official"):
        entry = _Entry(Result(**dict(FIELDS, verdict="confirmed",
                                     evidence_class=evidence_class)))
        problems[evidence_class] = (faithfulness_problems([], entry, goal),
                                     closure_evidence([], "Q1", 1))
    unlabelled, ledger = problems[None], problems["ledger"]
    assert unlabelled == ledger, "dropping the default loosened the net"
    assert unlabelled[0], "an unbacked confirmed summary must be named"
    for exempt in ("census", "official"):
        assert problems[exempt][0] == [], exempt

def test_the_rendered_view_shows_the_class_and_blanks_read_as_blanks(sandbox,
                                                                    store):
    from autoresearch.render import write_views
    _close(sandbox, store, "Q1")
    _close(sandbox, store, "Q2", evidence_class="census")
    view = sandbox.paths.root / "docs" / "log" / "Hypothesis Queue.md"
    view.parent.mkdir(parents=True, exist_ok=True)
    write_views(sandbox, store.all())
    rows = {line.split("|")[1].strip(): line
            for line in view.read_text().splitlines() if line.startswith("| Q")}
    assert "(unlabelled)" in rows["Q1"]
    assert "census" in rows["Q2"]

def test_result_rejects_a_class_it_does_not_declare():
    with pytest.raises(SchemaError):
        Result(evidence_class="measured-ish", **FIELDS)
    assert Result(evidence_class=None, **FIELDS).evidence_class is None

class _Entry:
    """The attributes the faithfulness net reads off an entry."""

    def __init__(self, result):
        self.id = "Q1"
        self.result = result
