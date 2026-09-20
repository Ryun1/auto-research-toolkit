"""Pointer-preserving relocation of oversized inbox evidence (QSB H32).

`ar validate` warns on every inbox file past 64 KiB with the standing advice
that terminal evidence belongs in `data/artifacts/` -- but until now the core
offered no way to act on the warning. A bare `mv` dangles every recorded
pointer: closed entries' `result.memo` and open entries' `sources` fields cite
the inbox path, the generated views repeat them, and `ar validate` then fails
on a memo that no longer exists (the H74/H117 failure class the close gate
exists to prevent).

So the move is a state surgery with one writer, like every other mutation:
check every refusal up front, move the file, rewrite each recorded pointer
through the entry store (atomic replace, history event per entry), regenerate
the views. Closed entries are rewritten too -- the verdict's evidence memo
changes location, never content, and the history event keeps that accountable.
"""
from __future__ import annotations

import pathlib
import shutil

from . import render
from .entries import Event, Store, _now
from .errors import AutoresearchError


def _normalize(pointer: str) -> str:
    return str(pathlib.PurePosixPath(pointer.strip())).lstrip("./")


def _under(path: pathlib.Path, ancestor: pathlib.Path) -> bool:
    try:
        path.relative_to(ancestor)
        return True
    except ValueError:
        return False


def relocate(config, source: str, dest: str | None, session: str, why: str) -> dict:
    """Move one inbox file into the findings lane and rewrite its pointers."""
    if not why or not why.strip():
        raise AutoresearchError(
            "relocation is a state surgery on recorded pointers; pass --why")
    root = config.paths.root
    src = (root / source).resolve()
    if not src.exists():
        raise AutoresearchError(f"no such file: {source}")
    if not src.is_file():
        raise AutoresearchError(f"not a regular file: {source}")
    if not _under(src, config.paths.memos.resolve()):
        raise AutoresearchError(
            f"{src.relative_to(root)} is not under the inbox "
            f"({config.paths.memos}); only handoff-queue evidence is relocated "
            "-- findings-lane paths need no relocation")
    if dest is None:
        target = root / "data" / "artifacts" / "inbox" / src.name
    else:
        target = (root / dest).resolve()
        if not _under(target, root.resolve()):
            raise AutoresearchError(f"{dest} escapes the domain root")
    if target.exists():
        raise AutoresearchError(f"destination exists: {target.relative_to(root)}")
    if _under(target, config.paths.memos.resolve()):
        raise AutoresearchError(
            "destination is still inside the inbox; relocation that stays in "
            "the handoff queue re-runs the same warning on every validate")
    if src == target:
        raise AutoresearchError("source and destination are the same file")

    store = Store(config.paths.entries)
    entries = store.all()
    old_rel = _normalize(src.relative_to(root).as_posix())
    new_rel = _normalize(target.relative_to(root).as_posix())
    touched: list[tuple[object, list[str]]] = []
    pointer_count = 0
    for entry in entries:
        fields: list[str] = []
        if old_rel in [_normalize(p) for p in entry.sources]:
            entry.sources = [new_rel if _normalize(p) == old_rel else p
                             for p in entry.sources]
            fields.append("sources")
        if entry.result is not None and _normalize(entry.result.memo) == old_rel:
            entry.result.memo = new_rel
            fields.append("result.memo")
        if fields:
            pointer_count += len(fields)
            touched.append((entry, fields))

    # Past every refusal: move, then rewrite, then re-render. Each entry save
    # is an atomic replace, so a crash mid-rewrite leaves whole records --
    # worst case the file moved while some pointers still cite the old path,
    # which `ar validate` names loudly (a missing memo) rather than silently.
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(target))
    for entry, fields in touched:
        entry.history.append(Event(
            _now(), "relocate", session,
            f"{old_rel} -> {new_rel} ({', '.join(fields)}): {why}"))
        entry.updated = _now()
        store.save(entry)
    render.write_views(config, store.all())
    return {"from": old_rel, "to": new_rel,
            "pointers": pointer_count,
            "entries": sorted(e.id for e, _ in touched)}


def _command(args):
    from .cli import _load
    config = _load(args)
    result = relocate(config, args.file, args.to, args.session, args.why)
    print(f"moved {result['from']} -> {result['to']}")
    print(f"rewrote {result['pointers']} pointer(s) across "
          f"{len(result['entries'])} entry(ies): "
          + (", ".join(result["entries"]) if result["entries"] else "none"))
    return 0


def register_parser(subparsers):
    parser = subparsers.add_parser(
        "relocate",
        help="move oversized inbox evidence into the findings lane, "
             "rewriting every recorded pointer")
    parser.add_argument("file", help="path under inbox/, relative to the domain root")
    parser.add_argument("--to",
                        help="destination relative to the domain root "
                             "(default: data/artifacts/inbox/<name>)")
    parser.add_argument("--why", required=True,
                        help="a relocation without a reason is an untraceable "
                             "state surgery")
    parser.set_defaults(func=_command)
