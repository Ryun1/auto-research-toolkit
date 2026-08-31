"""Pulling improvements: how a project moves its core forward.

The toolkit is installed as a pinned dependency and upgraded deliberately --
that is a decision worth making on day one, because closure semantics and the
ranking formula live here, and a record written under one core should be
readable under the next. This module makes "deliberately" cheap enough to
actually happen, for a person or for an agent told to keep the project current:

* `plan_update` compares what is installed against upstream and, when there is
  something to take, shows what changed between the two commits.
* `apply_update` installs the resolved head SHA, regenerates the views and
  validates. Two properties make that safe rather than optimistic:
  - the views are re-rendered *before* validating, because a new core may
    render differently and a stale view must not read as a broken upgrade;
  - a rollback fires only when validation found problems that were **not
    there before the upgrade**. A project with an incomplete defect entry
    pre-dating the update must still be able to take a core fix.
* Every upgrade writes a memo into `inbox/` -- from-version, to-version, the
  changelog -- because "every code path that observes something must write it
  to the record", and an environment change is something the record should
  know about.

The install source comes from pip's own `direct_url.json`; a domain can point
`[upstream]` at a fork or mirror instead. Publishing upstream stays human-only
(the defects half of this, in `defects.py`); pulling is safe to automate
precisely because it validates and can undo itself.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from importlib import metadata

from .errors import AutoresearchError

DEFAULT_UPSTREAM = "https://github.com/Ryun1/auto-research-toolkit"
DEFAULT_REF = "main"
#: network operations get a timeout, so an offline machine fails loudly in a
#: minute rather than hanging an agent's session
TIMEOUT_SECONDS = 60


# -- what is installed ------------------------------------------------------

@dataclass(frozen=True)
class Install:
    version: str
    commit: str | None = None
    url: str | None = None
    #: the branch or tag pip was pointed at, when the install recorded one
    ref: str | None = None
    editable: bool = False


def installed() -> Install:
    """Read the install from pip's own metadata. There is exactly one source
    of truth for where this core came from, and pip already wrote it."""
    try:
        dist = metadata.distribution("autoresearch")
    except metadata.PackageNotFoundError:
        raise AutoresearchError(
            "autoresearch is not installed in this environment; "
            "`ar` is running from somewhere unexpected") from None
    commit = url = ref = None
    editable = False
    raw = dist.read_text("direct_url.json")
    if raw:
        data = json.loads(raw)
        url = data.get("url")
        vcs = data.get("vcs_info") or {}
        commit = vcs.get("commit_id")
        ref = vcs.get("requested_revision")
        editable = bool((data.get("dir_info") or {}).get("editable"))
    return Install(version=dist.version or "unknown", commit=commit, url=url,
                   ref=ref, editable=editable)


def upstream_url(config, ref: str | None = None) -> tuple[str, str]:
    """Where to pull from: the domain's `[upstream]` table if it declares one,
    else where pip says this core was installed from. The override exists so a
    fork or a mirror is a one-line domain decision."""
    up = getattr(config, "upstream", None)
    if up is not None and up.url:
        return up.url, ref or up.ref or DEFAULT_REF
    inst = installed()
    if (inst.url and not inst.editable
            and inst.url.startswith(("http://", "https://", "git+", "ssh://", "git@"))):
        return inst.url.removeprefix("git+"), ref or inst.ref or DEFAULT_REF
    if inst.editable:
        source = inst.url or "a local checkout"
        raise AutoresearchError(
            f"autoresearch is installed editable from {source}; there is "
            "nothing to pull here -- update that checkout and reinstall, or "
            "declare an [upstream] url in domain.toml to pull from a fixed "
            "remote")
    raise AutoresearchError(
        "cannot tell where this core came from: no [upstream] table in "
        "domain.toml, and the install recorded no source url. Declare one:\n\n"
        "  [upstream]\n"
        f"  url = \"{DEFAULT_UPSTREAM}\"\n"
        f"  ref  = \"{DEFAULT_REF}\"")


# -- talking to upstream ------------------------------------------------------

def _git(args, timeout: float = TIMEOUT_SECONDS) -> str:
    try:
        proc = subprocess.run(["git", *args], capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise AutoresearchError(
            f"git {args[0]} timed out after {TIMEOUT_SECONDS}s; "
            "is the machine offline?") from exc
    if proc.returncode != 0:
        raise AutoresearchError(
            f"git {args[0]} failed ({proc.returncode}): "
            f"{proc.stderr.strip()[:300]}")
    return proc.stdout


def remote_head(url: str, ref: str = DEFAULT_REF) -> str | None:
    """The commit upstream's `ref` points at, following a tag if the ref names
    one. None means the remote has no such ref -- which is a configuration
    error the caller reports, not an empty result to guess past."""
    out = _git(["ls-remote", url, f"refs/heads/{ref}", f"refs/tags/{ref}",
                f"refs/tags/{ref}^{{}}"])
    peeled = None
    for line in out.splitlines():
        sha, _, name = line.partition("\t")
        if name == f"refs/heads/{ref}":
            return sha
        if name == f"refs/tags/{ref}^{{}}":
            peeled = sha
    if peeled:
        return peeled
    for line in out.splitlines():
        sha, _, name = line.partition("\t")
        if name == f"refs/tags/{ref}":
            return sha
    return None


def remote_tags(url: str) -> dict[str, str]:
    """Every tag upstream, peeled where annotated, so a version compare is
    comparing commits rather than objects."""
    out = _git(["ls-remote", "--tags", url])
    tags: dict[str, str] = {}
    for line in out.splitlines():
        sha, _, name = line.partition("\t")
        name = name.removeprefix("refs/tags/").removeprefix("^{}")
        if name:
            tags[name] = sha
    return tags


def _pull_status(url: str, ref: str, local: str,
                 limit: int = 30) -> tuple[str, bool, str | None]:
    """Compare a local commit against a remote ref, with one clone.

    Returns `(head, behind, changelog)`. 'Behind' requires the local commit to
    be an *ancestor* of the remote one: a checkout that is ahead of (or has
    diverged from) origin has nothing to pull, and reporting 'update
    available' at it would be exactly the false alarm that trains a reader to
    skip the real ones. A clone is needed either way, so the ancestry check and
    the changelog come out of the same one.
    """
    with tempfile.TemporaryDirectory(prefix="ar-upstream-") as tmp:
        clone = pathlib.Path(tmp) / "upstream"
        subprocess.run(
            ["git", "clone", "--quiet", "--no-checkout", url, str(clone)],
            capture_output=True, text=True, timeout=TIMEOUT_SECONDS, check=True)
        head = None
        for candidate in (f"refs/remotes/origin/{ref}",
                          f"refs/tags/{ref}^{{}}",    # annotated: peel to commit
                          f"refs/tags/{ref}"):       # lightweight: the tag is it
            proc = subprocess.run(
                ["git", "-C", str(clone), "rev-parse", "--verify", "--quiet",
                 candidate],
                capture_output=True, text=True, timeout=TIMEOUT_SECONDS)
            if proc.returncode == 0:
                head = proc.stdout.strip().splitlines()[0]
                break
        if head is None:
            raise AutoresearchError(
                f"upstream {url} has no branch or tag named {ref!r}; "
                "check the [upstream] table or pass --ref")
        if local == head:
            return head, False, None
        ancestor = subprocess.run(
            ["git", "-C", str(clone), "merge-base", "--is-ancestor",
             local, head], capture_output=True, text=True,
            timeout=TIMEOUT_SECONDS)
        if ancestor.returncode != 0:
            return head, False, None
        log = subprocess.run(
            ["git", "-C", str(clone), "log", "--oneline", "--no-decorate",
             f"{local}..{head}"],
            capture_output=True, text=True, timeout=TIMEOUT_SECONDS, check=True)
    lines = [line for line in log.stdout.splitlines() if line.strip()]
    shown = lines[:limit]
    tail = f"\n  ... and {len(lines) - len(shown)} more" if len(lines) > limit else ""
    body = "\n".join(f"  {line}" for line in shown) + tail
    return head, True, body or None


def changelog(url: str, old: str, new: str, limit: int = 30) -> str | None:
    """What changed between two commits, best effort.

    Needs a clone, so offline it degrades to None: the check that found the
    update must not fail because the summary of it could not be fetched."""
    try:
        with tempfile.TemporaryDirectory(prefix="ar-upstream-") as tmp:
            clone = pathlib.Path(tmp) / "upstream"
            subprocess.run(
                ["git", "clone", "--quiet", "--no-checkout", url, str(clone)],
                capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
                check=True)
            log = subprocess.run(
                ["git", "-C", str(clone), "log", "--oneline", "--no-decorate",
                 f"{old}..{new}"],
                capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
                check=True)
        lines = [line for line in log.stdout.splitlines() if line.strip()]
        if not lines:
            return None
        shown = lines[:limit]
        tail = f"\n  ... and {len(lines) - len(shown)} more" if len(lines) > limit else ""
        return "\n".join(f"  {line}" for line in shown) + tail
    except (subprocess.SubprocessError, AutoresearchError, OSError):
        return None


def _vkey(version: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", version) or [0])


def latest_tag(tags: dict[str, str]) -> str | None:
    versioned = [t for t in tags if re.match(r"^v?\d+(\.\d+)*$", t)]
    return max(versioned, key=_vkey) if versioned else None


# -- the plan ----------------------------------------------------------------

@dataclass
class UpdatePlan:
    install: Install
    url: str
    ref: str
    head: str
    behind: bool
    changelog: str | None = None
    #: for an editable checkout, the commit the checkout itself is on --
    #: distinct from install.commit, which only a pip-recorded git install has
    local: str | None = None

    def summary(self) -> str:
        inst = self.install
        if inst.editable:
            state = f" (editable from {inst.url})"
        elif inst.commit:
            state = f" @ {inst.commit[:12]}"
        else:
            state = " (not git-pinned)"
        local = f" @ {self.local[:12]}" if self.local else ""
        return (f"installed   core {inst.version}{state}{local}\n"
                f"upstream    {self.url}  {self.ref} @ {self.head[:12]}")


def _editable_head(inst: Install) -> str | None:
    """The HEAD of an editable checkout, so a source install can still be
    compared against upstream. None when the checkout cannot be read."""
    if not inst.url or not inst.url.startswith("file://"):
        return None
    path = pathlib.Path(inst.url.removeprefix("file://"))
    if not path.is_dir():
        return None
    try:
        # .strip(): rev-parse emits a trailing newline, and an unstripped SHA
        # compares unequal to the ls-remote one -- behind-always, forever.
        return _git(["-C", str(path), "rev-parse", "HEAD"]).strip() or None
    except AutoresearchError:
        return None


def plan_update(config, ref: str | None = None) -> UpdatePlan:
    """Compare the install against upstream. Raises when upstream cannot be
    reached or the declared ref does not exist there -- a plan that silently
    compared nothing would read as 'up to date'."""
    inst = installed()
    url, ref = upstream_url(config, ref)
    commit = inst.commit or (_editable_head(inst) if inst.editable else None)
    if inst.editable and commit:
        # A checkout may be ahead of or diverged from origin, so the plain
        # commit compare is not enough; the ancestry check comes free with the
        # clone this path does anyway -- which also resolves the ref, so no
        # ls-remote is spent on it here.
        head, behind, log = _pull_status(url, ref, commit)
    else:
        head = remote_head(url, ref)
        if head is None:
            raise AutoresearchError(
                f"upstream {url} has no branch or tag named {ref!r}; "
                "check the [upstream] table or pass --ref")
        if commit:
            behind = head != commit
            log = changelog(url, commit, head) if behind else None
        else:
            # Not a git install (a version pin from a package index, say):
            # compare against the newest tag, the only honest comparable.
            newest = latest_tag(remote_tags(url))
            behind = newest is not None and _vkey(newest) > _vkey(inst.version)
            log = None
    return UpdatePlan(install=inst, url=url, ref=ref, head=head,
                      behind=behind, changelog=log,
                      local=commit if inst.editable else None)


# -- applying it --------------------------------------------------------------

def pip_command(url: str, commit: str) -> list[str]:
    """Pinned to the resolved SHA, never to a moving ref name: what lands must
    be what was checked, or 'deliberately' means nothing."""
    prefix = "git+file://" if url.startswith("/") else "git+"
    return [sys.executable, "-m", "pip", "install", "--force-reinstall",
            f"autoresearch @ {prefix}{url}@{commit}"]


def _pip_install(cmd: list[str]) -> str:
    """Run pip, then ask a fresh interpreter what landed -- this process still
    has the old modules loaded, so asking it would report the past."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=TIMEOUT_SECONDS * 5)
        probe = subprocess.run(
            [sys.executable, "-c",
             "from autoresearch.skills import core_version; print(core_version())"],
            capture_output=True, text=True, timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise AutoresearchError(
            f"pip did not finish within {TIMEOUT_SECONDS * 5}s; is the "
            "machine offline? The install may be partial") from exc
    if proc.returncode != 0:
        raise AutoresearchError(
            f"pip install failed ({proc.returncode}):\n"
            f"{proc.stderr.strip()[-800:]}")
    if probe.returncode != 0:
        raise AutoresearchError(
            f"the upgraded core does not import ({probe.returncode}):\n"
            f"{probe.stderr.strip()[-500:]}")
    return probe.stdout.strip()


def _verify(config) -> list[str]:
    """Re-render, then validate, in a fresh interpreter.

    Returns one string per problem line; render failures read as problems too.
    This never raises for a failing domain -- a failing domain is exactly the
    evidence the rollback decision needs."""
    def run(*args: str) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                [sys.executable, "-m", "autoresearch.cli", *args,
                 "--domain", str(config.paths.root)],
                capture_output=True, text=True, timeout=TIMEOUT_SECONDS * 5)
        except subprocess.TimeoutExpired:
            # A wedged verify must not escape as a traceback: the caller needs
            # a problem list to roll back on, not an exception to die on with
            # the new core already installed.
            return subprocess.CompletedProcess(
                args, returncode=124, stdout="",
                stderr=f"timed out after {TIMEOUT_SECONDS * 5}s")

    problems: list[str] = []
    render = run("render")
    if render.returncode != 0:
        problems.append(f"render failed ({render.returncode}): "
                        f"{render.stderr.strip()[-400:]}")
    validate = run("validate")
    if validate.returncode != 0:
        body = (validate.stdout + "\n" + validate.stderr).strip()
        problems += [line for line in body.splitlines() if line.strip()] or \
            [f"validate failed ({validate.returncode}) with no output"]
    return problems


