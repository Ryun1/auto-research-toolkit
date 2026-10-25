"""A holdout the search never sees (#38).

`confirm_independently` accepted any second `ok` row from another session or
entry, and that row was scored by the very metric the search optimised: it
guarded against one lucky execution, not against overfitting to the
evaluator. AIRA_2 keeps final selection off the search signal (fixed splits,
labels hidden from the agent); ShinkaEvolve splits public from private
metrics; Self-Harness accepts an edit only after held-out regression tests.

A domain declares `[commands] holdout`: a command that, given the search row
to judge in `$AR_HOLDOUT_ROW` (JSON), emits one ar-run-1 row with the goal's
metrics on data the search never saw, plus any private ones. Core:

* consults it only when a win is on the table -- the stop decision, the
  independent confirmation, and an explicit `ar holdout check` (and, with
  `on_promotion`, whenever the frontier has a best) -- never from a read verb;
* keeps the rows and verdicts in coordinator state (`.ar/holdout/`), which no
  slot receives, and strips the holdout files out of every slot;
* shows only pass/fail and the consultation count -- no brief, board line or
  memo carries a holdout value;
* meters consultations per epoch against `max_consults`, because every peek
  leaks a little of the holdout. One config is consulted once per epoch.

A pass is `goal.is_met` on the holdout row, or `[holdout] pass_when`, an
expression over the holdout row's metrics, `objective`, `target` and
`search` (the judged row's objective).
"""
from __future__ import annotations

import json
import pathlib
import subprocess
from dataclasses import dataclass

from . import expr
from .errors import AutoresearchError, ConfigError

KEYS = {"files", "pass_when", "max_consults", "on_promotion"}


@dataclass(frozen=True)
class Settings:
    files: tuple[str, ...] = ()
    pass_when: str | None = None
    max_consults: int | None = None
    on_promotion: bool = False

    @classmethod
    def from_dict(cls, spec) -> Settings:
        unknown = set(spec) - KEYS
        if unknown:
            raise ConfigError(f"[holdout] has unknown key(s) {sorted(unknown)}; "
                              f"known: {sorted(KEYS)}")
        names = spec.get("files", [])
        if not isinstance(names, list) or not all(
                isinstance(n, str) and n and not pathlib.PurePosixPath(n).is_absolute()
                and ".." not in pathlib.PurePosixPath(n).parts for n in names):
            raise ConfigError("[holdout] files must be paths relative to the "
                              "domain root")
        pass_when = spec.get("pass_when")
        if pass_when is not None:
            if not isinstance(pass_when, str) or not pass_when.strip():
                raise ConfigError("[holdout] pass_when must be an expression")
            expr.names(pass_when)                # a syntax error fails at load
        cap = spec.get("max_consults")
        if cap is not None and (not isinstance(cap, int) or cap < 1):
            raise ConfigError("[holdout] max_consults must be a positive integer")
        on_promotion = spec.get("on_promotion", False)
        if not isinstance(on_promotion, bool):
            raise ConfigError("[holdout] on_promotion must be true or false")
        return cls(tuple(names), pass_when, cap, on_promotion)


def declared(config) -> bool:
    return "holdout" in config.commands


def files(config) -> list[str]:
    """What a slot must never receive: the holdout command's own script and
    every `[holdout] files` path."""
    if not declared(config):
        return []
    root = config.paths.root
    from .evaluator import _inside
    script = [t for t in config.commands["holdout"].split()
              if _inside(root, t) and (root / t).is_file()]
    return list(dict.fromkeys(script + list(config.holdout.files)))


def _ledger(config) -> pathlib.Path:
    return config.paths.holdout / "consults.jsonl"


def _records(config) -> list[dict]:
    path = _ledger(config)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _epoch(config) -> list[str]:
    from .epochs import current_key
    return list(current_key(config))


def consults(config) -> int:
    """Consultations in the current epoch."""
    epoch = _epoch(config)
    return sum(1 for r in _records(config) if r["epoch"] == epoch)


@dataclass
class Verdict:
    passed: bool | None          # None: no verdict (not consulted, capped, failed to run)
    reason: str = ""


def cached(config, row) -> Verdict | None:
    """This epoch's verdict on `row`'s config, without consulting."""
    from .noise import config_digest
    epoch, digest = _epoch(config), config_digest(row)
    for record in reversed(_records(config)):
        if record["epoch"] == epoch and record["digest"] == digest:
            return Verdict(record["passed"],
                           "the holdout confirms it" if record["passed"]
                           else "the holdout does not confirm it")
    return None


