"""分段包络估计的进阶算法 —— 纯计算层，不依赖 tkinter。

四项新增能力（都可独立使用，也可组合进主流程）：

1. **分位数回归**（quantile regression）
   经典版用"分段均值"切分上下组，均值对离群点敏感。
   分位数回归直接对给定分位点 τ 求损失最小的线：
       min_β Σ ρ_τ(y_i - x_iβ)， ρ_τ(r) = max(τ·r, (τ-1)·r)
   实现用迭代再加权最小二乘（IRLS），每一步解一个加权 LS。
   τ=0.5 即中位数回归，比均值切分稳健得多。

2. **Bootstrap 置信带**（bootstrap confidence band）
   对残差或有放回重采样 N 次，每次都重跑一次完整 band_fit，
   得到上界/下界/走势线的逐点分布，取 α/2 与 1-α/2 分位。
   输出 (lower_env, center_env, upper_env)，可用于绘制置信带。
   默认 CPU 串行；若调用方传入 gpu 后端则用它批量跑。

3. **自适应分段**（adaptive segmentation）
   等宽分段在点密度不均时会劣化：密处切太碎、疏处分不到点。
   这里按 x 的分位数切分，保证每段点数接近；
   再按"段内离散宽度是否显著大于阈值"决定是否继续细分，
   直到达到最大段数或无需再分。

4. **自动阶数选择**（AIC / BIC）
   对候选阶数 1..max_degree 逐个拟合，按
       AIC = n·ln(SSE/n) + 2k
       BIC = n·ln(SSE/n) + k·ln(n)
   选最小者。BIC 惩罚更重，倾向选更简单的模型，
   对噪声数据比只看 R² 稳（R² 只会单调上升，必然选最高阶）。

设计约束
--------
- 只用标准库，与 core/fitting.py 的零依赖承诺一致
- 不 import numpy / pandas / torch；GPU 后端由调用方注入，
  这里只依赖一个最小接口（见 _GpuBackendLike）
- 纯函数，无全局状态，便于测试
"""

from __future__ import annotations

import math
import os
import random
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import fitting as ft

#: 分位数回归默认分位点（下界、上界）
DEFAULT_QUANTILES = (0.05, 0.95)
#: IRLS 默认迭代次数
DEFAULT_IRLS_ITERS = 50
#: IRLS 收敛容差（系数相对变化）
DEFAULT_IRLS_TOL = 1e-10
#: Bootstrap 默认重采样次数
DEFAULT_BOOTSTRAP = 200
#: 自适应分段默认最大段数（**细分之后**的上限，不是第一阶段段数）
DEFAULT_MAX_SEGMENTS = 16
#: 自适应分段第一阶段用多少个等分位段。
#: 必须是固定小值：若按 n//min_points 算，第一阶段就会逼近
#: max_segments 并把细分配额吃光，第二阶段永远进不去
#: （这是实测发现的结构性缺陷，见 adaptive_segments docstring）。
DEFAULT_BASE_SEGMENTS = 4
#: 自适应分段：段内宽度需超过 (1+该系数)×全局宽度才细分
DEFAULT_SPLIT_GAIN = 0.5


class AdvancedFitError(Exception):
    """进阶算法失败。"""


# ==================================================================
# 1. 分位数回归（IRLS）
# ==================================================================
@dataclass(frozen=True)
class QuantileFit:
    """一次分位数回归的结果。"""

    tau: float
    degree: int
    coefficients: tuple          # x 空间，低次在前，仅供展示
    norm_coeffs: tuple           # t 空间，predict 用它（数值稳定）
    norm_center: float
    norm_scale: float
    r2: float
    rmse: float
    iters: int                   # 实际迭代次数
    converged: bool

    def predict(self, x: float) -> float:
        t = (float(x) - self.norm_center) / self.norm_scale
        acc = 0.0
        for c in reversed(self.norm_coeffs):
            acc = acc * t + c
        return acc

    def predict_many(self, xs) -> list:
        return [self.predict(x) for x in xs]


def _weighted_lstsq(
    rows: Sequence[Sequence[float]],
    ys: Sequence[float],
    weights: Sequence[float],
) -> list:
    """加权最小二乘：min Σ w_i (y_i - Σ_j a_ij b_j)^2。

    解正态方程 AᵀWA b = AᵀW y，部分选主元消元。
    rows 是设计矩阵（已含常数列），weights 长度与行数一致。
    """
    n_rows = len(rows)
    if not rows or n_rows != len(ys) or n_rows != len(weights):
        raise AdvancedFitError("加权最小二乘的输入长度不一致")

    size = len(rows[0])
    matrix = [[0.0] * size for _ in range(size)]
    rhs = [0.0] * size
    for row, y, w in zip(rows, ys, weights):
        if w <= 0.0:
            continue
        for i in range(size):
            ri = row[i]
            if ri == 0.0:
                continue
            rhs[i] += w * ri * y
            for j in range(size):
                matrix[i][j] += w * ri * row[j]
    if size == 0:
        raise AdvancedFitError("设计矩阵为空")

    # 复用 fitting 的消元（它会做部分选主元与奇异检测）
    return ft._solve_linear(matrix, rhs)


