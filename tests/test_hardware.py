"""Hardware awareness: know the host, refuse what it cannot do, and never
invent a speedup."""
import pytest

from autoresearch.errors import ConfigError
from autoresearch.hardware import (GPU, Host, Requirement, Throughput, check,
                                   detect)


def m2(**over):
    data = dict(os="Darwin", arch="arm64", chip="Apple M2", vendor="apple",
                cpu_threads=8, performance_cores=4, efficiency_cores=4,
                memory_gb=16.0, memory_available_gb=12.0,
                gpu=GPU(backend="metal", name="Apple M2", cores=10,
                        memory_gb=16.0, unified=True))
    data.update(over)
    return Host(**data)


def test_detect_reports_this_machine_without_raising():
    host = detect()
    assert host.cpu_threads >= 1
    assert host.describe()


def test_fingerprint_identifies_the_machine():
    assert m2().fingerprint == "Apple-M2/8t/16g"
    assert m2(chip="Apple M5", cpu_threads=10).fingerprint == "Apple-M5/10t/16g"


def test_apple_silicon_reports_heterogeneous_cores_and_unified_memory():
    text = m2().describe()
    assert "4P + 4E" in text
    assert "unified memory" in text and "competes with the CPU" in text


def test_battery_and_thermal_state_are_flagged():
    assert "sustained throughput will be lower" in m2(on_battery=True).describe()
    assert "THROTTLED" in m2(thermal_throttled=True).describe()


def test_memory_bounds_concurrency_before_the_run_not_after():
    """The shape of the real case: a workload costing 3.90 GB of state at
    512-way could not reach the concurrency its GPU needed on a 16 GB machine,
    and the truncated sweep was published as a 3.6x penalty before being
    retired. The point is that the ceiling is computable in advance."""
    per_unit = 3.90 / 512                       # ~7.6 MB per concurrent unit
    roomy = m2(memory_available_gb=12.0).max_concurrency(per_unit)
    tight = m2(memory_available_gb=12.0).max_concurrency(per_unit, base_gb=8.0)
    assert roomy > tight, "fixed overhead must reduce the ceiling"
    assert tight < 1024, tight                  # 1,024-way refused, as measured
    assert m2(memory_available_gb=1.0).max_concurrency(per_unit, base_gb=8.0) == 0


def test_a_host_that_cannot_hold_one_unit_fails_the_capability_check():
    cap = check(m2(memory_available_gb=2.0),
                Requirement(name="wide", base_memory_gb=4.0, gb_per_unit=0.5))
    assert not cap.ok and "cannot hold even one" in cap.problems[0]


def test_capability_failure_names_every_reason():
    host = m2(memory_gb=4.0, memory_available_gb=3.0, cpu_threads=2,
              gpu=GPU(backend="none"))
    cap = check(host, Requirement(name="big", min_memory_gb=16, min_threads=8,
                                  needs_gpu="cuda"))
    assert not cap.ok and len(cap.problems) == 3
    assert "FAIL" in cap.report()


def test_unified_memory_is_named_when_gpu_memory_is_short():
    cap = check(m2(), Requirement(name="x", min_gpu_memory_gb=64))
    assert any("unified" in p for p in cap.problems)


def test_metal_host_does_not_satisfy_a_cuda_requirement():
    cap = check(m2(), Requirement(name="x", needs_gpu="cuda"))
    assert not cap.ok and "cuda" in cap.problems[0]


def test_unknown_requirement_key_is_refused():
    with pytest.raises(ConfigError, match="unknown key"):
        Requirement.from_dict("x", {"min_memoryy_gb": 4})


# -- the rule the corpus paid for ------------------------------------------

def test_throughput_cannot_exist_without_machine_and_concurrency():
    with pytest.raises(ConfigError, match="concurrency"):
        Throughput(value=100, unit="cand", machine="m2", concurrency=0)
    with pytest.raises(ConfigError, match="machine"):
        Throughput(value=100, unit="cand", machine="", concurrency=8)


def test_ratio_refuses_a_mismatched_concurrency():
    """Comparing a capacity-truncated sweep against an uncapped one is how a
    3.6x penalty was published and later retired."""
    a = Throughput(2166, "op", "Apple-M2/8t/16g", 2048, workload="walk")
    b = Throughput(801, "op", "Apple-M2/8t/16g", 512, workload="walk")
    with pytest.raises(ConfigError, match="different concurrency"):
        a.ratio_to(b)
    assert a.ratio_to(b, allow_mismatch=True) == pytest.approx(2166 / 801)


def test_ratio_refuses_different_workloads():
    a = Throughput(100, "op", "m", 8, workload="walk")
    b = Throughput(50, "op", "m", 8, workload="keccak")
    with pytest.raises(ConfigError, match="different workloads"):
        a.ratio_to(b)


def test_label_names_both_facts_and_marks_a_lower_bound():
    t = Throughput(1932, "op", "Apple-M5/10t/16g", 512, workload="walk",
                   lower_bound=True)
    label = t.label()
    assert "512-way" in label and "Apple-M5" in label and "LOWER BOUND" in label


# -- the loop refuses work this machine cannot run --------------------------

def test_ranking_excludes_an_entry_the_host_cannot_run(sandbox, store):
    """Refuse before claiming, not after measuring."""
    from conftest import make_entry
    from autoresearch.rank import rank
    sandbox.hardware["wide"] = Requirement(name="wide", min_memory_gb=1024)
    make_entry(store, "Q1", impact=1.0, hardware="wide")
    make_entry(store, "Q2", impact=0.1)
    result = rank(store.all(), sandbox, host=m2())
    assert [s.entry_id for s in result.scored] == ["Q2"]
    assert "host cannot run 'wide'" in result.excluded[0].excluded


def test_ranking_keeps_an_entry_the_host_can_run(sandbox, store):
    from conftest import make_entry
    from autoresearch.rank import rank
    sandbox.hardware["ok"] = Requirement(name="ok", min_memory_gb=1)
    make_entry(store, "Q1", impact=1.0, hardware="ok")
    assert [s.entry_id for s in rank(store.all(), sandbox, host=m2()).scored] == ["Q1"]


def test_an_unknown_hardware_class_does_not_silently_exclude(sandbox, store):
    """A typo in `hardware:` must not read as 'this machine cannot run it' --
    that would quietly remove an entry from every future iteration."""
    from conftest import make_entry
    from autoresearch.rank import rank
    make_entry(store, "Q1", impact=1.0, hardware="typo-not-declared")
    assert [s.entry_id for s in rank(store.all(), sandbox, host=m2()).scored] == ["Q1"]


def test_validate_catches_an_undeclared_hardware_class(sandbox, store):
    """Ranking treats an unknown class as no-requirement -- the permissive
    direction -- so the typo has to be caught here or the gate silently does
    not exist."""
    import subprocess, sys
    from conftest import ROOT, make_entry
    from autoresearch import render
    make_entry(store, "Q1", hardware="typo")
    render.write_views(sandbox, store.all())
    out = subprocess.run(
        [sys.executable, "-m", "autoresearch.cli", "--domain",
         str(sandbox.paths.root), "validate"], cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 1
    assert "hardware class 'typo' is not declared" in out.stdout
