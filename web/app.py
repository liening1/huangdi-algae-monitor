# -*- coding: utf-8 -*-
"""
黄埭镇蓝藻卫星监控系统 · 交互式网站后端
======================================
把「哨兵二号(Sentinel-2) 水体边界精修 + 蓝藻多指数检测」流水线包装成
一个极简、零额外依赖的 Web 服务（仅用 Python 标准库 http.server）。

- 通过 importlib 直接复用已验证的 S2 处理脚本 S2_水体边界_黄埭镇.py
- /api/status   返回最近一次分析结果 (outputs/result.json)
- /api/analyze  按阈值/日期重跑流水线，生成新的 PNG / GeoJSON / result.json
- 其余路径作为静态文件服务 (前端 + outputs)

运行:
  cd /Users/shiyusheng/Documents/黄棣镇蓝藻卫星监控系统
  PYTHONPATH=/tmp/pylibs GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR CPL_VSIL_CURL_USE_HEAD=NO \
      /usr/bin/python3 web/app.py
"""
import importlib.util, os, json, math, base64, io, sys, re, shutil, time
import rasterio
from rasterio.features import rasterize as _rasterize
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import numpy as np

# ---------- 路径 ----------
HERE   = os.path.dirname(os.path.abspath(__file__))
ROOT   = os.path.dirname(HERE)                 # 项目根目录
OUT    = os.path.join(HERE, "outputs")
STATIC = os.path.join(HERE, "static")
os.makedirs(OUT, exist_ok=True)

# ---------- 载入已验证的 S2 流水线 (中文文件名脚本) ----------
spec = importlib.util.spec_from_file_location(
    "s2pipe", os.path.join(ROOT, "S2_水体边界_黄埭镇.py"))
s2pipe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s2pipe)
P = s2pipe  # alias

# ---------- 机器学习藻华检测（半监督随机森林） ----------
import sys as _sys
_sys.path.insert(0, ROOT)
try:
    import bloom_ml as ML
except Exception as _e:
    ML = None
    print("[ml] 未启用（", _e, "）")


CENTER_LON, CENTER_LAT = P.CENTER_LON, P.CENTER_LAT
TARGET_RES = P.TARGET_RES
BUFFER_M = 5000   # ★ 与 GEE 参考版 GEE_S2_蓝藻_黄埭镇.py 完全一致：roi = point.buffer(5000)

# 5km 缓冲区圆形 ROI（与 GEE clip(roi) 一致）的 bbox
_dlat = BUFFER_M / 110540.0
_dlon = BUFFER_M / (111320.0 * math.cos(math.radians(CENTER_LAT)))
_buffer_bbox = (round(CENTER_LON - _dlon, 6), round(CENTER_LAT - _dlat, 6),
                round(CENTER_LON + _dlon, 6), round(CENTER_LAT + _dlat, 6))

# ★ 分析网格 = 黄埭镇行政边界 bbox ∪ 5km 缓冲 bbox（两者都完整落入，避免裁剪镇域西/东缘）
import json as _json
_bd = _json.load(open(os.path.join(ROOT, "黄埭镇边界.json"), encoding="utf-8"))
_olons = [p[0] for p in _bd["outer"]]; _olats = [p[1] for p in _bd["outer"]]
_bd_bbox = (min(_olons), min(_olats), max(_olons), max(_olats))
BBOX = (min(_bd_bbox[0], _buffer_bbox[0]), min(_bd_bbox[1], _buffer_bbox[1]),
        max(_bd_bbox[2], _buffer_bbox[2]), max(_bd_bbox[3], _buffer_bbox[3]))
print(f"[grid] 分析网格 bbox={BBOX}  (镇域{_bd_bbox} ∪ 5km缓冲{_buffer_bbox})")

# ---------- 模块级：栅格网格 / 墨卡托边界 / 高德 GCJ 偏移 ----------
NCOL = int(round((BBOX[2]-BBOX[0])*111320*math.cos(math.radians(CENTER_LAT))/TARGET_RES))
NROW = int(round((BBOX[3]-BBOX[1])*110540/TARGET_RES))
DST_TRANSFORM = P.fb(BBOX[0], BBOX[1], BBOX[2], BBOX[3], NCOL, NROW)
DST_SHAPE = (NROW, NCOL)

# ★ 5km 缓冲区圆形 ROI 掩膜（与 GEE clip(roi) 一致；行政边界仅作黄色参考线，见前端）
def _buffer_geom():
    pts = []
    for k in range(96):
        a = 2 * math.pi * k / 96
        dlat = BUFFER_M * math.sin(a) / 110540.0
        dlon = BUFFER_M * math.cos(a) / (111320.0 * math.cos(math.radians(CENTER_LAT)))
        pts.append([CENTER_LON + dlon, CENTER_LAT + dlat])
    pts.append(pts[0])
    return {'type': 'Polygon', 'coordinates': [pts]}
ROI_GEOM = _buffer_geom()
ROI_MASK = _rasterize([(ROI_GEOM, 1)], out_shape=DST_SHAPE, transform=DST_TRANSFORM, fill=0).astype(bool)

# ★ 黄埭镇行政边界（OSM relation 7763011）—— 权威多边形，用于"精确切出镇域"显示
def _town_geom():
    import json as _json
    bd = _json.load(open(os.path.join(ROOT, "黄埭镇边界.json"), encoding="utf-8"))
    outer = [[p[0], p[1]] for p in bd["outer"]]
    if outer[0] != outer[-1]:
        outer = outer + [outer[0]]
    return {"type": "Polygon", "coordinates": [outer]}