def consult(config, row, target, session) -> Verdict:
    """The holdout's verdict on `row`'s config: from this epoch's record if
    one exists, else one consultation -- refused once `max_consults` is
    spent. Raises on a holdout command that fails or emits no row."""
    found = cached(config, row)
    if found is not None:
        return found
    cap = config.holdout.max_consults
    if cap is not None and consults(config) >= cap:
        raise AutoresearchError(
            f"[holdout] max_consults ({cap}) reached this epoch; every peek "
            "leaks a little of the holdout, so a person decides whether to "
            "raise it")
    command = config.commands["holdout"]
    config.policy.check_command(command)
    from .evaluator import _inside
    argv = [str(config.paths.root / t) if _inside(config.paths.root, t)
            and (config.paths.root / t).exists() else t for t in command.split()]
    proc = subprocess.run(argv, cwd=config.paths.root, capture_output=True,
                          text=True, env={**_env(), "AR_SESSION": session,
                                          "AR_HOLDOUT_ROW": json.dumps(row.to_dict())})
    if proc.returncode != 0:
        raise AutoresearchError(f"holdout failed ({proc.returncode}): "
                                f"{proc.stderr.strip()[:300]}")
    from .runs import RunRecord
    try:
        result = RunRecord.from_dict(json.loads(proc.stdout.strip().splitlines()[-1]))
    except (ValueError, IndexError, TypeError) as exc:
        raise AutoresearchError(f"holdout did not emit a run row: {exc}") from exc
    result.config = {**(result.config or {}), "holdout": True}
    passed = _passes(config, result, row, target)
    from .noise import config_digest
    config.paths.holdout.mkdir(parents=True, exist_ok=True)
    with _ledger(config).open("a") as fh:
        fh.write(json.dumps({"epoch": _epoch(config), "digest": config_digest(row),
                             "judged": row.id, "passed": passed,
                             "session": session, "row": result.to_dict()},
                            sort_keys=True) + "\n")
    return Verdict(passed, "the holdout confirms it" if passed
                   else "the holdout does not confirm it")


def _passes(config, result, row, target) -> bool:
    goal = config.goal
    if result.status != "ok" or target is None:
        return False
    try:
        if config.holdout.pass_when is None:
            return bool(goal.is_met(result.metrics, target))
        ns = goal.namespace(result.metrics)
        ns.update(objective=goal.objective_value(result.metrics), target=target,
                  search=goal.objective_value(row.metrics))
        return bool(expr.evaluate(config.holdout.pass_when, ns))
    except Exception:             # an unreadable holdout row passes nothing
        return False


def _env():
    import os
    return dict(os.environ)


def stop_reason(config, best, target, *, session=None) -> tuple[str | None, bool]:
    """`(reason, passed)` for the stop decision on `best`.

    `reason` is None when there is nothing to hold (no holdout declared, or it
    passed); otherwise why a met target is not yet a win. With a `session`,
    the holdout is consulted if this config has no verdict yet (the loop);
    without one it is only read (`ar budget`, which never peeks).
    """
    if not declared(config) or best is None:
        return None, False
    row = best[1]
    try:
        verdict = (consult(config, row, target, session) if session
                   else cached(config, row))
    except AutoresearchError as exc:
        return f"the holdout gives no verdict: {exc}", False
    if verdict is None:
        cap = config.holdout.max_consults
        if cap is not None and consults(config) >= cap:
            return (f"the holdout cannot be consulted: [holdout] max_consults "
                    f"({cap}) is spent this epoch, and a person decides "
                    "whether to raise it"), False
        return ("the holdout has not yet consulted on this config; the loop "
                "or `ar holdout check` consults it"), False
    if verdict.passed:
        return None, True
    return verdict.reason, False


def summary(config, best) -> dict:
    """What the search may see: the count, the cap and pass/fail."""
    verdict = cached(config, best[1]) if best else None
    return {"consults": consults(config), "max": config.holdout.max_consults,
            "last": None if verdict is None else ("pass" if verdict.passed else "fail")}


def strip(config, slot_path, worktree: bool) -> None:
    """Remove the holdout from a slot. In a worktree the paths are marked
    skip-worktree first, so a worker's `commit -a` cannot record the deletion
    and delete the holdout from the primary on merge."""
    names = [n for n in files(config) if (pathlib.Path(slot_path) / n).exists()]
    if not names:
        return
    if worktree:
        subprocess.run(["git", "update-index", "--skip-worktree", "--", *names],
                       cwd=slot_path, capture_output=True)
    for name in names:
        (pathlib.Path(slot_path) / name).unlink()
