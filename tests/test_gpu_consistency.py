"""GPU 后端与 CPU 路径的数值一致性测试。

背景：GPU 后端若与 band_fit 的语义不一致，bootstrap 会产出
**看起来正常但数值错误**的置信带。历史上真实发生过两次：

  1. 切分规则不一致：GPU 用「分段中位数」，band_fit 用「分段均值」。
     120 点上 8 个点归属不同，上下界偏差 0.4~1.4。
  2. 走势线语义不一致：GPU 拿「全量点最小二乘」冒充走势线，
     而 band_fit 的走势线是「上下界系数平均 + offset」。
     上下界偏差 0.000000、走势线偏差 1.26 —— 错得不对称，更难发现。

修复后的实测：走势线偏差 5.68e-14、上下界 1.14e-13（浮点级）。

本文件把一致性固化为回归测试。任一新算法上线前必须先过这里；
如果你的改动"应该"改变数值，那说明语义真的变了，
此时要同步更新 band_fit 而非只改这里。
"""

from __future__ import annotations

import random
import sys
import unittest

sys.path.insert(0, '.')

from core import band_advanced as ba
from core import band_fit as bf
from core import gpu_backend as gb

#: 一致性容差。GPU/CPU 的线性代数实现不同（torch.linalg.solve vs
#: 本项目高斯消元），累加顺序不同，结果差在最后几位是正常的。
#: 1e-9 对像素级应用足够宽松，同时仍能抓住上面那两类语义错误
#: （它们的量级是 1e-1 ~ 1e0）。
GPU_CPU_TOL = 1e-9


def _make_points(n=120, seed=11):
    rng = random.Random(seed)
    return [(float(i), 50.0 + 1.5 * i + rng.uniform(-8, 8))
            for i in range(1, n + 1)]


def _bootstrap_samples(points, n=8, seed=11):
    rng = random.Random(seed)
    return [[points[rng.randrange(len(points))] for _ in range(len(points))]
            for _ in range(n)]


class TestSegmentationMatches(unittest.TestCase):
    """分段与分组规则必须与 band_fit 逐点一致。"""

    def test_make_segments_identical(self):
        xs = [float(p[0]) for p in _make_points()]
        a = gb._make_segments(xs, 4)
        b = [tuple(s) for s in bf.make_segments(xs, 4)]
        self.assertEqual([tuple(x) for x in a], b)

    def test_seg_index_matches_segment_of(self):
        xs = [float(p[0]) for p in _make_points()]
        segs = bf.make_segments(xs, 4)
        for x in xs:
            self.assertEqual(gb._seg_index(x, segs), bf.segment_of(x, segs))

    def test_split_upper_lower_identical(self):
        """这是历史上第一个真实 bug 的回归测试。"""
        pts = _make_points()
        xs = [p[0] for p in pts]
        segs = bf.make_segments(xs, 4)

        gup, glo = gb._split_upper_lower(pts, segs)

        # band_fit 内部的切分逻辑（照抄其主体）
        means, _ = bf.segment_means(xs, [p[1] for p in pts], segs)
        cup, clo = [], []
        for x, y in pts:
            m = means[bf.segment_of(x, segs)]
            (cup if y >= m else clo).append((x, y))

        self.assertEqual(len(gup), len(cup), "上组点数不一致")
        self.assertEqual(len(glo), len(clo), "下组点数不一致")
        self.assertEqual(sorted(map(tuple, gup)), sorted(map(tuple, cup)))
        self.assertEqual(sorted(map(tuple, glo)), sorted(map(tuple, clo)))

    def test_split_handles_odd_segment_count(self):
        pts = _make_points(80)
        xs = [p[0] for p in pts]
        for n in (2, 3, 5):
            segs = bf.make_segments(xs, n)
            gup, glo = gb._split_upper_lower(pts, segs)
            means, _ = bf.segment_means(xs, [p[1] for p in pts], segs)
            cup, clo = [], []
            for x, y in pts:
                m = means[bf.segment_of(x, segs)]
                (cup if y >= m else clo).append((x, y))
            self.assertEqual(sorted(map(tuple, gup)), sorted(map(tuple, cup)))


