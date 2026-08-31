"""Skills: distilled knowledge, cited to the record and checked against it.

A domain's `knowledge` list is hand-written prose whose only validation is that
the file exists. That is enough for one guide and wrong for twelve: the corpus
this core is derived from grew thirteen (median 195 lines, 2,730 total), at
which point "read the guides" is either a full-corpus read in every brief or a
guess. It also grew, necessarily, a separate tool whose whole job was failing
the build when a guide quoted a number the record had since moved.

A **skill** is both mechanisms brought inside core, per project:

* a `description` that says *when* to read it, carried in every brief so a role
  routes on two lines instead of loading two thousand;
* a `cites` list naming the entries that back it, checked against the store, so
  the prose cannot outlive its evidence.

The body is model prose and cannot be byte-identical to a re-render, so a skill
is **not** a generated view under invariant 1 -- it is a third category,
cited-and-checked prose, and `check()` is what stands in for regeneration.
`README.md` states the exception; this module is the compensating control.

Two notes on invariant 2 ("no regex over prose reaches a decision"). This module
does two regex scans of a skill body -- inline `[Q12]` citations, and the
derived-constant lint. Neither reads *state* out of prose: the state is read
from the records, and the scan only asks whether the prose still agrees with it.
A scan that can refuse but never assert is not the failure class the invariant
names. The lint is deliberately narrow for the same reason: a missed stale
number is a gap, a falsely flagged one teaches everyone to pass `--no-verify`.
"""
from __future__ import annotations

import datetime as dt
import pathlib
import re
from dataclasses import dataclass, field

import yaml

from .errors import AutoresearchError, SchemaError

SKILL_FILE = "SKILL.md"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_DESCRIPTION = 1024
#: what a description must open with. Borrowed verbatim from the skill-authoring
#: guidance this format follows: a description that summarises the *workflow*
#: gets followed instead of the skill, so it must state triggering conditions
#: and nothing else.
DESCRIPTION_OPENER = "use when"


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def core_version() -> str:
    """Which core wrote a skill. Stamped rather than left blank: closure
    semantics and the check rules both live in core, so a skill diagnosed a
    year later has to say what wrote it. `unknown` when the package is not
    installed -- a running-from-source checkout is a real case, and saying so
    beats an empty string that reads as 'nobody recorded this'."""
    from importlib import metadata
    try:
        return metadata.version("autoresearch")
    except metadata.PackageNotFoundError:
        return "unknown"


@dataclass
class Skill:
    name: str
    description: str
    #: entry ids this skill's claims rest on. Never empty: a skill nothing backs
    #: is an opinion, and the loop already has a role for those.
    cites: list[str] = field(default_factory=list)
    #: written by the coordinator, never by the role that wrote the body -- a
    #: role does not get to certify its own output.
    distilled: dict = field(default_factory=dict)
    body: str = ""
    #: absolute path on disk; not part of the record, so excluded from `render`
    path: pathlib.Path | None = None

    @property
    def lines(self) -> int:
        return len(self.body.splitlines())

    def index_entry(self, root: pathlib.Path) -> dict:
        """The two lines a brief carries. Deliberately not the body: routing is
        the point, and a brief that inlines every skill has re-created the
        problem skills exist to solve."""
        return {"name": self.name,
                "description": self.description,
                "path": str(self.path.relative_to(root)) if self.path else "",
                "cites": list(self.cites)}


def render(skill: Skill) -> str:
    """One place that turns a skill into bytes, so a hand-written skill and a
    distilled one are the same file format."""
    front = {"name": skill.name, "description": skill.description,
             "cites": list(skill.cites)}
    if skill.distilled:
        front["distilled"] = dict(skill.distilled)
    dumped = yaml.safe_dump(front, sort_keys=False, allow_unicode=True,
                            default_flow_style=False, width=88)
    return f"---\n{dumped}---\n\n{skill.body.strip()}\n"


def parse(text: str, path=None) -> Skill:
    """Frontmatter plus body. Raises rather than returning a half-read skill:
    an unparseable skill that reads as an empty one is a skill silently dropped
    from every brief."""
    where = f"{path}: " if path else ""
    if not text.startswith("---\n"):
        raise SchemaError(
            f"{where}a skill must open with a `---` frontmatter block carrying "
            "`name`, `description` and `cites`")
    end = text.find("\n---", 3)
    if end == -1:
        raise SchemaError(f"{where}frontmatter is never closed by a `---` line")
    head, body = text[4:end + 1], text[end + 4:]
    try:
        front = yaml.safe_load(head) or {}
    except yaml.YAMLError as exc:
        raise SchemaError(f"{where}frontmatter is not valid YAML: {exc}") from exc
    if not isinstance(front, dict):
        raise SchemaError(f"{where}frontmatter must be a mapping, got {type(front).__name__}")
    known = {"name", "description", "cites", "distilled"}
    unknown = set(front) - known
    if unknown:
        raise SchemaError(
            f"{where}frontmatter has unknown key(s) {sorted(unknown)}; "
            f"known: {sorted(known)}")
    cites = front.get("cites") or []
    if not isinstance(cites, list):
        raise SchemaError(f"{where}cites must be a list of entry ids, got {cites!r}")
    return Skill(
        name=str(front.get("name", "")),
        description=str(front.get("description", "")),
        cites=[str(c) for c in cites],
        distilled=dict(front.get("distilled") or {}),
        body=body.lstrip("\n"),
        path=pathlib.Path(path) if path else None)


