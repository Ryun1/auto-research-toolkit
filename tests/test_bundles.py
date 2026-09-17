import hashlib
import json
import stat
import zipfile

import pytest

from autoresearch.bundles import MANIFEST, import_bundle, pack, verify
from autoresearch.errors import SchemaError
from autoresearch.lanes import Lanes
from autoresearch.runs import retain_output

LANES = Lanes(r"^findings/")


def prepared(tmp_path):
    source = tmp_path / "source"
    (source / "results").mkdir(parents=True)
    (source / "results" / "proof.bin").write_bytes(b"evidence\x00\xff")
    (source / "code.py").write_text("print('snapshot, not execution')\n")
    archive = tmp_path / "handoff.zip"
    packed = pack(source, ["results"], archive, sources=["code.py"],
                  metadata={"note": "untrusted producer metadata"})
    return source, archive, packed


def rewrite(archive, destination, mutate):
    with zipfile.ZipFile(archive) as incoming:
        members = [(member, incoming.read(member)) for member in incoming.infolist()]
    with zipfile.ZipFile(destination, "w") as outgoing:
        for member, data in mutate(members):
            outgoing.writestr(member, data)


def test_handoff_is_idempotent_and_retained_outputs_remain_ordinary_files(tmp_path):
    source, archive, packed = prepared(tmp_path)
    # Changing the live source cannot change the already packed evidence.
    (source / "results" / "proof.bin").write_bytes(b"later live bytes")
    checked = verify(archive)
    assert checked["id"] == packed["id"]
    root = tmp_path / "receiver"
    root.mkdir()
    imported = import_bundle(archive, root, "findings/imports", LANES, session="s1")
    assert import_bundle(archive, root, "findings/imports", LANES, session="s2") == imported
    evidence = next(name for name in imported["outputs"] if name.endswith("proof.bin"))
    assert (root / evidence).read_bytes() == b"evidence\x00\xff"
    final = tmp_path / "coordinator"
    final.mkdir()
    for name in imported["outputs"]:
        retain_output(root, final, name, LANES)
    assert (final / evidence).read_bytes() == b"evidence\x00\xff"
    assert not (root / "state").exists()


@pytest.mark.parametrize("damage", ["digest", "traversal", "absolute", "symlink", "duplicate",
                                    "undeclared", "missing", "size", "duplicate-key"])
def test_malformed_archives_do_not_write_receiver_state(tmp_path, damage):
    _, archive, _ = prepared(tmp_path)
    malicious = tmp_path / "malicious.zip"
    def mutate(members):
        evidence = next(i for i, (member, _) in enumerate(members)
                        if member.filename.startswith("evidence/"))
        manifest_index = next(i for i, (member, _) in enumerate(members)
                              if member.filename == MANIFEST)
        member, data = members[evidence]
        if damage == "digest":
            members[evidence] = member, b"x" * len(data)
        elif damage in ("traversal", "absolute"):
            name = "../escaped" if damage == "traversal" else "/absolute"
            members.append((zipfile.ZipInfo(name), b"bad"))
        elif damage == "symlink":
            member.external_attr = (stat.S_IFLNK | 0o777) << 16
        elif damage == "duplicate":
            members.append((member, data))
        elif damage == "undeclared":
            members.append((zipfile.ZipInfo("evidence/extra"), b"bad"))
        elif damage == "missing":
            del members[evidence]
        else:
            info, raw = members[manifest_index]
            manifest = json.loads(raw)
            if damage == "size":
                manifest["files"][0]["size"] += 1
                raw = json.dumps(manifest).encode()
            else:
                raw = raw.replace(b'{"assurance":', b'{"schema":"ignored","assurance":', 1)
            members[manifest_index] = info, raw
        return members
    if damage == "duplicate":
        with pytest.warns(UserWarning, match="Duplicate name"):
            rewrite(archive, malicious, mutate)
    else:
        rewrite(archive, malicious, mutate)
    root = tmp_path / "receiver"
    root.mkdir()
    (root / "owned").write_text("untouched")
    with pytest.raises(SchemaError):
        import_bundle(malicious, root, "findings/imports", LANES, session="s")
    assert sorted(path.name for path in root.iterdir()) == ["owned"]
    assert (root / "owned").read_text() == "untouched"
    assert not (tmp_path / "escaped").exists()


