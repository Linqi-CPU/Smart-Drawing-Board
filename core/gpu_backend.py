"""可选的 GPU 加速后端（Bootstrap 专用）。

设计原则
--------
**默认完全不用 GPU。** 本项目的两个核心承诺是：
  1. 零第三方依赖（build_exe.py 里显式 exclude numpy/torch/pandas）
  2. Release 解压即用（~12 MB/个，无需装 Python）

torch 会破坏第 1 条并让 Release 体积翻数倍，
所以做成**调用方注入**：只有显式传入后端实例才会启用。

适用范围：只有 Bootstrap 值得上 GPU。
band_fit 主体是 (degree+1)×(degree+1) 的高斯消元，最大 9×9，
kernel launch 开销比计算本身大几个量级，上 GPU 只会更慢。
Bootstrap 要重复拟合几百至几千次，才是真正 compute-bound 的环节。

接口约定
--------
bootstrap_band 需要的是"能求值的拟合结果"。
GPU 侧只批量解 t 空间最小二乘（这是重复劳动的大头），
再包成一个轻量结果对象，暴露与 BandFitResult 相同的三个属性：
    .predict(x) / .upper / .lower
其中 .upper/.lower 是带 predict(x) 的小对象。
upper/lower 的 t 系数由 GPU 一并解出（上下分组在 CPU 侧做，
分组本身是 O(n) 的廉价操作，不值得搬上 GPU）。
"""

from __future__ import annotations

import math
from typing import List, Optional, Sequence, Tuple

import band_fit as bf   # 分段/分组规则以这里为唯一真源
import fitting as ft    # t→x 系数还原（高精度路径）同样以 CPU 实现为唯一真源

#: GPU 是否可用的探测结果缓存（避免每次调用都 import torch）
_GPU_STATE: Optional[bool] = None


def gpu_available() -> bool:
    """探测 torch+CUDA 是否可用。任何失败都视为不可用。"""
    global _GPU_STATE
    if _GPU_STATE is not None:
        return _GPU_STATE
    try:
        import torch                                   # noqa: F401
        _GPU_STATE = bool(torch.cuda.is_available())
    except Exception:
        _GPU_STATE = False
    return _GPU_STATE


def gpu_info() -> str:
    """返回一段人类可读的 GPU 状态说明，供 UI 显示。"""
    if not gpu_available():
        return "GPU: 不可用（未检测到可用的 CUDA 设备或未安装 torch）"
    try:
        import torch
        name = torch.cuda.get_device_name(0)
        cap = torch.cuda.get_device_capability(0)
        return f"GPU: {name} (compute capability {cap[0]}.{cap[1]})"
    except Exception:
        return "GPU: 可用（设备信息读取失败）"


class _Curve:
    """一条可求值的曲线（t 空间系数）。"""

    __slots__ = ("norm_coeffs", "center", "scale")

    def __init__(self, norm_coeffs, center, scale):
        self.norm_coeffs = tuple(norm_coeffs)
        self.center = float(center)
        self.scale = float(scale)

    def predict(self, x: float) -> float:
        t = (float(x) - self.center) / self.scale
        acc = 0.0
        for c in reversed(self.norm_coeffs):
            acc = acc * t + c
        return acc

    def __call__(self, x: float) -> float:
        return self.predict(x)


class _XCurve:
    """x 空间系数的可求值曲线。

    走势线最终落在 x 空间系数上（BandFitResult.coefficients），
    求值方式也与 BandFitResult.predict 一致：直接在 x 上 Horner。
    """

    __slots__ = ("coefficients",)

    def __init__(self, coefficients):
        self.coefficients = tuple(coefficients)

    def predict(self, x: float) -> float:
        acc = 0.0
        for c in reversed(self.coefficients):
            acc = acc * float(x) + c
        return acc

    def __call__(self, x: float) -> float:
        return self.predict(x)


