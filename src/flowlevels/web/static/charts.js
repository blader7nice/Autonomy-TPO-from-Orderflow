// Chart helpers on TradingView Lightweight Charts v4.2 (vendored). Colors come only from the
// CSS tokens in theme.css, so charts, tables and badges share one palette.
/* global LightweightCharts */

export function token(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

const PREC = { BTCUSDT: 1, ETHUSDT: 2, SOLUSDT: 2, BNBUSDT: 2, XRPUSDT: 4, DOGEUSDT: 5 };
export function precision(symbol, price) {
  if (symbol in PREC) return PREC[symbol];
  const a = Math.abs(price || 0);
  return a >= 1000 ? 1 : a >= 10 ? 2 : a >= 0.1 ? 4 : 6;
}

export function fmtPrice(symbol, x) {
  const p = precision(symbol, x);
  return Number(x).toLocaleString("en-US", { minimumFractionDigits: p, maximumFractionDigits: p });
}

export function baseChart(el, opts = {}) {
  const chart = LightweightCharts.createChart(el, {
    autoSize: true,
    layout: {
      background: { type: "solid", color: token("--surface") },
      textColor: token("--muted"),
      fontFamily: token("--font"),
      fontSize: 11,
    },
    grid: { vertLines: { color: token("--grid") }, horzLines: { color: token("--grid") } },
    rightPriceScale: { borderColor: token("--border"), scaleMargins: { top: 0.12, bottom: 0.1 } },
    timeScale: {
      borderColor: token("--border"),
      timeVisible: true,
      secondsVisible: false,
      rightOffset: 6,
      ...(opts.timeScale || {}),
    },
    crosshair: {
      mode: LightweightCharts.CrosshairMode.Normal,
      vertLine: { color: token("--border-strong"), labelBackgroundColor: token("--surface-3"), width: 1, style: 3 },
      horzLine: { color: token("--border-strong"), labelBackgroundColor: token("--surface-3"), width: 1, style: 3 },
    },
    localization: { locale: "en-US" },
    handleScale: { axisPressedMouseMove: { time: true, price: true } },
    ...opts.chart,
  });
  return chart;
}

export function candles(chart, symbol, lastPrice) {
  const p = precision(symbol, lastPrice);
  return chart.addCandlestickSeries({
    upColor: token("--green"),
    downColor: token("--red"),
    borderVisible: false,
    wickUpColor: token("--green"),
    wickDownColor: token("--red"),
    priceFormat: { type: "custom", minMove: Math.pow(10, -p), formatter: (x) => fmtPrice(symbol, x) },
    priceLineColor: token("--text-2"),
    priceLineStyle: 2,
    lastValueVisible: false, // the last price is a pane label (PaneLabels): the axis keeps only ticks
  });
}

export function emaSeries(chart, kind) {
  return chart.addLineSeries({
    color: kind === "4h" ? token("--ema4") : token("--emad"),
    lineWidth: kind === "4h" ? 2 : 1.5,
    lineStyle: kind === "4h" ? 0 : 2,
    lineType: 1, // steps: the value in force from each closed bar on
    priceLineVisible: false,
    lastValueVisible: false,
    crosshairMarkerVisible: false,
    autoscaleInfoProvider: () => null, // the price scale follows the candles only
  });
}

// Translucent POI bands (series primitive; drawn below the candles).
export class Bands {
  constructor() {
    this.bands = [];
    this.times = [];
  }
  attached({ chart, series, requestUpdate }) {
    this.chart = chart;
    this.series = series;
    this.req = requestUpdate;
  }
  detached() {}
  set(bands, barTimes, reserveRight = 0) {
    this.bands = bands;
    this.times = barTimes;
    this.reserve = reserveRight; // px kept free for the right-edge labels
    if (this.req) this.req();
  }
  updateAllViews() {}
  _x(t, width, isEnd) {
    const ts = this.times;
    if (!ts.length) return null;
    if (t == null) return isEnd ? width : 0;
    if (t <= ts[0]) return isEnd ? null : 0;
    if (t > ts[ts.length - 1]) return isEnd ? width : null;
    let lo = 0, hi = ts.length - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (ts[mid] < t) lo = mid + 1; else hi = mid;
    }
    return this.chart.timeScale().timeToCoordinate(ts[lo]);
  }
  paneViews() {
    const self = this;
    return [{
      zOrder: () => "bottom",
      renderer: () => ({
        draw(target) {
          target.useBitmapCoordinateSpace((scope) => {
            const ctx = scope.context, hr = scope.horizontalPixelRatio, vr = scope.verticalPixelRatio;
            const width = scope.mediaSize.width;
            const labels = [];
            for (const b of self.bands) {
              const y1 = self.series.priceToCoordinate(b.top), y2 = self.series.priceToCoordinate(b.bottom);
              if (y1 == null || y2 == null) continue;
              const x1 = self._x(b.from, width, false);
              const x2 = self._x(b.to, width, true);
              if (x1 == null || x2 == null || x2 <= x1) continue;
              const top = Math.min(y1, y2), h = Math.max(Math.abs(y2 - y1), 1.5);
              ctx.fillStyle = b.fill;
              ctx.fillRect(Math.round(x1 * hr), Math.round(top * vr), Math.round((x2 - x1) * hr), Math.round(h * vr));
              ctx.fillStyle = b.edge;
              const ny = b.dir > 0 ? top : top + h; // near edge line (where price arrives)
              ctx.fillRect(Math.round(x1 * hr), Math.round(ny * vr) - Math.round(hr / 2), Math.round((x2 - x1) * hr), Math.max(1, Math.round(vr)));
              const ly = top + h / 2;
              ctx.font = `${Math.round(10 * vr)}px ${token("--font")}`;
              const tw = b.label ? ctx.measureText(b.label).width / hr : 0;
              const reserve = typeof self.reserve === "function" ? self.reserve() : self.reserve || 0;
            const fits = x1 + 6 + tw <= width - reserve - 4 && ly > 8;
              if (b.label && h >= 12 && fits && !labels.some((l) => Math.abs(l.y - ly) < 13 && Math.abs(l.x - x1) < 90)) {
                labels.push({ x: x1, y: ly });
                ctx.fillStyle = b.text;
                ctx.textBaseline = "middle";
                ctx.fillText(b.label, Math.round((x1 + 6) * hr), Math.round((top + h / 2) * vr));
              }
            }
          });
        },
      }),
    }];
  }
}

