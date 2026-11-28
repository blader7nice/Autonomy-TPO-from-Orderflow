// Paper Terminal: single-page dashboard (no build step). Data from the read-only API; live
// refresh when the WebSocket reports a database change.
import { token, precision, baseChart, candles, emaSeries, Bands, poiBand, priceLine, sparkline, PaneLabels, stepTo, valueAt } from "./charts.js";

// ------------------------------------------------------------------ icons
const I = {
  chart: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"><path d="M7 17V7M7 9H5M7 14h2M12 20V4M12 7h-2M12 15h2M17 16V9M17 11h-2M17 14h2"/></svg>',
  positions: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l9 5-9 5-9-5 9-5z"/><path d="M3 13l9 5 9-5"/></svg>',
  trades: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"><path d="M9 6h11M9 12h11M9 18h11M4.5 6h.01M4.5 12h.01M4.5 18h.01"/></svg>',
  performance: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M3 17l6-6 4 4 8-8"/><path d="M15 7h6v6"/></svg>',
  health: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12h4l3-8 4 16 3-8h4"/></svg>',
  check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12l5 5L20 7"/></svg>',
  dash: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"><path d="M7 12h10"/></svg>',
  close: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>',
  inbox: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M4 13l2.5-7h11L20 13v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1v-5z"/><path d="M4 13h5l1 2h4l1-2h5"/></svg>',
  alert: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9L2.4 18a2 2 0 0 0 1.7 3h15.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/></svg>',
};
const PAGES = [
  { id: "chart", label: "Chart" },
  { id: "positions", label: "Positions" },
  { id: "trades", label: "Trades" },
  { id: "performance", label: "Performance" },
  { id: "health", label: "Health" },
];

