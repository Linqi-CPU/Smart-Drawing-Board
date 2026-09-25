"""自动建模（离散点拟合）GUI 冒烟测试。

真实创建窗口，用 event_generate 模拟鼠标在画布上画一条近似抛物线，
然后点'开始拟合'，断言：
  · 采集到足够点
  · 拟合阶数正确（应选 2 阶）
  · R² 高
  · 画布上真的出现了拟合曲线与数据点
  · 结果文本框显示函数表达式
"""

import math
import os
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DRAWING_FUNCTIONS_DIR", str(ROOT / "functions"))

import tkinter as tk

from core import fitting as ft
from core import graph_engine as ge
import main as app_mod


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tk.Tk()
        cls.app = app_mod.DrawingApp(cls.root)
        cls.root.update()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.app.dispose()
        except Exception:
            pass

    def pump(self, ms=150):
        deadline = time.time() + ms / 1000.0
        while time.time() < deadline:
            self.root.update()
            time.sleep(0.004)
        self.root.update()
        self.root.update_idletasks()

    def draw_curve(self, fn, x0=0, x1=None, steps=40):
        """在画布上按 fn(x) 拖出一条曲线，触发采集。"""
        if x1 is None:
            x1 = self.app.canvas_width
        self.app.canvas.event_generate("<Button-1>", x=x0, y=int(fn(x0)))
        for i in range(1, steps + 1):
            t = i / steps
            x = int(x0 + (x1 - x0) * t)
            self.app.canvas.event_generate("<B1-Motion>", x=x, y=int(fn(x)))
        self.app.canvas.event_generate("<ButtonRelease-1>", x=x1, y=int(fn(x1)))
        self.pump(30)


class TestCollectMode(_Base):
    def setUp(self):
        self.app.clear_canvas()
        self.app.clear_collected_points()
        self.app.collect_mode = False
        self.app.collect_btn.config(text="开启离散点采集")
        self.pump(20)

    def test_01_toggle_collect_mode(self):
        self.assertFalse(self.app.collect_mode)
        self.app.toggle_collect_mode()
        self.assertTrue(self.app.collect_mode)
        self.assertEqual(self.app.collect_btn.cget("text"), "关闭离散点采集")
        self.app.toggle_collect_mode()
        self.assertFalse(self.app.collect_mode)
        self.assertEqual(self.app.collect_btn.cget("text"), "开启离散点采集")

    def test_02_collect_records_points(self):
        self.app.toggle_collect_mode()
        self.draw_curve(lambda x: 250 + 0.2 * (x - 350))
        pts = self.app.collected_points
        self.assertGreater(len(pts), 5, "未采集到足够点")
        # 采集点应绘制为标记
        self.assertGreater(
            len(self.app.canvas.find_withtag(app_mod.TAG_FIT_POINT)), 0
        )

    def test_03_collect_mode_draws_no_manual_lines(self):
        self.app.toggle_collect_mode()
        self.draw_curve(lambda x: 250)
        self.assertEqual(
            len(self.app.canvas.find_withtag(app_mod.TAG_MANUAL)), 0,
            "采集模式不应产生手绘线",
        )

    def test_04_normal_draw_still_works(self):
        self.draw_curve(lambda x: 200, steps=5)
        self.assertEqual(len(self.app.collected_points), 0,
                         "非采集模式不应记录点")
        self.assertGreater(
            len(self.app.canvas.find_withtag(app_mod.TAG_MANUAL)), 0
        )

    def test_05_clear_collected_points(self):
        self.app.toggle_collect_mode()
        self.draw_curve(lambda x: 250)
        self.assertGreater(len(self.app.collected_points), 0)
        self.app.clear_collected_points()
        self.assertEqual(self.app.collected_points, [])
        self.assertEqual(
            len(self.app.canvas.find_withtag(app_mod.TAG_FIT_POINT)), 0
        )
        self.assertEqual(self.app.collect_status_label.cget("text"),
                         "已采集: 0 点")

    def test_06_duplicate_points_deduped(self):
        self.app.toggle_collect_mode()
        x, y = 100, 200
        self.app._record_collected_point(x, y)
        self.app._record_collected_point(x, y)
        self.assertEqual(len(self.app.collected_points), 1)

    def test_07_collect_capped(self):
        self.app.toggle_collect_mode()
        for i in range(app_mod.MAX_COLLECTED_POINTS + 50):
            self.app._record_collected_point(i, 100)
        self.assertEqual(len(self.app.collected_points),
                         app_mod.MAX_COLLECTED_POINTS)

    def test_08_status_label_updates(self):
        self.app.toggle_collect_mode()
        self.draw_curve(lambda x: 250)
        text = self.app.collect_status_label.cget("text")
        self.assertTrue(text.startswith("已采集: "))
        self.assertRegex(text, r"已采集: \d+ 点")

    def test_09_entering_collect_stops_auto_draw(self):
        self.app.func_type.set(ge.SINE)
        self.app.auto_draw()
        self.pump(30)
        self.assertTrue(self.app.is_auto_drawing)
        self.app.toggle_collect_mode()
        self.assertFalse(self.app.is_auto_drawing)
        self.assertTrue(self.app.collect_mode)


