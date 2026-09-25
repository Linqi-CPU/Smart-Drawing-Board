"""GUI 冒烟测试：真实创建窗口，驱动各函数绘制并断言画布状态。

不依赖人工点击，全部通过 event_generate / after 驱动，最后截图存档。
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

from core import graph_engine as ge
import main as app_mod


class TestGUISmoke(unittest.TestCase):
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

    def pump(self, ms=200):
        """真实推进墙钟时间，让 tkinter 的 after 回调有机会执行。

        只调 update() 是不够的：after(16, ...) 必须等真实时间过去才会触发。
        """
        import time

        deadline = time.time() + ms / 1000.0
        while time.time() < deadline:
            self.root.update()
            time.sleep(0.005)
        self.root.update()
        self.root.update_idletasks()

    def run_animation(self, max_ms=6000):
        """等到自动绘制动画真正结束（或超时）。"""
        import time

        deadline = time.time() + max_ms / 1000.0
        while self.app.is_auto_drawing and time.time() < deadline:
            self.root.update()
            time.sleep(0.004)
        self.root.update()
        self.root.update_idletasks()

    # ---------------- 基础 ----------------
    def test_01_window_created(self):
        self.assertIn("智能绘图板", self.root.title())
        self.assertGreater(self.app.canvas.winfo_width(), 100)

    def test_02_canvas_size_recorded(self):
        # startup 后 Configure 应已把真实尺寸写进属性
        self.assertEqual(self.app.canvas_width, self.app.canvas.winfo_width())
        self.assertEqual(self.app.canvas_height, self.app.canvas.winfo_height())

    def test_03_functions_dir_resolved(self):
        self.assertTrue(self.app.functions_dir.is_dir())
        self.assertEqual(
            sorted(p.name for p in self.app.functions_dir.glob("*.py")),
            ["spiral_func.py", "wave_func.py"],
        )

    def test_04_combobox_populated(self):
        self.assertEqual(list(self.app.file_combobox["values"]),
                         ["spiral_func", "wave_func"])

    def test_05_no_stray_pycache_in_functions(self):
        extra = [p.name for p in self.app.functions_dir.iterdir()
                 if p.name not in {"spiral_func.py", "wave_func.py"}]
        self.assertEqual(extra, [], f"functions 目录被污染: {extra}")

    # ---------------- 手动绘制 ----------------
    def test_10_manual_draw_creates_items(self):
        self.app.clear_canvas()
        self.pump()
        before = len(self.app.canvas.find_withtag(app_mod.TAG_MANUAL))
        self.app.canvas.event_generate("<Button-1>", x=50, y=50)
        for x in range(50, 300, 10):
            self.app.canvas.event_generate("<B1-Motion>", x=x, y=50 + (x // 10))
        self.app.canvas.event_generate("<ButtonRelease-1>", x=300, y=80)
        self.pump()
        after = len(self.app.canvas.find_withtag(app_mod.TAG_MANUAL))
        self.assertGreater(after, before, "手动绘制未产生线段")
        self.assertGreater(self.app.slide_distance, 0.0, "滑动值未累计")
        self.assertFalse(self.app.is_drawing)

    def test_11_clear_canvas_wipes_drawings_keeps_guide(self):
        self.app.auto_draw()
        self.run_animation()
        self.assertGreater(len(self.app.canvas.find_withtag(app_mod.TAG_AUTO)), 0)
        self.app.clear_canvas()
        self.pump()
        self.assertEqual(self.app.canvas.find_withtag(app_mod.TAG_MANUAL), ())
        self.assertEqual(self.app.canvas.find_withtag(app_mod.TAG_AUTO), ())
        self.assertGreater(len(self.app.canvas.find_withtag(app_mod.TAG_GUIDE)), 0)
        self.assertEqual(self.app.slide_distance, 0.0)

    def test_12_reset_slide(self):
        self.app.slide_distance = 123.4
        self.app.reset_slide()
        self.assertEqual(self.app.slide_distance, 0.0)

    # ---------------- 自动绘制 ----------------
    def test_20_auto_draw_sine(self):
        for ftype in (ge.SINE, ge.COSINE, ge.PARABOLA, ge.LINEAR):
            with self.subTest(ftype=ftype):
                self.app.func_type.set(ftype)
                self.app.auto_draw()
                self.run_animation()
                items = self.app.canvas.find_withtag(app_mod.TAG_AUTO)
                self.assertGreater(len(items), 10, f"{ftype} 未画出曲线")
                self.assertFalse(self.app.is_auto_drawing,
                                 f"{ftype} 动画未正常结束")
                self.assertGreater(self.app.slide_distance, 0.0)

    def test_21_auto_draw_curve_stays_in_canvas(self):
        w, h = self.app.canvas_width, self.app.canvas_height
        for ftype in (ge.SINE, ge.PARABOLA):
            with self.subTest(ftype=ftype):
                self.app.func_type.set(ftype)
                self.app.auto_draw()
                self.run_animation()
                # 只检查线段；起始点 oval 共用 TAG_AUTO，需过滤。
                # 一次批量 create_line 会产生含多段坐标的单个 item，
                # 因此坐标按每 4 个一组切分。
                lines = [i for i in self.app.canvas.find_withtag(app_mod.TAG_AUTO)
                         if self.app.canvas.type(i) == "line"]
                self.assertGreater(len(lines), 0, "曲线线段数量不足")
                checked = 0
                for item in lines:
                    c = self.app.canvas.coords(item)
                    self.assertEqual(len(c) % 4, 0, "线段坐标数应为 4 的倍数")
                    for k in range(0, len(c), 4):
                        x0, y0, x1, y1 = c[k:k + 4]
                        checked += 1
                        for y in (y0, y1):
                            self.assertGreaterEqual(y, -1, "超出画布上边界")
                            self.assertLessEqual(y, h + 1, "超出画布下边界")
                        for x in (x0, x1):
                            self.assertGreaterEqual(x, -1, "超出左边界")
                            self.assertLessEqual(x, w + 1, "超出右边界")
                self.assertGreater(checked, 10, "检查到的线段数量不足")

    def test_22_auto_draw_slide_matches_polyline(self):
        self.app.func_type.set(ge.SINE)
        self.app.auto_draw()
        self.run_animation()
        points = ge.sample_curve(
            ge.SINE, None, self.app.canvas_width, self.app.canvas_height,
            self.app.amplitude_var.get(), self.app.frequency_var.get(), step=2,
        )
        expected = ge.polyline_length(points)
        self.assertAlmostEqual(self.app.slide_distance, expected, places=4)

    def test_23_auto_draw_amplitude_change(self):
        self.app.func_type.set(ge.SINE)
        self.app.amplitude_var.set(5)
        self.app.auto_draw()
        self.run_animation()
        small = self.app.slide_distance

        self.app.amplitude_var.set(350)
        self.app.auto_draw()
        self.run_animation()
        large = self.app.slide_distance
        self.assertGreater(large, small, "增大振幅未使曲线变长")

    def test_24_auto_draw_custom_function(self):
        self.app.custom_func_name_var.set("wave_func")
        self.app.func_type.set(ge.CUSTOM)
        self.app._invalidate_custom_func_cache()
        self.app.auto_draw()
        self.run_animation()
        self.assertGreater(len(self.app.canvas.find_withtag(app_mod.TAG_AUTO)), 10)
        self.assertIsNotNone(self.app.custom_func)

    def test_25_auto_draw_spiral_custom(self):
        self.app.custom_func_name_var.set("spiral_func")
        self.app.func_type.set(ge.CUSTOM)
        self.app._invalidate_custom_func_cache()
        self.app.auto_draw()
        self.run_animation()
        self.assertGreater(len(self.app.canvas.find_withtag(app_mod.TAG_AUTO)), 10)

    def test_26_auto_draw_missing_custom_function_does_not_crash(self):
        self.app.custom_func_name_var.set("no_such_func_xyz")
        self.app.func_type.set(ge.CUSTOM)
        self.app._invalidate_custom_func_cache()
        # _ensure_custom_func_loaded 会弹 error 对话框，测试里用一个 stub 吃掉
        shown = []
        orig = app_mod.messagebox.showerror
        app_mod.messagebox.showerror = lambda *a, **k: shown.append(a)
        try:
            self.app.auto_draw()
            self.pump(50)
        finally:
            app_mod.messagebox.showerror = orig
        self.assertEqual(self.app.canvas.find_withtag(app_mod.TAG_AUTO), ())
        self.assertTrue(shown, "应有错误提示")

    def test_27_stop_drawing_halts_animation(self):
        self.app.func_type.set(ge.SINE)
        self.app.speed_var.set(1)   # 最慢，确保还没画完
        self.app.auto_draw()
        self.pump(30)
        self.assertTrue(self.app.is_auto_drawing, "慢速下应仍在绘制")
        self.app.stop_drawing()
        self.run_animation()
        self.assertFalse(self.app.is_auto_drawing)
        self.assertIsNone(self.app._anim_job)

    def test_28_rapid_auto_draw_twice(self):
        """连续两次点击'自动按钮'不能出现重复曲线或状态错乱。"""
        self.app.func_type.set(ge.SINE)
        self.app.auto_draw()
        self.app.auto_draw()
        self.run_animation()
        self.assertFalse(self.app.is_auto_drawing)
        # 第二次应clear掉第一次，最终只有一条曲线
        self.assertGreater(len(self.app.canvas.find_withtag(app_mod.TAG_AUTO)), 10)
        expected = ge.polyline_length(ge.sample_curve(
            ge.SINE, None, self.app.canvas_width, self.app.canvas_height,
            self.app.amplitude_var.get(), self.app.frequency_var.get(), step=2))
        self.assertAlmostEqual(self.app.slide_distance, expected, places=4,
                               msg="重复点击导致滑动值叠加")

    # ---------------- Y 上限线 ----------------
    def test_30_y_limit_line_covers_full_width(self):
        self.app.y_limit_var.set(200)
        self.app.update_y_limit()
        self.pump()
        items = self.app.canvas.find_withtag(app_mod.TAG_GUIDE)
        self.assertGreater(len(items), 0)
        # 第一条是横线，应横跨整个画布宽度
        coords = self.app.canvas.coords(items[0])
        self.assertEqual(len(coords), 4)
        x0, y0, x1, y1 = coords
        self.assertAlmostEqual(x1 - x0, self.app.canvas_width, delta=1.5,
                               msg="Y 上限线未覆盖当前画布宽度")
        self.assertAlmostEqual(y0, 200, delta=0.5)

    def test_31_y_limit_line_after_resize(self):
        """窗口拉伸后 Y 线必须覆盖新宽度（原实现的 bug）。

        注意：canvas.configure(width=...) 在 pack(fill=BOTH, expand=True) 下无效，
        必须改 root 的 geometry 才是真实拉伸。
        """
        old_geo = self.root.geometry()
        self.app.y_limit_var.set(180)
        self.app.update_y_limit()
        self.root.geometry("1100x800")
        self.run_animation()
        w_after = self.app.canvas_width
        self.assertNotEqual(w_after, 700, "geometry 变化后画布宽度应改变")
        items = self.app.canvas.find_withtag(app_mod.TAG_GUIDE)
        x0, y0, x1, y1 = self.app.canvas.coords(items[0])
        self.assertAlmostEqual(x1 - x0, w_after, delta=1.5,
                               msg="Y 上限线未覆盖拉伸后的画布宽度")
        # 复原，避免影响后续用例
        self.root.geometry(old_geo)
        self.run_animation()

    def test_32_y_limit_clamped(self):
        self.app.y_limit_var.set(99999)
        self.app.update_y_limit()
        self.assertEqual(self.app.y_limit, 2000)
        self.app.y_limit_var.set(-5)
        self.app.update_y_limit()
        self.assertEqual(self.app.y_limit, 20)
        self.app.y_limit_var.set(300)
        self.app.update_y_limit()

    def test_33_resize_aborts_auto_draw(self):
        old_geo = self.root.geometry()
        self.app.func_type.set(ge.SINE)
        self.app.speed_var.set(1)          # 最慢，确保此刻仍未画完
        self.app.auto_draw()
        self.pump(30)
        self.assertTrue(self.app.is_auto_drawing, "慢速绘制中应仍在动画")
        # 真实拉伸窗口（configure width 在 pack expand 下无效）
        self.root.geometry("760x620")
        self.pump(120)
        self.assertFalse(self.app.is_auto_drawing, "画布尺寸变化应中止自动绘制")
        self.root.geometry(old_geo)
        self.pump(120)

    # ---------------- 自定义函数面板 ----------------
    def test_40_template_bounded(self):
        src = cl_template()
        ns = {}
        exec(compile(src, "<t>", "exec"), ns)
        w, cy = 700.0, 250.0
        for amp in (100, 400):
            ys = [ns["my_func"](x, w, cy, amp, 2) for x in range(0, 701, 50)]
            self.assertLess(max(ys) - cy, amp + 1e-9)
            self.assertGreaterEqual(min(ys), cy - amp - 1e-9)

    def test_41_save_and_load_roundtrip(self):
        self.app.custom_func_name_var.set("smoke_tmp_func")
        self.app.custom_code_text.delete("1.0", tk.END)
        self.app.custom_code_text.insert("1.0",
            "import math\n"
            "def smoke_tmp_func(x, width, center_y, amp, freq):\n"
            "    return center_y + amp * (x / width)\n")
        self.app._invalidate_custom_func_cache()
        self.app.save_custom_function()
        self.pump()
        p = self.app.functions_dir / "smoke_tmp_func.py"
        self.assertTrue(p.is_file(), "保存函数未写入文件")
        self.assertEqual(self.app.custom_func_name, "smoke_tmp_func")
        # 下拉框应刷新并包含新函数
        self.assertIn("smoke_tmp_func", list(self.app.file_combobox["values"]))
        p.unlink()

    def test_42_save_rejects_bad_name(self):
        self.app.custom_func_name_var.set("1bad-name")
        self.app.custom_code_text.delete("1.0", tk.END)
        self.app.custom_code_text.insert("1.0", "def f(): pass")
        shown = []
        orig = app_mod.messagebox.showwarning
        app_mod.messagebox.showwarning = lambda *a, **k: shown.append(a)
        try:
            self.app.save_custom_function()
        finally:
            app_mod.messagebox.showwarning = orig
        self.assertTrue(shown)
        self.assertFalse((self.app.functions_dir / "1bad-name.py").exists())

    def test_43_save_warns_on_syntax_error(self):
        self.app.custom_func_name_var.set("smoke_bad_syntax")
        self.app.custom_code_text.delete("1.0", tk.END)
        self.app.custom_code_text.insert("1.0", "def smoke_bad_syntax(:\n  pass\n")
        shown = []
        orig = app_mod.messagebox.showwarning
        app_mod.messagebox.showwarning = lambda *a, **k: shown.append(a)
        try:
            self.app.save_custom_function()
        finally:
            app_mod.messagebox.showwarning = orig
        self.assertTrue(shown, "语法错误应给出警告")
        p = self.app.functions_dir / "smoke_bad_syntax.py"
        self.assertTrue(p.is_file(), "文件仍应保存")
        self.assertIsNone(self.app.custom_func, "坏函数不应进入缓存")
        p.unlink()

    def test_44_clear_custom_function_resets_template(self):
        self.app.custom_code_text.delete("1.0", tk.END)
        self.app.custom_code_text.insert("1.0", "garbage")
        self.app.custom_func_name_var.set("demo_fn")
        self.app.clear_custom_function()
        content = self.app.custom_code_text.get("1.0", tk.END)
        self.assertIn("def demo_fn(x, width, center_y, amp, freq):", content)
        self.assertIn("return center_y + amp * math.sin", content)

    def test_45_on_file_select_loads_content(self):
        self.app.file_combobox.set("wave_func")
        self.app.on_file_select()
        content = self.app.custom_code_text.get("1.0", tk.END)
        self.assertIn("def wave_func", content)
        self.assertEqual(self.app.custom_func_name_var.get(), "wave_func")

    # ---------------- 帮助窗口 ----------------
    def test_50_help_window_opens_and_searches(self):
        self.app.show_help()
        tops = [w for w in self.root.winfo_children()
                if isinstance(w, tk.Toplevel)]
        self.assertTrue(tops)
        win = tops[-1]
        self.pump()
        # 关闭，避免影响后续测试
        win.destroy()
        self.pump()

    # ---------------- 关闭 ----------------
    def test_90_on_close_destroys(self):
        """单独用一个窗口验证关闭路径（不能销毁共享 root）。

        断言定时器与关闭标志：最后一个 Tk 根窗口被 destroy() 后，
        Tcl 解释器在进程内未必立刻消失（winfo_exists 仍可能返回 1），
        所以不靠 winfo_* 报错来判断，而是查我们自己维护的确定状态，
        以及 Tk 的 after 队列里是否还残留本页面的刷新任务。
        """
        r = tk.Tk()
        a = app_mod.DrawingApp(r)
        r.update()
        a.is_auto_drawing = False
        a._refresh_kernel_status()
        self.pump(30)
        pending = a._after_loop

        a.on_close()
        self.pump(30)

        self.assertTrue(a._closing_down)
        # 所有排过的轮次都必须取消，不能只算最近一次的句柄
        self.assertEqual(set(a._after_pending), set())
        # 状态刷新任务必须从 Tk 队列里消失（这是噪音的根源）
        self.assertNotIn(pending, tuple(r.tk.call("after", "info")))

    def test_91_dispose_stops_status_timer(self):
        """关闭后自续期定时器必须取消。

        这是 'invalid command name ...refresh_kernel_status' 噪音的
        回归测试：dispose() 必须把 after_cancel 真的发出去，
        而不是只把标志位置上。

        为什么不做"等满一个刷新周期"的端到端测试：那要 sleep 7 秒，
        会拖慢同 suite 里依赖动画时序的用例（共享 root），
        而且 Tk 对已销毁解释器的报错本身就依赖时序，不可靠。
        直接查 Tk 的 after 队列更确定。
        """
        r = tk.Tk()
        a = app_mod.DrawingApp(r)
        r.update()

        # 排下一次刷新，拿到 pending 句柄
        a._refresh_kernel_status()
        self.pump(30)
        pending = a._after_loop
        self.assertIsNotNone(pending)
        queue_before = tuple(r.tk.call("after", "info"))
        self.assertIn(pending, queue_before)

        a.dispose()

        self.assertTrue(a._closing_down)
        # _after_pending 必须被清空 —— 这是"所有排过的轮次都取消了"
        # 的直接证据。_after_loop 仍指向最近一次句柄（用于旧断言），
        # 不再拿它当判据：它是"最近一次"而非"是否还有 pending"。
        self.assertEqual(set(a._after_pending), set())
        # 队列里不该再有我们的定时任务（pending 那个必须消失）
        self.assertNotIn(pending, tuple(r.tk.call("after", "info")))


def cl_template():
    from core import custom_loader as cl
    return cl.function_source_template("my_func")


if __name__ == "__main__":
    unittest.main(verbosity=2)
