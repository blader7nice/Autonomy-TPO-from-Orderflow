"""Telegram notifications (notifications only) and message formatting.

The bot token is a secret: it is only placed in the request URL, which is never logged (httpx
request logging is silenced by the entry points) and never included in errors we store.
"""

from __future__ import annotations

import datetime as dt
import html
import logging
from pathlib import Path
from typing import Any

import httpx

from flowlevels.paper.store import Store

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 6
API = "https://api.telegram.org"


def quiet_http_logs() -> None:
    """httpx logs full request URLs at INFO; the Telegram URL contains the token."""
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


class Telegram:
    def __init__(
        self,
        token: str | None,
        chat_id: str | None,
        http: httpx.AsyncClient | None = None,
        api: str = API,
    ) -> None:
        self._token = token or None
        self.chat_id = chat_id or None
        self._api = api
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0))

    @property
    def enabled(self) -> bool:
        return bool(self._token and self.chat_id)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def send(self, text: str, photo: str | None = None) -> tuple[bool, str | None]:
        """(ok, error). The error never contains the URL or token."""
        if not self.enabled:
            return False, "telegram not configured"
        base = f"{self._api}/bot{self._token}"
        try:
            if photo and Path(photo).exists() and len(text) <= 1024:
                with open(photo, "rb") as fh:
                    r = await self._http.post(
                        f"{base}/sendPhoto",
                        data={"chat_id": self.chat_id, "caption": text, "parse_mode": "HTML"},
                        files={"photo": ("chart.png", fh.read(), "image/png")},
                    )
            else:
                r = await self._http.post(
                    f"{base}/sendMessage",
                    data={
                        "chat_id": self.chat_id,
                        "text": text[:4096],
                        "parse_mode": "HTML",
                        "disable_web_page_preview": "true",
                    },
                )
        except httpx.HTTPError as exc:
            return False, type(exc).__name__
        if r.status_code != 200:
            try:
                desc = str(r.json().get("description", ""))[:200]
            except ValueError:
                desc = ""
            return False, f"HTTP {r.status_code} {desc}".strip()
        return True, None


async def drain(store: Store, tg: Telegram, pause_s: float = 1.1) -> int:
    """Send pending notifications in order; returns the number sent."""
    import asyncio

    sent = 0
    for n in store.pending_notifications(MAX_ATTEMPTS):
        ok, err = await tg.send(n["text"], n["photo"])
        store.mark_notification(int(n["id"]), ok, err, MAX_ATTEMPTS)
        if ok:
            sent += 1
        else:
            log.warning("telegram send failed (%s): %s", n["kind"], err)
            break  # keep order; retry next loop
        await asyncio.sleep(pause_s)
    return sent


# --------------------------------------------------------------------------- formatting


def _utc(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.UTC).strftime("%Y-%m-%d %H:%M UTC")


def px(x: float | None) -> str:
    if x is None:
        return "–"
    a = abs(x)
    nd = 1 if a >= 1000 else 2 if a >= 10 else 4 if a >= 0.1 else 6
    return f"{x:,.{nd}f}"


def _side(d: int) -> str:
    return "LONG" if d > 0 else "SHORT"


def _head(t: dict[str, Any], what: str) -> str:
    late = " · LATE" if t.get("detected_late") else ""
    return (
        f"<b>{what}</b> · {html.escape(t['symbol'])} <b>{_side(int(t['direction']))}</b>{late}\n"
        f"variant {html.escape(t['variant'])} <code>{html.escape(t['config'])}</code>"
    )


def entry_text(t: dict[str, Any], link: str | None) -> str:
    r = float(t["r_unit"])
    tp2 = px(t["tp2"]) if t.get("tp2") is not None else "–"
    lines = [
        _head(t, "ENTRY"),
        f"Entry {px(t['entry'])} · {_utc(int(t['entry_ts']))}",
        f"Stop {px(t['stop'])} (1R = {r / float(t['entry']) * 1e4:.0f} bps)",
        f"TP1 {px(t['tp1'])} ({float(t['tp1_r']):.2f}R, 50%) · TP2 {tp2}",
        f"POI {html.escape(str(t['pois']))}",
        f"Bias: 4H EMA50 {px(t['ema4'])} · daily EMA50 {px(t['emad'])}",
    ]
    if link:
        lines.append(f'<a href="{html.escape(link)}#/trades/{t["id"]}">Open in dashboard</a>')
    return "\n".join(lines)


def tp1_text(t: dict[str, Any]) -> str:
    return (
        _head(t, "TP1 HIT")
        + f"\n50% closed at {px(t['x1_px'])} ({float(t['tp1_r']):+.2f}R gross) · "
        + _utc(int(t["x1_ts"]))
    )


def exit_text(t: dict[str, Any], link: str | None) -> str:
    held = (int(t["exit_ts"]) - int(t["entry_ts"])) / 3_600_000
    lines = [
        _head(t, "EXIT"),
        f"Result <b>{float(t['net_r']):+.2f}R</b> net ({float(t['net_bps']):+.1f} bps) · "
        f"{html.escape(str(t['exit_kind']))}",
        f"Exits {px(t['x1_px'])} / {px(t['x2_px'])} · entry {px(t['entry'])} · held {held:.1f} h",
    ]
    if link:
        lines.append(f'<a href="{html.escape(link)}#/trades/{t["id"]}">Open in dashboard</a>')
    return "\n".join(lines)


def weekly_text(week: str, rows: list[dict[str, Any]], link: str | None) -> str:
    lines = [f"<b>Paper test · weekly summary {html.escape(week)}</b>"]
    for r in rows:
        s = r["stats"]
        exp = f"{s['exp_r']:+.3f}R" if s["exp_r"] is not None else "–"
        wr = f"{s['win_rate']:.0%}" if s["win_rate"] is not None else "–"
        wk = next((w for w in s["weekly"] if w["week"] == week), None)
        wk_txt = f"{wk['n']:.0f} trades, {wk['sum_r']:+.2f}R" if wk else "no trades"
        lines.append(
            f"<b>{html.escape(r['variant'])}</b> <code>{html.escape(r['key'])}</code>: "
            f"this week {wk_txt} · total {s['n_closed']} closed, win {wr}, exp {exp}, "
            f"cum {s['sum_r']:+.2f}R, open {s['n_open']} · "
            f"{s['n_closed']}/{s['progress']['freeze']}"
        )
    if link:
        lines.append(f'<a href="{html.escape(link)}#/performance">Dashboard</a>')
    return "\n".join(lines)
