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
import hashlib
import json
import os
import pathlib
import shutil
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
    def from_dict(cls, data: dict) -> RunRecord:
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
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            lineno = exc.object.count(b"\n", 0, exc.start) + 1
            raise SchemaError(f"{path}:{lineno}: {exc}") from exc
        for lineno, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SchemaError(f"{path}:{lineno}: {exc}") from exc
            if not isinstance(payload, dict):
                if strict:
                    raise SchemaError(f"{path}:{lineno}: run record must be a JSON object")
                skipped += 1
                continue
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


def snapshot(directory) -> dict:
    """Remember inherited bytes, not just IDs: workers may only append evidence."""
    directory = pathlib.Path(directory)
    return {p.relative_to(directory): (p.stat().st_size, _digest(p))
            for p in directory.rglob("*.jsonl")}


def _digest(path, size=None):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while size is None or size > 0:
            chunk = stream.read(65536 if size is None else min(65536, size))
            if not chunk:
                break
            digest.update(chunk)
            if size is not None:
                size -= len(chunk)
    return digest.digest()


def retain_output(workspace, root, name, lanes, protected=()) -> None:
    """Copy declared findings without following links or replacing owned files."""
    relative = pathlib.Path(name)
    if (relative.is_absolute() or ".." in relative.parts or not relative.parts
            or lanes.classify(relative.as_posix()) != "findings"):
        raise SchemaError(f"unsafe or non-findings output {name!r}")
    source, target = workspace / relative, root / relative
    if any(target.resolve().is_relative_to(path.resolve())
           or path.resolve().is_relative_to(target.resolve()) for path in protected):
        raise SchemaError(f"output overlaps coordinator state: {name!r}")
    for base, path in ((workspace, source), (root, target)):
        if not path.resolve().is_relative_to(base.resolve()):
            raise SchemaError(f"output escapes owned root: {path}")
        current = base
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise SchemaError(f"symlink output is not owned: {current}")
    if source.is_dir():
        for child in sorted(source.iterdir()):
            retain_output(workspace, root,
                          child.relative_to(workspace).as_posix(), lanes, protected)
        return
    if not source.is_file():
        raise SchemaError(f"declared output is missing or not a file: {source}")
    if target.exists():
        if not target.is_file() or _digest(source) != _digest(target):
            raise SchemaError(f"output collision at {target}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation: a second worker must never replace the first's audit.
    with source.open("rb") as incoming, target.open("xb") as outgoing:
        shutil.copyfileobj(incoming, outgoing)


@dataclass
class Harvest:
    records: list[RunRecord] = field(default_factory=list)
    consumed: int = 0
    problems: list[str] = field(default_factory=list)


def harvest(directory, before, destination, *, workspace, root, goal, lanes,
            protected=()) -> Harvest:
    """Retain appended records and their declared local outputs before teardown.

    Bad rows stay in the workspace for audit, never become manufactured records.
    Callers serialize harvests and retain the workspace when problems are present.
    """
    directory, destination = pathlib.Path(directory), pathlib.Path(destination)
    result = Harvest()
    existing = {}
    for record in read_all(destination):
        if record.id in existing and existing[record.id] != record.to_dict():
            raise SchemaError(f"conflicting existing run ID {record.id!r}")
        existing[record.id] = record.to_dict()
    paths = {p.relative_to(directory): p for p in directory.rglob("*.jsonl")}
    for relative in before.keys() - paths.keys():
        result.problems.append(f"inherited run file disappeared: {relative}")
    for relative, path in sorted(paths.items()):
        size, digest = before.get(relative, (0, None))
        if (not path.resolve().is_relative_to(workspace.resolve())
                or path.is_symlink()):
            result.problems.append(f"run file escapes owned workspace: {path}")
            continue
        if digest is not None and (path.stat().st_size < size
                                   or _digest(path, size) != digest):
            result.problems.append(f"inherited run file was rewritten: {path}")
            continue
        with path.open("rb") as stream:
            stream.seek(size)
            for lineno, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                result.consumed += 1
                try:
                    payload = json.loads(line)
                    if not isinstance(payload, dict) or not {
                            "id", "schema", "session", "metrics", "provenance"
                    } <= payload.keys():
                        raise SchemaError("missing explicit run identity or evidence fields")
                    record = RunRecord.from_dict(payload)
                    if not isinstance(record.id, str) or not record.id:
                        raise SchemaError("run ID must be a nonempty string")
                    if (not isinstance(record.metrics, dict)
                            or not isinstance(record.provenance, dict)
                            or not isinstance(record.session, str)):
                        raise SchemaError("metrics/provenance must be objects and session a string")
                    record.validate(goal)
                    if not isinstance(record.outputs, list) or not all(
                            isinstance(name, str) for name in record.outputs):
                        raise SchemaError("outputs must be a list of local paths")
                    if record.id in existing:
                        if existing[record.id] != record.to_dict():
                            raise SchemaError(f"run ID collision: {record.id!r}")
                        continue
                    for name in record.outputs:
                        retain_output(workspace, root, name, lanes, protected)
                    destination.mkdir(parents=True, exist_ok=True)
                    target = destination / f"harvest-{uuid.uuid4().hex}.jsonl"
                    temporary = target.with_suffix(".tmp")
                    try:
                        temporary.write_text(json.dumps(record.to_dict(), sort_keys=True) + "\n")
                        os.link(temporary, target)
                    finally:
                        temporary.unlink(missing_ok=True)
                    existing[record.id] = record.to_dict()
                    result.records.append(record)
                except (OSError, ValueError, TypeError, AttributeError, SchemaError) as exc:
                    result.problems.append(f"{path}: appended row {lineno}: {exc}")
    return result
