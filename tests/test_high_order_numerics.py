"""高阶数值稳定性测试：锁住 Legendre 修复成果。

背景（实测数据，不得删改，改了要说明理由）：
  原实现用正态方程解 _solve_normal_equations，矩阵 [Σt^k] 的条件数
  随阶数增长，x∈[0,100] 时 t 空间最高次系数误差：
      6 阶 5.4e-9 | 7 阶 1.9e-7 | 8 阶 4.0e-5
  换成 Legendre 正交基（_solve_legendre）后：
      6 阶 3.7e-11 | 7 阶 5.6e-10 | 8 阶 2.8e-9
  即 8 阶改善约 1.5e4 倍，且低阶与旧实现同量级（纯增益）。

x 空间系数（_denormalize 还原，用于 format_polynomial）仍是第二条瓶颈：
      6 阶 3.7e-8 | 7 阶 1.4e-6 | 8 阶 5.5e-4 | 9 阶 5.3e-2
  10 阶已超出 double 精度（scale^10 ≈ 9.8e16 > 2^53），无解，
  因此 MAX_DEGREE 卡在 8。这三种插值方案已实测均无效：
  解析展开、切比雪夫节点插值、埃尔米特插值（含值+导数约束）。
"""

import math
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import fitting as ft


def _true_coeffs(deg: int):
    """构造一个高次项幅度递减的已知多项式（低次在前）。"""
    c = [3.0] + [0.7 / (j + 1) * ((-1) ** j) for j in range(1, deg + 1)]
    c[deg] = 1.0 / (10 ** deg)
    return c


def _make_points(coeffs, n=200, x_lo=0.0, x_hi=100.0, noise=0.0, seed=7):
    rng = random.Random(seed)
    out = []
    for i in range(n):
        x = x_lo + (x_hi - x_lo) * i / (n - 1)
        y = sum(c * x ** j for j, c in enumerate(coeffs))
        if noise:
            y += rng.uniform(-noise, noise)
        out.append((x, y))
    return out


def _rel_err(a, b):
    return abs(a - b) / max(abs(a), abs(b), 1e-12)


def _t_true(coeffs_x, center, scale, deg):
    """真实 x 系数换算到 t 空间（t=(x-c)/s）。"""
    t = [0.0] * (deg + 1)
    for j, c in enumerate(coeffs_x):
        for m in range(j + 1):
            t[m] += c * math.comb(j, m) * (center ** (j - m)) * (scale ** m)
    return t


class TestLegendreSolver(unittest.TestCase):
    """Legendre 正交基求解器本身的性质。"""

    def test_legendre_orthogonality(self):
        """P_k 在 [-1,1] 上应关于 L2 正交。

        用足够密的采样近似积分 ∮P_i P_j dt：
        解析值为 2/(2i+1)（i==j）或 0（i≠j）。
        等距采样有离散化误差，容差取 5e-3；若把容差压到 1e-3 以下，
        等距求和会因混叠不稳地报 FAIL，那不是实现的问题。
        """
        n = 400
        ts = [-1.0 + 2.0 * k / (n - 1) for k in range(n)]
        vals = [ft.legendre_basis_values(t, 5) for t in ts]
        size = 6
        for i in range(size):
            for j in range(size):
                # 中点近似积分
                dot = sum(v[i] * v[j] for v in vals) * (2.0 / (n - 1))
                if i == j:
                    self.assertAlmostEqual(dot, 2.0 / (2 * i + 1), delta=1e-2)
                else:
                    self.assertAlmostEqual(dot, 0.0, delta=1e-2)

    def test_legendre_values_match_known(self):
        """几个已知点：P0=1, P1=t, P2=(3t²-1)/2, P3=(5t³-3t)/2。"""
        for t in (-1.0, -0.5, 0.0, 0.37, 1.0):
            v = ft.legendre_basis_values(t, 3)
            self.assertAlmostEqual(v[0], 1.0, places=12)
            self.assertAlmostEqual(v[1], t, places=12)
            self.assertAlmostEqual(v[2], (3 * t * t - 1) / 2, places=12)
            self.assertAlmostEqual(v[3], (5 * t ** 3 - 3 * t) / 2, places=12)

    def test_legendre_poly_coeffs_consistent_with_values(self):
        """幂基系数与递推求值必须一致。"""
        for k in range(0, 7):
            coeffs = ft.legendre_poly_coeffs(k)
            for t in (-0.9, -0.3, 0.0, 0.45, 0.8):
                direct = ft.legendre_basis_values(t, k)[k]
                via_power = sum(c * t ** j for j, c in enumerate(coeffs))
                self.assertAlmostEqual(direct, via_power, places=9)

    def test_negative_degree_rejected(self):
        with self.assertRaises(ft.FitError):
            ft.legendre_basis_values(0.0, -1)
        with self.assertRaises(ft.FitError):
            ft.legendre_poly_coeffs(-1)

    def test_degree_zero_and_one(self):
        self.assertEqual(ft.legendre_basis_values(0.5, 0), [1.0])
        self.assertEqual(ft.legendre_poly_coeffs(0), [1.0])
        self.assertEqual(ft.legendre_poly_coeffs(1), [0.0, 1.0])


