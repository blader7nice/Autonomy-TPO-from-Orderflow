# Data sources — verification log

Rule: never invent API behavior. Every assumption the code relies on is listed here with
its status:

- **VERIFIED (date, source)**: read in an official source on that date.
- **UNVERIFIED**: a working assumption from prior knowledge. `make verify-api` on the VPS
  must confirm it before any real run. All of these are configurable or isolated in
  `flowlevels.venues.binance`.

**Status on 2026-11-26: every item below is VERIFIED.** `make verify-api` passed 11/11
checks on the VPS (report `data/reports/verify-api-2026-11-26.json`, not committed).

## 2026-11-25 verification attempt

- `developers.binance.com` (official USD-M Futures docs) could **not** be read: the host
  does not resolve from the dev container. Nothing from those docs is verified.
- `github.com/binance/binance-public-data` (official, by Binance) **was** read: the README,
  `python/utility.py` and `python/download-aggTrade.py`.

## 2026-11-26 verification (VPS, live network)

Sources used:
- **Official docs**: `https://developers.binance.com/en/docs/llms-full.txt` (official full-text
  export of developers.binance.com, retrieved 2026-11-26). Pages read: USD-M
  "Websocket Market Streams / Connect", "Important WebSocket Change Notice", "General Info",
  "Change Log". The per-endpoint HTML pages are behind a JS challenge and could not be read
  with a plain HTTP client. The old `/docs/derivatives/usds-margined-futures/...` paths now
  redirect to `/en/docs/products/derivatives-trading-usds-futures/...`.
- **Official SDK**: `github.com/binance/binance-connector-python` (by Binance, generated from
  its OpenAPI spec; master at 2026-11-26T11:06Z). Used for per-endpoint field descriptions
  and endpoint notes: `clients/derivatives_trading_usds_futures/.../websocket_streams/models/
  aggregate_trade_streams_response.py`, `.../liquidation_order_streams_response.py`,
  `.../rest_api/models/compressed_aggregate_trades_list_response_inner.py`,
  `.../rest_api/api/market_data_api.py`, `.../websocket_streams/websocket_streams.py`.
- **Live probes**: `make verify-api` (`flowlevels.venues.verify`) against the real endpoints.

### Differences from the 2026-11-25 assumptions (fixed in code)

1. **WS base URL.** Since 2026-04-23 the unrouted `wss://fstream.binance.com/stream` and
   `/ws` deliver only `/public` streams (depth, bookTicker). aggTrade, forceOrder and
   markPrice belong to `/market` and are **silent** on the unrouted URL. The connection opens
   and no data arrives, so the original code would have tripped the 60 s watchdog forever.
   Default is now `wss://fstream.binance.com/market`, and settings reject an unrouted
   fstream URL (D-021).
2. **aggTrade payload** has two new fields. `nq` is the "normal quantity without the trades
   involving RPI orders", in WS since 2025-12-31 and also in REST. `st` is the symbol type
   (1 = UM, 2 = CM), WS only. `q` is "quantity with all the market trades", including RPI.
   We keep `q` (D-020).
3. **forceOrder** pushes the **largest** liquidation per symbol per 1000 ms, not the latest
   (change log 2026-04-10). The payload has extra `ps` and `st` fields inside `o`.
4. **REST aggTrades lookback is 48 h** (change log 2026-08-11). An older `fromId` returns HTTP
   400 `{"code":-4166,"msg":"Search window is restricted to recent 2 days only."}`. Backfill
   now raises `RestLookbackExceededError` and logs the gap as unresolved with reason
   `beyond_rest_lookback` (D-022).
5. **REST weight is 20 per aggTrades request** and the IP limit is 2400/min. The old pacing
   (0.05 s) would have used ~24,000/min and got the shared IP banned. The pause is now 1 s
   and there is a used-weight guard (D-022).

## data.binance.vision (historical)

