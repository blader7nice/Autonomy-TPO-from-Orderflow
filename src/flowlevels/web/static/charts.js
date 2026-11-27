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
  set(bands, barTimes) {
    this.bands = bands;
    this.times = barTimes;
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
              if (b.label && h >= 12 && !labels.some((l) => Math.abs(l.y - ly) < 13 && Math.abs(l.x - x1) < 90)) {
                labels.push({ x: x1, y: ly });
                ctx.font = `${Math.round(10 * vr)}px ${token("--font")}`;
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
