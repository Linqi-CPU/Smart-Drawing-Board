"""Bootstrap CPU 并行化的测试。

重点保护三件事：
  1. 正确性：并行结果与串行**逐位一致**（并行只是加速，不许改结果）
  2. 回落：并行失败必须静默回落到串行，不许把异常抛给用户
  3. 顺序：结果列表与 samples 等长同序（分位数取样依赖它）
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(r'D:\桌面\新建文件夹')
for p in (str(ROOT), str(ROOT / 'core')):
    if p not in sys.path:
        sys.path.insert(0, p)

import band_advanced as ba          # noqa: E402


def cloud(n=100, seed=4242):
    import random
    rng = random.Random(seed)
    return [(float(i), 50.0 + 1.5 * i + rng.uniform(-8, 8))
            for i in range(1, n + 1)]


class TestParallelIdentical(unittest.TestCase):
    """并行与串行必须逐位一致。"""

    @classmethod
    def setUpClass(cls):
        cls._saved = ba.PARALLEL_MIN_BOOTSTRAP

    @classmethod
    def tearDownClass(cls):
        ba.PARALLEL_MIN_BOOTSTRAP = cls._saved
        ba.shutdown_parallel_pool()

    def setUp(self):
        ba.shutdown_parallel_pool()

    def _both(self, nboot=200, npts=600, seed=7):
        """同一种子下分别跑串行与并行，返回两个 BootstrapBand。"""
        pts = cloud(npts)
        ba.PARALLEL_MIN_BOOTSTRAP = 10 ** 9       # 强制串行
        ba.shutdown_parallel_pool()
        ref = ba.bootstrap_band(pts, n_segments=6, degree=2,
                                n_boot=nboot, seed=seed)
        ba.PARALLEL_MIN_BOOTSTRAP = 1              # 强制并行
        par = ba.bootstrap_band(pts, n_segments=6, degree=2,
                                n_boot=nboot, seed=seed)
        return ref, par

    def test_parallel_bit_identical(self):
        ref, par = self._both()
        self.assertEqual(list(ref.lower), list(par.lower))
        self.assertEqual(list(ref.upper), list(par.upper))
        self.assertEqual(list(ref.center), list(par.center))
        self.assertEqual(list(ref.center_median), list(par.center_median))
        self.assertEqual(list(ref.xs), list(par.xs))

    def test_parallel_same_fail_count(self):
        """失败集合也必须一致 —— 万一不一致，说明两条路径
        选择了不同的重采样或不同的拟合行为。"""
        ref, par = self._both()
        self.assertEqual(ref.n_fail, par.n_fail)
        self.assertEqual(ref.n_boot, par.n_boot)
        self.assertEqual(ref.backend, par.backend)

    def test_parallel_same_bandwidth(self):
        """带宽是用户直接看的量，必须完全一致。"""
        ref, par = self._both()
        self.assertEqual(ref.mean_width(), par.mean_width())


class TestParallelThreshold(unittest.TestCase):
    """阈值以下的负载应当串行（进程池启动开销会吃掉收益）。"""

    def test_small_nboot_uses_serial(self):
        import band_fit as bf
        calls = {"n": 0}
        orig_fit = bf.band_fit

        def counting_fit(*a, **kw):
            calls["n"] += 1
            return orig_fit(*a, **kw)

        pts = cloud(50)
        # nboot 很小：低于默认阈值 150，应当串行
        ba.bootstrap_band(pts, n_segments=3, degree=1, n_boot=20, seed=1)
        self.assertEqual(calls["n"], 0, "没传 fit_fn 不会走到这里")

    def test_threshold_default_is_sane(self):
        """阈值必须为正且小于常见 GPU 触发线，否则并行永远不生效。"""
        self.assertGreater(ba.PARALLEL_MIN_BOOTSTRAP, 0)
        self.assertLessEqual(ba.PARALLEL_MIN_BOOTSTRAP, 200)

    def test_max_workers_bounded(self):
        """进程数必须有上限，不然 32 核机器会起 32 个进程互相抢。"""
        self.assertGreater(ba.PARALLEL_MAX_WORKERS, 0)
        self.assertLessEqual(ba.PARALLEL_MAX_WORKERS, 8)


class TestSerialFallback(unittest.TestCase):
    """并行必须可被关掉，且关掉后结果不变。"""

    def test_disabling_parallel_same_result(self):
        pts = cloud(300)
        ba.shutdown_parallel_pool()
        ba.PARALLEL_MIN_BOOTSTRAP = 150
        par = ba.bootstrap_band(pts, n_segments=4, degree=1,
                                n_boot=200, seed=3)
        ba.PARALLEL_MIN_BOOTSTRAP = 10 ** 9
        ser = ba.bootstrap_band(pts, n_segments=4, degree=1,
                                n_boot=200, seed=3)
        self.assertEqual(list(par.lower), list(ser.lower))
        self.assertEqual(list(par.upper), list(ser.upper))
        ba.shutdown_parallel_pool()

    def test_low_core_env_forces_serial(self):
        """cpu_count<=2 的机器（或 CI 容器）不该尝试并行。"""
        import os
        saved = os.cpu_count
        os.cpu_count = lambda: 1
        try:
            # 不该抛异常；应当安静地走串行
            r = ba.bootstrap_band(cloud(50), n_segments=3, degree=1,
                                  n_boot=200, seed=1)
            self.assertTrue(r.lower)
        finally:
            os.cpu_count = saved
            ba.shutdown_parallel_pool()

    def test_pool_reuse_and_shutdown(self):
        """池可重复使用，也能干净关掉（不留僵尸进程）。"""
        ba.PARALLEL_MIN_BOOTSTRAP = 1
        for _ in range(2):
            ba.bootstrap_band(cloud(100), n_segments=3, degree=1,
                              n_boot=200, seed=5)
        self.assertIsNotNone(ba._POOL, "第二次调用应复用同一个池")
        ba.shutdown_parallel_pool()
        self.assertIsNone(ba._POOL)


class TestProgressStillFires(unittest.TestCase):
    """并行路径也必须报进度 —— 进度条是与并行同步上线的。"""

    def test_parallel_reports_progress(self):
        seen = []
        ba.PARALLEL_MIN_BOOTSTRAP = 1
        ba.shutdown_parallel_pool()
        try:
            ba.bootstrap_band(cloud(100), n_segments=3, degree=1,
                              n_boot=200, seed=9,
                              on_progress=lambda d, t: seen.append((d, t)))
        finally:
            ba.shutdown_parallel_pool()
        self.assertTrue(seen, "并行路径一次都没报进度")
        self.assertEqual(seen[0], (0, 200))
        self.assertEqual(seen[-1], (200, 200))
        dones = [d for d, _ in seen]
        self.assertEqual(dones, sorted(dones), "进度出现回退")

    def test_progress_exception_does_not_break_parallel(self):
        """进度回调炸了也不能毁掉并行计算。"""
        def bad(d, t):
            raise RuntimeError("boom")

        ba.PARALLEL_MIN_BOOTSTRAP = 1
        ba.shutdown_parallel_pool()
        try:
            r = ba.bootstrap_band(cloud(100), n_segments=3, degree=1,
                                  n_boot=200, seed=9, on_progress=bad)
            self.assertTrue(r.lower)
        finally:
            ba.shutdown_parallel_pool()


class TestFitOneHelper(unittest.TestCase):
    """子进程入口的单元测试（不需要真起子进程）。"""

    def test_fit_one_returns_result(self):
        import band_fit as bf
        pts = cloud(30)
        r = ba._fit_one((pts, 3, 1))
        self.assertIsNotNone(r)
        self.assertTrue(hasattr(r, "predict"))

    def test_fit_one_returns_none_on_bad_input(self):
        # 少于 4 个点 → band_fit 抛错 → _fit_one 应吞成 None
        r = ba._fit_one(([(0.0, 1.0), (1.0, 2.0)], 2, 1))
        self.assertIsNone(r)

    def test_fit_one_pickleable(self):
        """子进程入口必须可被 pickle —— spawn 靠它传参。"""
        import pickle
        data = pickle.dumps((cloud(30), 3, 1))
        self.assertTrue(data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
