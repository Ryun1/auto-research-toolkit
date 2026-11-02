"""Member domains: one checkout, several goals (#63).

The core runs one goal per domain, so a campaign with several goals is
several domains. starks-research ran five from one checkout by hand: an
umbrella at the root and members under `domains/`, each a complete domain.
This module is the thin layer that makes the relationship explicit:

    # the umbrella's domain.toml
    [domains]
    members = ["domains/*"]     # globs relative to this file

A member's `domain.toml` does not name its umbrella. The umbrella is found by
walking up from the member to the nearest `domain.toml` whose globs match it,
so a member stays loadable on its own and the relationship is written down
in exactly one place.

`Workspace` is the umbrella plus its members. A domain with no umbrella is a
one-domain workspace, so callers never branch on "is there an umbrella".
(Not `workspaces.py`: that module means worker slots.)

What loading refuses, each by name:
- a glob that matches no directory, and a matched directory with no
  `domain.toml` (default-deny, like `[lanes]`);
- a member outside the umbrella's directory (a glob that reaches the
  umbrella's own directory, `domains/..` say, skips it);
- nesting: a member that declares members, or a member inside another;
- an id prefix declared by two domains in the workspace. Unique prefixes are
  what let an id like `C1` name one entry across the workspace with no
  namespace syntax.
"""
from __future__ import annotations

import pathlib
import re
import tomllib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .config import CONFIG_NAME, DomainConfig
from .errors import ConfigError

if TYPE_CHECKING:
    from .entries import Entry

#: how `ar domains` and messages name the umbrella's own directory
UMBRELLA_PATH = "."


def _expand(root: pathlib.Path, globs, strict: bool) -> dict[str, pathlib.Path]:
    """Member directories matched by `globs`, keyed by their path relative to
    `root`, in glob order. `strict` refuses every problem; otherwise a glob
    that matches badly just contributes nothing (used while walking up, where
    a malformed umbrella is reported when it is loaded, not when passed)."""
    found: dict[str, pathlib.Path] = {}
    for pattern in globs:
        if pathlib.PurePath(pattern).is_absolute():
            if strict:
                raise ConfigError(
                    f"[domains] member glob {pattern!r} is absolute; members "
                    f"are globs relative to the umbrella's {CONFIG_NAME}")
            continue
        try:
            matches = sorted(p for p in root.glob(pattern) if p.is_dir())
        except (ValueError, NotImplementedError) as exc:
            if strict:
                raise ConfigError(
                    f"[domains] member glob {pattern!r} is not a valid glob: "
                    f"{exc}") from None
            continue
        if not matches and strict:
            raise ConfigError(
                f"[domains] member glob {pattern!r} matches no directory "
                f"under {root}")
        for match in matches:
            resolved = match.resolve()
            if resolved == root:
                continue        # the umbrella is not its own member
            if not resolved.is_relative_to(root):
                if strict:
                    raise ConfigError(
                        f"[domains] member glob {pattern!r} matches {match}, "
                        "which is not inside the umbrella's directory; a "
                        "member lives below its umbrella")
                continue
            if not (resolved / CONFIG_NAME).exists():
                if strict:
                    raise ConfigError(
                        f"[domains] member glob {pattern!r} matches "
                        f"{resolved.relative_to(root).as_posix()}, which has "
                        f"no {CONFIG_NAME}; narrow the glob or scaffold the "
                        "member")
                continue
            found.setdefault(resolved.relative_to(root).as_posix(), resolved)
    return found


def _load_domain(root: pathlib.Path, label: str = "") -> DomainConfig:
    """Load another domain of the workspace, naming it in any refusal.

    A member's `ar validate` and loop start now read its umbrella and its
    siblings. Their problems must say whose they are -- "domain.goal points
    at 'goal.yaml'" from a sibling reads as this domain's own -- and a
    malformed TOML there must be a refusal, not a traceback in a domain that
    never touched the file."""
    try:
        return DomainConfig.load(root)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{label + ': ' if label else ''}{root / CONFIG_NAME} "
                          f"is not valid TOML: {exc}") from None
    except ConfigError as exc:
        if not label:
            raise
        raise ConfigError(f"{label}: {exc}") from None


def _read_raw(config_path: pathlib.Path) -> dict | None:
    """A `domain.toml` as raw TOML, or None when it cannot be read: walking
    up must not fail on an unrelated directory's config."""
    try:
        with config_path.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return None