@dataclass
class UpdateResult:
    ok: bool
    rolled_back: bool
    detail: list[str]


def apply_update(config, plan: UpdatePlan, *, installer=None, verifier=None) -> UpdateResult:
    """Install the plan's head, re-render, validate, and only roll back when
    the upgrade made validation worse.

    `installer` and `verifier` are injectable so the sequence is testable
    without pip or a network: the installer takes the pip command and returns
    the freshly installed core version; the verifier takes the config and
    returns the problem list. Defaults do the real thing."""
    if plan.install.commit == plan.head:
        return UpdateResult(ok=True, rolled_back=False,
                            detail=[f"already at {plan.head[:12]}"])
    if plan.install.editable:
        raise AutoresearchError(
            "this core is installed editable from "
            f"{plan.install.url or 'a local checkout'}; pip has nothing to "
            "replace. Pull that checkout and reinstall -- or declare a "
            "non-editable pin when the project is not being developed "
            "alongside the core.")

    installer = installer or _pip_install
    verifier = verifier or _verify
    detail: list[str] = []

    # A human-gated domain refuses before anything runs -- including before
    # the pre-check below, which regenerates the views.
    config.policy.check_command(" ".join(pip_command(plan.url, plan.head)))

    # The pre-check is what makes "only new problems roll back" possible.
    before = set(verifier(config))
    if before:
        detail.append(f"{len(before)} validation problem(s) existed before the "
                      "upgrade; they do not block it and are not caused by it")

    # The installer reports what actually landed, from a fresh interpreter --
    # this process still has the old modules loaded and would report the past.
    new_version, failure = None, None
    try:
        new_version = installer(pip_command(plan.url, plan.head))
        detail.append(f"installed core {new_version} @ {plan.head[:12]} "
                      f"from {plan.url} ({plan.ref})")
        after = verifier(config)
    except AutoresearchError as exc:
        # An upgrade that cannot even complete is the strongest possible new
        # problem; it goes through the same rollback decision as any other.
        failure = exc
        after = []
    new_problems = [p for p in after if p not in before]
    if failure and not new_problems:
        new_problems = [f"the upgrade could not be completed: {failure}"]
    if new_problems:
        detail += new_problems
        rolled = _rollback(config, plan, installer, verifier, detail,
                           len(new_problems))
        return UpdateResult(ok=False, rolled_back=rolled, detail=detail)

    detail.append("re-rendered and validated clean under the new core")
    memo = _write_memo(config, plan, new_version)
    detail.append(f"wrote {memo.relative_to(config.paths.root)}")
    return UpdateResult(ok=True, rolled_back=False, detail=detail)


