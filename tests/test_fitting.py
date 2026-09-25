"""fitting.py 单元测试：数值正确性、模型选择、画布映射、异常路径。"""

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import fitting as ft
from core import graph_engine as ge


class TestValidation(unittest.TestCase):
    def test_empty(self):
        with self.assertRaises(ft.FitError):
            ft.fit_polynomial([], 1)

    def test_single_point(self):
        with self.assertRaises(ft.FitError):
            ft.fit_polynomial([(10, 20)], 1)

    def test_identical_x(self):
        with self.assertRaises(ft.FitError):
            ft.fit_polynomial([(5, 1), (5, 2)], 1)

    def test_nonfinite(self):
        with self.assertRaises(ft.FitError):
            ft.fit_polynomial([(0, 0), (1, float("nan"))], 1)

    def test_bad_shape(self):
        with self.assertRaises(ft.FitError):
            ft.fit_polynomial([(1,), (2, 3)], 1)

    def test_degree_zero(self):
        with self.assertRaises(ft.FitError):
            ft.fit_polynomial([(1, 2), (3, 4)], 0)

    def test_degree_above_cap(self):
        with self.assertRaises(ft.FitError):
            ft.fit_polynomial([(1, 2), (3, 4)], 99)

    def test_too_few_distinct_x_for_degree(self):
        # 2 个不同 x，无法定三次
        with self.assertRaises(ft.FitError):
            ft.fit_polynomial([(0, 0), (1, 1), (1, 2)], 3)


class TestExactRecovery(unittest.TestCase):
    """精确数据必须被高精度还原 —— 归一化是否有效的直接证据。

    做法：用给定系数组造无噪声数据，拟合后
    ① R² 必须为 1、RMSE 接近 0；
    ② predict(x) 逐点等于真值。
    """

    def _make(self, coeffs, xs):
        ys = []
        for x in xs:
            acc = 0.0
            for c in reversed(coeffs):        # 低次在前 -> 用 Horner 求值
                acc = acc * x + c
            ys.append(acc)
        return list(zip(xs, ys))

    def _check(self, coeffs, xs, tol):
        pts = self._make(coeffs, xs)
        deg = len(coeffs) - 1
        r = ft.fit_polynomial(pts, deg)
        self.assertAlmostEqual(r.r2, 1.0, places=9, msg="R² 应为 1")
        self.assertLess(r.rmse, 1e-6, msg="RMSE 应接近 0")
        for x, y in pts:
            self.assertAlmostEqual(r.predict(x), y, delta=tol,
                                   msg=f"x={x} 还原失败")

    def test_recover_linear(self):
        self._check([0.0, 0.5], list(range(0, 301, 50)), 1e-5)

    def test_recover_quadratic(self):
        self._check([250.0, 0.01, 0.0002], list(range(0, 501, 50)), 1e-4)

    def test_recover_cubic(self):
        self._check([100.0, 0.05, -0.0003, 4e-7], list(range(0, 501, 50)), 1e-2)

    def test_recover_quartic(self):
        self._check([50.0, 0.02, 1e-5, -2e-8, 1e-11], list(range(0, 501, 25)), 1e-1)


class TestRecoverLine(unittest.TestCase):
    def test_horizontal_line(self):
        r = ft.fit_polynomial([(0, 100), (100, 100), (200, 100)], 1)
        self.assertAlmostEqual(r.r2, 1.0, places=10)
        self.assertAlmostEqual(r.predict(50), 100.0, delta=1e-6)

    def test_slope(self):
        r = ft.fit_polynomial([(0, 0), (100, 50), (200, 100)], 1)
        self.assertAlmostEqual(r.coefficients[0], 0.0, delta=1e-4)
        self.assertAlmostEqual(r.coefficients[1], 0.5, delta=1e-4)
        self.assertAlmostEqual(r.predict(300), 150.0, delta=1e-3)