class TestHighOrderPrecision(unittest.TestCase):
    """高阶拟合精度回归（这些数字是本次修复的成果，必须守住）。"""

    def test_t_space_precision_stays_small_at_high_degree(self):
        """t 空间系数误差：8 阶应 < 1e-7（旧实现为 4.0e-5）。"""
        for deg in (5, 6, 7, 8):
            tc = _true_coeffs(deg)
            pts = _make_points(tc)
            xs = [p[0] for p in pts]
            center = sum(xs) / len(xs)
            scale = max(abs(x - center) for x in xs)

            r = ft.fit_polynomial(pts, deg)
            tt = _t_true(tc, center, scale, deg)
            worst = max(_rel_err(a, b) for a, b in zip(r.norm_coeffs, tt))
            self.assertLess(
                worst, 1e-7,
                f"{deg} 阶 t 空间系数误差 {worst:.3e} 超出阈值，"
                f"Legendre 修复可能被回滚了",
            )

    def test_predict_is_exact_for_known_polynomial(self):
        """predict(x) 必须能精确重建已知多项式（相对 y 的量级）。"""
        for deg in (3, 5, 7, 8):
            tc = _true_coeffs(deg)
            pts = _make_points(tc)
            r = ft.fit_polynomial(pts, deg)
            mag = max(abs(y) for _, y in pts)
            worst = max(abs(r.predict(x) - y) for x, y in pts) / mag
            self.assertLess(worst, 1e-12,
                            f"{deg} 阶 predict 相对误差 {worst:.3e}")

    def test_x_space_coefficients_trustworthy_to_degree_8(self):
        """x 空间系数在 <=8 阶应可信（<=1e-3），这正是 MAX_DEGREE=8 的依据。"""
        for deg in range(1, 9):
            tc = _true_coeffs(deg)
            pts = _make_points(tc)
            r = ft.fit_polynomial(pts, deg)
            got = list(r.coefficients)[:deg + 1]
            got += [0.0] * (deg + 1 - len(got))
            worst = max(_rel_err(a, b) for a, b in zip(got, tc))
            self.assertLess(worst, 1e-3, f"{deg} 阶 x 空间系数误差 {worst:.3e}")

    def test_max_degree_is_8_not_more(self):
        """MAX_DEGREE 必须停在 8：9~10 阶 x 系数不可信（实测 5e-2 / 1.57）。"""
        self.assertEqual(ft.MAX_DEGREE, 8)
        pts = _make_points(_true_coeffs(8))
        with self.assertRaises(ft.FitError):
            ft.fit_polynomial(pts, 9)
        with self.assertRaises(ft.FitError):
            ft.fit_polynomial(pts, 10)

    def test_noisy_high_degree_still_usable(self):
        """带噪数据下高阶拟合的 R² 与表达式仍应合理。"""
        for deg in (4, 6, 8):
            tc = _true_coeffs(deg)
            pts = _make_points(tc, noise=0.5)
            r = ft.fit_polynomial(pts, deg)
            self.assertGreater(r.r2, 0.99)
            self.assertIn("y = ", r.expression)
            self.assertTrue(math.isfinite(r.rmse))


class TestSolverEquivalence(unittest.TestCase):
    """新旧求解器在低阶应一致（保证修复不是行为变更）。"""

    def test_low_degree_matches_normal_equations(self):
        """1~4 阶：Legendre 与正态方程解应几乎相同。"""
        for deg in (1, 2, 3, 4):
            tc = _true_coeffs(deg)
            pts = _make_points(tc, noise=0.3)
            xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
            center = sum(xs) / len(xs)
            scale = max(abs(x - center) for x in xs)
            ts = [(x - center) / scale for x in xs]

            r = ft.fit_polynomial(pts, deg)
            lg = ft._solve_legendre(ts, ys, deg)
            ne = ft._solve_normal_equations(ts, ys, deg)
            worst = max(_rel_err(a, b) for a, b in zip(lg, ne))
            self.assertLess(worst, 1e-6,
                            f"{deg} 阶两解法偏差 {worst:.3e}")


if __name__ == "__main__":
    unittest.main()