| Fact | Status |
|---|---|
| USD-M aggTrades path: `data/futures/um/{daily\|monthly}/aggTrades/{SYMBOL}/` | VERIFIED 2026-11-25 (`utility.py`), live 2026-11-26 |
| Daily file name: `{SYMBOL}-aggTrades-{YYYY-MM-DD}.zip`; monthly: `{SYMBOL}-aggTrades-{YYYY}-{MM}.zip` | Daily VERIFIED live 2026-11-26. Monthly from `download-aggTrade.py`, not used yet. |
| Each zip has a `<file>.CHECKSUM` next to it | VERIFIED live 2026-11-26 |
| Host is `https://data.binance.vision/` | VERIFIED live 2026-11-26 |
| CHECKSUM content format is `<sha256-hex>  <filename>` (sha256sum format, two spaces) | VERIFIED live 2026-11-26, e.g. `ae35cb79…1bb2  BTCUSDT-aggTrades-2026-11-24.zip`. The sha256 matched the download. |
| aggTrades CSV columns: agg_trade_id, price, quantity, first_trade_id, last_trade_id, transact_time, is_buyer_maker | VERIFIED live 2026-11-26 (header line, 7 columns) |
| The CSV **has a header row** (`agg_trade_id,price,…`) | VERIFIED live 2026-11-26. The parser still accepts both. |
| Futures aggTrades timestamps are in ms | VERIFIED live 2026-11-26: `transact_time` 1795478400047 for 2026-11-24 00:00 UTC. The parser asserts ms. |
| A zip holds exactly one CSV named like the zip | VERIFIED live 2026-11-26 |
| Daily ids are contiguous within a day | VERIFIED live 2026-11-26 (BTCUSDT 2026-11-24: 2,462,147 rows, no gaps) |
| Archive `quantity` equals REST `q` (RPI-inclusive) for the same ids | VERIFIED live 2026-11-26: the last 1000 ids of 2026-11-24 are identical in archive and REST (price, qty, T, m, fill count) |
| USD-M 1m klines file `data/futures/um/daily/klines/{SYMBOL}/1m/{SYMBOL}-1m-{YYYY-MM-DD}.zip` | VERIFIED live 2026-11-26 (1440 rows, with header) |
| Klines CSV columns: open_time, open, high, low, close, volume, close_time, quote_volume, count, taker_buy_volume, taker_buy_quote_volume, ignore | VERIFIED live 2026-11-26 (header line, 12 columns) |
| Archived files may be updated later | VERIFIED 2026-11-25 (README) |

## Aggressor side (`m`)

| Fact | Status |
|---|---|
| `m` = "Is the buyer the market maker?" (WS), "Was the buyer the maker?" (REST); `is_buyer_maker` in CSV. `m=true` means the **aggressor is the SELLER**. | VERIFIED 2026-11-26 (official SDK field descriptions) **and empirically**: on BTCUSDT 2026-11-24 the buyer-aggressor volume under this mapping equals the kline `taker_buy_volume` exactly for the day (117946.8 = 117946.8). The per-minute match rate is 90.8% as mapped and 0.0% flipped. |
| Per-minute residual vs klines | Explained 2026-11-26 (30-day `validate-klines`: see PROGRESS Phase 1 step 3). An aggTrade has one `T` for fills aggregated over up to 100 ms, while klines bucket the individual fills. An aggTrade that straddles a minute boundary therefore lands wholly in one minute. All 245 minutes with a volume mismatch that day had a multi-fill aggTrade in the last 100 ms of that minute or the previous one (base rate for a random minute: 30%). Mismatches come in +x/−x pairs in adjacent minutes. This cannot be fixed from aggTrades. |

## Archive data quality (found by `make validate-klines`, 2026-11-26)