def _rollback(config, plan: UpdatePlan, installer, verifier, detail: list[str],
              new_problem_count: int) -> bool:
    """Put the previous core back and re-render with it. Returns whether the
    project was actually restored -- a rollback that silently failed would be
    a failure that reads as a result, the shape this harness exists to kill."""
    if not plan.install.commit:
        detail.append(
            "the upgrade introduced new problem(s) and the previous install "
            "recorded no commit to roll back to; reinstall the version you had")
        return False
    try:
        installer(pip_command(plan.url, plan.install.commit))
        verifier(config)   # re-render with the core that owns the views
    except AutoresearchError as exc:
        detail.append(
            f"ROLLBACK FAILED: the project may be left on the upgraded core. "
            f"Reinstall it by hand: pip install --force-reinstall "
            f"autoresearch @ git+{plan.url}@{plan.install.commit} ({exc})")
        return False
    detail.append(
        f"rolled back to core {plan.install.version} "
        f"@ {plan.install.commit[:12]}: the upgrade introduced "
        f"{new_problem_count} new problem(s). Fix upstream or here before "
        "taking it; the record is unchanged.")
    return True


def _write_memo(config, plan: UpdatePlan, new_version: str) -> pathlib.Path:
    """The record learns that the core moved. An environment change observed
    and not written is the H39 shape: reproducible by nobody, later."""
    date = dt.datetime.now(dt.UTC).date().isoformat()
    path = config.paths.memos / f"core-update-{date}-{plan.head[:12]}.md"
    old = plan.install
    lines = [
        f"# Core updated: {old.version}"
        + (f" ({old.commit[:12]})" if old.commit else "") +
        f" -> {new_version} ({plan.head[:12]})",
        "",
        f"- date: {_now()}",
        f"- source: {plan.url} ({plan.ref})",
        "",
        "Records written before this update were written by the previous core",
        "and stand as they are; validation accepts them as they were found.",
        "",
    ]
    if plan.changelog:
        lines += ["## What changed", "", plan.changelog, ""]
    config.paths.memos.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines))
    return path


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
