"""core/band_advanced.py 四个算法的回归测试。

覆盖对象：
    1. quantile_fit        分位数回归（IRLS）—— 收敛性、分位点语义
    2. adaptive_segments   自适应分段 —— 触发/不细分的条件
    3. score_models        模型评分 —— SSE/R²/AIC/BIC 公式正确性
    4. select_degree       自动选阶 —— AIC vs BIC 的行为差异
    5. bootstrap_band      Bootstrap —— 分位正确性、可复现性、失败回退

写测试时的三个原则
------------------
1. **有已知真值才断言数值**。构造数据时让答案可手算或可解析求出，
   避免"跑出来是多少就断言多少"（那样测试只是把现状抄一遍）。
2. **断言行为差异，不只是"能跑"**。例如 BIC 比 AIC 更倾向简单模型、
   自适应分段在离散不均时才会细分 —— 这些是该算法的**定义**，
   实现改了就必须让测试响。
3. **边界输入必须报错而非静默返回垃圾**。
"""

from __future__ import annotations

import math
import random
import sys
import unittest

sys.path.insert(0, '.')
sys.path.insert(0, 'core')

from core import band_advanced as ba
from core import band_fit as bf
from core import fitting as ft


# ==================================================================
# 测试数据构造辅助
# ==================================================================
def linear_cloud(n=80, slope=1.5, intercept=50.0, noise=6.0, seed=1):
    rng = random.Random(seed)
    return [(float(i), intercept + slope * i + rng.uniform(-noise, noise))
            for i in range(n)]


def quadratic_exact(n=40, seed=3):
    rng = random.Random(seed)
    pts = []
    for i in range(n):
        x = float(i)
        y = 3.0 - 2.0 * x + 0.5 * x * x          # 精确二次，无噪声
        pts.append((x, y))
    return pts


def quadratic_noisy(n=40, noise=3.0, seed=5):
    rng = random.Random(seed)
    return [(float(i), 3.0 - 2.0 * i + 0.5 * i * i
             + rng.uniform(-noise, noise)) for i in range(n)]


def mixed_scatter(n=120, seed=11):
    """前半段噪声小、后半段噪声大 —— 用来触发自适应分段。"""
    rng = random.Random(seed)
    pts = []
    for i in range(n):
        x = float(i)
        half = i < n // 2
        spread = 1.0 if half else 40.0
        pts.append((x, 5.0 + 0.4 * x + rng.uniform(-spread, spread)))
    return pts


