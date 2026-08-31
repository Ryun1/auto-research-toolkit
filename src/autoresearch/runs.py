"""Run records: the only thing that counts as a measurement.

The record this generalises has an entire failure class about results escaping
the ledger, and it is worth stating plainly because the fix is structural:

* `bin/harvest` was *documented* as the guard against a worktree vanishing with
  its data, but read only one file name, so a driver writing its own layout was
  outside the net -- 470 MB of a completed corpus survived by 11 hours of luck
  (H142).
* Undeclared dumps were invisible to harvesting (H64); the source behind a
  *confirmed* result lived only in a gitignored checkout (H39); a row could not
  say its builder had been patched, so one group could destroy a pooled
  analysis (H37, H63).
* Harvest destinations collided across sessions, so two sessions' rows
  overwrote each other (H100, H129).
* And four rows claimed a passing status with **zero** of the metric being
  optimised -- which would score as a perfect result -- because validation did
  not check that a passing run measured anything (H134).

So a result is not produced by convention and then collected. It is **returned**
through this schema, by a domain command, or it is not evidence. `metrics` must
carry every metric the goal declares, `outputs` must declare every artefact the
run produced, and `provenance` must identify what was actually built.
"""
from __future__ import annotations

import dataclasses
import json
import pathlib
import time
import uuid
from dataclasses import dataclass, field

from .errors import AutoresearchError, SchemaError

# Deliberately NOT "run-v1": the first domain to adopt this core already has a
# `schemas/run-v1.schema.json` of its own, with entirely different required
# keys. Two different schemas under one name invites appending a core record
# straight into that corpus and producing a row its own validator rejects,
# under a name that says it should pass.
SCHEMA = "ar-run-1"
OK, FAILED, INVALID = "ok", "failed", "invalid"
STATUSES = (OK, FAILED, INVALID)


@dataclass
class RunRecord:
    metrics: dict[str, float]
    session: str
    entry: str | None = None
    status: str = OK
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    schema: str = SCHEMA
    started: float = field(default_factory=time.time)
    finished: float | None = None
    config: dict = field(default_factory=dict)
    provenance: dict = field(default_factory=dict)
    outputs: list[str] = field(default_factory=list)
    cost: dict = field(default_factory=dict)
    notes: str = ""

    # -- validation -------------------------------------------------------

    def validate(self, goal=None) -> None:
        problems = self.problems(goal)
        if problems:
            raise SchemaError(
                f"run record {self.id} is invalid:\n  - " + "\n  - ".join(problems))

    def problems(self, goal=None) -> list[str]:
        out = []
        if self.schema != SCHEMA:
            out.append(f"schema is {self.schema!r}, expected {SCHEMA!r}"
                       + (" -- 'run-v1' is a different, domain-owned schema"
                          if self.schema == "run-v1" else ""))
        if self.status not in STATUSES:
            out.append(f"status {self.status!r} not in {STATUSES}")
        if not self.session:
            out.append("session is empty -- a row that cannot name its author "
                       "cannot be audited (H22)")
        for name, value in self.metrics.items():
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                out.append(f"metric {name!r} is {value!r}, not a number")
        if goal is not None:
            missing = set(goal.metrics) - set(self.metrics)
            if missing and self.status == OK:
                out.append(
                    f"status is {OK!r} but declared metric(s) {sorted(missing)} "
                    "are absent -- a passing run must measure what the goal is "
                    "about (H134)")
            # H134 directly: a passing run whose optimised metrics are all zero
            # would score as perfect, and did, four times.
            present = {k: self.metrics[k] for k in goal.metrics if k in self.metrics}
            if self.status == OK and present and all(v == 0 for v in present.values()):
                out.append(
                    "status is 'ok' but every goal metric is zero; this scores as "
                    "a perfect result and is almost certainly a run that did "
                    "nothing (H134). Mark it 'invalid' or explain it in notes.")
        if not self.provenance:
            out.append("provenance is empty -- a measurement with no record of "
                       "what was built cannot be reproduced or trusted (H37/H63)")
        return out

    # -- io ---------------------------------------------------------------

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "RunRecord":
        known = {f.name for f in dataclasses.fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise SchemaError(
                f"run record has unknown field(s) {sorted(unknown)}; known: "
                f"{sorted(known)}. A field nobody reads is a measurement nobody "
                "will find.")
        return cls(**data)


def append(path, record: RunRecord) -> pathlib.Path:
    """Append one record to a session-owned JSONL file.

    Session-owned is the concurrency model: one writer per path, so two sessions
    can never interleave. The name is the session's, never a basename derived
    from a directory -- that fallback collided across sessions and silently
    overwrote rows (H100, H129).
    """
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(record.to_dict(), sort_keys=True) + "\n")
    return path


def best_run(records, goal):
    """The best valid measurement, in whichever direction the goal wants.

    Returns `(objective_value, record)`, or None if no row is both `ok` and
    readable against this goal. Rows that cannot be scored are skipped rather
    than counted as zero -- a metric that reads as absent must not read as a
    perfect result (H134).

    This exists because the board, `ar budget` and the coordinator's stop
    decision each had their own copy of this loop and all three hardcoded `<`.
    A `direction: maximise` domain was therefore shown its *worst* row as its
    best, and the coordinator fed that row to `should_stop`, so the goal-met
    exit could never be reached.
    """
    best = None
    for record in records:
        if record.status != OK:
            continue
        try:
            value = goal.objective_value(record.metrics)
        except AutoresearchError:
            continue
        if best is None or goal.better(value, best[0]):
            best = (value, record)
    return best


def read_all(directory, strict: bool = False) -> list[RunRecord]:
    """Every core record under `directory`.

    A corpus reader that skips a malformed row silently is how a corpus grows
    rows nobody can explain -- so `strict=True` raises with the file and line
    named. But a domain adopting this core arrives with a corpus written to its
    own older schema (this one had 9,443 such rows), and refusing to start until
    every historical row is rewritten is not a real option. So the default is to
    read what is ours and **report what was not**: use `read_with_skipped` when
    the count matters, which is anywhere it is displayed.
    """
    return read_with_skipped(directory, strict)[0]


def read_with_skipped(directory, strict: bool = False) -> tuple[list, int]:
    """`(records, skipped)`. The count is never dropped on the floor: a reader
    that says "12 rows" when it saw 9,455 is the ambiguity H81/H96 are about."""
    directory = pathlib.Path(directory)
    if not directory.exists():
        return [], 0
    out, skipped = [], 0
    for path in sorted(directory.glob("*.jsonl")):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SchemaError(f"{path}:{lineno}: {exc}") from exc
            if not strict and payload.get("schema") != SCHEMA:
                skipped += 1        # a foreign row: not ours to interpret
                continue
            try:
                out.append(RunRecord.from_dict(payload))
            except (SchemaError, TypeError) as exc:
                if strict:
                    raise SchemaError(f"{path}:{lineno}: {exc}") from exc
                skipped += 1
    return out, skipped