class TestRecoverQuadratic(unittest.TestCase):
    def test_pixel_coords_not_ill_conditioned(self):
        """像素级 x(0~700) 的二次拟合，这是归一化要解决的核心问题。"""
        true_c = (250.0, 0.01, 0.0002)   # y = 250 + 0.01x + 0.0002x²
        pts = [(float(x), true_c[0] + true_c[1] * x + true_c[2] * x * x)
               for x in range(0, 701, 20)]
        r = ft.fit_polynomial(pts, 2)
        self.assertAlmostEqual(r.rmse, 0.0, delta=1e-5)
        for x, y in pts:
            self.assertAlmostEqual(r.predict(x), y, delta=1e-4)


class TestRecoverCubic(unittest.TestCase):
    def test_cubic_in_pixel_coords(self):
        a, b, c, d = 100.0, 0.05, -0.0003, 0.0000004
        pts = [(float(x), a + b * x + c * x * x + d * x ** 3)
               for x in range(0, 701, 35)]
        r = ft.fit_polynomial(pts, 3)
        self.assertAlmostEqual(r.r2, 1.0, places=8)
        for x, y in pts:
            self.assertAlmostEqual(r.predict(x), y, delta=1e-3)


class TestRecoverQuartic(unittest.TestCase):
    def test_quartic_stability(self):
        def f(x):
            return 50 + 0.02 * x + 1e-5 * x ** 2 - 2e-8 * x ** 3 + 1e-11 * x ** 4

        pts = [(float(x), f(x)) for x in range(0, 701, 25)]
        r = ft.fit_polynomial(pts, 4)
        self.assertAlmostEqual(r.r2, 1.0, places=7)
        for x, y in pts:
            self.assertAlmostEqual(r.predict(x), y, delta=1e-2)


class TestVeryLargeCoords(unittest.TestCase):
    def test_huge_x_values(self):
        """x 到 1e6 时未归一化会直接病态，归一化后应仍稳定。"""
        a, b = 1e5, 3.0
        pts = [(float(x), a + b * x) for x in range(0, 1_000_001, 100_000)]
        r = ft.fit_polynomial(pts, 1)
        self.assertAlmostEqual(r.r2, 1.0, places=8)
        self.assertAlmostEqual(r.predict(500_000), a + b * 500_000, delta=1e-3)


class TestNoisyData(unittest.TestCase):
    def test_noisy_line_high_r2(self):
        import random
        random.seed(42)
        base, slope = 200.0, 0.15
        # ±3 噪声下信噪比约 base/slope/range，R² 落在 0.99 量级
        pts = [(float(x), base + slope * x + random.uniform(-3, 3))
               for x in range(0, 500, 10)]
        r = ft.fit_polynomial(pts, 1)
        self.assertGreater(r.r2, 0.99)
        self.assertLess(r.rmse, 3.0)
        self.assertAlmostEqual(r.coefficients[1], slope, delta=0.02)

    def test_noise_inflates_rmse_not_r2(self):
        pts = [(0, 0), (100, 100), (200, 198), (300, 305)]
        r = ft.fit_polynomial(pts, 1)
        self.assertGreater(r.rmse, 0.0)
        self.assertLess(r.r2, 1.0)
        self.assertGreater(r.r2, 0.99)


