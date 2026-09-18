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
import math
import os
import pathlib
import re
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request
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
    cost_usd: float | None = None
    #: which backend answered, as the config named it. A curate answered by
    #: one agent and a curate answered by another are different facts on the
    #: record (the H98 principle, applied to backends), so this travels with
    #: every reply instead of living in one phase's detail.
    backend: str = ""

    def __post_init__(self):
        self.cost_usd = cost_value(self.cost_usd)


def cost_value(value):
    """None means unobserved; measured zero is a real measurement."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AutoresearchError("cost must be a finite nonnegative number or null")
    if not math.isfinite(value) or value < 0:
        raise AutoresearchError("cost must be finite and nonnegative")
    return float(value)


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
        self.unknown = False
        self._lock = threading.Lock()

    def remaining(self) -> float | None:
        with self._lock:
            if self.unknown and self.ceiling is not None:
                raise AutoresearchError(
                    "cost is unknown: supply measured backend costs before further spending "
                    "under a finite monetary ceiling")
            if self.ceiling is None:
                return None
            return max(0.0, self.ceiling - self.spent)

    def spend(self, cost: float | None) -> None:
        cost = cost_value(cost)
        with self._lock:
            if cost is None:
                self.unknown = True
            else:
                self.spent += cost


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
            return Reply(role=role, data=None, raw="(no handler)", backend=self.backend, cost_usd=0.0)
        data = handler(brief) if callable(handler) else handler
        return Reply(role=role, data=data, raw=json.dumps(data, default=str),
                     backend=self.backend, cost_usd=0.0)


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
      Missing costs are unknown, never zero. A finite shared ceiling refuses
      subsequent dispatch until the missing measurement is reconciled.

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
        if self.ledger is not None:
            remaining = self.ledger.remaining()
            if remaining is not None and remaining <= 0:
                raise AutoresearchError("monetary ceiling exhausted")
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
            cost = None
            if cost_file.exists():
                text = cost_file.read_text().strip()
                try:
                    cost = cost_value(float(text))
                except (ValueError, AutoresearchError) as exc:
                    if self.ledger is not None:
                        self.ledger.spend(None)
                    raise AutoresearchError(
                        f"{self.name} ({role}) wrote invalid cost: {text[:80]!r}") from exc
        if self.ledger is not None:
            self.ledger.spend(cost)
        self.spent_usd = (None if cost is None or self.spent_usd is None
                          else self.spent_usd + cost)
        return Reply(role=role, data=extract_json(result.stdout),
                     raw=result.stdout, cost_usd=cost, backend=self.name)


class TypeSafeBrain:
    """Jev, through the TypeSafe System One API: a dedicated third backend.

    Jev is not a text model. It evaluates typed questions (`choice`, `score`,
    `noul`) against a state and returns structured answers -- no prose, no
    tools, no agent loop. That makes it a poor generator, worker, scout or
    librarian (all four must produce text or act), and a good fit for the two
    roles that are pure judgement over a brief the coordinator already
    assembled:

    - `judge` -- one `choice` question per top-ranked entry (promote / demote /
      none), converted to the veto list `apply_veto` already polices. The
      justification the contract demands is synthesized from the calibrated
      probabilities, so it is recorded evidence, not generated prose. A veto
      fires only when the chosen action out-probabilities `none`.
    - `qc` -- one `noul` question per mechanical problem the checks already
      raised, keeping the real ones. It cannot file harness debt: a debt item
      needs a repro the model would have to compose, and Jev composes nothing.
      `harness_debt` is therefore always empty, visibly.

    Every other role refuses at ask time -- a refusal the phase records, never
    an empty result that reads as a clean pass (H98). The brief arrives as
    JSON (the coordinator's `_brief`) and is sent as the API's `state`; the
    coordinator's instructions ride along inside it.

    Fail-closed: building this brain without
    `TYPESAFE_API_KEY` set is a refusal at build, and `[brain]` must say
    `authorize_spend = true` before `build_brain` will build it at all.

    Cost: the API reports token usage, not dollars. Without per-mtok prices
    (`typesafe_input_per_mtok` / `typesafe_output_per_mtok`) the cost is
    unknown, and the shared ledger treats an unknown cost under a finite
    ceiling exactly as it does for every other backend. A brief that maps to
    no questions (an empty ranking, no mechanical problems) returns the
    role's empty contract without spending an API call -- nothing to judge is
    a measurement, not a skip.
    """

    #: Roles this backend can serve; everything else refuses.
    SUPPORTED = (Role.JUDGE, Role.QC)

    #: Top-ranked entries the judge reviews, one question each. A reorder from
    #: position 200 is noise whatever the model says, and the shortlist this
    #: feeds is `k` slots (the fanout, typically 3).
    JUDGE_REVIEW_CAP = 12

    API_URL = "https://api.typesafe.ai/v1/systemone"

    def __init__(self, model="jev-latest", url=None, input_per_mtok=None,
                 output_per_mtok=None, ledger: CostLedger | None = None,
                 timeout=30.0, name="typesafe", api_key=None):
        self.model = model
        self.url = url or self.API_URL
        self.input_per_mtok = input_per_mtok
        self.output_per_mtok = output_per_mtok
        self.ledger = ledger
        self.timeout = timeout
        self.name = name
        self.spent_usd = 0.0
        self.api_key = api_key or os.environ.get("TYPESAFE_API_KEY")
        if not self.api_key:
            raise AutoresearchError(
                "the typesafe brain is selected but TYPESAFE_API_KEY is not "
                "set; the loop cannot start with a backend it cannot call")

    # -- the Brain protocol ---------------------------------------------------

    #: Context fields that ride along with the trimmed wire state, when the
    #: brief carries them. Everything else stays in the record.
    WIRE_CONTEXT = ("domain", "iteration", "goal", "instruction",
                    "budget_remaining")

    def _wire_state(self, role: str, state: dict) -> dict:
        """The state System One actually sees.

        The coordinator's brief carries the whole record -- open entries,
        closed directions, the full ranking -- and a brief that size exceeds
        the API's token budget (HTTP 400 max_tokens_exceeded at ~258KB).
        These roles read only their question inputs, so only those travel:
        the judge reviews the top `JUDGE_REVIEW_CAP` ranking rows plus the
        reserves, qc the mechanical problems. Context rides as a small
        allowlist of scalars/maps, never entry corpora.
        """
        if role == Role.JUDGE:
            wire = {"ranking": (state.get("ranking") or [])[:self.JUDGE_REVIEW_CAP],
                    "explore_reserve": state.get("explore_reserve") or [],
                    "coverage_reserve": state.get("coverage_reserve") or []}
        else:
            wire = {"mechanical_problems":
                    state.get("mechanical_problems") or []}
        for key in self.WIRE_CONTEXT:
            if key in state:
                wire[key] = state[key]
        return wire

    def ask(self, role: str, brief: str, *, workspace=None,
            max_turns=None) -> Reply:
        if role not in self.SUPPORTED:
            raise AutoresearchError(
                f"the typesafe brain cannot serve {role!r}: Jev answers typed "
                f"questions, it does not generate text or run tools. Roles it "
                f"serves: {', '.join(self.SUPPORTED)}.")
        try:
            state = json.loads(brief) if isinstance(brief, str) else brief
        except ValueError:
            state = brief          # a prose brief is a valid state too
        questions = self._questions(role, state)
        if not questions:
            return Reply(role=role, data=self._contract(role, state, {}),
                         raw="", cost_usd=0.0, backend=self.name)
        if self.ledger is not None:
            remaining = self.ledger.remaining()
            if remaining is not None and remaining <= 0:
                raise AutoresearchError("monetary ceiling exhausted")
        response = self._post({"state": self._wire_state(role, state),
                               "model": self.model,
                               "questions": questions})
        answers = response.get("answers")
        if not isinstance(answers, dict):
            raise AutoresearchError(f"typesafe ({role}) returned no answers map")
        missing = set(questions) - set(answers)
        if missing:
            raise AutoresearchError(
                f"typesafe ({role}) did not answer {sorted(missing)}")
        cost = self._cost(response.get("usage"))
        if self.ledger is not None:
            self.ledger.spend(cost)
        self.spent_usd = (None if cost is None or self.spent_usd is None
                          else self.spent_usd + cost)
        return Reply(role=role, data=self._contract(role, state, answers),
                     raw=json.dumps(response, default=str), cost_usd=cost,
                     backend=self.name)

    # -- questions per role ----------------------------------------------------

    def _questions(self, role: str, state) -> dict:
        state = state if isinstance(state, dict) else {}
        if role == Role.JUDGE:
            return self._judge_questions(state)
        return self._qc_questions(state)

    def _judge_questions(self, state: dict) -> dict:
        ranking = state.get("ranking") or []
        reserves = set(state.get("explore_reserve") or []) \
            | set(state.get("coverage_reserve") or [])
        questions = {}
        for i, row in enumerate(ranking[:self.JUDGE_REVIEW_CAP]):
            entry_id = str(row.get("id", "")).strip()
            if not entry_id:
                continue
            note = ""
            if entry_id in reserves:
                note = (" It takes an explore/coverage reserve slot, which "
                        "sits low on score by design -- not by itself a "
                        "reason to demote.")
            questions[f"reorder_{entry_id}"] = {
                "type": "choice",
                "instructions": (
                    f"Entry {entry_id} ({row.get('title', '')!r}) ranks at "
                    f"position {i + 1} of {len(ranking)} with score "
                    f"{row.get('score')}.{note} Should the ranking be "
                    "reordered for this entry?"),
                "criteria": {
                    "promote": "The recorded evidence justifies ranking it first",
                    "demote": "The recorded evidence justifies ranking it last",
                    "none": "The formula's ordering is right",
                },
            }
        return questions

    def _qc_questions(self, state: dict) -> dict:
        questions = {}
        for i, problem in enumerate(state.get("mechanical_problems") or []):
            questions[f"problem_{i}"] = {
                "type": "noul",
                "instructions": (
                    "Is this a real defect in the harness rather than a "
                    f"transient or benign condition? Problem: "
                    f"{str(problem)[:500]}"),
                "criteria": {
                    "true": "A real defect worth recording",
                    "false": "Transient or benign; not a defect",
                },
            }
        return questions

    # -- answers to the role contract -------------------------------------------

    def _contract(self, role: str, state, answers: dict):
        state = state if isinstance(state, dict) else {}
        if role == Role.JUDGE:
            return self._judge_vetoes(state, answers)
        return self._qc_contract(state, answers)

    def _judge_vetoes(self, state: dict, answers: dict) -> list:
        ranking_ids = {str(row.get("id")) for row in state.get("ranking") or []}
        vetoes = []
        for key, answer in answers.items():
            if not key.startswith("reorder_"):
                continue
            entry_id = key[len("reorder_"):]
            if entry_id not in ranking_ids:
                continue
            action = answer.get("choice")
            if action not in ("promote", "demote"):
                continue
            probabilities = answer.get("probabilities") or {}
            if probabilities.get(action, 0) <= probabilities.get("none", 0):
                continue
            confidence = answer.get("confidence")
            confidence = (f", confidence {confidence:.2f}"
                          if isinstance(confidence, (int, float)) else "")
            vetoes.append({
                "entry_id": entry_id,
                "action": action,
                "justification": (
                    f"jev {self.model}: p={probabilities.get(action, 0):.2f}"
                    f"{confidence} for {action} over none"),
            })
        return vetoes

    def _qc_contract(self, state: dict, answers: dict) -> dict:
        problems = state.get("mechanical_problems") or []
        kept = []
        for key, answer in answers.items():
            if not key.startswith("problem_"):
                continue
            noul = answer.get("noul")
            if isinstance(noul, bool) or not isinstance(noul, (int, float)):
                raise AutoresearchError(
                    f"typesafe (qc) returned a non-numeric noul for {key}: "
                    f"{noul!r}")
            if noul > 0.5:
                kept.append(problems[int(key[len("problem_"):])])
        # Jev generates no text, so it cannot compose the repro a harness-debt
        # filing demands; it triages, it does not report. Always empty, on
        # purpose, so the shape stays what the coordinator's contract expects.
        return {"problems": kept, "harness_debt": [],
                "verdict": "problems" if kept else "clean"}

    # -- the wire ----------------------------------------------------------------

    def _cost(self, usage) -> float | None:
        """Dollars from token usage when the domain priced the model; unknown
        (None) otherwise. The ledger, not this brain, decides what an unknown
        cost means under a ceiling."""
        if not isinstance(usage, dict) \
                or self.input_per_mtok is None or self.output_per_mtok is None:
            return None
        tokens_in = usage.get("input_tokens")
        tokens_out = usage.get("output_tokens")
        if not isinstance(tokens_in, (int, float)) \
                or not isinstance(tokens_out, (int, float)):
            return None
        return (tokens_in * self.input_per_mtok
                + tokens_out * self.output_per_mtok) / 1e6

    def _post(self, payload: dict) -> dict:
        """One POST to the System One endpoint. A seam of its own so tests can
        stand in for the wire without an HTTP stub."""
        request = urllib.request.Request(
            self.url, data=json.dumps(payload, default=str).encode(),
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"},
            method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as r:
                body = r.read().decode()
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read()[:200].decode(errors="replace")
            except OSError:
                detail = ""
            raise AutoresearchError(
                f"typesafe request failed: HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise AutoresearchError(f"typesafe request failed: {exc}") from exc
        try:
            return json.loads(body)
        except ValueError as exc:
            raise AutoresearchError(
                f"typesafe returned unparsable JSON: {body[:200]!r}") from exc


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


def build_brain(config, max_budget_usd: float | None = None,
                allow_paid: bool | None = None):
    """Build the brain a domain's `[brain]` table describes.

    `"typesafe"` names the built-in TypeSafe (Jev) brain; any list of strings
    is a command per the `ProcessBrain` contract. Every backend shares one
    `CostLedger`, so the ceiling is campaign-wide no matter how many backends
    can spend.

    The built-in brain spends API money, so it is **fail-closed**: a domain
    must name it deliberately with `[brain] authorize_spend = true`, or the
    caller must pass `allow_paid=True` (`--allow-paid-brain`). An absent
    table, or an absent `default`, is a refusal that names the remedy: route
    the roles to commands.
    """
    spec = config.brain or {}
    # The flag GRANTS; it never revokes. argparse's store_true default is
    # False, not None, so a caller that did not pass the flag must not undo a
    # domain's own `authorize_spend = true`.
    authorized = bool(spec.get("authorize_spend", False)) or bool(allow_paid)
    # Role routes only: `default`, `authorize_spend` and the `typesafe_*`
    # options are settings, never backends.
    roles = {key: value for key, value in spec.items()
             if key not in ("default", "authorize_spend")
             and not key.startswith("typesafe_")}
    if "default" not in spec:
        raise AutoresearchError(
            "no default brain: set `[brain] default = [\"<command>\"]` in "
            "domain.toml to a command per the ProcessBrain contract "
            "(`driver/brain.py`), or name one per role with "
            "`brain.<role> = [\"<command>\"]`. The one built-in brain, "
            "\"typesafe\", spends API money and additionally needs "
            "`authorize_spend = true` or --allow-paid-brain.")
    wants_paid = [key for key, value in
                  [("default", spec["default"]), *roles.items()]
                  if value == "typesafe"]
    if wants_paid and not authorized:
        raise AutoresearchError(
            "the built-in TypeSafe brain spends API money and is refused "
            "until you say so: set `[brain] authorize_spend = true` in "
            "domain.toml, or pass --allow-paid-brain. Route the roles that "
            "need judgement to native agents with "
            "`brain.<role> = [\"<command>\"]` instead; "
            f"table keys naming a paid brain: {sorted(set(wants_paid))}")
    ledger = CostLedger(max_budget_usd if max_budget_usd is not None
                        else config.policy.spend_ceiling)

    def one(value):
        if value == "typesafe":
            return TypeSafeBrain(
                model=spec.get("typesafe_model", "jev-latest"),
                url=spec.get("typesafe_url"),
                input_per_mtok=spec.get("typesafe_input_per_mtok"),
                output_per_mtok=spec.get("typesafe_output_per_mtok"),
                ledger=ledger)
        return ProcessBrain(value, root=config.paths.root, ledger=ledger)

    default = one(spec["default"])
    routes = {role: one(value) for role, value in roles.items()}
    return RoutingBrain(routes, default)