// ------------------------------------------------------------------ formatting
const MINUS = "−";
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const sgn = (x, nd) => (x == null || !isFinite(x) ? "–" : (x >= 0 ? "+" : MINUS) + Math.abs(x).toFixed(nd));
const fR = (x, nd = 2) => (x == null || !isFinite(x) ? "–" : sgn(x, nd) + "R");
const fBps = (x) => (x == null || !isFinite(x) ? "–" : sgn(x, 1) + " bps");
const fPct = (x, nd = 0) => (x == null || !isFinite(x) ? "–" : (100 * x).toFixed(nd) + "%");
const cls = (x) => (x == null || !isFinite(x) || x === 0 ? "" : x > 0 ? "pos" : "neg");
const short = (s) => String(s || "").replace("USDT", "");
function fPx(symbol, x) {
  if (x == null || !isFinite(x)) return "–";
  const p = precision(symbol, x);
  return x.toLocaleString("en-US", { minimumFractionDigits: p, maximumFractionDigits: p });
}
// Full value for the verification readout: price precision + up to `extra` more decimals
// (trailing zeros dropped), so computed values (EMA, VWAP, TPO mid rows) are shown exactly.
function fExact(symbol, x, extra = 3) {
  if (x == null || !isFinite(x)) return "–";
  const p = precision(symbol, x);
  let [ip, dp = ""] = Math.abs(x).toFixed(p + extra).split(".");
  while (dp.length > p && dp.endsWith("0")) dp = dp.slice(0, -1);
  return (x < 0 ? "−" : "") + Number(ip).toLocaleString("en-US") + (dp ? "." + dp : "");
}
const TF_S = { "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400 };
function fTime(ms, withYear = false) {
  if (!ms) return "–";
  const d = new Date(ms);
  const p = (n) => String(n).padStart(2, "0");
  const s = `${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
  return withYear ? `${d.getUTCFullYear()}-${s} UTC` : s;
}
function fAgo(sec) {
  if (sec == null || !isFinite(sec)) return "–";
  sec = Math.max(0, sec);
  if (sec < 60) return `${Math.round(sec)}s`;
  if (sec < 3600) return `${Math.round(sec / 60)}m`;
  if (sec < 86400) return `${(sec / 3600).toFixed(1)}h`;
  return `${(sec / 86400).toFixed(1)}d`;
}
function fHeld(h) {
  if (h == null || !isFinite(h)) return "–";
  if (h < 1) return `${Math.round(h * 60)}m`;
  if (h < 48) return `${h.toFixed(1)}h`;
  return `${(h / 24).toFixed(1)}d`;
}
const side = (d) => `<span class="badge ${d > 0 ? "long" : "short"}">${d > 0 ? "LONG" : "SHORT"}</span>`;
const vtag = (v, key, color) => `<span class="vtag"><i style="background:${color || "var(--muted)"}"></i>${esc(v)}${key ? ` <code>${esc(key)}</code>` : ""}</span>`;
const exitLabel = (k) => ({ stop: "stop", time: "time stop", "tp1+tp2": "TP1 + TP2", "tp1+stop": "TP1 + stop", "tp1+time": "TP1 + time", "tp1+trail": "TP1 + trail" }[k] || k || "open");
function resultBadge(t) {
  if (t.status === "open") return `<span class="badge info">OPEN</span>`;
  return t.net_r > 0 ? `<span class="badge win">WIN</span>` : `<span class="badge loss">LOSS</span>`;
}
const empty = (title, text, compact = false) => (compact
  ? `<div class="empty compact">${esc(text || title)}</div>`
  : `<div class="empty">${I.inbox}<b>${esc(title)}</b><span>${esc(text)}</span></div>`);
const skel = (h, w = "100%") => `<div class="skel" style="height:${h}px;width:${w}"></div>`;

// ------------------------------------------------------------------ state
const S = {
  summary: null,
  page: null,
  pageId: null,
  ws: "connecting",
  flashes: new Map(),
  lastServerSkew: 0,
};
const LS = {
  get(k, d) { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
};

async function api(path) {
  const r = await fetch(path, { headers: { Accept: "application/json" } });
  if (!r.ok) {
    let msg = `${r.status} ${r.statusText}`;
    try { msg = (await r.json()).detail || msg; } catch { /* not json */ }
    throw new Error(msg);
  }
  return r.json();
}

function flash(root) {
  // value flash on change: elements with data-f="key" and data-v="number"
  root.querySelectorAll("[data-f]").forEach((el) => {
    const k = el.dataset.f, v = parseFloat(el.dataset.v);
    const prev = S.flashes.get(k);
    if (prev != null && isFinite(v) && v !== prev) {
      el.classList.remove("flash-up", "flash-down");
      void el.offsetWidth;
      el.classList.add(v > prev ? "flash-up" : "flash-down");
    }
    if (isFinite(v)) S.flashes.set(k, v);
  });
}

function errorCard(e, retry) {
  const id = "retry-" + Math.random().toString(36).slice(2);
  setTimeout(() => document.getElementById(id)?.addEventListener("click", retry), 0);
  return `<div class="card error-card"><div class="card-head"><span class="warn">${I.alert}</span><h3>Could not load data</h3></div>
    <p class="msg">${esc(e.message || e)}</p><button class="btn" id="${id}">Retry</button></div>`;
}

const demoBanner = () => (S.summary?.demo ? `<div class="demo-banner"><b>DEMO DATA</b> Backtest trades of window B replayed through the paper engine to show a populated dashboard. Not the live paper ledger.</div>` : "");

// ------------------------------------------------------------------ shell
function shell() {
  const links = PAGES.map((p) => `<a href="#/${p.id}" data-page="${p.id}">${I[p.id]}<span>${p.label}</span></a>`).join("");
  document.getElementById("nav").innerHTML = links;
  document.getElementById("tabbar").innerHTML = links;
  document.getElementById("side-foot").innerHTML = `Rules frozen per version · times in UTC<br>Charts by <a href="https://www.tradingview.com/lightweight-charts/" target="_blank" rel="noopener">TradingView Lightweight Charts™</a>`;
}

function statusbar() {
  const s = S.summary;
  const el = document.getElementById("statusbar");
  const page = PAGES.find((p) => p.id === S.pageId)?.label || "";
  if (!s) {
    el.innerHTML = `<div class="title">${page}</div><div class="stat"><span class="dot"></span>Connecting…</div>`;
    return;
  }
  const st = s.engine?.status || "unknown";
  const dot = st === "running" ? "ok" : st === "demo" ? "warn" : st === "unknown" ? "" : "bad";
  const label = { running: "Engine running", stale: "Engine stalled", stopped: "Engine stopped", unknown: "Engine not started", demo: "Demo data" }[st] || st;
  const lag = s.max_lag_s;
  const lagCls = lag == null ? "" : lag <= 90 ? "" : lag <= 300 ? "warn" : "neg";
  const nowMs = Date.now() - S.lastServerSkew;
  const ago = s.last_update_ms ? (nowMs - s.last_update_ms) / 1000 : null;
  const wsWarn = S.ws !== "open" ? `<div class="stat warn"><span class="dot warn"></span>${S.ws === "connecting" ? "Connecting" : "Reconnecting"}…</div>` : "";
  el.innerHTML = `<div class="title">${page}</div>
    <div class="stat"><span class="dot ${dot}"></span><b>${label}</b></div>
    <div class="stat hide-sm">Data lag <b class="num ${lagCls}">${lag == null ? "–" : fAgo(lag)}</b></div>
    <div class="stat">Updated <b class="num" id="ago">${ago == null ? "–" : fAgo(ago) + " ago"}</b></div>
    <div class="stat hide-sm">Open <b class="num">${s.open ?? 0}</b></div>${wsWarn}`;
}

function tickAgo() {
  const s = S.summary, el = document.getElementById("ago");
  if (!s || !el || !s.last_update_ms) return;
  el.textContent = fAgo((Date.now() - S.lastServerSkew - s.last_update_ms) / 1000) + " ago";
}

async function loadSummary() {
  try {
    S.summary = await api("/api/summary");
    S.lastServerSkew = Date.now() - S.summary.server_ms;
  } catch { /* keep previous */ }
  statusbar();
}

// ------------------------------------------------------------------ chart page
const ChartPage = {
  async mount(el) {
    this.el = el;
    this.symbol = LS.get("symbol", "BTCUSDT");
    this.tf = LS.get("tf", "30m");
    const syms = S.summary?.symbols || ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT"];
    if (!syms.includes(this.symbol)) this.symbol = syms[0];
    el.innerHTML = `<div class="page">${demoBanner()}
      <div class="page-head"><h1>Chart</h1>
        <div class="chips sym" id="syms">${syms.map((s) => `<button class="chip${s === this.symbol ? " active" : ""}" data-s="${s}">${short(s)}</button>`).join("")}</div>
        <div class="seg" id="tfs">${["15m", "30m", "1h", "4h"].map((t) => `<button class="${t === this.tf ? "active" : ""}" data-t="${t}">${t}</button>`).join("")}</div>
        <div id="bias-badge">${skel(36, "132px")}</div>
      </div>
      <div class="split">
        <div style="min-width:0"><div class="readout" id="ohlc"></div><div class="chart-wrap"><div class="chart-el" id="chart"></div></div>
          <div class="legend"><span><i style="border-color:${token("--green")}"></i>▲▼ entry (variant)</span><span><i style="border-color:${token("--ema4")}"></i>4H EMA50</span><span><i class="dash" style="border-color:${token("--emad")}"></i>Daily EMA50</span><span><i class="box" style="border-color:${token("--zone-long-edge")};background:${token("--zone-long")}"></i>Demand POI</span><span><i class="box" style="border-color:${token("--zone-short-edge")};background:${token("--zone-short")}"></i>Supply POI</span><span><i class="dash" style="border-color:${token("--level")}"></i>TPO level</span><span>↑↓ value off-scale</span><span><i class="box" style="border-color:${token("--muted")};border-radius:50%;width:10px"></i>skipped setup</span></div>
        </div>
        <div class="grid" id="side">${skel(180)}${skel(120)}</div>
      </div></div>`;
    el.querySelector("#syms").addEventListener("click", (e) => {
      const b = e.target.closest("[data-s]");
      if (!b) return;
      this.symbol = b.dataset.s; LS.set("symbol", this.symbol);
      el.querySelectorAll("#syms .chip").forEach((c) => c.classList.toggle("active", c === b));
      this.fit = true; this.refresh();
    });
    el.querySelector("#tfs").addEventListener("click", (e) => {
      const b = e.target.closest("[data-t]");
      if (!b) return;
      this.tf = b.dataset.t; LS.set("tf", this.tf);
      el.querySelectorAll("#tfs button").forEach((c) => c.classList.toggle("active", c === b));
      this.fit = true; this.refresh();
    });
    this.chart = baseChart(el.querySelector("#chart"));
    this.fit = true;
    this.lines = [];
    this.bands = new Bands();
    this.labels = new PaneLabels();
    this.idx = new Map();
    this.chart.subscribeCrosshairMove((p) => this.ohlc(p));
    await this.refresh();
  },
  paneH() {
    const el = this.el.querySelector("#chart");
    return el && this.chart ? el.clientHeight - this.chart.timeScale().height() : null;
  },
  // Readout of the hovered bar (the newest bar when not hovering): OHLC plus the exact values
  // the rules saw at that bar's close. EMA50s come from the engine payload; the TPO levels of
  // that UTC day and the session VWAP come from /api/chart readout (rule code, same candles).
  ohlc(param) {
    const box = this.el.querySelector("#ohlc");
    const d = this.data;
    if (!box) return;
    if (!d || !d.bars.length) { box.innerHTML = ""; return; }
    let i = d.bars.length - 1;
    if (param && param.time != null && this.idx.has(param.time)) i = this.idx.get(param.time);
    const b = d.bars[i];
    const sym = this.symbol;
    const tc = Math.min(b[0] + TF_S[this.tf], d.overlay.end);
    const f = (x) => fPx(sym, x), fx = (x) => fExact(sym, x);
    const ch = (b[4] - b[1]) / b[1];
    const rd = d.readout || {};
    const lv = (rd.levels || {})[String(Math.floor(b[0] / 86400) * 86400)] || [];
    const e4 = valueAt(d.ema4, tc), ed = valueAt(d.emad, tc);
    const vw = rd.vwap ? rd.vwap[i] : null;
    const H = this.paneH();
    const off = (v) => {
      const y = v == null || !this.cs ? null : this.cs.priceToCoordinate(v);
      if (y == null || !H) return "";
      return y < 0 ? '<span class="off">▲ above chart</span>' : y > H ? '<span class="off">▼ below chart</span>' : "";
    };
    const kv = (k, v, extra = "") => `<span class="kvp"><span class="k">${k}</span>${v}${extra}</span>`;
    const tri = (a, b2, c) => (a == null ? "–" : `${fx(a)} / ${fx(b2)} / ${fx(c)}`);
    box.innerHTML = `<div class="rl"><b>${short(sym)}/USDT · ${this.tf}</b><span class="muted">${fTime(b[0] * 1000)}${i === d.bars.length - 1 ? " · forming" : ""}</span>
        ${kv("O", f(b[1]))}${kv("H", f(b[2]))}${kv("L", f(b[3]))}${kv("C", `<b>${f(b[4])}</b>`)}<span class="${cls(ch)}">${sgn(100 * ch, 2)}%</span></div>
      <div class="rl">${kv("4H EMA50", fx(e4), off(e4))}${kv("D EMA50", fx(ed), off(ed))}${kv("VWAP", fx(vw))}</div>
      <div class="rl">${kv("pd VAH/POC/VAL", tri(lv[0], lv[1], lv[2]))}${kv("pw VAH/POC/VAL", tri(lv[3], lv[4], lv[5]))}</div>`;
  },
  // Initial view: readable bar spacing and free room on the right for the price labels.
  paneW() {
    const el = this.el.querySelector("#chart");
    return el ? el.clientWidth - (this.chart.priceScale("right").width() || 70) : 0;
  },
  view(n) {
    const W = this.paneW();
    if (!W) return;
    const nv = Math.min(n, Math.max(40, Math.floor(W / (W < 560 ? 3.5 : 2.5))));
    const px = this.labels.maxWidth(this.paneH()) + 26; // + leader lines
    const extra = Math.ceil((px * nv) / Math.max(W - px, 80));
    this.chart.timeScale().setVisibleLogicalRange({ from: n - nv - 0.5, to: n - 1 + extra });
    this.fitLabels(3);
  },
  // Keep the newest candle left of the price labels while the latest bars are in view: the
  // labels' width depends on the price scale, which follows the visible bars, so re-measure
  // after drawing and shift the view (zoom unchanged) until it fits.
  fitLabels(tries = 2) {
    requestAnimationFrame(() => requestAnimationFrame(() => {
      if (!this.chart || !this.data) return;
      const ts = this.chart.timeScale(), r = ts.getVisibleLogicalRange(), W = this.paneW();
      const n = this.data.bars.length;
      if (!r || !W || r.to < n - 1) return; // scrolled back in history: leave the view alone
      const bs = W / (r.to - r.from);
      const need = (this.labels.maxWidth(this.paneH()) + 26) / bs;
      const have = r.to - (n - 1);
      if (Math.abs(need - have) < 1) return;
      ts.setVisibleLogicalRange({ from: r.from + need - have, to: r.to + need - have });
      if (tries > 1) this.fitLabels(tries - 1);
    }));
  },
  async refresh() {
    const sym = this.symbol, tf = this.tf;
    let d;
    try {
      d = await api(`/api/chart/${sym}/${tf}`);
    } catch (e) {
      this.el.querySelector("#side").innerHTML = errorCard(e, () => this.refresh());
      return;
    }
    if (sym !== this.symbol || tf !== this.tf) return; // a newer selection is loading
    if (d.empty) {
      this.el.querySelector("#side").innerHTML = `<div class="card">${empty("No chart data yet", "The engine writes the first chart after its first computation.")}</div>`;
      return;
    }
    this.data = d;
    const last = d.bars.length ? d.bars[d.bars.length - 1][4] : null;
    if (!this.cs || this.csSym !== sym) {
      if (this.cs) { this.chart.removeSeries(this.cs); this.chart.removeSeries(this.e4); this.chart.removeSeries(this.ed); }
      this.cs = candles(this.chart, sym, last);
      this.cs.attachPrimitive(this.bands);
      this.cs.attachPrimitive(this.labels);
      this.e4 = emaSeries(this.chart, "4h");
      this.ed = emaSeries(this.chart, "d");
      this.csSym = sym;
      this.lines = [];
    }
    const bars = d.bars.map((b) => ({ time: b[0], open: b[1], high: b[2], low: b[3], close: b[4] }));
    this.cs.setData(bars);
    const times = bars.map((b) => b.time);
    this.idx = new Map(times.map((x, i) => [x, i]));
    const lastT = times[times.length - 1];
    this.e4.setData(stepTo(d.ema4, lastT)); // value in force until the next 4H / daily close
    this.ed.setData(stepTo(d.emad, lastT));
    for (const l of this.lines) this.cs.removePriceLine(l);
    this.lines = [];
    // Lines carry no axis label (it would cover the axis ticks). Names and prices are drawn
    // as pills at the pane's right edge (PaneLabels: merged or offset, never overlapping).
    const items = [];
    for (const lv of d.overlay.levels) {
      this.lines.push(priceLine(this.cs, lv.price, token("--level"), "", 2, false));
      items.push({ price: lv.price, name: lv.name, text: fPx(sym, lv.price), bg: token("--surface-3"), fg: token("--text-2"), lc: token("--level"), group: "tpo", mergePx: 4, prio: 1 });
    }
    const cur = (pts) => (pts.length ? pts[pts.length - 1][1] : null);
    const e4v = cur(d.ema4), edv = cur(d.emad);
    if (e4v != null) items.push({ price: e4v, name: "4H EMA", text: fPx(sym, e4v), bg: token("--ema4"), fg: token("--bg"), clamp: true, prio: 2 });
    if (edv != null) items.push({ price: edv, name: "D EMA", text: fPx(sym, edv), bg: token("--emad"), fg: token("--bg"), clamp: true, prio: 2 });
    for (const p of d.positions) {
      const add = (price, name, color, style) => {
        this.lines.push(priceLine(this.cs, price, color, "", style, false, 1));
        items.push({ price, tag: p.variant, name, text: fPx(sym, price), bg: color, fg: token("--bg"), prio: 3, group: "pos-" + name, exact: true });
      };
      add(p.entry, "entry", token("--text"), 0);
      add(p.stop, "stop", token("--red"), 0);
      if (!p.tp1_hit) add(p.tp1, "TP1", token("--green"), 0);
      if (p.tp2 != null && p.exit2 === "lvl") add(p.tp2, "TP2", token("--green"), 2);
    }
    const lb = d.bars[d.bars.length - 1];
    if (lb) items.push({ price: lb[4], name: "", text: fPx(sym, lb[4]), bg: lb[4] >= lb[1] ? token("--green") : token("--red"), fg: token("--bg"), prio: 9, price_always: true });
    this.labels.set(items, this.el.querySelector("#chart").clientWidth < 560);
    this.bands.set(d.overlay.pois.map(poiBand), times, () => this.labels.drawnWidth ?? this.labels.maxWidth());
    const t0 = times[0] || 0;
    const seenSig = new Set();
    const mk = d.markers.filter((m) => m.time >= t0).filter((m) => {
      if (m.kind !== "signal") return true;
      const k = m.time + ":" + m.position;
      if (seenSig.has(k)) return false;
      seenSig.add(k);
      return true;
    }).map((m) => {
      if (m.kind === "entry") return { time: m.time, position: m.position, shape: m.shape, color: m.dir > 0 ? token("--green") : token("--red"), text: m.variant, size: 1.2 };
      if (m.kind === "exit") return { time: m.time, position: m.position, shape: "circle", color: m.win ? token("--green") : token("--red"), text: "", size: 0.9 };
      return { time: m.time, position: m.position, shape: "circle", color: token("--muted"), text: "", size: 0.5 };
    });
    this.cs.setMarkers(mk);
    if (this.fit) { this.view(bars.length); this.fit = false; } else this.fitLabels();
    this.ohlc(null);
    this.side(d);
  },
  side(d) {
    const ov = d.overlay, b = ov.bias || {};
    const dir = b.dir || 0;
    this.el.querySelector("#bias-badge").innerHTML = `<span class="bias ${dir > 0 ? "long" : dir < 0 ? "short" : ""}">${dir > 0 ? "LONG BIAS" : dir < 0 ? "SHORT BIAS" : "NO BIAS"}</span>`;
    const ck = (ok) => `<span class="check ${ok ? "ok" : "no"}">${ok ? I.check : I.dash}</span>`;
    const want = dir >= 0 ? "long" : "short";
    const checks = (b.checks || []).map((c) => {
      const ok = dir === 0 ? (c.long || c.short) : c[want];
      const s = c.long ? "long" : c.short ? "short" : "flat";
      return `<div class="row">${ck(dir === 0 ? false : ok)}<span class="grow">${esc(c.name)}</span><span class="badge ${s === "long" ? "long" : s === "short" ? "short" : ""}">${s.toUpperCase()}</span></div>`;
    }).join("");
    const vals = b.ready ? `<div class="kv" style="margin-top:16px">
        <div><span class="label">Last</span><span class="v">${fPx(this.symbol, b.last)}</span></div>
        <div><span class="label">4H EMA50</span><span class="v">${fPx(this.symbol, b.ema4)}</span></div>
        <div><span class="label">Daily close</span><span class="v">${fPx(this.symbol, b.close_d)}</span></div>
        <div><span class="label">Daily EMA50</span><span class="v">${fPx(this.symbol, b.emad)}</span></div></div>` : "";
    const pos = d.positions.length
      ? d.positions.map((p) => `<div class="row">${vtag(p.variant, "", p.color)}${side(p.dir)}<span class="grow muted num">${fPx(this.symbol, p.entry)}</span><span class="right num ${cls(p.net_r)}" data-f="cp${p.id}" data-v="${p.net_r}">${fR(p.net_r)}</span></div>`).join("")
      : empty("No open position", `No paper position on ${short(this.symbol)}.`, true);
    const pois = ov.pois.length
      ? ov.pois.slice(0, 8).map((p) => `<div class="row poi-row"><span class="badge ${p.dir > 0 ? "long" : "short"}">${p.dir > 0 ? "DEMAND" : "SUPPLY"}</span><div class="grow"><div class="text-2">${esc(p.kind.replace("_", " "))}${p.state === "testing" ? ' <span class="badge pending testing">TESTING</span>' : ""}</div><div class="num muted px">${fPx(this.symbol, p.near)}${p.near !== p.far ? " – " + fPx(this.symbol, p.far) : ""}</div></div></div>`).join("")
      : empty("No fresh POI", "All POIs in range have had their first test.", true);
    this.el.querySelector("#side").innerHTML = `
      <div class="card"><div class="card-head"><h3>Bias at the last close</h3></div><div class="rows">${checks || empty("Not enough data", "", true)}</div>${vals}</div>
      <div class="card"><div class="card-head"><h3>Open positions</h3><span class="spacer badge">${d.positions.length}</span></div><div class="rows">${pos}</div></div>
      <div class="card"><div class="card-head"><h3>Fresh POIs</h3><span class="spacer muted" style="font-size:12px">nearest first</span></div><div class="rows">${pois}</div></div>`;
    flash(this.el);
  },
  destroy() { this.chart?.remove(); this.chart = null; this.cs = null; },
};

// ------------------------------------------------------------------ positions page
function rail(t) {
  const d = t.direction, r = t.r_unit;
  const R = (x) => (x == null ? null : (d * (x - t.entry)) / r);
  const tp1 = R(t.tp1), tp2 = t.exit2 === "lvl" ? R(t.tp2) : null, cur = R(t.last_px);
  const hi = Math.max(tp1 || 1, tp2 || 0, cur || 0) * 1.08, lo = Math.min(-1, cur || 0) * 1.04;
  const pct = (x) => (100 * (x - lo)) / (hi - lo);
  const mk = (x, col, lab) => (x == null ? "" : `<span class="mk" style="left:${pct(x)}%;background:${col}"><span>${lab}</span></span>`);
  const curCol = (cur || 0) >= 0 ? token("--green") : token("--red");
  return `<div class="rail">${mk(-1, token("--red"), "stop")}${mk(0, token("--text-2"), "entry")}${mk(tp1, token("--green"), t.tp1_hit ? "TP1 ✓" : "TP1")}${mk(tp2, token("--green"), "TP2")}
    ${cur == null ? "" : `<span class="now" style="left:${pct(cur)}%;background:${curCol}" title="now"></span>`}</div>`;
}

function positionCard(t) {
  const sym = t.symbol, f = (x) => fPx(sym, x);
  const second = t.exit2 === "lvl" ? (t.tp2 != null ? f(t.tp2) : "stop/time") : "1.5 ATR trail";
  return `<div class="card tile">
    <div class="tile-top"><div style="display:flex;flex-direction:column;gap:8px;min-width:0">
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap"><b style="font-size:16px">${short(sym)}</b>${side(t.direction)}${t.tp1_hit ? '<span class="badge win">TP1 HIT</span>' : ""}${t.detected_late ? '<span class="badge warn">LATE</span>' : ""}</div>
        <span class="muted num" style="font-size:12px">${fTime(t.entry_ts)} UTC · held ${fHeld(t.held_h)}</span></div>
      <div style="text-align:right;flex:none"><div class="label">If closed now</div><div class="kpi-value ${cls(t.net_r)}" data-f="p${t.id}" data-v="${t.net_r}">${fR(t.net_r)}</div><div class="kpi-sub num">${fBps(t.net_bps)}</div></div></div>
    ${rail(t)}
    <div class="kv">
      <div><span class="label">Entry</span><span class="v">${f(t.entry)}</span></div>
      <div><span class="label">Now</span><span class="v">${f(t.last_px)}</span></div>
      <div><span class="label">Stop</span><span class="v neg">${f(t.stop)}</span></div>
      <div><span class="label">TP1 · ${t.tp1_r != null ? t.tp1_r.toFixed(2) + "R" : ""}</span><span class="v pos">${f(t.tp1)}</span></div>
      <div><span class="label">2nd half</span><span class="v">${second}</span></div>
    </div></div>`;
}

const PositionsPage = {
  async mount(el) {
    this.el = el;
    el.innerHTML = `<div class="page">${demoBanner()}<div class="page-head"><h1>Open positions</h1></div><div class="grid grid-2">${skel(220)}${skel(220)}</div></div>`;
    await this.refresh();
  },
  async refresh() {
    let d;
    try { d = await api("/api/positions"); } catch (e) {
      this.el.querySelector(".page").innerHTML = demoBanner() + errorCard(e, () => this.refresh());
      return;
    }
    const byV = new Map(d.variants.map((v) => [v.id, []]));
    for (const p of d.positions) (byV.get(p.variant) || byV.set(p.variant, []).get(p.variant)).push(p);
    const total = d.positions.reduce((a, p) => a + (p.net_r || 0), 0);
    let html = `${demoBanner()}<div class="page-head"><h1>Open positions</h1><span class="badge">${d.positions.length} open</span><span class="badge ${total > 0 ? "win" : total < 0 ? "loss" : ""}">${fR(total)} unrealized</span></div>`;
    if (!d.variants.length) html += `<div class="card">${empty("Engine not started", "Start flowlevels-paper to create the ledgers.")}</div>`;
    let cols = "";
    for (const v of d.variants) {
      const ps = byV.get(v.id) || [];
      const sum = ps.reduce((a, p) => a + (p.net_r || 0), 0);
      cols += `<section class="vcol"><div class="vhead">${vtag(v.id, v.key, v.color)}<span class="spacer" style="margin-left:auto"></span><span class="muted num" style="font-size:12px">${ps.length} open${ps.length ? " · " + fR(sum) : ""}</span></div>
        ${ps.length ? ps.map(positionCard).join("") : `<div class="card">${empty("", "No open position. Waiting for the next setup.", true)}</div>`}</section>`;
    }
    if (d.variants.length) html += `<div class="vcols">${cols}</div>`;
    this.el.querySelector(".page").innerHTML = html;
    flash(this.el);
  },
  destroy() {},
};

// ------------------------------------------------------------------ trades page + sheet
const TradesPage = {
  async mount(el, arg) {
    this.el = el;
    this.f = { variant: LS.get("tf_v", ""), symbol: LS.get("tf_s", ""), result: LS.get("tf_r", "") };
    const syms = S.summary?.symbols || [];
    const vs = S.summary?.variants || [];
    el.innerHTML = `<div class="page">${demoBanner()}
      <div class="page-head"><h1>Trades</h1>
        <select class="select" id="fv" aria-label="Variant"><option value="">All variants</option>${vs.map((v) => `<option value="${v.id}">${v.id} · ${esc(v.key)}</option>`).join("")}</select>
        <select class="select" id="fs" aria-label="Symbol"><option value="">All symbols</option>${syms.map((s) => `<option value="${s}">${short(s)}</option>`).join("")}</select>
        <div class="seg" id="fr">${[["", "All"], ["win", "Win"], ["loss", "Loss"], ["open", "Open"]].map(([k, l]) => `<button data-r="${k}">${l}</button>`).join("")}</div>
      </div><div class="card" id="list" style="padding:8px 16px">${skel(44)}<div style="height:8px"></div>${skel(44)}<div style="height:8px"></div>${skel(44)}</div></div>`;
    el.querySelector("#fv").value = this.f.variant;
    el.querySelector("#fs").value = this.f.symbol;
    const mark = () => el.querySelectorAll("#fr button").forEach((b) => b.classList.toggle("active", b.dataset.r === this.f.result));
    mark();
    el.querySelector("#fv").addEventListener("change", (e) => { this.f.variant = e.target.value; LS.set("tf_v", this.f.variant); this.refresh(); });
    el.querySelector("#fs").addEventListener("change", (e) => { this.f.symbol = e.target.value; LS.set("tf_s", this.f.symbol); this.refresh(); });
    el.querySelector("#fr").addEventListener("click", (e) => {
      const b = e.target.closest("[data-r]"); if (!b) return;
      this.f.result = b.dataset.r; LS.set("tf_r", this.f.result); mark(); this.refresh();
    });
    el.querySelector("#list").addEventListener("click", (e) => {
      const r = e.target.closest("[data-id]");
      if (r) location.hash = `#/trades/${r.dataset.id}`;
    });
    await this.refresh();
    if (arg) Sheet.open(arg);
  },
  async refresh() {
    const q = new URLSearchParams(this.f).toString();
    let d;
    try { d = await api(`/api/trades?${q}`); } catch (e) {
      this.el.querySelector("#list").innerHTML = errorCard(e, () => this.refresh());
      return;
    }
    const list = this.el.querySelector("#list");
    if (!d.trades.length) {
      const any = this.f.variant || this.f.symbol || this.f.result;
      list.innerHTML = any ? empty("No trades match these filters", "Clear a filter to see more.") : empty("No trades yet", "Waiting for the first setup. Entries appear here within about a minute.");
      return;
    }
    const shown = d.trades.slice(0, this.limit || 40);
    const rows = shown.map((t) => {
      const f = (x) => fPx(t.symbol, x);
      return `<tr class="click" data-id="${t.id}"><td class="num">${fTime(t.entry_ts)}</td><td>${vtag(t.variant, "", t.color)}</td><td><b>${short(t.symbol)}</b></td><td>${side(t.direction)}</td>
        <td class="muted">${t.trigger}${t.pattern && t.trigger === "T2" ? " · " + esc(t.pattern) : ""}</td><td class="r num">${f(t.entry)}</td><td class="r num">${f(t.stop)}</td><td class="r num">${f(t.tp1)}</td>
        <td class="text-2">${t.status === "open" ? (t.tp1_hit ? "TP1 hit, open" : "open") : exitLabel(t.exit_kind)}</td><td class="r num">${fHeld(t.held_h)}</td>
        <td class="r num ${cls(t.net_r)}" data-f="t${t.id}" data-v="${t.net_r}"><b>${fR(t.net_r)}</b></td><td class="r num muted">${fBps(t.net_bps)}</td><td>${resultBadge(t)}${t.detected_late ? ' <span class="badge warn">LATE</span>' : ""}</td></tr>`;
    }).join("");
    const cards = shown.map((t) => `<div class="tcard" data-id="${t.id}"><div class="tcard-top"><b>${short(t.symbol)}</b>${side(t.direction)}${vtag(t.variant, "", t.color)}<span class="tcard-r ${cls(t.net_r)}">${fR(t.net_r)}</span></div>
      <div class="tcard-sub"><span>${fTime(t.entry_ts)} UTC</span><span>${t.status === "open" ? (t.tp1_hit ? "TP1 hit, open" : "open") : exitLabel(t.exit_kind)}</span><span>${fHeld(t.held_h)}</span><span>${fBps(t.net_bps)}</span>${resultBadge(t)}${t.detected_late ? '<span class="badge warn">LATE</span>' : ""}</div></div>`).join("");
    list.innerHTML = `<div class="only-desktop table-wrap" style="padding-top:12px"><table><thead><tr><th>Entry (UTC)</th><th>Var.</th><th>Symbol</th><th>Side</th><th>Trigger</th><th class="r">Entry</th><th class="r">Stop</th><th class="r">TP1</th><th>Exit</th><th class="r">Held</th><th class="r">Net R</th><th class="r">Net bps</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>
      <div class="only-mobile" style="margin:0 -16px">${cards}</div>
      ${d.trades.length > shown.length ? `<div class="more"><button class="btn" id="more">Show ${Math.min(40, d.trades.length - shown.length)} more · ${d.trades.length - shown.length} hidden</button></div>` : ""}`;
    list.querySelector("#more")?.addEventListener("click", (e) => { e.stopPropagation(); this.limit = (this.limit || 40) + 40; this.refresh(); });
    flash(list);
  },
  destroy() { Sheet.close(true); },
};

