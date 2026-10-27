"""Kits: one source in core, vendored into each domain, hashed as evaluator.

The property that matters is that a declared kit's copy scores runs exactly as
`bin/measure` does: editing it must move `evaluator_sha` and must count as
drift in a candidate, the same as editing the script.
"""
import ast
import subprocess
import sys

import pytest

from autoresearch import cli, evaluator, kits
from autoresearch.config import DomainConfig
from autoresearch.errors import ConfigError


def declare(sandbox, body='kits = ["cairo"]'):
    toml = sandbox.paths.root / "domain.toml"
    toml.write_text(toml.read_text() + f"\n[evaluator]\n{body}\n")
    return DomainConfig.load(sandbox.paths.root)


def ar(config, *args):
    return cli.main(["--domain", str(config.paths.root), *args])


def test_sync_vendors_the_kit_and_the_evaluator_hashes_it(sandbox, capsys):
    config = declare(sandbox)
    assert ar(config, "kit", "sync") == 0
    copy = config.paths.root / "bin" / "cairokit.py"
    assert copy.read_bytes() == kits.source("cairo").read_bytes()
    assert evaluator.files(config) == ["bin/measure", "bin/cairokit.py"]

    before = evaluator.identity(config)
    copy.write_text(copy.read_text() + "\nMAX_CASM_FELTS = 10**9\n")
    assert evaluator.identity(config) != before      # an edited kit is a new evaluator


def test_declared_files_still_gain_the_kit(sandbox):
    config = declare(sandbox, 'files = ["bin/measure"]\nkits = ["cairo"]')
    assert evaluator.files(config) == ["bin/measure", "bin/cairokit.py"]


def test_validate_refuses_a_missing_copy_and_warns_on_a_stale_one(sandbox, capsys):
    config = declare(sandbox)
    ar(config, "validate")
    assert "bin/cairokit.py is missing; run `ar kit sync`" in capsys.readouterr().out

    ar(config, "kit", "sync")
    capsys.readouterr()
    ar(config, "validate")
    out = capsys.readouterr().out
    assert "cairokit" not in out

    copy = config.paths.root / "bin" / "cairokit.py"
    copy.write_text(copy.read_text() + "\n# held\n")
    ar(config, "validate")
    assert "differs from the cairo kit this core ships" in capsys.readouterr().out
    ar(config, "kit", "list")
    assert "differs from this core" in capsys.readouterr().out


def test_an_unknown_kit_is_refused_at_load(sandbox):
    with pytest.raises(ConfigError, match=r"kits \['rust'\] unknown"):
        declare(sandbox, 'kits = ["rust"]')


def test_sync_without_a_declaration_says_how_to_declare(sandbox, capsys):
    assert ar(sandbox, "kit", "sync") == 2
    assert "[evaluator]" in capsys.readouterr().err


def test_a_kit_does_not_hide_a_measure_command_with_no_script(sandbox):
    """evaluator_warnings fires when nothing protects the measure script; a
    declared kit is not the script and must not silence it."""
    from autoresearch import validate as validate_mod
    toml = sandbox.paths.root / "domain.toml"
    text = toml.read_text()
    assert 'measure      = "bin/measure"' in text
    toml.write_text(text.replace('measure      = "bin/measure"',
                                 'measure      = "python3 -m bench"'))
    config = declare(sandbox)
    assert validate_mod.evaluator_warnings(config)


@pytest.mark.parametrize("name", sorted(kits.KITS))
def test_a_kit_imports_only_the_standard_library(name):
    """A vendored kit runs under whatever python3 runs bin/measure, where
    neither autoresearch nor any third-party package is installed."""
    tree = ast.parse(kits.source(name).read_text())
    roots = {alias.name.split(".")[0] for node in ast.walk(tree)
             if isinstance(node, ast.Import) for alias in node.names}
    roots |= {node.module.split(".")[0] for node in ast.walk(tree)
              if isinstance(node, ast.ImportFrom) and node.module and not node.level}
    assert roots <= set(sys.stdlib_module_names), roots - set(sys.stdlib_module_names)


@pytest.mark.parametrize("name", sorted(kits.KITS))
def test_a_vendored_kit_loads_beside_a_script_in_isolated_mode(name, tmp_path):
    copy = tmp_path / "bin" / f"{name}kit.py"
    copy.parent.mkdir()
    copy.write_bytes(kits.source(name).read_bytes())
    done = subprocess.run(
        [sys.executable, "-I", "-c",
         f"import sys; sys.path.insert(0, {str(copy.parent)!r}); "
         f"import {name}kit; print({name}kit.KIT)"],
        capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == name
