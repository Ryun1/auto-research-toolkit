import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOY = ROOT / "domains" / "toy"


@pytest.fixture(autouse=True)
def _no_inherited_session(monkeypatch):
    """`--session` defaults to $AR_SESSION (#30): a developer who exports it
    must not change which session every CLI test acts as."""
    monkeypatch.delenv("AR_SESSION", raising=False)


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


def race_next_id(monkeypatch, title="theirs"):
    """#21: another session files the number this one just computed, in the
    window between `next_id` and the write -- once, the way a real race
    happens. Its record is written straight to disk, as a second process
    would write it."""
    import yaml

    from autoresearch.entries import Store
    real = Store.next_id
    taken = []

    def racing(self, prefix):
        entry_id = real(self, prefix)
        if not taken:
            taken.append(entry_id)
            self.path(entry_id).write_text(yaml.safe_dump(
                {"id": entry_id, "track": "research", "title": title,
                 "status": "queued"}, sort_keys=False))
        return entry_id

    monkeypatch.setattr(Store, "next_id", racing)
    return taken


def append_measured(config, path, record):
    """A row as `ar measure --candidate` leaves it: appended in the slot, with
    the receipt harvest checks (#36). For tests about what happens to a
    measured row after it exists, not about how it was measured."""
    from autoresearch import evaluator
    from autoresearch.runs import append
    append(path, record)
    # The receipt names the slot, as `ar measure --candidate <slot>` does.
    path = pathlib.Path(path).resolve()
    pool = config.paths.workspaces.resolve()
    slot = next(p for p in path.parents if p.parent == pool)
    evaluator.write_receipt(config.paths.receipts, record, slot)


def _strip_sections(text, drop):
    """Remove from `text` every TOML section for which `drop(header, body)` is
    true. A section runs from its header to the next header; only the comment
    block directly above a dropped header goes with it, since that comment
    describes the section and a neighbour's comment describes the neighbour.
    Line-based on purpose: a TOML writer would reformat the whole file, and
    these fixtures should differ from the toy domain only where they say so."""
    lines = text.split("\n")
    header = re.compile(r"^\s*\[\[?([^\]\[]+)\]\]?\s*(#.*)?$")
    starts = [i for i, ln in enumerate(lines) if header.match(ln)]
    keep = [True] * len(lines)
    for n, i in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        # The next section's own comment block belongs to it, not to this body.
        while end > i + 1 and lines[end - 1].lstrip().startswith("#") and n + 1 < len(starts):
            end -= 1
        name = header.match(lines[i]).group(1).strip()
        if not drop(name, lines[i + 1:end]):
            continue
        first = i
        while first > 0 and lines[first - 1].lstrip().startswith("#"):
            first -= 1
        for j in range(first, end):
            keep[j] = False
    return "\n".join(ln for ln, k in zip(lines, keep, strict=True) if k)


def member_domain(dest, prefixes=("Q", "H"), name=None, extra="",
                  drop_tables=(), drop_tracks=()):
    """A copy of the toy domain at `dest`, its two tracks renamed to
    `prefixes`, with `extra` appended to its domain.toml.

    `drop_tables` names top-level tables to remove, each with its sub-tables
    and arrays (`policy` takes `[[policy.human_only]]` too, `hardware` every
    `[hardware.*]`, `remote` every `[[remote]]`). `drop_tracks` removes
    `[[tracks]]` blocks, with their `[tracks.*]` sub-tables, by prefix as
    renamed -- so it cannot name a track by its toy prefix."""
    shutil.copytree(TOY, dest, ignore=shutil.ignore_patterns("__pycache__"))
    toml = dest / "domain.toml"
    text = toml.read_text()
    text = text.replace('prefix = "Q"', f'prefix = "{prefixes[0]}"')
    text = text.replace('prefix = "H"', f'prefix = "{prefixes[1]}"')
    if name:
        text = text.replace('name = "toy"', f'name = "{name}"')
    if drop_tables:
        text = _strip_sections(text, lambda n, _body: n.split(".")[0] in drop_tables)
    if drop_tracks:
        dropping = False

        def drop_track(n, body):
            nonlocal dropping
            if n == "tracks":
                dropping = any(re.match(rf'\s*prefix\s*=\s*"{p}"', ln)
                               for p in drop_tracks for ln in body)
            elif not n.startswith("tracks."):
                dropping = False
            return dropping
        text = _strip_sections(text, drop_track)
    toml.write_text(text + "\n" + extra)
    (dest / "state" / "entries").mkdir(parents=True, exist_ok=True)
    return dest


MEMBERS = '[domains]\nmembers = ["domains/*"]\n'


@pytest.fixture
def umbrella(tmp_path):
    """The umbrella shape starks-research runs by hand: the toy domain as the
    umbrella (Q, H) over two members, `domains/a` (A, B) and `domains/b`
    (C, D)."""
    root = member_domain(tmp_path / "umb", name="umb", extra=MEMBERS)
    member_domain(root / "domains" / "a", ("A", "B"), name="a")
    member_domain(root / "domains" / "b", ("C", "D"), name="b")
    return root


@pytest.fixture
def git_umbrella(umbrella):
    """The umbrella as a git repository with one commit. Identity and signing
    are set on this throwaway repo only, so a developer's global config (a
    signing key that wants a passphrase) cannot decide whether a test runs."""
    def git(*args):
        subprocess.run(["git", *args], cwd=umbrella, check=True,
                       capture_output=True)
    git("init", "-q")
    git("config", "user.name", "Test Author")
    git("config", "user.email", "test@example.com")
    git("config", "commit.gpgsign", "false")
    git("add", "-A")
    git("commit", "-q", "-m", "umbrella")
    return umbrella