const Sheet = {
  async open(id) {
    const sh = document.getElementById("sheet"), sc = document.getElementById("scrim");
    sh.hidden = false; sc.hidden = false;
    sh.innerHTML = `<div class="sheet-head"><div class="grab"></div><b>Trade #${esc(id)}</b><span class="spacer" style="margin-left:auto"></span><button class="icon-btn" id="sheet-close" aria-label="Close">${I.close}</button></div><div class="sheet-body">${skel(320)}${skel(120)}</div>`;
    requestAnimationFrame(() => { sh.classList.add("open"); sc.classList.add("open"); });
    const close = () => { location.hash = "#/trades"; };
    sh.querySelector("#sheet-close").onclick = close;
    sc.onclick = close;
    this.id = id;
    await this.render(id);
  },
  async render(id) {
    const sh = document.getElementById("sheet");
    let t;
    try { t = await api(`/api/trades/${id}`); } catch (e) {
      sh.querySelector(".sheet-body").innerHTML = errorCard(e, () => this.render(id));
      return;
    }
    if (this.id !== id) return;
    const f = (x) => fPx(t.symbol, x);
    const head = sh.querySelector(".sheet-head");
    head.innerHTML = `<div class="grab"></div><b style="font-size:16px">${short(t.symbol)}</b>${side(t.direction)}${vtag(t.variant, t.config, t.color)}${resultBadge(t)}<span style="margin-left:auto"></span><button class="icon-btn" id="sheet-close" aria-label="Close">${I.close}</button>`;
    head.querySelector("#sheet-close").onclick = () => { location.hash = "#/trades"; };
    const second = t.exit2 === "lvl" ? (t.tp2 != null ? f(t.tp2) : "none → stop/time") : "1.5 × ATR trail";
    const reasons = t.reasons.map((r) => `<li><span class="check ${r.ok ? "ok" : "no"}">${r.ok ? I.check : I.dash}</span><span class="rule">${esc(r.rule)}</span><span class="txt num">${esc(r.text)}</span></li>`).join("");
    sh.querySelector(".sheet-body").innerHTML = `
      <div class="readout compact"><div class="rl"><b>${short(t.symbol)}/USDT · ${t.chart?.tf || ""}</b><span class="muted">entry ${fTime(t.entry_ts, true)}</span></div></div>
      <div class="trade-chart"><div class="chart-el" id="tchart"></div></div>
      <div class="legend" style="padding-top:0;margin-top:-8px"><span><i class="box" style="border-color:${token("--amber")};background:${token("--amber")}"></i>${t.trigger === "T1" ? "sweep, close-back" : "touch, " + esc(t.pattern || "confirmation")} (30m)</span><span>${t.direction > 0 ? "▲" : "▼"} entry</span><span>● exits (x1 half, x2 rest)</span><span><i class="box" style="border-color:${token(t.direction > 0 ? "--zone-long-edge" : "--zone-short-edge")};background:${token(t.direction > 0 ? "--zone-long" : "--zone-short")}"></i>POI</span></div>
      <div class="card"><div class="kv">
        <div><span class="label">Net result</span><span class="v ${cls(t.net_r)}"><b>${fR(t.net_r)}</b></span></div>
        <div><span class="label">Net bps</span><span class="v ${cls(t.net_bps)}">${fBps(t.net_bps)}</span></div>
        <div><span class="label">Gross / cost</span><span class="v">${fR(t.gross_r)} / ${t.cost_r != null ? t.cost_r.toFixed(2) + "R" : "–"}</span></div>
        <div><span class="label">Entry</span><span class="v">${f(t.entry)}</span></div>
        <div><span class="label">Stop · 1R</span><span class="v">${f(t.stop)} · ${((t.r_unit / t.entry) * 1e4).toFixed(0)} bps</span></div>
        <div><span class="label">TP1 · ${t.tp1_r?.toFixed(2)}R</span><span class="v">${f(t.tp1)}</span></div>
        <div><span class="label">2nd half</span><span class="v">${second}</span></div>
        <div><span class="label">Exit 1</span><span class="v">${t.x1_px != null ? f(t.x1_px) + " · " + fTime(t.x1_ts) : "–"}</span></div>
        <div><span class="label">Exit 2</span><span class="v">${t.x2_px != null ? f(t.x2_px) + " · " + fTime(t.x2_ts) : "open"}</span></div>
        <div><span class="label">Exit</span><span class="v">${t.status === "open" ? "open" : exitLabel(t.exit_kind)}</span></div>
        <div><span class="label">Held</span><span class="v">${fHeld(t.held_h)}</span></div>
        <div><span class="label">MFE / MAE</span><span class="v">${t.mfe_r?.toFixed(2)}R / ${t.mae_r?.toFixed(2)}R</span></div>
        <div><span class="label">Maker entry</span><span class="v">${fR(t.net_r_maker)}</span></div>
        <div><span class="label">OKX 2 / 4 tick</span><span class="v">${fR(t.net_r_2tick)} / ${fR(t.net_r_4tick)}</span></div>
      </div></div>
      <div class="card"><div class="card-head"><h3>Why it was taken</h3><span class="spacer muted" style="font-size:12px">rules version ${esc(t.version)}</span></div><ul class="reasons">${reasons}</ul></div>
      <div class="muted" style="font-size:12px">Detected ${fTime(t.detected_ts, true)}${t.detected_late ? " (late: found after downtime from backfilled data)" : ""}. ${t.has_png ? `<a href="/api/trades/${t.id}/png" target="_blank" rel="noopener">Static chart (PNG)</a>` : ""}</div>`;
    this.chart(t);
  },
  chart(t) {
    const c = t.chart;
    const el = document.getElementById("tchart");
    if (!c || !el) return;
    if (this.ch) this.ch.remove();
    const ch = baseChart(el);
    this.ch = ch;
    const bars = c.bars.map((b) => ({ time: b[0], open: b[1], high: b[2], low: b[3], close: b[4] }));
    const s = candles(ch, t.symbol, t.entry);
    s.applyOptions({ priceLineVisible: false }); // a past trade: no "last price" line
    s.setData(bars);
    const e4 = emaSeries(ch, "4h"), ed = emaSeries(ch, "d");
    const ts = bars.map((b) => b.time);
    e4.setData(stepTo(c.ema4, ts[ts.length - 1]));
    ed.setData(stepTo(c.emad, ts[ts.length - 1]));
    const bands = new Bands();
    s.attachPrimitive(bands);
    const labels = new PaneLabels();
    s.attachPrimitive(labels);
    const items = [];
    const add = (price, name, color, style, prio = 3) => {
      priceLine(s, price, color, "", style, false, 1);
      items.push({ price, name, text: fPx(t.symbol, price), bg: color, fg: token("--bg"), prio });
    };
    add(t.entry, "entry", token("--text"), 0);
    add(t.stop, "stop", token("--red"), 0);
    add(t.tp1, "TP1", token("--green"), 0);
    if (t.tp2 != null && t.exit2 === "lvl") add(t.tp2, "TP2", token("--green"), 2);
    if (c.swing != null && t.trigger === "T1") add(c.swing, "15m swing", token("--muted"), 1, 1);
    for (const [pts, name, col] of [[c.ema4, "4H EMA", "--ema4"], [c.emad, "D EMA", "--emad"]]) {
      const v = valueAt(pts, ts[ts.length - 1]);
      if (v != null) items.push({ price: v, name, text: fPx(t.symbol, v), bg: token(col), fg: token("--bg"), clamp: true, prio: 2 });
    }
    labels.set(items, el.clientWidth < 560);
    bands.set(c.pois.map((p) => ({ ...poiBand({ ...p, dir: t.direction, state: "fresh", from: c.a_ts - 6 * 1800, to: null }), label: "" })), ts);
    const step = c.tf === "15m" ? 900 : 1800;
    const snap = (ms) => Math.floor(ms / 1000 / step) * step;
    const d = t.direction;
    const mk = [
      { time: c.a_ts, position: d > 0 ? "belowBar" : "aboveBar", shape: "square", color: token("--amber"), text: "", size: 0.7 },
      { time: c.b_ts, position: d > 0 ? "belowBar" : "aboveBar", shape: "square", color: token("--amber"), text: "", size: 0.7 },
      { time: snap(t.entry_ts), position: d > 0 ? "belowBar" : "aboveBar", shape: d > 0 ? "arrowUp" : "arrowDown", color: d > 0 ? token("--green") : token("--red"), text: "", size: 1.3 },
    ];
    if (t.x1_ts) mk.push({ time: snap(t.x1_ts), position: d > 0 ? "aboveBar" : "belowBar", shape: "circle", color: t.x1_px != null && d * (t.x1_px - t.entry) > 0 ? token("--green") : token("--red"), text: "x1", size: 0.9 });
    if (t.x2_ts && t.x2_ts !== t.x1_ts) mk.push({ time: snap(t.x2_ts), position: d > 0 ? "aboveBar" : "belowBar", shape: "circle", color: d * (t.x2_px - t.entry) > 0 ? token("--green") : token("--red"), text: "x2", size: 0.9 });
    const uniq = new Map();
    for (const m of mk.sort((a, b) => a.time - b.time)) { const k = m.time + m.shape + m.position; if (!uniq.has(k)) uniq.set(k, m); }
    s.setMarkers([...uniq.values()]);
    // zoom to the trade: 50 bars before the touch to 20 bars after the exit (or the last bar)
    const endT = t.x2_ts ? snap(t.x2_ts) : ts[ts.length - 1];
    const i0 = Math.max(0, ts.findIndex((x) => x >= c.a_ts) - 50);
    let i1 = ts.findIndex((x) => x >= endT);
    i1 = i1 < 0 ? ts.length - 1 : Math.min(ts.length - 1, i1 + 20);
    const to = Math.max(i1, i0 + 60), W = el.clientWidth - 70, px = labels.maxWidth() + 10;
    ch.timeScale().setVisibleLogicalRange({ from: i0, to: to + Math.ceil((px * (to - i0)) / Math.max(W - px, 80)) });
  },
  close(silent) {
    const sh = document.getElementById("sheet"), sc = document.getElementById("scrim");
    if (this.ch) { this.ch.remove(); this.ch = null; }
    this.id = null;
    sh.classList.remove("open"); sc.classList.remove("open");
    setTimeout(() => { if (!sh.classList.contains("open")) { sh.hidden = true; sc.hidden = true; sh.innerHTML = ""; } }, silent ? 0 : 230);
  },
};

