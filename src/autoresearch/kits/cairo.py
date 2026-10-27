"""Measuring a Cairo patch with Scarb and Starknet Foundry (snforge).

A kit is vendored, not imported: `ar kit sync` copies this file into the domain
as `bin/cairokit.py`, and `[evaluator] kits = ["cairo"]` makes core hash it
with the rest of the evaluator. So it runs under whatever `python3` runs
`bin/measure`, imports nothing outside the standard library, and is edited
here and synced -- never edited in a domain.

Three domains (falcon1024, mldsa87, SLH-DSA-128) measured the same upstream
with three copies of this logic, and each fix stayed in the domain that found
it. What is here, and the incident each piece exists for:

* `parse_snforge` -- refuses a log whose `[PASS]`/`[FAIL]` lines do not add up
  to snforge's own `Tests:` summary. A regex that stops matching after a format
  change returns fewer rows, not an error.
* `check_patch` -- the hosted submit rules as far as a diff shows them.
* `apply_patch` -- `git apply` inside an enclosing repository resolves paths
  from that repository's root and silently skips every file (SLH-DSA-128 H3),
  so discovery is fenced off and the result is proven by a reverse check.
* `declare_sizes` / `over_caps` -- an account over Starknet's declare caps
  cannot be deployed, so its gas is not a result (mldsa87 H4). falcon1024
  recorded seven such rows as `ok`.
* `audited_libfuncs` -- the upstream builds the account with `experimental`
  libfuncs; the challenges declare it with `audited` ones, so a patch can pass
  locally and fail to declare.
* `Sandbox` -- other people's patches build and run only inside Docker, with no
  network.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

KIT = "cairo"

#: Starknet's declare limits: Sierra class bytes and CASM bytecode felts.
MAX_CLASS_BYTES = 4_089_446
MAX_CASM_FELTS = 81_920

#: The hosted patch size limit the PQ verifier challenges share.
MAX_PATCH_BYTES = 256 * 1024


class KitError(Exception):
    """A measurement that cannot be trusted. Report it; never score it."""


# -- snforge output ---------------------------------------------------------

@dataclass
class TestResult:
    name: str                       # the test function, `bench_verify_x`
    path: str                       # the full path snforge printed
    passed: bool
    l2_gas: int | None = None       # from the `[PASS] ... (l2_gas: ~N)` header
    sierra_gas: int | None = None   # from `sierra gas: N` (--detailed-resources)
    failure: str = ""               # the text under a `[FAIL]` line


_HEADER = re.compile(r"^\[(PASS|FAIL)\] (\S+)(?: \((.*)\))?\s*$")
_L2 = re.compile(r"\bl2_gas: ~?(\d+)")
_SIERRA = re.compile(r"^\s+sierra gas: (\d+)\s*$")
_SUMMARY = re.compile(r"^Tests: (\d+) passed, (\d+) failed")
_BLOCK_END = re.compile(r"^(Running \d+ test|Tests: |Failures:|Collected \d+ test)")


def parse_snforge(log: str) -> dict[str, TestResult]:
    """Every test snforge reported, by function name.

    Raises KitError when the log has no `Tests:` summary (the build failed, or
    snforge never ran), when the parsed results do not add up to it, or when
    two tests share a function name, since callers look tests up by name."""
    tests: dict[str, TestResult] = {}
    passed = failed = 0
    summaries = 0
    current: TestResult | None = None
    for line in log.splitlines():
        header = _HEADER.match(line)
        if header:
            verdict, path, resources = header.groups()
            name = path.rsplit("::", 1)[-1]
            if name in tests:
                raise KitError(f"two tests named {name!r} ({tests[name].path}, {path}); "
                               "look them up by path or rename one")
            l2 = _L2.search(resources or "")
            current = tests[name] = TestResult(
                name=name, path=path, passed=verdict == "PASS",
                l2_gas=int(l2.group(1)) if l2 else None)
            continue
        summary = _SUMMARY.match(line)
        if summary:
            summaries += 1
            passed += int(summary.group(1))
            failed += int(summary.group(2))
        if _BLOCK_END.match(line):
            current = None
            continue
        if current is None:
            continue
        sierra = _SIERRA.match(line)
        if sierra and current.passed:
            current.sierra_gas = int(sierra.group(1))
        elif not current.passed and line.strip():
            current.failure += line.strip() + "\n"
    if not summaries:
        raise KitError("no `Tests:` summary in the snforge output: the build failed "
                       "or snforge did not run\n" + log[-1500:])
    got_pass = sum(t.passed for t in tests.values())
    got_fail = len(tests) - got_pass
    if (got_pass, got_fail) != (passed, failed):
        raise KitError(f"snforge reported {passed} passed / {failed} failed but "
                       f"{got_pass} / {got_fail} were parsed: its output format "
                       "has changed, and a partial parse is not a measurement")
    return tests


def gas(tests: dict[str, TestResult], name: str, kind: str = "l2") -> int:
    """One passing test's gas. `kind` is "l2" or "sierra"; the challenges
    differ, so the caller says which one it is scored in."""
    if kind not in ("l2", "sierra"):
        raise ValueError(f"gas kind must be 'l2' or 'sierra', not {kind!r}")
    test = tests.get(name)
    if test is None:
        raise KitError(f"{name} did not run")
    if not test.passed:
        raise KitError(f"{name} failed:\n{test.failure}")
    value = test.l2_gas if kind == "l2" else test.sierra_gas
    if value is None:
        raise KitError(f"{name} reported no {kind} gas"
                       + (" (run snforge with --detailed-resources)"
                          if kind == "sierra" else ""))
    return value


def failures(tests: dict[str, TestResult]) -> list[str]:
    return [name for name, test in tests.items() if not test.passed]


# -- the patch ---------------------------------------------------------------

def _matches(path: str, patterns) -> bool:
    return any(fnmatch.fnmatchcase(path, p) for p in patterns)


def touched(patch: str) -> list[str]:
    """Every path a unified diff names on either side, /dev/null aside."""
    out = []
    for a, b in re.findall(r"^--- (?:a/)?(\S+)|^\+\+\+ (?:b/)?(\S+)", patch, re.M):
        path = a or b
        if path != "/dev/null" and path not in out:
            out.append(path)
    return out


def check_patch(patch: str, surface, forbidden=(),
                max_bytes: int = MAX_PATCH_BYTES) -> str | None:
    """The submit rules a diff can show. Returns the first violation, or None.

    `surface` and `forbidden` are fnmatch globs over repository paths, where
    `*` also crosses `/`: `crates/x/src/*.cairo` is every Cairo file under it.
    Files may be added and edited, never deleted or renamed, and no `cfg`
    attribute other than `cfg(test)` may be added."""
    size = len(patch.encode())
    if size > max_bytes:
        return f"patch is {size} bytes > {max_bytes}"
    if re.search(r"^(deleted file mode|rename from|\+\+\+ /dev/null)", patch, re.M):
        return "patch deletes or renames a file"
    for path in touched(patch):
        parts = PurePosixPath(path).parts
        if path.startswith("/") or ".." in parts:
            return f"{path} escapes the repository"
        if _matches(path, forbidden) or not _matches(path, surface):
            return f"{path} is outside the edit surface {list(surface)}"
    for line in patch.splitlines():
        if (line.startswith("+") and not line.startswith("+++")
                and re.search(r"#\[cfg\((?!test\))", line)):
            return f"cfg other than cfg(test): {line[1:].strip()}"
    return None


def export(repo, commit: str, dest) -> Path:
    """A clean tree of `commit` from `repo` (git archive: local edits and
    untracked files never leak in)."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(["git", "-C", str(repo), "archive", commit],
                             check=True, capture_output=True)
    subprocess.run(["tar", "-x", "-C", str(dest)], input=archive.stdout, check=True)
    return dest