def test_import_refuses_changed_existing_content_instead_of_repairing_it(tmp_path):
    _, archive, _ = prepared(tmp_path)
    root = tmp_path / "receiver"
    root.mkdir()
    imported = import_bundle(archive, root, "findings/imports", LANES, session="s")
    evidence = next(name for name in imported["outputs"] if name.endswith("proof.bin"))
    (root / evidence).write_bytes(b"owned conflicting content")
    with pytest.raises(SchemaError, match="collision"):
        import_bundle(archive, root, "findings/imports", LANES, session="s")
    assert (root / evidence).read_bytes() == b"owned conflicting content"


@pytest.mark.parametrize("destination,protected", [("scaffolding/imports", None),
                                                    ("findings/state/imports", "findings/state")])
def test_import_obeys_retention_lane_and_protected_state(tmp_path, destination, protected):
    _, archive, _ = prepared(tmp_path)
    root = tmp_path / "receiver"
    root.mkdir()
    protected_paths = [root / protected] if protected else []
    with pytest.raises(SchemaError):
        import_bundle(archive, root, destination, LANES,
                      protected=protected_paths, session="s")
    assert list(root.iterdir()) == []


def test_import_rejects_destination_symlink_before_writes(tmp_path):
    _, archive, _ = prepared(tmp_path)
    root, outside = tmp_path / "receiver", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "findings").symlink_to(outside, target_is_directory=True)
    with pytest.raises(SchemaError):
        import_bundle(archive, root, "findings/imports", LANES, session="s")
    assert list(outside.iterdir()) == []


def test_pack_rejects_source_symlinks_and_overlapping_selections(tmp_path):
    source, _, _ = prepared(tmp_path)
    (source / "linked").symlink_to(source / "results", target_is_directory=True)
    with pytest.raises(SchemaError, match="symlink"):
        pack(source, ["linked"], tmp_path / "linked.zip")
    with pytest.raises(SchemaError, match="duplicate"):
        pack(source, ["results", "results/proof.bin"], tmp_path / "duplicate.zip")
    assert not (tmp_path / "linked.zip").exists()
    assert not (tmp_path / "duplicate.zip").exists()


def test_pack_accepts_absolute_paths_only_inside_root(tmp_path):
    source, _, _ = prepared(tmp_path)
    inside = pack(source, [str(source / "results")], tmp_path / "inside.zip")
    assert "evidence/results/proof.bin" in {f["path"] for f in inside["manifest"]["files"]}
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "secret.txt").write_text("not ours")
    with pytest.raises(SchemaError, match="escapes root"):
        pack(source, [str(outside / "secret.txt")], tmp_path / "outside.zip")
    assert not (tmp_path / "outside.zip").exists()


def test_manifest_rejects_parent_file_conflict_before_staging(tmp_path):
    _, archive, _ = prepared(tmp_path)
    malicious = tmp_path / "conflict.zip"
    def mutate(members):
        for index, (member, data) in enumerate(members):
            if member.filename == MANIFEST:
                manifest = json.loads(data)
                manifest["files"].append({"path": "evidence/results", "size": 1,
                                          "sha256": hashlib.sha256(b"x").hexdigest()})
                members[index] = member, json.dumps(manifest).encode()
        members.append((zipfile.ZipInfo("evidence/results"), b"x"))
        return members
    rewrite(archive, malicious, mutate)
    root = tmp_path / "receiver"
    with pytest.raises(SchemaError, match="file/directory conflict"):
        import_bundle(malicious, root, "findings/imports", LANES, session="s")
    assert not root.exists()


@pytest.mark.parametrize("linked", ["results", "results/proof.bin"])
def test_absolute_inputs_do_not_hide_descendant_symlinks(tmp_path, linked):
    source, _, _ = prepared(tmp_path)
    (source / "linked").symlink_to(source / linked)
    archive = tmp_path / "linked.zip"
    with pytest.raises(SchemaError, match="symlink"):
        pack(source, [source / "linked"], archive)
    assert not archive.exists()
