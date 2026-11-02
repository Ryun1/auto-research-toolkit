"""What a member domain takes from its umbrella (#64).

A member inherits the umbrella's shared tables and overrides what differs.
The merge works on raw TOML, before any table is parsed, so every check in
`DomainConfig.load` and after sees the effective config, and the umbrella is
never loaded to build its member.

Member-wins is the rule, key by key, except where a member winning would let
it weaken a safety rule by omission or override:

- `policy.human_only` and `policy.never_push_remotes` accumulate: the
  umbrella's rules, then the member's. A rule a member could shadow would be
  a comment.
- `policy.spend_ceiling` takes the smaller value; both ceilings must be in
  the same effective `policy.currency` (a member's unset currency is the
  umbrella's; the umbrella's unset one is the Policy default), or it is
  refused.
- `upstream.min_core` takes the higher floor: one core install serves every
  member.

`policy.forbidden_paths` is member-wins like any key, and its patterns are
relative to whichever domain's root loads them. A member that does not set
it inherits the umbrella's patterns, applied from the member's own root; a
member that sets it replaces the umbrella's list whole -- they do not
accumulate, since the umbrella's patterns would match the wrong files there.

Identity tables (`[domain]`, `[state]`, `[[tracks]]`, `[commands]`,
`[lanes]`, ...) are never inherited: each says which domain this is.
"""
from __future__ import annotations

import copy
import math

from .errors import ConfigError
from .policy import Policy
from .upstream import version_tuple

#: the umbrella tables a member inherits unless it sets them
INHERITED: tuple[str, ...] = ("policy", "budgets", "upstream", "coordinator",
                              "skills", "hardware", "remote", "brain",
                              "session")

#: policy rule lists that accumulate rather than override
_ACCUMULATED = ("human_only", "never_push_remotes")

UMBRELLA, MEMBER = "umbrella", "member"


def merge(umbrella: dict, member: dict) -> tuple[dict, dict[str, str]]:
    """`member`'s raw TOML with the umbrella's shared tables merged in, and
    where each inherited key came from, keyed `<table>.<key>` (accumulated
    rules as `policy.human_only[<pattern>]`). The result shares nothing
    with either input.

    A member value that would remove what the umbrella sets rather than
    override it -- `policy = false`, `human_only = {}`, a `spend_ceiling`
    that is a string -- is refused: the loader reads a falsy table as an
    empty one, so passing it through would silently drop every inherited
    safety rule."""
    umbrella, member = copy.deepcopy(umbrella), copy.deepcopy(member)
    data = dict(member)
    sources: dict[str, str] = {}
    for table in INHERITED:
        if table not in umbrella and table not in member:
            continue
        theirs, ours = umbrella.get(table), member.get(table)
        if table not in member:
            data[table] = theirs
            _record(sources, table, theirs, UMBRELLA)
        elif isinstance(theirs, dict) and not isinstance(ours, dict):
            raise ConfigError(
                f"[{table}] is {ours!r} in the member, not a table; the "
                f"umbrella's [{table}] cannot be removed this way -- set the "
                "keys that differ, or leave the table out to inherit it")
        elif not (isinstance(theirs, dict) and isinstance(ours, dict)):
            # The member's alone, or a shape only the parser can judge
            # (`[[remote]]` is a list, and a list replaces a list whole).
            _record(sources, table, ours, MEMBER)
        else:
            data[table] = _merge_table(table, theirs, ours, sources)
    return data, sources


def _record(sources: dict[str, str], table: str, value, side: str) -> None:
    if not isinstance(value, dict):
        sources[table] = side
        return
    for key, item in value.items():
        if table == "policy" and key in _ACCUMULATED and isinstance(item, list):
            for rule in item:
                if (pattern := _pattern(rule)) is not None:
                    sources[f"policy.{key}[{pattern}]"] = side
        else:
            sources[f"{table}.{key}"] = side


