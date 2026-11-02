"""An entry id resolves to its owning domain across the workspace (#65)."""
import shutil

import pytest

from autoresearch import members
from autoresearch.config import DomainConfig
from autoresearch.entries import Store
from autoresearch.errors import ConfigError, SchemaError
from conftest import make_entry


def _load(root):
    return DomainConfig.load(root)


def test_resolve_finds_an_entry_in_a_sibling(umbrella):
    sibling = _load(umbrella / "domains" / "b")
    make_entry(Store(sibling.paths.entries), "C1", track="research")
    owner, entry = members.resolve(_load(umbrella / "domains" / "a"), "C1")
    assert owner.paths.root == sibling.paths.root
    assert entry.id == "C1"


def test_resolve_from_the_umbrella(umbrella):
    member = _load(umbrella / "domains" / "a")
    make_entry(Store(member.paths.entries), "A1", track="research")
    owner, entry = members.resolve(_load(umbrella), "A1")
    assert owner.paths.root == member.paths.root and entry.id == "A1"


def test_resolve_unowned_id_is_an_error(umbrella):
    with pytest.raises(ConfigError, match=r"no domain in this workspace owns "
                                          r"entry id 'X1'; prefixes are \["):
        members.resolve(_load(umbrella), "X1")


def test_resolve_missing_file_is_the_owners_schema_error(umbrella):
    with pytest.raises(SchemaError, match="no entry 'C9'"):
        members.resolve(_load(umbrella / "domains" / "a"), "C9")


def test_owner_of_does_not_read_stores(umbrella):
    shutil.rmtree(umbrella / "domains" / "b" / "state" / "entries")
    workspace = members.Workspace.load(umbrella)
    path, owner = workspace.owner_of("C1")
    assert path == "domains/b" and owner.name == "b"
    assert workspace.entry("C1") is None
    assert workspace.owner_of("X1") is None


def test_workspace_or_none_on_a_broken_umbrella(umbrella):
    member = _load(umbrella / "domains" / "a")
    (umbrella / "domains" / "b" / "domain.toml").write_text("[domain]\n")
    assert members.workspace_or_none(member) is None


def test_workspace_or_none_for_a_member(umbrella):
    workspace = members.workspace_or_none(_load(umbrella / "domains" / "a"))
    assert [p for p, _ in workspace.domains()] == [".", "domains/a", "domains/b"]