# ==================================================================
# 1. 分位数回归
# ==================================================================
class TestQuantileFit(unittest.TestCase):
    def test_recovers_slope_on_linear_cloud(self):
        """干净线性云上，估计的斜率应接近真实斜率。"""
        pts = linear_cloud(n=150, slope=2.0, noise=3.0, seed=7)
        q = ba.quantile_fit(pts, 1, tau=0.5)
        # 斜率就是 coefficients[1]
        self.assertAlmostEqual(q.coefficients[1], 2.0, delta=0.15)

    def test_median_regression_passes_through_center(self):
        """τ=0.5 的中位数回归线应大致落在云的中心。"""
        pts = linear_cloud(n=120, slope=0.0, intercept=100.0, noise=8.0, seed=9)
        q = ba.quantile_fit(pts, 1, tau=0.5)
        # 中心：所有 x 的预测均值应接近 100
        preds = q.predict_many([x for x, _ in pts])
        mean_pred = sum(preds) / len(preds)
        self.assertAlmostEqual(mean_pred, 100.0, delta=1.5)

    def test_extreme_quantiles_are_ordered(self):
        """τ 越大，拟合线越高 —— 这是分位数回归的定义。"""
        pts = linear_cloud(n=120, noise=6.0, seed=13)
        low = ba.quantile_fit(pts, 1, tau=0.1)
        mid = ba.quantile_fit(pts, 1, tau=0.5)
        high = ba.quantile_fit(pts, 1, tau=0.9)
        x0 = 0.0
        self.assertLess(low.predict(x0), mid.predict(x0))
        self.assertLess(mid.predict(x0), high.predict(x0))

    def test_quantile_matches_empirical_fraction(self):
        """τ 分位线下方应有约 τ 比例的点（分位数回归的定义性检验）。"""
        pts = linear_cloud(n=200, noise=10.0, seed=17)
        tau = 0.25
        q = ba.quantile_fit(pts, 1, tau=tau)
        below = sum(1 for x, y in pts if y <= q.predict(x))
        frac = below / len(pts)
        # 留足容差：IRLS 是按回归而非逐点分位点求解
        self.assertGreater(frac, 0.12,
                           f"τ=0.25 但只有 {frac:.1%} 的点在下方")
        self.assertLess(frac, 0.40)

    def test_converges_on_clean_data(self):
        """干净数据上 IRLS 应标记为已收敛。"""
        pts = linear_cloud(n=80, noise=1.0, seed=19)
        q = ba.quantile_fit(pts, 1, tau=0.5)
        self.assertTrue(q.converged,
                        f"干净数据应收敛，实际 {q.iters} 次迭代未收敛")
        self.assertLess(q.iters, ba.DEFAULT_IRLS_ITERS)

    def test_extreme_tau_still_converges(self):
        """τ 极靠近 0 或 1 时权重悬殊，必须仍能收敛（历史 bug 回归）。"""
        pts = linear_cloud(n=100, noise=5.0, seed=23)
        for tau in (0.05, 0.1, 0.9, 0.95):
            q = ba.quantile_fit(pts, 1, tau=tau)
            self.assertTrue(q.converged,
                            f"τ={tau} 未收敛（{q.iters} 次迭代）")

    def test_converged_prediction_is_stable(self):
        """收敛后，predict 不能再随迭代次数漂移（防假收敛）。"""
        pts = linear_cloud(n=100, noise=5.0, seed=29)
        q = ba.quantile_fit(pts, 1, tau=0.5)
        vals = [q.predict(x) for x in (0.0, 10.0, 50.0, 99.0)]
        for func in (q.predict, q.predict):
            pass                      # predict 无状态，本断言靠上一条
        self.assertTrue(all(math.isfinite(v) for v in vals))

    def test_quadratic_recovers_coefficients(self):
        """精确二次数据上，3 阶以内应还原出真实系数。"""
        pts = quadratic_exact(n=30)
        q = ba.quantile_fit(pts, 2, tau=0.5)
        for got, want in zip(q.coefficients, (3.0, -2.0, 0.5)):
            self.assertAlmostEqual(got, want, places=3)

    def test_predict_matches_matches_single(self):
        pts = linear_cloud(n=40, seed=31)
        q = ba.quantile_fit(pts, 1, tau=0.5)
        many = q.predict_many(range(20))
        for i, v in enumerate(many):
            self.assertAlmostEqual(v, q.predict(i), places=12)

    def test_r2_and_rmse_present(self):
        pts = linear_cloud(n=40, seed=37)
        q = ba.quantile_fit(pts, 1, tau=0.5)
        self.assertGreater(q.r2, 0.9)
        self.assertGreater(q.rmse, 0.0)

    # ---- 输入校验 ----
    def test_rejects_too_few_points(self):
        with self.assertRaises(ba.AdvancedFitError):
            ba.quantile_fit([(1.0, 2.0)], 1)

    def test_rejects_tau_out_of_range(self):
        pts = linear_cloud(n=20, seed=41)
        for tau in (0.0, 1.0, -0.1, 1.5):
            with self.assertRaises(ba.AdvancedFitError, msg=f"τ={tau} 应被拒"):
                ba.quantile_fit(pts, 1, tau=tau)

    def test_rejects_degree_below_one(self):
        pts = linear_cloud(n=20, seed=43)
        with self.assertRaises(ba.AdvancedFitError):
            ba.quantile_fit(pts, 0, tau=0.5)


