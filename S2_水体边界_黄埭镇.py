# -*- coding: utf-8 -*-
"""
黄埭镇 哨兵二号 水体边界 精修脚本
=================================
目标：让水体边界识别"像 Google / GEE 一样干净"。

方法（直接对应用户 GEE 参考代码里的做法）：
  1) 拉取 JRC 全球水体(JRC/GSW1_3) 历史频率 occurrence（多年度 Landsat 合成，
     30m，稳定性极佳）—— 这是 Google 类服务做水体参考的同一类产品。
  2) 用 S2 的 MNDWI(B03/B11) 在 10m 上对 JRC 参考做"细化"：
       水体 = (JRC频率>=10%) 且 (MNDWI 非强陆地)   —— 稳定核心水体
            ∪ (MNDWI>0.15)                          —— 补回 S2 高置信(养殖塘/新藻华水体)
  3) 形态学开/闭运算 + 填洞，去除椒盐噪声。
  4) 矢量化为多边形 + Douglas-Peucker 简化(≈15m)，渲染成光滑边界（不再是一块块像素）。
  5) 交互地图：真彩 + 精修水体(蓝,光滑) + NDCI + 藻华 + "旧掩膜(对照,红,像素块)" 可切换
     + JRC 参考轮廓 + 权威行政边界。

依赖：rasterio/numpy/folium(已装) + shapely/scipy(已装到 /tmp/pylibs)
用法：
  PYTHONPATH=/tmp/pylibs GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR CPL_VSIL_CURL_USE_HEAD=NO \
      /usr/bin/python3 S2_水体边界_黄埭镇.py
"""
import argparse, json, os, sys, math, base64, urllib.parse
import numpy as np
import urllib.request
from rasterio.windows import from_bounds
from rasterio.warp import reproject, Resampling, transform_bounds
from rasterio.transform import from_bounds as fb
from rasterio.features import rasterize, shapes
from rasterio.io import MemoryFile
import rasterio
from PIL import Image
import folium
from scipy import ndimage
from shapely.geometry import shape, mapping
import shapely.ops

# ---------- GDAL / 网络 稳定性配置 ----------
# 防止 /vsicurl 远程读无限挂起：设 HTTP 超时 + 低速断开
os.environ.setdefault('GDAL_HTTP_TIMEOUT','60')
os.environ.setdefault('GDAL_HTTP_LOW_SPEED_LIMIT','10000')   # bytes/s
os.environ.setdefault('GDAL_HTTP_LOW_SPEED_TIME','20')        # 持续低于上限 20s 则断
os.environ.setdefault('GDAL_DISABLE_READDIR_ON_OPEN','EMPTY_DIR')
os.environ.setdefault('CPL_VSIL_CURL_USE_HEAD','NO')
os.environ.setdefault('VSI_CACHE','TRUE')
os.environ.setdefault('VSI_CACHE_SIZE','5000000')

# 所有 print 实时刷新（便于后台监控，不会被管道缓冲吃掉进度）
import builtins as _bi
_bp = _bi.print
def print(*a, **k):
    k.setdefault('flush', True)
    return _bp(*a, **k)

# ---------- 配准参数 ----------
CENTER_LON, CENTER_LAT = 120.558, 31.432
TARGET_RES = 10.0
BBOX_LONMIN, BBOX_LATMIN, BBOX_LONMAX, BBOX_LATMAX = 120.471, 31.402, 120.605, 31.470

# ---------- 阈值 ----------
MNDWI_THR  = 0.10
NDCI_BLOOM = 0.10
FAI_BLOOM  = 0.005
MCI_THR    = 0.005        # MCI 阈值（三指数联合判定用）
BLOOM_AREA_HA = 5.0
SCL_MASK = {0,1,2,3,8,9,10,11}
JRC_OCC_THR = 10          # JRC 历史水体频率 >=10% 视为参考水体
MNDWI_CONF  = 0.15        # S2 高置信水体阈值

