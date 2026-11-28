# Deploying the paper test (VPS)

Two services, both run as user `flow` from the repo's virtualenv. No exchange keys, no
orders. Commands that need root are run by the owner; nothing in this repo runs `sudo`.

| unit | what | listens |
|---|---|---|
| `flowlevels-paper` | paper engine: polls Binance 1m klines every minute, runs the research code, writes the SQLite ledger, sends Telegram alerts | nothing (outbound HTTPS only) |
| `flowlevels-web` | read-only dashboard + heartbeat watchdog | `127.0.0.1:8080` only |

The dashboard is reached through **Tailscale** (`tailscale serve`, private to your tailnet).
There is no public port. Never use `tailscale funnel` for it.

## 1. `.env` (repo root, never committed)

Edit `/home/flow/Autonomy-TPO-from-Orderflow/.env` (template: `.env.example`) and set:

```
FLOWLEVELS_TELEGRAM_BOT_TOKEN=<token from @BotFather>
FLOWLEVELS_TELEGRAM_CHAT_ID=<your chat id>
FLOWLEVELS_DASHBOARD_URL=https://jarvis-trading-bot.<your-tailnet>.ts.net/
```

Leave the other keys as they are (`FLOWLEVELS_API_HOST=127.0.0.1`, `FLOWLEVELS_API_PORT=8080`).

- Bot token: Telegram → @BotFather → `/newbot` (or `/token` for an existing bot).
- Chat id: send any message to your bot, then open
  `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and copy
  `result[0].message.chat.id` (a number; negative for groups).
- Dashboard URL: printed by `tailscale serve status` in step 3.

Check it (sends one message, never prints the token):

```bash
cd /home/flow/Autonomy-TPO-from-Orderflow && .venv/bin/flowlevels telegram-test
```

`.env` is read at start: restart both services after editing it
(`sudo systemctl restart flowlevels-paper flowlevels-web`).

## 2. systemd units (root)

```bash
cd /home/flow/Autonomy-TPO-from-Orderflow
sudo install -m 0644 deploy/systemd/flowlevels-paper.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/flowlevels-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now flowlevels-paper.service flowlevels-web.service
systemctl status flowlevels-paper flowlevels-web --no-pager
```

**The first start of `flowlevels-paper` is the official paper start** (DECISIONS D-047). The
ledgers begin at that minute.

Logs (add `flow` to group `systemd-journal` once, `sudo usermod -aG systemd-journal flow`, to
read them without sudo):

```bash
journalctl -u flowlevels-paper -f
journalctl -u flowlevels-web -f
```

Caps (the VPS is shared with the bots): engine `Nice=10`, `MemoryMax=1200M`, `CPUQuota=150%`;
web `MemoryMax=400M`, `CPUQuota=50%`. Measured in the smoke run: see PROGRESS.md. Both units
are sandboxed (`ProtectSystem=strict`, write access only to `data/`). `myralabs.service` and
the bots are not touched.

Stop / update:

```bash
sudo systemctl stop flowlevels-paper flowlevels-web      # SIGTERM: flushes and exits cleanly
git pull && uv sync --frozen                              # as flow, in the repo
sudo systemctl start flowlevels-paper flowlevels-web
```

A code update that changes trading rules creates a new rule version with a new ledger
(`docs/PAPER_TEST.md`). Restarts are safe: the engine backfills missed minutes over REST and
marks trades found that way as LATE.

## 3. Tailscale (root)

On the server:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up                 # prints a login URL: open it, log in with your account
sudo tailscale set --operator=flow   # optional: lets user flow run 'tailscale serve' later
```

In the Tailscale admin console (https://login.tailscale.com/admin/dns): turn on **MagicDNS**
and **HTTPS Certificates** (needed once for the `https://…ts.net` URL).

Then publish the dashboard inside the tailnet only:

```bash
sudo tailscale serve --bg 8080
tailscale serve status            # shows the URL, e.g. https://jarvis-trading-bot.<tailnet>.ts.net
```

`tailscale serve` proxies `https://jarvis-trading-bot.<tailnet>.ts.net/` → `127.0.0.1:8080`
(WebSocket included). It is only reachable from devices logged in to your tailnet. The first
request can take a few seconds while the certificate is issued. To remove it:
`sudo tailscale serve reset`.

Tailscale needs no inbound firewall rule; your SSH access is unchanged.

## 4. Phone

1. Install the **Tailscale** app (iOS / Android) and log in with the same account.
2. Open `https://jarvis-trading-bot.<tailnet>.ts.net/` (the exact name is in
   `tailscale serve status`; MagicDNS lowercases the hostname).
3. Optional: browser menu → "Add to Home Screen".

## Health and alerts

- Telegram: every entry, TP1 and exit; weekly summary on Sunday 20:00 Europe/Warsaw; alerts
  when data stops for 5 minutes, when disk use passes 70 %, and (from the web service's
  watchdog) when the engine heartbeat is older than 3 minutes, plus a "running again" message.
- systemd restarts either service 10–15 s after a crash.
- Dashboard → Health shows lag per symbol, heartbeat, disk, notification queue and events.
- Dashboard → Health → *Independent cross-check* (every 15 min): engine values vs a separate
  implementation, stored candles vs Binance REST; Telegram alert when it starts failing. From
  the shell: `.venv/bin/flowlevels verify` (0 = pass, 1 = mismatch). The web service makes
  outbound HTTPS calls to Binance for this and for the chart readout (REST weight ≤ 2 per symbol
  per minute while a chart is open).
