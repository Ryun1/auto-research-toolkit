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
from .gates import Gate
from .gates import definitions as gate_definitions
from .goal import Goal
from .hardware import Requirement
from .lanes import Lanes
from .policy import Policy
from .rank import COVERAGE_FRACTION, EXPLORE_FRACTION
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
              "skills", "upstream", "brain", "plugins", "session"}

#: Option keys a `[brain]` table may carry for the built-in TypeSafe brain
#: (`value = "typesafe"`), beyond `default` / role names / `authorize_spend`.
#: An unrecognized `typesafe_*` key is refused at load, not ignored -- a
#: misspelled price is a cost meter that silently reads zero.
TYPESAFE_BRAIN_KEYS = {"typesafe_model", "typesafe_url",
                       "typesafe_input_per_mtok", "typesafe_output_per_mtok"}


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
    gates: list[Gate] = field(default_factory=list)

    def __post_init__(self):
        self.machine.required_gates = tuple(g.name for g in self.gates if g.required)

    def is_id(self, entry_id: str) -> bool:
        return entry_id.startswith(self.prefix) and entry_id[len(self.prefix):].isdigit()


@dataclass
class Upstream:
    """Where the core comes from, for `ar harness check` and
    `ar harness update`. Defaults are recovered from pip's own install record;
    a fork or a mirror is a one-line domain decision here, and `ref` names the
    branch or tag to follow -- pin it to a tag to make updates opt-in."""
    url: str = ""
    ref: str = ""

    @classmethod
    def from_dict(cls, spec: dict) -> Upstream:
        unknown = set(spec) - {"url", "ref"}
        if unknown:
            raise ConfigError(
                f"[upstream] has unknown key(s) {sorted(unknown)}; "
                f"known: ['url', 'ref']")
        url = spec.get("url", "")
        if not isinstance(url, str):
            raise ConfigError(f"upstream.url must be a string, got {url!r}")
        ref = spec.get("ref", "")
        if not isinstance(ref, str):
            raise ConfigError(f"upstream.ref must be a string, got {ref!r}")
        return cls(url=url, ref=ref)


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
    #: which backend serves each role. `"claude"` names the built-in SDK brain,
    #: `"typesafe"` the built-in TypeSafe (Jev) brain; a list of strings is a
    #: command run per the ProcessBrain contract in `driver/brain.py`.
    #: Validated at load -- an unknown role or placeholder
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
    #: share of each shortlist reserved for entries probing a mechanism tag no
    #: terminal entry has tested. Typed for the same reasons as
    #: `explore_fraction`: it is a coverage appetite, it reaches a float
    #: multiplication in `Ranking.shortlist`, and the reserves are checked
    #: together at load -- two fractions summing to a whole shortlist leave no
    #: exploit lane, which is a load-time refusal, not a mid-iteration surprise.
    coverage_fraction: float = COVERAGE_FRACTION
    #: run the distil phase every N iterations; 0 turns it off. Typed out of the
    #: coordinator dict for the same reason as `explore_fraction`: it decides
    #: whether a phase runs at all, and discovering it was misspelled mid-loop
    #: costs the iteration that would have distilled.
    distil_every: int = DISTIL_EVERY
    #: how dispatch hands work out. `"worker"` (default) runs workers in this
    #: process through the brain; `"native"` reserves each card through the
    #: external handoff and leaves the work to the surrounding harness's own
    #: subagents -- no model dispatch, no API spend, verdicts applied by
    #: `ar external complete`. Validated at load: a misspelled mode is a loop
    #: that silently became a different loop.
    dispatch: str = "worker"
    #: domain tooling the core loads by name. Each entry names an importable
    # module (resolved with the domain root on `sys.path`, so a package living
    # beside `domain.toml` works) exposing `register_cli(subparsers, config)`;
    # the module's subcommands run through the same policy engine as every
    # core verb. Declared here because qsbtools reached the same extension by
    # composing `build_parser()` privately -- an undeclared seam is one every
    # domain re-invents, differently.
    plugins: tuple[str, ...] = ()
    #: parent directory for `ar session` worktrees. `None` means a sibling of
    # the domain root (the shape the field harness converged on: a session
    # worktree sits beside the checkout it came from, where a human can see
    # it); a relative path resolves against the domain root, an absolute path
    # is used as-is. Declared because `discover()` exists precisely because
    # path-of-execution-dependent layout (H19) made the same command do
    # different things from different directories.
    session_parent: str | None = None
    #: hours a session worktree may sit idle before `ar session prune` offers
    # it for destruction. A settle window, not a TTL: prune only proposes;
    # nothing is destroyed without the destroy path's own guards.
    session_settle_hours: float = 24.0
    #: what each experiment class needs of the machine, checked before it runs
    hardware: dict = field(default_factory=dict)
    #: rentable classes, each costed only if someone measured its ratio
    remote: list = field(default_factory=list)
    #: where the core comes from, for `ar harness check` / `update`
    upstream: Upstream = field(default_factory=Upstream)
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
        # A required gate no track defines would hold the loop open forever --
        # should_stop would run with the stop refused and nothing able to pass
        # it. Checked here because Goal cannot see the tracks at load.
        gate_names = {g.name for t in self.tracks.values() for g in t.gates}
        for name in self.goal.required_gates:
            if name not in gate_names:
                problems.append(
                    f"goal.required_gates names {name!r}, which no track's "
                    f"[[tracks.gates]] defines; declared: "
                    f"{sorted(gate_names) or '(none)'}")
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
        # H135's shape in config form: `plugins = [...]` typed under [domain]
        # is a valid [domain] key to TOML and an invisible one to the loader --
        # the seam reads as absent and every plugin verb is an invalid choice.
        misplaced = sorted({"plugins", "session"} & set(domain))
        if misplaced:
            raise ConfigError(
                f"{misplaced} belong at the top level of {CONFIG_NAME}, not "
                f"inside [domain] -- a seam declared in the wrong table reads "
                f"as no seam at all. Move {misplaced} above the [domain] table.")

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
                gates=gate_definitions(spec.get("gates", [])),
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
            explore_fraction=(explore_fraction := _explore_fraction(coordinator)),
            coverage_fraction=_coverage_fraction(
                coordinator, explore_fraction),
            distil_every=_distil_every(coordinator),
            dispatch=_dispatch_mode(coordinator),
            plugins=_plugins(data),
            session_parent=_session(data.get("session") or {})["parent"],
            session_settle_hours=_session(data.get("session") or {})["settle_hours"],
            hardware={name: Requirement.from_dict(name, spec)
                      for name, spec in (data.get("hardware") or {}).items()},
            remote=[RemoteClass.from_dict(spec)
                    for spec in (data.get("remote") or [])],
            upstream=Upstream.from_dict(dict(data.get("upstream") or {})))


