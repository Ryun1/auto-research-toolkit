# What is known about the toy landscape

Domain knowledge the generator and worker agents read before forming hypotheses.
The equivalent, in a real domain, of the guides that carry measured numbers and
the map of closed directions.

## The knobs

| Knob | Range | Notes |
|---|---|---|
| `unroll` | 1–24 | Trades operation count against width: `ops` falls as `1e6/unroll`, but `peak` pays `2·unroll`. Interior optimum. |
| `width` | any int | Enters `peak` directly. **Validity gate: below 16 the run is `invalid` and does not score.** |
| `fold` | 0/1 | −15% ops, no width cost. |
| `cache` | 0/1 | −5% ops **only when `fold` is on**; costs 8 peak unconditionally. |

## Closed directions

- **`cache` alone is refuted (mechanism).** With `fold=0` it changes `ops` by
  exactly nothing and costs 8 `peak`. This holds for every config, not just the
  ones measured — it is a property of the evaluator, so it does not re-open.
- **`width` below 16 is refuted (mechanism).** It is a validity gate, not a
  trade.

## Open

- Whether `cache` pays *with* `fold`: it saves 5% of ops and costs 8 peak, so it
  turns on the ratio of `ops` to `peak` at the operating point — which is what
  `break_even` computes.
