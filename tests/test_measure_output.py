"""The measurement display is part of the measurement contract (H2).

`cmd_measure` printed the objective with `,.0f`, so a sub-1 objective -- a
time in ms, a ratio -- rendered as `1`: a display 1000x off that masks
pass/fail against the target. The first real domain hit this on day one
(bench_ms 0.701 printed `objective=1`).
"""
import json

from autoresearch import cli

MEASURE = """\
import json, os, sys, time
record = {
    "schema": "ar-run-1", "id": "fmt0701", "session": os.environ["AR_SESSION"],
    "status": "ok", "started": time.time(), "finished": time.time(),
    "metrics": {"ops": 1.0, "peak": 0.701},
    "config": {}, "provenance": {"source": "display test"},
    "outputs": [], "cost": {}, "notes": "",
}
json.dump(record, sys.stdout)
print()
"""


def test_measure_prints_a_sub1_objective_without_rounding_away(sandbox, capsys):
    """The toy objective is round(ops) * peak = 1 * 0.701 = 0.701; the old
    `,.0f` printed `objective=1`."""
    # cli.main reloads the domain from disk, so the command is overridden by
    # replacing the script, not by mutating the loaded config.
    measure = sandbox.paths.root / "bin" / "measure"
    measure.write_text("#!/usr/bin/env python3\n" + MEASURE)
    measure.chmod(0o755)
    assert cli.main(["--domain", str(sandbox.paths.root), "measure"]) == 0
    out = capsys.readouterr().out
    assert "objective=0.701" in out, out
    assert "objective=1" not in out


def test_a_target_of_millions_is_not_scientific_notation(sandbox, capsys):
    """The same lesson cuts the other way: the naive sub-1 fix (`,.4g`
    everywhere) rendered a 6,000,000 target as `6e+06`."""
    (sandbox.paths.runs / "seed.jsonl").write_text(json.dumps({
        "schema": "ar-run-1", "id": "seed", "session": "seed", "entry": "Q1",
        "status": "ok", "started": 0.0, "finished": 0.0,
        "metrics": {"ops": 1.0, "peak": 0.701},
        "config": {}, "provenance": {"source": "display test"},
        "outputs": [], "cost": {}, "notes": ""}))
    assert cli.main(["--domain", str(sandbox.paths.root), "budget"]) == 0
    out = capsys.readouterr().out
    assert "best objective 0.701 vs target 6,000,000" in out, out
