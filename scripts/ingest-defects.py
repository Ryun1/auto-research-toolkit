#!/usr/bin/env python3
"""Turn an upstream defect bundle into entries here.

The receiving half of `ar harness export`. A field project exports its defects
against the core as one JSON bundle; a person publishes it (an issue, a PR, a
file in this repository), and this script files each defect as an ordinary
entry here:

    python scripts/ingest-defects.py bundle.json --into state/entries --prefix F

Each entry is tagged `from:<project>/<id>`, so re-ingesting the same bundle
files nothing twice, and every skip is named. Defects missing their evidence
(`core`, `repro`, `observed`) are skipped too -- export upstream should have
refused them, and ingest trusts nothing it did not validate itself.

`--into` must point at a store whose domain declares the `--prefix` track, so
the entries validate like any other record.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from autoresearch.defects import ingest_bundle  # noqa: E402
from autoresearch.entries import Store  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("bundle", type=pathlib.Path)
    ap.add_argument("--into", default="state/entries",
                    help="entry store directory (default: state/entries)")
    ap.add_argument("--prefix", default="F",
                    help="id prefix for ingested defects (default: F)")
    ap.add_argument("--track", default="field",
                    help="track name recorded on each entry (default: field)")
    args = ap.parse_args()

    bundle = json.loads(args.bundle.read_text())
    store = Store(args.into)
    filed, skipped = ingest_bundle(bundle, store, prefix=args.prefix,
                                   track=args.track)
    project = (bundle.get("project") or {}).get("name") or "unknown-project"
    total = len(bundle.get("defects") or [])
    print(f"read {total} defect(s) from {project}: filed {len(filed)}, "
          f"skipped {len(skipped)}")
    for entry in filed:
        print(f"  filed {entry.id}: {entry.title}")
    for defect_id, reason in skipped:
        print(f"  skipped {defect_id}: {reason}")
    if any(reason.startswith("incomplete") for _, reason in skipped):
        print("\na defect arrived without its evidence; the exporter upstream "
              "should have refused it.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