def read_all(config) -> tuple[list[Skill], list[str]]:
    """Every skill under `skills.dir`, plus one problem line per file that could
    not be read.

    Returns both rather than raising, because every caller displays a count and
    a reader that stops at the first bad file makes "no skills" and "one broken
    skill" the same screen. A missing directory is zero skills and no problem --
    a domain that has not distilled anything yet is not misconfigured.
    """
    root = config.paths.root / config.skills.dir
    skills, problems = [], []
    if not root.is_dir():
        return skills, problems
    for sub in sorted(p for p in root.iterdir() if p.is_dir()):
        path = sub / SKILL_FILE
        if not path.exists():
            problems.append(
                f"skills: {sub.name}/ has no {SKILL_FILE}; a skill is a "
                f"directory containing one")
            continue
        try:
            skills.append(parse(path.read_text(), path))
        except SchemaError as exc:
            problems.append(f"skills: {exc}")
    return skills, problems


def index(config, skills) -> list[dict]:
    root = config.paths.root
    return [s.index_entry(root) for s in sorted(skills, key=lambda s: s.name)]


# -- validation -----------------------------------------------------------

def _terminal(config, entry) -> bool:
    return config.track_for(entry.id).machine.status(entry.status).terminal


def _cite_re(config) -> re.Pattern:
    """Inline citations, built from the track prefixes this domain actually
    declares. Guessing the prefix is how a regex over prose starts being
    approximately right."""
    prefixes = sorted((t.prefix for t in config.tracks.values()), key=len, reverse=True)
    alt = "|".join(re.escape(p) for p in prefixes)
    return re.compile(rf"\[({alt})(\d+)\]")


def _distilled_at(skill) -> str:
    """The timestamp every staleness check is measured from, or "".

    Coerced through `str` and checked for shape rather than trusted: a YAML
    `distilled: {at: }` parses to None, and `str(None)` is "None", which sorts
    above every ISO timestamp -- so the comparison below would be permanently
    false and the check would pass by never running.
    """
    raw = skill.distilled.get("at")
    return str(raw) if isinstance(raw, str) and raw[:4].isdigit() else ""


def _moved_since(entry, when: str) -> list[str]:
    """History events after `when` that invalidate a citation: the verdict was
    archived by a reopen, or the closure label was corrected. Both are recorded
    as events by `Entry.apply` and `Entry.relabel`, so this reads the record and
    not the prose.

    Callers must have established that `when` is a real timestamp -- an empty
    one here would silently return "nothing moved", which is the check passing
    because it could not run."""
    if not when:
        return []
    out = []
    for event in entry.history:
        # `>=`, not `>`. Both timestamps are ISO to the second, so an event in
        # the same second as the distillation is invisible to `>` -- and a skill
        # written and invalidated in one second is exactly what happens when a
        # human relabels right after a distil pass. The tie goes to flagging:
        # re-reading a skill that was in fact current costs one pass, while
        # missing one leaves confidently wrong knowledge in every brief.
        if event.kind in ("result-archived", "relabel") and event.at >= when:
            out.append(f"{event.kind} at {event.at} ({event.detail})")
    return out


def _quoted_constants(body: str, name: str) -> list[tuple[str, float]]:
    """Occurrences of `<name>` followed by a number, in the few shapes a
    sentence actually uses. Narrow on purpose -- see the module docstring."""
    # Only separators that ASSERT the number is the constant's value. `of` and
    # `at` were here and had to go: "a cost of 3 runs" and "measured cost at 4
    # threads" are ordinary prose, and a lint that fails the build on those is
    # the one that teaches everyone to skip it.
    pattern = re.compile(
        rf"`?\b{re.escape(name)}\b`?\s*(?:==|=|:|\bis\b)\s*"
        r"~?\s*(-?\d[\d,]*(?:\.\d+)?)")
    out = []
    for m in pattern.finditer(body):
        raw = m.group(1)
        try:
            out.append((raw, float(raw.replace(",", ""))))
        except ValueError:                                  # pragma: no cover
            continue
    return out