def apply_patch(tree, patch: str) -> str | None:
    """Apply `patch` in `tree`. Returns an error, or None once the patch is
    proven applied. An empty patch is the unchanged baseline."""
    if not patch.strip():
        return None
    tree = Path(tree)
    # A tree inside another repository: git apply resolves the patch from
    # that repository's root and skips every file without an error (H3).
    env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(tree.resolve().parent)}
    done = subprocess.run(["git", "apply", "--whitespace=nowarn", "-"], cwd=tree,
                          input=patch, text=True, capture_output=True, env=env)
    if done.returncode:
        return "git apply: " + done.stderr[-500:]
    check = subprocess.run(["git", "apply", "--reverse", "--check", "-"], cwd=tree,
                           input=patch, text=True, capture_output=True, env=env)
    if check.returncode:
        return "the patch did not land: git apply reported success but it does not reverse"
    return None


# -- the declared account ----------------------------------------------------

ACCOUNTS_TOML = "pq-accounts/contracts/Scarb.toml"
_LIBFUNCS = re.compile(r'^(allowed-libfuncs-list\.name\s*=\s*)"(\w+)"', re.M)


def audited_libfuncs(tree, toml: str = ACCOUNTS_TOML) -> None:
    """Build the account the way the challenges declare it: audited libfuncs.

    The upstream ships `experimental`, so a local build accepts a libfunc the
    hosted build refuses. falcon1024 and mldsa87 built that way; SLH-DSA-128's
    preflight switched it. Raises if the setting is not there, since a
    replacement that matches nothing is a switch that silently never happens."""
    path = Path(tree) / toml
    text = path.read_text() if path.is_file() else ""
    if not _LIBFUNCS.search(text):
        raise KitError(f"no allowed-libfuncs-list.name in {toml}; cannot "
                       "require audited libfuncs")
    path.write_text(_LIBFUNCS.sub(r'\1"audited"', text))