@unittest.skipUnless(gb.gpu_available(), "CUDA/torch 不可用")
class TestGpuCpuConsistency(unittest.TestCase):
    """GPU 后端必须与 band_fit 在浮点级别一致。"""

    def test_single_sample_matches_band_fit(self):
        be = gb.GpuFitBackend()
        samples = _bootstrap_samples(_make_points(), n=6, seed=21)
        got = be.fit_batch(samples, n_segments=4, degree=2)
        self.assertEqual(len(got), len(samples))

        worst_trend = worst_band = 0.0
        for s, g in zip(samples, got):
            self.assertIsNotNone(g)
            c = bf.band_fit(s, n_segments=4, degree=2)
            for x in (1.0, 33.0, 66.0, 99.0, 120.0):
                worst_trend = max(worst_trend, abs(g.predict(x) - c.predict(x)))
                worst_band = max(worst_band,
                                 abs(g.upper.predict(x) - c.upper.predict(x)),
                                 abs(g.lower.predict(x) - c.lower.predict(x)))
        self.assertLess(worst_trend, GPU_CPU_TOL,
                        f"走势线偏差 {worst_trend:.3e} 超出 {GPU_CPU_TOL:.0e}")
        self.assertLess(worst_band, GPU_CPU_TOL,
                        f"上下界偏差 {worst_band:.3e} 超出 {GPU_CPU_TOL:.0e}")

    def test_trend_is_reconciled_not_global_fit(self):
        """守护第二个真实 bug：走势线必须是上下界调和，不是全量拟合。

        若有人把 _build_result 改回"对全量点拟合一次当走势线"，
        本测试会失败（实测该错误模式的偏差约 1.26，远大于容差）。
        """
        be = gb.GpuFitBackend()
        pts = _make_points()
        g = be.fit_batch([pts], n_segments=4, degree=2)[0]
        c = bf.band_fit(pts, n_segments=4, degree=2)

        # 走势线应远离"全量点最小二乘"（两者是不同的数学对象）
        global_fit = __import__("core.fitting", fromlist=["fit_polynomial"])
        import core.fitting as ftmod
        gf = ftmod.fit_polynomial(pts, 2)
        diff_from_global = abs(g.predict(100.0) - gf.predict(100.0))
        diff_from_cpu = abs(g.predict(100.0) - c.predict(100.0))
        self.assertLess(diff_from_cpu, GPU_CPU_TOL)
        self.assertGreater(diff_from_global, GPU_CPU_TOL,
                           "走势线退化成全量点拟合了")

    def test_various_degrees(self):
        be = gb.GpuFitBackend()
        pts = _make_points(150, seed=33)
        samples = _bootstrap_samples(pts, n=4, seed=7)
        for degree in (1, 2, 3):
            got = be.fit_batch(samples, n_segments=4, degree=degree)
            worst = 0.0
            for s, g in zip(samples, got):
                if g is None:
                    self.skipTest("该 degree 下 GPU 分支回退")
                c = bf.band_fit(s, n_segments=4, degree=degree)
                for x in (10.0, 75.0, 140.0):
                    worst = max(worst, abs(g.predict(x) - c.predict(x)))
            self.assertLess(worst, GPU_CPU_TOL,
                            f"degree={degree} 偏差 {worst:.3e}")

    def test_bootstrap_gpu_matches_cpu(self):
        """端到端：bootstrap_band 走 GPU 与走 CPU 应一致。"""
        pts = _make_points(100, seed=5)
        be = gb.GpuFitBackend()
        bg = ba.bootstrap_band(pts, n_segments=4, degree=1, n_boot=20,
                               x_from=1, x_to=100, seed=5, gpu=be)
        bc = ba.bootstrap_band(pts, n_segments=4, degree=1, n_boot=20,
                               x_from=1, x_to=100, seed=5)
        self.assertEqual(bg.backend, "gpu")
        self.assertEqual(bc.backend, "cpu")
        self.assertEqual(len(bg.xs), len(bc.xs))
        for a, b in zip(zip(bg.lower, bg.upper), zip(bc.lower, bc.upper)):
            self.assertAlmostEqual(a[0], b[0], places=6)
            self.assertAlmostEqual(a[1], b[1], places=6)