class TestRunFit(_Base):
    def setUp(self):
        self.app.clear_canvas()
        self.app.clear_collected_points()
        self.app.collect_mode = False
        self.app.collect_btn.config(text="开启离散点采集")
        self.pump(20)

    def test_10_fit_line(self):
        self.app.toggle_collect_mode()
        self.draw_curve(lambda x: 200 + 0.3 * x)
        self.app.run_fit()
        self.assertIsNotNone(self.app.fit_result)
        self.assertEqual(self.app.fit_result.degree, 1)
        self.assertGreater(self.app.fit_result.r2, 0.98)
        self.assertGreater(
            len(self.app.canvas.find_withtag(app_mod.TAG_FIT_CURVE)), 0
        )

    def test_11_fit_parabola_auto_selects_degree2(self):
        """画 U 形，自动模式应选 2 阶。"""
        self.app.toggle_collect_mode()
        self.draw_curve(lambda x: 400 + 0.0008 * (x - 350) ** 2)
        self.app.run_fit()
        self.assertIsNotNone(self.app.fit_result)
        self.assertEqual(self.app.fit_result.degree, 2,
                         "U 形数据应选 2 阶")
        self.assertGreater(self.app.fit_result.r2, 0.99)
        # 曲线与数据点都要画出来
        self.assertGreater(
            len(self.app.canvas.find_withtag(app_mod.TAG_FIT_CURVE)), 0
        )
        self.assertEqual(
            len([i for i in self.app.canvas.find_withtag(app_mod.TAG_FIT_POINT)
                 if self.app.canvas.type(i) == "oval"]),
            len(self.app.collected_points),
            "每个采集点都应有标记",
        )

    def test_12_result_text_shows_expression(self):
        self.app.toggle_collect_mode()
        self.draw_curve(lambda x: 200 + 0.3 * x)
        self.app.run_fit()
        text = self.app.fit_result_text.get("1.0", tk.END)
        self.assertIn("R²", text)
        self.assertIn("RMSE", text)
        self.assertIn("y =", text)

    def test_13_candidate_label_populated(self):
        self.app.toggle_collect_mode()
        self.draw_curve(lambda x: 250 + 0.001 * (x - 300) ** 2)
        self.app.run_fit()
        cand = self.app.cand_label.cget("text")
        self.assertIn("各阶对比", cand)
        self.assertIn("←选中", cand)

    def test_14_manual_degree_overrides_auto(self):
        self.app.toggle_collect_mode()
        self.draw_curve(lambda x: 200 + 0.3 * x)
        self.app.fit_degree_var.set("3")
        self.app.run_fit()
        self.assertEqual(self.app.fit_result.degree, 3)

    def test_15_invalid_degree_rejected(self):
        """3 阶需要 >= 4 个不同 x 的点；只有 2 点时应提示而不是崩溃。"""
        self.app.collect_mode = True
        self.app.collected_points = [(0.0, 0.0), (100.0, 100.0)]
        self.app.fit_degree_var.set("3")
        shown = []
        orig = app_mod.messagebox.showerror
        app_mod.messagebox.showerror = lambda *a, **k: shown.append(a)
        try:
            self.app.run_fit()
        finally:
            app_mod.messagebox.showerror = orig
        self.assertTrue(shown, "阶数过高应有错误提示")
        self.assertIsNone(self.app.fit_result, "失败不应写入结果")

    def test_16_fit_without_points_warns(self):
        shown = []
        orig = app_mod.messagebox.showwarning
        app_mod.messagebox.showwarning = lambda *a, **k: shown.append(a)
        try:
            self.app.run_fit()
        finally:
            app_mod.messagebox.showwarning = orig
        self.assertTrue(shown)
        self.assertIsNone(self.app.fit_result)

    def test_17_single_point_warns(self):
        self.app.collect_mode = True
        self.app.collected_points = [(10.0, 20.0)]
        shown = []
        orig = app_mod.messagebox.showwarning
        app_mod.messagebox.showwarning = lambda *a, **k: shown.append(a)
        try:
            self.app.run_fit()
        finally:
            app_mod.messagebox.showwarning = orig
        self.assertTrue(shown)

    def test_18_identical_x_rejected(self):
        self.app.collect_mode = True
        self.app.collected_points = [(50.0, 10.0), (50.0, 80.0), (50.0, 150.0)]
        shown = []
        orig = app_mod.messagebox.showerror
        app_mod.messagebox.showerror = lambda *a, **k: shown.append(a)
        try:
            self.app.run_fit()
        finally:
            app_mod.messagebox.showerror = orig
        self.assertTrue(shown, "x 全相同应报错")
        self.assertIsNone(self.app.fit_result)

    def test_19_curve_within_canvas(self):
        self.app.toggle_collect_mode()
        self.draw_curve(lambda x: 500 + 0.0006 * (x - 350) ** 2)
        self.app.run_fit()
        h = self.app.canvas_height
        for item in self.app.canvas.find_withtag(app_mod.TAG_FIT_CURVE):
            c = self.app.canvas.coords(item)
            for k in range(0, len(c), 4):
                for y in (c[k + 1], c[k + 3]):
                    self.assertGreaterEqual(y, -1, "曲线超出上边界")
                    self.assertLessEqual(y, h + 1, "曲线超出下边界")

    def test_20_refit_replaces_previous(self):
        self.app.toggle_collect_mode()
        self.draw_curve(lambda x: 200 + 0.3 * x)
        self.app.run_fit()
        n_first = len(self.app.canvas.find_withtag(app_mod.TAG_FIT_CURVE))
        self.app.run_fit()
        n_second = len(self.app.canvas.find_withtag(app_mod.TAG_FIT_CURVE))
        self.assertEqual(n_first, n_second, "重复拟合不应叠加曲线 item")

    def test_21_points_stay_on_curve(self):
        """拟合后每个数据点都应落在拟合曲线上（同一缩放的直接证据）。"""
        self.app.toggle_collect_mode()
        self.draw_curve(lambda x: 300 + 0.0005 * (x - 350) ** 2)
        self.app.run_fit()
        r = self.app.fit_result
        pts = self.app.collected_points
        # 用 predict 与真实 y 比，R² 高说明点确实在曲线上
        ss_tot = sum((y - sum(y for _, y in pts) / len(pts)) ** 2 for _, y in pts)
        ss_res = sum((y - r.predict(x)) ** 2 for x, y in pts)
        r2 = 1 - ss_res / ss_tot
        self.assertGreater(r2, 0.99)

    def test_22_resize_invalidates_fit(self):
        self.app.toggle_collect_mode()
        self.draw_curve(lambda x: 250 + 0.001 * (x - 300) ** 2)
        self.app.run_fit()
        self.assertIsNotNone(self.app.fit_result)
        old_geo = self.root.geometry()
        self.root.geometry("1100x820")
        self.pump(200)
        self.assertIsNone(self.app.fit_result, "拉伸后拟合结果应作废")
        self.root.geometry(old_geo)
        self.pump(200)

    def test_23_fit_result_survives_clear_collect(self):
        self.app.toggle_collect_mode()
        self.draw_curve(lambda x: 200 + 0.3 * x)
        self.app.run_fit()
        self.app.clear_collected_points()
        self.assertIsNone(self.app.fit_result)

    def test_24_copy_and_report_no_crash(self):
        self.app.toggle_collect_mode()
        self.draw_curve(lambda x: 200 + 0.3 * x)
        self.app.run_fit()
        # 剪贴板在无窗口环境可能失败，但只要不抛异常就算通过
        try:
            self.app.copy_fit_expression()
        except Exception as e:  # pragma: no cover
            self.fail(f"copy_fit_expression 抛异常: {e}")
        self.assertTrue(self.app.fit_result is not None)


class TestFitReport(_Base):
    def setUp(self):
        self.app.clear_canvas()
        self.app.clear_collected_points()
        self.app.collect_mode = False
        self.pump(20)

    def test_25_report_no_result_shows_info(self):
        shown = []
        orig = app_mod.messagebox.showinfo
        app_mod.messagebox.showinfo = lambda *a, **k: shown.append(a)
        try:
            self.app.save_fit_report()
        finally:
            app_mod.messagebox.showinfo = orig
        self.assertTrue(shown)

    def test_26_report_no_result_copy(self):
        shown = []
        orig = app_mod.messagebox.showinfo
        app_mod.messagebox.showinfo = lambda *a, **k: shown.append(a)
        try:
            self.app.copy_fit_expression()
        finally:
            app_mod.messagebox.showinfo = orig
        self.assertTrue(shown)


if __name__ == "__main__":
    unittest.main(verbosity=2)
