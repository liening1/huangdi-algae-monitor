"use strict";

// 调试：把任何 JS 运行时错误显示成红框，便于排查"一直转圈"
(function () {
  function showErr(msg) {
    if (!document.body) return;
    var el = document.createElement("div");
    el.style.cssText = "position:fixed;left:8px;bottom:8px;z-index:99999;background:#c00;color:#fff;padding:8px 12px;border-radius:8px;max-width:92vw;font:12px/1.4 monospace;white-space:pre-wrap;max-height:60vh;overflow:auto";
    el.textContent = "JS错误: " + msg;
    document.body.appendChild(el);
  }
  window.addEventListener("error", function (e) {
    showErr((e.message || e.error || "unknown") + (e.filename ? (" @ " + e.filename + ":" + e.lineno) : ""));
  });
  window.addEventListener("unhandledrejection", function (e) {
    showErr("Promise: " + ((e.reason && e.reason.message) || e.reason));
  });
})();

const DEFAULT_BBOX = [120.4717559, 31.386768, 120.61064, 31.477232]; // 镇域∪5km缓冲 分析网格 bbox
let result = null;
let map = null;
let layers = {};          // 各图层对象
let allBloom = null;      // 原始 bloom geojson（含 mean/area_ha）
let waterFC = null, boundaryFC = null;
let REV = 0;              // 资源版本号：仅“重新分析”后变化，平时命中浏览器缓存
let BASEMAP = "gaode";    // gaode | esri | osm  (高德为国内默认，瓦片最快)
let MODE = "composite";   // composite | 2026-07-26 | 2026-07-21 | 2026-06-26
let ROI = "town";         // town（黄埭镇行政边界精确切出） | gee（5km 缓冲区，GEE 对齐）
let MANIFEST = null;      // 静态模式：outputs/manifest.json（列出全部预计算组合）
let COMBO = "composite_town";  // 当前组合键：composite_town / 2026-07-21_town ...
let ALL_BLOOM = {};       // 各组合 bloom.geojson 预加载（用于点位时序点在内判断）
let TREND = null;         // 时序趋势数据：{dates, byRoi:{town,gee}}
let TREND_ROI = "town";
let TREND_METRICS = { water: true, bloom: true, bloomml: false };

const $ = (id) => document.getElementById(id);
const ts = () => "?t=" + REV;
// 由当前 MODE/ROI 推导组合键（与后端 combo_key_of 一致）
function currentComboKey() {
  return (MODE === "composite" ? "composite" : MODE) + "_" + ROI;
}

// ---------- WGS84 -> GCJ-02（高德底图纠偏，保证叠加层对齐） ----------
function outOfChina(lon, lat) {
  return !(lon > 73.66 && lon < 135.05 && lat > 3.86 && lat < 53.55);
}
function _tLat(x, y) {
  let ret = -100 + 2*x + 3*y + 0.2*y*y + 0.1*x*y + 0.2*Math.sqrt(Math.abs(x));
  ret += (20*Math.sin(6*x*Math.PI) + 20*Math.sin(2*x*Math.PI)) * 2/3;
  ret += (20*Math.sin(y*Math.PI) + 40*Math.sin(y/3*Math.PI)) * 2/3;
  ret += (160*Math.sin(y/12*Math.PI) + 320*Math.sin(y*Math.PI/30)) * 2/3;
  return ret;
}
function _tLon(x, y) {
  let ret = 300 + x + 2*y + 0.1*x*x + 0.1*x*y + 0.1*Math.sqrt(Math.abs(x));
  ret += (20*Math.sin(6*x*Math.PI) + 20*Math.sin(2*x*Math.PI)) * 2/3;
  ret += (20*Math.sin(x*Math.PI) + 40*Math.sin(x/3*Math.PI)) * 2/3;
  ret += (150*Math.sin(x/12*Math.PI) + 300*Math.sin(x/30*Math.PI)) * 2/3;
  return ret;
}
function wgs84ToGcj02(lon, lat) {
  if (outOfChina(lon, lat)) return [lon, lat];
  const a = 6378245.0, ee = 0.00669342162296594323;
  let dLat = _tLat(lon - 105.0, lat - 35.0);
  let dLon = _tLon(lon - 105.0, lat - 35.0);
  const radLat = lat / 180.0 * Math.PI;
  let magic = Math.sin(radLat);
  magic = 1 - ee * magic * magic;
  const sqrtMagic = Math.sqrt(magic);
  dLat = (dLat * 180.0) / ((a * (1 - ee)) / (magic * sqrtMagic) * Math.PI);
  dLon = (dLon * 180.0) / (a / sqrtMagic * Math.cos(radLat) * Math.PI);
  return [lon + dLon, lat + dLat];
}
// 当前底图坐标系下的坐标转换（geojson 用）
function toBasemap(lon, lat) {
  return BASEMAP === "gaode" ? wgs84ToGcj02(lon, lat) : [lon, lat];
}
// 把 WGS84 的 bbox(4角) 映射到当前底图坐标系，取外包
function bboxToBasemap(bbox) {
  const c = [
    [bbox[0], bbox[1]], [bbox[2], bbox[1]], [bbox[0], bbox[3]], [bbox[2], bbox[3]],
  ].map(([lo, la]) => toBasemap(lo, la));
  const lons = c.map((p) => p[0]), lats = c.map((p) => p[1]);
  return [[Math.min(...lats), Math.min(...lons)], [Math.max(...lats), Math.max(...lons)]];
}
// geojson 坐标变换回调（leaflet coordsToLatLng: [lon,lat] -> [lat,lon]）
function coordsToLatLng(c) {
  const [lo, la] = toBasemap(c[0], c[1]);
  return [la, lo];
}

