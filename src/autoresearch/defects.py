"""Field defects: how an improvement found downstream gets back upstream.

A project running this core discovers defects in it constantly -- the QC role
files harness debt every iteration. The record stays authoritative: a defect is
an ordinary entry on a defect track (a track declaring
`requires_defect_evidence`), carrying the evidence fields that make it
reproducible by someone who has never seen this project -- `core`, `repro` and
`observed`.

What this module owns is the boundary:

* `export_bundle` validates those fields and emits every exportable defect as
  one JSON bundle, counting what it read, exported, refused and skipped -- a
  reader that reports nothing read is indistinguishable from an empty corpus,
  which is the ambiguity this harness refuses everywhere else.
* `ingest_bundle` is the upstream half: it turns a bundle into ordinary entries
  here, refusing duplicates by citation tag and skipping incomplete defects by
  name rather than silently.

Publishing the bundle -- an issue, a PR, a push -- is a human-only act. The
scaffold ships a `[[policy.human_only]]` rule saying exactly that; an agent
prepares the bundle and hands it to a person, which is the same shape every
other irreversible outward-facing action takes.
"""
from __future__ import annotations

import dataclasses
import datetime as dt

from .config import DomainConfig, Track
from .entries import Entry, Store
from .errors import AutoresearchError

#: Bumped only when the bundle shape changes; ingest refuses any other value.
BUNDLE_SCHEMA = "ar-defect-bundle-1"

#: The evidence an entry on a defect track must carry. `expected` is encouraged
#: but not required -- "it crashed" with no "should have" is still reproducible.
REQUIRED_FIELDS = ("core", "repro", "observed")


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def _local_defect_track(config) -> Track | None:
    """The track this domain itself declares for defects, or None.

    A track declares itself by `requires_defect_evidence = true`, so the
    boundary is data-driven: a track is never guessed from its prefix."""
    for track in config.tracks.values():
        if track.requires_defect_evidence:
            return track
    return None


@dataclasses.dataclass
class DefectHome:
    """Where a domain's harness defects are filed and exported from.

    `shared` is True when the track, config and store are the umbrella's, not
    this domain's: a member that declares no defect track files on the
    umbrella's, so one workspace keeps one defect queue and one bundle."""
    track: Track
    config: DomainConfig
    store: Store
    shared: bool


def defect_home(config) -> DefectHome | None:
    """This domain's defect track first; else, for a member, the umbrella's.

    A member declaring its own defect track keeps it -- the umbrella is only
    consulted when the domain declares none. A domain with no umbrella (or a
    broken workspace) gets None exactly when it declares no defect track, as
    before members existed (#67)."""
    track = _local_defect_track(config)
    if track is not None:
        return DefectHome(track, config, Store(config.paths.entries), False)
    from . import members
    workspace = members.workspace_or_none(config)
    if workspace is None or workspace.umbrella.paths.root == config.paths.root:
        return None
    umbrella = workspace.umbrella
    track = _local_defect_track(umbrella)
    if track is None:
        return None
    return DefectHome(track, umbrella, Store(umbrella.paths.entries), True)


def evidence_problems(entry: Entry, track) -> list[str]:
    """What makes this defect entry unpublishable, as a list. Empty means ready.

    Every refusal names the missing field, because "incomplete" without the
    field name sends the reporter back to diff-hunting."""
    missing = [name for name in REQUIRED_FIELDS if not getattr(entry, name)]
    if (getattr(entry, "core", "") or "").strip().lower() == "auto":
        # Present but meaningless: the field names no version, so the upstream
        # half of the loop has nothing to reproduce against (sig.golf H20).
        return [f"{entry.id}: core is the literal 'auto' -- name the version "
                f"this defect was observed on, or repair it with "
                f"`ar entry amend {entry.id} --core <version>`"]
    if not missing:
        return []
    return [
        f"{entry.id}: track {track.id!r} requires_defect_evidence and this "
        f"entry is missing {', '.join(sorted(missing))} -- a defect report "
        "nobody can reproduce is an opinion, not a record"]


