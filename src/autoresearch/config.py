"""Loading and validating a domain.

A domain supplies exactly four things; everything else is core-owned:

1. **Measurement** -- a command that runs one experiment and emits a validated
   run record. (`commands.measure`)
2. **Goal and constants** -- metrics, objective, target and derived values, as a
   typed goal rather than a table in a document. (`goal.yaml`)
3. **Safety policy** -- forbidden paths, never-push remotes, human-only commands,
   spend ceiling, declared as data. (`[policy]`)
4. **Knowledge** -- the guides agents read to form good hypotheses, and the map
   of closed directions ranking uses as a hard exclusion filter. (`knowledge`)

Everything is validated at load: unknown top-level keys are refused, every
expression's free names must resolve, every state machine must be total, and
every policy rule must demonstrably refuse something. The point is that a
misconfigured domain fails *before* an iteration is spent, not during one.
"""
from __future__ import annotations

import pathlib
import tomllib
from dataclasses import dataclass, field

import yaml

from .errors import ConfigError
from .escalate import RemoteClass
from .goal import Goal
from .hardware import Requirement
from .lanes import Lanes
from .policy import Policy
from .states import StateMachine, default_machine

CONFIG_NAME = "domain.toml"

_TOP_LEVEL = {"domain", "state", "tracks", "lanes", "policy", "budgets",
              "commands", "knowledge", "coordinator", "hardware", "remote"}


@dataclass
class Track:
    """One work queue. The original harness ran two -- research and harness debt
    -- deliberately separated so "an agent booting into research work should
    never read harness debt as a lead on the score". That separation is a domain
    decision, so tracks are a list rather than a hardcoded pair."""
    id: str
    prefix: str
    title: str
    view: str
    machine: StateMachine
    description: str = ""
    #: the prose document this track's records were (or will be) migrated from.
    #: Distinct from `view` so a staged cutover can render beside the live
    #: document instead of over it.
    migrate_from: str = ""

    def is_id(self, entry_id: str) -> bool:
        return entry_id.startswith(self.prefix) and entry_id[len(self.prefix):].isdigit()


@dataclass
class Paths:
    root: pathlib.Path
    entries: pathlib.Path
    claims: pathlib.Path
    runs: pathlib.Path
    memos: pathlib.Path
    iterations: pathlib.Path
    workspaces: pathlib.Path