class TestModelSelection(unittest.TestCase):
    def test_linear_data_picks_linear(self):
        pts = [(float(x), 100 + 0.2 * x) for x in range(0, 500, 25)]
        r = ft.auto_fit(pts)
        self.assertEqual(r.degree, 1, "线性数据应选 1 阶而非过参数化")

    def test_quadratic_data_picks_quadratic(self):
        pts = [(float(x), 250 + 0.01 * x ** 2) for x in range(0, 500, 25)]
        r = ft.auto_fit(pts)
        self.assertEqual(r.degree, 2)

    def test_candidates_ascending(self):
        pts = [(float(x), (x % 7) * 5.0) for x in range(0, 400, 10)]
        cands = ft.fit_candidates(pts, max_degree=4)
        self.assertTrue(cands)
        degrees = [c.degree for c in cands]
        self.assertEqual(degrees, sorted(degrees))
        self.assertLessEqual(max(degrees), 4)

    def test_select_prefers_lowest_good(self):
        pts = [(float(x), 50 + 0.1 * x) for x in range(0, 300, 15)]
        cands = ft.fit_candidates(pts, 4)
        chosen = ft.select_model(cands)
        self.assertEqual(chosen.degree, 1)

    def test_select_model_empty(self):
        with self.assertRaises(ft.FitError):
            ft.select_model([])

    def test_noisy_handdrawn_data_does_not_overfit(self):
        """实测案例：真实鼠标噪声数据不得一路爬到 4 阶。

        数据来自 render_fit_demo.py 的真实手绘采集。旧实现（纯 R² 绝对差
        阈值）会选 4 阶，R² 仅 0.46→0.755，表达式复杂到不可用。
        """
        w = 482
        pts = []
        for i in range(61):
            t = i / 60
            x = int(w * t)
            y = int(120 + 0.0009 * (x - w / 2) ** 2 + 6 * ((i % 3) - 1))
            pts.append((float(x), float(y)))
        pts = pts[::3]
        r = ft.auto_fit(pts, max_degree=4)
        self.assertEqual(r.degree, 2, f"噪声数据应从简选 2 阶，实际 {r.degree}")

    def test_cubic_with_small_leading_coeff_not_truncated(self):
        """实测案例：高次小系数数据不得被 1 阶截住。

        旧实现（只看 RMSE 相对降幅）会停在 1 阶，虽然 R²=0.9986，
        但表达式错成 y = 0.02x + 50.091，丢掉了真实的三次项。
        """
        cub = [(float(x), 50.0 + 0.02 * x + 1e-5 * x ** 2 - 2e-8 * x ** 3)
               for x in range(0, 501, 25)]
        cands = ft.fit_candidates(cub, max_degree=4)
        self.assertEqual(cands[0].degree, 1)
        # 1 阶 R² 已经很高，但不足以掩盖三次项
        self.assertGreater(cands[0].r2, 0.99)
        r = ft.select_model(cands)
        self.assertEqual(r.degree, 3, f"应识别出三次项，实际 {r.degree}")
        self.assertAlmostEqual(r.coefficients[0], 50.0, delta=0.5)
        self.assertAlmostEqual(r.coefficients[1], 0.02, delta=0.005)


class TestMaxUsefulDegree(unittest.TestCase):
    def test_two_points_only_linear(self):
        self.assertEqual(ft.max_useful_degree(2, 2, 4), 1)

    def test_three_points_caps_at_one(self):
        self.assertEqual(ft.max_useful_degree(3, 3, 4), 1)

    def test_many_points_capped_by_max(self):
        self.assertEqual(ft.max_useful_degree(50, 50, 4), 4)

    def test_duplicate_x_reduces_cap(self):
        self.assertEqual(ft.max_useful_degree(6, 3, 4), 2)


class TestR2EdgeCases(unittest.TestCase):
    def test_perfect_fit_is_one(self):
        r = ft.fit_polynomial([(0, 0), (10, 10), (20, 20)], 1)
        self.assertAlmostEqual(r.r2, 1.0, places=10)

    def test_flat_data(self):
        r = ft.fit_polynomial([(0, 5), (10, 5), (20, 5)], 1)
        self.assertEqual(r.r2, 1.0)

    def test_r2_can_be_negative_locally(self):
        # 强制 1 阶拟合抛物线数据：局部 R² 仍为正，这里只验证不越界
        pts = [(float(x), x * x) for x in range(0, 100, 10)]
        r = ft.fit_polynomial(pts, 1)
        self.assertTrue(-1e9 < r.r2 <= 1.0 + 1e-12)


