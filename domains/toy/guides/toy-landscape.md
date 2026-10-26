# What is known about the toy landscape

Domain knowledge the generator and worker agents read before forming hypotheses.
The equivalent, in a real domain, of the guides that carry measured numbers and
the map of closed directions.

## The knobs

| Knob | Range | Notes |
|---|---|---|
| `unroll` | ≥ 1 (1–24 by convention) | Trades operation count against width: `ops` is `1e6/unroll + 5000·unroll`, so it falls until `unroll` ≈ 14 and rises after, and `peak` pays `2·unroll` throughout. Interior optimum. Nothing enforces the upper bound; `bin/measure` clamps only below, to 1. |
| `width` | any int | Enters `peak` directly. **Validity gate: below 16 the run is `invalid` and does not score.** |
| `fold` | 0/1 | −15% ops, no width cost. |
| `cache` | 0/1 | −5% ops **only when `fold` is on**; costs 8 peak unconditionally. |

## Known dead ends

Prior knowledge from whoever wrote this guide, read off `bin/measure` rather
than closed in the record: the store ships empty, so no entry backs these and
`ar rank` excludes nothing on their account. Each is what a `mechanism`
refutation would say if someone filed and measured it.

- **`cache` alone does not pay.** With `fold=0` it leaves `ops` untouched
  (beyond the ±0.2% per-config jitter) and costs 8 `peak`. This holds for every
  config, not just the ones measured — it is a property of the evaluator.
- **`width` below 16 does not trade.** It is a validity gate: the run comes
  back `invalid` and does not score.

## Open

- Whether `cache` pays *with* `fold`: it saves 5% of ops and costs 8 peak, so it
  turns on the ratio of `ops` to `peak` at the operating point — which is what
  `break_even` computes.
