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
    # docs/skills is wiped like every other record directory: a skill written
    # by one test and read by the next is a corpus leaking across tests, which
    # is the failure the sandbox exists to prevent.
    for sub in ("state/entries", "data/runs", "state/claims", "docs/log",
                "docs/skills", "inbox"):
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


def close(config, store, entry, verdict="confirmed", **over):
    """Take an entry to a terminal state through the real machine, so a test
    fixture and a real closure cannot diverge."""
    from autoresearch.entries import Result
    result = Result(verdict=verdict, memo=memo(config, f"inbox/{entry.id}.md"),
                    at="2026-01-01T00:00:00+00:00", session="test",
                    summary=over.pop("summary", "it held"),
                    closure_kind=over.pop("closure_kind", None),
                    reopen_condition=over.pop("reopen_condition", ""))
    machine = config.track_for(entry.id).machine
    if entry.status == machine.initial:
        entry.apply(machine, "in-progress", "test", why="claiming")
    entry.apply(machine, verdict, "test", why="closing", result=result,
                memo_exists=lambda m: (config.paths.root / m).exists())
    store.save(entry)
    return entry


def make_skill(config, name="a-skill", cites=("Q1",), body=None,
               description=None, distilled=None):
    """Write a well-formed skill directly, bypassing the librarian. Uses
    `skills.render` so the format under test is the format core writes."""
    from autoresearch import skills as skills_mod
    cites = list(cites)
    if body is None:
        claims = "\n".join(f"It held for {c} [{c}]." for c in cites)
        body = f"# {name} states one claim\n\n{claims}\n"
    skill = skills_mod.Skill(
        name=name,
        description=description or f"Use when {name} might apply: a trigger.",
        cites=cites,
        # `is None` and not `or`: a test that passes {} is testing the
        # no-provenance case and must not be handed the default.
        distilled=dict({"at": "2026-01-01T00:00:00+00:00", "iteration": 1,
                        "core": "test"} if distilled is None else distilled),
        body=body)
    path = config.paths.root / config.skills.dir / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(skills_mod.render(skill))
    return path


def memo(config, name="inbox/m.md", text="evidence"):
    path = config.paths.root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return name