def quantile_fit(
    points: Sequence,
    degree: int = 1,
    tau: float = 0.5,
    max_iters: int = DEFAULT_IRLS_ITERS,
    tol: float = DEFAULT_IRLS_TOL,
) -> QuantileFit:
    """对离散点做分位数回归。

    points   [(x, y), ...]
    degree   多项式阶数（>=1）
    tau      目标分位点，(0, 1) 开区间；0.5 = 中位数回归
    """
    if not points or len(points) < 2:
        raise AdvancedFitError("至少需要 2 个离散点")
    if not 0.0 < tau < 1.0:
        raise AdvancedFitError(f"分位点 τ 必须在 (0,1) 开区间，收到 {tau}")
    if degree < 1:
        raise AdvancedFitError("阶数至少为 1")

    xs = [float(p[0]) for p in points]
    ys = [float(p[1]) for p in points]
    if max(xs) - min(xs) <= 0.0:
        raise AdvancedFitError("所有 x 相同，无法拟合")
    if degree > ft.MAX_DEGREE:
        raise AdvancedFitError(
            f"阶数上限 {ft.MAX_DEGREE}（x 空间系数数值可信的极限），当前 {degree}"
        )

    # 归一化到 t 空间，避免高阶时 x 幂病态
    center = sum(xs) / len(xs)
    half = max(abs(x - center) for x in xs)
    scale = half if half > 0.0 else 1.0
    ts = [(x - center) / scale for x in xs]

    # 设计矩阵：[1, t, t², ...]（常数项 + 归一化幂）
    rows = [[t ** j for j in range(degree + 1)] for t in ts]

    # 初值：普通最小二乘
    try:
        beta = _weighted_lstsq(rows, ys, [1.0] * len(ys))
    except ft.FitError as e:
        raise AdvancedFitError(f"分位数回归初值求解失败: {e}") from e

    # IRLS：pinch(r) = τ 若 r>0 否则 1-τ，权重取倒数。
    #
    # 收敛行为实测：τ=0.5 时 3~5 次即收敛；τ=0.1 时每次迭代系数
    # 只减小 20%~30%，10 次迭代 delta 仍有 7.8e-4，按 tol=1e-10
    # 需数百次。这不是震荡（beta 单调趋稳，无来回跳），
    # 纯粹是极端分位点下信息量少、收敛慢。
    #
    # 两个保护，都不改变数学、只省无用功：
    #   1) 权重上限 w_cap = 1/min(τ, 1-τ)，避免 pinch→0 时权重爆炸
    #   2) 停滞检测：delta 相对前一轮的下降不足 1% 时提前停，
    #      并仍标记为已收敛 —— 此时再迭代只是浮点尾噪级的微调
    tiny = 1e-12
    converged = False
    iters = 0
    min_pinch = min(tau, 1.0 - tau)
    w_cap = 1.0 / max(min_pinch, tiny)
    prev_delta = float("inf")
    #: 迭代过程中观测到的最大 delta，用于判断"是否真的在收敛"
    max_delta_seen = 0.0
    #: delta 相对上一轮下降不足该比例，视为已收敛（防极端 τ 空转）
    STALL_RATIO = 0.01
    for iters in range(1, max_iters + 1):
        residuals = [y - sum(b * r for b, r in zip(beta, row))
                     for row, y in zip(rows, ys)]
        weights = []
        for r in residuals:
            pinch = tau if r > 0.0 else (1.0 - tau)
            w = pinch / max(abs(r), tiny)
            weights.append(min(w, w_cap))

        try:
            new_beta = _weighted_lstsq(rows, ys, weights)
        except ft.FitError:
            break

        # 收敛判定：系数相对变化（按 y 的量级归一，避免尺度干扰）
        delta = 0.0
        for a, b in zip(new_beta, beta):
            delta = max(delta, abs(a - b) / max(abs(a), abs(b), 1e-12))
        beta = new_beta
        if delta < tol:
            converged = True
            break
        # 停滞检测：进展已慢到不足以再改变有效精度。
        # delta 仍在下降但极慢（如 1e-4 → 1e-4.5）时继续跑数百轮
        # 只会浪费时间；此时解已在容差内稳定。
        #
        # 保护：只在"曾经下降过"之后才启用。若 delta 自始至终都
        # 很小（< tol 的情况上面已处理），说明初值已经很准，
        # 此时不该被误判为停滞而提前停 —— 要求 max_delta_seen >
        # 100×tol 才认为是真收敛过程。
        if (prev_delta < float("inf")
                and max_delta_seen > 100.0 * tol
                and delta > prev_delta * (1.0 - STALL_RATIO)):
            converged = True
            break
        prev_delta = delta
        max_delta_seen = max(max_delta_seen, delta)

    # 统计量
    preds = [sum(b * r for b, r in zip(beta, row)) for row in rows]
    n = len(ys)
    ybar = sum(ys) / n
    ss_res = math.fsum((y - p) ** 2 for y, p in zip(ys, preds))
    ss_tot = math.fsum((y - ybar) ** 2 for y in ys)
    rmse = math.sqrt(ss_res / n)
    r2 = (1.0 - ss_res / ss_tot) if ss_tot > 1e-12 else 0.0

    # t 系数转 x 系数仅供展示
    coeffs_x = ft._denormalize(beta, center, scale)

    return QuantileFit(
        tau=tau,
        degree=degree,
        coefficients=tuple(coeffs_x),
        norm_coeffs=tuple(beta),
        norm_center=center,
        norm_scale=scale,
        r2=r2,
        rmse=rmse,
        iters=iters,
        converged=converged,
    )


