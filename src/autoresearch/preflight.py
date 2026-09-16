"""Mechanical feasibility before a selected entry consumes dispatch resources."""
from __future__ import annotations

import dataclasses
import json
import shlex
import subprocess

from .errors import AutoresearchError, PolicyError


def blocked_reason(config, entry) -> str | None:
    """Return a denial reason, or None when policy and the optional hook allow.

    The domain, not prose matching in the coordinator, decides whether its
    measurement path can satisfy an entry. A broken gate must not spend a run.
    """
    try:
        measure = config.commands.get("measure")
        if measure is not None:
            config.policy.check_command(measure)
        if "preflight" not in config.commands:
            return None
        command = config.commands["preflight"]
        config.policy.check_command(command)
        argv = shlex.split(command)
        if not argv:
            raise AutoresearchError("preflight command is empty")
        result = subprocess.run(
            argv, cwd=config.paths.root,
            input=json.dumps({"entry": dataclasses.asdict(entry)}),
            capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            raise AutoresearchError(
                f"preflight exited {result.returncode}: {result.stderr.strip()}")
        verdict = json.loads(result.stdout)
        if (not isinstance(verdict, dict)
                or type(verdict.get("feasible")) is not bool
                or not isinstance(verdict.get("reason"), str)):
            raise AutoresearchError(
                "preflight must output one JSON object with feasible: boolean "
                "and reason: string")
        if not verdict["feasible"]:
            if not verdict["reason"].strip():
                raise AutoresearchError("infeasible preflight requires a nonempty reason")
            return verdict["reason"]
        return None
    except PolicyError as exc:
        return str(exc)
    except Exception as exc:
        return f"preflight failed: {str(exc) or repr(exc)}"
