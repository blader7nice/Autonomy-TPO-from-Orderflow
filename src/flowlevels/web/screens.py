"""Design QA: Playwright screenshots of every dashboard page at desktop (1440 px) and mobile
(390 px). Usage: `uv run python -m flowlevels.web.screens --url http://127.0.0.1:8090 --out
docs/screens`. Chromium comes from PLAYWRIGHT_BROWSERS_PATH (installed under the repo's
gitignored .cache/)."""

from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path

from playwright.sync_api import ConsoleMessage, Error, sync_playwright

PAGES = ["chart", "positions", "trades", "trade", "performance", "health"]
SIZES = {"desktop": (1440, 900, 1), "mobile": (390, 844, 2)}


def first_trade_id(url: str) -> int | None:
    with urllib.request.urlopen(f"{url}/api/trades") as r:
        d = json.load(r)
    ts = d.get("trades", [])
    closed = [t for t in ts if t["status"] == "closed" and t["trigger"] == "T2"]
    tid = (closed or ts or [{"id": None}])[0]["id"]
    return None if tid is None else int(tid)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8090")
    ap.add_argument("--out", default="docs/screens")
    ap.add_argument("--prefix", default="")
    ap.add_argument("--pages", default=",".join(PAGES))
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    tid = first_trade_id(a.url)
    with sync_playwright() as pw:
        br = pw.chromium.launch(args=["--disable-gpu", "--font-render-hinting=none"])
        for size, (w, h, dpr) in SIZES.items():
            ctx = br.new_context(viewport={"width": w, "height": h}, device_scale_factor=dpr,
                                 is_mobile=size == "mobile",
                                 has_touch=size == "mobile")  # fmt: skip
            page = ctx.new_page()
            errors: list[str] = []

            def on_error(e: Error, errs: list[str] = errors) -> None:
                errs.append(str(e))

            def on_console(m: ConsoleMessage, errs: list[str] = errors) -> None:
                if m.type == "error":
                    errs.append(m.text)

            page.on("pageerror", on_error)
            page.on("console", on_console)
            for name in a.pages.split(","):
                hash_ = f"#/trades/{tid}" if name == "trade" else f"#/{name}"
                if name == "trade" and tid is None:
                    continue
                page.goto(f"{a.url}/{hash_}")
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(1500)
                fn = out / f"{a.prefix}{size}-{name}.png"
                page.screenshot(path=str(fn))  # what the screen shows
                print(fn)
                if size == "mobile" and name not in ("chart",):
                    # whole page for review; hide the fixed tab bar so it is not stamped mid-page
                    page.add_style_tag(content="#tabbar{display:none!important}")
                    fn = out / f"{a.prefix}{size}-{name}-full.png"
                    page.screenshot(path=str(fn), full_page=True)
                    print(fn)
                    page.reload()
                    page.wait_for_load_state("networkidle")
            if errors:
                print(f"{size}: browser errors: {errors}")
            ctx.close()
        br.close()


if __name__ == "__main__":
    main()
