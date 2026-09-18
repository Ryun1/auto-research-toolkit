"""Out-of-band spend, metered like any other.

A tool the brain never routed (a curator's decision API, a hand-paid rental)
spends campaign money all the same. The ledger is where that spend lands, and
these tests hold the metering rule: unpriced is unknown, never zero, and an
unknown money ceiling refuses new spend instead of reading as free.
"""
import json

import pytest

from autoresearch import budget, usage
from autoresearch.errors import AutoresearchError


def record(config, cost=0.25, **over):
    return usage.append(config, "owner", "jev", "api", cost, "batched suggestion pass", **over)


def test_priced_rows_feed_the_campaign_money_meter(sandbox):
    record(sandbox, cost=0.25)
    record(sandbox, cost=0.5)
    assert budget.recorded_usage(sandbox)["money"] == pytest.approx(0.75)


def test_an_unpriced_row_makes_money_unknown_never_free(sandbox):
    record(sandbox, cost=0.25)
    record(sandbox, cost=None)
    assert budget.recorded_usage(sandbox)["money"] is None
    with pytest.raises(AutoresearchError, match="usage.jsonl"):
        budget.require_money(sandbox, budget.recorded_usage(sandbox)["money"])


def test_reconcile_prices_one_row_by_appending_a_correction(sandbox):
    record(sandbox, cost=0.25)
    unpriced = record(sandbox, cost=None)
    usage.reconcile(sandbox, unpriced["lineno"], 1.5, "owner", "priced from the provider console")
    money, unpriced_rows = usage.effective(sandbox)
    assert money == pytest.approx(1.75) and unpriced_rows == []
    assert budget.recorded_usage(sandbox)["money"] == pytest.approx(1.75)


def test_a_correction_may_not_target_another_correction(sandbox):
    record(sandbox, cost=None)
    usage.append(sandbox, "owner", "jev", usage.CORRECTION, 1.0, "first pricing",
                 correction=1)
    # A second correction aimed at the first must be refused, not silently
    # ignored: the original row stays unpriced either way.
    usage.append(sandbox, "owner", "jev", usage.CORRECTION, 2.0, "pricing the pricing",
                 correction=2)
    with pytest.raises(AutoresearchError, match="itself a correction"):
        usage.rows(sandbox)


def test_rows_refuse_corrupt_lines_that_feed_the_ceiling(sandbox):
    record(sandbox, cost=0.25)
    path = usage.path(sandbox)
    path.write_text(path.read_text() + '{"schema": "ar-usage-1"}\n')
    with pytest.raises(AutoresearchError, match="invalid usage row"):
        usage.rows(sandbox)
    with pytest.raises(AutoresearchError, match="invalid usage row"):
        budget.recorded_usage(sandbox)


def test_a_currency_mismatch_is_refused_not_summed(sandbox):
    record(sandbox, cost=1.0)
    path = usage.path(sandbox)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[0]["currency"] = "eur"
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
    with pytest.raises(AutoresearchError, match="currency"):
        usage.rows(sandbox)


def test_the_ledger_travels_with_the_corpus_state(sandbox):
    assert usage.path(sandbox).parent == sandbox.paths.claims.parent