#: a `[domains]` header, found by text in a file that does not parse
_DOMAINS_HEADER = re.compile(r"^\s*\[\s*domains\s*\]", re.MULTILINE)


def _read_ancestor(config_path: pathlib.Path) -> dict | None:
    """`_read_raw` for a `domain.toml` above a domain, except that one which
    does not parse but declares `[domains]` is refused: it is plausibly this
    domain's umbrella, and a member loaded without it would run without the
    umbrella's `human_only` and `never_push_remotes` rules -- fail-open."""
    try:
        with config_path.open("rb") as fh:
            return tomllib.load(fh)
    except OSError:
        return None
    except tomllib.TOMLDecodeError as exc:
        try:
            text = config_path.read_text(errors="replace")
        except OSError:
            return None
        if _DOMAINS_HEADER.search(text):
            raise ConfigError(
                f"{config_path} is not valid TOML ({exc}) and declares "
                "[domains]; a member below it cannot load without its "
                "umbrella's safety rules -- fix that file") from None
        return None


def _globs_in(data: dict | None) -> tuple[str, ...]:
    domains = (data or {}).get("domains")
    members = domains.get("members") if isinstance(domains, dict) else None
    if not isinstance(members, list):
        return ()
    return tuple(g for g in members if isinstance(g, str) and g.strip())


def _declared_globs(config_path: pathlib.Path) -> tuple[str, ...]:
    """`[domains] members` read straight from a file, without loading the
    domain."""
    return _globs_in(_read_raw(config_path))


def umbrella_raw(root) -> tuple[pathlib.Path, dict] | None:
    """The root and raw TOML of the umbrella whose member globs match `root`,
    found by the same walk as `Workspace.for_domain`; None outside one.

    Raw, never a `DomainConfig`: `DomainConfig.load` calls this to merge the
    inherited tables, so loading the umbrella here would recurse, and would
    make a member's load fail on the umbrella's own problems -- which
    `check()` reports through `Workspace.for_domain` instead. An outer
    file that does not parse is passed over, unless it declares `[domains]`:
    then it is refused (`_read_ancestor`), never silently not inherited."""
    root = pathlib.Path(root).resolve()
    for candidate in root.parents:
        config_path = candidate / CONFIG_NAME
        if not config_path.exists():
            continue
        data = _read_ancestor(config_path)
        globs = _globs_in(data)
        if not globs:
            continue
        if root in _expand(candidate, globs, strict=False).values():
            return candidate, data
    return None


def unparseable_ancestors(root) -> list[str]:
    """A problem for each `domain.toml` above `root` that is not valid TOML.

    `umbrella_raw` passes over such a file when it does not declare
    `[domains]` (one that does refuses the load), so a domain's load never
    fails on an unrelated outer config. If the file was meant as this
    domain's umbrella, the domain is running without its shared tables and
    safety rules, and `ar validate` must say so rather than pass."""
    problems = []
    for candidate in pathlib.Path(root).resolve().parents:
        config_path = candidate / CONFIG_NAME
        if not config_path.exists():
            continue
        try:
            with config_path.open("rb") as fh:
                tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            problems.append(
                f"{config_path} is not valid TOML ({exc}); if it is this "
                "domain's umbrella, its shared tables and safety rules are "
                "NOT applied")
    return problems