def _constant_problems(config, skill, measurements) -> list[str]:
    """Refuse a skill that quotes a derived constant the goal no longer
    computes to that value.

    This is H36 ported: a saving quoted as a sentence survived four re-rank
    sections after the number under it moved. Core computes derived constants
    from the typed goal (invariant 4), so a literal beside its label is
    checkable rather than merely suspicious.
    """
    if not measurements or not config.goal.derived:
        return []
    try:
        ns = config.goal.namespace(measurements)
    except AutoresearchError:
        return []
    problems = []
    for name in config.goal.derived:
        actual = ns.get(name)
        if not isinstance(actual, (int, float)) or isinstance(actual, bool):
            continue
        for raw, quoted in _quoted_constants(skill.body, name):
            decimals = len(raw.split(".")[1]) if "." in raw else 0
            if abs(round(float(actual), decimals) - quoted) > 0.5 * 10 ** -decimals:
                problems.append(
                    f"skills: {skill.name} quotes {name} = {raw}, but the goal "
                    f"computes {round(float(actual), decimals)} from the best "
                    f"measurement. A derived constant is computed, never stored "
                    f"(invariant 4) -- cite it or drop the literal.")
    return problems


def best_measurements(config):
    """Metrics of the best scored run, or None when nothing has been measured.

    One implementation, because the coordinator and the CLI both need it and
    "reuse the reader that owns the parse" is the convention this core inherited
    verbatim. None means the derived-constant lint cannot run -- which every
    caller reports as a gap, never as a pass.
    """
    from . import runs as runs_mod
    best = runs_mod.best_run(runs_mod.read_all(config.paths.runs), config.goal)
    return best[1].metrics if best else None


def check(config, skills, entries, measurements=None) -> list[str]:
    """Every problem with this set of skills, rather than the first.

    `entries` is the whole store; `measurements` is the best scored run's
    metrics, or None when nothing has been measured yet -- in which case the
    derived-constant lint does not run, which is a gap and not a pass.
    """
    by_id = {e.id: e for e in entries}
    names = {}
    cite_re = _cite_re(config)
    problems = []
    for skill in sorted(skills, key=lambda s: s.name):
        where = skill.path.parent.name if skill.path else skill.name
        if not NAME_RE.match(skill.name):
            problems.append(
                f"skills: {where}: name {skill.name!r} must be lowercase "
                f"letters, digits and hyphens, 1-64 characters")
        elif skill.path and skill.path.parent.name != skill.name:
            problems.append(
                f"skills: {where}/ holds a skill named {skill.name!r}; the "
                f"directory is the address, so they must agree")
        if skill.name in names:
            problems.append(f"skills: two skills are named {skill.name!r}")
        names[skill.name] = skill

        desc = skill.description.strip()
        if not desc:
            problems.append(f"skills: {where}: description is required -- it is "
                            "the whole routing layer")
        elif not desc.lower().startswith(DESCRIPTION_OPENER):
            problems.append(
                f"skills: {where}: description must start "
                f"{DESCRIPTION_OPENER!r} and state triggering conditions only. "
                "A description that summarises the workflow gets followed "
                "instead of the skill.")
        if len(skill.description) > MAX_DESCRIPTION:
            problems.append(
                f"skills: {where}: description is {len(skill.description)} "
                f"characters, over the {MAX_DESCRIPTION} ceiling")
        if skill.lines > config.skills.max_lines:
            problems.append(
                f"skills: {where}: body is {skill.lines} lines, over "
                f"skills.max_lines={config.skills.max_lines}")

        # Established before the citation loop: with no timestamp there is
        # nothing to measure "since" against, so every staleness check below
        # would return clean by never running. A hand-written skill is a
        # supported way to write one; a hand-written skill nothing can check is
        # not, and the scaffold's README says so.
        distilled_at = _distilled_at(skill)
        if not distilled_at:
            problems.append(
                f"skills: {where} has no `distilled.at` timestamp, so nothing "
                "can tell whether its evidence moved after it was written. Add "
                "`distilled: {at: <ISO timestamp>}` or re-distil it.")
        if not skill.cites:
            problems.append(
                f"skills: {where} cites nothing. A skill is distilled from "
                "closed work; prose with no entry behind it cannot be checked "
                "and must not be handed to a role as knowledge.")
        seen = set()
        for cid in skill.cites:
            if cid in seen:
                problems.append(f"skills: {where} cites {cid} twice")
            seen.add(cid)
            entry = by_id.get(cid)
            if entry is None:
                problems.append(
                    f"skills: {where} cites {cid}, which is not in the store")
                continue
            if not _terminal(config, entry):
                problems.append(
                    f"skills: {where} cites {cid}, which is {entry.status!r} "
                    "and not terminal. Unsettled work is not evidence.")
            for moved in _moved_since(entry, distilled_at):
                problems.append(
                    f"skills: {where} cites {cid}, which has {moved} since this "
                    "skill was distilled. Re-distil it or retire it -- a skill "
                    "outliving its evidence is the failure it exists to prevent.")

        inline = {f"{m.group(1)}{m.group(2)}" for m in cite_re.finditer(skill.body)}
        for cid in sorted(inline - seen):
            problems.append(
                f"skills: {where} cites {cid} in its body but not in "
                "`cites`, so nothing checks it")
        for cid in sorted(seen - inline):
            problems.append(
                f"skills: {where} lists {cid} in `cites` but never cites it in "
                "the body; drop it or say what it backs")

        problems += _constant_problems(config, skill, measurements)
    return problems