export function poiBand(p) {
  const testing = p.state === "testing";
  const long = p.dir > 0;
  const top = Math.max(p.near, p.far), bottom = Math.min(p.near, p.far);
  return {
    top, bottom, from: p.from, to: p.to, dir: p.dir,
    fill: testing ? token("--zone-testing") : long ? token("--zone-long") : token("--zone-short"),
    edge: testing ? token("--zone-testing-edge") : long ? token("--zone-long-edge") : token("--zone-short-edge"),
    text: token("--text-2"),
    label: `${p.kind.replace("zone_", "zone ").replace("_", " ")}${testing ? " · testing" : ""}`,
  };
}

export function priceLine(series, price, color, title, style = 0, axis = true, width = 1) {
  return series.createPriceLine({
    price, color, lineWidth: width, lineStyle: style, axisLabelVisible: axis, title,
    axisLabelColor: color, axisLabelTextColor: token("--bg"),
  });
}

// Small inline SVG sparkline (equity) - no chart instance per tile.
export function sparkline(points, color) {
  if (!points || points.length < 2) {
    return `<svg class="spark" viewBox="0 0 112 40" aria-hidden="true"><line x1="0" y1="20" x2="112" y2="20" stroke="${token("--border-strong")}" stroke-dasharray="3 3"/></svg>`;
  }
  const ys = [0, ...points.map((p) => p[1])];
  const lo = Math.min(...ys), hi = Math.max(...ys);
  const span = hi - lo || 1;
  const n = ys.length - 1;
  const xy = ys.map((y, i) => [(i / n) * 108 + 2, 36 - ((y - lo) / span) * 32]);
  const d = xy.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join("");
  const zero = 36 - ((0 - lo) / span) * 32;
  const last = xy[xy.length - 1];
  return `<svg class="spark" viewBox="0 0 112 40" aria-hidden="true">
    <line x1="2" x2="110" y1="${zero.toFixed(1)}" y2="${zero.toFixed(1)}" stroke="${token("--border-strong")}" stroke-dasharray="2 3"/>
    <path d="${d}" fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
    <circle cx="${last[0].toFixed(1)}" cy="${last[1].toFixed(1)}" r="2.5" fill="${color}"/></svg>`;
}

