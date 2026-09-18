"""Domain-owned session lifecycle: one worktree per agent session.

The coordinator's `Pool` (workspaces.py) owns workspaces for the length of one
iteration and tears them down in its own `finally`. A *session* is the other
lifetime the field harness converged on: a worktree an agent lives in across
many iterations, created and destroyed by a person (or a loop tick) that is not
the coordinator. That lifecycle stayed domain-owned in the field (the hr-iter
clone's `domain.toml` records the H147 ownership decision: the settle-window
pruning and worktree lifecycle were load-bearing domain rules the core had no
verb for). This module is that verb, upstreamed with the lessons the field paid
for:

* **Harvest before destroy.** A session worktree once vanished taking its
  uncommitted run rows with it. `destroy` therefore scans the worktree's
  evidence -- run rows, entries, memos -- and refuses, naming what it found,
  BEFORE any removal. Nothing is deleted that the primary does not already
  hold, unless the caller explicitly asks for the rows to be appended to the
  primary's lanes first.
* **Config drift is refused.** The hr-iter-0902 clone edited `domain.toml`
  (its `[state] runs` moved to a projection path) while sessions were live, and
  a ledger split across the two run paths. So `create` pins the sha256 of
  `domain.toml`, and `destroy` refuses a drifted config unless the drift is
  accepted by name (`accept_drift=True`) -- a human decision, like the
  workspace recovery receipt's `confirm_inactive`.
* **Prune proposes; destroy disposes.** `prune` only reports: idle hours
  against the settle window, whether the worktree still exists, whether it is
  clean, and how much evidence is unharvested. Every destruction goes through
  `destroy`, whose guards are the only removal path (the `settle_hours`
  docstring in config.py promises exactly this split).
* **Duplicate creates are refused, never attached** (the upstream H70 lesson,
  shared with `workspaces.Pool`): two sessions on one worktree share one index.

Records live under `<root>/.ar/sessions/<name>.json`; destroying one archives
the record under `.ar/sessions/archived/` (the workspace-recover pattern:
evidence is archived, never silently deleted).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import re
import shutil
import subprocess

from . import runs
from .config import CONFIG_NAME
from .errors import ConfigError, SchemaError
from .workspaces import is_git_repo

#: A session name becomes one path component beside the domain root, so it is
#: deliberately stricter than the workspace pool's: lowercase slugs only.
SLUG = re.compile(r"[a-z0-9][a-z0-9-]*")
MAX_NAME = 48


def _git(root, *args) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", *args], cwd=str(root), capture_output=True,
                          text=True)
    if proc.returncode != 0:
        raise ConfigError(
            f"git {' '.join(args)} failed in {root}: {proc.stderr.strip()}")
    return proc


def _check_name(name) -> None:
    if not isinstance(name, str) or len(name) > MAX_NAME \
            or not SLUG.fullmatch(name):
        raise ConfigError(
            f"session name {name!r} must be a lowercase slug matching "
            f"[a-z0-9][a-z0-9-]* of at most {MAX_NAME} characters")


def _sessions_dir(root):
    return root / ".ar" / "sessions"


def _record_path(root, name):
    return _sessions_dir(root) / f"{name}.json"


def _session_parent(config, root):
    raw = config.session_parent
    if not raw:
        return root.parent
    path = pathlib.Path(raw).expanduser()
    return path if path.is_absolute() else root / path


def _load(config, name) -> tuple:
    """The record on disk, refusing a missing or unreadable one by name."""
    _check_name(name)
    path = _record_path(config.paths.root, name)
    try:
        record = json.loads(path.read_text())
    except FileNotFoundError:
        raise ConfigError(
            f"no session record at {path}. `ar session create {name}` first, "
            "or the session was already destroyed (its archive is under "
            f"{_sessions_dir(config.paths.root) / 'archived'}).") from None
    except ValueError as exc:
        raise ConfigError(
            f"session record {path} is not valid JSON: {exc}") from exc
    if not isinstance(record, dict) or record.get("name") != name:
        raise ConfigError(f"session record {path} does not name session {name!r}")
    return path, record


def _read_lane(path) -> tuple[list, int]:
    """`(rows, skipped)` for one lane file, under the reader rules
    `runs.read_with_skipped` applies. That reader is directory-granular and the
    refusal must name the file, so this applies the same rules to one file
    rather than a second format."""
    rows, skipped = [], 0
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return [], 1
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if not isinstance(payload, dict) or payload.get("schema") != runs.SCHEMA:
            skipped += 1        # a foreign row: not ours to interpret
            continue
        try:
            rows.append(runs.RunRecord.from_dict(payload))
        except (SchemaError, TypeError):
            skipped += 1
    return rows, skipped


def _scan(config, record) -> dict:
    """Divergence between a session worktree and the primary: unseen run rows
    per lane file, entries the primary lacks, entries that differ, and memo
    divergence. Read-only -- prune scans with it too."""
    root = config.paths.root
    wt = pathlib.Path(record["path"])
    result = {"runs": [], "entries_absent": [], "entries_differ": [],
              "memos": [], "skipped": []}

    primary_ids = {r.id for r in runs.read_all(config.paths.runs)}
    runs_rel = config.paths.runs.relative_to(root)
    wt_runs = wt / runs_rel
    for path in sorted(wt_runs.glob("*.jsonl")) if wt_runs.is_dir() else []:
        rows, skipped = _read_lane(path)
        unseen = [r for r in rows if r.id not in primary_ids]
        if skipped:
            result["skipped"].append(
                {"file": str(path.relative_to(wt)), "rows": skipped})
        if unseen:
            result["runs"].append({"file": str(path.relative_to(wt)),
                                   "rows": len(unseen), "records": unseen})
    entries_rel = config.paths.entries.relative_to(root)
    wt_entries = wt / entries_rel
    for path in sorted(wt_entries.glob("*.yaml")) if wt_entries.is_dir() else []:
        rel = str(path.relative_to(wt))
        primary = root / rel
        if not primary.exists():
            result["entries_absent"].append(rel)
        elif primary.read_bytes() != path.read_bytes():
            result["entries_differ"].append(rel)

    memos_rel = config.paths.memos.relative_to(root)
    wt_memos = wt / memos_rel
    if wt_memos.is_dir():
        for path in sorted(p for p in wt_memos.rglob("*") if p.is_file()):
            primary = config.paths.memos / path.relative_to(wt_memos)
            if not primary.exists() or primary.read_bytes() != path.read_bytes():
                result["memos"].append(str(path.relative_to(wt_memos)))
    return result


def create(config, name) -> dict:
    """Create a session worktree and its record; return the record.

    Refuses, in order: a non-slug name, a non-git domain root (a session is a
    git worktree, so there is nothing to create without one), and any reuse of
    an existing worktree path or record -- reusing one would give two sessions
    one index, the H70 failure the workspace pool already refuses.
    """
    root = config.paths.root
    _check_name(name)
    if not is_git_repo(root):
        raise ConfigError(
            f"domain root {root} is not a git repository; a session is a git "
            "worktree, so there is nothing to create. Commit the domain into "
            "git first.")
    parent = _session_parent(config, root)
    worktree = parent / f"{root.name}-session-{name}"
    record_path = _record_path(root, name)
    if worktree.exists() or worktree.is_symlink():
        raise ConfigError(
            f"a worktree for session {name!r} already exists at {worktree}. "
            "Reusing it would give two sessions one index (H70); pick another "
            "name, or destroy the existing session first.")
    if record_path.exists():
        raise ConfigError(
            f"a record for session {name!r} already exists at {record_path}; "
            "two sessions on one name share one index (H70). Pick another "
            "name, or destroy the existing session.")

    domain_toml = root / CONFIG_NAME
    base = _git(root, "rev-parse", "HEAD").stdout.strip()
    parent.mkdir(parents=True, exist_ok=True)
    _git(root, "worktree", "add", str(worktree), "HEAD")
    record = {
        "name": name,
        "path": str(worktree),
        "base": base,
        "config_sha256": hashlib.sha256(domain_toml.read_bytes()).hexdigest(),
        "created_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "settle_hours": float(config.session_settle_hours),
    }
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    return record


def destroy(config, name, *, harvest: bool = False,
            accept_drift: bool = False) -> dict:
    """Destroy a session worktree, archiving its record. Never destroys
    silently: every check names what it found before anything is removed.

    Order matters: all refusal checks run before any write, so a refused
    destroy leaves the primary untouched and a harvest that later hits a
    harder refusal has only appended rows the next attempt will dedupe by id.
    """
    root = config.paths.root
    record_path, record = _load(config, name)

    current = root / CONFIG_NAME
    digest = (hashlib.sha256(current.read_bytes()).hexdigest()
              if current.exists() else "")
    if digest != record.get("config_sha256"):
        if not accept_drift:
            raise ConfigError(
                f"refusing to destroy session {name!r}: {CONFIG_NAME} changed "
                f"since the session was created (recorded sha256 "
                f"{record.get('config_sha256')}, now {digest}). A drifted "
                "config splits a ledger across two run paths -- the hr-iter "
                "lesson. Re-run with --accept-drift if the drift is "
                "understood and the session's evidence is not on the moved "
                "path.")

    worktree = pathlib.Path(record["path"])
    scan = _scan(config, record)

    problems = []
    for item in scan["skipped"]:
        problems.append(
            f"{item['file']}: {item['rows']} row(s) the core reader does not "
            "recognise -- destruction would lose them uninterpreted; reconcile "
            "the file by hand first")
    for item in scan["entries_differ"]:
        problems.append(
            f"{item} exists in the worktree and the primary with different "
            "contents; harvest never overwrites the primary -- reconcile it by "
            "hand (`ar entry amend`), then destroy again")
    for item in scan["memos"]:
        problems.append(
            f"memo {item} diverges from the primary; memos are never "
            "auto-harvested -- fold it into the primary by hand, then destroy")
    if harvest and scan["runs"]:
        for item in scan["runs"]:
            target = root / item["file"]     # the same lane, in the primary
            for row in item["records"]:
                runs.append(target, row)     # append-only, deduped by id upstream
    if not harvest:
        for item in scan["runs"]:
            problems.append(
                f"{item['file']}: {item['rows']} run row(s) absent from the "
                "primary ledger -- a vanished worktree once took its rows with "
                "it; re-run with harvest=True to append them to the primary's "
                "lanes")
        for item in scan["entries_absent"]:
            problems.append(
                f"{item} exists only in the worktree; re-run with "
                "harvest=True to copy it to the primary")
    if problems:
        raise ConfigError(
            f"refusing to destroy session {name!r}: the worktree holds "
            "evidence the primary does not have --\n  - "
            + "\n  - ".join(problems))

    for item in scan["entries_absent"]:
        target = root / item
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(worktree / item, target)

    worktree_was_present = worktree.is_dir()
    if worktree_was_present:
        # --force: the divergence guards above are the safety, and a session
        # whose rows were just harvested is dirty by construction.
        _git(root, "worktree", "remove", "--force", str(worktree))
    _git(root, "worktree", "prune")

    archived_dir = _sessions_dir(root) / "archived"
    archived_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S.%f")
    archived = {**record, "destroyed_at":
                dt.datetime.now(dt.UTC).isoformat(timespec="seconds")}
    archived_path = archived_dir / f"{name}.{stamp}.json"
    archived_path.write_text(json.dumps(archived, indent=2, sort_keys=True) + "\n")
    record_path.unlink()

    return {
        "name": name,
        "destroyed": True,
        "worktree": str(worktree),
        "worktree_present": worktree_was_present,
        "harvested_runs": [{"file": item["file"], "rows": item["rows"]}
                           for item in scan["runs"] if harvest],
        "harvested_entries": list(scan["entries_absent"]),
        "archived": str(archived_path),
    }


def prune(config) -> list[dict]:
    """Report every session's teardown readiness. Proposes, destroys nothing:
    destruction is `destroy`'s job and its guards are the only removal path."""
    root = config.paths.root
    sessions = _sessions_dir(root)
    if not sessions.is_dir():
        return []
    now = dt.datetime.now(dt.UTC)
    out = []
    for path in sorted(sessions.glob("*.json")):
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise ConfigError(
                f"cannot read session record {path}: {exc}") from exc
        name = record.get("name", path.stem)
        try:
            created = dt.datetime.fromisoformat(record["created_at"])
            if created.tzinfo is None:
                created = created.replace(tzinfo=dt.UTC)
        except (KeyError, ValueError) as exc:
            raise ConfigError(
                f"session record {path} has no readable created_at: {exc}"
            ) from exc
        idle_hours = (now - created).total_seconds() / 3600.0
        settle_hours = float(record.get("settle_hours",
                                        config.session_settle_hours))
        worktree = pathlib.Path(record.get("path", ""))
        exists = worktree.is_dir()
        clean, unharvested, blockers = False, 0, []
        if not exists:
            blockers.append("worktree is already gone")
        else:
            clean = _git(worktree, "status", "--porcelain").stdout.strip() == ""
            if not clean:
                blockers.append("worktree is dirty")
            scan = _scan(config, record)
            unharvested = (sum(item["rows"] for item in scan["runs"])
                           + len(scan["entries_absent"]))
            if unharvested:
                blockers.append(f"{unharvested} unharvested item(s)")
            for item in scan["entries_differ"]:
                blockers.append(f"{item} differs from the primary")
            for item in scan["memos"]:
                blockers.append(f"memo {item} diverges from the primary")
            for item in scan["skipped"]:
                blockers.append(f"{item['file']}: {item['rows']} row(s) the "
                                "core reader does not recognise")
        eligible = exists and clean and unharvested == 0 \
            and idle_hours >= settle_hours and not blockers
        out.append({"name": name, "idle_hours": round(idle_hours, 4),
                    "settle_hours": settle_hours, "worktree_exists": exists,
                    "clean": clean, "unharvested": unharvested,
                    "eligible": eligible, "blockers": blockers})
    return out