// ------------------------------------------------------------------ performance page
const PerformancePage = {
  async mount(el) {
    this.el = el;
    el.innerHTML = `<div class="page">${demoBanner()}<div class="page-head"><h1>Performance</h1></div><div class="grid grid-4">${skel(230)}${skel(230)}${skel(230)}${skel(230)}</div>${skel(320)}</div>`;
    await this.refresh();
  },
  async refresh() {
    let d;
    try { d = await api("/api/performance"); } catch (e) {
      this.el.querySelector(".page").innerHTML = demoBanner() + errorCard(e, () => this.refresh());
      return;
    }
    const th = S.summary?.thresholds || { freeze: 30, decision: 50, ci_lower_min_r: -0.1, ci_min: 10 };
    const L = d.ledgers;
    if (!L.length) {
      this.el.querySelector(".page").innerHTML = demoBanner() + `<div class="card">${empty("No ledgers yet", "Start the paper engine to create one ledger per variant.")}</div>`;
      return;
    }
    const tiles = L.map((x) => {
      const s = x.stats, n = s.n_closed;
      const pct = Math.min(100, (100 * n) / th.decision);
      const ciCls = n < th.decision ? "text-2" : s.ci && s.ci.lo > th.ci_lower_min_r ? "pos" : "neg";
      const ci = s.ci ? `90% CI <b class="num ${ciCls}">[${sgn(s.ci.lo, 2)}, ${sgn(s.ci.hi, 2)}]R</b>` : `90% CI from ${th.ci_min} closed trades`;
      const status = n < th.freeze ? `<span class="badge">FROZEN ${n}/${th.freeze}</span>` : s.progress.decision_rule_met ? `<span class="badge win">RULE MET</span>` : n >= th.decision ? `<span class="badge loss">RULE NOT MET</span>` : `<span class="badge info">REVIEW OK</span>`;
      return `<div class="card tile">
        <div class="tile-top"><div style="display:flex;flex-direction:column;gap:6px;min-width:0">${vtag(x.variant, x.key, x.color)}<span class="muted" style="font-size:11px">version ${esc(x.version)} · since ${fTime(x.paper_start)}</span></div>${status}</div>
        <div class="tile-top" style="align-items:flex-end"><div><div class="label">Cumulative</div><div class="kpi-value ${cls(s.sum_r)}" data-f="cum${x.variant}" data-v="${s.sum_r}">${fR(s.sum_r)}</div></div>${sparkline(s.equity, x.color)}</div>
        <div class="mini">
          <div><div class="label">Trades</div><div class="v">${n}${s.n_open ? `<span class="muted" style="font-weight:500"> +${s.n_open}</span>` : ""}</div></div>
          <div><div class="label">Win</div><div class="v">${fPct(s.win_rate)}</div></div>
          <div><div class="label">Exp</div><div class="v ${cls(s.exp_r)}">${fR(s.exp_r)}</div></div>
          <div><div class="label">Max DD</div><div class="v">${s.max_dd_r ? s.max_dd_r.toFixed(1) + "R" : "–"}</div></div>
        </div>
        <div><div class="progress"><div class="fill" style="width:${pct}%"></div><div class="tick" style="left:${(100 * th.freeze) / th.decision}%"></div></div>
          <div class="progress-legend"><span>${n} / ${th.decision} closed</span><span class="at" style="left:${(100 * th.freeze) / th.decision}%">${th.freeze}</span><span class="end">${th.decision}</span></div></div>
        <div class="kpi-sub">${ci}${s.exp_bps != null ? ` · ${fBps(s.exp_bps)}` : ""}</div></div>`;
    }).join("");
    const cmp = L.map((x) => {
      const s = x.stats, b = x.backtest || {};
      const diff = s.exp_r != null && b.exp_r != null ? s.exp_r - b.exp_r : null;
      return `<tr><td>${vtag(x.variant, x.key, x.color)}</td><td class="r num">${s.n_closed}</td><td class="r num ${cls(s.exp_r)}">${fR(s.exp_r, 3)}</td><td class="r num">${fBps(s.exp_bps)}</td><td class="r num">${fPct(s.win_rate)}</td>
        <td class="r num muted">${b.n ?? "–"}</td><td class="r num ${cls(b.exp_r)}">${fR(b.exp_r, 3)}</td><td class="r num">${fBps(b.exp_bps)}</td><td class="r num">${fPct(b.win_rate)}</td><td class="r num ${cls(diff)}">${fR(diff, 3)}</td></tr>`;
    }).join("");
    const weeks = d.weeks.slice(0, 12);
    const wk = weeks.length ? weeks.map((w) => `<tr><td class="num">${w}</td>${L.map((x) => {
      const r = x.stats.weekly.find((y) => y.week === w);
      return r ? `<td class="r num"><span class="muted">${r.n} ·</span> <span class="${cls(r.sum_r)}">${fR(r.sum_r)}</span></td>` : `<td class="r muted">–</td>`;
    }).join("")}</tr>`).join("") : "";
    const anyEq = L.some((x) => x.stats.equity.length);
    this.el.querySelector(".page").innerHTML = `${demoBanner()}<div class="page-head"><h1>Performance</h1><span class="muted" style="font-size:12px">net of OKX default costs · R and bps</span></div>
      <div class="grid grid-4">${tiles}</div>
      <div class="card"><div class="card-head"><h3>Equity (cumulative net R, closed trades)</h3><span class="spacer"></span><div class="chips">${L.map((x) => vtag(x.variant, "", x.color)).join("")}</div></div>
        ${anyEq ? `<div class="trade-chart" style="height:300px"><div class="chart-el" id="eq"></div></div>` : empty("No closed trades yet", "The equity curves start with the first closed trade.")}</div>
      <div class="grid grid-2">
        <div class="card"><div class="card-head"><h3>Paper vs backtest expectation</h3><span class="spacer muted" style="font-size:12px">backtest = A+B in-sample</span></div>
          <div class="table-wrap"><table><thead><tr><th>Variant</th><th class="r">N</th><th class="r">Exp</th><th class="r">bps</th><th class="r">Win</th><th class="r">BT N</th><th class="r">BT exp</th><th class="r">BT bps</th><th class="r">BT win</th><th class="r">Δ exp</th></tr></thead><tbody>${cmp}</tbody></table></div></div>
        <div class="card"><div class="card-head"><h3>Weekly (entry week, UTC)</h3></div>
          ${wk ? `<div class="table-wrap"><table><thead><tr><th>Week</th>${L.map((x) => `<th class="r">${esc(x.variant)}</th>`).join("")}</tr></thead><tbody>${wk}</tbody></table></div>` : empty("No closed trades yet", "Weekly rows appear after the first exit.", true)}</div>
      </div>
      <div class="card"><div class="card-head"><h3>Decision rules (fixed before the first paper trade)</h3></div>
        <div class="rows">
          <div class="row"><span class="badge">1</span><span class="grow text-2">No rule changes before a variant has ≥ ${th.freeze} closed trades; weekly reviews are for bugs and visual checks only.</span></div>
          <div class="row"><span class="badge">2</span><span class="grow text-2">Real money is only considered after ≥ ${th.decision} closed trades with positive net expectancy and a 90% CI lower bound above ${sgn(th.ci_lower_min_r, 1)}R, and only by the owner's explicit decision.</span></div>
        </div></div>`;
    if (anyEq) this.equity(L);
    flash(this.el);
  },
  equity(L) {
    if (this.ch) this.ch.remove();
    const el = this.el.querySelector("#eq");
    const ch = baseChart(el, { timeScale: { timeVisible: false }, chart: { rightPriceScale: { borderColor: token("--border"), scaleMargins: { top: 0.15, bottom: 0.15 } } } });
    this.ch = ch;
    for (const x of L) {
      if (!x.stats.equity.length) continue;
      const pts = [];
      let lastT = 0;
      for (const [t, v] of x.stats.equity) { const tt = t <= lastT ? lastT + 1 : t; pts.push({ time: tt, value: v }); lastT = tt; }
      const s = ch.addLineSeries({ color: x.color, lineWidth: 2, priceLineVisible: false, lastValueVisible: true, title: x.variant, crosshairMarkerRadius: 4, priceFormat: { type: "custom", formatter: (v) => fR(v) } });
      s.setData(pts);
      s.createPriceLine({ price: 0, color: token("--border-strong"), lineWidth: 1, lineStyle: 2, axisLabelVisible: false });
    }
    ch.timeScale().fitContent();
  },
  destroy() { this.ch?.remove(); this.ch = null; },
};

