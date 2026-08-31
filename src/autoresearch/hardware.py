"""What machine is this, what can it actually do, and when is it not enough.

Three jobs, and the third is the one that pays.

**1. Know the host.** Every measurement is taken on a machine, and a throughput
figure that does not name the machine is not a measurement. Apple Silicon makes
this sharper than usual: cores are heterogeneous (a `4P+4E` M2 does not scale to
8 threads the way 8 identical cores would), memory is *unified* so the GPU
competes with the CPU for the same pool, and sustained clocks depend on thermal
and power state in a way a one-shot benchmark hides.

**2. Refuse work the host cannot do, before it is attempted.** The prior corpus
has the exact case: a probe needed 3.90 GB of state at 512-way concurrency, so a
16 GB M2 could not reach the concurrency its GPU needed and the measurement was
capped rather than slow. The published ratio was then an artefact of the
truncation -- a 3.6x penalty that was later retired as wrong. **A capacity limit
that surfaces as a number rather than as a refusal poisons the record.**

**3. Say when to stop buying local time.** See `escalate.py`.

The rule this module exists to enforce, taken verbatim from that corpus because
it was learned the hard way:

> Every ratio is a function of CONCURRENCY as well as machine -- never quote one
> without naming both.

So `Throughput` cannot be constructed without both, and `Measurement.ratio_to()`
refuses to compare two figures taken at different concurrencies unless the
caller says so explicitly.
"""
from __future__ import annotations

import dataclasses
import json
import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass, field

from .errors import ConfigError

GIB = 1024 ** 3


def _run(*args, timeout=5) -> str:
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _sysctl(key: str) -> str:
    return _run("sysctl", "-n", key)


@dataclass
class GPU:
    backend: str = "none"          # metal | cuda | rocm | none
    name: str = ""
    cores: int | None = None       # Apple GPU cores, or CUDA SM count
    memory_gb: float | None = None
    unified: bool = False          # shares the CPU's pool (all Apple Silicon)

    def describe(self) -> str:
        if self.backend == "none":
            return "no GPU compute backend detected"
        bits = [self.name or self.backend]
        if self.cores:
            bits.append(f"{self.cores} cores")
        if self.memory_gb:
            bits.append(f"{self.memory_gb:.0f} GB"
                        + (" unified" if self.unified else " dedicated"))
        return ", ".join(bits)