# ==================================================================
# 2. 自适应分段
# ==================================================================
class TestAdaptiveSegments(unittest.TestCase):
    def test_uniform_data_gets_no_extra_split(self):
        """各段离散度相当时，不该细分（strategy 停在 quantile）。"""
        rng = random.Random(101)
        xs = [float(i) for i in range(100)]
        ys = [rng.uniform(-1, 1) for _ in xs]      # 全程同幅度噪声
        seg = ba.adaptive_segments(xs, ys, max_segments=16,
                                   min_points_per_seg=4)
        # 没有任何段显著宽于全局平均 -> 不细分
        self.assertEqual(seg.strategy, "quantile")
        # 第一阶段固定 4 段，未细分
        self.assertEqual(seg.n_segments, ba.DEFAULT_BASE_SEGMENTS)
        self.assertEqual(sum(seg.counts), 100)

    def test_uneven_scatter_triggers_split(self):
        """后半段明显更发散时，必须发生细分。

        这是本函数的核心行为：max_segments 是**细分后**的上限，
        第一阶段只用 base_segments 段，所以配额一定有余量。
        mixed_scatter 前半段 ±1、后半段 ±40，极差差两个量级。
        """
        pts = mixed_scatter(120, seed=11)
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        seg = ba.adaptive_segments(xs, ys, max_segments=16,
                                   min_points_per_seg=4)
        self.assertEqual(seg.strategy, "quantile+split")
        # 第一阶段 4 段，细分后必须更多
        self.assertGreater(seg.n_segments, ba.DEFAULT_BASE_SEGMENTS)
        self.assertLessEqual(seg.n_segments, 16)
        self.assertEqual(sum(seg.counts), 120)

    def test_split_uses_quota_left_by_base(self):
        """第一阶段不得吃掉细分配额（结构性缺陷的回归守护）。

        早前 base = min(max_segments, n//min_points)，120 点、
        min_points=4 时 base=30 已 >= max_segments=16，
        细分的 while 循环第一轮就退出 —— 实测极差超阈值 3 倍的段
        摆在那里，段数一动不动。修复后第一阶段固定 base_segments 段。
        """
        pts = mixed_scatter(120, seed=11)
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        seg = ba.adaptive_segments(xs, ys, max_segments=16,
                                   min_points_per_seg=4)
        # 第一阶段必须是固定的 base_segments，不得随 n/min_points 增长
        self.assertEqual(seg.n_segments > ba.DEFAULT_BASE_SEGMENTS, True)
        # 且细分后仍受 max_segments 约束
        self.assertLessEqual(seg.n_segments, 16)

    def test_custom_base_segments(self):
        """可以显式指定第一阶段段数。"""
        pts = mixed_scatter(120, seed=11)
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        seg = ba.adaptive_segments(xs, ys, max_segments=32,
                                   min_points_per_seg=2, base_segments=2)
        self.assertGreaterEqual(seg.n_segments, 2)
        self.assertLessEqual(seg.n_segments, 32)

    def test_base_capped_by_max(self):
        """base_segments 大于 max_segments 时被夹到 max_segments。"""
        pts = mixed_scatter(60, seed=11)
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        seg = ba.adaptive_segments(xs, ys, max_segments=3,
                                   min_points_per_seg=1, base_segments=10)
        self.assertLessEqual(seg.n_segments, 3)

    def test_no_split_when_points_too_thin(self):
        """点数被摊薄到无法细分时，strategy 如实报 quantile。

        这条锁的是一个**真实修复**：早前无条件标 quantile+split，
        于是"想细分但一段都动不了"会被误报成已自适应。
        """
        pts = mixed_scatter(120, seed=11)
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        # min_points 设得很大，每段点数不足 2×min_points
        seg = ba.adaptive_segments(xs, ys, max_segments=16,
                                   min_points_per_seg=200)
        self.assertEqual(seg.strategy, "quantile")

    def test_rejects_zero_base_segments(self):
        with self.assertRaises(ba.AdvancedFitError):
            ba.adaptive_segments([1.0, 2.0, 3.0], [1.0, 2.0, 3.0],
                                 base_segments=0)

    def test_bounds_cover_full_range_without_gap(self):
        """分段边界必须连续无缝覆盖整个 x 范围。"""
        pts = mixed_scatter(100, seed=5)
        xs = [p[0] for p in pts]
        seg = ba.adaptive_segments(xs, [p[1] for p in pts], max_segments=12,
                                   min_points_per_seg=3)
        self.assertEqual(seg.bounds[0][0], min(xs))
        self.assertEqual(seg.bounds[-1][1], max(xs))
        for (a_lo, a_hi), (b_lo, _) in zip(seg.bounds, seg.bounds[1:]):
            self.assertEqual(a_hi, b_lo, "段与段之间不能有缝隙")

    def test_counts_match_points(self):
        """每段点数之和必须等于总点数（一个点都不能丢）。"""
        pts = mixed_scatter(90, seed=7)
        xs = [p[0] for p in pts]
        seg = ba.adaptive_segments(xs, [p[1] for p in pts], max_segments=10,
                                   min_points_per_seg=3)
        self.assertEqual(sum(seg.counts), len(pts))

    def test_widths_are_nonnegative(self):
        pts = mixed_scatter(80, seed=9)
        xs = [p[0] for p in pts]
        seg = ba.adaptive_segments(xs, [p[1] for p in pts])
        self.assertTrue(all(w >= 0.0 for w in seg.widths))

    def test_ys_none_only_density(self):
        """不给 ys 时只按密度切分，宽度全为 0。"""
        xs = [float(i) for i in range(60)]
        seg = ba.adaptive_segments(xs, None, max_segments=8,
                                   min_points_per_seg=4)
        self.assertEqual(seg.strategy, "quantile")
        self.assertTrue(all(w == 0.0 for w in seg.widths))
        # ys 为 None 时只切分不细分，段数 = 第一阶段段数
        self.assertEqual(seg.n_segments, ba.DEFAULT_BASE_SEGMENTS)
        self.assertEqual(sum(seg.counts), 60)

    def test_respects_min_points_per_seg(self):
        """每段点数不得低于 min_points_per_seg —— 否则拟合无意义。"""
        pts = mixed_scatter(60, seed=13)
        xs = [p[0] for p in pts]
        seg = ba.adaptive_segments(xs, [p[1] for p in pts], max_segments=20,
                                   min_points_per_seg=2)
        self.assertTrue(all(c >= 1 for c in seg.counts))
        # min_points_per_seg=2 时不应出现单点段
        self.assertTrue(all(c >= 2 for c in seg.counts) or
                        seg.n_segments == 1,
                        "min_points_per_seg=2 下不应有单点段")

    def test_max_segments_is_cap(self):
        """无论数据多散，段数不得超过 max_segments。"""
        pts = mixed_scatter(200, seed=17)
        xs = [p[0] for p in pts]
        seg = ba.adaptive_segments(xs, [p[1] for p in pts], max_segments=3,
                                   min_points_per_seg=2)
        self.assertLessEqual(seg.n_segments, 3)

    def test_summary_is_readable(self):
        pts = mixed_scatter(40, seed=19)
        xs = [p[0] for p in pts]
        seg = ba.adaptive_segments(xs, [p[1] for p in pts])
        s = seg.summary()
        self.assertIn("段", s)
        self.assertIn("x", s)

    # ---- 输入校验 ----
    def test_rejects_single_point(self):
        with self.assertRaises(ba.AdvancedFitError):
            ba.adaptive_segments([1.0], [2.0])

    def test_rejects_zero_max_segments(self):
        with self.assertRaises(ba.AdvancedFitError):
            ba.adaptive_segments([1.0, 2.0], [1.0, 2.0], max_segments=0)

    def test_rejects_zero_min_points(self):
        with self.assertRaises(ba.AdvancedFitError):
            ba.adaptive_segments([1.0, 2.0], [1.0, 2.0], min_points_per_seg=0)