def export_bundle(config, entries=None, all_status: bool = False,
                  ) -> tuple[dict, list, dict]:
    """Collect this project's defect entries into one upstream bundle.

    The project is the defect home's: a member filing on the umbrella's track
    exports the umbrella's bundle, the same one the umbrella itself exports.
    `entries` defaults to that home's store.

    Returns `(bundle, refused, stats)`. `refused` is a list of
    `(entry_id, reason)` for entries missing evidence; they are named, never
    silently dropped. Closed defects are skipped unless `all_status` -- a fixed
    defect still tells upstream what to check for, but the open queue is what a
    maintainer asked for. `stats` carries the counts and the track id, so the
    caller can print what it read.
    """
    from . import hardware as hw
    from .skills import core_version

    home = defect_home(config)
    if home is None:
        raise AutoresearchError(
            f"domain {config.name!r} declares no track with "
            "`requires_defect_evidence = true`; there is no defect track to "
            "export. Add one to domain.toml, or file harness debt on the "
            "research track if that is where this domain keeps it.")
    track = home.track
    if entries is None:
        entries = home.store.all()

    machine = track.machine
    defects: list[dict] = []
    refused: list[tuple[str, str]] = []
    closed = 0
    for entry in entries:
        if not track.is_id(entry.id):
            continue
        if machine.status(entry.status).terminal:
            if not all_status:
                closed += 1
                continue
        problems = evidence_problems(entry, track)
        if problems:
            refused.append((entry.id, problems[0].split(": ", 1)[1]))
            continue
        defects.append(entry.to_dict())

    bundle = {
        "schema": BUNDLE_SCHEMA,
        "exported_at": _now(),
        "project": {
            "name": home.config.name,
            "core": core_version(),
            "host": hw.detect().fingerprint,
        },
        "defects": defects,
    }
    stats = {"track": track.id, "read": len(defects) + len(refused) + closed,
             "closed_skipped": closed}
    return bundle, refused, stats


def ingest_bundle(bundle: dict, store, *, track: Track,
                  ) -> tuple[list[Entry], list[tuple[str, str]]]:
    """Turn an upstream bundle into ordinary entries. The receiving side.

    Each defect becomes a real entry under `track.prefix`, tagged
    `from:<project>/<id>` so re-ingesting the same bundle files nothing twice.
    Returns `(filed, skipped)`; every skip carries its reason, and a skip for
    missing evidence says which field -- a bundle should not have been able to
    carry one, since export refuses it, but ingest trusts nothing it did not
    validate itself.

    The caller supplies the receiving domain's configured track. Entries use
    its id, prefix and initial status, never an independent state default.
    """
    if bundle.get("schema") != BUNDLE_SCHEMA:
        raise AutoresearchError(
            f"bundle schema {bundle.get('schema')!r} is not {BUNDLE_SCHEMA!r}; "
            "refusing to guess at an unknown shape")
    project = str((bundle.get("project") or {}).get("name") or "unknown-project")

    # Dedupe and file under one filing lock (#48): two concurrent ingests
    # of one bundle both passed the tag check before either wrote.
    with store.filing_lock("defect ingest"):
        seen_tags = {tag for entry in store.all() for tag in entry.tags}
        filed: list[Entry] = []
        skipped: list[tuple[str, str]] = []
        for defect in bundle.get("defects") or []:
            if not isinstance(defect, dict) or not defect.get("id"):
                skipped.append(("<no id>", "defect carries no id; cannot be cited"))
                continue
            defect_id = str(defect["id"])
            tag = f"from:{project}/{defect_id}"
            if tag in seen_tags:
                skipped.append((defect_id, f"already ingested ({tag})"))
                continue
            missing = [name for name in REQUIRED_FIELDS if not str(defect.get(name) or "")]
            if missing:
                skipped.append((defect_id,
                                f"incomplete: missing {', '.join(sorted(missing))}; "
                                "export upstream should have refused it"))
                continue
            entry = Entry(
                id=store.next_id(track.prefix), track=track.id,
                status=track.machine.initial,
                title=str(defect.get("title") or "")[:200] or f"field defect {defect_id}",
                hypothesis=str(defect.get("hypothesis") or ""),
                observed=str(defect.get("observed") or ""),
                expected=str(defect.get("expected") or ""),
                repro=str(defect.get("repro") or ""),
                core=str(defect.get("core") or ""),
                why_filed=f"field defect from {project}",
                tags=[tag])
            entry.gates = [dataclasses.replace(gate) for gate in track.gates]
            store.create(entry, renumber=True)
            seen_tags.add(tag)
            filed.append(entry)
    return filed, skipped
