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

import datetime as dt

from .entries import Entry
from .errors import AutoresearchError

#: Bumped only when the bundle shape changes; ingest refuses any other value.
BUNDLE_SCHEMA = "ar-defect-bundle-1"

#: The evidence an entry on a defect track must carry. `expected` is encouraged
#: but not required -- "it crashed" with no "should have" is still reproducible.
REQUIRED_FIELDS = ("core", "repro", "observed")


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def defect_track(config):
    """The track this domain files harness defects on, or None.

    A track declares itself by `requires_defect_evidence = true`, so the
    boundary is data-driven: a domain that names no defect track has no
    export surface, and `export_bundle` says so rather than guessing a track
    by its prefix.
    """
    for track in config.tracks.values():
        if track.requires_defect_evidence:
            return track
    return None


def evidence_problems(entry: Entry, track) -> list[str]:
    """What makes this defect entry unpublishable, as a list. Empty means ready.

    Every refusal names the missing field, because "incomplete" without the
    field name sends the reporter back to diff-hunting."""
    missing = [name for name in REQUIRED_FIELDS if not getattr(entry, name)]
    if not missing:
        return []
    return [
        f"{entry.id}: track {track.id!r} requires_defect_evidence and this "
        f"entry is missing {', '.join(sorted(missing))} -- a defect report "
        "nobody can reproduce is an opinion, not a record"]


def export_bundle(config, entries, all_status: bool = False) -> tuple[dict, list, dict]:
    """Collect this project's defect entries into one upstream bundle.

    Returns `(bundle, refused, stats)`. `refused` is a list of
    `(entry_id, reason)` for entries missing evidence; they are named, never
    silently dropped. Closed defects are skipped unless `all_status` -- a fixed
    defect still tells upstream what to check for, but the open queue is what a
    maintainer asked for. `stats` carries the counts and the track id, so the
    caller can print what it read.
    """
    from . import hardware as hw
    from .skills import core_version

    track = defect_track(config)
    if track is None:
        raise AutoresearchError(
            f"domain {config.name!r} declares no track with "
            "`requires_defect_evidence = true`; there is no defect track to "
            "export. Add one to domain.toml, or file harness debt on the "
            "research track if that is where this domain keeps it.")

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
            "name": config.name,
            "core": core_version(),
            "host": hw.detect().fingerprint,
        },
        "defects": defects,
    }
    stats = {"track": track.id, "read": len(defects) + len(refused) + closed,
             "closed_skipped": closed}
    return bundle, refused, stats


def ingest_bundle(bundle: dict, store, prefix: str = "F", track: str = "field",
                  ) -> tuple[list[Entry], list[tuple[str, str]]]:
    """Turn an upstream bundle into ordinary entries. The receiving side.

    Each defect becomes a real entry under `prefix`, tagged
    `from:<project>/<id>` so re-ingesting the same bundle files nothing twice.
    Returns `(filed, skipped)`; every skip carries its reason, and a skip for
    missing evidence says which field -- a bundle should not have been able to
    carry one, since export refuses it, but ingest trusts nothing it did not
    validate itself.

    Entries land in the target machine's initial status by way of the plain
    string `track`; the caller is responsible for pointing `--into` at a store
    whose domain declares that prefix.
    """
    if bundle.get("schema") != BUNDLE_SCHEMA:
        raise AutoresearchError(
            f"bundle schema {bundle.get('schema')!r} is not {BUNDLE_SCHEMA!r}; "
            "refusing to guess at an unknown shape")
    project = str((bundle.get("project") or {}).get("name") or "unknown-project")

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
            id=store.next_id(prefix), track=track,
            title=str(defect.get("title") or "")[:200] or f"field defect {defect_id}",
            hypothesis=str(defect.get("hypothesis") or ""),
            observed=str(defect.get("observed") or ""),
            expected=str(defect.get("expected") or ""),
            repro=str(defect.get("repro") or ""),
            core=str(defect.get("core") or ""),
            why_filed=f"field defect from {project}",
            tags=[tag])
        store.save(entry)
        seen_tags.add(tag)
        filed.append(entry)
    return filed, skipped
