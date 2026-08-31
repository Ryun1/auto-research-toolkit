"""Never-rules as data, enforced generically.

In the harness this core is derived from, the two rules that protect what leaves
the machine -- never `git add -A` in the public clone, and never run the
submission command -- were prose in an agent brief plus bespoke hook code plus a
deny list in a harness-specific settings file. Three encodings of one rule.

Two entries say what that costs. **H89: nothing asserted either pre-commit hook
refuses anything, so deleting rule 1's enforcement left the whole gate green.**
And H31: nothing asserted the public clone's hook was even installed. A rule
whose enforcement can be deleted without a test failing is a comment.

So policy here is declarative, lives in `domain.toml`, is enforced by one engine,
and every rule ships with a negative test proving it refuses something. The
policy carries its own `selftest()` for exactly that reason: `ar validate` runs
it, and a policy that refuses nothing is itself a failure.

Rule kinds:

* `forbidden_paths`   -- globs that may never be staged or committed here
* `never_push_remotes`-- remotes that must never be pushed to, at all
* `human_only`        -- commands only a person may run; agents are refused
* `spend_ceiling`     -- money above this needs a person, per the human gates
"""
from __future__ import annotations

import fnmatch
import re
import shlex
from dataclasses import dataclass, field

from .errors import ConfigError, PolicyError


@dataclass(frozen=True)
class Rule:
    kind: str
    pattern: str
    reason: str
    #: an input this rule MUST refuse. Optional, but the honest way to make a
    #: rule testable: a synthesised probe cannot always be derived from a
    #: pattern that is a regex, a glob, or both at once.
    example: str = ""

    def probes(self) -> tuple:
        """Inputs the selftest tries. The first that is refused proves the rule
        does something; if none is, the rule refuses nothing and is reported.

        An earlier version synthesised exactly one probe by replacing `*` with
        `x`, which for a regex pattern like `ecdsafail\\s+submit` produced a
        probe the rule could not match -- and reported a WORKING rule as broken.
        That is the H138 shape in miniature: a false alarm trains the reader to
        skip the real ones. Declare `example` when in doubt."""
        if self.example:
            return (self.example,)
        pattern = self.pattern
        return tuple(dict.fromkeys([
            pattern,
            pattern.replace("*", "x"),
            pattern.replace(r"\s+", " ").replace(r"\s*", "").replace("*", "x"),
            re.sub(r"\\[sdw][+*?]?", " ", pattern).replace("*", "x").strip(),
        ]))

    def describe(self) -> str:
        return f"[{self.kind}] {self.pattern} -- {self.reason}"