// GCJ-02 -> WGS84 逆变换（高德底图下点击坐标需转回 WGS 再与 geojson 比对）
function gcj02ToWgs84(lon, lat) {
  if (outOfChina(lon, lat)) return [lon, lat];
  const a = 6378245.0, ee = 0.00669342162296594323;
  let dLat = _tLat(lon - 105.0, lat - 35.0);
  let dLon = _tLon(lon - 105.0, lat - 35.0);
  const radLat = lat / 180.0 * Math.PI;
  let magic = Math.sin(radLat);
  magic = 1 - ee * magic * magic;
  const sqrtMagic = Math.sqrt(magic);
  dLat = (dLat * 180.0) / ((a * (1 - ee)) / (magic * sqrtMagic) * Math.PI);
  dLon = (dLon * 180.0) / (a / sqrtMagic * Math.cos(radLat) * Math.PI);
  return [lon - dLon, lat - dLat];
}
// 把地图点击的 latlng（已是底图坐标系）转回 WGS84
function clickedToWgs(latlng) {
  const lon = latlng.lng, lat = latlng.lat;
  return BASEMAP === "gaode" ? gcj02ToWgs84(lon, lat) : [lon, lat];
}

// ---------- 点在多边形内（ray casting，WGS84 坐标） ----------
function pointInRing(lon, lat, ring) {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const xi = ring[i][0], yi = ring[i][1], xj = ring[j][0], yj = ring[j][1];
    const hit = ((yi > lat) !== (yj > lat)) &&
      (lon < (xj - xi) * (lat - yi) / (yj - yi) + xi);
    if (hit) inside = !inside;
  }
  return inside;
}
function pointInFeature(lon, lat, f) {
  const g = f.geometry;
  if (!g) return false;
  if (g.type === "Polygon") {
    if (!g.coordinates.length) return false;
    if (pointInRing(lon, lat, g.coordinates[0])) return true;   // 忽略孔洞
  } else if (g.type === "MultiPolygon") {
    for (const poly of g.coordinates) {
      if (poly.length && pointInRing(lon, lat, poly[0])) return true;
    }
  }
  return false;
}
function centroidOf(f) {
  const g = f.geometry;
  let ring = null;
  if (g.type === "Polygon") ring = g.coordinates[0];
  else if (g.type === "MultiPolygon") ring = g.coordinates[0][0];
  if (!ring || !ring.length) return null;
  let x = 0, y = 0;
  for (const p of ring) { x += p[0]; y += p[1]; }
  return [x / ring.length, y / ring.length];
}

// ---------- 初始化 ----------
let STATIC_MODE = false;   // true = 静态托管（GitHub Pages），无 Python 后端

async function loadFlatGeojson() {
  try {
    const [water, bloom, bd] = await Promise.all([
      fetch("outputs/water.geojson" + ts()).then((x) => x.json()),
      fetch("outputs/bloom.geojson" + ts()).then((x) => x.json()),
      fetch("outputs/boundary.json" + ts()).then((x) => x.json()),
    ]);
    waterFC = water; allBloom = bloom; boundaryFC = bd;
  } catch (e) { console.warn("geojson 加载失败", e); }
}

// 静态模式：按组合键拉取对应 result.json + geojson 并重建图层（秒切，无后端）
async function applyCombo(key) {
  COMBO = key;
  const parts = key.split("_");
  MODE = parts[0];                                   // composite 或 日期
  ROI = parts.length > 1 ? parts[1] : "town";
  const [res, water, bloom, bd] = await Promise.all([
    fetch("outputs/combos/" + key + "/result.json" + ts()).then((x) => x.json()),
    fetch("outputs/combos/" + key + "/water.geojson" + ts()).then((x) => x.json()),
    fetch("outputs/combos/" + key + "/bloom.geojson" + ts()).then((x) => x.json()),
    fetch("outputs/boundary.json" + ts()).then((x) => x.json()),   // 行政边界（全组合共用，顶层 outputs）
  ]);
  result = res; waterFC = water; allBloom = bloom; boundaryFC = bd;
  if (result.rev) REV = result.rev;
  rebuildOverlays();          // buildLayers + applyVisibility + renderBloom
  applyResult();
}