NDCI_CMAP = ['#00008b','#0000ff','#00ffff','#00ff00','#ffff00','#ff0000']

# JRC Global Surface Water (Planetary Computer, COG, occurrence 0-100, 30m)
JRC_HREF = "https://ai4edataeuwest.blob.core.windows.net/jrcglobalwater/occurrence/occurrence_120E_40Nv1_3_2020cog.tif"

def _hex2rgb(h):
    h=h.lstrip('#'); return tuple(int(h[i:i+2],16) for i in (0,2,4))

def build_palette(cmap_hex, n=256):
    stops=[_hex2rgb(c) for c in cmap_hex]
    lut=np.zeros((n,3),dtype=np.uint8)
    for i in range(n):
        t=i/(n-1)*(len(stops)-1); k=int(t); f=t-k
        a=stops[k]; b=stops[min(k+1,len(stops)-1)]
        lut[i]=(int(a[0]+(b[0]-a[0])*f),int(a[1]+(b[1]-a[1])*f),int(a[2]+(b[2]-a[2])*f))
    return lut

def stretch_truecolor(rgb, pmin=2, pmax=98):
    out=rgb.astype(np.float32)
    for i in range(3):
        lo,hi=np.percentile(rgb[i],(pmin,pmax))
        if hi>lo: out[i]=np.clip((rgb[i]-lo)/(hi-lo),0,1)
    return (out*255).clip(0,255).astype(np.uint8)

def index_to_png(idx, water_mask, cmap_hex, vmin, vmax, out_png):
    lut=build_palette(cmap_hex)
    nod=np.isnan(idx)
    val=np.clip(idx,vmin,vmax)
    val=np.nan_to_num(val, nan=vmin)          # 避免 NaN→int64(min) 导致 lut 越界
    frac=((val-vmin)/(vmax-vmin)*(len(lut)-1)).astype(int)
    rgb=lut[frac]
    alpha=np.where((~nod)&water_mask,235,0).astype(np.uint8)
    Image.fromarray(np.dstack([rgb,alpha])).save(out_png)
    return out_png

def bloom_to_png(bloom_mask, out_png):
    rgb=np.zeros(bloom_mask.shape+(3,),dtype=np.uint8)
    rgb[bloom_mask]=[255,0,0]
    alpha=np.where(bloom_mask,255,0).astype(np.uint8)
    Image.fromarray(np.dstack([rgb,alpha])).save(out_png)
    return out_png

def mask_to_png(mask, color, out_png, alpha=180):
    rgb=np.zeros(mask.shape+(3,),dtype=np.uint8)
    rgb[mask]=color
    a=np.where(mask,alpha,0).astype(np.uint8)
    Image.fromarray(np.dstack([rgb,a])).save(out_png)
    return out_png

def read_band_window(url, dst_transform, dst_shape, dst_crs, resampling=Resampling.bilinear, dst_bounds=None):
    with rasterio.open('/vsicurl/'+url) as src:
        xmin,ymin,xmax,ymax = dst_bounds
        sb = transform_bounds('EPSG:4326', src.crs, xmin,ymin,xmax,ymax)
        w=from_bounds(*sb, src.transform)
        data=src.read(1,window=w)
        src_t=src.window_transform(w)
    dst=np.full(dst_shape,np.nan,dtype=np.float32)
    reproject(source=data, src_transform=src_t, src_crs=src.crs,
              destination=dst, dst_transform=dst_transform, dst_crs=dst_crs,
              resampling=resampling)
    return dst