| Fact | Status |
|---|---|
| aggTrade `T` is the time of the **first** fill; the other fills in `[f, l]` are 0–100 ms later | VERIFIED 2026-11-26: every raw fill of BTCUSDT 2026-11-24 (5,901,953) and ETHUSDT 2026-11-16/17, mapped to its aggTrade via `[f, l]`, has fill time − T in [0, 100] ms |
| `l − f + 1` is **not** the number of market fills. `[f, l]` ranges contain trade ids that are not in the raw `trades` archive and not in the kline `count` (consistent with "insurance fund and ADL trades are not aggregated"). Σ(l−f+1) exceeds the kline count by 75–4,143 per day. | VERIFIED 2026-11-26. `fill_count` (D-011) is informational only and is not part of the kline mismatch criterion. |
| The raw `trades` archive (`data/futures/um/daily/trades/{S}/{S}-trades-{D}.zip`, columns `id,price,qty,quote_qty,time,is_buyer_maker`, with header) row count equals the kline `count` exactly | VERIFIED 2026-11-26 (BTCUSDT 2026-10-27: 2,540,264 = 2,540,264) |
| **The aggTrades archive can have time holes with contiguous aggTrade ids.** On 2026-10-27, 00:02:46 → 00:30:05 UTC, there are no aggTrades for BTCUSDT and ETHUSDT. Klines and the raw `trades` archive have normal trading there (BTC: 38,355 fills; missing volume below). The daily and the monthly (regenerated 2026-11-11) aggTrades archives are identical: same rows, same id range. Id-gap detection cannot see this. | VERIFIED 2026-11-26. Detected now by `data_quality.max_trade_silence_s` (D-026). Not repaired: owner decision 2026-11-26 is to keep it flagged and exclude affected bars/sessions (D-031); repair from the raw `trades` archive before the hold-out (BACKLOG). |

Missing volume in the hole: BTC 17.344, ETH 1014.017 (kline volume of the 27 hole minutes plus
the two partial edge minutes).

## REST (fapi)

| Fact | Status |
|---|---|
| Base URL `https://fapi.binance.com` | VERIFIED live 2026-11-26 |
| `GET /fapi/v1/aggTrades?symbol=&fromId=&limit=`; `fromId` inclusive; limit max **1000** | VERIFIED live 2026-11-26: limit=1000 returns 1000 rows starting at `fromId`; limit=1001 returns HTTP 400 `-1130` |
| Response list of `{a, p, q, nq, f, l, T, m}`, with p, q and nq as strings and T in ms | VERIFIED live 2026-11-26 (SDK model and live payload) |
| Only the last **48 h** are queryable. An older `fromId` returns HTTP 400 `-4166` "Search window is restricted to recent 2 days only." | VERIFIED 2026-11-26 (SDK docstring, change log 2026-08-11, live probe) |
| Request weight **20**; IP limit `REQUEST_WEIGHT` **2400 / 1 MINUTE**; header `X-MBX-USED-WEIGHT-1M` | VERIFIED 2026-11-26 (change log, SDK, exchangeInfo `rateLimits`, live header delta 20 per call) |
| HTTP 429 means a rate limit was hit (back off, respect `Retry-After`). 418 means an IP auto-ban after repeated 429s. 403 means the WAF limit was violated. | VERIFIED 2026-11-26 (General Info). 451 (restricted location) is not in the USD-M General Info and is still treated as fatal. |
| Insurance-fund and ADL trades are not in aggTrades; RPI trades are aggregated with no tag | VERIFIED 2026-11-26 (SDK docstring) |
| WS trades equal REST trades for the same ids | VERIFIED live 2026-11-26 (514–1037 trades per run, all identical) |

## WebSocket (fstream)