// 静态模式：用 manifest 填充 影像模式 / 显示范围 下拉
function populateDropdowns() {
  const md = $("mode"), rs = $("roi");
  if (!md || !rs || !MANIFEST) return;
  const dates = [];
  Object.values(MANIFEST.combos).forEach((c) => {
    if (c.date && !dates.includes(c.date)) dates.push(c.date);
  });
  dates.sort();
  md.innerHTML = "";
  const o0 = document.createElement("option");
  o0.value = "composite"; o0.textContent = "45天合成（最新）"; md.appendChild(o0);
  dates.forEach((d) => {
    const o = document.createElement("option"); o.value = d; o.textContent = d; md.appendChild(o);
  });
  rs.innerHTML = "";
  const ot = document.createElement("option"); ot.value = "town"; ot.textContent = "黄埭镇边界"; rs.appendChild(ot);
  const og = document.createElement("option"); og.value = "gee"; og.textContent = "5km缓冲(GEE)"; rs.appendChild(og);
  const parts = COMBO.split("_");
  md.value = parts[0];
  rs.value = parts.length > 1 ? parts[1] : "town";
}

// 预加载所有组合的 bloom.geojson（点位时序需要跨期比对）
async function loadAllCombos() {
  if (!MANIFEST) return;
  const keys = Object.keys(MANIFEST.combos);
  await Promise.all(keys.map(async (k) => {
    try {
      const fc = await fetch("outputs/combos/" + k + "/bloom.geojson" + ts()).then((x) => x.json());
      ALL_BLOOM[k] = fc;
    } catch (e) { /* 个别组合缺失则跳过 */ }
  }));
}

// ---------- 时序趋势（⑤） ----------
function buildTrend() {
  if (!MANIFEST) return;
  const single = Object.values(MANIFEST.combos).filter((c) => c.date);
  const dates = [...new Set(single.map((c) => c.scene_date))].sort();
  const byRoi = {};
  ["town", "gee"].forEach((roi) => {
    byRoi[roi] = dates.map((d) => {
      const c = single.find((x) => x.roi === roi && x.scene_date === d);
      return c ? { date: d, water: c.water_ha, bloom: c.bloom_ha, bloomml: c.bloom_ml_ha, status: c.status } : null;
    });
  });
  TREND = { dates, byRoi };
}