BD_GEOM = _town_geom()
BD_MASK = _rasterize([(BD_GEOM, 1)], out_shape=DST_SHAPE, transform=DST_TRANSFORM, fill=0).astype(bool)
BD_AREA_HA = round(float(BD_MASK.sum()) * TARGET_RES * TARGET_RES / 1e4, 1)  # 镇域面积约 49 km²

# 多组合瓦片：按 comboKey 缓存已计算的 combo（开发服务器按需渲染 / 静态构建复用）
COMBOS = {}

_RM = 6378137.0
def _mx(lon): return math.radians(lon) * _RM
def _my(lat): return math.log(math.tan(math.pi/4 + math.radians(lat)/2)) * _RM

def _stretch_safe(rgb, pmin=10, pmax=98):
    """真彩拉伸（pmin=10 而非默认 2）：避免把插值区/暗区压成纯黑"""
    out = rgb.astype(np.float32)
    for i in range(3):
        lo, hi = np.percentile(rgb[i], (pmin, pmax))
        if hi > lo:
            out[i] = np.clip((rgb[i] - lo) / (hi - lo), 0, 1)
    return (out * 255).clip(0, 255).astype(np.uint8)
BBOX_MERC = (_mx(BBOX[0]), _my(BBOX[1]), _mx(BBOX[2]), _my(BBOX[3]))

def _gcj_shift(lon, lat):
    """与前端 wgs84ToGcj02 完全一致的 WGS84->GCJ-02 偏移（用于高德瓦片对齐）。
    ★ 修复：必须先减去基准点 (105°E, 35°N)，与前端 app.js:61-62 一致。
    之前漏减导致偏移量误差 ~2.2km（1950m 经度 + 1109m 纬度），影像整体偏东北。"""
    if not (73.66 < lon < 135.05 and 3.86 < lat < 53.55):
        return 0.0, 0.0
    # ★ 与前端一致：相对 (105°E, 35°N) 的偏移多项式
    x, y = lon - 105.0, lat - 35.0
    a = 6378245.0; ee = 0.00669342162296594323
    dLat = -100 + 2*x + 3*y + 0.2*y*y + 0.1*x*y + 0.2*math.sqrt(abs(x))
    dLat += (20*math.sin(6*x*math.pi) + 20*math.sin(2*x*math.pi))*2/3
    dLat += (20*math.sin(y*math.pi) + 40*math.sin(y/3*math.pi))*2/3
    dLat += (160*math.sin(y/12*math.pi) + 320*math.sin(y*math.pi/30))*2/3
    dLon = 300 + x + 2*y + 0.1*x*x + 0.1*x*y + 0.1*math.sqrt(abs(x))
    dLon += (20*math.sin(6*x*math.pi) + 20*math.sin(2*x*math.pi))*2/3
    dLon += (20*math.sin(x*math.pi) + 40*math.sin(x/3*math.pi))*2/3
    dLon += (150*math.sin(x/12*math.pi) + 300*math.sin(x/30*math.pi))*2/3
    radLat = lat/180.0*math.pi
    magic = math.sin(radLat); magic = 1 - ee*magic*magic
    sqrtMagic = math.sqrt(magic)
    dLat = (dLat*180.0)/((a*(1-ee))/(magic*sqrtMagic)*math.pi)
    dLon = (dLon*180.0)/(a/sqrtMagic*math.cos(radLat)*math.pi)
    return dLon, dLat

_cx, _cy = (BBOX[0]+BBOX[2])/2, (BBOX[1]+BBOX[3])/2
GCJ_DLON, GCJ_DLAT = _gcj_shift(_cx, _cy)

# =====================================================================
# 核心分析函数 —— 直接复用 s2pipe 的已验证函数
# =====================================================================
def _load_bands(item, date_tag):
    """读取 S2 波段 (缓存 / 否则从 AWS COG 下载)。返回 dict。"""
    dst_transform = P.fb(BBOX[0], BBOX[1], BBOX[2], BBOX[3],
                         int(round((BBOX[2]-BBOX[0])*111320*math.cos(math.radians(CENTER_LAT))/TARGET_RES)),
                         int(round((BBOX[3]-BBOX[1])*110540/TARGET_RES)))
    dst_shape = (int(round((BBOX[3]-BBOX[1])*110540/TARGET_RES)),
                 int(round((BBOX[2]-BBOX[0])*111320*math.cos(math.radians(CENTER_LAT))/TARGET_RES)))
    dst_bounds = BBOX
    cache = f"/tmp/s2_bands_{date_tag}.npz"
    if os.path.exists(cache):
        d = np.load(cache)
        return dict(zip(['B02','B03','B04','B05','B06','B08','B11','SCL'],
                        [d[k] for k in ['B02','B03','B04','B05','B06','B08','B11','SCL']])), \
               dst_transform, dst_shape, dst_bounds
    A = item['assets']
    hrefs = {k: A[k]['href'] for k in ['blue','green','red','rededge1','rededge2','nir','swir16','scl']}
    def rb(url, resampling=P.Resampling.bilinear, scale=10000.0):
        a = P.read_band_window(url, dst_transform, dst_shape, 'EPSG:4326', resampling, dst_bounds)
        return a/scale if scale else a
    bands = {
        'B02': rb(hrefs['blue']), 'B03': rb(hrefs['green']), 'B04': rb(hrefs['red']),
        'B05': rb(hrefs['rededge1']), 'B06': rb(hrefs['rededge2']), 'B08': rb(hrefs['nir']),
        'B11': rb(hrefs['swir16']), 'SCL': rb(hrefs['scl'], P.Resampling.nearest, 0).astype(int),
    }
    # ★ 无数据掩膜：S2 COG 在图幅外/缺失区常返回反射率 0（而非 NaN），
    #   median 后仍是 0 → 拉伸成纯黑，造成底图边角“黑块/分裂”。
    #   这里把 0(及负)统一转 NaN，使无效区能被正确识别并在瓦片渲染中透明处理。
    for k in ['B02', 'B03', 'B04', 'B05', 'B06', 'B08', 'B11']:
        bands[k][bands[k] <= 0] = np.nan
    save = {k: bands[k] for k in ['B02','B03','B04','B05','B06','B08','B11','SCL']}
    np.savez(cache, **save)
    return bands, dst_transform, dst_shape, dst_bounds