class TestGpuFallback(unittest.TestCase):
    """任何后端异常都必须静默回落，且结果自洽。

    这些测试不依赖 GPU：后端是 mock 的，只验证回落逻辑本身。
    """

    def _pts(self):
        return _make_points(60, seed=9)

    def test_backend_raises(self):
        class Broken:
            def fit_batch(self, *a, **k):
                raise RuntimeError("模拟 GPU 崩溃")

        b = ba.bootstrap_band(self._pts(), n_segments=4, degree=1,
                              n_boot=10, seed=5, gpu=Broken())
        self.assertEqual(b.backend, "cpu")
        self.assertTrue(all(l <= u for l, u in zip(b.lower, b.upper)))

    def test_backend_returns_wrong_type(self):
        class WrongType:
            def fit_batch(self, samples, *a, **k):
                return [(1, 2, 3)] * len(samples)

        b = ba.bootstrap_band(self._pts(), n_segments=4, degree=1,
                              n_boot=10, seed=5, gpu=WrongType())
        self.assertEqual(b.backend, "cpu")
        self.assertTrue(all(l <= u for l, u in zip(b.lower, b.upper)))

    def test_backend_returns_short(self):
        b = ba.bootstrap_band(self._pts(), n_segments=4, degree=1,
                              n_boot=10, seed=5, gpu=type("S", (), {
                                  "fit_batch": lambda s, sm, n, d: []})())
        self.assertEqual(b.backend, "cpu")

    def test_backend_returns_all_none(self):
        b = ba.bootstrap_band(self._pts(), n_segments=4, degree=1,
                              n_boot=10, seed=5, gpu=type("N", (), {
                                  "fit_batch": lambda s, sm, n, d:
                                  [None] * len(s)})())
        self.assertEqual(b.backend, "cpu")
        self.assertTrue(all(l <= u for l, u in zip(b.lower, b.upper)))

    def test_backend_returns_mixed(self):
        """部分成功也应回落整体走 CPU，避免两种后端混用。"""
        class Mixed:
            def fit_batch(self, samples, n_segments, degree):
                out = [None] * len(samples)
                if samples:
                    out[0] = _FakeResult()
                return out

        b = ba.bootstrap_band(self._pts(), n_segments=4, degree=1,
                              n_boot=10, seed=5, gpu=Mixed())
        self.assertEqual(b.backend, "cpu")


class _FakeResult:
    """带正确接口但值随意的对象，用于测试 _usable_result 的类型判定。"""

    class _C:
        def predict(self, x):
            return float(x)

    upper = lower = _C()

    def predict(self, x):
        return float(x)


class TestUsableResult(unittest.TestCase):
    """_usable_result 必须挡住类型不符的返回值。"""

    def test_accepts_band_fit_result(self):
        pts = _make_points(30, seed=1)
        self.assertTrue(ba._usable_result(bf.band_fit(pts, n_segments=3,
                                                      degree=1)))

    def test_accepts_gpu_result(self):
        r = _FakeResult()
        self.assertTrue(ba._usable_result(r))

    def test_rejects_tuple(self):
        self.assertFalse(ba._usable_result((1, 2, 3)))

    def test_rejects_none(self):
        self.assertFalse(ba._usable_result(None))

    def test_rejects_missing_lower(self):
        class NoLower:
            class _C:
                def predict(self, x):
                    return 0.0
            upper = _C()

            def predict(self, x):
                return 0.0

        self.assertFalse(ba._usable_result(NoLower()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