@dataclass
class Workspace:
    umbrella: DomainConfig
    #: member configs keyed by their path relative to the umbrella root,
    #: posix-style (`domains/t1`), in the order the globs found them
    members: dict[str, DomainConfig] = field(default_factory=dict)

    @property
    def root(self) -> pathlib.Path:
        return self.umbrella.paths.root

    def domains(self) -> list[tuple[str, DomainConfig]]:
        """Every domain in the workspace, umbrella first."""
        return [(UMBRELLA_PATH, self.umbrella), *self.members.items()]

    def path_of(self, config: DomainConfig) -> str:
        """`config`'s path in this workspace, as `domains()` names it."""
        for path, member in self.domains():
            if member.paths.root == config.paths.root:
                return path
        raise ConfigError(f"{config.paths.root} is not in the workspace at "
                          f"{self.root}")

    def owner_of(self, entry_id: str) -> tuple[str, DomainConfig] | None:
        """The domain whose track prefix `entry_id` carries, with its path;
        None when no domain owns it. Prefix matching only -- prefixes are
        unique across the workspace -- so this never touches a store."""
        for path, config in self.domains():
            if any(track.is_id(entry_id) for track in config.tracks.values()):
                return path, config
        return None

    def entry(self, entry_id: str) -> tuple[DomainConfig, Entry] | None:
        """The owning domain and the entry, or None when no domain owns the
        id or the owner has no such file. A file that exists but does not
        load still raises its `SchemaError`: that is a problem to name."""
        from .entries import Store
        owner = self.owner_of(entry_id)
        if owner is None:
            return None
        config = owner[1]
        store = Store(config.paths.entries)
        if not store.path(entry_id).exists():
            return None
        return config, store.load(entry_id)

    # -- construction -----------------------------------------------------

    @classmethod
    def load(cls, root) -> Workspace:
        """The workspace whose umbrella is at `root`. A domain declaring no
        members loads as a one-domain workspace."""
        umbrella = DomainConfig.load(root)
        return cls._with_members(umbrella)

    @classmethod
    def for_domain(cls, config: DomainConfig) -> Workspace:
        """The workspace `config` belongs to: as a member when an umbrella
        above it lists it, as the umbrella when it lists members, else alone.

        The walk goes up from the domain's root and stops at the first
        `domain.toml` whose member globs match it."""
        root = config.paths.root
        for candidate in root.parents:
            config_path = candidate / CONFIG_NAME
            if not config_path.exists():
                continue
            globs = _declared_globs(config_path)
            if not globs:
                continue
            matched = _expand(candidate, globs, strict=False)
            if root not in matched.values():
                continue
            # Everything wrong up there is the umbrella's, not this domain's:
            # its own load, its globs, its members, its prefixes.
            try:
                workspace = cls._with_members(_load_domain(candidate))
            except ConfigError as exc:
                raise ConfigError(f"umbrella at {candidate}: {exc}") from None
            # Keep the caller's object: a verb that already loaded its own
            # config must not be handed a second copy of it.
            for path, member in workspace.members.items():
                if member.paths.root == root:
                    workspace.members[path] = config
            return workspace
        return cls._with_members(config)

    @classmethod
    def _with_members(cls, umbrella: DomainConfig) -> Workspace:
        root = umbrella.paths.root
        members: dict[str, DomainConfig] = {}
        if umbrella.members:
            for path, member_root in _expand(root, umbrella.members,
                                             strict=True).items():
                member = _load_domain(member_root, f"member {path}")
                if member.members:
                    raise ConfigError(
                        f"member {path} declares members of its own; member "
                        "domains nest one level only -- list them in the "
                        f"umbrella's [domains] instead")
                members[path] = member
            _refuse_nested(members)
        workspace = cls(umbrella=umbrella, members=members)
        _refuse_shared_prefixes(workspace)
        return workspace


def _refuse_nested(members: dict[str, DomainConfig]) -> None:
    """A member inside another member's directory: `discover()` from inside
    it finds the inner one, and the outer one's lanes and paths reach in."""
    roots = {path: m.paths.root for path, m in members.items()}
    for inner, inner_root in roots.items():
        for outer, outer_root in roots.items():
            if inner != outer and inner_root.is_relative_to(outer_root):
                raise ConfigError(
                    f"member {inner} is inside member {outer}; member domains "
                    "nest one level only")


def _label(path: str, config: DomainConfig) -> str:
    role = "umbrella" if path == UMBRELLA_PATH else "member"
    return f"{role} {config.name!r} ({path})"


def _refuse_shared_prefixes(workspace: Workspace) -> None:
    owners: dict[str, tuple[str, str]] = {}
    for path, config in workspace.domains():
        label = _label(path, config)
        for track in config.tracks.values():
            # Two tracks of one domain sharing a prefix is that domain's own
            # problem, which `DomainConfig.check` already names.
            if track.prefix in owners and owners[track.prefix][0] != path:
                raise ConfigError(
                    f"id prefix {track.prefix!r} is declared by both "
                    f"{owners[track.prefix][1]} and {label}; prefixes must be "
                    "unique across the umbrella and its members, so an id "
                    "names one entry -- rename one of the tracks' prefixes")
            owners.setdefault(track.prefix, (path, label))


def workspace_or_none(config: DomainConfig) -> Workspace | None:
    """`Workspace.for_domain(config)`, or None when the umbrella or a sibling
    is broken. A verb that merely looks across domains must not die on
    another domain's problem, so callers treat None as "this domain only";
    `ar validate` is where the breakage is reported."""
    try:
        return Workspace.for_domain(config)
    except ConfigError:
        return None