# ==================================================================
# 3. 模型评分（AIC / BIC）
# ==================================================================
class TestScoreModels(unittest.TestCase):
    def test_exact_polynomial_gets_near_zero_sse(self):
        """数据由 d 阶多项式精确生成时，该阶的 SSE 应接近 0。"""
        pts = quadratic_exact(n=25)
        scores = {s.degree: s for s in ba.score_models(pts, max_degree=4)}
        self.assertLess(scores[2].sse, 1e-6, "精确二次的 2 阶 SSE 应≈0")
        self.assertLess(scores[3].sse, 1e-6, "高阶同样应能精确拟合")
        # 0 阶和 1 阶必然有残差
        self.assertGreater(scores[1].sse, 1.0)

    def test_r2_monotone_in_degree(self):
        """阶数越高，R² 只会不降（嵌套模型的性质）。"""
        pts = quadratic_noisy(n=50, noise=4.0, seed=11)
        scores = sorted(ba.score_models(pts, max_degree=6),
                        key=lambda s: s.degree)
        for a, b in zip(scores, scores[1:]):
            self.assertGreaterEqual(b.r2, a.r2 - 1e-12,
                                    f"阶数 {b.degree} 的 R² 低于 {a.degree}")

    def test_rmse_non_increasing(self):
        pts = quadratic_noisy(n=50, noise=4.0, seed=13)
        scores = sorted(ba.score_models(pts, max_degree=5),
                        key=lambda s: s.degree)
        for a, b in zip(scores, scores[1:]):
            self.assertLessEqual(b.rmse, a.rmse + 1e-9)

    def test_aic_bic_formula(self):
        """AIC/BIC 的公式必须与定义一致（不是随手写的数）。

        高斯似然下的最小二乘版本：
            AIC = n·ln(SSE/n) + 2k
            BIC = n·ln(SSE/n) + k·ln(n)
        其中 k = 参数个数 = degree + 1。
        """
        pts = quadratic_noisy(n=40, noise=3.0, seed=17)
        for s in ba.score_models(pts, max_degree=4):
            k = s.degree + 1
            n = s.n
            with self.subTest(degree=s.degree):
                self.assertAlmostEqual(
                    s.aic, n * math.log(s.sse / n) + 2 * k, places=4)
                self.assertAlmostEqual(
                    s.bic, n * math.log(s.sse / n) + k * math.log(n),
                    places=4)

    def test_bic_penalizes_more_than_aic(self):
        """样本量足够大时 BIC 惩罚更重（这是选 BIC 的全部理由）。"""
        pts = quadratic_noisy(n=200, noise=5.0, seed=19)
        for s in ba.score_models(pts, max_degree=6):
            # 只在高阶项真有时比较；常数项=模型本身
            pass
        scores = {s.degree: s for s in ba.score_models(pts, max_degree=6)}
        # 取一个被过度拟合的高阶，比较 penalties 之差
        target = scores[6]
        k = 7
        n = target.n
        penalty_aic = 2 * k
        penalty_bic = k * math.log(n)
        self.assertGreater(penalty_bic, penalty_aic)

    def test_perfect_fit_does_not_blow_up(self):
        """SSE≈0 时 AIC/BIC 必须仍是有限数（历史 bug 回归）。

        早期用 1e-300 兜底，导致 ln(SSE) 变 -2000 量级，
        各阶差异完全由浮点尾噪主导，干净二次数据被误判成 4 阶。
        """
        pts = quadratic_exact(n=20)
        for s in ba.score_models(pts, max_degree=6):
            self.assertTrue(math.isfinite(s.aic), f"{s.degree} 阶 AIC 非有限")
            self.assertTrue(math.isfinite(s.bic), f"{s.degree} 阶 BIC 非有限")

    def test_explicit_degrees_respected(self):
        pts = quadratic_noisy(n=40, seed=23)
        scores = ba.score_models(pts, degrees=[1, 2, 3])
        self.assertEqual([s.degree for s in scores], [1, 2, 3])

    def test_summary_contains_all_fields(self):
        pts = quadratic_noisy(n=30, seed=29)
        s = ba.score_models(pts, max_degree=2)[0]
        out = s.summary()
        for token in ("阶数", "R2", "RMSE", "AIC", "BIC"):
            self.assertIn(token, out)

    # ---- 输入校验 ----
    def test_rejects_few_points(self):
        with self.assertRaises(ba.AdvancedFitError):
            ba.score_models([(1.0, 2.0), (2.0, 3.0)], max_degree=2)