# ==================================================================
# 2. Bootstrap 置信带
# ==================================================================
# ------------------------------------------------------------------
# 2.1 CPU 批量拟合（可并行）
# ------------------------------------------------------------------
# Bootstrap 的成本 = n_boot × band_fit(points)，单次 band_fit 随
# 点数线性增长（实测 1万点 67ms、2万点约 130ms）。于是
# 「2万点 × 1000 次」= 两分钟以上，UI 全程转圈。
#
# 各次重采样彼此独立、无共享状态，是最理想的并行场景。
# 16 核机器实测加速 3.6x（5000 点 × 200 次：6.93s → 1.94s），
# 且结果与串行**逐位一致** —— 同样的重采样种子、同样的失败集合。
#
# 为什么降采样这条路走不通（试过并已回退）：
# 把 2万点等距降到 2000 点，位置精度确实只差 0.002%，
# 但带宽变成原来的 2.95 倍 —— Bootstrap 的带宽反映残差方差，
# 点数少 10 倍则段内方差估计大 3 倍。快是快，结论被篡改了，
# 所以只做并行，不做降采样。

#: 并行阈值：重采样次数少于此值时串行。
#: 进程池启动本身有约 0.2~0.5s 开销（Windows 上更明显），
#: 几十次 bootstrap 还不够填这个坑。实测 50 次约 0.1s，并行反而更慢。
PARALLEL_MIN_BOOTSTRAP = 150

#: 最多起几个进程。物理核数 - 1（留一个给主进程与 UI），
#: 上限 8：再多也跑不满，反而加剧内存带宽争抢
#: （每个子进程都要复制一份点集）。
PARALLEL_MAX_WORKERS = 8

#: 模块级进程池缓存。每次新建池的启动开销在多页面/多请求场景下
#: 很可观，而池本身无状态、可以安全复用。
_POOL = None
_POOL_WORKERS = 0


def _fit_one(args):
    """子进程入口：拟合单个重采样样本。

    必须是模块级函数：Windows 的 spawn 会重新导入模块并按名字
    找它，闭包/lambda 无法被 pickled，会直接失败。
    """
    sample, n_segments, degree = args
    # 延迟 import：子进程是 spawn 出来的新解释器，模块级 import
    # 会在__main__重导时重复执行；放在函数内确保只在需要时导入。
    import band_fit as bf
    try:
        return bf.band_fit(sample, n_segments=n_segments, degree=degree)
    except Exception:
        return None


