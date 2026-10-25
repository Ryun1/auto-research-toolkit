"""The evaluator a run is scored by, and the receipt that says core scored it.

#36. A worker measured with `ar --domain <slot> measure`, so the command,
the `[policy]` it was checked against and the script that produced the number
were all files in the worker's own workspace. The script hashed itself into
`provenance.measure_sha`, so an edited script reported whatever hash it
liked. And harvest admitted any schema-valid line under the slot's runs
directory, so a row nobody measured read as evidence. METR's o3 runs patched
`time.time`, monkey-patched the grader and overrode `__eq__`; a loop that
optimises hard finds the scorer if the scorer is in reach.

So a candidate is measured from the primary checkout (`ar measure
--candidate <slot>`): the command, policy and evaluator files come from the
primary, the candidate tree from the slot, and the slot's copies of the
evaluator must be byte-identical to the primary's. Core hashes the evaluator
itself, and writes a receipt for each row it records into coordinator state
outside the slot. Harvest admits a row only against a matching receipt.

Not a sandbox: a worker that can write anywhere on the machine can write a
receipt. This closes the in-workspace edits, which are the ones a worker
makes without leaving the directory it was handed.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re

from .errors import AutoresearchError, ConfigError


def files(config) -> list[str]:
    """The evaluator: `[evaluator] files`, or by default every token of the
    measure command's script: its first token when that names a file in the
    domain root (`bin/measure ...`), else its second (`python bin/m.py ...`).
    Later tokens are arguments -- often the candidate's own files -- and
    are never taken for the evaluator (review of #36)."""
    if config.evaluator_files is not None:
        return list(config.evaluator_files)
    return [script] if (script := script_token(config.paths.root,
                                               config.commands.get("measure", ""))) else []


def script_token(root, command: str) -> str | None:
    """The token of `command` that names its script in `root`, if any."""
    for token in command.split()[:2]:
        if _inside(root, token) and (pathlib.Path(root) / token).is_file():
            return token
    return None


def _inside(root, name) -> bool:
    """Lexically inside `root`. Not resolved: a script symlinked in from a
    shared location is still the domain's evaluator, and resolving it made
    it "outside" and left it to run from the slot (review of #36)."""
    path = pathlib.Path(name)
    return bool(path.parts) and not path.is_absolute() and ".." not in path.parts


def _file_digest(path) -> str:
    """A file's bytes, or a directory's names and bytes, as one digest."""
    path = pathlib.Path(path)
    try:
        if path.is_dir():
            digest = hashlib.sha256()
            for child in sorted(p for p in path.rglob("*") if p.is_file()):
                digest.update(f"{child.relative_to(path).as_posix()}\0"
                              f"{_file_digest(child)}\n".encode())
            return digest.hexdigest()
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"


def identity(config, root=None) -> str:
    """One hash over the evaluator files, read from `root` (default: the
    domain root). Names are part of it, so moving a file is a change."""
    root = pathlib.Path(root or config.paths.root)
    digest = hashlib.sha256()
    for name in sorted(files(config)):
        digest.update(f"{name}\0{_file_digest(root / name)}\n".encode())
    return digest.hexdigest()[:16]


def drift(config, candidate) -> list[str]:
    """Evaluator files whose bytes in `candidate` differ from the primary's."""
    return [name for name in sorted(files(config))
            if _file_digest(pathlib.Path(candidate) / name)
            != _file_digest(config.paths.root / name)]


def command(config) -> list[str]:
    """The measure command with every token naming a file in the primary root
    made absolute, so it runs the primary's copy from inside a candidate."""
    root = config.paths.root
    protected = set(files(config))
    return [str(root / token) if token in protected else token
            for token in config.command("measure").split()]


def candidate(config, path) -> pathlib.Path:
    """A candidate tree is a workspace this domain's pool holds; anywhere else
    is a directory the coordinator never handed out."""
    path = pathlib.Path(path).resolve()
    pool = config.paths.workspaces.resolve()
    if not path.is_dir() or not path.is_relative_to(pool) or path == pool:
        raise AutoresearchError(
            f"--candidate {path} is not a workspace under {pool}; a candidate "
            "is a slot the coordinator handed out")
    return path


def _row_digest(record) -> str:
    return hashlib.sha256(json.dumps(record.to_dict(), sort_keys=True)
                          .encode()).hexdigest()


#: A run id names its receipt file, and the run id is written by the measure
#: script -- worker-controlled bytes. `../workspaces/<slot>/x` would read a
#: receipt from inside the slot, one the worker forged. One path component,
#: no dots-only names.
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _receipt_path(directory, run_id) -> pathlib.Path | None:
    directory = pathlib.Path(directory)
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        return None
    path = directory / f"{run_id}.json"
    if not path.resolve().is_relative_to(directory.resolve()):
        return None
    return path


def write_receipt(directory, record, candidate_path) -> pathlib.Path:
    """One file per row, created exclusively: a run id is receipted once."""
    directory = pathlib.Path(directory)
    path = _receipt_path(directory, record.id)
    if path is None:
        raise AutoresearchError(
            f"run id {record.id!r} is not a plain name (letters, digits, "
            "`.`, `_`, `-`); it cannot be receipted")
    directory.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"run": record.id, "digest": _row_digest(record),
                       "session": record.session,
                       "candidate": str(candidate_path)}, sort_keys=True)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(fd, "w") as fh:
        fh.write(body + "\n")
    return path


def receipt_problem(directory, record, workspace=None) -> str | None:
    """Why `record` is not a row `ar measure` recorded, or None if it is.
    With `workspace`, the receipt must also name that slot: a receipted line
    copied into another slot's runs file is not that slot's measurement."""
    path = _receipt_path(directory, record.id)
    if path is None:
        return (f"run id {record.id!r} is not a plain name, so it names no "
                "receipt; a row like this was not recorded by `ar measure`")
    try:
        receipt = json.loads(path.read_text())
    except (OSError, ValueError):
        return (f"row {record.id!r} has no measurement receipt: it was not "
                "recorded by `ar measure --candidate` from the primary checkout")
    if receipt.get("digest") != _row_digest(record):
        return (f"row {record.id!r} does not match its receipt: it was "
                "changed after `ar measure` recorded it")
    if workspace is not None and (
            pathlib.Path(str(receipt.get("candidate", ""))).resolve()
            != pathlib.Path(workspace).resolve()):
        return (f"row {record.id!r} was measured in another workspace "
                f"({receipt.get('candidate')}), not this one")
    return None


def has_receipt(config, record) -> bool:
    return receipt_problem(config.paths.receipts, record) is None


def may_edit(config, entry_id) -> bool:
    """Harness debt may change the evaluator: that is what fixing a scorer
    looks like. Every other entry is scored by it, so may not."""
    if not entry_id:
        return False
    try:
        return config.track_for(entry_id).requires_defect_evidence
    except ConfigError:
        return False