// ---------------------------------------------------------------- step values (EMA)

// EMA points sit at each closed bar's close: the rules use that value until the next close.
// Carry the last value to `tEnd` so the line reaches the newest candle.
export function stepTo(points, tEnd) {
  const out = points.map((x) => ({ time: x[0], value: x[1] }));
  const last = out[out.length - 1];
  if (last && tEnd > last.time) out.push({ time: tEnd, value: last.value });
  return out;
}

// Value in force at time t (seconds): the last point with time <= t.
export function valueAt(points, t) {
  let lo = 0, hi = points.length - 1, k = -1;
  while (lo <= hi) {
    const m = (lo + hi) >> 1;
    if (points[m][0] <= t) { k = m; lo = m + 1; } else hi = m - 1;
  }
  return k < 0 ? null : points[k][1];
}

// ---------------------------------------------------------------- right-edge labels

const PH = 18, GAP = 2, FONT_PX = 11;
let measureCtx = null;
function textWidth(s) {
  if (!measureCtx) measureCtx = document.createElement("canvas").getContext("2d");
  measureCtx.font = `600 ${FONT_PX}px ${token("--font")}`;
  return measureCtx.measureText(s).width;
}

// Price labels drawn inside the pane at its right edge - never on the price axis, so they
// cannot cover the axis tick labels. Labels closer than one pill height are merged (same
// `group`; with `exact` only at the identical price) or pushed apart with a leader to their price. Items with `clamp` (EMAs) stay
// visible when their price is off-scale: pinned to the top/bottom edge with an arrow. When
// there is no room, the labels with the lowest `prio` are dropped.
export class PaneLabels {
  constructor() { this.items = []; this.compact = false; }
  attached({ series, requestUpdate }) { this.series = series; this.req = requestUpdate; }
  detached() {}
  set(items, compact) { this.items = items; this.compact = compact; if (this.req) this.req(); }
  updateAllViews() {}
  text(g) {
    // merged position labels read "a·b entry"; other merged labels "pdVAL · pwVAL"
    const tags = [...new Set(g.items.map((i) => i.tag).filter(Boolean))];
    const base = [...new Set(g.items.map((i) => i.name).filter(Boolean))].join(" · ");
    const names = tags.length ? `${tags.join("·")} ${base}` : base;
    if (this.compact && !g.clampDir && !g.items.some((i) => i.price_always)) return names;
    const prices = [...new Set(g.items.map((i) => i.text))].join(" / ");
    return `${g.clampDir < 0 ? "↑ " : g.clampDir > 0 ? "↓ " : ""}${names} ${prices}`.trim();
  }
  // Width the labels need: from the real layout once the chart has a price scale (merged
  // labels are wider than single ones), else an estimate from the single labels.
  maxWidth(H) {
    let w = 0;
    if (H && this.series && this.series.priceToCoordinate(this.items[0]?.price ?? 0) != null) {
      for (const g of this.layout(H)) w = Math.max(w, textWidth(this.text(g)));
    } else {
      for (const i of this.items) w = Math.max(w, textWidth(this.compact && !i.price_always ? `${i.tag || ""} ${i.name}` : `↑ ${i.tag || ""} ${i.name} ${i.text}`));
    }
    return Math.ceil(w) + 12;
  }
  layout(H) {
    const pts = [];
    for (const it of this.items) {
      const y0 = this.series.priceToCoordinate(it.price);
      if (y0 == null) continue;
      let clampDir = 0;
      if (y0 < 0 || y0 > H) {
        if (!it.clamp) continue;
        clampDir = y0 < 0 ? -1 : 1;
      }
      pts.push({ it, y: Math.min(Math.max(y0, PH / 2 + 1), H - PH / 2 - 1), clampDir });
    }
    pts.sort((a, b) => a.y - b.y);
    const groups = [];
    for (const p of pts) {
      const g = groups[groups.length - 1];
      const near = p.it.mergePx ?? PH; // closer than this: one label; else offset with a leader
      const same = g && p.it.group && g.group === p.it.group && !p.clampDir && !g.clampDir && p.y - g.ys[g.ys.length - 1] < near;
      if (same && (!p.it.exact || g.items.every((i) => i.text === p.it.text))) {
        g.items.push(p.it); g.ys.push(p.y);
        g.prio = Math.max(g.prio, p.it.prio || 0);
      } else {
        groups.push({ items: [p.it], ys: [p.y], clampDir: p.clampDir, group: p.it.group, prio: p.it.prio || 0, bg: p.it.bg, fg: p.it.fg });
      }
    }
    for (const g of groups) { g.y0 = g.ys.reduce((a, b) => a + b, 0) / g.ys.length; g.y = g.y0; }
    const cap = Math.max(1, Math.floor((H - 2) / (PH + GAP)));
    while (groups.length > cap) { // drop the least important labels until they fit
      let k = 0;
      groups.forEach((g, i) => { if (g.prio < groups[k].prio) k = i; });
      groups.splice(k, 1);
    }
    for (let i = 0; i < groups.length; i++) {
      const lo = i ? groups[i - 1].y + PH + GAP : PH / 2 + 1;
      groups[i].y = Math.max(groups[i].y, lo);
    }
    for (let i = groups.length - 1; i >= 0; i--) {
      const hi = i < groups.length - 1 ? groups[i + 1].y - PH - GAP : H - PH / 2 - 1;
      groups[i].y = Math.min(groups[i].y, hi);
    }
    return groups;
  }
  paneViews() {
    const self = this;
    return [{
      zOrder: () => "top",
      renderer: () => ({
        draw(target) {
          target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
            if (!self.series) return;
            const W = mediaSize.width, H = mediaSize.height;
            ctx.font = `600 ${FONT_PX}px ${token("--font")}`;
            ctx.textBaseline = "middle";
            const gs = self.layout(H).map((g) => {
              const s = self.text(g);
              const w = Math.ceil(ctx.measureText(s).width) + 10;
              return { ...g, s, w, x: W - w - 2, yi: Math.round(g.y) };
            });
            const widest = gs.reduce((m, g) => Math.max(m, g.w + 2), 0);
            const xl = W - widest; // left edge of the label column
            // leaders first (pills are drawn over them): price level -> column -> pill
            for (const g of gs) {
              if (Math.abs(g.y - g.y0) <= 1.5 || g.clampDir) continue;
              const yt = Math.round(g.y0) + 0.5;
              ctx.strokeStyle = g.items[0].lc || g.bg; ctx.lineWidth = 1;
              ctx.beginPath(); ctx.moveTo(xl - 14, yt); ctx.lineTo(xl - 8, yt); ctx.lineTo(xl - 2, g.yi + 0.5); ctx.lineTo(g.x, g.yi + 0.5); ctx.stroke();
            }
            for (const g of gs) {
              ctx.fillStyle = g.bg;
              ctx.beginPath();
              if (ctx.roundRect) ctx.roundRect(g.x, g.yi - PH / 2, g.w, PH, 4); else ctx.rect(g.x, g.yi - PH / 2, g.w, PH);
              ctx.fill();
              ctx.fillStyle = g.fg;
              ctx.fillText(g.s, g.x + 5, g.yi + 0.5);
            }
            self.drawnWidth = widest; // the POI band labels keep clear of this
          });
        },
      }),
    }];
  }
}