# ---------- 45 天 median 合成（与 GEE 参考版 GEE_S2_蓝藻_黄埭镇.py 完全一致） ----------
STAC_BASE = "https://earth-search.aws.element84.com/v1/collections/sentinel-2-l2a/items"

def _query_stac(start, end):
    api = (STAC_BASE + f"?bbox={BBOX[0]},{BBOX[1]},{BBOX[2]},{BBOX[3]}"
           f"&datetime={start}T00:00:00Z/{end}T23:59:59Z&limit=100")
    for attempt in range(4):
        try:
            feats = json.load(urllib_request(api))["features"]
            return [f for f in feats if float(f["properties"]["eo:cloud_cover"]) < 60]
        except Exception as e:
            if "429" in str(e) and attempt < 3:
                wait = 10 * (attempt + 1)
                print(f"  [stac retry {attempt+1}/4] 429 限流，等待 {wait}s...")
                time.sleep(wait); continue
            raise

_COMPOSITE_CACHE = {}   # (start,end) -> (comp, meta)，避免多 ROI 重复拉取合成

def _build_composite():
    """45 天窗口内多景 median 合成（GEE: s2.median().clip(roi)）。
    逐景用 SCL 做云掩膜（清晰像元 = 非 SCL_MASK = {4,5,6,7}），再对所有景做
    per-pixel 中位数（nanmedian 自动跳过被云掩掉的 NaN 像元）。"""
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    start = (now - _dt.timedelta(days=45)).strftime('%Y-%m-%d')
    end = now.strftime('%Y-%m-%d')
    if (start, end) in _COMPOSITE_CACHE:
        return _COMPOSITE_CACHE[(start, end)]
    feats = _query_stac(start, end)
    extra = 0
    while len(feats) < 3 and extra <= 90:      # GEE MIN_SCENES = 3
        extra += 30
        sw = (now - _dt.timedelta(days=45 + extra)).strftime('%Y-%m-%d')
        feats = _query_stac(sw, end)
    feats = sorted(feats, key=lambda x: x["properties"]["datetime"])
    if not feats:
        raise RuntimeError("45 天窗口内无可用 S2 场景（云量过高）")
    latest = feats[-1]
    latest_date = latest["properties"]["datetime"][:10]
    print(f"[composite] {len(feats)} 景 median 合成 | 窗口 {start}->{end} | 最新 {latest_date}")
    for f in feats:
        print("   -", f["properties"]["datetime"][:10],
              "cloud=%.1f%%" % f["properties"]["eo:cloud_cover"], f["id"])

    _latest_raw = None  # 最新景的原始（未掩膜）波段，用作 NaN 回填
    keys = ['B02','B03','B04','B05','B06','B08','B11']
    stacks = {k: [] for k in keys}
    used = []
    coverage = np.zeros(DST_SHAPE, bool)   # S2 图幅实际覆盖区(union)，用于区分“有数据角落”与“图幅外”
    for f in feats:
        dt = f["properties"]["datetime"][:10].replace("-", "")
        b = None
        for attempt in range(4):
            try:
                b, _, _, _ = _load_bands(f, dt)
                break
            except Exception as e:
                if "429" in str(e) and attempt < 3:
                    wait = 15 * (attempt + 1)
                    print(f"  [retry {attempt+1}/4] 429 限流，等待 {wait}s 后重试 {dt}...")
                    time.sleep(wait)
                    continue
                print("  [skip]", f["id"], e); break
        if b is None:
            continue
        coverage |= ~np.isnan(b['B04'])   # 该景图幅覆盖区(0→NaN 已标记无数据)，union 累积
        scl = b['SCL']
        clear = ~np.isin(scl, list(P.SCL_MASK)) if scl.size else np.zeros(DST_SHAPE, bool)
        _latest_raw = {k: b[k].astype(np.float32).copy() for k in keys}  # 未掩膜��始值
        for k in keys:
            arr = b[k].astype(np.float32).copy()
            arr[~clear] = np.nan
            stacks[k].append(arr)
        used.append(f["properties"]["datetime"][:10])
    comp = {}
    latest_raw = None  # 最新景的原始波段（未云掩膜），用作 NaN 回退
    for k in keys:
        if stacks[k]:
            med = np.nanmedian(np.stack(stacks[k], 0), 0)
            comp[k] = med
        else:
            comp[k] = np.full(DST_SHAPE, np.nan, np.float32)
    # ★ NaN 空洞修复：最近有效像元邻域修复（nearest-neighbor inpaint）。
    #   之前用固定/统计值平涂（0.15/0.30/0.38/中位数…）都会在缺失带形成
    #   一块均匀明暗带 → 影像被"劈成两半"。该带本质是多景在该纬度带全部有云，
    #   nanmedian 后成 NaN。正确做法是把周围真实地物反射率复制到空洞里，
    #   使缺失带与相邻影像纹理连续，肉眼不再有断层/分裂感。
    def _fill_nan_simple(arr):
        from scipy import ndimage
        nan_mask = np.isnan(arr)
        if not nan_mask.any():
            return arr
        if not (~nan_mask).any():
            return np.full_like(arr, 0.15)
        out = arr.copy()
        inds = ndimage.distance_transform_edt(
            nan_mask, return_distances=False, return_indices=True)
        # 每个 NaN 像元取"最近的有效像元"的反射率；有效像元索引指向自身→不变
        out = out[tuple(inds)]
        print(f"[fill] shape={arr.shape} nan={int(nan_mask.sum())} "
              f"-> nearest-neighbor inpaint", flush=True)
        return out
    for k in keys:
        comp[k] = _fill_nan_simple(comp[k])
        # ★ 不再对"图幅外"区域 re-NaN：inpaint 已用最近邻纹理填充所有空洞（含云洞+图幅外），
        #   整幅图连续无空隙。若强制透明则瓦片行间出现缝隙（底图露出），体验更差。
    meta = {
        "n_scenes": len(used),
        "dates": used,
        "latest_date": latest_date,
        "latest_id": latest["id"],
        "latest_cloud": float(latest["properties"]["eo:cloud_cover"]),
        "coverage": coverage,
    }
    _COMPOSITE_CACHE[(start, end)] = (comp, meta)
    return comp, meta