| Fact | Status |
|---|---|
| Market streams base **`wss://fstream.binance.com/market`**; combined streams at `/market/stream?streams=a/b/c`, frames `{"stream": ..., "data": {...}}` | VERIFIED 2026-11-26 (docs "Connect" and "Important WebSocket Change Notice", live) |
| The unrouted `wss://fstream.binance.com/{ws,stream}` delivers nothing for aggTrade/forceOrder/markPrice (only `/public` streams). Legacy URLs were decommissioned on 2026-04-23. | VERIFIED 2026-11-26 (docs and live: 0 messages in 15 s) |
| Stream names `<symbol>@aggTrade`, `<symbol>@forceOrder`, `!forceOrder@arr` and `<symbol>@markPrice@1s` are all `/market`; symbol lower-case | VERIFIED 2026-11-26 (docs mapping table, live) |
| aggTrade payload `{e:"aggTrade", E, s, a, p, q, nq, f, l, T, m, st}`; update speed 100 ms; same price and taker side aggregated | VERIFIED 2026-11-26 (SDK, live) |
| `T` ≤ `E` and `a` strictly increasing per symbol on one connection | VERIFIED live 2026-11-26 (100% of frames; ids contiguous) |
| forceOrder payload `{e:"forceOrder", E, o:{s, S, o, f, q, p, ap, X, l, z, T, ps, st}}` | VERIFIED 2026-11-26 (SDK, live samples) |
| forceOrder pushes **only the largest** liquidation per symbol per 1000 ms (a snapshot); nothing is pushed if there is none | VERIFIED 2026-11-26 (SDK docstring, change log 2026-04-10) |
| A connection is valid for 24 h. The client reconnects proactively at 23 h (config). | VERIFIED 2026-11-26 (docs "Connect") |
| The server pings every **3 min** and disconnects if no pong arrives within **10 min**. Unsolicited pongs are allowed. The `websockets` library answers pings automatically. | VERIFIED 2026-11-26 (docs). In practice: the 1 h `make live-smoke` run must show no ping-related disconnects. |
| Limits: 10 incoming messages/s, 1024 streams per connection | VERIFIED 2026-11-26 (docs). Not hit: 4 streams, no subscribe messages. |

## `make verify-api`

Checks (all PASS on 2026-11-26):
`vision_aggTrades`, `vision_klines` (path, CHECKSUM format and sha256, header, columns, ms),
`aggressor_m_semantics` (day totals vs kline taker-buy; mapping vs flipped),
`vision_vs_rest` (archive = REST for the same ids), `ws_legacy_unrouted_is_silent`,
`ws_stream` (the /market URL, keys, `T ≤ E`, id order), `ws_forceOrder` (via
`!forceOrder@arr` so a sample arrives within 60 s), `rest_aggTrades` (keys, `fromId`
inclusive, contiguity, weight header), `rest_limits` (limit 1001 rejected, −4166 beyond 48 h,
2400/min), `ws_vs_rest`. It uses about 7 REST requests (~140 weight).

## Execution venue fees (cost model, D-030)

The signals come from Binance USD-M. The owner executes on **OKX perpetual swaps**, so the
default cost model uses OKX fees (`configs/default.yaml` → `costs.fee_profiles`).

### OKX EU execution instruments — confirmed by owner 2026-11-27

The owner trades the perpetuals shown as "BTCUSD" and "ETHUSD" in the OKX EU app, with USDC
as collateral, long and short. These are the **inverse (coin-margined) swaps** below.
`BTC-USDC-SWAP` / `ETH-USDC-SWAP` do not exist: OKX's public instruments endpoint lists no
USDC-settled BTC/ETH swap on www/eea/my.okx.com (checked 2026-11-27; only `USDC-USDT-SWAP`).

| Field | BTC-USD-SWAP | ETH-USD-SWAP |
|---|---|---|
| Contract type (`ctType`) | inverse | inverse |
| Contract value (`ctVal` × `ctMult`) | 100 USD | 10 USD |
| Settlement currency (`settleCcy`) | BTC | ETH |
| Tick size (`tickSz`) | 0.1 USD | 0.01 USD |
| Lot size / min order (`lotSz` / `minSz`) | 0.1 / 0.1 contracts (10 USD) | 1 / 1 contract (10 USD) |
| Max market order (`maxMktSz`) | 49,000 contracts | 300,000 contracts |
| Max leverage (`lever`) | 100 | 100 |
| State | live | live |

Source: `GET https://eea.okx.com/api/v5/public/instruments?instType=SWAP&instId=<id>`
(public, unauthenticated), retrieved 2026-11-27 from the VPS. The www.okx.com endpoint
returns identical specs. Status: **instrument confirmed by owner**, specs VERIFIED from the
official API. Margin in USDC is an account setting (multi-currency / portfolio margin), not
an instrument property, so the API cannot confirm it. Fees for these instruments on the
owner's EEA account: **UNCONFIRMED** (see below).

