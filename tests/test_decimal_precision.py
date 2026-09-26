"""高精度（decimal）系数还原的回归测试。

背景
----
``_denormalize`` 把 t 空间系数还原成 x 空间系数，含 ``/s**j``。
double 只有约 15.95 位十进制精度，scale=49、8 阶时 ``scale^8≈3.3e13``
已逼近尾数极限，实测该步骤自身引入的最大相对误差：

    degree   scale^deg    double 还原误差
      5      2.8e+08      3.67e-12
      6      1.4e+10      2.44e-11
      7      6.8e+11      3.48e-10
      8      3.3e+13      4.78e-09

decimal 版用 Fraction 级精度的有理展开，上表四档全部降到 0
（相对 Fraction 基准）。代价是实测慢约 5.8 倍（8 阶 1000 次：
double 0.010s / decimal 0.056s），因此低阶仍走 double。

**decimal 的边界（重要，改了要重测）**
  它只消除"还原"这一步的舍入，**不能**恢复拟合阶段已丢失的信息。
  早前"decimal 能修 8 阶 5.5e-4、10 阶 1.57"的说法经实测不成立：
  那两项误差的源头在 t 空间系数本身（Legendre 求解后转 double
  的表示误差），不在 _denormalize。治本的是 Legendre 基。
  当前误差量级见 tests/test_high_order_numerics.py。
"""

from __future__ import annotations

import math
import random
import sys
import unittest
from fractions import Fraction

sys.path.insert(0, '.')

from core import fitting as ft


def _denorm_reference(b, c, s):
    """用 Fraction 算的精确基准（不经过 double）。"""
    d = len(b) - 1
    CF = Fraction(float(c))
    SF = Fraction(float(s))
    out = []
    for m in range(d + 1):
        acc = Fraction(0)
        for j in range(m, d + 1):
            acc += (Fraction(b[j]) * math.comb(j, m)
                    * ((-CF) ** (j - m)) / (SF ** j))
        out.append(acc)
    return out


class TestDecimalAvailable(unittest.TestCase):
    def test_probe_true_on_cpython(self):
        # decimal 是标准库，正常 CPython 下必然可用
        self.assertTrue(ft.high_precision_available())

    def test_probe_survives_reset(self):
        ft._reset_hp_cache()
        self.assertTrue(ft.high_precision_available())

    def test_probe_respects_forced_false(self):
        saved = ft._HP_CACHE
        try:
            ft._HP_CACHE = False
            self.assertFalse(ft.high_precision_available())
            # 关键：缓存为 False 后不得再探测（否则 reset 无意义）
            self.assertFalse(ft.high_precision_available())
        finally:
            ft._HP_CACHE = saved


class TestDecimalMatchesFloat(unittest.TestCase):
    """数学等价性：两个实现对同一输入必须给出同一数学结果。"""

    def test_same_math_low_degree(self):
        # 3 阶 / center=5 / scale=5，两端都能精确表示，结果应几乎一致
        b = [1.0, 2.0, 3.0, 0.5]
        c, s = 5.0, 5.0
        a_f = ft._denormalize_float(b, c, s)
        a_d = ft._denormalize_decimal(b, c, s)
        for x, y in zip(a_f, a_d):
            self.assertAlmostEqual(x, y, places=9)

    def test_zero_scale_raises(self):
        # scale=0 时 double 版会跳过（sj==0），decimal 版显式报错
        with self.assertRaises(ZeroDivisionError):
            ft._denormalize_decimal([1.0, 2.0], 3.0, 0.0)


class TestDecimalBeatsFloat(unittest.TestCase):
    """核心收益：decimal 消除还原步骤自身的舍入误差。"""

    CASES = [(5, 49.0), (6, 49.0), (7, 49.0), (8, 49.0)]

    def _run_case(self, degree, scale):
        rng = random.Random(1000 + degree)
        center = scale
        xs = [center - scale * (1 - 2.0 * k / 20) for k in range(21)]
        b = [round(rng.uniform(-2, 2), 6) for _ in range(degree + 1)]
        truth = _denorm_reference(b, center, scale)

        a_f = ft._denormalize_float(b, center, scale)
        a_d = ft._denormalize_decimal(b, center, scale)

        def rel(a, t):
            return max(
                (abs(ai) if abs(float(ti)) < 1e-12
                 else abs(ai - float(ti)) / abs(float(ti)))
                for ai, ti in zip(a, t)
            )
        return rel(a_f, truth), rel(a_d, truth)

    def test_decimal_not_worse_than_float(self):
        for degree, scale in self.CASES:
            with self.subTest(degree=degree):
                ef, ed = self._run_case(degree, scale)
                self.assertLessEqual(
                    ed, ef * 1.0000001,
                    f"degree={degree}: decimal ({ed:.3e}) 不应差于 "
                    f"double ({ef:.3e})",
                )

    def test_float_introduces_error_at_high_degree(self):
        # 反向守护：确认 double 版在高阶确实有可测误差
        # （若哪天这个断言失败，说明基准或实现变了，需重估 decimal 的必要性）
        ef, ed = self._run_case(8, 49.0)
        self.assertGreater(ef, 0.0, "8 阶下 double 版应有非零还原误差")
        self.assertLessEqual(ed, ef)


class TestDispatch(unittest.TestCase):
    """_denormalize 的分派逻辑。"""

    def test_low_degree_uses_float(self):
        # degree <= DECIMAL_MIN_DEGREE 时应直接给 float 路径的结果
        b = [1.0, 2.0, 3.0]
        self.assertEqual(
            ft._denormalize(b, 5.0, 5.0),
            ft._denormalize_float(b, 5.0, 5.0),
        )

    def test_high_degree_dispatches_to_decimal(self):
        b = [round(random.Random(1).random(), 6) for _ in range(9)]
        got = ft._denormalize(b, 49.0, 49.0)
        self.assertEqual(got, ft._denormalize_decimal(b, 49.0, 49.0))

    def test_falls_back_when_decimal_unavailable(self):
        b = [round(random.Random(2).random(), 6) for _ in range(9)]
        saved = ft._HP_CACHE
        try:
            ft._HP_CACHE = False
            self.assertEqual(
                ft._denormalize(b, 49.0, 49.0),
                ft._denormalize_float(b, 49.0, 49.0),
            )
        finally:
            ft._HP_CACHE = saved

    def test_empty_coeffs(self):
        self.assertEqual(ft._denormalize([], 1.0, 1.0), [])

    def test_fit_polynomial_still_works(self):
        # 端到端：高精度路径不能把主流程弄坏
        xs = list(range(0, 51, 5))
        ys = [3.0 + 0.7 * x - 0.01 * x * x for x in xs]
        fr = ft.fit_polynomial(list(zip(xs, ys)), degree=7)
        self.assertTrue(all(math.isfinite(c) for c in fr.coefficients))
        self.assertGreater(fr.r2, 0.99)


if __name__ == "__main__":
    unittest.main(verbosity=2)
