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
  const [res, water, bloom] = await Promise.all([
    fetch("outputs/combos/" + key + "/result.json" + ts()).then((x) => x.json()),
    fetch("outputs/combos/" + key + "/water.geojson" + ts()).then((x) => x.json()),
    fetch("outputs/combos/" + key + "/bloom.geojson" + ts()).then((x) => x.json()),
  ]);
  result = res; waterFC = water; allBloom = bloom;
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
  const tUrl = (n) => tBase + COMBO + "/" + n + "/{z}/{x}/{y}.png?v=" + REV;
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
