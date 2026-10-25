"""#46: reads that re-scanned the corpus on every call.

Measured on a qsb-sized corpus (1,500 entries, 13,000 rows): every frontier
read took ~50 s, because `exclusion_reason` re-loaded the owning entry from
disk per row; `reapable()` took ~3 s for 50 stale claims, rescanning the
store and ledger per holder; and `[commands] benchmark_revision` ran as a
subprocess on every board, budget, close and loop read, failing them all
when it failed.
"""
import pytest

from autoresearch import epochs, runs
from autoresearch.config import DomainConfig
from autoresearch.entries import Store
from autoresearch.errors import AutoresearchError
from autoresearch.runs import RunRecord
from conftest import make_entry


def test_a_frontier_read_parses_each_entry_once(sandbox, store, monkeypatch):
    make_entry(store, "Q1")
    rows = [RunRecord(id=f"r{n}", metrics={"ops": 1.0 + n, "peak": 1.0},
                      session="s", entry="Q1", provenance={"host": "x"})
            for n in range(50)]
    loads = []
    real = Store.load
    monkeypatch.setattr(Store, "load", lambda self, i: loads.append(i) or real(self, i))
    runs.frontier(store, rows, sandbox.goal)
    assert loads == []          # read once through read_all, never per row


def _counting(sandbox, body):
    script = sandbox.paths.root / "bin/bench-rev"
    count = sandbox.paths.root / "count"
    script.write_text(f"#!/bin/sh\necho x >> {count}\n{body}\n")
    script.chmod(0o755)
    toml = sandbox.paths.root / "domain.toml"
    toml.write_text(toml.read_text().replace(
        "[commands]\n", '[commands]\nbenchmark_revision = "bin/bench-rev"\n'))
    return DomainConfig.load(sandbox.paths.root), script, count


def test_the_benchmark_revision_is_not_rerun_on_every_read(sandbox):
    config, _, count = _counting(sandbox, "echo v1")
    epochs._REVISIONS.clear()
    assert [epochs.benchmark_revision(config) for _ in range(5)] == ["v1"] * 5
    assert count.read_text().count("x") == 1


def test_a_failing_revision_command_falls_back_to_the_last_value(sandbox, capsys):
    config, script, _ = _counting(sandbox, "echo v1")
    epochs._REVISIONS.clear()
    assert epochs.benchmark_revision(config) == "v1"
    script.write_text("#!/bin/sh\nexit 3\n")
    epochs._REVISIONS.clear()             # past the TTL
    assert epochs.benchmark_revision(config) == "v1"
    assert "last known" in capsys.readouterr().err


def test_with_no_last_value_a_failure_is_still_an_error(sandbox):
    config, _, _ = _counting(sandbox, "exit 3")
    epochs._REVISIONS.clear()
    with pytest.raises(AutoresearchError, match="benchmark_revision"):
        epochs.benchmark_revision(config)
