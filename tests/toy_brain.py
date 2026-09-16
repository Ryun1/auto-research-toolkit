"""A scripted brain that does real work on the toy domain.

It exercises the coordinator's control flow -- generate, rank, dispatch in
parallel, curate, QC, stop -- with no model in the loop, which is what makes
`ar loop` a unit test rather than a bill. The worker genuinely runs the domain's
measure command and genuinely decides the entry's registered bar; only the
judgement about *which* configuration to try is scripted.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

from autoresearch.driver.brain import Reply, Role

# A search plan: each becomes one entry, and the worker measures exactly it.
PLAN = [
    ("fold is free ops", {"unroll": 1, "width": 32, "fold": 1, "cache": 0},
     ["ops-multiplier"], 0.8, 0.15, 1),
    ("cache costs 8 peak for 5% ops", {"unroll": 1, "width": 32, "fold": 1, "cache": 1},
     ["width-cost"], 0.3, 0.05, 1),
    ("width to the validity gate", {"unroll": 1, "width": 16, "fold": 1, "cache": 0},
     ["width-floor"], 0.9, 0.30, 1),
    ("unroll trades ops against 2x peak", {"unroll": 8, "width": 16, "fold": 1, "cache": 0},
     ["unroll-tradeoff"], 0.7, 0.60, 2),
    ("unroll past the joint optimum", {"unroll": 20, "width": 16, "fold": 1, "cache": 0},
     ["unroll-tradeoff"], 0.3, 0.10, 2),
]


class ToyBrain:
    def __init__(self, config, per_iteration=2):
        self.config = config
        self.per_iteration = per_iteration
        self.filed = 0
        self.calls = []

    def ask(self, role, brief, *, workspace=None, max_turns=None) -> Reply:
        self.calls.append(role)
        payload = json.loads(brief)
        handler = {
            Role.GENERATOR: self._generate, Role.JUDGE: lambda p: [],
            Role.WORKER: self._work, Role.CURATOR: lambda p: {"reprice": [], "notes": []},
            Role.LIBRARIAN: self._distil,
            Role.QC: lambda p: {"problems": [], "harness_debt": [], "verdict": "clean"},
        }[role]
        return Reply(role=role, data=handler(payload))

    def _generate(self, payload):
        if payload.get("generator_index", 0) != 0:
            return []                      # one generator files; the rest see the same plan
        out = []
        for title, knobs, mechanisms, confidence, impact, cost in \
                PLAN[self.filed:self.filed + self.per_iteration]:
            out.append({
                "title": title, "hypothesis": f"measure {knobs}",
                "prediction": "objective falls", "bar": "confirmed if objective improves",
                "confidence": confidence, "impact": impact, "cost": cost,
                "mechanisms": mechanisms, "why_filed": "search plan",
                "sources": [json.dumps(knobs)],
            })
        self.filed += len(out)
        return out

    def _distil(self, payload):
        """One skill per closed entry the record has not distilled yet, cited
        properly. Enough to exercise the phase end to end with no model -- which
        is the whole point of a scripted brain."""
        out = []
        for item in payload.get("undistilled", [])[:1]:
            out.append({
                "name": f"lesson-{item['id'].lower()}",
                "description": (f"Use when a proposal looks like {item['title']!r}: "
                                f"the same knobs, the same mechanism."),
                "cites": [item["id"]],
                "body": (f"# {item['summary'] or item['title']}\n\n"
                         f"Measured and closed as {item['status']} [{item['id']}].\n"),
            })
        return {"write": out, "retire": [], "notes": []}

    def _work(self, payload):
        entry = payload["entry"]
        workspace = pathlib.Path(payload["workspace"])
        knobs = json.loads(entry["sources"][0]) if entry.get("sources") else {}
        args = [f"--knob={k}={v}" for k, v in knobs.items()]
        proc = subprocess.run(
            [sys.executable, "-m", "autoresearch.cli",
             "--session", f"worker-{entry['id']}",
             "measure", "--entry", entry["id"], "--", *args],
            cwd=workspace, capture_output=True, text=True)
        line = proc.stdout.strip()
        if proc.returncode != 0:
            return {"verdict": "inconclusive",
                    "summary": f"measure failed: {proc.stderr[:200]}",
                    "verification": {"reread": True,
                                     "claims_checked": ["failure reproduced"]}}

        objective = float(line.split("objective=")[1].split()[0].replace(",", "")) \
            if "objective=" in line else None
        baseline = payload["goal"].get("best_so_far")
        memo = f"inbox/{entry['id']}-toy.md"
        (workspace / memo).parent.mkdir(parents=True, exist_ok=True)
        (workspace / memo).write_text(
            f"# {entry['id']} — {entry['title']}\n\n"
            f"knobs: {knobs}\nobjective: {objective}\nbaseline: {baseline}\n")

        if objective is None:
            return {"verdict": "refuted", "memo": memo, "closure_kind": "mechanism",
                    "summary": "run was invalid: below the validity gate", "runs": 1,
                    "verification": {"reread": True,
                                     "claims_checked": ["objective", "validity gate"]}}
        improved = baseline is None or objective < baseline
        if improved:
            return {"verdict": "confirmed", "memo": memo, "runs": 1,
                    "summary": f"objective {objective:,.0f} beats baseline {baseline}",
                    "verification": {"reread": True,
                                     "claims_checked": ["objective", "arithmetic"]}}
        return {"verdict": "refuted", "memo": memo, "closure_kind": "cell",
                "reopen_condition": f"measured at {knobs} only; re-opens at another width",
                "summary": f"objective {objective:,.0f} does not beat {baseline:,.0f}",
                "runs": 1,
                "verification": {"reread": True,
                                 "claims_checked": ["objective", "arithmetic"]}}