// ------------------------------------------------------------------ health page
// Independent cross-check (flowlevels.paper.verify): engine values vs a separate implementation.
const CHECK_COLS = [
  ["candles_vs_rest", "Candles vs REST"], ["bars", "Bars"], ["ema50_4h", "4H EMA50"], ["ema50_1d", "D EMA50"],
  ["tpo_now", "TPO now"], ["tpo_history", "TPO history"], ["bias", "Bias"],
];
function verifyCard(v) {
  v = v || {};
  const r = v.result;
  const badge = r == null ? `<span class="badge pending">${v.running ? "RUNNING" : "PENDING"}</span>`
    : r.ok === true ? '<span class="badge win">PASS</span>' : r.ok === false ? '<span class="badge loss">FAIL</span>' : '<span class="badge">NO DATA</span>';
  const cell = (c) => {
    if (!c) return '<td class="c muted">–</td>';
    const tip = `n=${c.n} · max rel. diff ${c.max_rel.toExponential(1)}${c.detail ? " · " + c.detail : ""}`;
    if (c.ok == null) return `<td class="c muted" title="${esc(tip)}">–</td>`;
    return `<td class="c" title="${esc(tip)}"><span class="check ${c.ok ? "ok" : "bad"}">${c.ok ? I.check : I.close}</span></td>`;
  };
  const rows = r ? r.symbols.map((x) => {
    const by = Object.fromEntries((x.checks || []).map((c) => [c.name, c]));
    return `<tr><td><b>${short(x.symbol)}</b></td>${CHECK_COLS.map(([k]) => cell(by[k])).join("")}</tr>`;
  }).join("") : "";
  const fails = r ? r.symbols.flatMap((x) => [
    ...(x.note ? [`<b>${short(x.symbol)}</b> ${esc(x.note)}`] : []),
    ...(x.checks || []).filter((c) => c.ok === false).map((c) => `<b>${short(x.symbol)}</b> ${esc(c.name)}: ${esc(c.detail)}`),
  ]) : [];
  const mx = r ? Math.max(0, ...r.symbols.flatMap((x) => (x.checks || []).map((c) => c.max_rel))) : null;
  const ago = r ? fAgo((Date.now() - r.ts) / 1000) + " ago" : "–";
  return `<div class="card" id="verify"><div class="card-head"><h3>Independent cross-check</h3>${badge}<span class="spacer muted" style="font-size:12px">${r ? ago : ""}</span><button class="btn small" id="verify-run"${v.running ? " disabled" : ""}>Run now</button></div>
    <p class="muted note">Bars, 4H/daily EMA50, prior-day/week TPO levels and the bias are recomputed from the stored candles by a separate implementation (not the strategy code) and compared with what the engine computed and this dashboard shows; the newest stored candles are compared with Binance REST. Runs every ${v.every_min || 15} min; CLI: <code>flowlevels verify</code>.</p>
    ${r ? `<div class="table-wrap"><table class="verify"><thead><tr><th>Symbol</th>${CHECK_COLS.map(([, n]) => `<th class="c">${n}</th>`).join("")}</tr></thead><tbody>${rows}</tbody></table></div>
    <div class="kv" style="margin-top:12px"><div><span class="label">Last run (UTC)</span><span class="v">${fTime(r.ts)}</span></div>
      <div><span class="label">Max rel. difference</span><span class="v num">${mx.toExponential(1)}</span></div>
      <div><span class="label">Tolerance</span><span class="v num">${r.rel_tol.toExponential(0)}</span></div>
      <div><span class="label">REST check</span><span class="v">${r.rest ? "on" : "off (demo)"}</span></div></div>
    ${fails.length ? `<div class="rows" style="margin-top:12px">${fails.map((x) => `<div class="row"><span class="badge loss">FAIL</span><span class="grow text-2" style="font-size:13px">${x}</span></div>`).join("")}</div>` : ""}`
    : empty("First check pending", "The first run starts about a minute after the dashboard starts.", true)}</div>`;
}
const HealthPage = {
  async mount(el) {
    this.el = el;
    el.innerHTML = `<div class="page">${demoBanner()}<div class="page-head"><h1>Health</h1></div><div class="grid grid-3">${skel(160)}${skel(160)}${skel(160)}</div>${skel(300)}</div>`;
    await this.refresh();
  },
  async refresh() {
    let d;
    try { d = await api("/api/health"); } catch (e) {
      this.el.querySelector(".page").innerHTML = demoBanner() + errorCard(e, () => this.refresh());
      return;
    }
    const e = d.engine || {};
    const st = e.status || "unknown";
    const dot = st === "running" ? "ok" : st === "demo" ? "warn" : st === "unknown" ? "" : "bad";
    const stallS = (d.stall_minutes || 5) * 60;
    const lagCls = (x) => (x == null ? "muted" : x <= 90 ? "pos" : x <= stallS ? "warn" : "neg");
    const sym = d.symbols.length ? d.symbols.map((x) => {
      const b = x.bias || {};
      const bias = b.dir > 0 ? '<span class="badge long">LONG</span>' : b.dir < 0 ? '<span class="badge short">SHORT</span>' : '<span class="badge">NONE</span>';
      return `<tr><td><b>${short(x.symbol)}</b></td><td class="num">${fTime(x.last_minute_ts)}</td><td class="r num ${lagCls(x.lag_s)}" data-f="lag${x.symbol}" data-v="${x.lag_s}">${fAgo(x.lag_s)}</td><td class="r num muted">${x.compute_ms != null ? Math.round(x.compute_ms) + " ms" : "–"}</td><td>${bias}</td><td class="num muted">${fTime(x.updated_ts)}</td></tr>`;
    }).join("") : "";
    const nt = d.notifications || {};
    const ev = d.events.length ? d.events.map((x) => `<div class="row"><span class="badge ${x.level === "error" ? "loss" : x.level === "warning" ? "warn" : ""}">${x.level.toUpperCase()}</span><span class="num muted" style="font-size:12px;white-space:nowrap">${fTime(x.ts)}</span><span class="grow text-2" style="font-size:13px;overflow:hidden;text-overflow:ellipsis">${x.symbol ? `<b>${short(x.symbol)}</b> ` : ""}${esc(x.kind)}: ${esc(x.message)}</span></div>`).join("") : empty("No events", "", true);
    const disk = d.disk || {};
    const vers = (d.versions || []).map((v) => `<div class="row" style="padding:8px 0"><div class="grow" style="display:flex;flex-direction:column;gap:2px">${vtag(v.id, v.key, v.color)}<span class="muted num" style="font-size:12px">paper start ${fTime(v.paper_start, true)}</span></div><span class="mono muted">${esc(v.version)}</span></div>`).join("");
    this.el.querySelector(".page").innerHTML = `${demoBanner()}<div class="page-head"><h1>Health</h1></div>
      <div class="grid grid-3">
        <div class="card"><div class="card-head"><span class="dot ${dot}"></span><h3>Engine ${esc(st)}</h3></div>
          <div class="kv"><div><span class="label">Heartbeat</span><span class="v">${e.heartbeat_age_s != null ? fAgo(e.heartbeat_age_s) + " ago" : "–"}</span></div>
            <div><span class="label">Up since</span><span class="v">${fTime(e.started)}</span></div>
            <div><span class="label">Loops</span><span class="v">${e.loops ?? "–"}</span></div>
            <div><span class="label">Commit</span><span class="v mono">${esc((e.commit || "–").slice(0, 12))}</span></div>
            <div><span class="label">Config</span><span class="v mono">${esc((e.config || "–").slice(0, 12))}</span></div>
            <div><span class="label">Telegram</span><span class="v ${e.telegram ? "pos" : "warn"}">${e.telegram ? "enabled" : "not set"}</span></div></div></div>
        <div class="card"><div class="card-head"><h3>Disk</h3><span class="spacer num ${disk.pct > disk.limit_pct ? "neg" : "muted"}">${disk.pct?.toFixed(1)}%</span></div>
          <div class="progress"><div class="fill" style="width:${Math.min(100, disk.pct || 0)}%;background:${disk.pct > disk.limit_pct ? token("--red") : token("--accent")}"></div><div class="tick" style="left:${disk.limit_pct}%;background:${token("--amber")}"></div></div>
          <div class="progress-legend"><span>${disk.free_gb?.toFixed(1)} GB free of ${disk.total_gb?.toFixed(0)} GB</span><span class="warn end">alert at ${disk.limit_pct}%</span></div></div>
        <div class="card"><div class="card-head"><h3>Notifications</h3></div>
          <div class="kv"><div><span class="label">Sent</span><span class="v ${nt.sent ? "pos" : ""}">${nt.sent || 0}</span></div><div><span class="label">Pending</span><span class="v ${nt.pending ? "warn" : ""}">${nt.pending || 0}</span></div>
            <div><span class="label">Failed</span><span class="v ${nt.failed ? "neg" : ""}">${nt.failed || 0}</span></div><div><span class="label">Not sent (no token)</span><span class="v muted">${nt.disabled || 0}</span></div>
            <div><span class="label">Last sent</span><span class="v">${fTime(d.last_sent_ms)}</span></div></div></div>
      </div>
      ${verifyCard(d.verify)}
      <div class="split">
        <div class="card"><div class="card-head"><h3>Data feed (Binance USD-M 1m klines)</h3><span class="spacer muted" style="font-size:12px">stall alert after ${d.stall_minutes} min</span></div>
          ${sym ? `<div class="table-wrap"><table><thead><tr><th>Symbol</th><th>Last closed (UTC)</th><th class="r">Lag</th><th class="r">Compute</th><th>Bias</th><th>Updated</th></tr></thead><tbody>${sym}</tbody></table></div>` : empty("No data yet", "The engine has not written any symbol state.")}</div>
        <div class="card"><div class="card-head"><h3>Rule versions</h3></div><div class="rows">${vers || empty("No versions", "", true)}</div></div>
      </div>
      <div class="card"><div class="card-head"><h3>Recent events</h3></div><div class="rows">${ev}</div></div>`;
    flash(this.el);
    const btn = this.el.querySelector("#verify-run");
    if (btn) btn.onclick = async () => {
      btn.disabled = true; btn.textContent = "Running…";
      try { await fetch("/api/verify/run", { method: "POST" }); } finally { this.refresh(); }
    };
  },
  destroy() {},
};