class _GpuBandResult:
    """与 BandFitResult 最小兼容的结果对象。

    只需让 bootstrap_band 的取值环节工作：
        .predict(x)      -> 走势线
        .upper.predict(x)-> 上界
        .lower.predict(x)-> 下界
    """

    __slots__ = ("upper", "lower", "coefficients", "trend")

    def __init__(self, trend: _XCurve, upper: _Curve, lower: _Curve):
        self.trend = trend              # 走势线（x 空间系数）
        self.upper = upper
        self.lower = lower
        # 与 BandFitResult 同名的展示字段
        self.coefficients = trend.coefficients

    def predict(self, x: float) -> float:
        """走势线在 x 处的值。

        与 BandFitResult.predict 一致：直接在 x 空间以 Horner 求值
        （BandFitResult 用的就是 self.coefficients，不是 norm_coeffs）。
        """
        return self.trend.predict(x)


def _split_upper_lower(
    points: Sequence,
    segments: Sequence[Tuple[float, float]],
) -> Tuple[list, list]:
    """按**分段均值**把点分成上/下两组。

    .. important::
        这里**必须**复用 band_fit 的实现，不能自己写一份。
        历史上自己写的那版用了「分段中位数」切分，而 band_fit
        用的是「分段均值」切分，两者在 120 点上差 8 个点的归属，
        导致 GPU 与 CPU 的上下界系统性偏离（实测约 0.4~1.4）。
        分段/分组规则是 band_fit 的语义，不属于 GPU 后端；
        GPU 后端只负责"把相同的数学问题解得更快"。
        自己复刻一份规则，规则一改就静默不一致 —— 这是本次修复的核心教训。

    切分规则（与 band_fit.band_fit 完全一致）：
       段均值 m_i = sum(y)/count；y >= m_i 归上组，否则归下组。
    均值非有限值的点被跳过（band_fit 同样跳过）。
    """
    xs = [float(p[0]) for p in points]
    ys = [float(p[1]) for p in points]
    means, _counts = bf.segment_means(xs, ys, segments)

    upper, lower = [], []
    for x, y in zip(xs, ys):
        i = bf.segment_of(x, segments)
        m = means[i]
        if not math.isfinite(m):
            continue
        (upper if y >= m else lower).append((x, y))
    return upper, lower


def _seg_index(x: float, segments: Sequence[Tuple[float, float]]) -> int:
    """x 落在第几段（最后一段右闭）。

    与 band_fit.segment_of 语义一致，保留供需要下标而非归属的场景使用。
    注意：band_fit 的 segment_of 对落在所有段左侧的点返回 0，
    这里同样（第一段 i==0 时直接命中）。
    """
    for i, (lo, hi) in enumerate(segments):
        if x < hi or i == len(segments) - 1:
            if x >= lo or i == 0:
                return i
    return 0


def _make_segments(xs: Sequence[float], n_segments: int) -> List[Tuple[float, float]]:
    """按 x 极差等宽分段。

    同样直接复用 band_fit.make_segments，避免两套规则漂移。
    注意 band_fit 内部会把 n_segments 钳到 [2, len(points)//2]，
    且点数 <2 或 x 全同时抛错。GPU 后端此处只在不满足条件时
    返回空列表，由调用方回落到 CPU。
    """
    try:
        if len(xs) < 2:
            return []
        return list(bf.make_segments(xs, n_segments))
    except Exception:
        return []