def _brain_spec(data) -> dict:
    """Read and check the top-level `brain` table.

    Keys are `default`, a role name, `authorize_spend`, or one of
    `TYPESAFE_BRAIN_KEYS`; values are `"claude"` (the built-in SDK brain),
    `"typesafe"` (the built-in TypeSafe/Jev brain), a non-empty list of
    strings -- the command the ProcessBrain contract runs -- or, for
    `authorize_spend` and the `typesafe_*` options, a bool / string / number
    per key. Both built-in brains can incur API charges and are fail-closed:
    without `authorize_spend = true`, a domain naming one is refused at load,
    because a default that can spend without saying so is a bill waiting to
    happen.
    """
    from .driver.brain import Role

    spec = {}
    for key, value in dict(data or {}).items():
        if key == "authorize_spend":
            if not isinstance(value, bool):
                raise ConfigError(
                    f"brain.authorize_spend: must be true or false, got {value!r}")
            spec[key] = value
            continue
        if key in TYPESAFE_BRAIN_KEYS:
            if key in ("typesafe_model", "typesafe_url") \
                    and (not isinstance(value, str) or not value.strip()):
                raise ConfigError(f"brain.{key}: must be a non-empty string")
            if key.endswith("_per_mtok") \
                    and (isinstance(value, bool)
                         or not isinstance(value, (int, float)) or value < 0):
                raise ConfigError(
                    f"brain.{key}: must be a nonnegative number of US dollars "
                    f"per million tokens, got {value!r}")
            spec[key] = value
            continue
        if key.startswith("typesafe_"):
            raise ConfigError(
                f"brain.{key}: unknown typesafe option; allowed: "
                f"{sorted(TYPESAFE_BRAIN_KEYS)}")
        if key != "default" and key not in Role.ALL:
            raise ConfigError(
                f"brain.{key}: not a role; keys are 'default' or one of "
                f"{', '.join(Role.ALL)}")
        if value in ("claude", "typesafe"):
            spec[key] = value
            continue
        if (not isinstance(value, list) or not value
                or not all(isinstance(part, str) for part in value)):
            raise ConfigError(
                f"brain.{key}: must be \"claude\", \"typesafe\", or a "
                f"non-empty list of strings, got {value!r}")
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