def get_jrc_occurrence(dst_transform, dst_shape, dst_bounds):
    """窗口化读取 JRC occurrence（只取 bbox 范围，绝不读整块全球瓦片）"""
    sgn=json.load(urllib.request.urlopen(
        "https://planetarycomputer.microsoft.com/api/sas/v1/sign?href="+
        urllib.parse.quote(JRC_HREF, safe=''), timeout=30))
    surl=sgn['href']
    with rasterio.open('/vsicurl/'+surl) as src:
        xmin,ymin,xmax,ymax=dst_bounds
        # JRC 是 EPSG:4326 地理坐标，直接按经纬度窗口裁剪
        sb=transform_bounds('EPSG:4326', src.crs, xmin,ymin,xmax,ymax)
        w=from_bounds(*sb, src.transform)
        data=src.read(1, window=w)
        src_t=src.window_transform(w)
        occ=np.full(dst_shape, np.nan, dtype=np.float32)
        reproject(source=data, src_transform=src_t, src_crs=src.crs,
                  destination=occ, dst_transform=dst_transform, dst_crs='EPSG:4326',
                  resampling=Resampling.bilinear)
    return occ

def vectorize_water(water, dst_transform, tol_m=15.0):
    """把二值水体掩膜转为简化(光滑)矢量多边形 -> GeoJSON FeatureCollection"""
    polys=[]
    for geom,val in shapes(water.astype(np.uint8), transform=dst_transform):
        if val==1:
            g=shape(geom).simplify(tol_m, preserve_topology=True)
            if not g.is_valid:
                g=shapely.ops.unary_union([g.buffer(0)])  # 修复自交/洞拓扑
            if g.geom_type=='Polygon' and g.area>0:
                polys.append(g)
            elif g.geom_type=='MultiPolygon':
                for gg in g.geoms:
                    if gg.area>0: polys.append(gg)
    if not polys:
        return None
    try:
        mp = shapely.ops.unary_union(polys) if len(polys)>1 else polys[0]
        if not mp.is_valid:
            mp = shapely.ops.unary_union([mp.buffer(0)])
    except Exception as e:
        print(f"  [warn] 矢量合并失败({e}), 退回逐个多边形")
        from shapely.geometry import MultiPolygon
        mp = MultiPolygon(polys)
    fc={"type":"FeatureCollection",
        "features":[{"type":"Feature",
                     "properties":{"name":"黄埭镇水体边界 (JRC参考+S2 10m细化)"},
                     "geometry":mapping(mp)}]}
    return fc, mp

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--out',default='S2_水体边界_黄埭镇.html')
    args=ap.parse_args()

    # 1) S2 07-21 场景（带 npz 缓存，避免重复下载）
    item_path='/tmp/s2_item_07-21.json'
    if not os.path.exists(item_path):
        api=("https://earth-search.aws.element84.com/v1/collections/sentinel-2-l2a/items"
             f"?bbox={BBOX_LONMIN},{BBOX_LATMIN},{BBOX_LONMAX},{BBOX_LATMAX}"
             "&datetime=2026-07-19T00:00:00Z/2026-07-23T00:00:00Z&limit=20")
        item=json.load(urllib.request.urlopen(api,timeout=25))["features"][0]
        json.dump(item,open(item_path,'w'))
    else:
        item=json.load(open(item_path))
    A=item['assets']
    scene_id=item['id']; scene_date=item['properties']['datetime'][:10]
    print(f"[场景] {scene_id}  日期 {scene_date}  云量 {item['properties']['eo:cloud_cover']:.1f}%")
    hrefs={k:A[k]['href'] for k in ['blue','green','red','rededge1','rededge2','nir','swir16','scl']}

    lon0,lon1=BBOX_LONMIN,BBOX_LONMAX
    lat0,lat1=BBOX_LATMIN,BBOX_LATMAX
    dst_bounds=(lon0,lat0,lon1,lat1)
    ncol=int(round((lon1-lon0)*111320*math.cos(math.radians(CENTER_LAT))/TARGET_RES))
    nrow=int(round((lat1-lat0)*110540/TARGET_RES))
    dst_transform=fb(lon0,lat0,lon1,lat1,ncol,nrow)
    dst_shape=(nrow,ncol); dst_crs='EPSG:4326'
    print(f"[网格] {ncol}x{nrow} px @10m, 范围 {lon0:.4f}~{lon1:.4f}E / {lat0:.4f}~{lat1:.4f}N")

    cache='/tmp/s2_bands_0721.npz'
    if os.path.exists(cache):
        print("[读取] 使用缓存波段 ...")
        d=np.load(cache)
        B02,B03,B04,B05,B06,B08,B11,SCL=d['B02'],d['B03'],d['B04'],d['B05'],d['B06'],d['B08'],d['B11'],d['SCL']
    else:
        def rb(url,resampling=Resampling.bilinear,scale=10000.0):
            a=read_band_window(url,dst_transform,dst_shape,dst_crs,resampling,dst_bounds)
            return a/scale if scale else a
        print("[读取] 拉取波段 B02/B03/B04/B05/B06/B08/B11/SCL ...")
        B02=rb(hrefs['blue']); B03=rb(hrefs['green']); B04=rb(hrefs['red'])
        B05=rb(hrefs['rededge1']); B06=rb(hrefs['rededge2']); B08=rb(hrefs['nir'])
        B11=rb(hrefs['swir16']); SCL=rb(hrefs['scl'],Resampling.nearest,scale=0).astype(int)
        np.savez(cache, B02=B02,B03=B03,B04=B04,B05=B05,B06=B06,B08=B08,B11=B11,SCL=SCL)
        print(f"  [缓存] {cache}")

    cloud=np.isin(SCL,list(SCL_MASK)) if SCL.size else np.zeros(dst_shape,dtype=bool)
    valid=~np.isnan(B04)&~cloud

    # 2) 指数
    MNDWI=(B03-B11)/(B03+B11+1e-6)
    NDCI =(B05-B04)/(B05+B04+1e-6)
    f=(842.0-665.0)/(1610.0-665.0)
    FAI=B08-(B04+(B11-B04)*f)
    MCI=B05-B04-(B06-B04)*((705-665)/(740-665))

    # 旧方法（对照）：纯 MNDWI>0.10 & SCL==6
    water_old=(MNDWI>MNDWI_THR)&valid&(SCL==6)

    # 3) JRC 参考 + S2 细化
    print("[JRC] 拉取全球水体历史频率 occurrence ...")
    occ=get_jrc_occurrence(dst_transform,dst_shape,dst_bounds)
    jrc_water = (occ>=JRC_OCC_THR)
    print(f"  JRC 频率>=10% 像元: {int(jrc_water.sum())}  (对应 ≈ {jrc_water.sum()*100/1e4:.1f} ha)")
    # 精修水体
    water_new = (jrc_water & (MNDWI > -0.10)) | (MNDWI > MNDWI_CONF)
    water_new &= valid
    # 形态学去噪
    water_new = ndimage.binary_opening(water_new.astype(np.uint8), iterations=1).astype(bool)
    water_new = ndimage.binary_closing(water_new.astype(np.uint8), iterations=1).astype(bool)
    water_new = ndimage.binary_fill_holes(water_new)

    # 藻华：多指数联合(AND) —— 必须 NDCI / FAI / MCI 同时超阈才判为藻华，
    # 避免单层 NDCI>0.10 在浑浊养殖塘上过拟合（OR 写法曾导致 50% 水体被标红）。
    bloom=water_new&(NDCI>NDCI_BLOOM)&(FAI>FAI_BLOOM)&(MCI>MCI_THR)

    # 权威边界
    bd=json.load(open('黄埭镇边界.json'))
    bd_outer=[[p[0],p[1]] for p in bd['outer']]
    if bd_outer[0]!=bd_outer[-1]: bd_outer=bd_outer+[bd_outer[0]]
    bd_geom={'type':'Polygon','coordinates':[bd_outer]}
    bd_mask=rasterize([(bd_geom,1)], out_shape=dst_shape, transform=dst_transform, fill=0).astype(bool)

    def ha(n): return n*TARGET_RES*TARGET_RES/1e4
    n_old=int(water_old.sum()); n_new=int(water_new.sum()); n_bloom=int(bloom.sum())
    print(f"\n=========== 水体边界精修结果 ({scene_date}) ===========")
    print(f"  旧掩膜(对照) 水体: {n_old} px ≈ {ha(n_old):.1f} ha")
    print(f"  精修(JRC+S2) 水体: {n_new} px ≈ {ha(n_new):.1f} ha  (较旧 +{ha(n_new-n_old):.1f} ha)")
    print(f"  藻华: {n_bloom} px ≈ {ha(n_bloom):.2f} ha")
    wi=water_new&bd_mask
    print(f"  边界内精修水体: {int(wi.sum())} px ≈ {ha(wi.sum()):.1f} ha (占比 {100*wi.sum()/max(n_new,1):.1f}%)")
    if wi.sum():
        yy,xx=np.where(wi)
        clat=float(np.mean([(dst_transform*(0,int(r)))[1] for r in yy]))
        clon=float(np.mean([(dst_transform*(int(c),0))[0] for c in xx]))
        print(f"  边界内水体质心: {clon:.5f}E, {clat:.5f}N  (应在权威边界内)")
    if n_new:
        wnd=NDCI[water_new]
        yy,xx=np.where(water_new)
        water_lat=float(np.mean([(dst_transform*(0,int(r)))[1] for r in yy]))
        print(f"  水体质心纬度 ≈ {water_lat:.5f}°N  (漕湖在北/春申湖在南 → 应>31.432)")
        print(f"  水体 NDCI: p50={np.percentile(wnd,50):+.3f} p90={np.percentile(wnd,90):+.3f} max={wnd.max():+.3f}")
    print("=======================================================\n")

    # 4) 矢量化简 -> GeoJSON
    print("[矢量] 水体边界简化(≈15m) ...")
    vec=vectorize_water(water_new, dst_transform, tol_m=15.0)
    if vec is None:
        print("  [警告] 无有效水体多边形"); fc=None; mp=None
    else:
        fc,mp=vec
        json.dump(fc, open('S2_水体边界_黄埭镇_water.geojson','w'), ensure_ascii=False)
        print(f"  [GeoJSON] S2_水体边界_黄埭镇_water.geojson  (多边形数≈{len(mp.geoms) if mp.geom_type=='MultiPolygon' else 1})")

    # 5) 渲染图层
    print("[渲染] 生成真彩 / NDCI / 藻华 / 旧掩膜对照 ...")
    rgb=np.stack([B04,B03,B02],0); rgb=np.clip(rgb,0,0.4)
    rgb_s=np.transpose(stretch_truecolor(rgb),(1,2,0))
    rgb_png='S2_水体边界_黄埭镇_rgb.png'; Image.fromarray(rgb_s).save(rgb_png)
    ndci_png=index_to_png(NDCI,water_new.astype(bool),NDCI_CMAP,-0.05,0.15,'S2_水体边界_黄埭镇_ndci.png')
    bloom_png=bloom_to_png(bloom,'S2_水体边界_黄埭镇_bloom.png')
    old_png=mask_to_png(water_old,[255,60,60],'S2_水体边界_黄埭镇_old.png',alpha=170)
    # 精修水体栅格(蓝,半透明) 也出一张，便于与旧对比
    new_png=mask_to_png(water_new,[30,144,255],'S2_水体边界_黄埭镇_water.png',alpha=150)
    print(f"  已生成 {rgb_png}, {ndci_png}, {bloom_png}, {old_png}, {new_png}")

    def save_tif(arr, path, dtype='float32', nodata=None):
        a=arr.astype(dtype)
        with rasterio.open(path, mode='w', driver='GTiff',
                           height=a.shape[0],width=a.shape[1],count=1,
                           dtype=dtype,crs=dst_crs,transform=dst_transform,nodata=nodata) as d:
            d.write(a,1)
        print(f"  [GeoTIFF] {path}")
    save_tif(NDCI,'S2_水体边界_黄埭镇_ndci.tif')
    save_tif(water_new.astype('uint8'),'S2_水体边界_黄埭镇_water.tif',dtype='uint8',nodata=0)
    save_tif(occ,'S2_水体边界_黄埭镇_jrc_occ.tif')

    # 6) Folium 地图
    b=dst_bounds
    m=folium.Map(location=[CENTER_LAT,CENTER_LON],zoom_start=12,control_scale=True)
    def b64(p):
        with open(p,'rb') as f: return 'data:image/png;base64,'+base64.b64encode(f.read()).decode()
    folium.raster_layers.ImageOverlay(b64(rgb_png),bounds=[[b[1],b[0]],[b[3],b[2]]],name='S2 真彩(10m)',opacity=1.0).add_to(m)
    folium.raster_layers.ImageOverlay(b64(ndci_png),bounds=[[b[1],b[0]],[b[3],b[2]]],name='NDCI 叶绿素',opacity=0.7).add_to(m)
    folium.raster_layers.ImageOverlay(b64(bloom_png),bounds=[[b[1],b[0]],[b[3],b[2]]],name='藻华红点',opacity=0.9).add_to(m)
    # 旧掩膜(对照, 红, 像素块)
    folium.raster_layers.ImageOverlay(b64(old_png),bounds=[[b[1],b[0]],[b[3],b[2]]],name='旧掩膜(对照,MNDWI+SCL)',opacity=0.6).add_to(m)
    # JRC 参考轮廓（虚线）
    jrc_outline=None
    if occ is not None:
        jrc_mask=(occ>=JRC_OCC_THR)
        jrc_mask=ndimage.binary_opening(jrc_mask.astype(np.uint8),iterations=1).astype(bool)
        v=vectorize_water(jrc_mask,dst_transform,tol_m=20.0)
        if v is not None:
            jrc_fc,_=v
            folium.GeoJson(jrc_fc, name='JRC 全球水体参考(30m)',
                           style_function=lambda f:{'color':'#00ffd0','weight':1,'fill':False,'opacity':0.7,'dashArray':'4,4'}).add_to(m)
    # 精修水体（蓝, 光滑矢量）
    if fc is not None:
        folium.GeoJson(fc, name='精修水体边界(蓝,光滑)',
                       style_function=lambda f:{'color':'#1e90ff','weight':1.5,'fillColor':'#1e90ff','fillOpacity':0.30,'opacity':0.95}).add_to(m)
    folium.Marker([CENTER_LAT,CENTER_LON],tooltip='★黄埭镇',icon=folium.Icon(color='red',icon='star')).add_to(m)
    # 权威行政边界
    poly={'type':'Feature','geometry':{'type':'Polygon','coordinates':[[[p[0],p[1]] for p in bd['outer']]]},'properties':{}}
    for h in bd.get('holes',[]): poly['geometry']['coordinates'].append([[p[0],p[1]] for p in h])
    folium.GeoJson(poly, name='黄埭镇行政边界(权威)',
                   style_function=lambda f:{'color':'#ffd000','weight':2,'fill':False,'opacity':0.9}).add_to(m)
    html=(f"<div style='font-size:13px;line-height:1.5'>"
          f"<b>哨兵二号 水体边界精修</b> · {scene_date}<br>"
          f"精修水体 ≈ {ha(n_new):.0f} ha ({n_new} px)<br>"
          f"较旧掩膜 +{ha(n_new-n_old):.0f} ha<br>"
          f"藻华 ≈ {ha(n_bloom):.2f} ha ({n_bloom} px)<br>"
          f"方法: JRC频率≥10% + S2 MNDWI 10m 细化 + 矢量化简</div>")
    folium.map.Marker([b[1],b[0]],icon=folium.DivIcon(html=html)).add_to(m)
    folium.LayerControl().add_to(m)
    m.save(args.out)
    print(f"[完成] 地图已写出: {args.out}")

if __name__=='__main__':
    main()
