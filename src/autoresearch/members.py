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
import tomllib
from dataclasses import dataclass, field

from .config import CONFIG_NAME, DomainConfig
from .errors import ConfigError

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
    `check()` reports through `Workspace.for_domain` instead. An umbrella
    that does not parse is not one: nothing is inherited from it."""
    root = pathlib.Path(root).resolve()
    for candidate in root.parents:
        config_path = candidate / CONFIG_NAME
        if not config_path.exists():
            continue
        data = _read_raw(config_path)
        globs = _globs_in(data)
        if not globs:
            continue
        if root in _expand(candidate, globs, strict=False).values():
            return candidate, data
    return None


def unparseable_ancestors(root) -> list[str]:
    """A problem for each `domain.toml` above `root` that is not valid TOML.

    `umbrella_raw` passes over such a file, so a domain's load never fails
    on an unrelated outer config. But if the file is this domain's umbrella,
    the domain is running without the umbrella's shared tables and safety
    rules, and `ar validate` must say so rather than pass."""
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