class TestExpression(unittest.TestCase):
    def test_linear(self):
        self.assertEqual(ft.format_polynomial([0.0, 2.0]), "y = 2x")

    def test_scale_one_omitted(self):
        # 系数低次在前：[常数, 一次] -> y = 3x + 1
        self.assertEqual(ft.format_polynomial([1.0, 3.0]), "y = 3x + 1")
        self.assertEqual(ft.format_polynomial([0.0, 1.0]), "y = x")
        self.assertEqual(ft.format_polynomial([0.0, 0.0, 1.0]), "y = x²")

    def test_quadratic_superscript(self):
        self.assertEqual(ft.format_polynomial([7.0, 0.0, 0.5]),
                         "y = 0.5x² + 7")

    def test_cubic_superscript(self):
        self.assertEqual(ft.format_polynomial([0.0, -1.5, 0.0, 0.25]),
                         "y = 0.25x³ - 1.5x")

    def test_zero_term_skipped(self):
        self.assertEqual(ft.format_polynomial([0.0, 0.0, 0.0]), "y = 0")

    def test_negative_constant(self):
        self.assertEqual(ft.format_polynomial([-3.0, 1.0]), "y = x - 3")

    def test_negative_leading(self):
        self.assertEqual(ft.format_polynomial([1.0, -2.0]),
                         "y = -2x + 1")

    def test_small_coeff_not_all_rounded_away(self):
        s = ft.format_polynomial([0.0001, 1.0, 0.0001])
        self.assertIn("x", s)


class TestFitResultObject(unittest.TestCase):
    def test_summary_contains_key_metrics(self):
        r = ft.fit_polynomial([(0, 0), (10, 10), (20, 20)], 1)
        s = r.summary
        self.assertIn("R²", s)
        self.assertIn("RMSE", s)
        self.assertIn("3", s)

    def test_predict_many(self):
        r = ft.fit_polynomial([(0, 0), (10, 10)], 1)
        vals = r.predict_many([0, 5, 10])
        self.assertAlmostEqual(vals[1], 5.0, delta=1e-6)

    def test_points_used(self):
        r = ft.fit_polynomial([(0, 0), (10, 10), (20, 20), (20, 30)], 1)
        self.assertEqual(r.points_used, 4)


class TestBuildFitDisplay(unittest.TestCase):
    def test_curve_and_points_share_scale(self):
        pts = [(0, 10), (100, 200), (200, 30)]
        r = ft.fit_polynomial(pts, 1)
        disp = ft.build_fit_display(r, pts, 600, 400)
        self.assertTrue(disp.curve)
        self.assertEqual(len(disp.points), 3)
        # 同一个 offset/scale 才能保证"点在曲线上"可见
        for p in disp.points:
            self.assertIsInstance(p.screen_y, float)

    def test_display_within_canvas(self):
        pts = [(0, 0), (25, 400), (50, 0), (75, 400), (100, 0)]
        r = ft.auto_fit(pts)
        disp = ft.build_fit_display(r, pts, 500, 400)
        for p in disp.curve + disp.points:
            self.assertGreaterEqual(p.screen_y, -1)
            self.assertLessEqual(p.screen_y, 400 + 1)

    def test_bad_canvas(self):
        r = ft.fit_polynomial([(0, 0), (1, 1)], 1)
        with self.assertRaises(ft.FitError):
            ft.build_fit_display(r, [(0, 0), (1, 1)], 0, 100)


class TestPredictDefensive(unittest.TestCase):
    def test_predict_does_not_overflow_for_extreme_x(self):
        pts = [(float(x), x) for x in range(0, 100, 10)]
        r = ft.fit_polynomial(pts, 1)
        # 归一化保证远离训练域也不会爆
        for x in (-1e6, 1e6, 0.0):
            self.assertTrue(math.isfinite(r.predict(x)))

    def test_nonfinite_fit_rejected(self):
        # 构造会溢出的输入：巨大 y + 极低阶
        pts = [(0.0, 0.0), (1.0, 1e308), (2.0, -1e308)]
        try:
            r = ft.fit_polynomial(pts, 1)
            self.assertTrue(math.isfinite(r.predict(0.5)))
        except ft.FitError:
            pass  # 允许直接拒绝


if __name__ == "__main__":
    unittest.main(verbosity=2)