# -- writing --------------------------------------------------------------

def write(config, name, description, cites, body, *, iteration=None,
          entries=None, measurements=None) -> pathlib.Path:
    """Validate and write one skill, or raise.

    The coordinator calls this; the librarian role never touches the filesystem.
    `distilled` is constructed here rather than taken from the role for the
    reason stated on the field: a role does not certify its own output.
    """
    skill = Skill(name=str(name).strip(), description=str(description).strip(),
                  cites=[str(c).strip() for c in (cites or [])],
                  body=str(body or ""),
                  distilled={"at": _now(),
                             "iteration": iteration,
                             "core": core_version()})
    root = (config.paths.root / config.skills.dir).resolve()
    if not NAME_RE.match(skill.name):
        raise SchemaError(
            f"skill name {skill.name!r} must be lowercase letters, digits and "
            f"hyphens, 1-64 characters. The name is a directory, so a name that "
            f"is not one is a path traversal wearing a title.")
    target = (root / skill.name / SKILL_FILE).resolve()
    if root not in target.parents:                              # pragma: no cover
        raise SchemaError(f"skill {skill.name!r} would be written outside {root}")

    if entries is not None:
        skill.path = target
        problems = check(config, [skill], entries, measurements)
        if problems:
            raise SchemaError("; ".join(problems))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render(skill))
    return target


def retire(config, name: str) -> pathlib.Path:
    """Remove a skill. Returns its path; raises if it was not there, because a
    retire that silently succeeds against nothing is how a stale skill stays in
    every brief while the log says it went."""
    if not NAME_RE.match(str(name)):
        raise SchemaError(f"skill name {name!r} is not a valid skill name")
    path = config.paths.root / config.skills.dir / name / SKILL_FILE
    if not path.exists():
        raise SchemaError(f"no skill named {name!r} at {path}")
    path.unlink()
    parent = path.parent
    if not any(parent.iterdir()):
        parent.rmdir()
    return path


def stale_report(config, skills, entries) -> dict[str, list[str]]:
    """Per-skill staleness, for the librarian's brief: which skills are citing
    evidence that moved. Separate from `check` because the librarian is asked to
    *fix* these, and `check` is a gate rather than a worklist."""
    by_id = {e.id: e for e in entries}
    out = {}
    for skill in skills:
        reasons = []
        when = _distilled_at(skill)
        if not when:
            out[skill.name] = ["no `distilled.at`: nothing can tell whether its "
                               "evidence moved"]
            continue
        for cid in skill.cites:
            entry = by_id.get(cid)
            if entry is None:
                reasons.append(f"{cid} is no longer in the store")
                continue
            reasons += [f"{cid}: {m}" for m in _moved_since(entry, when)]
        if reasons:
            out[skill.name] = reasons
    return out


def undistilled(config, skills, entries) -> list[dict]:
    """Terminal entries no skill cites, newest first.

    This is the librarian's raw material and the honest definition of what is
    left to do: an entry closed and never distilled is knowledge that exists
    only as one record among hundreds.
    """
    cited = {cid for s in skills for cid in s.cites}
    out = []
    # Newest first, by the id's NUMBER: sorting the id as a string puts Q10
    # before Q2, and every consumer that truncates this list -- which is the
    # normal way to use it -- would then distil the oldest work forever.
    ordered = sorted(entries, key=lambda e: (e.prefix, -e.number))
    for entry in ordered:
        if entry.id in cited or not _terminal(config, entry):
            continue
        result = entry.result
        out.append({
            "id": entry.id,
            "title": entry.title,
            "status": entry.status,
            "closure_kind": getattr(result, "closure_kind", None),
            "summary": getattr(result, "summary", "") or "",
            "memo": getattr(result, "memo", "") or "",
            "mechanisms": list(entry.mechanisms),
        })
    return out
