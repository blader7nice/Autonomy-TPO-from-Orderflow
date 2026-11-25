# Data sources — verification log

Rule: never invent API behavior. Every assumption the code relies on is listed here with
its status:

- **VERIFIED (date, source)**: read in an official source on that date.
- **UNVERIFIED**: a working assumption from prior knowledge. `make verify-api` on the VPS
  must confirm it before any real run. All of these are configurable or isolated in
  `flowlevels.venues.binance`.

## 2026-11-25 verification attempt

- `developers.binance.com` (official USD-M Futures docs) could **not** be read: the host
  does not resolve from the dev container. Nothing from those docs is verified.
- `github.com/binance/binance-public-data` (official, by Binance) **was** read: the README,
  `python/utility.py` and `python/download-aggTrade.py`.

## data.binance.vision (historical)

| Fact | Status |
|---|---|
| USD-M aggTrades path: `data/futures/um/{daily\|monthly}/aggTrades/{SYMBOL}/` | VERIFIED 2026-11-25 (`utility.py` `get_path`) |
| Daily file name: `{SYMBOL}-aggTrades-{YYYY-MM-DD}.zip`; monthly: `{SYMBOL}-aggTrades-{YYYY}-{MM}.zip` | VERIFIED 2026-11-25 (`download-aggTrade.py`) |
| Each zip has a `<file>.CHECKSUM` next to it, checkable with `sha256sum -c` | VERIFIED 2026-11-25 (README) |
| Host is `https://data.binance.vision/` | VERIFIED 2026-11-25 (README example URL, spot) |
| CHECKSUM content format is `<sha256-hex>  <filename>` (sha256sum format) | UNVERIFIED (implied by `sha256sum -c`). The parser only takes the first 64-hex token. |
| aggTrades CSV columns: agg_trade_id, price, quantity, first_trade_id, last_trade_id, transact_time, is_buyer_maker | VERIFIED 2026-11-25 (README table) |
| Whether the CSV has a header row | UNVERIFIED. The parser accepts both (it detects a non-numeric first field). |
| Futures aggTrades timestamps are in ms. (The README's microsecond change from 2025-01-01 applies to **SPOT** only.) | Partly verified: the README sample is ms and the µs note says SPOT. The parser **asserts** ms magnitude (1e12–1e13) and fails loudly otherwise. |
| USD-M 1m klines path `data/futures/um/{daily}/klines/{SYMBOL}/1m/{SYMBOL}-1m-{YYYY-MM-DD}.zip` | Path derived from `get_path`; file name UNVERIFIED (`download-kline.py` not read) |
| Klines CSV columns: open_time, open, high, low, close, volume, close_time, quote_volume, count, taker_buy_volume, taker_buy_quote_volume, ignore | VERIFIED 2026-11-25 (README table) |
| Archived files may be updated later | VERIFIED 2026-11-25 (README) |

## REST (fapi), all UNVERIFIED

| Assumption | Used by |
|---|---|
| Base URL `https://fapi.binance.com` | `Settings.binance_rest_url` |
| `GET /fapi/v1/aggTrades?symbol=&fromId=&limit=` with limit max 1000 | gap backfill |
| Response list of `{a, p, q, f, l, T, m}`, with p and q as strings and T in ms | parser |
| HTTP 429 means rate limit (respect `Retry-After`) and 418 means IP ban. HTTP 451 means a restricted location. | backoff policy |
| `GET /fapi/v1/klines?symbol=&interval=1m` | validation (optional) |

## WebSocket (fstream), all UNVERIFIED

| Assumption | Used by |
|---|---|
| Base `wss://fstream.binance.com`; combined streams at `/stream?streams=a/b/c`, frames `{"stream": ..., "data": {...}}` | `Settings.binance_ws_url`, `LiveSource` |
| Possible newer routed endpoints (`/public`, `/market`) | `make verify-api` must check the change log |
| Stream names `<symbol>@aggTrade`, `<symbol>@forceOrder` and `<symbol>@markPrice@1s`, lower-case symbol | `BinanceStreams` |
| aggTrade payload `{e:"aggTrade", E, s, a, p, q, f, l, T, m}`, where `m=true` means the buyer is the maker, so the **aggressor is the seller** | parser and aggressor mapping |
| forceOrder payload `{e:"forceOrder", E, o:{s, S, o, f, q, p, ap, X, l, z, T}}` | liquidation parser |
| forceOrder pushes at most the latest liquidation per symbol per 1000 ms (a snapshot) | limitation 4.4 |
| The server closes the connection after 24 h. The client reconnects proactively at 23 h (config). | `LiveSource` |
| The server pings every ~3 min and expects a pong within ~10 min. The `websockets` library answers pings automatically. | `LiveSource` |
| Up to 1024 streams per connection, plus an incoming-message limit (10/s) | not hit: 4 to 6 streams, no subscribe messages |

## `make verify-api` (to run on the VPS)

It checks these, and on failure prints exactly what differs:
1. data.binance.vision: download yesterday's BTCUSDT daily aggTrades zip plus its CHECKSUM.
   Check the sha256 and the CHECKSUM format. Check the CSV header or no header, and the
   column count. Check the timestamp unit is ms.
2. The same for the 1m klines daily zip (file name pattern).
3. REST `/fapi/v1/aggTrades` with `fromId` and `limit=1000`: field names, types, and that
   IDs are contiguous.
4. WS: subscribe to the combined `btcusdt@aggTrade/btcusdt@forceOrder` stream for 60 s.
   Check the frame wrapper and field names, that `a` is strictly increasing, and that
   `T` ≤ `E`.
5. Cross-check: REST aggTrades for the WS-observed ID range must equal the WS trades
   (price, qty, T, m).

After running it, record the results here with the date and change the matching rows to
VERIFIED, or fix the code.