def _vectorize_with_stats(mask, dst_transform, arr, tol_m=15.0):
    """把二值掩膜矢量化，并为每个多边形附加 arr 的均值统计 -> FeatureCollection。"""
    from rasterio.features import shapes, rasterize
    from shapely.geometry import shape, mapping
    import shapely.ops
    feats = []
    for geom, val in shapes(mask.astype(np.uint8), transform=dst_transform):
        if val != 1:
            continue
        g = shape(geom).simplify(tol_m, preserve_topology=True)
        if not g.is_valid:
            g = shapely.ops.unary_union([g.buffer(0)])
        polys = g.geoms if g.geom_type == 'MultiPolygon' else [g]
        for gg in polys:
            if gg.area <= 0:
                continue
            gm = mapping(gg)
            # 计算该多边形内 arr 的均值
            m = rasterize([(gm, 1)], out_shape=mask.shape, transform=dst_transform, fill=0).astype(bool)
            arr_m = arr[m] if m.sum() else np.array([np.nan])
            area_ha = m.sum()*TARGET_RES*TARGET_RES/1e4
            feats.append({
                "type": "Feature",
                "properties": {
                    "area_ha": round(float(area_ha), 3),
                    "mean": None if np.isnan(arr_m).all() else round(float(np.nanmean(arr_m)), 4),
                },
                "geometry": gm,
            })
    return {"type": "FeatureCollection", "features": feats}

def combo_key_of(date, roi):
    """组合键：composite_town / composite_gee / 2026-07-21_town ..."""
    base = "composite" if date is None else date
    return base + "_" + ("town" if roi in ("town", None) else "gee")


