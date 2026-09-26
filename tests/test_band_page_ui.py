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
        # 页面本地点集必须一并给：_run_calc() 第一行就是
        # `if not self.points: return`，只往内核 session 存点的话
        # 页面侧的点集仍是空的，点"开始计算"会静默返回，
        # 既不计算也不显示进度条。
        cls.page.points = list(cls.pts)
        # 注意：不在这里起 mainloop 线程 —— Tk 要求 mainloop 跑在
        # 创建 root 的那个线程里，另起线程跑会直接崩。
        # 需要"完整走一次计算"的测试改走 _run_calc_worker() 同步版，
        # 见 test_progress_cleared_after_real_advanced_calc。

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

    # ------------------------------------------------------------------
    # 5. 计算进度条
    # ------------------------------------------------------------------
    def test_progress_widgets_exist(self):
        """进度控件必须建好：条 + 文字，都由 _progress_widgets 管。

        只该有**两个**：进度条与文字。骨架 Frame 不进来 ——
        它被反复 forget/pack 会让周围控件来回跳动。
        """
        self.assertTrue(hasattr(self.page, "prog_bar"))
        self.assertTrue(hasattr(self.page, "prog_label"))
        self.assertEqual(len(self.page._progress_widgets), 2)
        self.assertEqual(set(self.page._progress_widgets),
                         {self.page.prog_bar, self.page.prog_label})

    def test_progress_hidden_initially(self):
        """没计算时进度区不该占着一行空白。"""
        self.assertFalse(bool(str(self.page.prog_bar.winfo_manager())))
        self.assertEqual(self.page.prog_label.cget("text"), "")

    def test_show_then_hide_progress(self):
        self.page._show_progress()
        self.root.update()
        self.assertTrue(bool(str(self.page.prog_bar.winfo_manager())),
                        "显示后应被 pack 管理")
        self.assertIn("计算", self.page.prog_label.cget("text"))
        self.page._hide_progress()
        self.root.update()
        self.assertFalse(bool(str(self.page.prog_bar.winfo_manager())),
                         "隐藏后应脱离 pack 管理")

    def test_progress_bar_actually_has_size(self):
        """pack 上不等于看得见 —— 尺寸必须非零。

        这条守的是一个真事故：进度条原本在左侧栏，被参数区/导入导出/
        分段明细挤出了 760px 窗口的可视范围，用户根本看不到它。
        判据是**有实际尺寸**，不是 pack 状态。
        """
        self.page._show_progress()
        self.root.update_idletasks()
        w = self.page.prog_bar.winfo_width()
        h = self.page.prog_bar.winfo_height()
        self.assertGreater(w, 50,
                           f"进度条宽度 {w}，可能被布局挤没了")
        self.assertGreater(h, 5, f"进度条高度 {h}")
        self.page._hide_progress()

    def test_progress_bar_visible_after_layout(self):
        """布局走完后进度条应有实际尺寸。

        注意不断言 winfo_ismapped()：测试窗口是 withdraw() 的
        （不抢焦点，CI 上也能跑），withdraw 的窗口永不 mapped ——
        那是测试环境的限制，不是产品缺陷。
        真实可见性由 D:/.cache/e2e/check_progressbar_visible.py
        在 deiconify() 的真窗口上验证并截图。
        """
        self.page._show_progress()
        self.root.update_idletasks()
        self.root.update()
        w = self.page.prog_bar.winfo_width()
        self.assertGreater(w, 50,
                           f"布局后宽度仍为 {w}，说明没真正布局")
        self.page._hide_progress()

    def test_progress_in_right_panel_not_left(self):
        """进度条必须与结果文本框在**同一栏** —— 左侧装不下。

        左侧有参数区/导入导出/分段明细，总高度超过常见窗口高度，
        放在那里用户根本看不到（760px 窗口实测如此；
        整改前后对比见 D:/.cache/e2e/shot_progress_visible.png）。

        布局是：right 容器用 grid 排 [进度区, 结果, 图1, 图2]，
        进度区与结果区是**兄弟**而非父子。所以判据是共同父容器，
        不是包含关系。
        """
        prog_host = self.page.prog_bar.master          # prog 骨架
        res_host = self.page.result_text.master        # 结果 LabelFrame
        # 两者的父容器应是同一个（right）
        self.assertIs(prog_host.master, res_host.master,
                      "进度区与结果区不在同一容器，可能被搬回左栏了")

    def test_progress_grid_row_above_result(self):
        """进度区必须在结果区**上方**（grid 行号更小）。

        用户先看到过程、再看到结果，顺序反了会很怪。
        """
        info = self.page.prog_bar.master.grid_info()
        res_info = self.page.result_text.master.grid_info()
        self.assertLess(int(info["row"]), int(res_info["row"]),
                        "进度区应在结果区上方")

    def test_apply_progress_percentage(self):
        """有总数时显示百分比与 done/total，且 mode 切到 determinate。"""
        self.page._show_progress()
        self.page._apply_progress({"active": True, "done": 30,
                                   "total": 120, "phase": "bootstrap"})
        self.root.update()
        self.assertEqual(self.page.prog_bar.cget("value"), 25)
        self.assertEqual(str(self.page.prog_bar.cget("mode")), "determinate")
        txt = self.page.prog_label.cget("text")
        self.assertIn("Bootstrap", txt)
        self.assertIn("30/120", txt)
        self.assertIn("25%", txt)
        self.page._hide_progress()

    def test_apply_progress_unknown_total_goes_indeterminate(self):
        """total=0（选阶、分段这类阶段）走 indeterminate，不得崩。"""
        self.page._show_progress()
        self.page._apply_progress({"active": True, "done": 0, "total": 0,
                                   "phase": "select_degree"})
        self.root.update()
        self.assertEqual(str(self.page.prog_bar.cget("mode")),
                         "indeterminate")
        self.assertIn("自动选阶", self.page.prog_label.cget("text"))
        self.page._hide_progress()

    def test_poll_progress_hides_when_kernel_idle(self):
        """内核没有进行中的计算时，轮询一次就收起。"""
        self.page._show_progress()
        self.page._poll_progress()
        self.root.update()
        self.assertFalse(bool(str(self.page.prog_bar.winfo_manager())),
                         "空闲时应收起")
        self.assertIsNone(self.page._progress_poll,
                          "空闲时不该留下待触发的 after 句柄")

    def _run_calc_sync(self):
        """同步跑完一次计算并把结果送进 UI。

        为什么不走 _run_calc()：它会把 worker 丢进 threading.Thread，
        而 Tk 的 root.after() 只在 main loop 活着时可跨线程注册 ——
        测试环境没有 main loop，注册会抛 RuntimeError 并被
        _safe_after 静默跳过，回调就丢了。
        这里同线程调 _run_calc_worker()，跑完再手动补执行 pending 回调。
        """
        p = self.page._params()
        p["algorithm"] = self._algo_key_of()
        self.page._show_progress()
        self.page._run_calc_worker(p)
        # 补跑 worker 里没能注册成功的 after 回调
        for fn in list(self.page._pending_after):
            fn()
        self.page._pending_after.clear()
        self.root.update()

    def _algo_key_of(self):
        from edit.page import _algo_key
        return _algo_key(self.page.algo_var.get())

    def test_progress_cleared_after_real_advanced_calc(self):
        """真跑一次进阶计算，结束后进度必须收起且不留定时器。"""
        self.select_algo("advanced")
        self.page.sp_boot.set(4)
        self._run_calc_sync()
        self.assertFalse(bool(str(self.page.prog_bar.winfo_manager())),
                         "计算完成后进度条应收起")
        self.assertIsNone(self.page._progress_poll,
                          "计算完成后不该继续轮询")
        text = self.adv_text()
        self.assertIn("进阶分析", text)
        self.assertIn("Bootstrap", text)

    def test_close_stops_progress_poll(self):
        """关窗口必须取消轮询句柄，否则刷 invalid command name。

        与 main.py 状态定时器踩的是同一个坑（见 CHANGELOG）。
        """
        self.page._show_progress()
        self.page._start_progress_poll()
        self.assertIsNotNone(self.page._progress_poll)
        # 只调清理逻辑，不真的销毁窗口（tearDownClass 还要用）
        self.page._stop_progress_poll()
        self.root.update()
        self.assertIsNone(self.page._progress_poll)

    def test_progress_survives_bad_payload(self):
        """畸形进度数据不能让 UI 崩（内核写什么就显示什么）。"""
        self.page._show_progress()
        for bad in ({}, {"active": True},
                    {"active": True, "done": "x", "total": "y"},
                    {"active": True, "done": None, "total": None},
                    {"active": True, "done": 5, "total": 10,
                     "phase": "unknown_phase"}):
            try:
                self.page._apply_progress(bad)
                self.root.update()
            except Exception as e:
                self.page._hide_progress()
                self.fail(f"畸形进度 {bad!r} 导致异常: {e}")
        self.page._hide_progress()

    def test_progress_on_all_algorithms(self):
        """进度条不只服务进阶算法 —— 经典/改进/对比也走同一条路。

        为什么用"是否调用过 show"而不是"点击瞬间是否可见"：
        经典算法是毫秒级返回的，_run_calc() 返回时 after(0) 里
        的 _hide_progress 可能已经跑完，进度条现身即消失。
        那是**正确行为**（快算法不需要进度条停留），
        断言某个瞬间一定可见是在赌竞态，会随机红。
        所以这里验证不变量：每次计算都 show 过、且最终都收起。
        """
        for key in ("classic", "improved", "compare", "advanced"):
            self.select_algo(key)
            self.page.sp_boot.set(2)

            calls = []
            real_show, real_hide = self.page._show_progress, \
                self.page._hide_progress
            self.page._show_progress = lambda: (calls.append("show"),
                                                real_show())[1]
            self.page._hide_progress = lambda: (calls.append("hide"),
                                                real_hide())[1]
            try:
                self.page._run_calc()
                self.pump(2500)
            finally:
                self.page._show_progress = real_show
                self.page._hide_progress = real_hide
                real_hide()

            self.assertEqual(calls.count("show"), 1,
                             f"{key} 算法应只 show 一次，实际 {calls}")
            self.assertIn("hide", calls,
                          f"{key} 算法结束后应收起")
            self.assertIsNone(self.page._progress_poll,
                              f"{key} 算法结束后不该继续轮询")


if __name__ == "__main__":
    unittest.main(verbosity=2)
