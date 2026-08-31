"""The seam between the loop and the model.

The loop's control flow -- what runs, in what order, under which budget, and
when to stop -- is ordinary code, tested without a model. Judgement is delegated
through this interface. Two reasons that split is worth the indirection:

1. **The control flow is the part that must not be improvised.** Budgets,
   claim serialisation, closure evidence and the stop decision are exactly the
   things the source harness kept discovering it had got wrong; none of them
   should depend on a model choosing to follow an instruction.
2. **The whole loop is testable offline.** `ScriptedBrain` makes
   `ar loop --iterations 5` a unit test rather than a bill.

Roles are the auditician shape: one prompt file per role, a coordinator that
dispatches several at once, and a quality-control role whose only job is to
prove no phase was silently skipped.
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from typing import Protocol

from ..errors import AutoresearchError

AGENTS_DIR = pathlib.Path(__file__).resolve().parent.parent / "agents"


class Role:
    GENERATOR = "generator"
    JUDGE = "judge"
    WORKER = "worker"
    CURATOR = "curator"
    LIBRARIAN = "librarian"
    QC = "qc"
    SCOUT = "scout"

    ALL = (GENERATOR, JUDGE, WORKER, CURATOR, LIBRARIAN, QC, SCOUT)


@dataclass
class Reply:
    role: str
    data: object
    raw: str = ""
    cost_usd: float = 0.0
    #: which backend answered, as the config named it. A curate answered by
    #: one agent and a curate answered by another are different facts on the
    #: record (the H98 principle, applied to backends), so this travels with
    #: every reply instead of living in one phase's detail.
    backend: str = ""


class CostLedger:
    """One shared money ceiling across every backend.

    A ceiling enforced inside each brain separately is N ceilings of the full
    size, which is no ceiling at all once more than one backend can spend.
    Every brain built from a domain's `[brain]` table reports what it spent
    here, and reads what is left before it starts the next ask. Thread-safe:
    fan-out runs several briefs of one role concurrently.
    """

    def __init__(self, ceiling_usd: float | None):
        self.ceiling = ceiling_usd
        self.spent = 0.0
        self._lock = threading.Lock()

    def remaining(self) -> float | None:
        with self._lock:
            if self.ceiling is None:
                return None
            return max(0.0, self.ceiling - self.spent)

    def spend(self, cost: float) -> None:
        with self._lock:
            self.spent += float(cost or 0.0)


class Brain(Protocol):
    def ask(self, role: str, brief: str, *, workspace=None,
            max_turns: int | None = None) -> Reply: ...


def role_prompt(role: str) -> str:
    path = AGENTS_DIR / f"{role}.md"
    if not path.exists():
        raise AutoresearchError(
            f"no prompt for role {role!r} at {path}; roles are {Role.ALL}")
    return path.read_text()


def extract_json(text: str):
    """Pull the last JSON value out of a model reply.

    Models fence JSON, prefix it with prose, or both. Refusing to parse is
    better than guessing -- a phase that silently returns nothing looks exactly
    like a phase that found nothing, and that ambiguity is H98's whole subject.
    """
    fenced = re.findall(r"```(?:json)?\s*(.+?)```", text, re.S)
    for candidate in reversed(fenced):
        try:
            return json.loads(candidate.strip())
        except ValueError:
            continue
    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except ValueError:
                continue
    raise AutoresearchError(
        f"no JSON found in a {len(text)}-character reply. The role contract "
        f"requires a JSON document; got: {text[:200]!r}")


class ScriptedBrain:
    """A brain driven by callables, for tests and for offline runs.

    Each role maps to `fn(brief) -> data`. A role with no handler returns an
    empty result *and records that it did* -- so a scripted run cannot silently
    look like a real one.
    """

    def __init__(self, handlers: dict, default=None, backend: str = "scripted"):
        self.handlers = handlers
        self.default = default
        self.backend = backend
        self.calls: list[tuple[str, str]] = []

    def ask(self, role: str, brief: str, *, workspace=None, max_turns=None) -> Reply:
        self.calls.append((role, brief))
        handler = self.handlers.get(role, self.default)
        if handler is None:
            return Reply(role=role, data=None, raw="(no handler)", backend=self.backend)
        data = handler(brief) if callable(handler) else handler
        return Reply(role=role, data=data, raw=json.dumps(data, default=str),
                     backend=self.backend)


class SDKBrain:
    """The real brain: one Claude Agent SDK query per role.

    Money is metered twice on purpose -- `max_budget_usd` caps the SDK itself,
    and the caller's domain budget records what was spent -- because a ceiling
    enforced in only one place is a ceiling that stops existing the moment
    someone calls the other path.
    """

    def __init__(self, config, model: str | None = None,
                 max_budget_usd: float | None = None, allowed_tools=None,
                 ledger: CostLedger | None = None):
        self.config = config
        self.model = model
        self.max_budget_usd = (max_budget_usd if max_budget_usd is not None
                               else config.policy.spend_ceiling)
        self.allowed_tools = allowed_tools or [
            "Read", "Grep", "Glob", "Bash", "Write", "Edit"]
        self.ledger = ledger
        self.spent_usd = 0.0
        self.backend = "claude-sdk"

    def ask(self, role: str, brief: str, *, workspace=None, max_turns=None) -> Reply:
        import asyncio
        return asyncio.run(self._ask(role, brief, workspace, max_turns))

    def _ceiling(self) -> float | None:
        """The budget this one ask may hand the SDK.

        With a shared ledger it is what is left of the campaign's money, so N
        backends share one ceiling instead of each holding a full copy of it.
        Without a ledger the brain still caps itself against its own spend --
        metered twice on purpose.
        """
        if self.ledger is not None:
            return self.ledger.remaining()
        if self.max_budget_usd is None:
            return None
        return max(0.0, self.max_budget_usd - self.spent_usd)

    async def _ask(self, role, brief, workspace, max_turns) -> Reply:
        from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

        if role in (Role.WORKER, Role.CURATOR):
            tools = self.allowed_tools
        elif role == Role.SCOUT:
            # A scout reads the world, not the record: research needs the web,
            # but its findings come back as JSON for the coordinator to file,
            # never as writes.
            tools = ["Read", "Grep", "Glob", "WebSearch", "WebFetch"]
        else:
            tools = ["Read", "Grep", "Glob"]

        # With a shared ledger the per-ask ceiling is what is left of the
        # campaign's money, so N backends share one ceiling instead of each
        # holding a full copy of it.
        ceiling = self._ceiling()
        options = ClaudeAgentOptions(
            system_prompt=role_prompt(role),
            cwd=str(workspace or self.config.paths.root),
            # Only the worker and the curator write. The librarian is read-only
            # on purpose: it returns a skill body and the coordinator writes it,
            # so a role never certifies its own output (see skills.write).
            allowed_tools=tools,
            permission_mode="acceptEdits",
            max_turns=max_turns,
            model=self.model,
            max_budget_usd=ceiling,
        )
        chunks, cost = [], 0.0
        async for message in query(prompt=brief, options=options):
            if isinstance(message, ResultMessage):
                cost = getattr(message, "total_cost_usd", 0.0) or 0.0
                continue
            for block in getattr(message, "content", []) or []:
                text = getattr(block, "text", None)
                if text:
                    chunks.append(text)
        raw = "\n".join(chunks)
        self.spent_usd += cost
        if self.ledger is not None:
            self.ledger.spend(cost)
        return Reply(role=role, data=extract_json(raw), raw=raw, cost_usd=cost,
                     backend=self.backend)


class ProcessBrain:
    """A brain that is any command, so no agent is a special case.

    The contract, the same shape as a domain's measure command:

    - the command is a list of strings carrying the placeholders `{role}`,
      `{prompt_file}`, `{workspace}` and `{cost_file}` (unknown placeholders
      are refused at config load, not here);
    - the brief arrives on **stdin** -- briefs are large, never argv;
    - the role's prompt is written to `{prompt_file}` by the harness, because
      the prompts are core-owned (`agents/*.md`) and the backend decides how
      to feed them to its agent;
    - `{workspace}` names where a worker may work;
    - the reply is the command's stdout, parsed by `extract_json` like every
      other role's;
    - `{cost_file}` may be written with a number, which becomes `cost_usd`.
      A backend that does not meter runs free as far as the ceiling knows,
      and the run record will show `cost_usd=0` saying exactly that.

    One subprocess and one temp directory per ask: no shared state, so the
    fan-out path can run several briefs of one role through the same brain.
    """

    PLACEHOLDERS = ("role", "prompt_file", "workspace", "cost_file")

    def __init__(self, command, root, ledger: CostLedger | None = None,
                 name: str | None = None):
        self.command = [str(part) for part in command]
        self.root = pathlib.Path(root)
        self.ledger = ledger
        self.name = name or pathlib.Path(self.command[0]).name
        self.spent_usd = 0.0

    def _argv(self, role: str, prompt_file: pathlib.Path,
              workspace: pathlib.Path, cost_file: pathlib.Path) -> list[str]:
        values = {"role": role, "prompt_file": str(prompt_file),
                  "workspace": str(workspace), "cost_file": str(cost_file)}
        out = []
        for part in self.command:
            for key, value in values.items():
                part = part.replace("{" + key + "}", value)
            out.append(part)
        return out

    def ask(self, role: str, brief: str, *, workspace=None,
            max_turns=None) -> Reply:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            prompt_file = tmp / f"{role}.md"
            prompt_file.write_text(role_prompt(role))
            cost_file = tmp / "cost"
            result = subprocess.run(
                self._argv(role, prompt_file, pathlib.Path(workspace or self.root),
                           cost_file),
                input=brief, cwd=str(workspace or self.root),
                capture_output=True, text=True)
            if result.returncode != 0:
                tail = " ; ".join((result.stderr or "").strip().splitlines()[-5:])
                raise AutoresearchError(
                    f"{self.name} ({role}) exited {result.returncode}: "
                    f"{tail or 'no stderr'}")
            cost = 0.0
            if cost_file.exists():
                text = cost_file.read_text().strip()
                if text:
                    try:
                        cost = float(text)
                    except ValueError:
                        raise AutoresearchError(
                            f"{self.name} ({role}) wrote a cost file that is "
                            f"not a number: {text[:80]!r}") from None
        if self.ledger is not None and cost:
            self.ledger.spend(cost)
        self.spent_usd += cost
        return Reply(role=role, data=extract_json(result.stdout),
                     raw=result.stdout, cost_usd=cost, backend=self.name)


class RoutingBrain:
    """Dispatch by role: `routes[role]` when the domain named one, else the
    default. The loop never learns a backend exists."""

    def __init__(self, routes: dict, default):
        self.routes = dict(routes)
        self.default = default

    def ask(self, role: str, brief: str, *, workspace=None,
            max_turns=None) -> Reply:
        brain = self.routes.get(role, self.default)
        return brain.ask(role, brief, workspace=workspace, max_turns=max_turns)


def build_brain(config, model: str | None = None,
                max_budget_usd: float | None = None):
    """Build the brain a domain's `[brain]` table describes.

    `"claude"` names the built-in SDK brain; any list of strings is a command
    per the `ProcessBrain` contract. Every backend shares one `CostLedger`, so
    the ceiling is campaign-wide no matter how many backends can spend. Absent
    table means the incumbent: one SDK brain for every role.
    """
    spec = config.brain or {"default": "claude"}
    ledger = CostLedger(max_budget_usd if max_budget_usd is not None
                        else config.policy.spend_ceiling)

    def one(value):
        if value == "claude":
            return SDKBrain(config, model=model, ledger=ledger)
        return ProcessBrain(value, root=config.paths.root, ledger=ledger)

    default = one(spec.get("default", "claude"))
    routes = {role: one(value) for role, value in spec.items()
              if role != "default"}
    return RoutingBrain(routes, default)
