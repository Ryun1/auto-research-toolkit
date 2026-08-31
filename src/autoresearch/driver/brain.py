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

    ALL = (GENERATOR, JUDGE, WORKER, CURATOR, LIBRARIAN, QC)


@dataclass
class Reply:
    role: str
    data: object
    raw: str = ""
    cost_usd: float = 0.0


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

    def __init__(self, handlers: dict, default=None):
        self.handlers = handlers
        self.default = default
        self.calls: list[tuple[str, str]] = []

    def ask(self, role: str, brief: str, *, workspace=None, max_turns=None) -> Reply:
        self.calls.append((role, brief))
        handler = self.handlers.get(role, self.default)
        if handler is None:
            return Reply(role=role, data=None, raw="(no handler)")
        data = handler(brief) if callable(handler) else handler
        return Reply(role=role, data=data, raw=json.dumps(data, default=str))


class SDKBrain:
    """The real brain: one Claude Agent SDK query per role.

    Money is metered twice on purpose -- `max_budget_usd` caps the SDK itself,
    and the caller's domain budget records what was spent -- because a ceiling
    enforced in only one place is a ceiling that stops existing the moment
    someone calls the other path.
    """

    def __init__(self, config, model: str | None = None,
                 max_budget_usd: float | None = None, allowed_tools=None):
        self.config = config
        self.model = model
        self.max_budget_usd = (max_budget_usd if max_budget_usd is not None
                               else config.policy.spend_ceiling)
        self.allowed_tools = allowed_tools or [
            "Read", "Grep", "Glob", "Bash", "Write", "Edit"]
        self.spent_usd = 0.0

    def ask(self, role: str, brief: str, *, workspace=None, max_turns=None) -> Reply:
        import asyncio
        return asyncio.run(self._ask(role, brief, workspace, max_turns))

    async def _ask(self, role, brief, workspace, max_turns) -> Reply:
        from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

        options = ClaudeAgentOptions(
            system_prompt=role_prompt(role),
            cwd=str(workspace or self.config.paths.root),
            # Only the worker and the curator write. The librarian is read-only
            # on purpose: it returns a skill body and the coordinator writes it,
            # so a role never certifies its own output (see skills.write).
            allowed_tools=(self.allowed_tools if role in (Role.WORKER, Role.CURATOR)
                           else ["Read", "Grep", "Glob"]),
            permission_mode="acceptEdits",
            max_turns=max_turns,
            model=self.model,
            max_budget_usd=(None if self.max_budget_usd is None
                            else max(0.0, self.max_budget_usd - self.spent_usd)),
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
        return Reply(role=role, data=extract_json(raw), raw=raw, cost_usd=cost)
