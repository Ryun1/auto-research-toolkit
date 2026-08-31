"""Loading and validating a domain.

A domain supplies exactly four things; everything else is core-owned:

1. **Measurement** -- a command that runs one experiment and emits a validated
   run record. (`commands.measure`)
2. **Goal and constants** -- metrics, objective, target and derived values, as a
   typed goal rather than a table in a document. (`goal.yaml`)
3. **Safety policy** -- forbidden paths, never-push remotes, human-only commands,
   spend ceiling, declared as data. (`[policy]`)
4. **Knowledge** -- the guides agents read to form good hypotheses, and the map
   of closed directions ranking uses as a hard exclusion filter. (`knowledge`,
   and the distilled skills under `[skills]`)

Everything is validated at load: unknown top-level keys are refused, every
expression's free names must resolve, every state machine must be total, and
every policy rule must demonstrably refuse something. The point is that a
misconfigured domain fails *before* an iteration is spent, not during one.
"""
from __future__ import annotations

import pathlib
import re
import tomllib
from dataclasses import dataclass, field

import yaml

from .errors import ConfigError
from .escalate import RemoteClass
from .goal import Goal
from .hardware import Requirement
from .lanes import Lanes
from .policy import Policy
from .rank import EXPLORE_FRACTION
from .states import StateMachine, default_machine

#: Placeholders a `[brain]` command may carry. Validated at load: a misspelled
#: placeholder discovered mid-iteration is a scout that ran without its prompt.
BRAIN_PLACEHOLDERS = {"role", "prompt_file", "workspace", "cost_file"}

CONFIG_NAME = "domain.toml"

#: default cadence for the distil phase: every N iterations. Not every one --
#: a librarian asked to distil after a single verdict writes a skill that says
#: what one entry already says, and the record already says it better.
DISTIL_EVERY = 5

_TOP_LEVEL = {"domain", "state", "tracks", "lanes", "policy", "budgets",
              "commands", "knowledge", "coordinator", "hardware", "remote",
              "skills"}


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
    #: entries on this track are defects against the harness itself, so each
    #: must carry the evidence that makes it reproducible by someone who has
    #: never seen this project: `core`, `repro` and `observed` (entries.py).
    #: `ar validate` refuses an entry missing them, `ar harness export` refuses
    #: to publish one, and ingest upstream skips one -- each naming the field.
    requires_defect_evidence: bool = False

    def is_id(self, entry_id: str) -> bool:
        return entry_id.startswith(self.prefix) and entry_id[len(self.prefix):].isdigit()


@dataclass
class Skills:
    """Where a domain's distilled skills live, and how big one may get.

    Distinct from `knowledge`, which is a hand-written list of guide paths.
    A skill is written by the loop, cites the entries that back it, and is
    checked against the record -- so it needs a directory core owns the shape
    of rather than a path list a human maintains.

    `dir` defaults under `docs/` rather than `guides/` for a lane reason: the
    shipped findings pattern matches `docs/` and not `guides/`, so a distil
    phase writing into `guides/` would put a scaffolding-lane file on every
    branch that also carries findings, and `Lanes.explain` refuses a mixed
    branch (H51). A distilled skill is derived from findings and publishes
    with them.
    """
    dir: str = "docs/skills"
    #: body line limit, following the 500-line ceiling in the skill-authoring
    #: guidance this format borrows. A skill nobody finishes reading is a
    #: guide with extra steps.
    max_lines: int = 500

    @classmethod
    def from_dict(cls, spec: dict) -> Skills:
        unknown = set(spec) - {"dir", "max_lines"}
        if unknown:
            raise ConfigError(
                f"[skills] has unknown key(s) {sorted(unknown)}; "
                f"known: ['dir', 'max_lines']")
        dir_ = spec.get("dir", "docs/skills")
        if not isinstance(dir_, str) or not dir_ or dir_.startswith("/") or ".." in dir_:
            raise ConfigError(
                f"skills.dir must be a relative path inside the domain, got {dir_!r}")
        raw = spec.get("max_lines", 500)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
            raise ConfigError(f"skills.max_lines must be a positive integer, got {raw!r}")
        return cls(dir=dir_, max_lines=raw)


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
    #: where distilled skills live. Always present -- a domain that declares no
    #: [skills] table gets the default directory, which may not exist yet; an
    #: absent directory reads as zero skills, never as an error.
    skills: Skills = field(default_factory=Skills)
    coordinator: dict = field(default_factory=dict)
    #: which backend serves each role. `"claude"` names the built-in SDK brain;
    #: a list of strings is a command run per the ProcessBrain contract in
    #: `driver/brain.py`. Validated at load -- an unknown role or placeholder
    #: here is a loop that cannot start, so say so before anything spends.
    brain: dict = field(default_factory=dict)
    #: share of each shortlist reserved for the largest `impact`, ignoring
    #: confidence and cost. Typed rather than left in the `coordinator` dict
    #: because it is a risk appetite -- how much of an iteration a domain will
    #: spend on a swing that probably fails -- and a domain chasing a frontier
    #: that moved 22.5% in 18.8 days wants a different one from a domain
    #: polishing a number that is nearly converged. Validated at load: it
    #: reaches a float multiplication in `Ranking.shortlist`, and finding that
    #: out mid-iteration costs a run.
    explore_fraction: float = EXPLORE_FRACTION
    #: run the distil phase every N iterations; 0 turns it off. Typed out of the
    #: coordinator dict for the same reason as `explore_fraction`: it decides
    #: whether a phase runs at all, and discovering it was misspelled mid-loop
    #: costs the iteration that would have distilled.
    distil_every: int = DISTIL_EVERY
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
        skills_dir = self.paths.root / self.skills.dir
        if skills_dir.exists() and not skills_dir.is_dir():
            problems.append(
                f"skills.dir {self.skills.dir!r} exists and is not a directory")
        prefixes = [t.prefix for t in self.tracks.values()]
        if len(set(prefixes)) != len(prefixes):
            problems.append(
                f"two tracks share an id prefix {prefixes}; entry ids would be "
                "ambiguous and the wrong track would answer for them")
        return problems

    # -- construction -----------------------------------------------------

    @classmethod
    def load(cls, root) -> DomainConfig:
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
                migrate_from=spec.get("migrate_from", ""),
                requires_defect_evidence=bool(spec.get(
                    "requires_defect_evidence", False)))

        coordinator = dict(data.get("coordinator") or {})
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
            skills=Skills.from_dict(dict(data.get("skills") or {})),
            coordinator=coordinator,
            brain=_brain_spec(data.get("brain")),
            explore_fraction=_explore_fraction(coordinator),
            distil_every=_distil_every(coordinator),
            hardware={name: Requirement.from_dict(name, spec)
                      for name, spec in (data.get("hardware") or {}).items()},
            remote=[RemoteClass.from_dict(spec)
                    for spec in (data.get("remote") or [])])