def declare_sizes(tree, package: str, contract: str,
                  target: str = "target/dev") -> dict[str, int]:
    """`class_bytes` and `casm_felts` of `scarb build`'s output for one contract."""
    base = Path(tree) / target
    sierra = base / f"{package}_{contract}.contract_class.json"
    casm = base / f"{package}_{contract}.compiled_contract_class.json"
    if not sierra.is_file() or not casm.is_file():
        raise KitError(f"scarb build produced no {package}_{contract} class "
                       f"in {target}; is casm = true set for the contract target?")
    return {"class_bytes": sierra.stat().st_size,
            "casm_felts": len(json.loads(casm.read_text())["bytecode"])}


def over_caps(sizes: dict[str, int]) -> str | None:
    """Why an account cannot be declared, or None."""
    over = [f"{key} {sizes[key]:,} > {cap:,}"
            for key, cap in (("class_bytes", MAX_CLASS_BYTES),
                             ("casm_felts", MAX_CASM_FELTS))
            if sizes[key] > cap]
    return ("over the declare caps: " + ", ".join(over)) if over else None


# -- running the toolchain ---------------------------------------------------

@dataclass
class Sandbox:
    """Docker for code we did not write. No network unless asked for, and the
    tree mounted at /work."""
    image: str
    cache_volume: str = ""
    cache_path: str = "/opt/falcon/scarb-cache"
    memory: str = "6g"
    cpus: str = "4"
    platform: str = "linux/amd64"

    def argv(self, tree, cmd, network: bool = False) -> list[str]:
        args = ["docker", "run", "--rm", "--platform", self.platform,
                "-v", f"{Path(tree).resolve()}:/work", "-w", "/work",
                "--memory", self.memory, "--cpus", self.cpus]
        if self.cache_volume:
            args += ["-v", f"{self.cache_volume}:{self.cache_path}"]
        args += (["-e", "SCARB_OFFLINE=false"] if network
                 else ["--network", "none", "-e", "SCARB_OFFLINE=true"])
        return [*args, self.image, *cmd]


def run(tree, cmd, sandbox: Sandbox | None = None, network: bool = False,
        timeout: int | None = None) -> subprocess.CompletedProcess:
    """`cmd` in `tree`: on the host, or inside `sandbox` when one is given."""
    argv = sandbox.argv(tree, cmd, network) if sandbox else list(cmd)
    return subprocess.run(argv, cwd=None if sandbox else tree, text=True,
                          capture_output=True, timeout=timeout)


def snforge(tree, args, sandbox: Sandbox | None = None,
            timeout: int | None = None) -> tuple[dict[str, TestResult], str]:
    """Run `snforge test <args>` and parse it. Returns (tests, log).

    A failing test is a result the caller reads from `failures`; snforge
    exiting non-zero with every test passing is not, and raises."""
    done = run(tree, ["snforge", "test", *args], sandbox, timeout=timeout)
    log = done.stdout + done.stderr
    tests = parse_snforge(log)
    if done.returncode and not failures(tests):
        raise KitError(f"snforge exited {done.returncode} with no failing test\n"
                       + log[-1500:])
    return tests, log


def versions(cwd, sandbox: Sandbox | None = None) -> dict[str, str]:
    """The toolchain a row was measured with, for its provenance."""
    out = {}
    for tool in ("snforge", "scarb"):
        try:
            done = run(cwd, [tool, "--version"], sandbox, timeout=120)
            out[tool] = (done.stdout.strip().splitlines() or [""])[0]
        except (OSError, subprocess.TimeoutExpired):
            out[tool] = "unavailable"
    return out
