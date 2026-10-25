"""#45: records this core writes carry fields an older core cannot read.

`claim.token_sha` (#33), `derived_from` (#23) and `result.pin` (#40) are new
record fields. A core that predates one read it as a raw `TypeError` from
`Claim(**...)` -- or, for an unknown entry key, a refusal that never said
why. Unknown fields stay a refusal (an older core that dropped `token_sha`
would grant a claim by name again), but the refusal now says what it is,
and `ar validate` names the `min_core` floor the records need.
"""
import pytest
import yaml

from autoresearch import validate
from autoresearch.errors import SchemaError
from conftest import make_entry


def _with(store, entry_id, mutate):
    path = store.path(entry_id)
    data = yaml.safe_load(path.read_text())
    mutate(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    from autoresearch.entries import _ENTRY_CACHE
    _ENTRY_CACHE.clear()


@pytest.mark.parametrize("where", ["entry", "claim", "result", "event"])
def test_a_field_from_a_newer_core_is_named_not_a_traceback(sandbox, store, where):
    from conftest import close
    entry = make_entry(store, "Q1")
    if where == "result":
        close(sandbox, store, entry)
    elif where == "claim":
        from autoresearch.claims import Claims
        Claims(store, sandbox, "a").claim("Q1", why="t")
    else:
        entry.history.append(__import__("autoresearch.entries", fromlist=["Event"])
                             .Event("2026-01-01T00:00:00+00:00", "x", "a"))
        store.save(entry)

    def mutate(data):
        target = {"entry": data, "claim": data.get("claim"),
                  "result": data.get("result"),
                  "event": (data.get("history") or [{}])[-1]}[where]
        target["from_the_future"] = 1

    _with(store, "Q1", mutate)
    with pytest.raises(SchemaError) as exc:
        store.load("Q1")
    message = str(exc.value)
    assert "from_the_future" in message and "newer core" in message


def test_validate_names_the_floor_records_with_new_fields_need(sandbox, store):
    from autoresearch.claims import Claims
    make_entry(store, "Q1")
    Claims(store, sandbox, "a").claim("Q1", why="t")        # writes token_sha
    [warning] = validate.record_floor_warnings(sandbox)
    assert "token_sha" in warning and 'min_core = "0.11.0"' in warning


def test_a_floor_at_or_above_the_new_fields_is_quiet(sandbox, store):
    from autoresearch.claims import Claims
    from autoresearch.config import DomainConfig
    toml = sandbox.paths.root / "domain.toml"
    toml.write_text(toml.read_text().replace('ref  = "main"',
                                             'ref  = "main"\nmin_core = "0.11.0"'))
    config = DomainConfig.load(sandbox.paths.root)
    make_entry(store, "Q1")
    Claims(store, config, "a").claim("Q1", why="t")
    assert validate.record_floor_warnings(config) == []


def test_records_without_new_fields_need_no_floor(sandbox, store):
    make_entry(store, "Q1")
    assert validate.record_floor_warnings(sandbox) == []
