# Backlog (out of scope for the MVP)

- Other venues and multi-exchange aggregation (Bybit, OKX, Coinbase). The VenueAdapter
  interface is ready for them.
- Paid data vendors (Tardis.dev), including full liquidation history and L2 history.
- Options-market levels (Deribit: max pain, gamma levels).
- L2 orderbook features (spoofing, pulling, icebergs).
- Price-action pattern recognition.
- ML over detector features (only after the rule-based score shows an edge).
- More symbols.
- Automatic trade execution.
- Microsecond-resolution sub-minute footprint (the sequence of trades inside a minute).
- Single-print (TPO) detection and flagging.
- **Repair aggTrade archive holes from the raw `trades` archive** (D-026). The raw archive is
  complete and its fills map exactly into aggTrade `[f, l]` ranges. Options: (a) synthesize
  aggTrade-like events for the hole only (new id namespace, flagged), or (b) build all history
  from raw trades (exact per-fill timestamps, removes the minute-boundary residual; ~1.7× the
  download size; live would still be aggTrade, so live/hist parity has to be re-argued).
  Needs an owner decision.
