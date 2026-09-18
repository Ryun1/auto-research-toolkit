"""Declared CLI plugins.

A plugin is an importable module that exposes ``register_cli(subparsers,
config)`` and adds its own subcommands to the ``ar`` parser. Its verbs run
through the same policy engine and record as every core verb because they
must call core functions to do their work -- the core cannot enforce that; it
can only refuse modules that do not declare the contract.

This is the declared form of what qsbtools already does by reaching into
``cli.build_parser()`` directly: the same orderliness, but the domain says so
in ``domain.toml`` (``plugins = ["name"]``) and the core loads it.
"""

import argparse
import importlib
import sys
from types import ModuleType

from .errors import ConfigError


def load(names, root) -> list[ModuleType]:
    """Import plugin modules from ``names``, searching ``root`` first.

    ``root`` is prepended to sys.path (idempotently) so a domain's plugins
    resolve before installed packages. Returns the imported modules in order.
    """
    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    modules = []
    for name in names:
        try:
            module = importlib.import_module(name)
        except ImportError as exc:
            raise ConfigError(
                f"plugin {name!r} could not be imported from {root_str}: {exc}"
            ) from exc
        if not callable(getattr(module, "register_cli", None)):
            raise ConfigError(
                f"plugin module {name!r} does not expose "
                f"register_cli(subparsers, config)")
        modules.append(module)
    return modules


def register(subparsers, names, root, config) -> int:
    """Load plugins and call each ``register_cli(subparsers, config)``.

    Returns the number of NEW commands added. A plugin command whose name is
    already present on the subparsers action is skipped, so re-registering
    (the parser is rebuilt after domain discovery and argv re-parsed) is
    safe. Empty names loads nothing and returns 0.
    """
    if not names:
        return 0
    before = set(subparsers.choices)
    for module in load(names, root):
        try:
            module.register_cli(subparsers, config)
        except argparse.ArgumentError as exc:
            # Re-register safety: the parent rebuilds the parser after domain
            # discovery and re-parses argv, so a command name already present
            # on the action is skipped rather than fatal.
            if not any(name in exc.message for name in before):
                raise
    return len(set(subparsers.choices) - before)