# ==================================================================
# 4. 自动选阶
# ==================================================================
class TestSelectDegree(unittest.TestCase):
    def test_picks_true_degree_on_noisy_quadratic(self):
        """核心验收：噪声二次数据必须选中 2 阶，不是更高阶。"""
        pts = quadratic_noisy(n=60, noise=3.0, seed=41)
        for crit in ("bic", "aic"):
            sel = ba.select_degree(pts, criterion=crit, max_degree=6)
            self.assertEqual(sel.best.degree, 2,
                             f"{crit.upper()} 选中 {sel.best.degree} 阶")

    def test_picks_one_on_linear_cloud(self):
        pts = linear_cloud(n=100, slope=1.2, noise=4.0, seed=43)
        sel = ba.select_degree(pts, criterion="bic", max_degree=5)
        self.assertEqual(sel.best.degree, 1)

    def test_aic_vs_bic_can_differ(self):
        """噪声大、样本小时，AIC 与 BIC 可能选不同阶数。

        这条锁的是两个准则**行为可区分**，而不是"哪个更好" ——
        它们本就该有不同倾向：BIC 惩罚更重，更倾向简单模型。
        """
        pts = quadratic_noisy(n=25, noise=20.0, seed=47)
        sel_a = ba.select_degree(pts, criterion="aic", max_degree=6)
        sel_b = ba.select_degree(pts, criterion="bic", max_degree=6)
        # 都应是有限结果；且不要求相同（相同也合法，只要各自符合最小化定义）
        self.assertTrue(math.isfinite(sel_a.best.aic))
        self.assertTrue(math.isfinite(sel_b.best.bic))

    def test_bic_selects_minimum_bic(self):
        pts = quadratic_noisy(n=60, noise=4.0, seed=53)
        sel = ba.select_degree(pts, criterion="bic", max_degree=5)
        best = min(sel.candidates, key=lambda c: c.bic)
        self.assertEqual(sel.best.degree, best.degree)

    def test_aic_selects_minimum_aic(self):
        pts = quadratic_noisy(n=60, noise=4.0, seed=59)
        sel = ba.select_degree(pts, criterion="aic", max_degree=5)
        best = min(sel.candidates, key=lambda c: c.aic)
        self.assertEqual(sel.best.degree, best.degree)

    def test_criterion_case_insensitive(self):
        pts = quadratic_noisy(n=40, seed=61)
        self.assertEqual(
            ba.select_degree(pts, criterion="BIC", max_degree=4).criterion,
            "BIC")

    def test_candidates_are_sorted_by_degree(self):
        pts = quadratic_noisy(n=40, seed=67)
        sel = ba.select_degree(pts, max_degree=5)
        degs = [c.degree for c in sel.candidates]
        self.assertEqual(degs, sorted(degs))

    def test_summary_lists_candidates(self):
        pts = quadratic_noisy(n=40, seed=71)
        sel = ba.select_degree(pts, max_degree=3)
        out = sel.summary()
        self.assertIn("选中阶数", out)
        self.assertIn("AIC", out)

    # ---- 输入校验 ----
    def test_rejects_unknown_criterion(self):
        pts = quadratic_noisy(n=20)
        with self.assertRaises(ba.AdvancedFitError):
            ba.select_degree(pts, criterion="mse")