@dataclass
class Host:
    os: str = ""
    arch: str = ""
    chip: str = ""                 # "Apple M2", "AMD EPYC 7502", ...
    vendor: str = ""               # apple | intel | amd | unknown
    cpu_threads: int = 0
    performance_cores: int | None = None
    efficiency_cores: int | None = None
    memory_gb: float = 0.0
    memory_available_gb: float | None = None
    gpu: GPU = field(default_factory=GPU)
    on_battery: bool | None = None
    thermal_throttled: bool | None = None
    hostname: str = ""

    # -- derived ---------------------------------------------------------

    @property
    def is_apple_silicon(self) -> bool:
        return self.vendor == "apple" and self.arch == "arm64"

    @property
    def fingerprint(self) -> str:
        """Short, stable identity for a measurement row. Two rows with different
        fingerprints are not comparable without saying so."""
        core = self.chip or f"{self.vendor}-{self.arch}"
        return f"{core}/{self.cpu_threads}t/{self.memory_gb:.0f}g".replace(" ", "-")

    @property
    def recommended_threads(self) -> int:
        """Threads to use by default.

        On heterogeneous Apple Silicon the efficiency cores contribute real but
        much lower throughput, and a compute-bound sweep that spreads across all
        of them can measure *slower* per-thread than one pinned to the P cores.
        The honest default is all threads -- the corpus measured its throughput
        that way -- but the split is reported so a domain can choose.
        """
        return max(1, self.cpu_threads)

    def max_concurrency(self, gb_per_unit: float, base_gb: float = 0.0,
                        headroom: float = 0.75) -> int:
        """How many concurrent units this host's memory allows.

        `base_gb` is the fixed footprint that does not scale with concurrency --
        the loaded stream, the binary, the runtime. Modelling only the per-unit
        cost overestimates what fits, which defeats the purpose: this check
        exists so a capacity limit surfaces as a refusal rather than as a
        silently truncated sweep whose throughput was published and retired.
        """
        if gb_per_unit <= 0:
            raise ConfigError("gb_per_unit must be positive")
        pool = (self.memory_available_gb or self.memory_gb) * headroom - base_gb
        return max(0, int(pool // gb_per_unit))

    def describe(self) -> str:
        cores = f"{self.cpu_threads} threads"
        if self.performance_cores and self.efficiency_cores:
            cores += f" ({self.performance_cores}P + {self.efficiency_cores}E)"
        lines = [
            f"host        {self.hostname or '(unknown)'}",
            f"chip        {self.chip or '(unknown)'}  [{self.vendor} {self.arch}]",
            f"cpu         {cores}",
            f"memory      {self.memory_gb:.1f} GB"
            + (f" ({self.memory_available_gb:.1f} GB available)"
               if self.memory_available_gb is not None else ""),
            f"gpu         {self.gpu.describe()}",
        ]
        if self.on_battery is not None:
            lines.append(f"power       {'battery' if self.on_battery else 'AC'}"
                         + ("  ** sustained throughput will be lower **"
                            if self.on_battery else ""))
        if self.thermal_throttled:
            lines.append("thermal     THROTTLED — any figure taken now is a "
                         "lower bound, not a measurement")
        if self.gpu.unified:
            lines.append("note        unified memory: the GPU competes with the "
                         "CPU for the same pool, so GPU concurrency is bounded "
                         "by total RAM, not by a separate VRAM budget")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


# -- detection -------------------------------------------------------------

def _detect_macos(host: Host) -> None:
    host.chip = _sysctl("machdep.cpu.brand_string")
    host.vendor = "apple" if host.chip.startswith("Apple") else "intel"
    for key, attr in (("hw.perflevel0.logicalcpu", "performance_cores"),
                      ("hw.perflevel1.logicalcpu", "efficiency_cores")):
        value = _sysctl(key)
        if value.isdigit():
            setattr(host, attr, int(value))

    mem = _sysctl("hw.memsize")
    if mem.isdigit():
        host.memory_gb = int(mem) / GIB

    # Free + inactive + speculative pages are what a new allocation can actually
    # take; "free" alone reads catastrophically low on a warm macOS box and
    # would make every capacity check refuse.
    vm = _run("vm_stat")
    if vm:
        size = re.search(r"page size of (\d+)", vm)
        page = int(size.group(1)) if size else 4096
        counts = dict(re.findall(r"^Pages ([\w \-]+):\s+(\d+)\.", vm, re.M))
        usable = sum(int(counts.get(k, 0)) for k in
                     ("free", "inactive", "speculative", "purgeable"))
        if usable:
            host.memory_available_gb = usable * page / GIB

    if host.vendor == "apple":
        host.gpu = GPU(backend="metal", name=host.chip, unified=True,
                       memory_gb=host.memory_gb)
        display = _run("system_profiler", "-json", "SPDisplaysDataType", timeout=20)
        if display:
            try:
                items = json.loads(display).get("SPDisplaysDataType", [])
                if items:
                    cores = items[0].get("sppci_cores")
                    if cores and str(cores).isdigit():
                        host.gpu.cores = int(cores)
                    host.gpu.name = items[0].get("sppci_model") or host.gpu.name
            except ValueError:
                pass

    power = _run("pmset", "-g", "batt")
    if power:
        host.on_battery = "Battery Power" in power
    therm = _run("pmset", "-g", "therm")
    if therm:
        limit = re.search(r"CPU_Speed_Limit\s*=\s*(\d+)", therm)
        if limit:
            host.thermal_throttled = int(limit.group(1)) < 100


def _detect_linux(host: Host) -> None:
    try:
        info = open("/proc/cpuinfo").read()
        model = re.search(r"^model name\s*:\s*(.+)$", info, re.M)
        if model:
            host.chip = model.group(1).strip()
    except OSError:
        pass
    lower = host.chip.lower()
    host.vendor = ("amd" if "amd" in lower else
                   "intel" if "intel" in lower else "unknown")
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemTotal:"):
                host.memory_gb = int(line.split()[1]) * 1024 / GIB
            elif line.startswith("MemAvailable:"):
                host.memory_available_gb = int(line.split()[1]) * 1024 / GIB
    except OSError:
        pass

    if shutil.which("nvidia-smi"):
        out = _run("nvidia-smi",
                   "--query-gpu=name,memory.total", "--format=csv,noheader")
        if out:
            name, _, mem = out.splitlines()[0].partition(",")
            host.gpu = GPU(backend="cuda", name=name.strip(), unified=False,
                           memory_gb=float(re.sub(r"[^\d.]", "", mem) or 0) / 1024)
    elif shutil.which("rocm-smi"):
        host.gpu = GPU(backend="rocm", name="AMD ROCm device")


def detect() -> Host:
    """Inspect this machine. Never raises: an undetectable field stays None, and
    a None is reported as unknown rather than defaulted to something plausible."""
    host = Host(os=platform.system(), arch=platform.machine(),
                cpu_threads=os.cpu_count() or 1,
                hostname=platform.node())
    try:
        if host.os == "Darwin":
            _detect_macos(host)
        elif host.os == "Linux":
            _detect_linux(host)
        else:
            host.chip = platform.processor()
    except Exception:                       # noqa: BLE001 -- detection is best-effort
        pass
    if not host.memory_gb:
        host.memory_gb = 0.0
    return host


# -- throughput, which is never a property of hardware alone ---------------

@dataclass(frozen=True)
class Throughput:
    """A rate, and the two facts without which it means nothing.

    The corpus states the rule in its own guide description: *every ratio is a
    function of CONCURRENCY as well as machine -- never quote one without naming
    both*. It was learned by publishing a 3.6x penalty that turned out to be an
    artefact of comparing a capacity-truncated sweep against an uncapped one.

    So this type will not hold a bare number.
    """
    value: float                    # units per second
    unit: str                       # "candidates", "shots", "ops"
    machine: str                    # a Host.fingerprint
    concurrency: int
    workload: str = ""              # rates for different workloads never compare
    lower_bound: bool = False       # capped while still climbing

    def __post_init__(self):
        if self.concurrency < 1:
            raise ConfigError("throughput needs the concurrency it was taken at")
        if not self.machine:
            raise ConfigError("throughput needs the machine it was taken on")

    def label(self) -> str:
        return (f"{self.value:,.4g} {self.unit}/s @ {self.concurrency}-way "
                f"on {self.machine}"
                + (f" [{self.workload}]" if self.workload else "")
                + ("  LOWER BOUND (still climbing when capped)"
                   if self.lower_bound else ""))

    def ratio_to(self, other: Throughput, allow_mismatch: bool = False) -> float:
        """`self / other`, refusing the comparison that produced a retired figure."""
        if not allow_mismatch:
            if self.concurrency != other.concurrency:
                raise ConfigError(
                    f"refusing to divide {self.label()} by {other.label()}: "
                    f"different concurrency ({self.concurrency} vs "
                    f"{other.concurrency}). Comparing a capacity-truncated "
                    f"sweep against an uncapped one is how a 3.6x penalty was "
                    f"published and later retired. Re-measure at equal "
                    f"concurrency, or pass allow_mismatch=True and say so in "
                    f"the memo.")
            if self.workload != other.workload:
                raise ConfigError(
                    f"refusing to compare different workloads "
                    f"({self.workload!r} vs {other.workload!r})")
        if other.value == 0:
            raise ConfigError("cannot divide by a zero-throughput baseline")
        return self.value / other.value


# -- capability gating -----------------------------------------------------

@dataclass
class Requirement:
    """What one experiment class needs. Declared by the domain, checked by core."""
    name: str = "default"
    min_memory_gb: float = 0.0
    base_memory_gb: float = 0.0     # fixed footprint, independent of concurrency
    gb_per_unit: float = 0.0        # memory cost of ONE concurrent unit
    min_threads: int = 1
    needs_gpu: str = ""             # "", "any", "metal", "cuda"
    min_gpu_memory_gb: float = 0.0

    @classmethod
    def from_dict(cls, name: str, spec: dict) -> Requirement:
        known = {f.name for f in dataclasses.fields(cls)} - {"name"}
        unknown = set(spec) - known
        if unknown:
            raise ConfigError(
                f"hardware requirement {name!r} has unknown key(s) "
                f"{sorted(unknown)}; known: {sorted(known)}")
        return cls(name=name, **spec)


@dataclass
class Capability:
    ok: bool
    requirement: str
    host: str
    problems: list[str] = field(default_factory=list)
    max_concurrency: int | None = None

    def report(self) -> str:
        head = (f"{'OK  ' if self.ok else 'FAIL'} {self.requirement} on {self.host}")
        if self.max_concurrency is not None:
            head += f"  (memory allows {self.max_concurrency}-way concurrency)"
        return "\n".join([head] + [f"  - {p}" for p in self.problems])


def check(host: Host, requirement: Requirement) -> Capability:
    problems = []
    if requirement.min_memory_gb and host.memory_gb < requirement.min_memory_gb:
        problems.append(
            f"needs {requirement.min_memory_gb:.1f} GB, host has "
            f"{host.memory_gb:.1f} GB")
    if host.cpu_threads < requirement.min_threads:
        problems.append(
            f"needs {requirement.min_threads} threads, host has {host.cpu_threads}")
    if requirement.needs_gpu:
        if host.gpu.backend == "none":
            problems.append(f"needs a {requirement.needs_gpu} GPU; host has none")
        elif (requirement.needs_gpu != "any"
              and host.gpu.backend != requirement.needs_gpu):
            problems.append(
                f"needs {requirement.needs_gpu}, host has {host.gpu.backend}")
    if requirement.min_gpu_memory_gb:
        available = host.gpu.memory_gb or 0
        if available < requirement.min_gpu_memory_gb:
            problems.append(
                f"needs {requirement.min_gpu_memory_gb:.1f} GB of GPU memory, "
                f"host has {available:.1f} GB"
                + (" (unified — shared with the CPU)" if host.gpu.unified else ""))

    concurrency = None
    if requirement.gb_per_unit:
        concurrency = host.max_concurrency(requirement.gb_per_unit,
                                           requirement.base_memory_gb)
        if concurrency < 1:
            problems.append(
                f"one unit needs {requirement.gb_per_unit:.3f} GB on top of a "
                f"{requirement.base_memory_gb:.1f} GB base, and the host cannot "
                f"hold even one")
    return Capability(ok=not problems, requirement=requirement.name,
                      host=host.fingerprint, problems=problems,
                      max_concurrency=concurrency)
