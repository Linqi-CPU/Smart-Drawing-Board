"""计算进度（PageState.progress + bootstrap_band.on_progress）测试。

覆盖：
  A. PageState.set_progress / clear_progress 的取值规则
  B. to_dict() 是否把 progress 带出来（UI 轮询依赖它）
  C. bootstrap_band 的 on_progress 回调是否按预期节奏触发
  D. 回调抛异常不能毁掉 Bootstrap（进度是观察性的）
  E. 不传 on_progress 时行为与原来完全一致（回归）
  F. handle_band_advanced 跑完后 progress 必须清零
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(r'D:\桌面\新建文件夹')
for p in (str(ROOT), str(ROOT / 'core')):
    if p not in sys.path:
        sys.path.insert(0, p)

import session as ses           # noqa: E402
import band_advanced as ba      # noqa: E402
import server as srv            # noqa: E402


def cloud(n=60, seed=1):
    import random
    rng = random.Random(seed)
    return [(float(i), 50.0 + 1.5 * i + rng.uniform(-6, 6))
            for i in range(n)]


class TestPageStateProgress(unittest.TestCase):
    """A + B：进度快照的取值与暴露。"""

    def test_default_is_inactive(self):
        p = ses.PageState(session_id="t1")
        self.assertEqual(p.progress, {})
        self.assertFalse(p.to_dict().get("progress"))

    def test_progress_active_rule(self):
        p = ses.PageState(session_id="t2")
        p.set_progress(0, 100, phase="bootstrap")
        d = p.progress
        self.assertTrue(d["active"])
        self.assertEqual(d["done"], 0)
        self.assertEqual(d["total"], 100)
        self.assertEqual(d["phase"], "bootstrap")
        self.assertIn("at", d)

    def test_progress_inactive_when_done_ge_total(self):
        """done >= total 视为已结束，这是 UI 收起进度条的判据。"""
        p = ses.PageState(session_id="t3")
        p.set_progress(100, 100)
        self.assertFalse(p.progress["active"])

    def test_progress_inactive_when_total_zero(self):
        """total<=0 表示不知道总数，不该显示为"进行中"。"""
        p = ses.PageState(session_id="t4")
        p.set_progress(0, 0)
        self.assertFalse(p.progress["active"])

    def test_clear_progress(self):
        p = ses.PageState(session_id="t5")
        p.set_progress(50, 100, phase="bootstrap")
        p.clear_progress()
        d = p.progress
        self.assertFalse(d["active"])
        self.assertEqual((d["done"], d["total"]), (0, 0))
        self.assertEqual(d["phase"], "")

    def test_progress_in_to_dict(self):
        """UI 轮询读的是 to_dict()，progress 必须出现在里面。"""
        p = ses.PageState(session_id="t6")
        p.set_progress(3, 10, phase="segments")
        out = p.to_dict()
        self.assertIn("progress", out)
        self.assertEqual(out["progress"]["done"], 3)
        self.assertEqual(out["progress"]["total"], 10)
        self.assertEqual(out["progress"]["phase"], "segments")

    def test_negative_done_clamped(self):
        p = ses.PageState(session_id="t7")
        p.set_progress(-5, 10)
        self.assertEqual(p.progress["done"], 0)


class TestBootstrapProgress(unittest.TestCase):
    """C + D + E：bootstrap_band 的进度回调。"""

    def test_callback_fires_monotonic(self):
        seen = []
        ba.bootstrap_band(cloud(40), n_segments=3, degree=1,
                          n_boot=12, seed=7,
                          on_progress=lambda d, t: seen.append((d, t)))
        self.assertTrue(seen, "回调一次都没触发")
        # 第一次与最后一次必须覆盖首尾
        self.assertEqual(seen[0][0], 1)
        self.assertEqual(seen[-1], (12, 12))
        # done 单调不减，total 恒定
        dones = [d for d, _ in seen]
        self.assertEqual(dones, sorted(dones), "done 出现回退")
        self.assertTrue(all(t == 12 for _, t in seen))

    def test_callback_exception_does_not_break(self):
        """进度是观察性的：回调炸了也要把 Bootstrap 结果算完。"""
        def bad(d, t):
            raise RuntimeError("progress callback boom")

        r = ba.bootstrap_band(cloud(40), n_segments=3, degree=1,
                              n_boot=8, seed=7, on_progress=bad)
        self.assertEqual(r.n_boot, 8)
        self.assertTrue(r.lower)

    def test_no_callback_same_result(self):
        """不传回调时，结果必须与原来的实现逐位一致。"""
        a = ba.bootstrap_band(cloud(50), n_segments=3, degree=1,
                              n_boot=10, seed=42)
        b = ba.bootstrap_band(cloud(50), n_segments=3, degree=1,
                              n_boot=10, seed=42, on_progress=lambda d, t: None)
        self.assertEqual(list(a.lower), list(b.lower))
        self.assertEqual(list(a.upper), list(b.upper))
        self.assertEqual(list(a.center), list(b.center))
        self.assertEqual(a.backend, b.backend)
        self.assertEqual(a.n_fail, b.n_fail)

    def test_small_nboot_still_reports(self):
        """n_boot 很小也要报进度（report_every 至少为 1）。"""
        seen = []
        ba.bootstrap_band(cloud(20), n_segments=2, degree=1,
                          n_boot=1, seed=1,
                          on_progress=lambda d, t: seen.append((d, t)))
        self.assertEqual(seen[-1], (1, 1))

    def test_gpu_backend_reports_both_ends(self):
        """GPU 分支整批提交，只能报"开始"与"完成"两点。"""

        class FakeGPU:
            def fit_batch(self, samples, n_seg, deg):
                # 用 CPU 实现冒充 GPU —— 只为验证进度触发点
                import band_fit as bf
                return [bf.band_fit(s, n_segments=n_seg, degree=deg)
                        for s in samples]

        seen = []
        ba.bootstrap_band(cloud(30), n_segments=2, degree=1,
                          n_boot=6, seed=3, gpu=FakeGPU(),
                          on_progress=lambda d, t: seen.append((d, t)))
        self.assertEqual(seen, [(0, 6), (6, 6)],
                         "GPU 分支应报 已提交 与 已完成")


class TestServerAdvancedProgress(unittest.TestCase):
    """F：handle_band_advanced 结束后 progress 必须清零。"""

    def setUp(self):
        self.port = 8891
        srv._registry._sessions.clear() if hasattr(
            srv._registry, "_sessions") else None

    def _make_page(self, sid="prog_t"):
        page = srv._registry.get_or_create(sid)
        page.points = cloud(60)
        return page

    def test_progress_cleared_after_advanced(self):
        sid = "prog_adv"
        page = self._make_page(sid)
        body = {
            "session_id": sid,
            "n_segments": 4,
            "degree": 2,
            "n_boot": 6,
            "alpha": 0.05,
            "select_degree": False,
            "adaptive": True,
            "quantile_tau": 0.5,
            "use_gpu": False,
            "seed": 5,
        }
        r = srv.handle_band_advanced(body)
        self.assertTrue(r.get("ok"), r.get("error"))
        # 关键断言：算完后不能残留 active 进度
        self.assertFalse(page.progress.get("active"),
                         "计算结束仍显示进行中，UI 进度条不会收起")
        self.assertEqual(page.progress.get("done"), 0)
        self.assertEqual(page.progress.get("total"), 0)

    def test_progress_cleared_even_when_fit_fails(self):
        """band_fit 抛错时 progress 也必须清零。

        这条锁的是一个**真缺陷**：早前 clear_progress() 放在函数尾部，
        一旦末尾的 band_fit 抛 KernelError 就直接 raise，永远走不到
        清理那行 —— UI 进度条会一直转，用户以为还在算。
        现已改为 try/finally。
        """
        sid = "prog_fitfail"
        page = self._make_page(sid)
        body = {
            "session_id": sid,
            "n_segments": 4,
            "degree": 2,
            "n_boot": 5,
            "alpha": 0.05,
            "select_degree": False,
            "adaptive": False,
            "quantile_tau": 0.5,
            "use_gpu": False,
        }
        # 两个点的数据会让 band_fit 抛"无法构成上下界"
        page.points = [(0.0, 1.0), (1.0, 2.0)]
        with self.assertRaises(srv.KernelError):
            srv.handle_band_advanced(body)
        # 异常路径下也不能残留 active 进度
        self.assertFalse(page.progress.get("active"),
                         "计算失败后进度未清零，UI 进度条会一直转")

    def test_progress_cleared_when_boot_fails(self):
        """Bootstrap 失败被降级为 warning，进度同样要清零。"""
        sid = "prog_bootfail"
        page = self._make_page(sid)
        body = {
            "session_id": sid,
            "n_segments": 4,
            "degree": 2,
            "n_boot": 5,
            "alpha": 0.05,
            "select_degree": False,
            "adaptive": False,
            "quantile_tau": 0.5,
            "use_gpu": False,
        }
        # 4 个点刚好过 bootstrap 门槛但会让拟合并/分位失败，
        # 至少验证"不至于整体崩、且进度清零"
        page.points = cloud(8)
        r = srv.handle_band_advanced(body)
        self.assertTrue(r.get("ok"), r.get("error"))
        self.assertFalse(page.progress.get("active"),
                         "Bootstrap 失败后进度未清零")


if __name__ == "__main__":
    unittest.main(verbosity=2)