def _brain_spec(data) -> dict:
    """Read and check the top-level `brain` table.

    Keys are `default` or a role name; values are `"claude"` (the built-in SDK
    brain) or a non-empty list of strings, the command the ProcessBrain
    contract runs. Absent table means every role on the default backend.
    """
    from .driver.brain import Role

    spec = {}
    for key, value in dict(data or {}).items():
        if key != "default" and key not in Role.ALL:
            raise ConfigError(
                f"brain.{key}: not a role; keys are 'default' or one of "
                f"{', '.join(Role.ALL)}")
        if value == "claude":
            spec[key] = value
            continue
        if (not isinstance(value, list) or not value
                or not all(isinstance(part, str) for part in value)):
            raise ConfigError(
                f"brain.{key}: must be \"claude\" or a non-empty list of "
                f"strings, got {value!r}")
        unknown = ({match for part in value
                    for match in re.findall(r"\{(\w+)\}", part)}
                   - BRAIN_PLACEHOLDERS)
        if unknown:
            raise ConfigError(
                f"brain.{key}: unknown placeholder(s) {sorted(unknown)}; "
                f"allowed: {sorted(BRAIN_PLACEHOLDERS)}")
        spec[key] = [str(part) for part in value]
    return spec


def _explore_fraction(coordinator: dict) -> float:
    """Read and check `[coordinator] explore_fraction`.

    Absent means the core default; `0` means the domain has decided against a
    reserve and must not be defaulted back into one, which is why this tests for
    the key rather than for falsiness."""
    if "explore_fraction" not in coordinator:
        return EXPLORE_FRACTION
    raw = coordinator["explore_fraction"]
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ConfigError(
            f"coordinator.explore_fraction must be a number, got {raw!r}")
    if not 0.0 <= raw < 1.0:
        raise ConfigError(
            f"coordinator.explore_fraction must be in [0, 1), got {raw!r}; "
            "a reserve of the whole shortlist leaves no exploit lane, and the "
            "score is what connects an iteration to the objective")
    return float(raw)


def _distil_every(coordinator: dict) -> int:
    """Read and check `[coordinator] distil_every`.

    Like `explore_fraction`, `0` is a decision -- the domain has turned
    distillation off -- and must not be defaulted back on, so this tests for
    the key rather than for falsiness."""
    if "distil_every" not in coordinator:
        return DISTIL_EVERY
    raw = coordinator["distil_every"]
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise ConfigError(
            f"coordinator.distil_every must be a non-negative integer, got {raw!r}; "
            "0 turns distillation off")
    return raw


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
