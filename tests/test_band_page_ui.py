"""Band 页面 GUI 冒烟测试：真实创建窗口，验证「进阶算法」这条 UI 链路。

重点不是"能不能画出来"，而是**这次新接的东西真的能用**：
  - 下拉框里出现「进阶算法」
  - 选中它时进阶参数区展开，选回经典时收起
  - 六个进阶控件都存在、默认值正确（尤其 GPU 默认关）
  - 进阶摘要能渲染（用内核真实返回的 advanced 结构）
  - 三个经典模式的结果渲染不受影响（零回归）

内核用真的起，client 走 HTTP，与运行时路径一致。
"""

from __future__ import annotations

import json
import os
import random
import sys
import threading
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "core"))

os.environ.setdefault("DRAWING_FUNCTIONS_DIR", str(ROOT / "functions"))

import tkinter as tk                                     # noqa: E402

from core import client as cl                            # noqa: E402
import server as sv                                      # noqa: E402


class _MiniServer:
    """跑一个真实的线程式内核，跑在随机端口。"""

    def __init__(self):
        self.srv = sv.ThreadingHTTPServer(("127.0.0.1", 0), sv.Handler)
        self.port = self.srv.server_address[1]
        self.t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.t.start()

    def stop(self):
        try:
            self.srv.shutdown()
            self.srv.server_close()
        except Exception:
            pass

    def post(self, path, payload):
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())


class TestBandPageAdvancedUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        CLASS_NAME = "TestBandPageAdvancedUI"
        cls.mini = _MiniServer()
        # KernelClient 收 host/port，不是完整 URL ——
        # 传完整 URL 会拼出 "http://http://..."。
        cls.client = cl.KernelClient("127.0.0.1", cls.mini.port)

        # 注册 + 存点，让页面状态与内核一致
        rng = random.Random(4242)
        cls.pts = [(float(i), 50.0 + 1.5 * i + rng.uniform(-8, 8))
                   for i in range(1, 101)]
        cls.mini.post("/api/register", {"session_id": CLASS_NAME})
        cls.mini.post("/api/save_points",
                      {"session_id": CLASS_NAME,
                       "points": [list(p) for p in cls.pts]})

        cls.root = tk.Tk()
        cls.root.withdraw()          # 不抢焦点，CI 上也能跑
        from edit.page import BandPage
        cls.page = BandPage(cls.root, CLASS_NAME, cls.client, "band")
        cls.root.update()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except Exception:
            pass
        cls.mini.stop()

    def pump(self, ms=200):
        deadline = time.time() + ms / 1000.0
        while time.time() < deadline:
            self.root.update()
            time.sleep(0.01)

    def select_algo(self, key):
        from edit.page import ALGO_LABELS
        self.page.algo_var.set(ALGO_LABELS[key])
        self.page._sync_adv_visibility()
        self.root.update()

    def adv_text(self):
        return self.page.result_text.get("1.0", tk.END)

    # ------------------------------------------------------------------
    # 1. 下拉框与联动
    # ------------------------------------------------------------------
    def test_algo_list_contains_advanced(self):
        from edit.page import ALGO_DISPLAY, ALGO_LABELS
        self.assertIn("进阶算法", ALGO_DISPLAY)
        self.assertEqual(ALGO_LABELS["advanced"], "进阶算法")
        self.assertIn("advanced", ALGO_LABELS)

    def test_advanced_key_roundtrip(self):
        from edit.page import _algo_key, ALGO_LABELS
        self.assertEqual(_algo_key(ALGO_LABELS["advanced"]), "advanced")
        self.assertEqual(_algo_key("advanced"), "advanced")
        self.assertEqual(_algo_key("不存在的值"), "classic")

    def test_adv_frame_hidden_by_default(self):
        """默认 classic 时进阶区必须收起。"""
        self.select_algo("classic")
        self.assertFalse(self._adv_visible(), "默认状态进阶区应收起")

    def _adv_visible(self):
        """进阶区是否展开。

        不能用 winfo_ismapped()：测试环境把主窗口 withdraw() 了，
        所有子控件都 unmapped，判出来永远是 False。
        winfo_manager() 返回 geometry manager 的名字（'grid' / ''），
        反映的是"是否已被 grid 管理"，正是 grid_remove() 会抹掉的东西。
        """
        return bool(str(self.page.adv_frame.winfo_manager()))

    def test_adv_frame_shown_when_advanced(self):
        self.select_algo("advanced")
        self.assertTrue(self._adv_visible(),
                        "切到进阶算法时进阶区应展开")
        self.assertTrue(str(self.page.adv_frame.winfo_manager()) == "grid",
                        "进阶区应被 grid 管理（= 可见）")

    def test_adv_frame_hidden_again(self):
        self.select_algo("advanced")
        self.assertTrue(self._adv_visible())
        self.select_algo("compare")
        self.assertFalse(self._adv_visible(), "切回对比模式应收起")

    # ------------------------------------------------------------------
    # 2. 控件存在性与默认值
    # ------------------------------------------------------------------
    def test_adv_widgets_exist(self):
        for name in ("sp_boot", "sp_alpha", "sp_tau",
                     "var_select_deg", "var_adaptive", "var_use_gpu"):
            self.assertTrue(hasattr(self.page, name), f"缺少控件 {name}")

    def test_adv_defaults_conservative(self):
        """所有进阶开关默认必须关闭。"""
        self.assertEqual(int(self.page.sp_boot.get()), 0)
        self.assertAlmostEqual(float(self.page.sp_alpha.get()), 0.05, places=6)
        self.assertAlmostEqual(float(self.page.sp_tau.get()), 0.5, places=6)
        self.assertFalse(self.page.var_select_deg.get())
        self.assertFalse(self.page.var_adaptive.get())
        self.assertFalse(self.page.var_use_gpu.get(),
                         "GPU 必须默认关闭（零依赖承诺）")

    def test_params_include_adv_keys(self):
        """_params() 必须把进阶参数一起吐出来，否则 worker 取不到。"""
        p = self.page._params()
        for k in ("n_boot", "alpha", "select_degree", "adaptive",
                  "quantile_tau", "use_gpu", "seed"):
            self.assertIn(k, p, f"_params() 缺少 {k}")
        self.assertFalse(p["use_gpu"])

    # ------------------------------------------------------------------
    # 3. 端到端：真的跑一次进阶计算，验证摘要渲染
    # ------------------------------------------------------------------
    def test_advanced_calc_end_to_end(self):
        self.select_algo("advanced")
        self.page.var_select_deg.set(True)
        self.page.sp_boot.set(20)

        res = self.page.client.band_fit_advanced(
            self.page.session_id,
            n_segments=8, degree=2, n_boot=20, seed=7,
            select_degree=True)

        self.assertEqual(res.get("algorithm"), "advanced")
        adv = res.get("advanced") or {}
        self.assertTrue(adv.get("selection"), "应返回选阶结果")
        self.assertEqual((adv.get("bootstrap") or {}).get("n_boot"), 20)

        # 走渲染路径（这是上一轮被打断、这一轮补上的部分）
        self.page._on_calc_done(res, self.page._params(), "advanced")
        self.pump(120)
        text = self.adv_text()

        self.assertIn("进阶分析", text, "结果区应出现进阶分析小节")
        self.assertIn("自动选阶", text)
        self.assertIn("Bootstrap", text)
        self.assertIn("GPU: 未启用", text, "默认应显示未启用")
        self.assertIn("分段策略", text)

    def test_advanced_summary_does_not_break_classic(self):
        """经典结果渲染不能因为新增参数受影响。"""
        self.select_algo("classic")
        res = self.page.client.band_fit(self.page.session_id,
                                        n_segments=8, degree=1)
        self.page._on_calc_done(res, self.page._params(), "classic")
        self.pump(80)
        text = self.adv_text()
        self.assertIn("整体走势", text)
        self.assertIn("离散程度", text)
        self.assertNotIn("进阶分析", text,
                         "经典模式不该出现进阶小节")

    def test_summary_survives_bad_payload(self):
        """advanced 内容畸形时不能把整块结果吞掉。"""
        res = {"expression": "y = 1x + 2", "n_points": 10, "n_segments": 4,
               "degree_upper": 1, "degree_lower": 1, "offset": 0.0,
               "r2": 0.9, "rmse": 0.1, "mean_abs_dev": 0.1,
               "upper": {"expression": "a"}, "lower": {"expression": "b"},
               "scatter": {"description": "d", "mean_d": 1.0,
                           "min_d": 0.5, "max_d": 1.5, "tail_ratio": 1.0},
               "advanced": {"warnings": ["模拟警告"], "effective_degree": 3}}
        self.page._on_calc_done(res, self.page._params(), "advanced")
        self.pump(60)
        text = self.adv_text()
        self.assertIn("整体走势", text, "经典部分必须还在")
        self.assertIn("进阶分析", text)
        self.assertIn("模拟警告", text)

    # ------------------------------------------------------------------
    # 4. 三种经典模式零回归
    # ------------------------------------------------------------------
    def test_classic_modes_still_work(self):
        for algo in ("classic", "improved"):
            self.select_algo(algo)
            if algo == "classic":
                res = self.page.client.band_fit(self.page.session_id,
                                                n_segments=8, degree=1)
            else:
                res = self.page.client.band_fit_improved(
                    self.page.session_id, n_segments=8, degree=2)
            self.page._on_calc_done(res, self.page._params(), algo)
            self.pump(60)
            self.assertIn("整体走势", self.adv_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
