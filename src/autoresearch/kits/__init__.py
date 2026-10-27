"""Kits: measurement code shared by every domain that scores the same toolchain.

A kit is one standard-library module here, vendored into each domain that
declares it (`[evaluator] kits = ["cairo"]`) by `ar kit sync`. Vendored rather
than imported because `bin/measure` runs under the system `python3`, and
because core hashes the evaluator from files inside the domain: a kit imported
from the installed core could change what a run scores without changing
`evaluator_sha`. Declaring a kit adds its vendored copy to the evaluator, and
`ar validate` warns when that copy differs from the one this core ships.
"""
from __future__ import annotations

import pathlib

_HERE = pathlib.Path(__file__).resolve().parent

#: Kit name -> the module that is its single source.
KITS = {"cairo": _HERE / "cairo.py"}


def source(name: str) -> pathlib.Path:
    return KITS[name]


def vendored(name: str) -> str:
    """Where a domain keeps its copy, relative to the domain root."""
    return f"bin/{name}kit.py"


def problems(config) -> list[str]:
    """A declared kit with no vendored copy: `bin/measure` would fail to
    import it, and the evaluator hash would cover a file that is not there."""
    return [f"[evaluator] kits declares {name!r} but {vendored(name)} is "
            f"missing; run `ar kit sync`"
            for name in config.evaluator_kits
            if not (config.paths.root / vendored(name)).is_file()]


def warnings(config) -> list[str]:
    """A vendored copy that differs from the kit this core ships. A warning,
    not a problem: a domain mid-season may hold its kit on purpose, since a
    new kit is a new evaluator. But it should be a decision, not drift."""
    out = []
    for name in config.evaluator_kits:
        copy = config.paths.root / vendored(name)
        if copy.is_file() and copy.read_bytes() != source(name).read_bytes():
            out.append(f"{vendored(name)} differs from the {name} kit this core "
                       f"ships; `ar kit sync` updates it (a new evaluator: "
                       f"re-measure before comparing across the change)")
    return out


def sync(config) -> list[str]:
    """Write every declared kit's copy that is missing or differs. Returns
    the paths written."""
    written = []
    for name in config.evaluator_kits:
        copy = config.paths.root / vendored(name)
        data = source(name).read_bytes()
        if copy.is_file() and copy.read_bytes() == data:
            continue
        copy.parent.mkdir(parents=True, exist_ok=True)
        copy.write_bytes(data)
        written.append(vendored(name))
    return written
