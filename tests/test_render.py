"""Board's GOAL MET must mean what the goal's stop_when means."""
from autoresearch import cli
from autoresearch.config import DomainConfig
from autoresearch.runs import RunRecord, append


def test_board_goal_met_requires_stop_when(sandbox, store, capsys):
    """qsb's promotion bar is +1% over the target; a board that prints GOAL MET
    below the bar is a second, looser definition of met (H9)."""
    path = sandbox.paths.root / "goal.yaml"
    # Objective round(ops)*peak = 5 vs probed target 6e6: raw distance says
    # met, but a +1% promotion bar (qsb's stop_when shape) does not.
    path.write_text(path.read_text().replace(
        'stop_when: "objective < target"',
        'stop_when: "objective >= target * 1.01"'))
    config = DomainConfig.load(sandbox.paths.root)
    assert config.goal.stop_when
    append(sandbox.paths.runs / "best.jsonl", RunRecord(
        session="worker", metrics={"ops": 5.0, "peak": 1.0, "cost": 50.25},
        status="ok"))
    assert cli.main(["--domain", str(sandbox.paths.root), "board"]) == 0
    out = capsys.readouterr().out
    assert "GOAL MET" not in out, out
    assert "distance -100.00%" in out, out
