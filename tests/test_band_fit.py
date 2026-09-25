"""分段包络估计（band_fit）测试。

覆盖：分段均值分组、系数调和、偏移常数 C、离散宽度非负、
离散趋势判定、包络/离散度序列生成、已知边界。
"""

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import band_fit as bf
from core import fitting as ft


# ------------------------------------------------------------------
# 测试数据构造
# ------------------------------------------------------------------
def noisy_linear(n=60, seed=3):
    """围绕 y = 1.5x + 50 散布的噪声点（等宽误差）。"""
    import random
    rng = random.Random(seed)
    return [(float(i), 50.0 + 1.5 * i + rng.uniform(-8, 8))
            for i in range(1, n + 1)]


def two_parallel_lines(n=30):
    """两支斜率相同、常数差固定：y = 1.5x + 50 ± 5（两两交替）。"""
    pts = []
    for i in range(1, n + 1):
        x = float(i)
        bias = 5.0 if (i // 2) % 2 == 0 else -5.0
        pts.append((x, 50.0 + 1.5 * x + bias))
    return pts


def two_different_slopes(n=40):
    """两支斜率不同（1.8 / 2.2），两两交替保证 x 区间重叠。"""
    pts = []
    for i in range(1, n + 1):
        x = float(i)
        slope = 1.8 if (i // 2) % 2 == 0 else 2.2
        pts.append((x, 40.0 + slope * x))
    return pts


def narrowing_scatter(n=120):
    """误差随 x 增大而减小：后段更集中。"""
    pts = []
    for i in range(1, n + 1):
        x = float(i)
        w = 200.0 / (x + 1.0)
        pts.append((x, 100.0 + 2.0 * x + w * (1 if i % 2 else -1)))
    return pts


def widening_scatter(n=60):
    """误差随 x 增大而增大：后段更发散。"""
    return [(float(i), 50.0 + 0.5 * i + 1.0 * i * (1 if i % 2 else -1))
            for i in range(1, n + 1)]


class TestSegmentMeans(unittest.TestCase):
    def test_equal_width(self):
        segs = bf.make_segments([0.0, 1.0, 2.0, 3.0, 4.0], 2)
        self.assertEqual(len(segs), 2)
        self.assertAlmostEqual(segs[0][0], 0.0)
        self.assertAlmostEqual(segs[0][1], 2.0)
        self.assertAlmostEqual(segs[1][0], 2.0)
        self.assertAlmostEqual(segs[1][1], 4.0)

    def test_means_computed(self):
        segs = bf.make_segments([1.0, 2.0, 10.0, 11.0], 2)
        means, counts = bf.segment_means(
            [1.0, 2.0, 10.0, 11.0], [2.0, 4.0, 20.0, 40.0], segs)
        self.assertAlmostEqual(means[0], 3.0)
        self.assertAlmostEqual(means[1], 30.0)
        self.assertEqual(counts, [2, 2])

    def test_last_segment_closed(self):
        segs = bf.make_segments([0.0, 1.0, 2.0], 2)
        # 右端点必须被归入最后一段而不是越界
        self.assertEqual(bf.segment_of(2.0, segs), 1)
        self.assertEqual(bf.segment_of(0.0, segs), 0)

    def test_empty_segment_is_nan(self):
        segs = [(0.0, 5.0), (5.0, 10.0)]
        means, counts = bf.segment_means([1.0, 2.0], [1.0, 2.0], segs)
        self.assertTrue(math.isnan(means[1]))
        self.assertEqual(counts[1], 0)


class TestReconcile(unittest.TestCase):
    def test_plain_average(self):
        out = bf.reconcile_coefficients([1.0], [3.0], mode="mean")
        self.assertAlmostEqual(out[0], 2.0)

    def test_default_mode_is_mean(self):
        """默认规则必须是普通平均（当前在用）。"""
        out = bf.reconcile_coefficients([1.8], [2.2])
        self.assertAlmostEqual(out[0], 2.0)

    def test_no_amplification(self):
        """平均规则不能放大系数：结果必须落在两支之间。"""
        for a, b in [(1.8, 2.2), (5.0, 7.0), (-3.0, -1.0)]:
            out = bf.reconcile_coefficients([a], [b])[0]
            self.assertGreaterEqual(out, min(a, b) - 1e-12)
            self.assertLessEqual(out, max(a, b) + 1e-12)

    def test_spec_mode_doubles_when_same_sign(self):
        """记录旧规则行为：同号时翻倍。已被否决，仅留档。"""
        spec = bf.reconcile_coefficients([1.8], [2.2], mode="spec")
        self.assertAlmostEqual(spec[0], 4.0)

    def test_equal_magnitude_takes_upper_value(self):
        """spec 模式下 |a|==|b| 判一致取 a，不抵消（固有行为）。"""
        out = bf.reconcile_coefficients([3.0], [-3.0], mode="spec")
        self.assertAlmostEqual(out[0], 3.0)

    def test_unequal_length_padded(self):
        out = bf.reconcile_coefficients([1.0, 2.0], [1.0], mode="mean")
        self.assertEqual(len(out), 2)
        self.assertAlmostEqual(out[1], 1.0)   # 短侧补 0 后平均


class TestScatterProfile(unittest.TestCase):
    def test_stable_band_detected(self):
        """等宽误差 -> 离散程度应判 stable。"""
        import random
        rng = random.Random(3)
        ds = [8.0 + rng.uniform(-0.5, 0.5) for _ in range(12)]
        p = bf.profile_scatter(ds, ds)
        self.assertEqual(p.trend, "stable")
        self.assertIn("相当", p.description)

    def test_narrowing_detected(self):
        """宽度递减 -> 判 narrowing，描述"后段更集中"。"""
        ds = [20.0 - 1.5 * i for i in range(12)]
        p = bf.profile_scatter(ds, ds)
        self.assertEqual(p.trend, "narrowing")
        self.assertIn("集中", p.description)

    def test_widening_detected(self):
        """宽度递增 -> 判 widening，描述"后段更发散"。"""
        ds = [2.0 + 1.5 * i for i in range(12)]
        p = bf.profile_scatter(ds, ds)
        self.assertEqual(p.trend, "widening")
        self.assertIn("发散", p.description)

    def test_no_convergence_claim(self):
        """离散画像绝不能出现'收敛/极限'字样——不是本算法的职责。"""
        for ds in ([10.0, 8.0, 6.0, 4.0, 2.0, 1.0],
                   [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
                   [5.0, 5.0, 5.0, 5.0, 5.0, 5.0]):
            p = bf.profile_scatter(ds, ds)
            self.assertNotIn("收敛", p.description)
            self.assertNotIn("极限", p.description)

    def test_widest_and_tightest(self):
        ds = [5.0, 9.0, 3.0, 7.0]
        p = bf.profile_scatter(ds, ds)
        self.assertEqual(p.widest_segment, 1)
        self.assertEqual(p.tightest_segment, 2)
        self.assertAlmostEqual(p.min_d, 3.0)
        self.assertAlmostEqual(p.max_d, 9.0)
        self.assertAlmostEqual(p.mean_d, 6.0)

    def test_length_mismatch_raises(self):
        with self.assertRaises(bf.BandFitError):
            bf.profile_scatter([1.0, 2.0], [1.0])

    def test_empty_raises(self):
        with self.assertRaises(bf.BandFitError):
            bf.profile_scatter([], [])


class TestTrendEstimation(unittest.TestCase):
    def test_recovers_true_linear_trend(self):
        """围绕直线散布的点，走势线应还原真实斜率与截距。"""
        pts = noisy_linear(60)
        r = bf.band_fit(pts, n_segments=10, degree=1)
        self.assertAlmostEqual(r.coefficients[1], 1.5, delta=0.05)
        self.assertAlmostEqual(r.coefficients[0], 50.0, delta=2.0)
        self.assertEqual(r.caution, "")

    def test_recovers_true_quadratic_trend(self):
        """绕抛物线散布的点，走势线应还原二次项。"""
        pts = []
        for i in range(1, 81):
            x = float(i)
            pts.append((x, 3.0 + 0.5 * x - 0.02 * x * x
                        + ((i * 7) % 5 - 2) * 2.0))
        r = bf.band_fit(pts, n_segments=10, degree=2)
        self.assertAlmostEqual(r.coefficients[2], -0.02, delta=0.002)
        self.assertAlmostEqual(r.coefficients[1], 0.5, delta=0.05)

    def test_parallel_lines_recovered_exactly(self):
        """平行支：平均后应精确得到中心线，C 约 0。"""
        pts = two_parallel_lines(30)
        r = bf.band_fit(pts, n_segments=8, degree=1)
        self.assertAlmostEqual(r.coefficients[1], 1.5, places=6)
        self.assertAlmostEqual(r.coefficients[0], 50.0, places=6)
        self.assertAlmostEqual(r.offset, 0.0, places=6)

    def test_reconciliation_is_plain_average(self):
        """两支系数应逐位平均，不引入额外放大。"""
        pts = two_parallel_lines(30)
        r = bf.band_fit(pts, n_segments=8, degree=1)
        self.assertAlmostEqual(r.upper.coefficients[1], 1.5, places=6)
        self.assertAlmostEqual(r.lower.coefficients[1], 1.5, places=6)
        self.assertAlmostEqual(r.reconciled[1], 1.5, places=6)
        self.assertAlmostEqual(r.reconciled[0], 50.0, places=6)

    def test_different_slopes_averaged(self):
        """两支异斜率时，走势线斜率应接近两者均值。"""
        pts = two_different_slopes(40)
        r = bf.band_fit(pts, n_segments=8, degree=1)
        self.assertAlmostEqual(r.reconciled[1], 2.0, delta=0.1)


class TestScatterWidth(unittest.TestCase):
    def test_width_never_negative(self):
        """离散宽度必须非负：两支后段可能交叉。"""
        for pts in (noisy_linear(60), narrowing_scatter(80),
                    widening_scatter(60), two_parallel_lines(30)):
            r = bf.band_fit(pts, n_segments=10, degree=1)
            for d in r.scatter.d_values:
                self.assertGreaterEqual(d, 0.0)

    def test_dev_series_nonneg(self):
        r = bf.band_fit(two_parallel_lines(30), n_segments=8, degree=1)
        xs, dy = bf.dev_series(r, 1.0, 30.0, 3)
        for d in dy:
            self.assertGreaterEqual(d, 0.0)

    def test_width_tracks_real_error(self):
        """已知 ±5 的平行支，实测离散宽度应接近 10。"""
        pts = two_parallel_lines(30)
        r = bf.band_fit(pts, n_segments=8, degree=1)
        self.assertAlmostEqual(r.scatter.mean_d, 10.0, delta=1.0)

    def test_width_tracks_varying_error(self):
        """误差按 200/(x+1) 衰减时，最大宽度应远大于最小宽度。"""
        pts = narrowing_scatter(120)
        r = bf.band_fit(pts, n_segments=12, degree=1)
        self.assertGreater(r.scatter.max_d, 3 * r.scatter.min_d)

    def test_segments_populated(self):
        r = bf.band_fit(noisy_linear(60), n_segments=10, degree=1)
        self.assertTrue(r.segments)
        self.assertGreater(r.n_segments, 0)
        for s in r.segments:
            self.assertGreaterEqual(s.x_hi, s.x_lo)
            self.assertGreaterEqual(s.spread, 0.0)
            self.assertGreaterEqual(s.n_upper + s.n_lower, 0)

    def test_summary_contains_all_parts(self):
        r = bf.band_fit(two_parallel_lines(30), n_segments=8, degree=1)
        s = r.summary
        self.assertIn("整体走势", s)
        self.assertIn("上界", s)
        self.assertIn("下界", s)
        self.assertIn("C =", s)
        self.assertIn("离散宽度", s)
        self.assertIn("离散趋势", s)

    def test_derivation_shows_steps(self):
        r = bf.band_fit(two_parallel_lines(30), n_segments=8, degree=1)
        d = r.derivation
        self.assertIn("逐位取平均", d)
        self.assertIn("上界系数", d)
        self.assertIn("下界系数", d)
        self.assertIn("偏移常数 C", d)
        self.assertIn("R²", d)


class TestErrorHandling(unittest.TestCase):
    def test_too_few_points(self):
        with self.assertRaises(bf.BandFitError):
            bf.band_fit([(1.0, 2.0)])

    def test_no_points(self):
        with self.assertRaises(bf.BandFitError):
            bf.band_fit([])

    def test_identical_x(self):
        pts = [(1.0, float(i)) for i in range(10)]
        with self.assertRaises(bf.BandFitError):
            bf.band_fit(pts)

    def test_identical_y(self):
        pts = [(float(i), 5.0) for i in range(10)]
        with self.assertRaises(bf.BandFitError):
            bf.band_fit(pts)

    def test_negative_degree(self):
        with self.assertRaises(bf.BandFitError):
            bf.band_fit([(1.0, 2.0), (2.0, 3.0)], degree=-1)

    def test_all_same_side_raises(self):
        """点数远少于分段数时空段导致一侧无点，必须报错。"""
        with self.assertRaises(bf.BandFitError):
            bf.band_fit([(1.0, 1.0), (2.0, 2.0)], n_segments=8, degree=1)


class TestKnownBoundary(unittest.TestCase):
    def test_x_disjoint_groups_gives_wrong_slope(self):
        """已知边界：偏差在 x 上连续成片时，两组 x 区间不重叠，
        各自只拟合一半范围，斜率估计失真。

        这是结构性限制（不是 bug），留测试记录它，
        并验证此时 caution 会给出提示。
        """
        pts = [(float(i), 50 + 1.5 * i + (5.0 if i <= 15 else -5.0))
               for i in range(1, 31)]
        r = bf.band_fit(pts, n_segments=8, degree=1)
        self.assertLess(r.coefficients[1], 1.2)     # 真值 1.5
        self.assertTrue(r.caution, "应提示走势线不可靠")
        self.assertIn("偏离", r.caution)

    def test_low_r2_triggers_caution(self):
        """发散型数据下单多项式套不住，R² 低时必须提示。"""
        pts = [(float(i), 100.0 + 2.0 * i + 0.05 * i * i * (1 if i % 2 else -1))
               for i in range(1, 81)]
        r = bf.band_fit(pts, n_segments=12, degree=1)
        self.assertLess(r.r2, 0.5)
        self.assertTrue(r.caution, "R² 过低时应给出提示")
        self.assertIn("R²", r.caution)

    def test_good_data_has_no_caution(self):
        """噪声/交替型数据（真实测量常见）不应触发告警。"""
        r = bf.band_fit(noisy_linear(60), n_segments=10, degree=1)
        self.assertEqual(r.caution, "")


class TestSeries(unittest.TestCase):
    def test_band_series_length(self):
        r = bf.band_fit(two_parallel_lines(30), n_segments=8, degree=1)
        xs, up, lo = bf.band_series(r, 1.0, 30.0, 3)
        self.assertEqual(len(xs), len(up))
        self.assertEqual(len(xs), len(lo))
        for u, l in zip(up, lo):
            self.assertGreaterEqual(u, l)

    def test_reversed_range_returns_empty(self):
        r = bf.band_fit(two_parallel_lines(30), n_segments=8, degree=1)
        xs, up, lo = bf.band_series(r, 30.0, 1.0, 3)
        self.assertEqual(xs, [])

    def test_describe_segments(self):
        r = bf.band_fit(two_parallel_lines(30), n_segments=8, degree=1)
        txt = bf.describe_segments(r)
        self.assertIn("分段统计", txt)
        self.assertIn("离散宽", txt)
        self.assertIn("离散程度", txt)
        self.assertIn("上组", txt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