def compute(date=None, roi="town"):
    """运行完整流水线，返回 combo 字典（含渲染用数组 + 统计），不写文件。
    与 GEE 参考版逐字一致；roi 仅影响裁剪/统计，不影响波段与指数。
    阈值固定为站点的默认阈值（NDCI0.10 / FAI0.005 / MCI0.005 / JRC10），与静态站一致。"""
    ndci_thr, fai_thr, mci_thr, jrc_thr = 0.10, 0.005, 0.005, 10
    # 1) 场景选择：默认 45 天 median 合成；date= 时单景
    if date:
        api = (STAC_BASE + f"?bbox={BBOX[0]},{BBOX[1]},{BBOX[2]},{BBOX[3]}"
               f"&datetime={date}T00:00:00Z/{date}T23:59:59Z&limit=20")
        for _att in range(4):
            try:
                feats = json.load(urllib_request(api))["features"]
                break
            except Exception as e:
                if "429" in str(e) and _att < 3:
                    time.sleep(10 * (_att + 1)); continue
                raise
        if not feats:
            raise RuntimeError(f"STAC 未找到 {date} 的 S2 场景")
        item = feats[0]
        date_tag = date.replace("-", "")
        bands = None
        for attempt in range(4):
            try:
                bands, _, _, _ = _load_bands(item, date_tag)
                break
            except Exception as e:
                if "429" in str(e) and attempt < 3:
                    wait = 15 * (attempt + 1)
                    print(f"[retry {attempt+1}/4] 429 限流，等待 {wait}s 后重试 {date}...")
                    time.sleep(wait); continue
                raise
        B02,B03,B04,B05,B06,B08,B11,SCL = (bands['B02'],bands['B03'],bands['B04'],bands['B05'],
                                           bands['B06'],bands['B08'],bands['B11'],bands['SCL'])
        cloud_mask = np.isin(SCL, list(P.SCL_MASK)) if SCL.size else np.zeros(DST_SHAPE, bool)
        valid = ~np.isnan(B04) & ~cloud_mask
        meta = {"n_scenes":1, "dates":[item['properties']['datetime'][:10]],
                "latest_date":item['properties']['datetime'][:10],
                "latest_id":item['id'], "latest_cloud":float(item['properties']['eo:cloud_cover'])}
    else:
        comp, meta = _build_composite()
        B02,B03,B04,B05,B06,B08,B11 = (comp['B02'],comp['B03'],comp['B04'],comp['B05'],
                                       comp['B06'],comp['B08'],comp['B11'])
        SCL = None
        valid = ~np.isnan(B04)

    # ★ 有效数据边界：S2 场景实际覆盖范围(可能小于网格 BBOX)，
    #   用于瓦片渲染缩小显示区域，避免出现两块/空洞
    valid_any = (~np.isnan(B04)) | (~np.isnan(B03))
    if valid_any.any():
        _rows = np.where(valid_any.any(axis=1))[0]
        _cols = np.where(valid_any.any(axis=0))[0]
        _r0, _r1 = _rows.min(), _rows.max() + 1
        _c0, _c1 = _cols.min(), _cols.max() + 1
        lon_min, lat_max = DST_TRANSFORM * (_c0, _r0)   # 左上角 → (lon, lat)
        lon_max, lat_min = DST_TRANSFORM * (_c1, _r1)   # 右下角 → (lon, lat)
        data_bbox = (lon_min, lat_min, lon_max, lat_max)
    else:
        data_bbox = BBOX

    scene_id   = meta["latest_id"]
    scene_date = meta["latest_date"]
    cloud      = meta["latest_cloud"]
    print(f"[compute] {scene_id} {scene_date} cloud={cloud:.1f}% | 合成 {meta['n_scenes']} 景 "
          f"roi={roi}")

    # 2) 指数（与 GEE 参考版逐字一致）
    MNDWI = (B03-B11)/(B03+B11+1e-6)
    NDCI  = (B05-B04)/(B05+B04+1e-6)
    f = (842.0-665.0)/(1610.0-665.0)
    FAI = B08-(B04+(B11-B04)*f)
    MCI = B05-B04-(B06-B04)*((705-665)/(740-665))

    # 旧掩膜（对照）：单景模式用 MNDWI>0.10 & SCL==6；合成模式仅 MNDWI>0.10
    if SCL is not None:
        water_old = (MNDWI > P.MNDWI_THR) & valid & (SCL == 6)
    else:
        water_old = (MNDWI > P.MNDWI_THR) & valid

    # 3) 水体掩膜
    occ = P.get_jrc_occurrence(DST_TRANSFORM, DST_SHAPE, BBOX)
    water_full = (MNDWI > P.MNDWI_THR) & (occ >= jrc_thr) & valid

    from scipy import ndimage
    water_full = ndimage.binary_opening(water_full.astype(np.uint8), iterations=1).astype(bool)
    water_full = ndimage.binary_closing(water_full.astype(np.uint8), iterations=1).astype(bool)
    water_full = ndimage.binary_fill_holes(water_full)

    clip_mask  = BD_MASK if roi in ("town", None) else ROI_MASK
    water_new  = (water_full & BD_MASK) if roi in ("town", None) else (water_full & ROI_MASK)

    # 3.2) 藻华
    bloom_full   = water_full & ((NDCI > ndci_thr) | (FAI > fai_thr))
    bloom        = (bloom_full & BD_MASK) if roi in ("town", None) else (bloom_full & ROI_MASK)
    bloom_ndci   = (water_new & (NDCI > ndci_thr))

    # 3.5) 藻华检测 v2（Otsu + CMI）
    bloom_ml = bloom.copy()
    bloom_prob = np.zeros(DST_SHAPE, dtype="float32")
    if ML is not None:
        try:
            bloom_ml, bloom_prob = ML.detect(B02, B03, B04, B05, B06, B08, B11, water_new)
            bloom_ml &= clip_mask
            bloom_prob *= clip_mask
        except Exception as _e:
            print("[ml] detect 失败，退回规则法:", _e)
            bloom_ml = bloom.copy()

    def ha(n): return n*TARGET_RES*TARGET_RES/1e4

    # 4) 矢量化
    water_fc, _ = P.vectorize_water(water_new, DST_TRANSFORM, tol_m=15.0)
    bloom_fc = _vectorize_with_stats(bloom, DST_TRANSFORM, NDCI, tol_m=12.0)
    bloom_ml_fc = _vectorize_with_stats(bloom_ml, DST_TRANSFORM, NDCI, tol_m=12.0)

    n_old = int(water_old.sum()); n_new = int(water_new.sum())
    n_bloom = int(bloom_ndci.sum())
    gee_water = int((water_full & ROI_MASK).sum()); gee_bloom = int((water_full & ROI_MASK & (NDCI > ndci_thr)).sum())
    wi = water_new
    centroid = None
    if wi.sum():
        yy, xx = np.where(wi)
        clat = float(np.mean([(DST_TRANSFORM*(0, int(r)))[1] for r in yy]))
        clon = float(np.mean([(DST_TRANSFORM*(int(c), 0))[0] for c in xx]))
        centroid = [round(clon, 5), round(clat, 5)]
    ndci_w = NDCI[water_new]
    result = {
        "ready": True,
        "scene_id": scene_id,
        "date": scene_date,
        "cloud": round(cloud, 1),
        "n_scenes": meta["n_scenes"],
        "scene_dates": meta["dates"],
        "composite": date is None,
        "roi": "huangdi_town" if roi in ("town", None) else "5km_buffer",
        "bbox": [BBOX[0], BBOX[1], BBOX[2], BBOX[3]],
        "data_bbox": [data_bbox[0], data_bbox[1], data_bbox[2], data_bbox[3]],
        "water_ha": round(ha(n_new), 1),
        "water_old_ha": round(ha(n_old), 1),
        "water_gain_ha": round(ha(n_new - n_old), 1),
        "bloom_ha": round(ha(n_bloom), 2),
        "bloom_px": n_bloom,
        "bloom_features": len(bloom_fc["features"]),
        "bloom_ml_ha": round(ha(int(bloom_ml.sum())), 2),
        "bloom_ml_px": int(bloom_ml.sum()),
        "bloom_ml_features": len(bloom_ml_fc["features"]),
        "ml_enabled": ML is not None,
        "status": "预警" if (n_bloom and ha(n_bloom) >= P.BLOOM_AREA_HA) else "正常",
        "centroid": centroid,
        "in_boundary_ha": round(ha(int((water_new & BD_MASK).sum())), 1),
        "in_boundary_pct": round(100*int((water_new & BD_MASK).sum())/max(n_new, 1), 1),
        "gee_water_ha": round(ha(gee_water), 1),
        "gee_bloom_ha": round(ha(gee_bloom), 2),
        "ndci_p50": round(float(np.nanpercentile(ndci_w, 50)), 3) if n_new else None,
        "ndci_p90": round(float(np.nanpercentile(ndci_w, 90)), 3) if n_new else None,
        "ndci_max": round(float(np.nanmax(ndci_w)), 3) if n_new else None,
        "fai_p50": round(float(np.nanpercentile(FAI[water_new], 50)), 4) if n_new else None,
        "mci_p50": round(float(np.nanpercentile(MCI[water_new], 50)), 3) if n_new else None,
        "thresholds": {"ndci": ndci_thr, "fai": fai_thr, "mci": mci_thr, "jrc": jrc_thr},
        "rev": 0,
    }

    # 渲染用数组：真彩底图显示完整网格（不裁剪，避免"饼干模"碎裂）；
    # 检测图层(NDCI/bloom/water)仍裁剪到 clip_mask 以约束显示范围
    from PIL import Image as _PILImage
    rgb = np.stack([B04, B03, B02], 0); rgb = np.clip(rgb, 0.05, 0.4)  # 下限 0.05 避免云阴影/插值区被拉成纯黑
    rgb = np.nan_to_num(rgb, nan=0.0)
    # ★ 自定义拉伸（pmin=10 而非默认 2）：避免把插值区/云阴影压成纯黑
    #   默认 pmin=2 太激进，连 0.15 都映射到 0；pmin=10 让中间灰 (~128) 可见
    rgb_s = np.transpose(_stretch_safe(rgb), (1, 2, 0))
    rgb_full = rgb_s.copy().astype("uint8")          # 完整网格，不裁剪
    ndci_disp = NDCI.astype("float32").copy(); ndci_disp[~clip_mask] = 0.0

    combo = {
        "key": combo_key_of(date, roi), "date": scene_date, "roi": roi,
        "data_bbox": data_bbox,
        # ★ valid 全 True：inpaint 已填充所有空洞（含图幅外），瓦片完全不透明，无空隙
        "valid": np.ones(DST_SHAPE, dtype=bool),
        "rgb": rgb_full, "ndci": ndci_disp,
        "water": water_new, "bloom": bloom,
        "bloomml": bloom_ml, "bloommlp": bloom_prob,
        "old": (water_old & clip_mask),
        "water_fc": water_fc, "bloom_fc": bloom_fc, "bloom_ml_fc": bloom_ml_fc,
        "stats": result,
    }
    return combo


