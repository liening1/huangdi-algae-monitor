#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_static.py — 把动态网站"固化"为纯静态站点（GitHub Pages 可托管）。

方案 A：构建时把多个组合（45天合成 / 各单景 × 黄埭镇边界 / 5km缓冲）一次性算好并切片，
前端下拉直接切换不同瓦片集，静态站也能"秒切"，无需后端。

流程：
  1) 发现最近可用单景日期（STAC）
  2) 对每个组合 (date × roi) 调用 app.compute()，写出：
       outputs/combos/<comboKey>/result.json + *.geojson
       outputs/tiles/<comboKey>/<layer>/z/x/y.png        (WGS84)
       outputs/tiles_gcj/<comboKey>/<layer>/z/x/y.png      (高德 GCJ-02)
  3) 生成 outputs/manifest.json（列出全部组合 + 关键统计）
  4) 组装 dist/：index.html / static/ / outputs/(含 combos、tiles、tiles_gcj、manifest)

本地（macOS）用法（依赖已固化到项目内 venv，系统 python3.9.6 构建）：
   venv/bin/python build_static.py
GitHub Actions（ubuntu）用法：
   python3 build_static.py        # 依赖见 requirements_build.txt
"""
import os, sys, json, math, shutil, time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
WEB  = os.path.join(HERE, "web")
OUT  = os.path.join(WEB, "outputs")
DIST = os.path.join(HERE, "dist")
LAYERS = ["rgb", "ndci", "water", "bloom", "old", "bloomml", "bloommlp"]   # 每个组合预渲染的全部图层
ZMIN, ZMAX = 10, 15
WORKERS = 8
MAX_SINGLE_DATES = 5                          # 单景组合数量上限（控制瓦片总量）

sys.path.insert(0, WEB)
import app as A  # web/app.py（提供 compute / render_combo_tile / discover_dates / BBOX 等）


# ---------------------------------------------------------------- 瓦片枚举
def tiles_intersecting(z):
    """正确的墨卡托重叠判定（瓦片与 bbox 任意相交即收），返回 [(z,x,y), ...]。"""
    n = 2 ** z
    RM = 6378137.0
    mx = lambda lon: math.radians(lon) * RM
    my = lambda lat: math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) * RM
    BM = (mx(A.BBOX[0]), my(A.BBOX[1]), mx(A.BBOX[2]), my(A.BBOX[3]))

    def mb(zz, x, y):
        lon0 = x / zz * 360 - 180
        lon1 = (x + 1) / zz * 360 - 180
        lat = lambda yy: math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yy / zz))))
        return (mx(lon0), my(lat(y)), mx(lon1), my(lat(y + 1)))   # (w, n, e, s)

    def tr(lat):
        return (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n

    out = []
    xs = range(int((A.BBOX[0] + 180) / 360 * n) - 1, int((A.BBOX[2] + 180) / 360 * n) + 2)
    yN, yS = tr(A.BBOX[3]), tr(A.BBOX[1])
    ys = range(int(min(yN, yS)) - 1, int(max(yN, yS)) + 2)
    for x in xs:
        for y in ys:
            w, nn, e, s = mb(n, x, y)
            if w < BM[2] and e > BM[0] and nn > BM[1] and s < BM[3]:
                out.append((z, x, y))
    return out


def _roi_label(roi):
    return "黄埭镇边界" if roi in ("town", None) else "5km缓冲(GEE)"


# ---------------------------------------------------------------- 步骤
def step_build_combos():
    print("[build] 1/3 多组合预计算（合成 + 单景 × 镇域/5km缓冲）...", flush=True)
    os.makedirs(OUT, exist_ok=True)
    dates = A.discover_dates(limit=MAX_SINGLE_DATES)
    print(f"[build]   发现单景日期: {dates}", flush=True)

    combos = [(None, "town"), (None, "gee")]
    for d in dates:
        combos.append((d, "town"))
        combos.append((d, "gee"))

    manifest = {"default": "composite_town", "built_at": "", "combos": {}}
    total_tiles = 0

    for (date, roi) in combos:
        key = A.combo_key_of(date, roi)
        print(f"[build]   ▶ {key} ...", flush=True)
        try:
            combo = A.compute(date, roi)
        except Exception as e:
            print(f"[build]     ✗ compute 失败，跳过: {e}", flush=True)
            continue
        stats = combo["stats"]
        # 每组合独立输出目录
        cdir = os.path.join(OUT, "combos", key)
        os.makedirs(cdir, exist_ok=True)
        json.dump(combo["water_fc"],    open(os.path.join(cdir, "water.geojson"), "w"),    ensure_ascii=False)
        json.dump(combo["bloom_fc"],    open(os.path.join(cdir, "bloom.geojson"), "w"),    ensure_ascii=False)
        json.dump(combo["bloom_ml_fc"], open(os.path.join(cdir, "bloom_ml.geojson"), "w"), ensure_ascii=False)
        json.dump(stats,                open(os.path.join(cdir, "result.json"), "w"),     ensure_ascii=False, indent=2)

        # 离线渲染瓦片（WGS + GCJ 两套）
        jobs = []
        for z in range(ZMIN, ZMAX + 1):
            cells = tiles_intersecting(z)
            for (zz, x, y) in cells:
                for layer in LAYERS:
                    jobs.append((combo, layer, zz, x, y, False, OUT))
                    jobs.append((combo, layer, zz, x, y, True, OUT))
        done = 0
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            for ok in ex.map(_render_one, jobs):
                done += 1 if ok else 0
        total_tiles += len(jobs)
        print(f"[build]     ✓ 水{stats['water_ha']}ha 藻{stats['bloom_ha']}ha "
              f"状态{stats['status']} | 瓦片 {len(jobs)}", flush=True)

        manifest["combos"][key] = {
            "key": key,
            "date": date,
            "roi": roi,
            "label": ("45天合成" if date is None else date) + " · " + _roi_label(roi),
            "roi_label": _roi_label(roi),
            "composite": date is None,
            "scene_date": stats["date"],
            "cloud": stats["cloud"],
            "n_scenes": stats["n_scenes"],
            "water_ha": stats["water_ha"],
            "bloom_ha": stats["bloom_ha"],
            "bloom_ml_ha": stats["bloom_ml_ha"],
            "status": stats["status"],
            "ndci_p90": stats["ndci_p90"],
        }

    # 顶部默认输出（composite_town）供开发模式 / 向后兼容
    def _copy_default(name, src_name=None):
        src_name = src_name or name
        s = os.path.join(OUT, "combos", "composite_town", src_name)
        if os.path.exists(s):
            shutil.copy(s, os.path.join(OUT, name))
    _copy_default("result.json")
    _copy_default("water.geojson")
    _copy_default("bloom.geojson")
    _copy_default("bloom_ml.geojson")
    # 边界文件（前端需要 outputs/boundary.json）
    bd_src = os.path.join(HERE, "黄埭镇边界.json")
    if os.path.exists(bd_src):
        shutil.copy(bd_src, os.path.join(OUT, "boundary.json"))

    manifest["built_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    json.dump(manifest, open(os.path.join(OUT, "manifest.json"), "w"), ensure_ascii=False, indent=2)
    print(f"[build]   共 {len(manifest['combos'])} 个组合，计划瓦片 {total_tiles} 块", flush=True)
    return manifest


def _render_one(args):
    combo, layer, z, x, y, gcj, tiles_root = args
    try:
        A.render_combo_tile(combo, layer, z, x, y, gcj, tiles_root)
        return True
    except Exception as e:
        print("[tile] fail", combo["key"], layer, z, x, y, gcj, repr(e), flush=True)
        return False


def step_assemble():
    print("[build] 2/3 组装 dist/ ...", flush=True)
    if os.path.isdir(DIST):
        shutil.rmtree(DIST)
    os.makedirs(DIST)

    # 前端（index.html 已用相对路径）
    shutil.copy(os.path.join(WEB, "static", "index.html"), os.path.join(DIST, "index.html"))
    shutil.copytree(os.path.join(WEB, "static"), os.path.join(DIST, "static"),
                    ignore=shutil.ignore_patterns("index.html"))

    # 数据层
    os.makedirs(os.path.join(DIST, "outputs"))
    for fn in ("result.json", "water.geojson", "bloom.geojson",
               "bloom_ml.geojson", "boundary.json", "manifest.json", "build_info.json"):
        src = os.path.join(OUT, fn)
        if os.path.exists(src):
            shutil.copy(src, os.path.join(DIST, "outputs", fn))

    # 多组合结果
    cdir = os.path.join(OUT, "combos")
    if os.path.isdir(cdir):
        shutil.copytree(cdir, os.path.join(DIST, "outputs", "combos"))

    # 瓦片：WGS 与 GCJ 已分目录，整树拷贝到 dist 顶层（dist/tiles、dist/tiles_gcj）。
    # 注意：前端用相对路径 "tiles_gcj/<combo>/..." 请求（与已验证的旧版一致），
    # 开发服务器 app.py 也以 "/tiles_gcj/..." 从 web/outputs/tiles_gcj 提供，故瓦片必须放顶层。
    n_wgs = n_gcj = 0
    for sub in ("tiles", "tiles_gcj"):
        src = os.path.join(OUT, sub)
        if not os.path.isdir(src):
            continue
        for root, _d, files in os.walk(src):
            for f in files:
                if not (f.endswith(".png") or f.endswith(".webp")):
                    continue
                rel = os.path.relpath(root, src)
                dst = os.path.join(DIST, sub, rel, f)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy(os.path.join(root, f), dst)
                if sub == "tiles":
                    n_wgs += 1
                else:
                    n_gcj += 1
    print(f"[build]    瓦片落盘 tiles/={n_wgs}  tiles_gcj/={n_gcj}", flush=True)

    # .nojekyll + 构建信息
    open(os.path.join(DIST, ".nojekyll"), "w").close()
    info = {"built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "zoom": [ZMIN, ZMAX], "layers": LAYERS}
    json.dump(info, open(os.path.join(DIST, "outputs", "build_info.json"), "w"),
              ensure_ascii=False, indent=2)

    total = 0
    for root, _d, files in os.walk(DIST):
        for f in files:
            total += os.path.getsize(os.path.join(root, f))
    print(f"[build] 完成：dist/ 共 {total/1e6:.1f} MB", flush=True)


def main():
    step_build_combos()
    step_assemble()


if __name__ == "__main__":
    main()
