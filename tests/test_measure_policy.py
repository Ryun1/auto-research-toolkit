"""`ar measure` must refuse a human-only measurement command, not run it."""
from unittest import mock

from autoresearch import cli


def test_measure_refuses_human_only_command(sandbox, store):
    config = sandbox.paths.root / "domain.toml"
    text = config.read_text().replace(
        'measure      = "bin/measure"', 'measure      = "yukon run --track pinning"')
    text = text.replace("[policy]", '[policy]\n\n[[policy.human_only]]\n'
                        'pattern = "yukon run"\nreason  = "submission gate stand-in"')
    config.write_text(text)
    with mock.patch("autoresearch.cli.subprocess.run") as executed:
        assert cli.main(["--domain", str(sandbox.paths.root), "measure"]) == 2
    executed.assert_not_called()