def discover_dates(window=45, limit=6):
    """从 STAC 发现最近可用的单景日期（云量<60），用于静态多组合预计算。"""
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    start = (now - _dt.timedelta(days=window)).strftime('%Y-%m-%d')
    end = now.strftime('%Y-%m-%d')
    feats = _query_stac(start, end)
    ds = sorted({f["properties"]["datetime"][:10] for f in feats})
    return ds[-limit:]


def analyze(ndci_thr=0.10, fai_thr=0.005, mci_thr=0.005, jrc_thr=10, date=None, roi="town"):
    """兼容旧接口：运行流水线并写出默认（composite_town 等价）扁平 outputs/*，供本地开发服务器使用。"""
    combo = compute(date, roi)
    result = combo["stats"]
    json.dump(combo["water_fc"], open(os.path.join(OUT, "water.geojson"), "w"), ensure_ascii=False)
    json.dump(combo["bloom_fc"], open(os.path.join(OUT, "bloom.geojson"), "w"), ensure_ascii=False)
    json.dump(combo["bloom_ml_fc"], open(os.path.join(OUT, "bloom_ml.geojson"), "w"), ensure_ascii=False)
    result["rev"] = int(time.time())
    json.dump(result, open(os.path.join(OUT, "result.json"), "w"), ensure_ascii=False, indent=2)
    print(f"[analyze] done: water={result['water_ha']}ha bloom={result['bloom_ha']}ha status={result['status']}")
    return result

def urllib_request(url):
    import urllib.request
    return urllib.request.urlopen(url, timeout=30)