def _coverage_fraction(coordinator: dict, explore_fraction: float) -> float:
    """Read and check `[coordinator] coverage_fraction`.

    Same rules as `explore_fraction`: absent means the core default, `0` is a
    domain's decision against the reserve and is never defaulted back. The
    combined check applies to the *effective* value, defaulted or declared --
    the sum is clamped in `Ranking.shortlist`, but a config whose reserves
    total a whole shortlist is refused at load rather than silently
    truncated."""
    if "coverage_fraction" not in coordinator:
        raw: float | int = COVERAGE_FRACTION
    else:
        raw = coordinator["coverage_fraction"]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ConfigError(
                f"coordinator.coverage_fraction must be a number, got {raw!r}")
        if not 0.0 <= raw < 1.0:
            raise ConfigError(
                f"coordinator.coverage_fraction must be in [0, 1), got {raw!r}; "
                "a reserve of the whole shortlist leaves no exploit lane, and "
                "the score is what connects an iteration to the objective")
    if float(raw) + explore_fraction >= 1.0:
        raise ConfigError(
            f"coordinator.explore_fraction {explore_fraction} + "
            f"coverage_fraction {raw} reserves the whole shortlist; at least "
            "one slot must stay with the score. If you have not set "
            "coverage_fraction, the core default (0.2) now participates in "
            "this check: set `coverage_fraction = 0` to keep the reserve "
            "composition this domain had before coverage existed, or lower "
            "explore_fraction to share the shortlist between them.")
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


DISPATCH_MODES = ("worker", "native")


def _dispatch_mode(coordinator: dict) -> str:
    """Read and check `[coordinator] dispatch`.

    `"worker"` runs this process's brain; `"native"` hands each shortlisted
    card to the surrounding harness's own subagents through the external
    handoff. Anything else is a loop that became a different loop at
    dispatch time -- refused at load instead.
    """
    raw = coordinator.get("dispatch", "worker")
    if raw not in DISPATCH_MODES:
        raise ConfigError(
            f"coordinator.dispatch must be one of {DISPATCH_MODES}, got {raw!r}")
    return raw


def _plugins(data) -> tuple[str, ...]:
    """Read and check the top-level `plugins` list.

    Each entry names an importable module that exposes
    `register_cli(subparsers, config)`. Refused here as data problems; import
    and contract failures are refused at load time in `plugins.py`, which is
    the module that resolves the names."""
    raw = data.get("plugins") or ()
    if isinstance(raw, (str, dict)) or not isinstance(raw, (list, tuple)):
        raise ConfigError(
            "plugins must be a list of module names, e.g. plugins = [\"qsbtools\"]")
    names = []
    for item in raw:
        if not isinstance(item, str) or not item or item != item.strip():
            raise ConfigError(
                f"plugin name must be a non-empty module name, got {item!r}")
        if (any(ch.isspace() for ch in item) or item.startswith((".", "-"))
                or "/" in item or "\\" in item):
            raise ConfigError(f"plugin name {item!r} is not a module name")
        names.append(item)
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise ConfigError(f"plugins lists {dupes} more than once")
    return tuple(names)


def _session(data) -> dict:
    """Read and check `[session]`: the worktree parent and the settle window."""
    parent = data.get("parent")
    if parent is not None and (not isinstance(parent, str) or not parent.strip()):
        raise ConfigError(f"session.parent must be a path string, got {parent!r}")
    settle = data.get("settle_hours", 24.0)
    if isinstance(settle, bool) or not isinstance(settle, (int, float)) or settle <= 0:
        raise ConfigError(
            f"session.settle_hours must be a positive number, got {settle!r}")
    return {"parent": parent, "settle_hours": float(settle)}


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
