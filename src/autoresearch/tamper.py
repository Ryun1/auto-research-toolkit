"""Diff hunks shaped like tampering, listed at a `confirmed` close (#39).

The exploits METR found in o3's RE-Bench runs had shapes: patching
`time.time`, monkey-patching the grader, overriding `__eq__`. ImpossibleBench
found models editing the tests. Each is visible in a diff, and none is
proof: a timing change is often the optimisation itself, and a test edit
is often honest. So this lists, typed, what a person or the QC role should
read before trusting a confirmation, and decides nothing (invariant 2: a
regex over prose or code never reaches a decision).

Kinds:
  evaluator    a hunk in a file the domain's evaluator is made of (#36)
  test         a hunk in a test file
  timing       an added line calling a clock or a device sync
  equality     an added `__eq__` / `__ne__` / `__hash__`
  monkeypatch  an added assignment to an imported module's attribute,
               a `setattr`, or a mock patch

Files in the findings lane (memos, run rows, docs) are not scanned: prose
about `time.time` is not a patch of it.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass

_TEST = re.compile(r"(^|/)(tests?|spec)/|(^|/)test_[^/]*\.py$|_test\.\w+$"
                   r"|\.(spec|test)\.\w+$|(^|/)conftest\.py$")
_TIMING = re.compile(r"\b(time\.(time|perf_counter|monotonic|process_time)"
                     r"(_ns)?|perf_counter(_ns)?|cuda\.synchronize|"
                     r"cuda\.Event|timeit)\b")
_EQUALITY = re.compile(r"\bdef\s+__(eq|ne|hash)__\s*\(")
_PATCH_CALL = re.compile(r"\bsetattr\s*\(|\bmock\.patch\b|\bpatch\s*\(|"
                         r"\bmonkeypatch\.setattr\b|\bimportlib\.reload\b")
_ASSIGN = re.compile(r"^\s*([A-Za-z_]\w*)\.[\w.]*\w\s*=(?!=)")
_IMPORT = re.compile(r"^\s*(?:import\s+([\w.]+)(?:\s+as\s+(\w+))?"
                     r"|from\s+[\w.]+\s+import\s+(.+))")
#: Modules worth flagging even when the diff does not show their import.
_PATCHED_MODULES = {"time", "os", "sys", "builtins", "math", "random",
                    "socket", "subprocess", "json", "numpy", "np", "torch",
                    "datetime", "timeit", "functools", "operator"}


@dataclass(frozen=True)
class Hunk:
    kind: str
    path: str
    line: int
    text: str

    def __str__(self) -> str:
        return f"{self.kind}  {self.path}:{self.line}  {self.text.strip()[:100]}"


def _imported(lines) -> set[str]:
    names = set()
    for line in lines:
        match = _IMPORT.match(line)
        if not match:
            continue
        if match.group(1):
            names.add(match.group(2) or match.group(1).split(".")[0])
        else:
            for part in match.group(3).split(","):
                part = part.strip().strip("()")
                if part:
                    names.add(part.split(" as ")[-1].strip())
    return names


def scan(config, diff: str) -> list[Hunk]:
    """Typed tamper-shaped hunks in a unified diff (`git diff` output)."""
    from . import evaluator
    evaluator_files = set(evaluator.files(config))
    files: dict[str, list[tuple[int, str]]] = {}
    path, line_no = None, 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            path = None if target == "/dev/null" else re.sub(r"^b/", "", target)
            if path is not None:
                files.setdefault(path, [])
            continue
        if raw.startswith("@@"):
            match = re.search(r"\+(\d+)", raw)
            line_no = int(match.group(1)) if match else 0
            continue
        if path is None or raw.startswith(("---", "diff ", "index ")):
            continue
        if raw.startswith("+"):
            files[path].append((line_no, raw[1:]))
            line_no += 1
        elif not raw.startswith("-"):
            line_no += 1
    out = []
    for path, added in files.items():
        if config.lanes.classify(path) == "findings" or not added:
            continue
        first = added[0]
        if path in evaluator_files:
            out.append(Hunk("evaluator", path, *first))
        if _TEST.search(path):
            out.append(Hunk("test", path, *first))
        modules = _imported(text for _, text in added) | _PATCHED_MODULES
        for number, text in added:
            if _TIMING.search(text):
                out.append(Hunk("timing", path, number, text))
            if _EQUALITY.search(text):
                out.append(Hunk("equality", path, number, text))
            assign = _ASSIGN.match(text)
            if _PATCH_CALL.search(text) or (
                    assign and assign.group(1) in modules
                    and assign.group(1) not in ("self", "cls")):
                out.append(Hunk("monkeypatch", path, number, text))
    return out


def _git(root, *args) -> str | None:
    proc = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    return proc.stdout if proc.returncode == 0 else None


def diff_of(tree, base="HEAD") -> str | None:
    """Tracked changes since `base`, plus untracked files as whole additions.
    None outside git -- there is no diff to read, and none is guessed."""
    tracked = _git(tree, "diff", "--unified=0", "--no-color", base)
    if tracked is None:
        return None
    parts = [tracked]
    for name in (_git(tree, "ls-files", "--others", "--exclude-standard") or "").splitlines():
        # --no-index exits 1 whenever the files differ, which here is always.
        parts.append(subprocess.run(
            ["git", "diff", "--unified=0", "--no-color", "--no-index",
             "/dev/null", name], cwd=tree, capture_output=True, text=True).stdout)
    return "\n".join(parts)


def base_of(tree) -> str | None:
    """The commit a workspace starts from, or None outside git."""
    head = _git(tree, "rev-parse", "HEAD")
    return head.strip() if head else None


def scan_tree(config, tree, base="HEAD") -> list[Hunk] | None:
    diff = diff_of(tree, base)
    return None if diff is None else scan(config, diff)


def event_detail(hunks) -> str:
    return json.dumps([asdict(h) for h in hunks])