def _batch_lstsq_t(
    pts_list: Sequence[Sequence],
    degree: int,
    device: str,
):
    """在 GPU 上批量解 t 空间最小二乘（Legendre 正交基）。

    为什么不用幂基+正态方程：A = PᵀP 的条件数是设计矩阵的**平方**，
    阶数一高就崩。本次开发中先用幂基实现，实测 8 个样本的走势线
    与 CPU 偏差 1.65、上下界偏差 4.07 —— 而 CPU 侧早就换成了
    Legendre 基（见 core/fitting.py 的 _solve_legendre）。
    两边算法不一致，结果就不可能一致。故此处同样用 Legendre。

    pts_list 里各样本的**点数可以不同** —— bootstrap 的分组
    （上组/下组）大小随样本变化，这是常态。
    因此按点数分桶：点数相同的样本一起解，桶与桶之间多次 launch。

    返回与 pts_list 等长的 list[(norm_coeffs, center, scale), ...]，
    norm_coeffs 是 t 空间的**幂基**系数（predict 直接可用），
    失败项为 None。
    """
    import torch

    n = len(pts_list)
    if n == 0:
        return []
    if degree < 1:
        raise ValueError("degree 至少为 1")

    metas = []
    for pts in pts_list:
        xs = [float(p[0]) for p in pts]
        ys = [float(p[1]) for p in pts]
        center = sum(xs) / len(xs)
        half = max(abs(x - center) for x in xs) or 1.0
        ts = [(x - center) / half for x in xs]
        metas.append((ts, ys, center, half))

    # Legendre 解出的系数 -> t 空间幂基系数，用 CPU 完全相同的函数，
    # 保证两条路径的结果逐位一致（否则"GPU 精度验证"没有意义）
    # 扁平 import —— 内核只把 core/ 放进 sys.path，不存在 core 包。
    import fitting as ft
    conv = None  # 逐样本调用 ft._legendre_to_power，避免重复实现

    # 按点数分桶
    buckets: dict = {}
    for idx, m in enumerate(metas):
        buckets.setdefault(len(m[0]), []).append(idx)

    out: List[Optional[tuple]] = [None] * n
    size = degree + 1

    for length, idxs in buckets.items():
        if length < size:
            # 该桶点数不足以定 size 个参数
            continue
        try:
            t_t = torch.tensor([metas[i][0] for i in idxs],
                               dtype=torch.float64, device=device)
            y_t = torch.tensor([metas[i][1] for i in idxs],
                               dtype=torch.float64, device=device)

            # Legendre 基设计矩阵 L[b, m, k] = P_k(t_bm)
            # 用递推批量求值（比转幂基再乘更稳）
            cols = [torch.ones_like(t_t)]
            if degree >= 1:
                cols.append(t_t)
            for k in range(1, degree):
                cols.append(((2 * k + 1) * t_t * cols[k] - k * cols[k - 1]) / (k + 1))
            L = torch.stack(cols[:size], dim=-1)             # [b, m, size]
        except Exception:
            continue

        # 正态方程（Legendre 基下接近对角，条件数可控）
        A = torch.einsum("bmi,bmj->bij", L, L)              # [b, size, size]
        rhs = torch.einsum("bmi,bm->bi", L, y_t)            # [b, size]

        try:
            leg = torch.linalg.solve(A, rhs.unsqueeze(-1)).squeeze(-1)
        except Exception:
            continue

        # Legendre 系数 -> t 空间幂基系数（复用 CPU 侧同一函数）
        leg_cpu = leg.detach().to("cpu").tolist()
        sol_cpu = []
        for row in leg_cpu:
            try:
                sol_cpu.append(tuple(ft._legendre_to_power(row)))
            except Exception:
                sol_cpu.append(None)

        for local_i, idx in enumerate(idxs):
            coeffs = sol_cpu[local_i]
            if coeffs is None or not all(math.isfinite(c) for c in coeffs):
                continue
            _, _, center, half = metas[idx]
            out[idx] = (coeffs, center, half)

    return out


class GpuFitBackend:
    """注入给 bootstrap_band 的 GPU 后端。

    约定接口：
        fit_batch(samples, n_segments, degree) -> list[_GpuBandResult|None]
    """

    def __init__(self, device: str = "cuda"):
        self.device = device
        self.calls = 0
        self.items = 0

    def fit_batch(self, samples, n_segments, degree):
        self.calls += 1
        self.items += len(samples)

        # 分组在 CPU 侧做（O(n) 廉价操作）
        groups = []
        for s in samples:
            xs = [float(p[0]) for p in s]
            segs = _make_segments(xs, n_segments)
            up, lo = _split_upper_lower(s, segs)
            groups.append((list(s), up, lo))

        if not groups:
            return []

        # 三次批量解：全量点（走势线）、上组、下组
        # 各组长度不齐没关系，_batch_lstsq_t 内部会按点数分桶。
        all_pts = [g[0] for g in groups]
        up_pts = [g[1] for g in groups]
        lo_pts = [g[2] for g in groups]
        # 但全空或长度过短的组解不出来，直接整批回退
        if (any(not p for p in all_pts) or any(not p for p in up_pts)
                or any(not p for p in lo_pts)):
            return []
        if any(len(p) <= degree for p in all_pts + up_pts + lo_pts):
            return []

        try:
            up_sol = _batch_lstsq_t(up_pts, degree, self.device)
            lo_sol = _batch_lstsq_t(lo_pts, degree, self.device)
        except Exception:
            return []

        results = []
        for (pts_all, pts_up, pts_lo), u, l in zip(groups, up_sol, lo_sol):
            if u is None or l is None:
                results.append(None)
                continue
            try:
                results.append(_build_result(pts_all, pts_up, pts_lo, u, l,
                                             self.device))
            except Exception:
                results.append(None)
        return results