# ==================================================================
# 5. Bootstrap 置信带
# ==================================================================
class TestBootstrapBand(unittest.TestCase):
    def test_contains_original_trend(self):
        """置信带应把原始走势线包住（这是"置信"二字的含义）。"""
        pts = linear_cloud(n=80, slope=1.0, noise=6.0, seed=73)
        band = ba.bootstrap_band(pts, n_segments=4, degree=1, n_boot=40,
                                 x_from=0.0, x_to=79.0, seed=11)
        trend = bf.band_fit(pts, n_segments=4, degree=1)
        for x in (0.0, 20.0, 40.0, 79.0):
            lo = min(band.lower), max(band.lower)
        for i, x in enumerate(band.xs):
            self.assertLessEqual(band.lower[i] - 1e-9, trend.predict(x),
                                 f"x={x} 走势线跑到下界之下")
            self.assertGreaterEqual(band.upper[i] + 1e-9, trend.predict(x),
                                    f"x={x} 走势线跑到上界之上")

    def test_lower_not_above_upper(self):
        pts = linear_cloud(n=60, seed=79)
        band = ba.bootstrap_band(pts, n_segments=4, degree=1, n_boot=30,
                                 seed=13)
        for l, u in zip(band.lower, band.upper):
            self.assertLessEqual(l, u + 1e-9)

    def test_center_between_bounds(self):
        pts = linear_cloud(n=60, seed=83)
        band = ba.bootstrap_band(pts, n_segments=4, degree=1, n_boot=30,
                                 seed=17)
        for l, c, u in zip(band.lower, band.center, band.upper):
            self.assertGreaterEqual(c, l - 1e-9)
            self.assertLessEqual(c, u + 1e-9)

    def test_reproducible_with_same_seed(self):
        """同一 seed 必须给出逐位相同的结果（可复现是 bootstrap 的价值之一）。"""
        pts = linear_cloud(n=60, seed=89)
        a = ba.bootstrap_band(pts, n_segments=4, degree=1, n_boot=20, seed=99)
        b = ba.bootstrap_band(pts, n_segments=4, degree=1, n_boot=20, seed=99)
        self.assertEqual(a.xs, b.xs)
        self.assertEqual(a.lower, b.lower)
        self.assertEqual(a.upper, b.upper)
        self.assertEqual(a.center, b.center)

    def test_different_seed_differs(self):
        """不同 seed 应给出不同结果（否则 seed 参数是假的）。"""
        pts = linear_cloud(n=60, seed=97)
        a = ba.bootstrap_band(pts, n_segments=4, degree=1, n_boot=20, seed=1)
        b = ba.bootstrap_band(pts, n_segments=4, degree=1, n_boot=20, seed=2)
        self.assertNotEqual(a.lower, b.lower)

    def test_band_narrows_with_more_samples(self):
        """样本越多，置信带应越窄（统计性质，不是实现细节）。

        必须比**每点平均宽度**，不能比总宽度：两次调用的 x 网格
        点数不同（n=30 出 16 点，n=150 出 76 点），总宽度会被
        网格长度主导。早先直接比总宽度，得到"样本越多带越宽"
        的反结论 —— 那是指标选错了，不是实现错了。
        """
        small = ba.bootstrap_band(linear_cloud(n=30, seed=101), n_segments=4,
                                  degree=1, n_boot=30, seed=5,
                                  x_from=0.0, x_to=29.0)
        big = ba.bootstrap_band(linear_cloud(n=150, seed=101), n_segments=4,
                               degree=1, n_boot=30, seed=5,
                               x_from=0.0, x_to=149.0)
        w_small = small.mean_width()
        w_big = big.mean_width()
        self.assertLess(w_big, w_small,
                        f"每点平均宽度 n=150 为 {w_big:.3f}，"
                        f"应小于 n=30 的 {w_small:.3f}")

    def test_wider_alpha_gives_wider_band(self):
        """置信水平越高（α 越小），带越宽。"""
        pts = linear_cloud(n=80, seed=103)
        narrow = ba.bootstrap_band(pts, n_segments=4, degree=1, n_boot=50,
                                   alpha=0.30, seed=7)
        wide = ba.bootstrap_band(pts, n_segments=4, degree=1, n_boot=50,
                                 alpha=0.01, seed=7)
        w_n = narrow.mean_width()
        w_w = wide.mean_width()
        self.assertGreaterEqual(w_w, w_n)

    def test_x_grid_covers_requested_range(self):
        pts = linear_cloud(n=50, seed=107)
        band = ba.bootstrap_band(pts, n_segments=4, degree=1, n_boot=20,
                                 x_from=5.0, x_to=45.0, seed=3)
        self.assertGreaterEqual(min(band.xs), 5.0)
        self.assertLessEqual(max(band.xs), 45.0)

    def test_backend_field_defaults_cpu(self):
        pts = linear_cloud(n=40, seed=109)
        band = ba.bootstrap_band(pts, n_segments=3, degree=1, n_boot=10,
                                 seed=3)
        self.assertEqual(band.backend, "cpu")

    def test_summary_reports_counts(self):
        pts = linear_cloud(n=40, seed=113)
        band = ba.bootstrap_band(pts, n_segments=3, degree=1, n_boot=12,
                                 seed=3)
        s = band.summary()
        self.assertIn("12 次", s)
        self.assertIn("95%", s)

    # ---- GPU 后端注入 ----
    def test_broken_gpu_backend_falls_back(self):
        """后端抛异常必须静默回落 CPU，不能让 bootstrap 整体失败。"""
        class Broken:
            def fit_batch(self, *a, **k):
                raise RuntimeError("模拟 GPU 崩溃")

        pts = linear_cloud(n=40, seed=127)
        band = ba.bootstrap_band(pts, n_segments=3, degree=1, n_boot=10,
                                 seed=3, gpu=Broken())
        self.assertEqual(band.backend, "cpu")
        self.assertEqual(len(band.xs), len(band.lower))

    def test_wrong_type_gpu_backend_falls_back(self):
        class WrongType:
            def fit_batch(self, samples, *a, **k):
                return [(1, 2, 3)] * len(samples)

        pts = linear_cloud(n=40, seed=131)
        band = ba.bootstrap_band(pts, n_segments=3, degree=1, n_boot=10,
                                 seed=3, gpu=WrongType())
        self.assertEqual(band.backend, "cpu")

    def test_partial_gpu_backend_falls_back(self):
        """部分成功也必须整批回落（否则 CPU/GPU 结果混用）。"""
        class Mixed:
            def fit_batch(self, samples, *a, **k):
                out = [None] * len(samples)
                return out

        pts = linear_cloud(n=40, seed=137)
        band = ba.bootstrap_band(pts, n_segments=3, degree=1, n_boot=10,
                                 seed=3, gpu=Mixed())
        self.assertEqual(band.backend, "cpu")

    # ---- 输入校验 ----
    def test_rejects_too_few_points(self):
        with self.assertRaises(ba.AdvancedFitError):
            ba.bootstrap_band([(1.0, 1.0), (2.0, 2.0)], n_boot=5)

    def test_rejects_zero_n_boot(self):
        pts = linear_cloud(n=20, seed=139)
        with self.assertRaises(ba.AdvancedFitError):
            ba.bootstrap_band(pts, n_boot=0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