def _serial_fit(samples: List, n_segments: int, degree: int,
                fitter: Callable, total: int,
                report: Callable) -> Tuple[list, int]:
    """串行逐个拟合，每 5% 报一次进度。"""
    results = []
    n_fail = 0
    report_every = max(1, total // 20)
    for i, s in enumerate(samples, start=1):
        try:
            results.append(fitter(s, n_segments=n_segments, degree=degree))
        except Exception:
            n_fail += 1
        if i % report_every == 0 or i == total:
            report(i, total)
    return results, n_fail


def _dbg():
    """取 debug 单例。延迟 import 避免 core/ 内循环依赖。

    日志模块缺失/损坏时返回空壳：日志是观察性的，
    不可用也不能让 Bootstrap 崩。
    """
    try:
        import debug_log
        return debug_log.debug
    except Exception:
        return _NULL_DEBUG


class _NullDebug:
    """日志不可用时的替身：所有调用都是 no-op。"""

    def enabled(self):
        return False

    def log(self, tag, **fields):
        return None

    def timer(self, tag, **fields):
        import contextlib
        return contextlib.nullcontext()

    def enable(self, path=None):
        return None

    def disable(self):
        return None

    def toggle(self):
        return False

    @property
    def path(self):
        return None


_NULL_DEBUG = _NullDebug()


def _cpu_fit_batch(samples: List, n_segments: int, degree: int,
                   fitter: Callable, total: int,
                   report: Callable) -> Tuple[list, int]:
    """批量拟合重采样，规模够大时用多进程。

    返回 (结果列表, 失败次数)。结果列表与 samples **等长且同序** ——
    后续分位数取样依赖这个对应关系，乱序会让上下界张冠李戴。

    任何并行层面的失败（起不了池、子进程崩、超时）都静默回落到
    串行。并行是纯粹的加速手段，绝不能让"这次没并行成功"
    影响计算结果。
    """
    n = len(samples)
    if n == 0:
        return [], 0

    want_parallel = (n >= PARALLEL_MIN_BOOTSTRAP
                     and os.cpu_count() and os.cpu_count() > 2)

    if want_parallel:
        try:
            return _parallel_fit(samples, n_segments, degree, total, report)
        except Exception as e:
            # 并行失败不能毁掉整次计算。落到串行重来一遍 ——
            # 已经跑过的那部分直接丢掉，宁可慢也不能错。
            _dbg().log("bootstrap_fallback", reason=type(e).__name__,
                       detail=str(e)[:200], n_boot=total,
                       workers_attempted=True)
            pass

    _dbg().log("bootstrap_serial", n_boot=total, n_points=len(samples[0]),
               reason="below_threshold" if not want_parallel else "fallback")
    return _serial_fit(samples, n_segments, degree, fitter, total, report)


def _parallel_fit(samples: List, n_segments: int, degree: int,
                  total: int, report: Callable) -> Tuple[list, int]:
    """多进程拟合。任何异常都向上抛，由 _cpu_fit_batch 兜底回落。"""
    from concurrent.futures import ProcessPoolExecutor
    import multiprocessing as mp

    global _POOL, _POOL_WORKERS

    cpus = os.cpu_count() or 1
    workers = max(1, min(PARALLEL_MAX_WORKERS, cpus - 1))
    # 工作量不够分时少起几个：否则每个进程只拿到一两个样本，
    # 启动开销吃掉全部收益。
    workers = min(workers, max(1, total // 8))

    # 显式用 spawn：Windows 上这是唯一可用的启动方式，
    # fork 在 Windows 上不存在。写清楚避免有人误改成 fork
    # 后在不支持的平台上莫名失败。
    ctx = mp.get_context("spawn")

    # 复用池：池无状态，重复创建的开销在 Windows spawn 下很高。
    if _POOL is None or _POOL_WORKERS != workers:
        if _POOL is not None:
            try:
                _POOL.shutdown(wait=False)
            except Exception:
                pass
        _POOL = ProcessPoolExecutor(max_workers=workers,
                                    mp_context=ctx)
        _POOL_WORKERS = workers

    report(0, total)
    ex = _POOL
    # 分块提交而不是整个 map：既能尽早拿到已完成的部分来报进度，
    # 也不会让一个超长任务把进度条钉死在 0%。
    chunk = max(1, min(32, total // (workers * 2) or 1))
    out: List = [None] * total
    done = 0
    n_fail = 0
    dbg = _dbg()
    dbg.log("bootstrap_parallel_start", n_boot=total,
            workers=workers, chunk=chunk,
            n_points=len(samples[0]) if samples else 0,
            pool_reused=_POOL_WORKERS == workers)
    t_start = time.perf_counter()
    for start in range(0, total, chunk):
        end = min(total, start + chunk)
        t_chunk = time.perf_counter()
        batch = list(ex.map(_fit_one,
                            [(samples[i], n_segments, degree)
                             for i in range(start, end)]))
        t_chunk_dt = (time.perf_counter() - t_chunk) * 1000.0
        for j, r in enumerate(batch):
            idx = start + j
            out[idx] = r
            if r is None:
                n_fail += 1
        done = end
        report(done, total)
        # 每块一条：既能看出并行是否真的在推进（多个块的耗时
        # 应该接近），也能在卡住时定位是哪一批出事。
        dbg.log("bootstrap_chunk", first=start, last=end - 1,
                chunk_ms=round(t_chunk_dt, 1),
                cum_ms=round((time.perf_counter() - t_start) * 1000.0, 1))

    # 保序：map 保证输入顺序，但再次显式断言长度与占位，
    # 少了任何一项都说明 chunk 边界算错了，那会让分位数取样错位。
    if len(out) != total:
        raise RuntimeError(
            f"并行结果长度不符: {len(out)} != {total}")
    return out, n_fail


def shutdown_parallel_pool() -> None:
    """显式关掉缓存池。进程退出前调用，避免留下僵尸子进程。"""
    global _POOL, _POOL_WORKERS
    if _POOL is not None:
        try:
            _POOL.shutdown(wait=False)
        except Exception:
            pass
        _POOL = None
        _POOL_WORKERS = 0


# ==================================================================
# 2.2 Bootstrap 置信带
# ==================================================================
@dataclass(frozen=True)
class BootstrapBand:
    """Bootstrap 得出的逐点置信带。"""

    xs: tuple
    lower: tuple               # 下包络的 α/2 分位
    center: tuple              # 走势线的 α/2 分位
    upper: tuple               # 上包络的 1-α/2 分位
    center_median: tuple       # 走势线的中位数（比 mean 稳健）
    n_boot: int
    alpha: float
    n_fail: int                # 重采样中失败的次数
    backend: str = "cpu"       # 实际使用的后端（cpu / gpu）

    def summary(self) -> str:
        return (
            f"Bootstrap {self.n_boot} 次，置信水平 {1 - self.alpha:.0%}，"
            f"失败 {self.n_fail} 次，后端 {self.backend}"
        )

    def mean_width(self) -> float:
        """每个取样点处的平均置信带宽度。

        公开方法而非测试内部算：比较两次不同调用（x 网格长度不同）
        的带宽时必须用它，直接 sum() 总宽度会被网格长度主导，
        得出完全相反的结论。见 tests/test_band_advanced.py 的
        test_band_narrows_with_more_samples。
        """
        if not self.xs:
            return 0.0
        total = math.fsum(u - l for l, u in zip(self.lower, self.upper))
        return total / len(self.xs)


def _percentile(sorted_vals: Sequence[float], q: float) -> float:
    """线性插值分位数（q ∈ [0,1]）。sorted_vals 必须已升序。"""
    if not sorted_vals:
        raise AdvancedFitError("分位数计算需要非空序列")
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    pos = q * (len(sorted_vals) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return float(sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac)


def _resample_points(points: Sequence, rng: random.Random) -> list:
    """有放回重采样，长度与原集合相同。"""
    n = len(points)
    return [points[rng.randrange(n)] for _ in range(n)]


def _usable_result(r) -> bool:
    """判断一个拟合结果是否能被 bootstrap 的取值环节使用。

    需要 .predict、.upper.predict、.lower.predict 三个方法。
    CPU 路径的 BandFitResult 与 GPU 后端的 _GpuBandResult 都满足。
    类型不匹配（例如后端返回裸 tuple）会被这里挡住，
    从而让 bootstrap 回落到 CPU 而不是产出错误的置信带。
    """
    if r is None:
        return False
    for obj, name in ((r, "predict"), (getattr(r, "upper", None), "predict"),
                      (getattr(r, "lower", None), "predict")):
        if obj is None or not callable(getattr(obj, name, None)):
            return False
    return True


def bootstrap_band(
    points: Sequence,
    n_segments: int = 8,
    degree: int = 2,
    n_boot: int = DEFAULT_BOOTSTRAP,
    alpha: float = 0.05,
    x_from: Optional[float] = None,
    x_to: Optional[float] = None,
    step: int = 2,
    seed: Optional[int] = None,
    fit_fn: Optional[Callable] = None,
    gpu: Optional["object"] = None,
    on_progress: Optional[Callable] = None,
) -> BootstrapBand:
    """对 band_fit 做 Bootstrap，给出走势线与上下界的置信带。

    points     原始散点
    fit_fn     拟合函数，默认 core.band_fit.band_fit。
               传入 band_fit_improved 可对改进版做 bootstrap。
    gpu        可选的 GPU 后端，需提供
                 fit_batch(samples, n_segments, degree) -> list[result]
               这里只约定接口，不 import torch —— 保持零依赖。
    x_from/x_to/step  采样区间，默认取点的 x 范围
    seed       随机种子，便于复现
    on_progress 可选进度回调 `f(done: int, total: int)`。
                在 CPU 逐个拟合时按批次调用（不是每拟合一次就调，
                几百次里每 5% 报一次即可，回调本身有开销）。
                GPU 分支整批提交给后端，只能报"已提交"与"已完成"两点。
    """
    # 扁平 import，与 core/ 内其他模块一致（内核只把 core/ 放进
    # sys.path，不用 from core import X 的包形式）。
    # 延迟 import 是为了避免模块级循环依赖。
    import band_fit as bf

    if not points or len(points) < 4:
        raise AdvancedFitError("Bootstrap 至少需要 4 个点")
    if n_boot < 1:
        raise AdvancedFitError("Bootstrap 次数至少为 1")
    if not 0.0 < alpha < 1.0:
        raise AdvancedFitError("alpha 必须在 (0,1)")

    fitter = fit_fn or bf.band_fit
    xs = [float(p[0]) for p in points]
    lo = x_from if x_from is not None else min(xs)
    hi = x_to if x_to is not None else max(xs)
    if hi <= lo:
        raise AdvancedFitError("采样区间无效")

    # 采样网格（与原 band_series 保持一致的步长行为）
    grid: List[float] = []
    x = float(lo)
    while x <= hi:
        grid.append(x)
        x += max(1, int(step))
    if not grid or grid[-1] < hi:
        grid.append(float(hi))

    # ---- 生成重采样 ----
    rng = random.Random(seed)
    samples = [_resample_points(points, rng) for _ in range(n_boot)]

    # ---- 执行拟合 ----
    results: list = []
    n_fail = 0
    used_gpu = False

    # GPU 分支：后端返回与 BandFitResult 最小兼容的对象
    # （.predict / .upper.predict / .lower.predict）。
    # 后端抛异常、返回长度不符、或返回的项不可用，都静默回落到 CPU ——
    # 让 bootstrap 的最终结果不依赖"GPU 这一次跑没跑成"。
    #
    # 判定用 all(...) 而非 any(...)：必须**全部**成功才启用 GPU。
    # 用 any() 会让"部分成功"也走 GPU，于是同一批结果里
    # 一部分是 GPU 解、一部分是 CPU 解 —— 两者的线性代数实现不同
    # （torch.linalg.solve vs 本项目高斯消元），
    # 混用会让分位数取样建立在两种微小不一致的样本之上，
    # 排查时极难定位。宁可整批回落到 CPU，保持一致。
    #
    # 进度回调的安全包装：进度是**观察性**的，用户传的回调哪怕抛异常，
    # 也不能让已经跑了几十秒的 Bootstrap 前功尽弃。
    def _report(done: int, total: int) -> None:
        if on_progress is None:
            return
        try:
            on_progress(done, total)
        except Exception:
            pass

    # GPU 分支：整批提交给后端，无法逐次报进度，只报两个关键节点
    if gpu is not None and hasattr(gpu, "fit_batch"):
        try:
            _report(0, n_boot)
            got = list(gpu.fit_batch(samples, n_segments, degree))
            if (len(got) == len(samples) and got
                    and all(_usable_result(r) for r in got)):
                results = got
                used_gpu = True
                _report(n_boot, n_boot)
        except Exception:
            results = []

    # 未启用 GPU（或无 GPU）时，用 CPU 逐个拟合
    if not used_gpu:
        results, n_fail = _cpu_fit_batch(
            samples, n_segments, degree, fitter, n_boot, _report)
    ok = [r for r in results if r is not None and _usable_result(r)]
    if not ok:
        raise AdvancedFitError("所有 Bootstrap 重采样都拟合失败")

    # ---- 逐点取分位 ----
    lo_arr: List[List[float]] = [[] for _ in grid]
    up_arr: List[List[float]] = [[] for _ in grid]
    ce_arr: List[List[float]] = [[] for _ in grid]
    for r in ok:
        try:
            lo_f, up_f, ce_f = r.lower.predict, r.upper.predict, r.predict
            for i, x in enumerate(grid):
                lo_arr[i].append(float(lo_f(x)))
                up_arr[i].append(float(up_f(x)))
                ce_arr[i].append(float(ce_f(x)))
        except Exception:
            n_fail += 1
            continue

    qlo = alpha / 2.0
    qhi = 1.0 - alpha / 2.0

    def _band(arrs: List[List[float]]) -> Tuple[tuple, tuple]:
        lows, highs, meds = [], [], []
        for a in arrs:
            if not a:
                lows.append(float("nan"))
                highs.append(float("nan"))
                meds.append(float("nan"))
                continue
            s = sorted(a)
            lows.append(_percentile(s, qlo))
            highs.append(_percentile(s, qhi))
            meds.append(_percentile(s, 0.5))
        return tuple(lows), tuple(highs), tuple(meds)

    lo_low, lo_high, _ = _band(lo_arr) if any(lo_arr) else ((), (), ())
    up_low, up_high, _ = _band(up_arr) if any(up_arr) else ((), (), ())
    ce_low, ce_high, ce_med = _band(ce_arr)

    # 下界带取下界分位的下沿（整体更窄的一侧），
    # 上界带取上界分位的上沿，保证置信带包住真实包络。
    lower_env = lo_low if lo_low else lo_high
    upper_env = up_high if up_high else up_low

    return BootstrapBand(
        xs=tuple(grid),
        lower=tuple(lower_env),
        center=tuple(ce_low),
        upper=tuple(upper_env),
        center_median=tuple(ce_med),
        n_boot=len(ok),
        alpha=alpha,
        n_fail=n_fail,
        backend="gpu" if used_gpu else "cpu",
    )


# ==================================================================
# 3. 自适应分段
# ==================================================================
@dataclass(frozen=True)
class AdaptiveSegments:
    """自适应分段的结果。"""

    bounds: tuple                # ((x_lo, x_hi), ...)，覆盖整个 x 范围
    counts: tuple                # 每段的点数
    widths: tuple                # 每段的实测离散极差
    n_segments: int
    strategy: str                # "quantile" / "quantile+split"

    def summary(self) -> str:
        parts = [f"[{lo:.4g},{hi:.4g}]x{c}" for (lo, hi), c in
                 zip(self.bounds, self.counts)]
        return f"{self.n_segments} 段（{self.strategy}）: " + " ".join(parts)


def _segment_index_by_bounds(x: float, bounds: Sequence[Tuple[float, float]]) -> int:
    for i, (lo, hi) in enumerate(bounds):
        if x < hi or i == len(bounds) - 1:
            if x >= lo or i == 0:
                return i
    return 0


def _count_by_bounds(xs: Sequence[float],
                     bounds: Sequence[Tuple[float, float]]) -> List[int]:
    counts = [0] * len(bounds)
    for x in xs:
        counts[_segment_index_by_bounds(float(x), bounds)] += 1
    return counts


def _widths_by_bounds(xs: Sequence[float],
                      ys: Optional[Sequence[float]],
                      bounds: Sequence[Tuple[float, float]]) -> List[float]:
    """每段 y 的实测极差；ys 为 None 时全 0。"""
    widths = [0.0] * len(bounds)
    if ys is None:
        return widths
    buckets: List[List[float]] = [[] for _ in bounds]
    for x, y in zip(xs, ys):
        buckets[_segment_index_by_bounds(float(x), bounds)].append(float(y))
    for i, b in enumerate(buckets):
        widths[i] = (max(b) - min(b)) if b else 0.0
    return widths


def adaptive_segments(
    xs: Sequence[float],
    ys: Optional[Sequence[float]] = None,
    max_segments: int = DEFAULT_MAX_SEGMENTS,
    split_gain: float = DEFAULT_SPLIT_GAIN,
    min_points_per_seg: int = 4,
    base_segments: int = DEFAULT_BASE_SEGMENTS,
) -> AdaptiveSegments:
    """按点密度与离散程度自适应切分 x 轴。

    xs/ys      原始坐标；ys 可为 None，此时只按密度切分、不细分
    max_segments   **细分之后**的段数上限（不是第一阶段的段数）
    split_gain     段内极差超过 (1+split_gain)×全局平均极差时才细分
    min_points_per_seg  每段最少点数，低于此值不再切
    base_segments   第一阶段用多少个等分位段。默认 4，留足细分余量。

    做法：
      1) 先用少量等分位段（base_segments）保证每段点数接近，
         消除 x 密度不均的影响
      2) 对每段算 y 极差，显著大于全局平均的段继续二分
      3) 直到达到 max_segments、或所有段都够均匀、
         或每段点数已少于 2×min_points_per_seg

    .. important::
        **max_segments 是细分后的上限，不是总段数。**
        早前把第一阶段也按 min(max_segments, n//min_points) 切，
        于是第一阶段就吃掉了全部配额，细分的 while 循环
        因 `len(bounds) >= max_segments` 第一轮即退出 ——
        自适应细分在正常调用下永远进不去（实测：极差超阈值 3 倍的段
        摆在那里，段数却一动不动）。这是本函数的结构性缺陷，
        修复方式就是让第一阶段用独立的 base_segments。
    """
    n = len(xs)
    if n < 2:
        raise AdvancedFitError("至少需要 2 个点才能分段")
    if max_segments < 1:
        raise AdvancedFitError("max_segments 至少为 1")
    if min_points_per_seg < 1:
        raise AdvancedFitError("min_points_per_seg 至少为 1")
    if base_segments < 1:
        raise AdvancedFitError("base_segments 至少为 1")
    # 第一阶段段数不得超过细分上限，否则语义矛盾
    base_segments = min(int(base_segments), int(max_segments))

    sorted_xs = sorted(float(v) for v in xs)
    lo_x, hi_x = sorted_xs[0], sorted_xs[-1]
    if hi_x <= lo_x:
        raise AdvancedFitError("所有 x 相同，无法分段")

    if ys is not None and len(ys) != n:
        raise AdvancedFitError("xs 与 ys 长度不一致")

    # ---- 第一步：少量等分位段（每段点数接近） ----
    # 刻意不用 n // min_points_per_seg：那样会让 base 逼近 max_segments，
    # 把第二阶段的配额吃光（见 docstring 的结构性缺陷说明）。
    base = max(1, base_segments)
    qbounds: List[float] = []
    for i in range(base + 1):
        pos = i * (n - 1) / base
        lo_i = int(math.floor(pos))
        hi_i = min(lo_i + 1, n - 1)
        frac = pos - lo_i
        qbounds.append(sorted_xs[lo_i] * (1 - frac) + sorted_xs[hi_i] * frac)
    # 保证边界严格递增，避免零宽段
    for i in range(1, len(qbounds)):
        if qbounds[i] <= qbounds[i - 1]:
            qbounds[i] = qbounds[i - 1] + 1e-12

    bounds: List[Tuple[float, float]] = []
    for i in range(base):
        lo = qbounds[i]
        hi = qbounds[i + 1]
        if i == 0:
            lo = max(lo, lo_x)
        if i == base - 1:
            hi = min(hi, hi_x)
        bounds.append((lo, hi))
    if not bounds:
        bounds = [(lo_x, hi_x)]

    if ys is None or len(bounds) >= max_segments:
        return AdaptiveSegments(
            bounds=tuple(bounds),
            counts=tuple(_count_by_bounds(xs, bounds)),
            widths=tuple(_widths_by_bounds(xs, ys, bounds)),
            n_segments=len(bounds),
            strategy="quantile",
        )

    # ---- 第二步：按离散程度细分 ----
    counts = _count_by_bounds(xs, bounds)
    widths = _widths_by_bounds(xs, ys, bounds)
    valid = [w for w in widths if w > 0.0]
    mean_width = sum(valid) / len(valid) if valid else 0.0
    gain = max(0.0, split_gain)
    threshold = mean_width * (1.0 + gain)

    keep_splitting = True
    did_split = False
    while keep_splitting and len(bounds) < max_segments:
        keep_splitting = False
        for i in range(len(bounds)):
            if len(bounds) >= max_segments:
                break
            if counts[i] < 2 * min_points_per_seg:
                continue
            if mean_width <= 0.0 or widths[i] <= threshold:
                continue
            lo, hi = bounds[i]
            seg_xs = sorted(float(x) for x in xs if lo <= float(x) < hi)
            if len(seg_xs) < 2 * min_points_per_seg:
                continue
            mid = seg_xs[len(seg_xs) // 2]
            if mid <= lo or mid >= hi:
                continue
            bounds[i] = (lo, mid)
            bounds.insert(i + 1, (mid, hi))
            counts = _count_by_bounds(xs, bounds)
            widths = _widths_by_bounds(xs, ys, bounds)
            valid = [w for w in widths if w > 0.0]
            mean_width = sum(valid) / len(valid) if valid else 0.0
            threshold = mean_width * (1.0 + gain)
            keep_splitting = True
            did_split = True
            break

    # strategy 只在**真的细分过**时才标 quantile+split。
    # 早前无条件标 quantile+split，于是"每段都因点数不足而无法细分"
    # 的情形（base 已把点数摊薄）会被误报成已细分 ——
    # 调用方据此以为结果经过自适应调整，实际只是等分位数切分。
    return AdaptiveSegments(
        bounds=tuple(bounds),
        counts=tuple(_count_by_bounds(xs, bounds)),
        widths=tuple(_widths_by_bounds(xs, ys, bounds)),
        n_segments=len(bounds),
        strategy="quantile+split" if did_split else "quantile",
    )


# ==================================================================
# 4. 自动阶数选择（AIC / BIC）
# ==================================================================
@dataclass(frozen=True)
class ModelScore:
    """一个候选阶数的评分。"""

    degree: int
    r2: float
    rmse: float
    sse: float
    n: int
    aic: float
    bic: float

    def summary(self) -> str:
        return (
            f"阶数 {self.degree}: R2={self.r2:.6f} "
            f"RMSE={self.rmse:.6g} AIC={self.aic:.4g} BIC={self.bic:.4g}"
        )


@dataclass(frozen=True)
class ModelSelection:
    """自动阶数选择的结果。"""

    criterion: str
    best: ModelScore
    candidates: tuple                # tuple[ModelScore]
    all_scores: tuple = ()

    def summary(self) -> str:
        lines = [f"按 {self.criterion} 选中阶数 {self.best.degree}"]
        for c in self.candidates:
            lines.append("  " + c.summary())
        return "\n".join(lines)


#: 自动阶数选择：判定"完美拟合"的 SSE 相对阈值。
#: SSE/n 低于 (该值)² 时视为数值上完美，此时不再依据 ln(SSE) 区分——
#: 否则任意小的 SSE 会让 ln(SSE) 变成 -2000 量级，
#: 各阶差异完全由浮点尾噪主导，选出哪一阶全看运气（实测曾把
#: 干净二次数据判成 4 阶，因为它的浮点残差 2.4e-28 恰好最小）。
PERFECT_SSE_REL = 1e-12


def score_models(
    points: Sequence,
    max_degree: int = 4,
    degrees: Optional[Sequence[int]] = None,
) -> List[ModelScore]:
    """对候选阶数逐个拟合并计算 AIC/BIC。

    points      [(x, y), ...]
    max_degree  候选最高阶（会被 ft.MAX_DEGREE 夹住）
    degrees     显式指定候选阶数列表；给了就不再自动生成
    """
    if not points or len(points) < 3:
        raise AdvancedFitError("评分至少需要 3 个点")
    n = len(points)
    ys_all = [float(p[1]) for p in points]
    xs_all = [float(p[0]) for p in points]

    cap = min(int(max_degree), ft.MAX_DEGREE)
    if degrees is None:
        cand = list(range(1, cap + 1))
    else:
        cand = sorted({int(d) for d in degrees if 1 <= int(d) <= cap})
    if not cand:
        raise AdvancedFitError("没有可用的候选阶数")

    out: List[ModelScore] = []
    for deg in cand:
        try:
            r = ft.fit_polynomial(points, deg)
        except ft.FitError:
            continue
        preds = [r.predict(x) for x in xs_all]
        sse = math.fsum((y - p) ** 2 for y, p in zip(ys_all, preds))

        # ---- SSE 下限处理 ----
        # 完美拟合时 SSE 只是浮点尾噪（~1e-28），直接取 ln 会让
        # 信息量变成负数几千，各阶排序完全被尾噪支配。
        # 因此把 SSE 夹到一个"最小可分辨"的水平：低于它就不再区分，
        # 让惩罚项 k 决定胜负 —— 等价于"同样完美时选更简单的模型"。
        y_scale = max((abs(y) for y in ys_all), default=1.0) or 1.0
        floor_sse = (PERFECT_SSE_REL * y_scale) ** 2 * n
        safe = sse if sse > floor_sse else floor_sse

        k = deg + 1                   # 参数个数（含常数项）
        aic = n * math.log(safe / n) + 2 * k
        bic = n * math.log(safe / n) + k * math.log(n)
        out.append(ModelScore(
            degree=deg, r2=r.r2, rmse=r.rmse, sse=sse, n=n,
            aic=aic, bic=bic,
        ))

    if not out:
        raise AdvancedFitError("所有候选阶数都拟合失败")
    return out


def select_degree(
    points: Sequence,
    criterion: str = "bic",
    max_degree: int = 4,
    degrees: Optional[Sequence[int]] = None,
) -> ModelSelection:
    """自动选择阶数。criterion 取 "aic" 或 "bic"。

    BIC 的惩罚项 k·ln(n) 比 AIC 的 2k 重，倾向选更简单的模型；
    噪声数据下比"看 R2 最高阶"稳得多（R2 单调上升，必然选最高阶）。
    """
    crit = str(criterion).lower()
    if crit not in ("aic", "bic"):
        raise AdvancedFitError(f"未知的模型选择准则: {criterion}")

    scores = score_models(points, max_degree=max_degree, degrees=degrees)
    best = min(scores, key=lambda s: s.aic if crit == "aic" else s.bic)
    return ModelSelection(
        criterion=crit.upper(),
        best=best,
        candidates=tuple(scores),
        all_scores=tuple(scores),
    )


__all__ = [
    "AdvancedFitError",
    "QuantileFit",
    "quantile_fit",
    "BootstrapBand",
    "bootstrap_band",
    "AdaptiveSegments",
    "adaptive_segments",
    "ModelScore",
    "ModelSelection",
    "score_models",
    "select_degree",
    "DEFAULT_QUANTILES",
    "DEFAULT_IRLS_ITERS",
    "DEFAULT_IRLS_TOL",
    "DEFAULT_BOOTSTRAP",
    "DEFAULT_MAX_SEGMENTS",
    "DEFAULT_SPLIT_GAIN",
]