def _merge_table(table: str, theirs: dict, ours: dict,
                 sources: dict[str, str]) -> dict:
    merged = {**theirs, **ours}
    for key in merged:
        sources[f"{table}.{key}"] = MEMBER if key in ours else UMBRELLA
    if table == "policy":
        for key in _ACCUMULATED:
            if (isinstance(theirs.get(key), list) and key in ours
                    and not isinstance(ours[key], list)):
                raise ConfigError(
                    f"policy.{key} is {ours[key]!r} in the member, not a list "
                    f"of rules; the umbrella's policy.{key} rules cannot be "
                    "removed -- a member's rules are added to them")
            # Any other malformed (non-list) value is left member-wins for
            # `Policy.from_dict` to refuse; a list from one side only is
            # that side's rules, recorded rule by rule like an accumulation.
            if not isinstance(merged.get(key), list):
                continue
            del sources[f"policy.{key}"]
            merged[key] = _accumulate(
                key, theirs.get(key) if isinstance(theirs.get(key), list) else [],
                ours.get(key) if isinstance(ours.get(key), list) else [],
                sources)
        ceilings = (theirs.get("spend_ceiling"), ours.get("spend_ceiling"))
        if all(c is not None for c in ceilings):
            # min() of a string and a number is not a ceiling: "inf" would
            # win over 5.0 and the loader would read it as no limit at all.
            if not all(_is_number(c) for c in ceilings):
                raise ConfigError(
                    f"policy.spend_ceiling: the umbrella's {ceilings[0]!r} and "
                    f"the member's {ceilings[1]!r} must both be numbers (not "
                    "strings or NaN) so the smaller can be chosen")
            # min() of 5 EUR and 2 USD is not a ceiling in either currency.
            # Each side's currency is its effective one after the key-level
            # merge: the umbrella's own, else the Policy default; the
            # member's own, else the umbrella's (an unset member key is
            # inherited like any other).
            default = Policy.__dataclass_fields__["currency"].default
            umbrella_currency = theirs.get("currency", default)
            currencies = (umbrella_currency,
                          ours.get("currency", umbrella_currency))
            if currencies[0] != currencies[1]:
                raise ConfigError(
                    f"policy.spend_ceiling: the umbrella's is "
                    f"{ceilings[0]!r} {currencies[0]} and the member's "
                    f"{ceilings[1]!r} {currencies[1]}; the smaller of two "
                    "currencies is meaningless -- set the same "
                    "policy.currency in both")
            # Ties go to the umbrella: the value is the umbrella's either way.
            merged["spend_ceiling"] = min(ceilings)
            sources["policy.spend_ceiling"] = (
                UMBRELLA if ceilings[0] <= ceilings[1] else MEMBER)
    if table == "upstream" and theirs.get("min_core"):
        # An empty member floor is no floor, and never lowers the umbrella's.
        higher = (_higher_floor(theirs["min_core"], ours["min_core"])
                  if ours.get("min_core") else UMBRELLA)
        merged["min_core"] = (theirs if higher == UMBRELLA else ours)["min_core"]
        sources["upstream.min_core"] = higher
    return merged


def _accumulate(key: str, theirs: list, ours: list,
                sources: dict[str, str]) -> list:
    """The umbrella's rules, then the member's whose pattern is new. A
    malformed rule passes through for `Policy.from_dict` to refuse."""
    seen = set()
    out = []
    for side, rules in ((UMBRELLA, theirs), (MEMBER, ours)):
        for rule in rules:
            pattern = _pattern(rule)
            if pattern is not None:
                if pattern in seen:
                    continue
                seen.add(pattern)
                sources[f"policy.{key}[{pattern}]"] = side
            out.append(rule)
    return out


def _is_number(value) -> bool:
    """An int or float, infinity allowed; not a bool, a string or NaN."""
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and not math.isnan(value))


def _pattern(rule):
    return rule.get("pattern") if isinstance(rule, dict) else None


def _higher_floor(theirs: str, ours: str) -> str:
    """Which side's `min_core` is higher; ties go to the umbrella. A floor
    neither side can compare is refused, never guessed at."""
    a, b = version_tuple(str(theirs)), version_tuple(str(ours))
    if a is None or b is None:
        raise ConfigError(
            f"upstream.min_core: the umbrella's {theirs!r} and the member's "
            f"{ours!r} cannot be compared; write both as dotted release "
            "numbers (0.9.0) so the higher floor can be chosen")
    return MEMBER if b > a else UMBRELLA
