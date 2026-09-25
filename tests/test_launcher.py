"""启动器（launcher）的测试。

启动器是生命周期最长的进程，整套系统的锚点。这里锁定它的核心行为：
1. 功能列表 = 主 UI + 所有已注册 edit 页面（自动，不用改启动器）
2. 能拉起功能进程，并记录为自己的子进程
3. 关闭时收尾页面，但**保留内核**（内核是共享资产，下次还能用）
4. 状态轮询在关闭后必须停止（否则刷 invalid command name 噪音）
"""

import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tkinter as tk                     # noqa: E402

import launcher as lc                    # noqa: E402
import kernel_bridge as kb               # noqa: E402
from core.client import KernelClient     # noqa: E402

PORT = int(os.environ.get("HERMES_TEST_KERNEL_PORT", "8765"))


def _reap(proc: subprocess.Popen) -> None:
    """结束一个子进程并 wait，避免 ResourceWarning 与孤儿。"""
    try:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass


class TestEntries(unittest.TestCase):
    """功能列表：必须自动包含主 UI 与所有注册页面。"""

    def test_includes_main_ui(self):
        entries = lc.LauncherApp._entries(None)
        keys = [k for k, _, _ in entries]
        self.assertIn("__main__", keys)

    def test_includes_all_registered_pages(self):
        entries = lc.LauncherApp._entries(None)
        keys = [k for k, _, _ in entries]
        for page in kb.LAUNCHERS:
            self.assertIn(f"page:{page}", keys,
                          f"注册了页面 {page} 但启动器没列出")

    def test_adding_a_page_automatically_appears(self):
        """核心承诺：新页面加进 LAUNCHERS 就自动出现在启动器里。"""
        saved = dict(kb.LAUNCHERS)
        try:
            kb.LAUNCHERS["demo_new_page"] = "演示新页面"
            keys = [k for k, _, _ in lc.LauncherApp._entries(None)]
            self.assertIn("page:demo_new_page", keys)
        finally:
            kb.LAUNCHERS.clear()
            kb.LAUNCHERS.update(saved)

    def test_every_entry_has_label_and_desc(self):
        for key, label, desc in lc.LauncherApp._entries(None):
            self.assertTrue(label, f"{key} 缺 label")
            self.assertTrue(desc, f"{key} 缺 desc")


class TestLauncherRuntime(unittest.TestCase):
    """启动器实际运行行为（真实建窗口）。"""

    @classmethod
    def setUpClass(cls):
        # 确保内核在跑，否则启动器的探测无从谈起
        cls.kernel_started_here = False
        if not kb.kernel_alive(PORT):
            kb.start_kernel(PORT)
            cls.kernel_started_here = True

    def setUp(self):
        self.root = tk.Tk()
        self.app = lc.LauncherApp(self.root)
        self.root.update()

    def tearDown(self):
        try:
            self.app.on_close()
        except Exception:
            pass

    def test_kernel_status_shown(self):
        self.root.update()
        txt = self.app.kernel_var.get()
        self.assertIn("内核:", txt)
        #  setUpClass 保证了内核在跑
        self.assertIn("运行中", txt)

    def test_refresh_reports_page_count(self):
        k = KernelClient(port=PORT)
        sid = k.register(source="launcher-test")
        try:
            self.app._refresh()
            self.root.update()
            self.assertIn("在线页面: 1 个", self.app.pages_var.get())
        finally:
            k.unregister(sid)

    def test_launch_main_ui_records_child(self):
        self.app._launch("__main__")
        self.root.update()
        self.assertIn("main", self.app._children)
        proc = self.app._children["main"]
        self.assertIsInstance(proc, subprocess.Popen)

        # 收尾，别留孤儿；terminate + wait 才不会报 ResourceWarning
        def _reap():
            try:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=5)
            except Exception:
                pass
        self.addCleanup(_reap)

    def test_launch_unknown_key_is_harmless(self):
        """未知 key 不能崩，只在状态栏提示。"""
        self.app._launch("page:不存在的页面")
        self.root.update()
        # 不该抛异常；children 里也不该多出东西
        self.assertNotIn("不存在的页面", self.app._children)

    def test_on_close_stops_refresh_timer(self):
        """关闭后定时器必须停 —— 这是 invalid command name 噪音的回归。"""
        self.app._schedule_refresh()
        self.app._refresh()
        self.root.update()
        self.assertIsNotNone(self.app._after)

        self.app.on_close()

        self.assertTrue(self.app._closed)
        self.assertIsNone(self.app._after)

    def test_on_close_is_idempotent(self):
        self.app.on_close()
        self.app.on_close()          # 第二次不该抛异常
        self.assertTrue(self.app._closed)

    def test_on_close_keeps_kernel_alive(self):
        """关闭启动器不能顺手杀掉内核 —— 那是共享资产。"""
        self.assertTrue(kb.kernel_alive(PORT))
        self.app.on_close()
        self.root.update()
        # 内核应为探测留出时间
        deadline = time.time() + 5
        while time.time() < deadline and not kb.kernel_alive(PORT):
            time.sleep(0.2)
        self.assertTrue(kb.kernel_alive(PORT),
                        "启动器关闭时把内核也杀了")

    def test_on_close_reaps_pages(self):
        """关闭启动器应收尾它开启的页面。"""
        # 自己 spawn 页面进程并持有句柄，才能确保收尾时不留孤儿
        # （kb.launch_page 故意不返回句柄，页面要脱离父进程）
        env = dict(os.environ)
        env["HERMES_KERNEL_PORT"] = str(PORT)
        proc = subprocess.Popen(
            [sys.executable, "-u", str(kb.PAGE_ENTRY), "--page", "band"],
            cwd=str(ROOT), env=env, close_fds=True,
            creationflags=lc._detached_flags(),
            startupinfo=lc._hidden_startupinfo(),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL)
        self.addCleanup(_reap, proc)

        deadline = time.time() + 10
        while time.time() < deadline:
            if kb.list_pages(PORT):
                break
            time.sleep(0.3)
        self.assertTrue(kb.list_pages(PORT), "页面没注册成功")

        self.app.on_close()
        # 启动器已发 taskkill；再手动触发一次内核巡检，
        # 避免等 30 秒后台 reap 线程才把死亡页面从注册表里清掉。
        try:
            kb.reap(PORT)
        except Exception:
            pass
        deadline = time.time() + 10
        while time.time() < deadline:
            if not kb.list_pages(PORT):
                break
            time.sleep(0.3)
        self.assertEqual(kb.list_pages(PORT), [],
                         "启动器关闭后页面仍在跑")


class TestLauncherHelpers(unittest.TestCase):
    def test_detached_flags_on_windows(self):
        flags = lc._detached_flags()
        if os.name == "nt":
            self.assertNotEqual(flags, 0)
            self.assertTrue(flags & getattr(subprocess, "DETACHED_PROCESS", 0))
        else:
            self.assertEqual(flags, 0)

    def test_main_entry_exists(self):
        self.assertTrue(lc.MAIN_ENTRY.exists(),
                        f"找不到主 UI 入口: {lc.MAIN_ENTRY}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
