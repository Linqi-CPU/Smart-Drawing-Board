"""分段包络估计 —— 纯计算层，不依赖 tkinter。

用途（按需求）
--------------
真实测量/仿真得到的离散点会因为误差围绕真实曲线散布。
本模块做两件事：

1. **估计整体走势**：用分段均值把点切成上、下两支，分别拟合后**逐位取平均**，
   得到不偏向任何一支的中心趋势线 `f(x)`。这就是"散点的整体走势"。
2. **刻画离散程度**：上下两支之差 `d(x) = f_up(x) - f_lo(x)` 是散点围绕
   `f(x)` 的散布宽度（误差带）。输出 `d(x)` 的图像与统计，
   用来描述"这个数据对于该拟合函数的离散情况"——
   哪一段集中、哪一段发散、离散程度大致服从什么规律。

注意：这里**不判断数学收敛性**。`d(x)` 收窄只说明该区间测量更准，
不说明任何极限存在；`d(x)` 走平也只说明该区间散布范围稳定。
所有结论都描述"离散程度"，不下数学断言。

算法步骤
--------
1. **分段均值**：x 轴等宽分段，算每段 y 均值，构成阶梯函数 m(x)。
2. **上下分组**：y >= m(x) 归上组，否则归下组。
3. **分别拟合**：两组各做同阶多项式最小二乘 → f_up、f_lo。
4. **系数调和**：逐位取平均 (a_up + a_lo)/2 → g(x)。
5. **偏移常数 C**：以两支在全量点上的残差均值为基准，取 g 与其偏差，
   并入常数项 → 最终走势线 f(x) = g(x) + C。
6. **离散度分析**：对 d(x) = f_up - f_lo 逐段统计绝对带宽、
   段内极差、上下组点数，并按带宽相对首段的变化趋势给出描述。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Sequence, Tuple

import fitting as ft

#: 默认分段数
DEFAULT_SEGMENTS = 8
#: 默认拟合阶数
DEFAULT_DEGREE = 2
#: 默认系数一致性容差（保留参数位，当前 mean 规则不使用）
DEFAULT_COEF_TOL = 1e-9


class BandFitError(Exception):
    """分段包络估计失败。"""


@dataclass(frozen=True)
class SegmentBand:
    """一个分段的包络信息。"""

    index: int
    x_lo: float
    x_hi: float
    center: float
    mean_y: float          # 分段均值 m(x)
    n_upper: int           # 段内落在均值上方的点数
    n_lower: int           # 段内落在均值下方的点数
    d_center: float        # f_up(center) - f_lo(center)
    d_mean_points: float   # 段内离散宽度（两支差取绝对值后的平均）
    spread: float          # 段内 y 的极差（最直接的离散程度）


@dataclass(frozen=True)
class ScatterProfile:
    """散点围绕走势线的离散程度画像。

    只描述"离散程度如何变化"，不判断收敛。
    """

    d_values: tuple          # 各分段的离散宽度 d(x)
    spreads: tuple           # 各分段内 y 的实测极差
    head_mean: float         # 前三分段平均宽度
    tail_mean: float         # 后三分段平均宽度
    head_spread: float       # 前三分段平均极差
    tail_spread: float       # 后三分段平均极差
    tail_ratio: float        # tail_mean / head_mean（宽度的后/前比）
    min_d: float
    max_d: float
    mean_d: float
    widest_segment: int      # 离散宽度最大的分段下标
    tightest_segment: int    # 离散宽度最小的分段下标
    # "narrowing"  宽度大致递减   -> 后段更集中
    # "stable"     宽度大致恒定   -> 各段离散程度相当
    # "widening"   宽度大致递增   -> 后段更发散
    # "mixed"      无明显单调规律
    trend: str
    swing: float             # (max_d - min_d) / mean_d，宽度起伏强度
    description: str         # 给人读的一句结论


@dataclass(frozen=True)
class BandFitResult:
    """分段包络估计的完整结果。"""

    upper: ft.FitResult               # 上界拟合
    lower: ft.FitResult               # 下界拟合
    reconciled: tuple                 # 上下界系数的逐位平均（未加 C）
    offset: float                     # 偏移常数 C
    coefficients: tuple               # 最终系数（C 已并入常数项）
    expression: str                   # 最终走势线公式
    segments: tuple                   # tuple[SegmentBand]
    scatter: ScatterProfile           # 离散程度画像
    degree_upper: int
    degree_lower: int
    n_points: int
    n_segments: int
    y_min: float
    y_max: float
    r2: float = 0.0                   # 走势线在全量点上的 R²
    rmse: float = 0.0                 # 走势线在全量点上的 RMSE
    # 走势线作为"中心线"的贴合度：mean(|y_i - f(x_i)|)
    mean_abs_dev: float = 0.0
    caution: str = ""                 # 结论不可靠时的提示

    def predict(self, x: float) -> float:
        acc = 0.0
        for c in reversed(self.coefficients):
            acc = acc * float(x) + c
        return acc

    def predict_many(self, xs) -> list:
        return [self.predict(x) for x in xs]

    def band_at(self, x: float) -> Tuple[float, float]:
        """返回该 x 处的 (上界, 下界)。"""
        return self.upper.predict(x), self.lower.predict(x)

    def dev_at(self, x: float) -> float:
        """返回该 x 处的离散宽度（上下界之差，非负）。"""
        return abs(self.upper.predict(x) - self.lower.predict(x))

    @property
    def summary(self) -> str:
        s = self.scatter
        return (
            f"整体走势: {self.expression}\n"
            f"上界:     {self.upper.expression}\n"
            f"下界:     {self.lower.expression}\n"
            f"偏移常数 C = {self.offset:.6g}\n"
            f"离散宽度: 平均 {s.mean_d:.4g}，"
            f"范围 [{s.min_d:.4g}, {s.max_d:.4g}]\n"
            f"离散趋势: {s.description}"
        )

    @property
    def derivation(self) -> str:
        """走势线的推导过程，便于人工核对每一步。"""
        return (
            "系数调和规则：上界与下界逐位取平均\n"
            f"  上界系数 {tuple(self.upper.coefficients)}\n"
            f"  下界系数 {tuple(self.lower.coefficients)}\n"
            f"  平均后   {tuple(self.reconciled)}\n"
            f"  偏移常数 C = {self.offset:.6g}\n"
            f"  最终走势 {tuple(self.coefficients)}\n"
            f"  R² = {self.r2:.6f}   RMSE = {self.rmse:.6g}\n"
            f"  平均绝对偏差 = {self.mean_abs_dev:.6g}"
        )


# ==================================================================
# 分段均值
# ==================================================================
def make_segments(xs: Sequence[float], n_segments: int) -> List[Tuple[float, float]]:
    """按 x 的极差等宽分段，返回 [(x_lo, x_hi), ...]。"""
    if len(xs) < 2:
        raise BandFitError("至少需要 2 个点才能分段")
    lo, hi = min(xs), max(xs)
    if hi - lo <= 0:
        raise BandFitError("所有 x 相同，无法分段")
    n = max(1, int(n_segments))
    width = (hi - lo) / n
    return [(lo + i * width, lo + (i + 1) * width) for i in range(n)]


def segment_of(x: float, segments: Sequence[Tuple[float, float]]) -> int:
    """x 落在第几段（最后一段右闭）。"""
    for i, (lo, hi) in enumerate(segments):
        if x < hi or i == len(segments) - 1:
            if x >= lo:
                return i
    return 0


def segment_means(xs, ys, segments) -> Tuple[List[float], List[int]]:
    """每段 y 的均值与该段的点数。空段均值为 nan，调用方需过滤。"""
    sums = [0.0] * len(segments)
    counts = [0] * len(segments)
    for x, y in zip(xs, ys):
        i = segment_of(float(x), segments)
        sums[i] += float(y)
        counts[i] += 1
    means = [sums[i] / counts[i] if counts[i] else float("nan")
             for i in range(len(segments))]
    return means, counts


# ==================================================================
# 分支拟合
# ==================================================================
def _fit_branch(points: Sequence, degree: int) -> Tuple[ft.FitResult, int]:
    """从高到低尝试降阶，直到能拟合为止。"""
    if not points:
        raise BandFitError("该分支没有任何点")
    last_err = None
    for d in range(max(0, int(degree)), -1, -1):
        try:
            return ft.fit_polynomial(points, d), d
        except ft.FitError as e:
            last_err = e
            continue
    raise BandFitError(f"分支点数过少，无法拟合: {last_err}")


def _poly_eval(coeffs: Sequence[float], x: float) -> float:
    acc = 0.0
    for c in reversed(coeffs):
        acc = acc * float(x) + c
    return acc


# ==================================================================
# 系数调和
# ==================================================================
def reconcile_coefficients(
    c_upper: Sequence[float],
    c_lower: Sequence[float],
    tol: float = DEFAULT_COEF_TOL,
    mode: str = "mean",
) -> List[float]:
    """调和上下两支的系数。

    mode="mean"（默认，当前在用）：逐位普通平均 (a_up + a_lo)/2。
                两支同号时得到两支正中间的线，不偏向任何一侧，
                也不放大系数。

    mode="spec"（旧规则，仅留档备查）：|a_up| 与 |a_lo| 一致时取该值，
                否则 (a_up + a_lo)/2 + (|a_up| + |a_lo|)/2。
                该式在两支同号时等于 (a_up + a_lo)，即系数翻倍；
                常数项翻倍可由 C 抵消，但斜率翻倍无法补救，故已不用。

    两支阶数不同时，短的一侧按 0 补齐。
    """
    n = max(len(c_upper), len(c_lower))
    up = list(c_upper) + [0.0] * (n - len(c_upper))
    lo = list(c_lower) + [0.0] * (n - len(c_lower))

    out = []
    for a, b in zip(up, lo):
        if mode == "mean":
            out.append((a + b) / 2.0)
        elif abs(abs(a) - abs(b)) <= tol:
            out.append(a)
        else:
            out.append((a + b) / 2.0 + (abs(a) + abs(b)) / 2.0)
    return out


# ==================================================================
# 离散程度分析
# ==================================================================
def profile_scatter(
    d_values: Sequence[float],
    spreads: Sequence[float],
) -> ScatterProfile:
    """根据各分段离散宽度与极差，描述散点的离散情况。

    只回答"离散程度怎么变化"，不回答"是否收敛"。
    """
    ds = [float(v) for v in d_values]
    sp = [float(v) for v in spreads]
    if not ds:
        raise BandFitError("没有离散宽度数据")
    if len(sp) != len(ds):
        raise BandFitError("离散宽度与极差序列长度不一致")

    third = max(1, len(ds) // 3)
    head = sum(ds[:third]) / third
    tail = sum(ds[-third:]) / third
    head_sp = sum(sp[:third]) / third
    tail_sp = sum(sp[-third:]) / third
    ratio = (tail / head) if abs(head) > 1e-12 else float("inf")

    min_d, max_d = min(ds), max(ds)
    mean_d = sum(ds) / len(ds)
    widest = ds.index(max_d)
    tightest = ds.index(min_d)

    # ---- 趋势判定：只看离散宽度大致走向 ----
    # 0.8 ~ 1.25 视为"基本持平"，超出才算变宽/变窄。
    if ratio < 0.8:
        trend = "narrowing"
        trend_desc = "后段比前段更集中"
    elif ratio > 1.25:
        trend = "widening"
        trend_desc = "后段比前段更发散"
    else:
        trend = "stable"
        trend_desc = "各段离散程度大致相当"

    # 起伏强度：最宽与最窄的落差相对均值。
    swing = (max_d - min_d) / mean_d if mean_d > 1e-12 else 0.0
    if swing > 1.0:
        law = f"离散宽度随位置变化明显（最宽段约是最窄段的 {swing + 1:.1f} 倍）"
    else:
        law = "离散宽度全程较平稳"

    description = (
        f"{trend_desc}；{law}。"
        f"平均离散宽度 {mean_d:.4g}，最宽在第 {widest} 段"
        f"（{max_d:.4g}），最窄在第 {tightest} 段（{min_d:.4g}）。"
    )
    if trend == "narrowing":
        description += "尾部取值比头部更集中。"
    elif trend == "widening":
        description += "尾部比头部更分散，需留意该区间数据质量。"
    elif trend == "stable":
        description += "全程散布范围接近，误差带近似等宽。"
    else:
        description += "离散宽度随位置起伏，无单调规律。"

    return ScatterProfile(
        d_values=tuple(ds),
        spreads=tuple(sp),
        head_mean=head,
        tail_mean=tail,
        head_spread=head_sp,
        tail_spread=tail_sp,
        tail_ratio=ratio,
        min_d=min_d,
        max_d=max_d,
        mean_d=mean_d,
        widest_segment=widest,
        tightest_segment=tightest,
        trend=trend,
        swing=swing,
        description=description,
    )


# ==================================================================
# 主入口
# ==================================================================
def band_fit(
    points: Sequence,
    n_segments: int = DEFAULT_SEGMENTS,
    degree: int = DEFAULT_DEGREE,
    coef_tol: float = DEFAULT_COEF_TOL,
) -> BandFitResult:
    """对离散点做分段包络估计，得到整体走势与离散程度画像。

    points   [(x, y), ...]  围绕真实曲线散布的离散点
    """
    if not points or len(points) < 2:
        raise BandFitError("至少需要 2 个离散点")
    if degree < 0:
        raise BandFitError("阶数不能为负")

    xs = [float(p[0]) for p in points]
    ys = [float(p[1]) for p in points]
    if max(xs) - min(xs) <= 0:
        raise BandFitError("所有 x 相同，无法分段")
    if max(ys) - min(ys) <= 0:
        raise BandFitError("所有 y 相同，无离散可言，无需估计走势")

    # 分段数不能超过点数的一半，否则会出现空段
    n_seg = max(2, min(int(n_segments), len(points) // 2))
    segments = make_segments(xs, n_seg)
    means, counts = segment_means(xs, ys, segments)

    # 分组：以所在段的均值为界
    upper: List[Tuple[float, float]] = []
    lower: List[Tuple[float, float]] = []
    for x, y in zip(xs, ys):
        i = segment_of(x, segments)
        m = means[i]
        if not math.isfinite(m):
            continue
        (upper if y >= m else lower).append((x, y))

    if not upper or not lower:
        raise BandFitError(
            "所有点都落在分段均值的同一侧，无法构成上下界。\n"
            "请增加分段数或补充更有起伏的数据。"
        )

    up_fit, deg_up = _fit_branch(upper, degree)
    lo_fit, deg_lo = _fit_branch(lower, degree)

    # ---- 系数调和：上下两支逐位取平均 ----
    reconciled = reconcile_coefficients(
        up_fit.coefficients, lo_fit.coefficients, coef_tol, mode="mean"
    )

    # ---- 偏移常数 C ----
    # 不能直接取 mean(y - g(x))：两支各自的均值已经代表了数据中心，
    # 那样会把中心位置再扣一次。以两支在全量点上的残差均值为基准，
    # 取平均曲线相对该基准的净偏差。
    up_r = sum(y - up_fit.predict(x) for x, y in zip(xs, ys)) / len(xs)
    lo_r = sum(y - lo_fit.predict(x) for x, y in zip(xs, ys)) / len(xs)
    g_r = sum(y - _poly_eval(reconciled, x) for x, y in zip(xs, ys)) / len(xs)
    offset = g_r - (up_r + lo_r) / 2.0

    final_coeffs = list(reconciled)
    final_coeffs[0] += offset
    expression = ft.format_polynomial(final_coeffs)

    # ---- 分段索引，避免 O(n²) 查找 ----
    seg_cix: List[List[int]] = [[] for _ in segments]
    for idx, x in enumerate(xs):
        seg_cix[segment_of(x, segments)].append(idx)

    seg_bands: List[SegmentBand] = []
    d_values: List[float] = []
    spreads: List[float] = []
    for i, (lo_x, hi_x) in enumerate(segments):
        if not math.isfinite(means[i]):
            continue
        center = (lo_x + hi_x) / 2.0
        idxs = seg_cix[i]
        seg_ys = [ys[k] for k in idxs]
        d_c = up_fit.predict(center) - lo_fit.predict(center)
        # 段内离散宽度：两支在该段各点上的差，取绝对值后平均
        # （两支斜率不同时后段可能交叉，必须取绝对值才有"宽度"意义）
        d_pts = [abs(up_fit.predict(xs[k]) - lo_fit.predict(xs[k]))
                 for k in idxs]
        d_mean = sum(d_pts) / len(d_pts) if d_pts else abs(d_c)
        n_up = sum(1 for k in idxs if ys[k] >= means[i])
        seg_bands.append(SegmentBand(
            index=i, x_lo=lo_x, x_hi=hi_x, center=center,
            mean_y=means[i], n_upper=n_up, n_lower=len(idxs) - n_up,
            d_center=d_c, d_mean_points=d_mean,
            spread=(max(seg_ys) - min(seg_ys)) if seg_ys else 0.0,
        ))
        d_values.append(d_mean)
        spreads.append(seg_bands[-1].spread)

    scatter = profile_scatter(d_values, spreads)

    # ---- 走势线的拟合质量 ----
    preds = [_poly_eval(final_coeffs, x) for x in xs]
    ybar = sum(ys) / len(ys)
    ss_tot = sum((y - ybar) ** 2 for y in ys)
    ss_res = sum((y - p) ** 2 for y, p in zip(ys, preds))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-12 else 1.0
    rmse = math.sqrt(ss_res / len(ys)) if ys else 0.0
    # 走势线应当穿过散点云中心，故平均绝对偏差应接近半带宽
    mean_abs_dev = sum(abs(y - p) for y, p in zip(ys, preds)) / len(ys)

    # ---- 提示：走势线可能不可靠的情况 ----
    # 走势线应穿过散点云中心，故它到各点的平均距离应接近半带宽。
    # 明显超过半带宽 -> 线偏离了云中心（通常是两组 x 不重叠所致）。
    caution = ""
    half_band = scatter.mean_d / 2.0
    if half_band > 1e-12 and mean_abs_dev > 1.5 * half_band:
        caution = (
            f"走势线与散点的平均偏差 {mean_abs_dev:.4g}，"
            f"约为半带宽 {half_band:.4g} 的 "
            f"{mean_abs_dev / half_band:.1f} 倍，说明这条线偏离了"
            f"散点云的中心。常见原因：上下两支的 x 区间不重叠"
            f"（偏差在 x 上连续成片时，两组各只覆盖一半范围，"
            f"斜率被局部趋势带偏），或所选阶数与真实规律不符。"
            f"建议减少分段数或调整阶数后重试。"
        )

    if r2 < 0:
        caution = (
            f"走势线 R² = {r2:.4f} 为负，表示它比直接取均值还差，"
            f"当前阶数或分段设置不适合这批数据。"
        )
    elif not caution and r2 < 0.5:
        caution = (
            f"走势线 R² = {r2:.4f} 偏低，说明单个多项式表达不了这批"
            f"散点的整体走势（可能趋势本身分段、或离散过强）。"
            f"走势与离散结论仍可参考，但不要外推到数据范围之外。"
        )

    return BandFitResult(
        upper=up_fit,
        lower=lo_fit,
        reconciled=tuple(reconciled),
        offset=offset,
        coefficients=tuple(final_coeffs),
        expression=expression,
        segments=tuple(seg_bands),
        scatter=scatter,
        degree_upper=deg_up,
        degree_lower=deg_lo,
        n_points=len(points),
        n_segments=len(seg_bands),
        y_min=min(ys),
        y_max=max(ys),
        r2=r2,
        rmse=rmse,
        mean_abs_dev=mean_abs_dev,
        caution=caution,
    )


def band_series(result: BandFitResult, x_from: float, x_to: float,
                step: int = 2) -> Tuple[list, list, list]:
    """返回 (xs, upper_ys, lower_ys) 用于绘制上下界包络图。"""
    xs, up, lo = [], [], []
    if x_to <= x_from:
        return xs, up, lo
    x = float(x_from)
    while x <= x_to:
        u, l = result.band_at(x)
        xs.append(x)
        up.append(u)
        lo.append(l)
        x += max(1, int(step))
    if xs and xs[-1] < x_to:
        u, l = result.band_at(float(x_to))
        xs.append(float(x_to))
        up.append(u)
        lo.append(l)
    return xs, up, lo


def dev_series(result: BandFitResult, x_from: float, x_to: float,
               step: int = 2) -> Tuple[list, list]:
    """返回 (xs, d_ys)，d(x) = |f_up - f_lo|，即离散宽度曲线。"""
    xs, up, lo = band_series(result, x_from, x_to, step)
    return xs, [abs(u - l) for u, l in zip(up, lo)]


def describe_segments(result: BandFitResult) -> str:
    """分段统计的可读报告：走势、离散宽度、分组情况。"""
    s = result.scatter
    lines = [
        "分段统计（走势中心 + 离散宽度 + 分组点数）:",
        f"  {'段':<4}{'x 区间':<22}{'均值m':>10}"
        f"{'离散宽d':>10}{'极差':>10}{'上组':>6}{'下组':>6}",
    ]
    for seg in result.segments:
        interval = f"[{seg.x_lo:.4g}, {seg.x_hi:.4g}]"
        lines.append(
            f"  {seg.index:<4}{interval:<22}{seg.mean_y:>10.4g}"
            f"{seg.d_mean_points:>10.4g}{seg.spread:>10.4g}"
            f"{seg.n_upper:>6}{seg.n_lower:>6}"
        )
    lines.append("")
    lines.append(f"离散程度: {s.description}")
    return "\n".join(lines)


__all__ = [
    "BandFitError",
    "BandFitResult",
    "SegmentBand",
    "ScatterProfile",
    "DEFAULT_SEGMENTS",
    "DEFAULT_DEGREE",
    "DEFAULT_COEF_TOL",
    "band_fit",
    "band_series",
    "dev_series",
    "describe_segments",
    "make_segments",
    "segment_of",
    "segment_means",
    "reconcile_coefficients",
    "profile_scatter",
]
