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
- ~~Repair aggTrade archive holes from the raw `trades` archive~~ — **done 2026-11-27 (D-032)**,
  option (a) below. Still open: before the hold-out, bootstrap the 9 months and check the
  report for unrepaired holes. Original note (D-026): The raw archive is
  complete and its fills map exactly into aggTrade `[f, l]` ranges. Options: (a) synthesize
  aggTrade-like events for the hole only (new id namespace, flagged), or (b) build all history
  from raw trades (exact per-fill timestamps, removes the minute-boundary residual; ~1.7× the
  download size; live would still be aggTrade, so live/hist parity has to be re-argued).
  **Owner decision 2026-11-26: not now; do it before the 9-month hold-out run.** Until then,
  affected bars/sessions are excluded (D-031). Before the hold-out, also scan the 9 months
  for further holes (`make bootstrap` records them).
- Finer hole exclusion: exclude only the levels whose inputs overlap a hole (for example
  only the prior-week profile and the monthly VWAP) instead of the whole session (D-031
  costs 41% of sessions in the 30-day window). Only worth it if the hole is not repaired.
- **OKX as a second flow source, A/B test against Binance-only** (owner, 2026-11-26; after
  Phase 3). Trades are executed on OKX (D-030). Test whether OKX aggTrades/footprint, alone
  or merged with Binance, improve confirmation, using the same paired design. This needs an
  OKX VenueAdapter, a verify-api for OKX, history from OKX's archive (check availability and
  depth), and a decision on how to merge two venues' aggressor flow.
- Funding cost in the trade model (OKX funding for holds across a funding timestamp).
- **Calibrate slippage on OKX EEA before going live** (owner, 2026-11-27): measure the real
  spread and top-of-book depth of `BTC-USD-SWAP` / `ETH-USD-SWAP` from OKX public market data
  (`/api/v5/market/books`, `/market/ticker`) over a few days at the times signals fire, and
  compare with the 1-bucket default (10 USD BTC, 0.5 USD ETH per side) and the 2/4-tick
  sensitivities (D-033).
- Inverse-contract side effects not in the trade model (D-033): PnL and fees booked in BTC/ETH
  (coin exposure until converted) and interest when a loss is borrowed against USDC collateral.
- Confirm the owner's actual OKX EEA maker/taker rates for these swaps and replace the
  UNCONFIRMED `okx_eu_inverse` placeholder before the hold-out.
- **Required before the hold-out run:** stream the research run (write trades per symbol and
  resolution to parquet and aggregate lazily). The 60-day run peaks at 2.1 GB RSS; the 9-month
  hold-out would not fit in the VPS's 3.8 GB next to the trading bots.