function renderTrendChart() {
  const host = $("trend-chart"), note = $("trend-note");
  if (!host || !TREND) return;
  const W = 720, H = 300, L = 48, R = 18, T = 18, B = 42;
  const plotW = W - L - R, plotH = H - T - B;
  const data = TREND.byRoi[TREND_ROI].filter((d) => d);
  const n = data.length;
  if (!n) { host.innerHTML = ""; return; }

  const metrics = [
    { key: "water", color: "var(--water)", label: "水体面积" },
    { key: "bloom", color: "var(--bloom)", label: "规则藻华" },
    { key: "bloomml", color: "#ff9500", label: "ML 藻华" },
  ].filter((m) => TREND_METRICS[m.key]);

  let maxV = 1;
  metrics.forEach((m) => data.forEach((d) => { if (d[m.key] > maxV) maxV = d[m.key]; }));
  maxV = Math.ceil(maxV * 1.12);

  const xFor = (i) => n === 1 ? L + plotW / 2 : L + (i / (n - 1)) * plotW;
  const yFor = (v) => T + plotH - (v / maxV) * plotH;

  let svg = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="蓝藻面积时序趋势">`;
  // 网格 + Y 轴标签
  const ticks = 4;
  for (let i = 0; i <= ticks; i++) {
    const v = (maxV / ticks) * i;
    const y = yFor(v);
    svg += `<line class="grid" x1="${L}" y1="${y.toFixed(1)}" x2="${W - R}" y2="${y.toFixed(1)}"/>`;
    svg += `<text class="tick" x="${L - 8}" y="${(y + 3.5).toFixed(1)}" text-anchor="end">${v.toFixed(0)}</text>`;
  }
  svg += `<text class="tick" x="${L - 8}" y="${T - 6}" text-anchor="end" style="font-weight:600">ha</text>`;
  // X 轴标签
  data.forEach((d, i) => {
    svg += `<text class="tick" x="${xFor(i).toFixed(1)}" y="${H - B + 18}" text-anchor="middle">${d.date.slice(5)}</text>`;
  });
  svg += `<line class="axis" x1="${L}" y1="${T + plotH}" x2="${W - R}" y2="${T + plotH}"/>`;
  // 各指标折线 + 点
  metrics.forEach((m) => {
    let pts = "";
    data.forEach((d, i) => { pts += `${xFor(i).toFixed(1)},${yFor(d[m.key]).toFixed(1)} `; });
    svg += `<polyline fill="none" stroke="${m.color}" stroke-width="2.4" stroke-linejoin="round" stroke-linecap="round" points="${pts.trim()}"/>`;
    data.forEach((d, i) => {
      const val = m.key === "water" ? d[m.key].toFixed(1) : d[m.key].toFixed(2);
      let tip;
      if (m.key === "water") {
        tip = `水体面积 ${val} ha\n${d.date}\n口径：当前影像 MNDWI 判水 ∩ JRC 长期出现率≥10% 的水面，已裁至监测范围`;
      } else {
        tip = `${m.label} ${val} ha\n${d.date}`;
      }
      svg += `<circle cx="${xFor(i).toFixed(1)}" cy="${yFor(d[m.key]).toFixed(1)}" r="3.4" fill="#fff" stroke="${m.color}" stroke-width="2"><title>${tip}</title></circle>`;
    });
  });
  svg += `</svg>`;
  host.innerHTML = svg;

  // 说明
  const last = data[data.length - 1];
  const peak = data.reduce((a, b) => (b.bloom > a.bloom ? b : a), data[0]);
  const warn = data.filter((d) => d.status === "预警").length;
  const roiName = TREND_ROI === "town" ? "黄埭镇边界" : "5km缓冲(GEE)";
  note.innerHTML = `监测范围：<b>${roiName}</b> · 共 <b>${n}</b> 期（${data[0].date} ~ ${last.date}）` +
    ` · 规则藻华峰值 <b>${peak.bloom.toFixed(2)} ha</b>（${peak.date}）` +
    ` · <b>${warn}</b> 期判为预警。`;

  const def = $("trend-def");
  if (def) {
    def.innerHTML = `水体面积 = 当前影像下 <b>MNDWI 判水</b> ∩ <b>JRC 长期出现率≥10%</b> 的水面，已裁至监测范围；` +
      `为藻华检测的基底掩膜，逐期跳动反映实际水面变化（非水位、非多年均值）。`;
  }
}

// 点位时序：给定 WGS 经纬度，跨各单景期判断是否落在藻华斑块内
// 按需懒加载某组合的 bloom.geojson（点位时序用），带缓存与容错。
// 点击时才拉取，避免后台预加载（loadAllCombos）失败/未就绪时整功能“全空”。
async function ensureComboBloom(k) {
  if (ALL_BLOOM[k]) return ALL_BLOOM[k];
  try {
    const fc = await fetch("outputs/combos/" + k + "/bloom.geojson" + ts()).then((x) => x.json());
    ALL_BLOOM[k] = fc;
    return fc;
  } catch (e) {
    console.warn("bloom 懒加载失败:", k, e);
    return null;
  }
}

// ---------- 点位命中判定（稳健版） ----------
// 同时用「点在多边形内」与「最近斑块质心距离 ≤ 容差」两种判定，
// 并对 WGS84 点与原始地图坐标（GCJ-02 帧）双坐标系各算一次取最小距离，
// 彻底规避：① 后台预加载失败导致全空；② 高德纠偏方向万一相反导致全部落空；
// ③ 小斑块点不中 polygon 边缘的问题。
function haversineM(lon1, lat1, lon2, lat2) {
  const R = 6371000;
  const r1 = lat1 * Math.PI / 180, r2 = lat2 * Math.PI / 180;
  const dLat = (lat2 - lat1) * Math.PI / 180, dLon = (lon2 - lon1) * Math.PI / 180;
  const a = Math.sin(dLat / 2) ** 2 + Math.cos(r1) * Math.cos(r2) * Math.sin(dLon / 2) ** 2;
  return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}
const POINT_TOL_M = 800;   // 点位命中容差（米）：覆盖坐标纠偏偏移 + 斑块尺寸

async function pointBloomSeries(lon, lat, altLon, altLat) {
  const out = [];
  if (!MANIFEST) return out;
  const keys = Object.keys(MANIFEST.combos)
    .filter((k) => MANIFEST.combos[k].date && MANIFEST.combos[k].roi === ROI);
  keys.sort((a, b) => MANIFEST.combos[a].scene_date.localeCompare(MANIFEST.combos[b].scene_date));
  // 按需懒加载缺失组合（容错），不再依赖后台预加载 ALL_BLOOM
  await Promise.all(keys.map((k) => ensureComboBloom(k)));
  keys.forEach((k) => {
    const fc = ALL_BLOOM[k], meta = MANIFEST.combos[k];
    let hit = null, minD = Infinity;
    if (fc && fc.features) {
      for (const f of fc.features) {
        const insidePoly =
          pointInFeature(lon, lat, f) ||
          (altLon != null && pointInFeature(altLon, altLat, f));
        // 最近质心距离（双坐标系取小）
        const c = centroidOf(f);
        let d = Infinity;
        if (c) {
          const dWgs = haversineM(lon, lat, c[0], c[1]);
          const dAlt = altLon != null ? haversineM(altLon, altLat, c[0], c[1]) : Infinity;
          d = Math.min(dWgs, dAlt);
        }
        if (insidePoly || (c && d <= POINT_TOL_M)) {
          if (d < minD) { minD = d; hit = f; }
        }
      }
    }
    const inside = !!hit;
    out.push({
      date: meta.scene_date,
      inside,
      area: inside ? (hit.properties.area_ha || 0) : 0,
      ndci: inside && hit.properties.mean != null ? hit.properties.mean : null,
      status: meta.status,
      dist: hit ? minD : null,
    });
  });
  return out;
}

// 迷你时序 sparkline（点在多边形内命中率）
function sparkSVG(series) {
  const W = 248, H = 70, L = 6, R = 6, T = 8, B = 8;
  const plotW = W - L - R, plotH = H - T - B;
  const n = series.length;
  if (!n) return "";
  // 点位时序画"是否藻华 + NDCI 强度"，不画 patch 面积（点位尺度无意义且≈0）
  const FLOOR = 0.15;                                  // 命中但缺 NDCI 时的可见高度
  const valOf = (s) => (s.inside ? (s.ndci != null ? s.ndci : FLOOR) : 0);
  let maxV = 0.2;
  series.forEach((s) => { const v = valOf(s); if (v > maxV) maxV = v; });
  maxV = Math.ceil(maxV * 1.15 * 10) / 10;             // 留 15% 余量，按 0.1 取整
  const xFor = (i) => n === 1 ? L + plotW / 2 : L + (i / (n - 1)) * plotW;
  const yFor = (v) => T + plotH - (v / maxV) * plotH;
  let svg = `<svg viewBox="0 0 ${W} ${H}" aria-label="点位藻华历史">`;
  let line = "";
  series.forEach((s, i) => { line += `${xFor(i).toFixed(1)},${yFor(valOf(s)).toFixed(1)} `; });
  svg += `<polyline fill="none" stroke="var(--bloom)" stroke-width="2" stroke-linejoin="round" points="${line.trim()}"/>`;
  series.forEach((s, i) => {
    if (s.inside) {
      svg += `<circle cx="${xFor(i).toFixed(1)}" cy="${yFor(valOf(s)).toFixed(1)}" r="3.6" fill="var(--bloom)"/>`;
    } else {
      svg += `<circle cx="${xFor(i).toFixed(1)}" cy="${yFor(0).toFixed(1)}" r="2.6" fill="none" stroke="rgba(0,0,0,.25)" stroke-width="1.4"/>`;
    }
  });
  svg += `</svg>`;
  return svg;
}

function popupHeader(color, title) {
  return `<h4><span class="dot" style="background:${color}"></span>${title}</h4>`;
}

// 统一点位查询弹窗：点击藻华/水体斑块或地图空白处都走这里。
// patch: 可选 {feature, name, color}，用于展示斑块自身面积/NDCI；为空则只做点位时序。
// 关键：先开“查询中”弹窗，再 await 跨期查询（按需懒加载缺失组合），避免白屏 / 依赖后台预加载。
async function showPointQuery(latlng, patch) {
  const [lon, lat] = clickedToWgs(latlng);
  const headColor = patch ? patch.color : "var(--accent)";
  const title = patch ? (patch.name + " 斑块") : "点位查询";
  const roiName = ROI === "town" ? "黄埭镇边界" : "5km缓冲(GEE)";
  const patchBlock = (pf) => {
    if (!pf) return "";
    const area = (pf.area_ha || 0);
    const mean = pf.mean;
    return `<div class="kv"><span>面积</span><b>${area.toFixed(2)} ha</b></div>` +
           `<div class="kv"><span>平均 NDCI</span><b>${mean != null ? mean.toFixed(3) : "—"}</b></div>`;
  };
  // 1) 立即开“查询中”弹窗
  const pop = L.popup({ className: "glass-pop", maxWidth: 280 }).setLatLng(latlng).openOn(map);
  pop.setContent(
    popupHeader(headColor, title) +
    patchBlock(patch && patch.feature ? patch.feature.properties : null) +
    `<div class="kv"><span>坐标 (WGS84)</span><b>${lon.toFixed(4)}, ${lat.toFixed(4)}</b></div>` +
    `<div class="kv"><span>各期检出</span><b>查询中…</b></div>`
  );
  // 2) 跨期查询（按需加载缺失组合）；alt 为原始地图坐标，双坐标系兜底纠偏方向
  const alt = [latlng.lng, latlng.lat];
  const series = await pointBloomSeries(lon, lat, alt[0], alt[1]);
  const hitN = series.filter((s) => s.inside).length;
  const cap = patch
    ? `该点位各期藻华：${hitN}/${series.length} 期检出（范围：${roiName}）`
    : `该点位藻华历史（范围：${roiName}）· ● 检出/○ 未检出，高度=NDCI强度`;
  let nearestNote = "";
  if (!patch && hitN === 0) {
    let md = Infinity, mdDate = "";
    series.forEach((s) => { if (s.dist != null && s.dist < md) { md = s.dist; mdDate = s.date; } });
    if (md < Infinity) nearestNote = `<div class="kv"><span>最近藻华</span><b>约 ${Math.round(md)} m（${mdDate}）</b></div>`;
  }
  pop.setContent(
    popupHeader(headColor, title) +
    patchBlock(patch && patch.feature ? patch.feature.properties : null) +
    `<div class="kv"><span>坐标 (WGS84)</span><b>${lon.toFixed(4)}, ${lat.toFixed(4)}</b></div>` +
    `<div class="kv"><span>各期检出</span><b>${hitN}/${series.length} 期</b></div>` +
    nearestNote +
    `<div class="pop-spark"><div class="cap">${cap}</div>${series.length ? sparkSVG(series) : "<div class=\"cap\">暂无多期数据</div>"}</div>`
  );
  pop.update();
}

async function init() {
  const bbox = DEFAULT_BBOX;
  const center = [(bbox[1] + bbox[3]) / 2, (bbox[0] + bbox[2]) / 2];

  // 1) 创建地图容器（必须最先，否则图层无处挂载 -> 页面一直转圈）
  map = L.map("leaflet", { zoomControl: true, attributionControl: false, preferCanvas: true })
        .setView(center, 13);
  L.control.attribution({ position: "bottomleft", prefix: false }).addTo(map);
  setBasemap(BASEMAP);

  // 2) 开发模式：优先 /api/status（本地 Flask 式服务）
  let devResult = null;
  try {
    const r = await fetch("/api/status");
    const j = await r.json();
    if (j && j.ready) devResult = j;
  } catch (e) { /* 非开发模式 */ }

  if (devResult) {
    result = devResult; STATIC_MODE = false;
    await loadFlatGeojson();
    if (result.rev) REV = result.rev;
    buildLayers(); applyResult(); applyVisibility(); renderBloom(parseFloat($("ndci-slider").value));
    $("map-loading").classList.add("hidden");
    wireControls();
    return;
  }

  // 3) 静态模式：manifest 驱动多组合切换
  try {
    const m = await fetch("outputs/manifest.json" + "?t=" + Date.now());
    MANIFEST = await m.json();
    STATIC_MODE = true;
  } catch (e) { console.warn("manifest 加载失败，回退扁平 result", e); }

  if (MANIFEST) {
    REV = MANIFEST.built_at ? Math.floor(Date.parse(MANIFEST.built_at) / 1000) : Date.now();
    COMBO = MANIFEST.default || "composite_town";
    populateDropdowns();
    await applyCombo(COMBO);
    buildTrend();
    renderTrendChart();
    loadAllCombos();                 // 预加载各期 bloom（点位时序用，后台）
  } else {
    // 4) 兜底：旧扁平 result.json
    try {
      const r2 = await fetch("outputs/result.json" + "?t=" + Date.now());
      const j = await r2.json();
      if (j && j.ready) { result = j; STATIC_MODE = true; }
    } catch (e) { /* 保持 null */ }
    await loadFlatGeojson();
    if (result && result.rev) REV = result.rev;
    COMBO = currentComboKey();
    buildLayers(); applyResult(); applyVisibility(); renderBloom(parseFloat($("ndci-slider").value));
  }

  map.on("click", (e) => showPointQuery(e.latlng));   // 点击空白：点位时序查询
  $("map-loading").classList.add("hidden");
  wireControls();
}

// ---------- 底图切换（含高德 GCJ-02 纠偏） ----------
let baseLayer = null;
function setBasemap(name) {
  BASEMAP = name;
  if (baseLayer && map) map.removeLayer(baseLayer);
  let url, attr, opt = { maxZoom: 19 };
  if (name === "gaode") {
    url = "https://webst0{s}.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}";
    opt = { maxZoom: 19, subdomains: "1234" };
    attr = "© 高德 AutoNavi";
  } else if (name === "osm") {
    url = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png";
    attr = "© OpenStreetMap";
  } else {
    url = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}";
    attr = "© Esri";
  }
  baseLayer = L.tileLayer(url, Object.assign({ attribution: attr }, opt)).addTo(map);
  // 坐标系变了 -> 重建叠加层以保持对齐
  if (Object.keys(layers).length) rebuildOverlays();
}

// ---------- 构建图层 ----------
function buildLayers() {
  layers = {};
  COMBO = currentComboKey();   // 始终由当前 MODE/ROI 推导
  const b = bboxToBasemap((result && result.bbox) || DEFAULT_BBOX);
  const wb = (result && result.bbox) || DEFAULT_BBOX;
  // 目录式 GCJ 变体（静态服务器忽略 query，故用目录区分）：高德底图用 tiles_gcj/，其余用 tiles/
  const tBase = (BASEMAP === "gaode" ? "tiles_gcj/" : "tiles/");
  const tUrl = (n) => tBase + COMBO + "/" + n + "/{z}/{x}/{y}" + (n === "rgb" ? ".webp" : ".png") + "?v=" + REV;
  const tOpt = { bounds: [[wb[1], wb[0]], [wb[3], wb[2]]], noWrap: true, minZoom: 10, maxZoom: 17, keepBuffer: 2 };
  layers.rgb  = L.tileLayer(tUrl("rgb"),  Object.assign({ opacity: 1.0,  zIndex: 10 }, tOpt));
  layers.ndci = L.tileLayer(tUrl("ndci"), Object.assign({ opacity: 0.7,  zIndex: 30 }, tOpt));
  layers.old  = L.tileLayer(tUrl("old"),  Object.assign({ opacity: 0.6,  zIndex: 20 }, tOpt));
  // ML 藻华检测（Otsu 自适应阈值 + CMI 水草掩膜）：二值红 + 置信度热图
  layers.bloomml  = L.tileLayer(tUrl("bloomml"),  Object.assign({ opacity: 0.55, zIndex: 42 }, tOpt));
  layers.bloommlp = L.tileLayer(tUrl("bloommlp"), Object.assign({ opacity: 0.70, zIndex: 38 }, tOpt));
  layers.water = L.geoJSON(waterFC || { type: "FeatureCollection", features: [] }, {
    coordsToLatLng,
    style: { color: "#1e90ff", weight: 1.5, fillColor: "#1e90ff", fillOpacity: 0.30, opacity: 0.95 },
  });
  // boundary.json 是自定义格式 {outer:[[lon,lat]...]}，需转成 GeoJSON 再交给 Leaflet
  let boundaryData = { type: "FeatureCollection", features: [] };
  if (boundaryFC && boundaryFC.outer) {
    const ring = boundaryFC.outer.slice();
    const a = ring[0], z = ring[ring.length - 1];
    if (a[0] !== z[0] || a[1] !== z[1]) ring.push(a);   // 闭合环
    boundaryData = {
      type: "FeatureCollection",
      features: [{ type: "Feature", properties: { name: boundaryFC.name || "行政边界" },
                   geometry: { type: "Polygon", coordinates: [ring] } }],
    };
  } else if (boundaryFC && boundaryFC.type) {
    boundaryData = boundaryFC;   // 已是 GeoJSON
  }
  layers.boundary = L.geoJSON(boundaryData, {
    coordsToLatLng,
    style: { color: "#ffd000", weight: 2, fill: false, opacity: 0.9 },
  });
  // bloom 单独维护（受阈值过滤）
  layers.bloom = L.geoJSON({ type: "FeatureCollection", features: [] }, {
    coordsToLatLng,
    style: { color: "#ff3b30", weight: 1.2, fillColor: "#ff3b30", fillOpacity: 0.45, opacity: 0.95 },
  }).addTo(map);
  // 点击藻华 / 水体斑块：属性 + 点位时序（e.layer.feature 在 canvas 模式下可能为空，故容错）
  layers.bloom.on("click", (e) => {
    if (e.originalEvent) L.DomEvent.stopPropagation(e.originalEvent);
    const pf = e.layer && e.layer.feature ? e.layer.feature : null;
    showPointQuery(e.latlng, pf ? { feature: pf, name: "藻华", color: "var(--bloom)" } : null);
  });
  layers.water.on("click", (e) => {
    if (e.originalEvent) L.DomEvent.stopPropagation(e.originalEvent);
    const pf = e.layer && e.layer.feature ? e.layer.feature : null;
    showPointQuery(e.latlng, pf ? { feature: pf, name: "水体", color: "var(--water)" } : null);
  });
}

// ---------- 重建所有叠加层（底图坐标系变化时） ----------
function rebuildOverlays() {
  [layers.rgb, layers.ndci, layers.old, layers.water, layers.boundary,
   layers.bloom, layers.bloomml, layers.bloommlp]
    .forEach((l) => { if (l && map.hasLayer(l)) map.removeLayer(l); });
  buildLayers();
  applyVisibility();
  renderBloom(parseFloat($("ndci-slider").value));
}

// ---------- 藻华阈值过滤 ----------
function renderBloom(thr) {
  if (!allBloom) return;
  const feats = allBloom.features.filter((f) => (f.properties.mean ?? -1) >= thr);
  const area = feats.reduce((s, f) => s + (f.properties.area_ha || 0), 0);
  layers.bloom.clearLayers();
  L.geoJSON({ type: "FeatureCollection", features: feats }, {
    coordsToLatLng,
    style: { color: "#ff3b30", weight: 1.2, fillColor: "#ff3b30", fillOpacity: 0.45, opacity: 0.95 },
  }).addTo(layers.bloom);
  $("bloom-filtered").textContent = area.toFixed(2);
  $("s-bloom").textContent = area.toFixed(area < 10 ? 2 : 1);
}

// ---------- 结果填充 ----------
function applyResult() {
  if (!result) {
    $("nav-meta").textContent = "数据：—";
    return;
  }
  const nsc = result.n_scenes || 1;
  const compTxt = result.composite ? (" · median " + nsc + " 景合成") : " · 单景";
  const roiTxt = result.roi === "huangdi_town" ? " · 黄埭镇边界" : " · 5km缓冲(GEE)";
  const gee = ("gee_water_ha" in result)
    ? (" · GEE缓冲对齐: 水" + result.gee_water_ha + "/藻" + result.gee_bloom_ha + "ha") : "";
  $("nav-meta").textContent = "数据：" + result.date + compTxt + roiTxt + " · 云量 " + result.cloud + "%" + gee;
  $("s-water").textContent = result.water_ha;
  $("s-date").textContent = result.date;
  $("s-cloud").textContent = "云量 " + result.cloud + "%";
  const mlArea = $("bloomml-area");
  if (mlArea && "bloom_ml_ha" in result) mlArea.textContent = result.bloom_ml_ha;
  const st = $("s-status");
  st.textContent = result.status;
  st.className = "stat-value " + (result.status === "预警" ? "alert" : "ok");
  $("s-status-sub").textContent = result.status === "预警" ? "疑似蓝藻暴发" : "水体较清洁";
}

// ---------- 图层显隐 ----------
function applyVisibility() {
  document.querySelectorAll('input[data-layer]').forEach((inp) => {
    const ly = layers[inp.dataset.layer];
    if (!ly) return;
    if (inp.checked) { if (!map.hasLayer(ly)) ly.addTo(map); }
    else { if (map.hasLayer(ly)) map.removeLayer(ly); }
  });
}

// ---------- 交互绑定 ----------
function wireControls() {
  document.querySelectorAll('input[data-layer]').forEach((inp) => {
    inp.addEventListener("change", applyVisibility);
  });
  const slider = $("ndci-slider");
  const setFill = () => slider.style.setProperty("--fill", (slider.value / slider.max * 100) + "%");
  setFill();
  slider.addEventListener("input", () => {
    $("ndci-val").textContent = parseFloat(slider.value).toFixed(2);
    setFill();
    renderBloom(parseFloat(slider.value));
  });
  $("btn-rerun").addEventListener("click", rerun);
  const md = $("mode");
  if (md) {
    md.value = MODE;
    md.addEventListener("change", () => {
      MODE = md.value;
      if (STATIC_MODE) applyCombo(currentComboKey()); else rerun();
    });
  }
  const rs = $("roi");
  if (rs) {
    rs.value = ROI;
    rs.addEventListener("change", () => {
      ROI = rs.value;
      if (STATIC_MODE) applyCombo(currentComboKey()); else rerun();
    });
  }
  const bm = $("basemap");
  if (bm) { bm.value = BASEMAP; bm.addEventListener("change", () => setBasemap(bm.value)); }
  // 时序趋势：监测范围 / 指标切换
  const roiBox = $("trend-roi");
  if (roiBox && TREND) {
    roiBox.querySelectorAll(".seg-btn").forEach((b) => {
      b.addEventListener("click", () => {
        roiBox.querySelectorAll(".seg-btn").forEach((x) => x.classList.remove("active"));
        b.classList.add("active");
        TREND_ROI = b.dataset.roi;
        renderTrendChart();
      });
    });
  }
  const metricBox = $("trend-metrics");
  if (metricBox && TREND) {
    metricBox.querySelectorAll(".chip").forEach((c) => {
      c.addEventListener("click", () => {
        const m = c.dataset.m;
        TREND_METRICS[m] = !TREND_METRICS[m];
        c.classList.toggle("active", TREND_METRICS[m]);
        renderTrendChart();
      });
    });
  }
  // 静态托管模式：无 Python 后端，「重新分析」禁用；但 影像模式/显示范围 下拉
  // 已变为「即时切换预计算组合」，保持可用（applyCombo 秒切不同瓦片集）。
  if (STATIC_MODE) {
    const rb = $("btn-rerun");
    if (rb) { rb.disabled = true; rb.style.opacity = ".55"; rb.textContent = "重新分析（需后端）"; }
    const rh = $("rerun-hint");
    if (rh) rh.textContent = "静态展示版 · 多组合已预切片，下拉即时切换；数据每日自动更新";
  }
}

// ---------- 重新分析（服务端；静态模式下禁用） ----------
async function rerun() {
  if (STATIC_MODE) {
    $("rerun-hint").textContent = "静态展示版：数据每日自动更新，交互重算需部署后端。";
    return;
  }
  const btn = $("btn-rerun");
  btn.disabled = true;
  $("rerun-hint").textContent = "正在运行哨兵二号流水线…（首次约十几秒）";
  $("map-loading").classList.remove("hidden");
  try {
    const thr = parseFloat($("ndci-slider").value);
    let url = "/api/analyze?ndci=" + thr;
    if (MODE !== "composite") url += "&date=" + encodeURIComponent(MODE);
    if (ROI !== "town") url += "&roi=" + encodeURIComponent(ROI);
    const r = await fetch(url);
    const data = await r.json();
    if (data.error) throw new Error(data.error);
    result = data;
    if (data.rev) REV = data.rev;   // 新结果：刷新资源缓存
    const bbox = result.bbox || DEFAULT_BBOX;
    const b = [[bbox[1], bbox[0]], [bbox[3], bbox[2]]];
    // 重新拉取最新矢量 + 影像
    const [water, bloom] = await Promise.all([
      fetch("outputs/water.geojson" + ts()).then((x) => x.json()),
      fetch("outputs/bloom.geojson" + ts()).then((x) => x.json()),
    ]);
    waterFC = water; allBloom = bloom;
    // 重建所有图层（并更新影像缓存）
    [layers.rgb, layers.ndci, layers.old, layers.water, layers.boundary,
     layers.bloom, layers.bloomml, layers.bloommlp]
      .forEach((l) => { if (map.hasLayer(l)) map.removeLayer(l); });
    buildLayers();
    applyResult();
    applyVisibility();
    renderBloom(parseFloat($("ndci-slider").value));
    const mlTxt = ("bloom_ml_ha" in result) ? (" · ML 藻华 " + result.bloom_ml_ha + " ha") : "";
    const roiLbl = result.roi === "huangdi_town" ? "黄埭镇边界" : "5km缓冲(GEE)";
    $("rerun-hint").textContent = "完成[" + roiLbl + "] · 水体 " + result.water_ha + " ha / 规则藻华 "
      + result.bloom_ha + " ha" + mlTxt;
  } catch (e) {
    $("rerun-hint").textContent = "分析失败：" + e.message;
  } finally {
    btn.disabled = false;
    $("map-loading").classList.add("hidden");
  }
}

// ---------- 滚动动画 ----------
function setupReveal() {
  const io = new IntersectionObserver((es) => {
    es.forEach((e) => { if (e.isIntersecting) e.target.classList.add("in"); });
  }, { threshold: 0.12 });
  document.querySelectorAll(".reveal").forEach((el) => io.observe(el));
}

document.addEventListener("DOMContentLoaded", () => { init(); setupReveal(); });
