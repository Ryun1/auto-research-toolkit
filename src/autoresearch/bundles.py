"""Portable evidence bytes, not authentication or measurement eligibility.

A bundle records hashes of files as they were read while packing. Source
snapshots are optional context, never an assertion of what executed. Importing
retains files only: it neither executes a verifier nor creates run records.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import stat
import tempfile
import zipfile
import zlib
from contextlib import contextmanager

from .claims import Lock
from .errors import SchemaError
from .runs import validate_output_path

SCHEMA = "ar-evidence-1"
MANIFEST = "manifest.json"
ASSURANCE = "integrity-only; not trusted origin, executed identity, or ranked eligibility"


def _relative(name):
    if (not isinstance(name, str) or not name or "\\" in name or ":" in name
            or "\x00" in name or any(part in ("", ".", "..") for part in name.split("/"))
            or pathlib.PurePosixPath(name).is_absolute()):
        raise SchemaError(f"unsafe bundle path: {name!r}")
    return pathlib.PurePosixPath(name)


def _canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SchemaError(f"bundle metadata must be finite JSON: {exc}") from exc


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SchemaError(f"duplicate manifest key: {key!r}")
        result[key] = value
    return result


def _manifest(data):
    try:
        value = json.loads(data, object_pairs_hook=_object)
    except (ValueError, UnicodeError) as exc:
        raise SchemaError(f"invalid bundle manifest: {exc}") from exc
    if (not isinstance(value, dict) or set(value) - {"schema", "assurance", "files", "metadata"}
            or value.get("schema") != SCHEMA or value.get("assurance") != ASSURANCE
            or not isinstance(value.get("files"), list) or not value["files"]
            or ("metadata" in value and not isinstance(value["metadata"], dict))):
        raise SchemaError("invalid bundle manifest structure or schema")
    paths = set()
    for entry in value["files"]:
        if not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256"}:
            raise SchemaError("invalid bundle file declaration")
        path = _relative(entry["path"])
        if len(path.parts) < 2 or path.parts[0] not in ("evidence", "source"):
            raise SchemaError(f"invalid bundle file namespace: {path}")
        digest = entry["sha256"]
        if (type(entry["size"]) is not int or entry["size"] < 0
                or not isinstance(digest, str) or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)):
            raise SchemaError(f"invalid size or sha256 for {path}")
        if str(path) in paths:
            raise SchemaError(f"duplicate manifest path: {path}")
        paths.add(str(path))
    if not any(path.startswith("evidence/") for path in paths):
        raise SchemaError("bundle must contain explicit evidence, not only source snapshots")
    for name in paths:
        if any(str(parent) in paths for parent in pathlib.PurePosixPath(name).parents):
            raise SchemaError(f"bundle file/directory conflict: {name}")
    return value


def _identity(manifest):
    return hashlib.sha256(_canonical(manifest)).hexdigest()


@contextmanager
def _archive(path):
    try:
        with zipfile.ZipFile(path, "r") as archive:
            yield archive
    except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, NotImplementedError,
            EOFError, OSError, zlib.error) as exc:
        raise SchemaError(f"cannot read evidence archive: {exc}") from exc


def _inspect(archive, staging=None):
    """Validate every member, optionally spooling bytes outside the domain."""
    members = {}
    for member in archive.infolist():
        name = member.filename
        _relative(name)
        mode = member.external_attr >> 16
        if (member.orig_filename != name or name in members or member.is_dir()
                or stat.S_IFMT(mode) not in (0, stat.S_IFREG)
                or member.flag_bits & 1
                or member.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
            raise SchemaError(f"unsafe or duplicate archive member: {name!r}")
        members[name] = member
    if MANIFEST not in members or members[MANIFEST].file_size > 8 * 1024 * 1024:
        raise SchemaError("missing or oversized bundle manifest")
    manifest = _manifest(archive.read(members[MANIFEST]))
    declared = {entry["path"]: entry for entry in manifest["files"]}
    if set(members) != {MANIFEST, *declared}:
        raise SchemaError("archive members do not match manifest declarations")
    for name, entry in declared.items():
        if members[name].file_size != entry["size"]:
            raise SchemaError(f"size mismatch: {name}")
        digest, size = hashlib.sha256(), 0
        target = None
        if staging is not None:
            path = staging / name
            path.parent.mkdir(parents=True, exist_ok=True)
            target = path.open("xb")
        try:
            with archive.open(members[name]) as stream:
                while chunk := stream.read(65536):
                    size += len(chunk)
                    if size > entry["size"]:
                        raise SchemaError(f"size mismatch: {name}")
                    digest.update(chunk)
                    if target is not None:
                        target.write(chunk)
        finally:
            if target is not None:
                target.close()
        if size != entry["size"] or digest.hexdigest() != entry["sha256"]:
            raise SchemaError(f"digest mismatch: {name}")
    if staging is not None:
        (staging / MANIFEST).write_bytes(_canonical(manifest))
    return manifest


def verify(archive) -> dict:
    """Verify exact declared bytes; the returned ID does not authenticate origin."""
    with _archive(archive) as opened:
        manifest = _inspect(opened)
    return {"id": _identity(manifest), "manifest": manifest, "assurance": ASSURANCE}


def _source(root, relative):
    path = root
    for part in _relative(relative).parts:
        path /= part
        if path.is_symlink():
            raise SchemaError(f"symlink evidence is not owned: {path}")
    if not path.resolve().is_relative_to(root.resolve()):
        raise SchemaError(f"evidence escapes root: {relative}")
    return path


def _files(root, names, namespace):
    def walk(name):
        path = _source(root, name)
        if path.is_dir():
            for child in sorted(path.iterdir()):
                yield from walk(child.relative_to(root).as_posix())
        elif path.is_file():
            yield f"{namespace}/{name}", path
        else:
            raise SchemaError(f"evidence is missing or not a regular file: {path}")
    for name in names:
        path = pathlib.Path(name)
        if path.is_absolute():
            # Resolve only the root prefix (e.g. macOS /var -> /private/var),
            # never descendants: resolving the file first hides symlinks.
            for ancestor in reversed(path.parents):
                if ancestor.resolve() == root:
                    path = path.relative_to(ancestor)
                    break
            else:
                raise SchemaError(f"evidence escapes root: {name}")
        yield from walk(path.as_posix())


def pack(root, paths, archive, *, sources=(), metadata=None) -> dict:
    """Pack explicit evidence paths inside root and optional source snapshots.

    Directories include all regular descendants. No links are followed. Output
    is a deterministic ZIP, published without replacing an existing archive.
    """
    root, archive = pathlib.Path(root).resolve(), pathlib.Path(archive).absolute()
    if metadata is not None and not isinstance(metadata, dict):
        raise SchemaError("bundle metadata must be a JSON object")
    selected = sorted([*_files(root, paths, "evidence"),
                       *_files(root, sources, "source")])
    if len({name for name, _ in selected}) != len(selected):
        raise SchemaError("duplicate evidence paths (including overlapping directories)")
    if any(path.resolve() == archive.resolve() for _, path in selected):
        raise SchemaError("archive cannot include itself")
    if not any(name.startswith("evidence/") for name, _ in selected):
        raise SchemaError("at least one evidence file is required")
    manifest = {"schema": SCHEMA, "assurance": ASSURANCE, "files": []}
    if metadata is not None:
        manifest["metadata"] = metadata
    _canonical(manifest)
    archive.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".evidence-", dir=archive.parent)
    os.close(fd)
    temporary = pathlib.Path(temporary)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as output:
            for name, path in selected:
                _source(root, path.relative_to(root).as_posix())
                digest, size = hashlib.sha256(), 0
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(descriptor, "rb") as incoming:
                    if not stat.S_ISREG(os.fstat(incoming.fileno()).st_mode):
                        raise SchemaError(f"evidence is not a regular file: {path}")
                    info = zipfile.ZipInfo(name)
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = (stat.S_IFREG | 0o644) << 16
                    with output.open(info, "w", force_zip64=True) as outgoing:
                        while chunk := incoming.read(65536):
                            digest.update(chunk)
                            size += len(chunk)
                            outgoing.write(chunk)
                manifest["files"].append({"path": name, "size": size,
                                          "sha256": digest.hexdigest()})
            info = zipfile.ZipInfo(MANIFEST)
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            output.writestr(info, _canonical(manifest))
        try:
            os.link(temporary, archive)
        except FileExistsError as exc:
            raise SchemaError(f"archive already exists: {archive}") from exc
    finally:
        temporary.unlink(missing_ok=True)
    return {"id": _identity(manifest), "archive": str(archive), "manifest": manifest,
            "assurance": ASSURANCE}


def _same_tree(existing, staged):
    if not existing.is_dir() or existing.is_symlink():
        return False
    def inventory(root):
        result = {}
        for directory, dirs, files in os.walk(root, followlinks=False):
            for name in dirs + files:
                path = pathlib.Path(directory) / name
                if path.is_symlink() or not (path.is_dir() or path.is_file()):
                    raise SchemaError(f"unsafe existing import member: {path}")
                result[path.relative_to(root).as_posix()] = path.is_dir()
        return result
    expected = inventory(staged)
    if inventory(existing) != expected:
        return False
    for name, is_dir in expected.items():
        if is_dir:
            continue
        with (existing / name).open("rb") as left, (staged / name).open("rb") as right:
            while True:
                a, b = left.read(65536), right.read(65536)
                if a != b:
                    return False
                if not a:
                    break
    return True


def import_bundle(archive, root, into, lanes, *, protected=(), session,
                  lock_path=None) -> dict:
    """Validate fully before domain writes; atomically publish by manifest hash.

    Return root-relative files usable in RunRecord.outputs without manufacturing
    a run. Existing content at that address must match the entire tree exactly.
    """
    root = pathlib.Path(root).resolve()
    prefix = _relative(str(into)).as_posix()
    if not isinstance(session, str) or not session.strip():
        raise SchemaError("evidence import requires a nonempty session")
    with tempfile.TemporaryDirectory(prefix="ar-evidence-") as temporary:
        staged = pathlib.Path(temporary)
        with _archive(archive) as opened:
            manifest = _inspect(opened, staged)
        identity = _identity(manifest)
        relative = f"{prefix}/{identity}"
        names = [MANIFEST, *(entry["path"] for entry in manifest["files"])]
        # Validate every destination before even acquiring a domain-local lock.
        target = validate_output_path(root, relative, lanes, protected)
        for name in names:
            validate_output_path(root, f"{relative}/{name}", lanes, protected)
        lock = pathlib.Path(lock_path) if lock_path else root / ".evidence-import.lock"
        with Lock(lock, session, stale_after=float("inf")):
            validate_output_path(root, relative, lanes, protected)
            if target.exists():
                if not _same_tree(target, staged):
                    raise SchemaError(f"evidence import collision at {target}")
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                publication = pathlib.Path(tempfile.mkdtemp(prefix=".import-", dir=target.parent))
                try:
                    shutil.copytree(staged, publication, dirs_exist_ok=True)
                    if target.exists() or target.is_symlink():
                        raise SchemaError(f"evidence import collision at {target}")
                    os.rename(publication, target)
                finally:
                    if publication.exists():
                        shutil.rmtree(publication)
    return {"id": identity, "path": relative,
            "outputs": [f"{relative}/{name}" for name in names], "assurance": ASSURANCE}


def _load(args):
    from .config import DomainConfig, discover
    config = DomainConfig.load(args.domain or discover())
    config.policy.check_command(["ar", "evidence", args.evidence_command])
    return config


def _pack_command(args):
    from .runs import read_all
    config = _load(args)
    paths = list(args.paths)
    if args.run:
        records = [record for record in read_all(config.paths.runs) if record.id == args.run]
        if not records:
            raise SchemaError(f"unknown run ID: {args.run}")
        if any(record.to_dict() != records[0].to_dict() for record in records[1:]):
            raise SchemaError(f"conflicting run ID: {args.run}")
        paths.extend(records[0].outputs)
    try:
        metadata = json.loads(args.metadata) if args.metadata else None
    except ValueError as exc:
        raise SchemaError(f"invalid metadata JSON: {exc}") from exc
    result = pack(config.paths.root, paths, args.output, sources=args.source, metadata=metadata)
    print(json.dumps(result, sort_keys=True))
    return 0


def _verify_command(args):
    _load(args)
    print(json.dumps(verify(args.archive), sort_keys=True))
    return 0


def _import_command(args):
    config = _load(args)
    paths = config.paths
    result = import_bundle(args.archive, paths.root, args.into, config.lanes,
                           protected=(paths.entries, paths.claims, paths.runs,
                                      paths.iterations, paths.workspaces),
                           session=args.session, lock_path=paths.claims / "evidence.lock")
    print(json.dumps(result, sort_keys=True))
    return 0


def register_parser(subparsers):
    parser = subparsers.add_parser("evidence", help="portable integrity-only evidence bundles")
    verbs = parser.add_subparsers(dest="evidence_command", required=True)
    pack_parser = verbs.add_parser("pack", help="snapshot explicit files or a run's declared outputs")
    pack_parser.add_argument("paths", nargs="*", help="evidence files/directories inside the domain root")
    pack_parser.add_argument("--run", help="include declared outputs of this existing run ID")
    pack_parser.add_argument("--source", action="append", default=[], help="optional source snapshot inside the domain root")
    pack_parser.add_argument("--metadata", help="optional JSON object, not authenticated")
    pack_parser.add_argument("--output", required=True, help="new transport ZIP path")
    pack_parser.set_defaults(func=_pack_command)
    verify_parser = verbs.add_parser("verify", help="check hashes and archive safety, not origin or eligibility")
    verify_parser.add_argument("archive")
    verify_parser.set_defaults(func=_verify_command)
    import_parser = verbs.add_parser("import", help="atomically retain validated bytes, without creating runs")
    import_parser.add_argument("archive")
    import_parser.add_argument("--into", required=True, help="domain-relative findings destination prefix")
    import_parser.set_defaults(func=_import_command)
