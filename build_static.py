#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_static.py — 把动态网站"固化"为纯静态站点（GitHub Pages 可托管）。

流程：
  1) 重新运行分析管线（45 天 median 合成，镇域 ROI）→ web/outputs/*
  2) 启动本地瓦片服务（子进程，端口 8123）
  3) 遍历 z10..z16 与分析网格相交的所有瓦片，分别预热 WGS(tiles/) 与
     高德 GCJ(tiles_gcj/) 两套缓存（服务按请求生成并落盘 web/outputs/tiles/）
  4) 组装 dist/：
       index.html / static/                      （前端，已用相对路径）
       outputs/  result.json + *.geojson + boundary.json（不含大型 tif）
       tiles/    WGS 瓦片；tiles_gcj/ GCJ 瓦片（_gcj 后缀剥离子目录）
       .nojekyll （关闭 Pages 的 Jekyll 处理）

本地（macOS）用法：
  PYTHONPATH=/tmp/pylibs /usr/bin/python3 build_static.py
GitHub Actions（ubuntu）用法：
  python3 build_static.py        # 依赖见 requirements_build.txt
"""
import os, sys, json, math, shutil, subprocess, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
WEB  = os.path.join(HERE, "web")
OUT  = os.path.join(WEB, "outputs")
DIST = os.path.join(HERE, "dist")
PORT = int(os.environ.get("BUILD_PORT", "8123"))
LAYERS = ["rgb", "ndci", "water", "bloom", "bloomml", "bloommlp", "old"]
ZMIN, ZMAX = 10, 16
WORKERS = 8

sys.path.insert(0, WEB)
import app as A  # web/app.py（模块级常量：BBOX / DST_TRANSFORM 等）


# ---------------------------------------------------------------- 瓦片枚举
def tiles_intersecting(z):
    """正确的墨卡托重叠判定（瓦片与 bbox 任意相交即收），返回 [(z,x,y), ...]。
    注：mb 返回 (west, north, east, south)；只要四边有交叠即算命中。"""
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


# ---------------------------------------------------------------- 步骤
def step_analyze():
    print("[build] 1/4 运行分析管线（45天合成 · 镇域 ROI）...", flush=True)
    os.makedirs(OUT, exist_ok=True)   # 全新克隆（Actions）时 outputs/ 可能不存在
    r = A.analyze(roi="town")
    print(f"[build]    water={r['water_ha']}ha bloom={r['bloom_ha']}ha "
          f"ndci_p90={r.get('ndci_p90')} scenes={r.get('scene_dates')}", flush=True)
    return r


def step_serve():
    print("[build] 2/4 启动本地瓦片服务 :%d ..." % PORT, flush=True)
    env = dict(os.environ)
    env["PORT"] = str(PORT)
    proc = subprocess.Popen(
        [sys.executable, os.path.join(WEB, "app.py")],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    for i in range(90):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/status", timeout=2)
            print("[build]    服务就绪", flush=True)
            return proc
        except Exception:
            time.sleep(1)
    proc.kill()
    raise RuntimeError("瓦片服务启动超时")


def _fetch(url):
    try:
        with urllib.request.urlopen(url, timeout=120) as r:
            r.read()
        return True
    except Exception as e:
        print("[build]    瓦片失败", url, repr(e), flush=True)
        return False


def step_warm_tiles():
    print("[build] 3/4 预热瓦片 z%d-%d × %d 层 × 2 变体 ..." % (ZMIN, ZMAX, len(LAYERS)), flush=True)
    jobs = []
    for z in range(ZMIN, ZMAX + 1):
        cells = tiles_intersecting(z)
        print(f"[build]    z{z}: {len(cells)} 块", flush=True)
        for (zz, x, y) in cells:
            for layer in LAYERS:
                jobs.append(f"http://127.0.0.1:{PORT}/tiles/{layer}/{zz}/{x}/{y}.png")
                jobs.append(f"http://127.0.0.1:{PORT}/tiles_gcj/{layer}/{zz}/{x}/{y}.png")
    print(f"[build]    共 {len(jobs)} 个请求（{WORKERS} 并发）", flush=True)
    ok = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for good in ex.map(_fetch, jobs):
            ok += 1 if good else 0
    print(f"[build]    瓦片完成 {ok}/{len(jobs)}", flush=True)
    if ok < len(jobs) * 0.98:
        raise RuntimeError(f"瓦片预热失败过多：{len(jobs)-ok} 个失败")


def step_assemble():
    print("[build] 4/4 组装 dist/ ...", flush=True)
    if os.path.isdir(DIST):
        shutil.rmtree(DIST)
    os.makedirs(DIST)

    # 前端（index.html 已在仓库内使用相对路径）
    shutil.copy(os.path.join(WEB, "static", "index.html"), os.path.join(DIST, "index.html"))
    shutil.copytree(os.path.join(WEB, "static"), os.path.join(DIST, "static"),
                    ignore=shutil.ignore_patterns("index.html"))

    # 数据（只带前端需要的；不带大型 tif/png/jpg 中间产物）
    os.makedirs(os.path.join(DIST, "outputs"))
    for fn in ("result.json", "water.geojson", "bloom.geojson",
               "bloom_ml.geojson", "boundary.json"):
        src = os.path.join(OUT, fn)
        if not os.path.exists(src) and fn == "boundary.json":
            src = os.path.join(HERE, "黄埭镇边界.json")   # 全新克隆时回退到仓库根
        if os.path.exists(src):
            shutil.copy(src, os.path.join(DIST, "outputs", fn))
        else:
            print("[build]    ⚠ 缺", fn, flush=True)

    # 瓦片：无 _gcj 后缀 → tiles/；有 _gcj 后缀 → tiles_gcj/（剥后缀）
    tdir = os.path.join(OUT, "tiles")
    n_wgs = n_gcj = 0
    for layer in LAYERS:
        for root, _dirs, files in os.walk(os.path.join(tdir, layer)):
            for f in files:
                if not f.endswith(".png"):
                    continue
                src = os.path.join(root, f)
                rel_dir = os.path.relpath(root, tdir)      # <layer>/<z>/<x>
                if f.endswith("_gcj.png"):
                    dst_dir = os.path.join(DIST, "tiles_gcj", rel_dir)
                    dst = os.path.join(dst_dir, f[:-len("_gcj.png")] + ".png")
                    n_gcj += 1
                else:
                    dst_dir = os.path.join(DIST, "tiles", rel_dir)
                    dst = os.path.join(dst_dir, f)
                    n_wgs += 1
                os.makedirs(dst_dir, exist_ok=True)
                shutil.copy(src, dst)
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
    step_analyze()
    proc = step_serve()
    try:
        step_warm_tiles()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
    step_assemble()


if __name__ == "__main__":
    main()
