# -*- coding: utf-8 -*-
"""
蓝藻(藻华)检测 —— Otsu 自适应阈值 + CMI 水生植物掩膜
======================================================

基于期刊验证的指数法与自适应阈值，替代原先的"伪标签随机森林"：

  * NDCI  (Mishra & Mishra 2012, Remote Sensing of Environment)
        (B5 - B4) / (B5 + B4)，随 Chl-a 增大，适合浑浊水体。
  * FAI   (Hu 2009, Remote Sensing of Environment)
        B8 - [B4 + (λNIR-λRed)/(λSWIR-λRed)·(B11 - B4)]，识别漂浮藻类。
  * CMI   (Liang et al. 2017)
        藻蓝蛋白(phycocyanobilin)吸收指数，用于区分蓝藻与挺水/沉水植物
        （二者 NIR 光谱相近，FAI 易混淆）；蓝藻 CMI 偏正、水草偏负。
        CMI = B3 - [B2 + (λG-λB)/(λSWIR-λB)·(B11 - B2)]
        （S2: B2=490nm, B3=560nm, B11=1610nm）。

  * Otsu 1979 自动阈值：对水体内 NDCI / FAI 分别做 Otsu 分割
        （Yan 2022 等对 FAI/CMI 采用 Otsu 抑制近零背景噪声），
        比固定 0.10 更自适应；样本单峰/方差过小则回退经验阈值。

算法：
    1. 水体内 NDCI / FAI 各自 Otsu → 阈值 t_ndci / t_fai；
    2. 蓝藻候选 = water & (NDCI > t_ndci) & (FAI > t_fai)；
    3. 水生植物掩膜 = water & (NDVI > 0.35) & (CMI < 0) → 从候选剔除；
    4. 概率图 = 水体内 sigmoid((NDCI - t_ndci)/0.05)，水生植物区归 0。

优点（相对伪标签 RF）：
    * 阈值由数据自适应（Otsu），不再"一刀切"；
    * 引入 CMI 区分蓝藻 / 水草，减少 Vegetation 误判；
    * 纯 numpy 实现，无需 sklearn，可复现、可解释；
    * 决策依据对应可引用的期刊方法，而非自造标签。

依赖: numpy only。
"""
import numpy as np

# S2 波段中心波长 (nm)
WL_B2, WL_B3, WL_B11 = 490.0, 560.0, 1610.0


def _otsu(vals):
    """对一维数组做 Otsu 最优阈值；样本不足/单峰/方差为 0 时返回 None。"""
    vals = vals[np.isfinite(vals)]
    if vals.size < 20:
        return None
    lo, hi = float(vals.min()), float(vals.max())
    if hi - lo < 1e-4:
        return None
    hist, edges = np.histogram(vals, bins=128, range=(lo, hi))
    hist = hist.astype(np.float64)
    total = hist.sum()
    if total <= 0:
        return None
    centers = (edges[:-1] + edges[1:]) / 2.0
    sum_total = float((np.arange(hist.size) * hist).sum())
    sum_b = 0.0
    w_b = 0.0
    max_var = -1.0
    thr = None
    for i in range(1, hist.size):
        w_b += hist[i - 1]
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += (i - 1) * hist[i - 1]
        m_b = sum_b / w_b
        m_f = (sum_total - sum_b) / w_f
        var_b = w_b * w_f * (m_b - m_f) ** 2
        if var_b > max_var:
            max_var = var_b
            thr = (centers[i - 1] + centers[i]) / 2.0
    return thr


def detect(B02, B03, B04, B05, B06, B08, B11, water,
           ndci_fallback=0.10, fai_fallback=0.005, seed=42):
    """
    返回 (bloom_ml, prob)：
      bloom_ml : bool (H,W)，判定的藻华二值图（限定在水体内）
      prob     : float (H,W)，藻华概率 0~1（水体内 sigmoid；水生植物区=0）
    """
    H, W = water.shape
    water = water.astype(bool)

    # ---- 1) 指数 ----
    NDCI = (B05 - B04) / (B05 + B04 + 1e-6)
    f = (842.0 - 665.0) / (1610.0 - 665.0)
    FAI = B08 - (B04 + (B11 - B04) * f)
    c = (WL_B3 - WL_B2) / (WL_B11 - WL_B2)
    CMI = B03 - (B02 + c * (B11 - B02))          # Liang 2017
    NDVI = (B08 - B04) / (B08 + B04 + 1e-6)

    wf = water.reshape(-1)
    nd = NDCI.reshape(-1)
    fa = FAI.reshape(-1)
    cm = CMI.reshape(-1)
    nv = NDVI.reshape(-1)

    # ---- 2) Otsu 自适应阈值（仅在水体内）----
    t_ndci = _otsu(nd[wf]) or ndci_fallback
    t_fai = _otsu(fa[wf]) or fai_fallback
    print(f"[ml] Otsu 阈值: NDCI={t_ndci:.3f} (回退{ndci_fallback}) "
          f"FAI={t_fai:.3f} (回退{fai_fallback})")

    # ---- 3) 蓝藻候选 ----
    cand = (nd > t_ndci) & (fa > t_fai) & wf

    # ---- 4) 水生植物掩膜（CMI 区分蓝藻/水草）----
    macrophyte = wf & (nv > 0.35) & (cm < 0)
    n_macro = int(macrophyte.sum())
    if n_macro:
        removed = int((cand & macrophyte).sum())
        cand = cand & (~macrophyte)
        print(f"[ml] CMI 排除水生植物 {n_macro}px（其中重叠藻华候选 {removed}px）")
    else:
        print("[ml] 未检测到明显水生植物掩膜")

    bloom_ml = cand.reshape(H, W)

    # ---- 5) 概率图：水体内 sigmoid((NDCI - t_ndci)/0.05) ----
    prob = np.zeros(wf.shape[0], dtype=np.float32)
    prob[wf] = 1.0 / (1.0 + np.exp(-(nd[wf] - t_ndci) / 0.05))
    prob[macrophyte] = 0.0
    return bloom_ml, prob.reshape(H, W)


if __name__ == "__main__":
    # 简单自检：随机构造波段，注入少量强藻华，跑通流程
    rng = np.random.default_rng(0)
    shape = (60, 60)
    bands = {b: rng.random(shape) * 0.05 + 0.02
             for b in ["B02", "B03", "B04", "B05", "B06", "B08", "B11"]}
    water = rng.random(shape) > 0.4
    # 注入蓝藻斑块：高红边、高 NIR、高 FAI
    bands["B05"][10:20, 10:20] = 0.18
    bands["B04"][10:20, 10:20] = 0.04
    bands["B08"][10:20, 10:20] = 0.20
    bands["B11"][10:20, 10:20] = 0.10
    bm, pr = detect(bands["B02"], bands["B03"], bands["B04"], bands["B05"],
                    bands["B06"], bands["B08"], bands["B11"], water)
    print("self-test OK | bloom px:", int(bm.sum()),
          "prob range:", float(pr.min()), round(float(pr.max()), 3))