@dataclass
class DomainConfig:
    name: str
    paths: Paths
    goal: Goal
    lanes: Lanes
    policy: Policy
    tracks: dict[str, Track]
    commands: dict[str, str]
    budgets: dict[str, float]
    knowledge: list[str] = field(default_factory=list)
    coordinator: dict = field(default_factory=dict)
    #: what each experiment class needs of the machine, checked before it runs
    hardware: dict = field(default_factory=dict)
    #: rentable classes, each costed only if someone measured its ratio
    remote: list = field(default_factory=list)
    description: str = ""

    # -- lookup -----------------------------------------------------------

    def track_for(self, entry_id: str) -> Track:
        for track in self.tracks.values():
            if track.is_id(entry_id):
                return track
        raise ConfigError(
            f"no track owns entry id {entry_id!r}; prefixes are "
            f"{sorted(t.prefix for t in self.tracks.values())}")

    def command(self, name: str) -> str:
        try:
            return self.commands[name]
        except KeyError:
            raise ConfigError(
                f"domain {self.name!r} declares no `{name}` command; "
                f"declared: {sorted(self.commands)}") from None

    def budget(self, name: str, default=None):
        value = self.budgets.get(name, default)
        if value is None:
            raise ConfigError(
                f"domain {self.name!r} declares no budget {name!r}; "
                f"declared: {sorted(self.budgets)}")
        return value

    # -- validation -------------------------------------------------------

    def check(self) -> list[str]:
        """Every problem found, rather than the first. A validator that stops at
        the first failure makes a misconfigured domain take N runs to fix."""
        problems = []
        for track in self.tracks.values():
            try:
                track.machine.check_total()
            except ConfigError as exc:
                problems.append(f"track {track.id!r}: {exc}")
        problems += [f"policy: {f}" for f in self.policy.selftest()]
        for required in ("measure",):
            if required not in self.commands:
                problems.append(f"commands.{required} is required and missing")
        for name, rel in self.commands.items():
            target = self.paths.root / rel.split()[0]
            if not target.exists():
                problems.append(
                    f"commands.{name} points at {rel!r}, which does not exist")
        for rel in self.knowledge:
            if not (self.paths.root / rel).exists():
                problems.append(f"knowledge path {rel!r} does not exist")
        prefixes = [t.prefix for t in self.tracks.values()]
        if len(set(prefixes)) != len(prefixes):
            problems.append(
                f"two tracks share an id prefix {prefixes}; entry ids would be "
                "ambiguous and the wrong track would answer for them")
        return problems

    # -- construction -----------------------------------------------------

    @classmethod
    def load(cls, root) -> "DomainConfig":
        root = pathlib.Path(root).resolve()
        config_path = root / CONFIG_NAME
        if not config_path.exists():
            raise ConfigError(
                f"no {CONFIG_NAME} at {root}. A domain is a directory containing "
                f"{CONFIG_NAME}; run `ar init` to scaffold one.")
        with config_path.open("rb") as fh:
            data = tomllib.load(fh)

        # H135: an unrecognised key must never read as a successful run of what
        # you meant. The same applies to config as to flags.
        unknown = set(data) - _TOP_LEVEL
        if unknown:
            raise ConfigError(
                f"{CONFIG_NAME} has unknown top-level table(s) {sorted(unknown)}; "
                f"known: {sorted(_TOP_LEVEL)}")

        domain = data.get("domain") or {}
        name = domain.get("name")
        if not name:
            raise ConfigError(f"{CONFIG_NAME} must set domain.name")

        state = data.get("state") or {}
        paths = Paths(
            root=root,
            entries=root / state.get("entries", "state/entries"),
            claims=root / state.get("claims", "state/claims"),
            runs=root / state.get("runs", "data/runs"),
            memos=root / state.get("memos", "inbox"),
            iterations=root / state.get("iterations", "state/iterations"),
            workspaces=root / state.get("workspaces", ".ar/workspaces"))

        goal_rel = domain.get("goal", "goal.yaml")
        goal_path = root / goal_rel
        if not goal_path.exists():
            raise ConfigError(f"domain.goal points at {goal_rel!r}, which does not exist")
        goal = Goal.from_dict(yaml.safe_load(goal_path.read_text()) or {})

        raw_tracks = data.get("tracks") or []
        if not raw_tracks:
            raise ConfigError(f"{CONFIG_NAME} declares no [[tracks]]")
        tracks = {}
        for spec in raw_tracks:
            try:
                tid, prefix = spec["id"], spec["prefix"]
            except KeyError as exc:
                raise ConfigError(f"a track is missing {exc}: {spec!r}") from exc
            machine = (StateMachine.from_dict(spec["states"]) if "states" in spec
                       else default_machine(spec.get("terminal")))
            tracks[tid] = Track(
                id=tid, prefix=prefix,
                title=spec.get("title", tid),
                view=spec.get("view", f"docs/{tid}.md"),
                machine=machine,
                description=spec.get("description", ""),
                migrate_from=spec.get("migrate_from", ""))

        lanes_spec = data.get("lanes") or {}
        if "findings" not in lanes_spec:
            raise ConfigError(
                "lanes.findings is required: the boundary is default-deny, and "
                "leaving it implicit is how a findings path silently became "
                "unpublishable (H51)")

        return cls(
            name=name,
            description=domain.get("description", ""),
            paths=paths,
            goal=goal,
            lanes=Lanes(lanes_spec["findings"]),
            policy=Policy.from_dict(data.get("policy") or {}),
            tracks=tracks,
            commands=dict(data.get("commands") or {}),
            budgets={k: float(v) for k, v in (data.get("budgets") or {}).items()},
            knowledge=list(data.get("knowledge") or []),
            coordinator=dict(data.get("coordinator") or {}),
            hardware={name: Requirement.from_dict(name, spec)
                      for name, spec in (data.get("hardware") or {}).items()},
            remote=[RemoteClass.from_dict(spec)
                    for spec in (data.get("remote") or [])])


def discover(start=None) -> pathlib.Path:
    """Nearest enclosing directory containing a domain.toml.

    H19: the original `bin/session` named worktrees relative to wherever it was
    run, so the same command did different things from different directories.
    Anchoring on the config file rather than on the cwd removes that class.
    """
    here = pathlib.Path(start or pathlib.Path.cwd()).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / CONFIG_NAME).exists():
            return candidate
    raise ConfigError(
        f"no {CONFIG_NAME} in {here} or any parent. Run from inside a domain, "
        f"or pass --domain <path>.")
