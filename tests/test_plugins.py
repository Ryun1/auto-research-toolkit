"""Tests for the declared CLI plugin seam (src/autoresearch/plugins.py)."""

import argparse
import textwrap

import pytest

from autoresearch import plugins
from autoresearch.errors import ConfigError


def _write_plugin(root, name, source):
    pkg = root / name
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(textwrap.dedent(source))


def _stub_package_source():
    return '''
        COMMANDS = []

        def register_cli(subparsers, config):
            sub = subparsers.add_parser("hello", help="plugin verb")
            sub.add_argument("--name", default="world")
            sub.set_defaults(func=lambda args: None)
            COMMANDS.append("hello")
    '''


def _make_parser():
    ap = argparse.ArgumentParser(prog="ar")
    sub = ap.add_subparsers(dest="cmd", required=True)
    return ap, sub


def test_plugin_command_is_registered_and_parsable(tmp_path):
    _write_plugin(tmp_path, "stubplug", _stub_package_source())
    ap, sub = _make_parser()
    added = plugins.register(sub, ["stubplug"], tmp_path, config=None)
    assert added == 1
    args = ap.parse_args(["hello", "--name", "core"])
    assert args.cmd == "hello"
    assert args.name == "core"


def test_missing_module_raises_config_error_naming_it(tmp_path):
    ap, sub = _make_parser()
    with pytest.raises(ConfigError, match="no_such_plugin"):
        plugins.register(sub, ["no_such_plugin"], tmp_path, config=None)


def test_module_without_register_cli_raises_config_error(tmp_path):
    _write_plugin(tmp_path, "emptyplug", "X = 1\n")
    ap, sub = _make_parser()
    with pytest.raises(ConfigError, match="emptyplug.*register_cli"):
        plugins.register(sub, ["emptyplug"], tmp_path, config=None)


def test_reregister_skips_existing_command_and_counts_new_only(tmp_path):
    _write_plugin(tmp_path, "stubplug", _stub_package_source())
    ap, sub = _make_parser()
    assert plugins.register(sub, ["stubplug"], tmp_path, config=None) == 1
    # Same plugin again: "hello" already exists, nothing new, no duplicate.
    assert plugins.register(sub, ["stubplug"], tmp_path, config=None) == 0
    args = ap.parse_args(["hello"])
    assert args.cmd == "hello"
    # A fresh plugin alongside an existing command counts only its own.
    _write_plugin(tmp_path, "secondplug", '''
        def register_cli(subparsers, config):
            subparsers.add_parser("second")
    ''')
    assert plugins.register(
        sub, ["stubplug", "secondplug"], tmp_path, config=None) == 1


def test_empty_names_returns_zero_without_importing(tmp_path, monkeypatch):
    import importlib

    def fail(name, *args, **kwargs):
        raise AssertionError(f"import_module called for {name!r}")

    monkeypatch.setattr(importlib, "import_module", fail)
    ap, sub = _make_parser()
    assert plugins.register(sub, [], tmp_path, config=None) == 0
