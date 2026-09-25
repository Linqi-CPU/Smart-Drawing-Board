"""回归测试：主 UI 的两个遗留 bug。

Bug 1: 主 UI 无法单独关闭（点 X 关不掉窗口）
        根因：_alive() 把 _closing_down 也算作"已死"，导致 dispose()
        一开始置位 flags 就让自己失去 destroy() 能力 —— 自相矛盾。

Bug 2: 清除按钮删不掉采集模式画出的散点
        根因：散点用 TAG_FIT_POINT，而 clear_canvas() 只删
        TAG_MANUAL / TAG_AUTO。
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tkinter as tk   # noqa: E402

import main as ui      # noqa: E402


def _window_gone(root) -> bool:
    """窗口是否真的销毁了。

    destroy() 之后 Tcl 解释器都没了，此时 winfo_exists() 会抛
    TclError 而不是返回 0 —— 所以异常本身就是"已关闭"的证据。
    """
    try:
        return not root.winfo_exists()
    except tk.TclError:
        return True


class TestCloseWindow(unittest.TestCase):
    """Bug 1: 主 UI 必须能被单独关闭。"""

    def setUp(self):
        self.root = tk.Tk()
        self.app = ui.DrawingApp(self.root)
        self.root.update()

    def tearDown(self):
        # 必须走 dispose()：它负责取消自续期的状态刷新回调。
        # 只 destroy() 会把 pending 的 _refresh_kernel_status 留在
        # after 队列里，几秒后 Tk 对着已销毁的窗口派发它，刷
        # 'invalid command name' 噪音。
        try:
            self.app.dispose()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    def test_alive_true_when_open(self):
        self.assertTrue(self.app._alive())
        self.app.dispose()

    def test_alive_ignores_closing_down_flag(self):
        """核心回归：_closing_down 不能影响"窗口是否活着"的判定。

        dispose() 依赖 _alive() 来决定要不要 destroy()。如果把
        _closing_down 算进 _alive()，置位之后就再也 destroy 不了。
        """
        self.app._closing_down = True
        self.assertTrue(self.app._alive(),
                        "_closing_down=True 时 _alive() 仍应为 True")

    def test_on_close_destroys_window(self):
        """点关闭按钮，窗口必须真的消失。"""
        self.root.update()
        self.assertTrue(self.app._alive())

        self.app.on_close()
        self.root.update()

        self.assertTrue(_window_gone(self.root),
                        "on_close() 之后窗口仍然存在 —— 关不掉")

    def test_on_close_cancels_status_timer(self):
        """关闭后不能有残留回调（噪音回归）。"""
        self.app._refresh_kernel_status()
        self.assertIsNotNone(self.app._after_loop)

        self.app.on_close()

        self.assertTrue(self.app._closing_down)
        # 真正的判据是"所有排过的轮次都取消了"，而不是最近一次句柄
        # 变 None —— 后者是旧实现留下的弱断言。
        self.assertEqual(set(self.app._after_pending), set())

    def test_on_close_is_idempotent(self):
        self.app.on_close()
        self.app.on_close()               # 第二次不能抛异常
        self.assertTrue(_window_gone(self.root))

    def test_dispose_after_destroy_is_safe(self):
        self.root.destroy()
        self.app.dispose()                # 对已销毁窗口调用不能崩


class TestClearCanvas(unittest.TestCase):
    """Bug 2: 清除按钮必须删掉散点。"""

    def setUp(self):
        self.root = tk.Tk()
        self.app = ui.DrawingApp(self.root)
        self.root.update()

    def tearDown(self):
        try:
            self.app.on_close()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    def _tags_on_canvas(self):
        tags = set()
        for item in self.app.canvas.find_all():
            tags.update(self.app.canvas.gettags(item))
        return tags

    def _draw_scatter_like_collect_mode(self):
        """按采集模式的真实画法画散点（TAG_FIT_POINT）。"""
        pts = [(100, 100), (200, 180), (300, 260), (400, 340)]
        self.app.collected_points.extend(pts)
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            self.app.canvas.create_line(
                x0, y0, x1, y1, fill=ui.COLOR_FIT_POINT, width=1,
                dash=(2, 2), tags=ui.TAG_FIT_POINT)
        self.app._redraw_fit_points()
        self.root.update()

    def test_clear_button_removes_scatter(self):
        """【清除】必须连采集散点一起删 —— 这是 Bug 2 的核心回归。"""
        self._draw_scatter_like_collect_mode()
        self.assertIn(ui.TAG_FIT_POINT, self._tags_on_canvas(),
                      "前置条件：散点应已在画布上")

        self.app.clear_canvas()
        self.root.update()

        self.assertNotIn(ui.TAG_FIT_POINT, self._tags_on_canvas(),
                         "清除后散点仍在 —— Bug 2 回归")

    def test_clear_button_removes_fit_curve(self):
        self.app.canvas.create_line(
            10, 10, 500, 400, fill="blue", width=2, tags=ui.TAG_FIT_CURVE)
        self.root.update()
        self.assertIn(ui.TAG_FIT_CURVE, self._tags_on_canvas())

        self.app.clear_canvas()
        self.root.update()

        self.assertNotIn(ui.TAG_FIT_CURVE, self._tags_on_canvas())

    def test_clear_button_keeps_y_limit_line(self):
        """Y 上限线是画布基础设施，按设计保留。"""
        self.app.clear_canvas()
        self.root.update()
        self.assertIn(ui.TAG_GUIDE, self._tags_on_canvas(),
                      "清除后 Y 上限线也被删了 —— 与设计不符")

    def test_clear_button_keeps_manual_when_clean(self):
        """有手绘内容时应一并删除。"""
        self.app.canvas.create_line(
            10, 10, 100, 100, fill=ui.COLOR_MANUAL, width=2,
            capstyle=tk.ROUND, tags=ui.TAG_MANUAL)
        self.root.update()

        self.app.clear_canvas()
        self.root.update()

        self.assertNotIn(ui.TAG_MANUAL, self._tags_on_canvas())

    def test_clear_collected_points_also_clears_canvas(self):
        """【清空数据点】按钮也应清掉散点。"""
        self._draw_scatter_like_collect_mode()
        self.assertIn(ui.TAG_FIT_POINT, self._tags_on_canvas())

        self.app.clear_collected_points()
        self.root.update()

        self.assertNotIn(ui.TAG_FIT_POINT, self._tags_on_canvas())
        self.assertEqual(self.app.collected_points, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