def _build_result(pts_all, pts_up, pts_lo, up_sol, lo_sol, device) -> _GpuBandResult:
    """复刻 band_fit.band_fit 的走势线构造流程。

    .. important::
        band_fit 的走势线**不是**"对全量点做最小二乘"。
        它是：  reconciled = (upper.coefficients + lower.coefficients) / 2
                offset     = g_r - (up_r + lo_r) / 2      # 见下
                final[0]  += offset
        早期 GPU 后端拿"全量点拟合"冒充走势线，
        实测 predict 偏差约 1.26（而上下界偏差 0.000000）——
        上下界准、走势线不准，正是因为这两者本来就不是同一个数学对象。

        offset 的推导（照抄 band_fit 注释）：
            两支各自的均值已代表数据中心，直接用 mean(y - g(x))
            会把中心再扣一次。故以两支在全量点上的残差均值为基准，
            取平均曲线相对该基准的净偏差。
    """
    # 上下界曲线（GPU 侧解出的 t 空间系数 + 归一化参数）
    uc = _Curve(up_sol[0], up_sol[1], up_sol[2])
    lc = _Curve(lo_sol[0], lo_sol[1], lo_sol[2])

    xs = [float(p[0]) for p in pts_all]
    ys = [float(p[1]) for p in pts_all]

    # ---- 系数调和：与 band_fit.reconcile_coefficients(mode="mean") 一致 ----
    # 注意 band_fit 调和的是 **x 空间** coefficients；这里必须同样在
    # x 空间做，否则 (a_up + a_lo)/2 的结果会因为基底不同而不同。
    up_x = ft._denormalize(up_sol[0], up_sol[1], up_sol[2])
    lo_x = ft._denormalize(lo_sol[0], lo_sol[1], lo_sol[2])
    reconciled = bf.reconcile_coefficients(up_x, lo_x, mode="mean")

    # ---- 偏移常数 C ----
    # band_fit 用 x 空间系数直接求值（_poly_eval），这里同样。
    # 若改用 t 空间求值，上/下两支的残差均值会有差异，offset 就不同。
    def _poly_eval_local(coeffs, x):
        acc = 0.0
        for c in reversed(coeffs):
            acc = acc * float(x) + c
        return acc

    up_r = sum(y - _poly_eval_local(up_x, x) for x, y in zip(xs, ys)) / len(xs)
    lo_r = sum(y - _poly_eval_local(lo_x, x) for x, y in zip(xs, ys)) / len(xs)
    g_r = sum(y - _poly_eval_local(reconciled, x) for x, y in zip(xs, ys)) / len(xs)
    offset = g_r - (up_r + lo_r) / 2.0

    final_coeffs = list(reconciled)
    final_coeffs[0] += offset

    # ---- 走势线用 x 空间系数求值（BandFitResult.predict 正是这样）----
    tc = _XCurve(tuple(final_coeffs))
    return _GpuBandResult(tc, uc, lc)


def make_backend(use_gpu: bool = False) -> Optional[GpuFitBackend]:
    """按需创建 GPU 后端；不可用时返回 None。"""
    if not use_gpu:
        return None
    if not gpu_available():
        return None
    try:
        return GpuFitBackend()
    except Exception:
        return None


__all__ = [
    "gpu_available",
    "gpu_info",
    "GpuFitBackend",
    "make_backend",
]