@dataclass
class Policy:
    forbidden_paths: list[Rule] = field(default_factory=list)
    never_push_remotes: list[Rule] = field(default_factory=list)
    human_only: list[Rule] = field(default_factory=list)
    spend_ceiling: float | None = None
    currency: str = "USD"

    # -- checks ----------------------------------------------------------

    def check_paths(self, paths) -> None:
        """Refuse a staged/committed path set touching anything forbidden."""
        hits = []
        for path in paths:
            norm = str(path).lstrip("./")
            for rule in self.forbidden_paths:
                if fnmatch.fnmatch(norm, rule.pattern) or norm.startswith(
                        rule.pattern.rstrip("*").rstrip("/") + "/"):
                    hits.append((norm, rule))
        if hits:
            raise PolicyError(
                "refused by forbidden_paths:\n" + "\n".join(
                    f"  {p}\n      {r.describe()}" for p, r in hits))

    def check_push(self, remote: str, url: str = "") -> None:
        for rule in self.never_push_remotes:
            if fnmatch.fnmatch(remote, rule.pattern) or (
                    url and fnmatch.fnmatch(url, rule.pattern)):
                raise PolicyError(
                    f"refused: push to {remote!r}"
                    + (f" ({url})" if url else "") + f"\n  {rule.describe()}")

    def check_command(self, command) -> None:
        """Refuse a human-only command, including an attempt to route around it.

        Matching is on the normalised token stream, so `ecdsafail  submit` and
        `ecdsafail submit --yes` are both caught; and the raw string is checked
        too, so a rule written as a regex can catch an alias or a wrapper script.
        The brief this generalises spells the evasion out -- "no aliases, no
        scripts that call it" -- so the check must not be a bare equality test.
        """
        raw = command if isinstance(command, str) else " ".join(map(str, command))
        try:
            tokens = shlex.split(raw)
        except ValueError:
            tokens = raw.split()
        normalised = " ".join(tokens)
        for rule in self.human_only:
            if (fnmatch.fnmatch(normalised, rule.pattern)
                    or normalised.startswith(rule.pattern + " ")
                    or normalised == rule.pattern
                    or re.search(rule.pattern, raw)):
                raise PolicyError(
                    f"refused: {raw!r} is human-only.\n  {rule.describe()}\n"
                    "  An agent may prepare and recommend this action, never take it.")

    def check_spend(self, amount: float, note: str = "") -> None:
        if self.spend_ceiling is None:
            return
        if amount > self.spend_ceiling:
            raise PolicyError(
                f"refused: {amount:.2f} {self.currency} exceeds the "
                f"{self.spend_ceiling:.2f} {self.currency} ceiling a person must "
                f"approve{(' -- ' + note) if note else ''}")

    # -- proof that it refuses something (invariant 3) --------------------

    def selftest(self) -> list[str]:
        """Every declared rule must demonstrably refuse at least one input.

        This is the direct answer to H89. A rule that refuses nothing -- because
        its pattern is wrong, or because its enforcement was deleted -- reports
        here rather than passing quietly.
        """
        def refuses(rule, check):
            for probe in rule.probes():
                if not probe:
                    continue
                try:
                    check(probe)
                except PolicyError:
                    return True
            return False

        failures = []
        for rule in self.forbidden_paths:
            if not refuses(rule, lambda p: self.check_paths([p])):
                failures.append(
                    f"forbidden_paths rule refuses nothing: {rule.describe()}"
                    "  (declare `example` if the pattern is a regex)")
        for rule in self.never_push_remotes:
            if not refuses(rule, self.check_push):
                failures.append(
                    f"never_push_remotes rule refuses nothing: {rule.describe()}")
        for rule in self.human_only:
            if not refuses(rule, self.check_command):
                failures.append(
                    f"human_only rule refuses nothing: {rule.describe()}"
                    "  (declare `example` if the pattern is a regex)")
        return failures

    def describe(self) -> str:
        rules = (self.forbidden_paths + self.never_push_remotes + self.human_only)
        lines = [f"{len(rules)} rule(s) declared"]
        lines += [f"  {r.describe()}" for r in rules]
        if self.spend_ceiling is not None:
            lines.append(f"  [spend_ceiling] {self.spend_ceiling} {self.currency} "
                         "-- above this, a person approves")
        return "\n".join(lines)

    # -- construction ----------------------------------------------------

    @classmethod
    def from_dict(cls, data: dict) -> Policy:
        def rules(key):
            out = []
            for spec in data.get(key) or []:
                if isinstance(spec, str):
                    raise ConfigError(
                        f"policy.{key} entry {spec!r} has no reason. Every "
                        "never-rule states why, or the next agent deletes it.")
                try:
                    pattern, reason = spec["pattern"], spec["reason"]
                except KeyError as exc:
                    raise ConfigError(
                        f"policy.{key} entry is missing {exc}: {spec!r}") from exc
                # An empty pattern is not a disabled rule, it is a rule that
                # matches EVERYTHING -- `re.search("", x)` is always a hit. A
                # never-rule that refuses every command is as broken as one that
                # refuses none, and it fails in the direction nobody tests for.
                if not str(pattern).strip():
                    raise ConfigError(
                        f"policy.{key} entry has an empty pattern, which matches "
                        f"every input rather than none: {spec!r}")
                out.append(Rule(kind=key, pattern=pattern, reason=reason,
                                example=str(spec.get("example", ""))))
            return out

        ceiling = data.get("spend_ceiling")
        return cls(
            forbidden_paths=rules("forbidden_paths"),
            never_push_remotes=rules("never_push_remotes"),
            human_only=rules("human_only"),
            spend_ceiling=None if ceiling is None else float(ceiling),
            currency=data.get("currency", "USD"))