def resolve(config: DomainConfig, entry_id: str) -> tuple[DomainConfig, Entry]:
    """The entry `entry_id` names and the domain that owns it, looked up in
    `config`'s workspace (just `config` when it has none)."""
    workspace = Workspace.for_domain(config)
    owner = workspace.owner_of(entry_id)
    if owner is None:
        prefixes = [t.prefix for _, c in workspace.domains()
                    for t in c.tracks.values()]
        raise ConfigError(
            f"no domain in this workspace owns entry id {entry_id!r}; "
            f"prefixes are {prefixes}")
    from .entries import Store
    owning = owner[1]
    return owning, Store(owning.paths.entries).load(entry_id)


def shared_workspace(config: DomainConfig) -> Workspace | None:
    """`config`'s workspace when it has other domains to look into, else
    None. Every cross-member lookup goes through this, so a domain with no
    umbrella -- and one whose workspace is broken -- reads only its own store
    and says exactly what it said before members existed."""
    workspace = workspace_or_none(config)
    return workspace if workspace is not None and workspace.members else None


def foreign_owner(workspace: Workspace | None, config: DomainConfig,
                  entry_id: str) -> tuple[str, DomainConfig] | None:
    """The workspace path and config of the domain owning `entry_id`, when
    that is a domain other than `config`; None otherwise."""
    if workspace is None:
        return None
    owner = workspace.owner_of(entry_id)
    if owner is None or owner[1].paths.root == config.paths.root:
        return None
    return owner


def describe(path: str) -> str:
    """A workspace path as a message names it: `member domains/b`, or `the
    umbrella` (its path, `.`, reads as punctuation in a sentence)."""
    return "the umbrella" if path == UMBRELLA_PATH else f"member {path}"


# -- `ar domains` -------------------------------------------------------------

def rows(workspace: Workspace, current: DomainConfig | None = None) -> list[dict]:
    """One row per domain, umbrella first: what `ar domains` prints."""
    from .entries import Store
    out = []
    for path, config in workspace.domains():
        open_, closed = 0, 0
        # read_all, not all: one malformed file in one member must not hide
        # every domain's counts. The row says how many did not load.
        entries, problems = Store(config.paths.entries).read_all()
        for entry in entries:
            try:
                machine = config.track_for(entry.id).machine
            except ConfigError:
                continue        # `ar validate` reports an unowned id
            if entry.is_open(machine):
                open_ += 1
            else:
                closed += 1
        out.append({
            "path": path,
            "role": "umbrella" if path == UMBRELLA_PATH else "member",
            "name": config.name,
            "root": str(config.paths.root),
            "prefixes": [t.prefix for t in config.tracks.values()],
            "open": open_,
            "closed": closed,
            "unreadable": len(problems),
            "current": (current is not None
                        and config.paths.root == current.paths.root),
        })
    return out


def render_rows(rows_: list[dict]) -> str:
    header = ("PATH", "NAME", "PREFIXES", "OPEN", "CLOSED")
    table = [header] + [
        ((("* " if r["current"] else "  ") + r["path"]
          + (" (umbrella)" if r["role"] == "umbrella" and len(rows_) > 1 else "")),
         r["name"], " ".join(r["prefixes"]), str(r["open"]), str(r["closed"]))
        for r in rows_]
    table[0] = ("  " + header[0], *header[1:])
    widths = [max(len(row[i]) for row in table) for i in range(len(header))]
    lines = ["  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip()
             for row in table]
    lines += [f"! {r['path']}: {r['unreadable']} entry file(s) do not load; "
              f"run `ar --domain {r['root']} validate`"
              for r in rows_ if r["unreadable"]]
    return "\n".join(lines) + "\n"