**ITERATION-5 symbols (public API only, not owner-confirmed).** Same endpoint, queried
2026-11-27 (`instType=SWAP`, all instruments): SOL-USD-SWAP (inverse, 10 USD, tick 0.01, min 1
contract), XRP-USD-SWAP (inverse, 10 USD, tick 0.0001, min 1), DOGE-USD-SWAP (inverse, 10 USD,
tick 0.00001, min 1). **No inverse BNB swap is listed on OKX EEA**; BNB-USDT-SWAP (linear,
0.01 BNB, tick 0.01, min 1) is the execution assumption. Binance USD-M signal ticks
(`/fapi/v1/exchangeInfo`, 2026-11-27): SOL 0.01, BNB 0.01, XRP 0.0001, DOGE 0.00001. Fees: the
same UNCONFIRMED placeholder as BTC/ETH.

**Effect on R/bps (D-033).** An inverse contract with USD notional Q has
PnL_coin = Q·(1/P0 − 1/P1). Converted at the exit price P1, PnL_USD = Q·(P1 − P0)/P0,
which is exactly the PnL of a linear contract with notional Q. Fees are charged on the USD
contract value (paid in coin), so they are the same fraction of notional. **R and bps
results are unaffected.** What remains is outside the per-trade model: realised PnL and fees
are booked in BTC/ETH, not USDC, so they carry coin price exposure until converted, and a
loss larger than the account's coin balance is borrowed against the USDC collateral
(interest). Both are small for intraday holds and are not modelled (BACKLOG).

### OKX fees — VERIFIED 2026-11-26

| Item | Value |
|---|---|
| Tier | Regular user (non-VIP), futures fee schedule (perpetual and expiry futures) |
| Maker | **0.0200%** (0.0002) |
| Taker | **0.0500%** (0.0005) |
| Primary source | OKX official notice "Advance Notice: Adjustments to VIP Tier Thresholds and Futures Fee Rates", published 2026-03-24, effective 2026-04-08 (`https://www.okx.com/en-ae/help/advance-notice-adjustment-to-vip-tier-and-future-fees`). Its futures table has a "Regular user" row: before and after are both maker 0.0200% / taker 0.0500%, for Group 1 ("Top 10 pairs") and Group 2 ("All other pairs"); thresholds are in USD. |
| Still current | OKX notice "Advance Notice: Spot and Futures Trading Fee Adjustment", published 2026-08-24, effective 2026-09-09 (`https://www.okx.com/en-us/help/advance-notice-spot-and-futures-trading-fee-adjustment-090926`). It changes only VIP 7 and VIP 8 and lists the regular tier at 0.0200% / 0.0500%. That page is the OKX Singapore version (SGD thresholds). |
| Retrieved | 2026-11-26, from the VPS |

Caveats:
- **The fee page itself (`okx.com/fees`) could not be read as text.** Its table loads with
  JavaScript, and the server-rendered state only contains the Spot tab. Calling the page's
  internal (undocumented) fee-table endpoint directly was not done. The numbers above come
  from OKX's official notices, not from the live fee table. Recheck on `okx.com/fees`
  (Futures tab) when logged in. OKX says the logged-out table is not tied to any account,
  and the logged-in "My trading fees" shows your actual rate.
- The notices do not say whether USDT-margined, USDC-margined and coin-margined swaps
  differ for the regular tier. Some third-party summaries list USDC-settled maker 0.018%.
  For USDT-margined BTC/ETH swaps we use the futures schedule above.
- **Region:** from the VPS (geolocated in Germany) `okx.com/fees` redirects to the EEA site
  (`/en-eu/fees`), which has **no Futures tab** (Spot, Options, Spreads, Events only). The
  UK site (`/en-gb`) has none either. The global sites (`/en-ae`, `/en-sg`) do have one. If
  the trading account is an OKX EEA account, check that perpetual swaps are available to it
  and at which fees.
- Funding payments are not part of the fee profile (BACKLOG/Phase 3 cost model).

### Binance USD-M fees — UNVERIFIED

`binance_regular`: maker 0.0200%, taker 0.0500% (VIP 0, no BNB discount). This is prior
knowledge, not checked against Binance's fee page in this session. It is kept only as the
alternative profile.
