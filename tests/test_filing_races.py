"""#48: check-then-file gaps that #21's exclusive create did not close.

Ids can no longer collide, but two concurrent filings could still both pass
a check made before either wrote: defect ingest's `from:<project>/<id>` tag
dedupe and the `tree_max_children` sibling cap. Both now check and create
under one store-wide filing lock. And `save()` no longer creates over a file
this process never read, so a stray save cannot replace an entry `create`
just filed. `derived_from` is held to the same shape as an entry id.
"""
import pytest

from autoresearch import cli, validate
from autoresearch.entries import Entry, Store
from autoresearch.errors import SchemaError
from conftest import make_entry


def _filing_lock(store):
    return store.dir.parent / ".locks" / "filing.lock"


def _spy_create(monkeypatch, store, seen):
    real = Store.create

    def spy(self, entry, renumber=False):
        seen.append(_filing_lock(self).exists())
        return real(self, entry, renumber=renumber)

    monkeypatch.setattr(Store, "create", spy)


def test_branch_filing_checks_and_creates_under_the_filing_lock(
        sandbox, store, monkeypatch):
    make_entry(store, "Q1")
    seen = []
    _spy_create(monkeypatch, store, seen)
    assert cli.main(["--domain", str(sandbox.paths.root), "entry", "new", "b",
                     "--track", "research", "--parent", "Q1", "--kind", "improve",
                     "--why", "x"]) == 0
    assert seen == [True]
    assert not _filing_lock(store).exists()


def test_loop_filing_creates_under_the_filing_lock(sandbox, store, monkeypatch):
    from autoresearch.driver.brain import Role, ScriptedBrain
    from autoresearch.driver.loop import Coordinator
    from test_loop import IDLE
    seen = []
    _spy_create(monkeypatch, store, seen)
    proposal = {"title": "p", "hypothesis": "h", "prediction": "p", "bar": "b",
                "confidence": 0.4, "impact": 0.05, "cost": 2.0,
                "mechanisms": ["m"], "why_filed": "w"}
    Coordinator(sandbox, ScriptedBrain(
        {**IDLE, Role.GENERATOR: {"proposals": [proposal]}})).run(max_iterations=1)
    assert seen and all(seen)


def test_defect_ingest_dedupes_and_creates_under_the_filing_lock(
        sandbox, monkeypatch):
    from autoresearch import defects as defects_mod
    from test_defects import make_bundle
    toml = sandbox.paths.root / "domain.toml"
    toml.write_text(toml.read_text() + '\n[[tracks]]\nid = "field"\nprefix = "F"\n')
    from autoresearch.config import DomainConfig
    config = DomainConfig.load(sandbox.paths.root)
    target = Store(config.paths.entries)
    seen = []
    _spy_create(monkeypatch, target, seen)
    bundle = make_bundle({"id": "H1", "title": "t", "core": "0.1.0",
                          "repro": "ar rank", "observed": "o"})
    filed, _ = defects_mod.ingest_bundle(bundle, target, track=config.tracks["field"])
    assert len(filed) == 1 and seen == [True]


def test_save_does_not_create_over_a_file_this_process_never_read(sandbox, store):
    from autoresearch.entries import _ENTRY_CACHE
    path = store.path("Q1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("id: Q1\ntrack: research\ntitle: theirs\nstatus: queued\n")
    _ENTRY_CACHE.clear()
    with pytest.raises(SchemaError, match="never read"):
        store.save(Entry(id="Q1", track="research", title="mine"))
    assert "theirs" in path.read_text()
    store.save(Entry(id="Q1", track="research", title="mine"), force=True)


@pytest.mark.parametrize("bad", [3, "x/../Q1", "Q 1"])
def test_derived_from_must_be_an_entry_id(bad):
    with pytest.raises(SchemaError, match="derived_from"):
        Entry(id="Q2", track="research", title="t", derived_from=bad)


def test_validate_warns_on_dangling_lineage(sandbox, store):
    make_entry(store, "Q2", parent="Q9", kind="improve")
    make_entry(store, "Q3", derived_from="Q8")
    [warning] = validate.lineage_warnings(sandbox)
    assert "Q2 -> parent Q9" in warning and "Q3 -> derived_from Q8" in warning


def test_intact_lineage_is_quiet(sandbox, store):
    make_entry(store, "Q1")
    make_entry(store, "Q2", parent="Q1", kind="improve")
    make_entry(store, "Q3", derived_from="Q1")
    assert validate.lineage_warnings(sandbox) == []