# =====================================================================
# HTTP 服务
# =====================================================================
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        if path in ("/", "/index.html"):
            return self._serve_file(os.path.join(STATIC, "index.html"), "text/html; charset=utf-8")
        if path.startswith("/static/"):
            return self._serve_file(os.path.join(HERE, path.lstrip("/")), self._ctype(path))
        if path.startswith("/outputs/"):
            return self._serve_file(os.path.join(HERE, path.lstrip("/")), self._ctype(path))
        if path.startswith("/tiles/") or path.startswith("/tiles_gcj/"):
            return self._serve_tile(path)
        if path == "/api/status":
            rp = os.path.join(OUT, "result.json")
            if os.path.exists(rp):
                return self._send(200, open(rp, encoding="utf-8").read())
            return self._send(200, json.dumps({"ready": False}))
        if path == "/api/analyze":
            q = parse_qs(u.query)
            def f(k, d):
                try: return float(q.get(k, [d])[0])
                except: return d
            try:
                res = analyze(ndci_thr=f("ndci", 0.10), fai_thr=f("fai", 0.005),
                              mci_thr=f("mci", 0.005), jrc_thr=f("jrc", 10),
                              date=q.get("date", [None])[0],
                              roi=(q.get("roi", ["town"])[0] or "town"))
                return self._send(200, res)
            except Exception as e:
                return self._send(500, {"error": str(e)})
        return self._send(404, {"error": "not found"})

    def _serve_file(self, fp, ctype):
        if not os.path.exists(fp) or not os.path.isfile(fp):
            return self._send(404, {"error": "file not found: " + fp})
        with open(fp, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _ctype(self, p):
        if p.endswith(".html"): return "text/html; charset=utf-8"
        if p.endswith(".css"):  return "text/css; charset=utf-8"
        if p.endswith(".js"):   return "application/javascript; charset=utf-8"
        if p.endswith(".json"): return "application/json; charset=utf-8"
        if p.endswith(".png"):  return "image/png"
        if p.endswith(".jpg"):  return "image/jpeg"
        if p.endswith(".webp"): return "image/webp"
        if p.endswith(".geojson"): return "application/json; charset=utf-8"
        return "application/octet-stream"

    # ---------- XYZ 瓦片服务（B 方案：放大锐利 + 超分上采样） ----------
    def _mercator_bounds(self, z, x, y):
        n = 2 ** z
        lon0 = x / n * 360 - 180
        lon1 = (x + 1) / n * 360 - 180
        def _lat(yy):
            return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yy / n))))
        return (_mx(lon0), _my(_lat(y + 1)), _mx(lon1), _my(_lat(y)))

    def _transparent_tile(self):
        from PIL import Image as _PILImage
        import io as _io
        im = _PILImage.new("RGBA", (256, 256), (0, 0, 0, 0))
        b = _io.BytesIO(); im.save(b, "PNG"); return b.getvalue()

    def _send_png(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()
        self.wfile.write(data)

def _prob_to_png(prob, water, fp):
    """把 0~1 概率按 黄->橙->红 热图渲染（透明<0.3），带水体掩膜。"""
    from PIL import Image as _PILImage
    H, W = prob.shape
    rgba = np.zeros((H, W, 4), dtype="uint8")
    m = water & (prob > 0.3)
    if m.sum() == 0:
        _PILImage.fromarray(rgba, "RGBA").save(fp, "PNG", optimize=True)
        return
    t = np.clip((prob[m] - 0.3) / 0.7, 0, 1)          # 0.3->0, 1.0->1
    r = np.interp(t, [0.0, 0.5, 1.0], [255, 255, 255]).astype("uint8")
    g = np.interp(t, [0.0, 0.5, 1.0], [255, 140, 0]).astype("uint8")
    b = np.interp(t, [0.0, 0.5, 1.0], [0, 0, 0]).astype("uint8")
    rgba[m, 0], rgba[m, 1], rgba[m, 2] = r, g, b
    rgba[m, 3] = 210
    _PILImage.fromarray(rgba, "RGBA").save(fp, "PNG", optimize=True)

    def _serve_tile(self, path):
        # 支持 /tiles[/_gcj]/<comboKey>/<layer>/z/x/y.{png|webp}
        m = re.match(r"/(tiles_gcj|tiles)/([A-Za-z0-9_-]+)/([a-z]+)/(\d+)/(\d+)/(\d+)\.(png|webp)$", path)
        if not m:
            return self._send(404, {"error": "bad tile"})
        prefix, combo_key, layer, z, x, y = (m.group(1), m.group(2), m.group(3),
                                             int(m.group(4)), int(m.group(5)), int(m.group(6)))
        gcj = (prefix == "tiles_gcj")
        mb = self._mercator_bounds(z, x, y)
        # 相交判定：瓦片与 bbox 任意重叠即渲染（mb[1]=瓦片北界, mb[3]=瓦片南界）
        if not (mb[0] < BBOX_MERC[2] and mb[2] > BBOX_MERC[0]
                and mb[1] > BBOX_MERC[1] and mb[3] < BBOX_MERC[3]):
            return self._send_png(self._transparent_tile())
        try:
            combo = _get_combo(combo_key)
            tile_fp = render_combo_tile(combo, layer, z, x, y, gcj, OUT)
        except Exception as e:
            print("[tile] gen fail", combo_key, layer, z, x, y, repr(e))
            try:
                open("/tmp/tile_err.log", "a").write("ERR %s %s %d/%d/%d: %r\n" % (combo_key, layer, z, x, y, e))
            except Exception:
                pass
            return self._send_png(self._transparent_tile())
        ctype = "image/webp" if tile_fp.endswith(".webp") else "image/png"
        return self._serve_file(tile_fp, ctype)

    def log_message(self, *a):
        pass  # 静默


# ---------- 多组合瓦片渲染（开发服务器按需 / 静态构建离线复用） ----------
def _combo_from_key(key):
    """comboKey -> (date, roi)。composite_town / composite_gee / 2026-07-21_town ..."""
    if key.startswith("composite"):
        date = None
        roi = key[len("composite"):]
        if roi.startswith("_"):
            roi = roi[1:]
    else:
        date, roi = key.rsplit("_", 1)
    if roi not in ("town", "gee"):
        roi = "town"
    return date, roi


def _get_combo(key):
    if key in COMBOS:
        return COMBOS[key]
    date, roi = _combo_from_key(key)
    combo = compute(date, roi)
    COMBOS[key] = combo
    return combo


def render_combo_tile(combo, layer, z, x, y, gcj, tiles_root):
    """把 combo 的某个图层渲染成 XYZ 瓦片 PNG，写入
       tiles_root/[tiles_gcj|tiles]/<combo_key>/<layer>/z/x/y.png。"""
    from rasterio.warp import reproject, Resampling
    from PIL import Image as _PILImage
    sub = "tiles_gcj" if gcj else "tiles"
    out_dir = os.path.join(tiles_root, sub, combo["key"], layer, str(z), str(x))
    os.makedirs(out_dir, exist_ok=True)
    ext = ".webp" if layer == "rgb" else ".png"   # RGB 用 WebP(4x 小)，其余 PNG
    out_path = os.path.join(out_dir, str(y) + ext)
    if os.path.exists(out_path):
        return out_path
    n = 2 ** z
    lon0 = x / n * 360 - 180
    lon1 = (x + 1) / n * 360 - 180
    lat = lambda yy: math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yy / n))))
    mb = (_mx(lon0), _my(lat(y)), _mx(lon1), _my(lat(y + 1)))   # (w, n, e, s)
    dt = P.fb(*mb, 256, 256)
    if gcj:
        st = P.fb(BBOX[0] + GCJ_DLON, BBOX[1] + GCJ_DLAT,
                  BBOX[2] + GCJ_DLON, BBOX[3] + GCJ_DLAT, NCOL, NROW)
    else:
        st = DST_TRANSFORM
    RW = dict(src_transform=st, src_crs="EPSG:4326", dst_transform=dt, dst_crs="EPSG:3857")
    if layer == "rgb":
        src = np.transpose(combo["rgb"], (2, 0, 1)).astype("uint8")
        dst = np.zeros((3, 256, 256), dtype="uint8")
        reproject(src, dst, resampling=Resampling.bilinear, **RW)
        # ★ alpha = 有效数据掩膜：S2 图幅外/无数据区(含瓦片越界)设为透明，
        #   不再填充纯黑。这样底图边角无黑块，缺失区显示页面底色(深色)。
        valid = combo.get("valid")
        if valid is not None:
            src_v = valid.astype("uint8")[None, :, :]
            dst_v = np.zeros((1, 256, 256), dtype="uint8")
            reproject(src_v, dst_v, resampling=Resampling.nearest, **RW)
            mask = (dst_v[0] > 0).astype("uint8") * 255
        else:
            mask = np.full((256, 256), 255, dtype="uint8")
        _PILImage.fromarray(np.dstack([dst[0], dst[1], dst[2], mask]), "RGBA").save(out_path, "WEBP", quality=78, method=4)
    elif layer == "ndci":
        nd = combo["ndci"].astype("float32")
        wt = combo["water"].astype("uint8")
        dst_nd = np.zeros((256, 256), dtype="float32")
        dst_wt = np.zeros((256, 256), dtype="uint8")
        reproject(nd, dst_nd, resampling=Resampling.bilinear, **RW)
        reproject(wt, dst_wt, resampling=Resampling.nearest, **RW)
        P.index_to_png(dst_nd, dst_wt.astype(bool), P.NDCI_CMAP, -0.05, 0.15, out_path)
    elif layer in ("water", "old"):
        src = combo[layer].astype("uint8")
        dst = np.zeros((256, 256), dtype="uint8")
        reproject(src, dst, resampling=Resampling.nearest, **RW)
        col = [30, 144, 255] if layer == "water" else [255, 60, 60]
        al = 150 if layer == "water" else 170
        P.mask_to_png(dst.astype(bool), col, out_path, alpha=al)
    elif layer == "bloom":
        src = combo["bloom"].astype("uint8")
        dst = np.zeros((256, 256), dtype="uint8")
        reproject(src, dst, resampling=Resampling.nearest, **RW)
        P.bloom_to_png(dst.astype(bool), out_path)
    elif layer == "bloomml":
        src = combo["bloomml"].astype("uint8")
        dst = np.zeros((256, 256), dtype="uint8")
        reproject(src, dst, resampling=Resampling.nearest, **RW)
        P.bloom_to_png(dst.astype(bool), out_path)
    elif layer == "bloommlp":
        pr = combo["bloommlp"].astype("float32")
        wt = combo["water"].astype("uint8")
        dst_p = np.zeros((256, 256), dtype="float32")
        dst_w = np.zeros((256, 256), dtype="uint8")
        reproject(pr, dst_p, resampling=Resampling.bilinear, **RW)
        reproject(wt, dst_w, resampling=Resampling.nearest, **RW)
        _prob_to_png(dst_p, dst_w.astype(bool), out_path)
    else:
        raise ValueError("unknown layer " + layer)
    return out_path


def main():
    port = int(os.environ.get("PORT", "8000"))
    # 启动时预生成一次分析结果（波段已缓存，约十几秒）
    try:
        if not os.path.exists(os.path.join(OUT, "result.json")):
            print("[startup] 预生成分析结果 ...")
            analyze()
    except Exception as e:
        print("[startup] 预生成失败(可稍后在页面点击重新分析):", e)
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"[server] http://localhost:{port}")
    srv.serve_forever()

if __name__ == "__main__":
    main()