// ------------------------------------------------------------------ router + live updates
const PAGE_OBJ = { chart: ChartPage, positions: PositionsPage, trades: TradesPage, performance: PerformancePage, health: HealthPage };

async function route() {
  const h = location.hash.replace(/^#\/?/, "");
  const [id, arg] = h.split("/");
  const pageId = PAGE_OBJ[id] ? id : "chart";
  document.querySelectorAll("[data-page]").forEach((a) => a.classList.toggle("active", a.dataset.page === pageId));
  if (pageId === S.pageId && pageId === "trades") {
    if (arg) Sheet.open(arg); else Sheet.close();
    return;
  }
  S.page?.destroy();
  S.pageId = pageId;
  statusbar();
  S.page = PAGE_OBJ[pageId];
  window.scrollTo(0, 0);
  await S.page.mount(document.getElementById("view"), arg);
}

let refreshTimer = null;
function scheduleRefresh() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(async () => {
    await loadSummary();
    await S.page?.refresh();
    if (Sheet.id) Sheet.render(Sheet.id);
  }, 250);
}

function connect(delay = 1000) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  let ws;
  try { ws = new WebSocket(`${proto}://${location.host}/ws`); } catch { setTimeout(() => connect(Math.min(delay * 2, 15000)), delay); return; }
  let lastDv = null;
  ws.onopen = () => { S.ws = "open"; statusbar(); delay = 1000; };
  ws.onmessage = (m) => {
    const msg = JSON.parse(m.data);
    S.lastServerSkew = Date.now() - msg.server_ms;
    if (msg.dv !== lastDv) { lastDv = msg.dv; scheduleRefresh(); }
  };
  ws.onclose = () => { S.ws = "reconnecting"; statusbar(); setTimeout(() => connect(Math.min(delay * 2, 15000)), delay); };
  ws.onerror = () => ws.close();
}

async function main() {
  // charts draw text on canvas: make sure the vendored Inter faces are loaded first
  try { await Promise.all(["400", "500", "600", "700"].map((w) => document.fonts.load(`${w} 12px Inter`))); } catch { /* fallback font */ }
  shell();
  await loadSummary();
  window.addEventListener("hashchange", route);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && Sheet.id) location.hash = "#/trades"; });
  await route();
  connect();
  setInterval(tickAgo, 1000);
  setInterval(() => { if (S.ws !== "open") scheduleRefresh(); }, 30000); // fallback polling
}
main();
