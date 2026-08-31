import pathlib
import shutil

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOY = ROOT / "domains" / "toy"


@pytest.fixture
def toy():
    """The toy domain, read-only."""
    from autoresearch.config import DomainConfig
    return DomainConfig.load(TOY)


@pytest.fixture
def sandbox(tmp_path):
    """A private copy of the toy domain: tests that mutate state use this, so a
    test run never disturbs the committed fixture."""
    from autoresearch.config import DomainConfig
    dest = tmp_path / "toy"
    shutil.copytree(TOY, dest, ignore=shutil.ignore_patterns("__pycache__"))
    for sub in ("state/entries", "data/runs", "state/claims", "docs/log", "inbox"):
        target = dest / sub
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
    return DomainConfig.load(dest)


@pytest.fixture
def store(sandbox):
    from autoresearch.entries import Store
    return Store(sandbox.paths.entries)


def make_entry(store, entry_id="Q1", **over):
    from autoresearch.entries import Entry
    data = dict(id=entry_id, track="research", title=f"{entry_id} title")
    data.update(over)
    entry = Entry(**data)
    store.save(entry)
    return entry


def memo(config, name="inbox/m.md", text="evidence"):
    path = config.paths.root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return name