def effective_rows(config: DomainConfig) -> list[dict]:
    """What `ar domains show` prints: every key of every inherited table in
    `config`'s effective config, with where it came from.

    `source` is `umbrella` or `member` for a key a file sets (a domain with no
    umbrella sets all of its own: `member`), and `default` for the typed
    defaults the loader supplies when neither file does -- those carry the
    loaded value. Values are the merged raw TOML, so the merge is recomputed
    here rather than stored on the config. `skills.dir` is resolved against
    this domain's root, the directory the loader actually reads, whichever
    file named it. A list-valued table (`[[remote]]`) is one row, key `*`; a
    policy rule that accumulates is one row per rule, `human_only[<pattern>]`.
    """
    from . import inherit
    raw = _read_raw(config.paths.root / CONFIG_NAME) or {}
    sources: dict[str, str] = {}
    found = umbrella_raw(config.paths.root)
    if found is not None:
        raw, sources = inherit.merge(found[1], raw)
    root = config.paths.root
    policy = config.policy
    defaults = {
        "coordinator": {
            "risk": config.risk, "tree_max_depth": config.tree_max_depth,
            "tree_max_children": config.tree_max_children,
            "branch_stagnation": config.branch_stagnation,
            "confirm_runs": config.confirm_runs,
            "distil_every": config.distil_every, "dispatch": config.dispatch},
        "upstream": {"url": config.upstream.url, "ref": config.upstream.ref,
                     "min_core": config.upstream.min_core},
        "session": {"parent": config.session_parent,
                    "settle_hours": config.session_settle_hours},
        "skills": {"dir": config.skills.dir,
                   "max_lines": config.skills.max_lines},
        "policy": {"spend_ceiling": policy.spend_ceiling},
    }
    out: list[dict] = []

    def add(table, key, value, source):
        if (table, key) == ("skills", "dir"):
            value = str(root / value)
        out.append({"table": table, "key": key, "value": value,
                    "source": source})

    for table in inherit.INHERITED:
        value = raw.get(table)
        if isinstance(value, dict):
            for key, item in value.items():
                if (table == "policy" and key in inherit._ACCUMULATED
                        and isinstance(item, list)):
                    for rule in item:
                        pattern = inherit._pattern(rule)
                        add(table, f"{key}[{pattern}]", rule, sources.get(
                            f"policy.{key}[{pattern}]", inherit.MEMBER))
                else:
                    add(table, key, item,
                        sources.get(f"{table}.{key}", inherit.MEMBER))
        elif value is not None:
            add(table, "*", value, sources.get(table, inherit.MEMBER))
        for key, item in defaults.get(table, {}).items():
            if not (isinstance(value, dict) and key in value):
                add(table, key, item, "default")
    return out


def render_effective(rows_: list[dict]) -> str:
    """One line per row: `[budgets] iteration_fanout = 9.0    member`."""
    import json
    return "".join(
        f"[{r['table']}] {r['key']} = {json.dumps(r['value'], default=str)}"
        f"    {r['source']}\n" for r in rows_)


# -- `ar domains relink` (#65 migration) --------------------------------------

#: what `ar close --superseded-by <url>` writes; relink reads it back
EXTERNAL_SUPERSEDE = "retired as superseded by external record "


@dataclass
class Relink:
    old: DomainConfig
    old_id: str
    url: str
    new: DomainConfig
    new_id: str


def _url_target(workspace: Workspace, url: str) -> tuple[DomainConfig, str] | None:
    """The workspace entry `url` points at: its path must end with
    `<domain path>/<entries dir>/<ID>.yaml` for the domain owning ID, and
    that entry must exist. Ids are unique across the workspace, so the owner
    decides which domain's path is expected -- an umbrella's bare
    `<entries dir>/<ID>.yaml` cannot swallow a member's URL."""
    from urllib.parse import urlparse
    from .entries import Store
    path = urlparse(url).path if "://" in url else url
    name = path.rsplit("/", 1)[-1]
    if not name.endswith(".yaml"):
        return None
    new_id = name[:-len(".yaml")]
    owner = workspace.owner_of(new_id)
    if owner is None:
        return None
    domain_path, config = owner
    rel = config.paths.entries.relative_to(config.paths.root).as_posix()
    tail = f"{rel}/{new_id}.yaml"
    if domain_path != UMBRELLA_PATH:
        tail = f"{domain_path}/{tail}"
    if path != tail and not path.endswith("/" + tail):
        return None
    if not Store(config.paths.entries).path(new_id).exists():
        return None
    return config, new_id


def relinks(workspace: Workspace) -> list[Relink]:
    """Entries retired as superseded by an external URL that now names an
    entry in this workspace. Once relinked, the latest `superseded` event no
    longer carries the URL form, so a second pass finds nothing."""
    from .entries import Store
    found = []
    for _, config in workspace.domains():
        for entry in Store(config.paths.entries).all():
            if entry.status != "superseded":
                continue
            events = [e for e in entry.history if e.kind == "superseded"]
            if not events or not events[-1].detail.startswith(EXTERNAL_SUPERSEDE):
                continue
            url = events[-1].detail[len(EXTERNAL_SUPERSEDE):].strip()
            target = _url_target(workspace, url)
            if target is None or target[1] == entry.id:
                continue
            found.append(Relink(config, entry.id, url, target[0], target[1]))
    return found
