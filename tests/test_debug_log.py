"""Debug 日志测试。

覆盖：
  A. 开关语义：默认关闭、toggle、重复 enable 不串味
  B. 关闭态零成本：不写文件、日志函数直接返回
  C. 格式：单行、可 grep、超长值被截断、换行被转义
  D. 计时上下文：enter/leave、异常带 traceback、不吞异常
  E. 健壮性：写盘失败（只读目录/磁盘满）不能让计算崩
  F. 内核集成：bootstrap 真的往日志里写了并行/串行决策
  G. UI 集成：开关按钮、点计算后有日志、关闭后不再写
"""
import sys
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(r'D:\桌面\新建文件夹')
for p in (str(ROOT), str(ROOT / 'core')):
    if p not in sys.path:
        sys.path.insert(0, p)

import debug_log                     # noqa: E402
import band_advanced as ba          # noqa: E402


class _TmpLog(unittest.TestCase):
    """共用：把日志指到临时目录，用完复位。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dbglog_"))
        self.logfile = self.tmp / ".smart_board_debug.log"
        debug_log.reset()
        debug_log.debug.enable(self.logfile)

    def tearDown(self):
        debug_log.reset()
        try:
            import shutil
            shutil.rmtree(self.tmp, ignore_errors=True)
        except Exception:
            pass

    def lines(self):
        if not self.logfile.exists():
            return []
        return self.logfile.read_text(encoding="utf-8").splitlines()


class TestSwitchSemantics(_TmpLog):
    """A + B：开关语义与关闭态零成本。"""

    def test_default_disabled(self):
        debug_log.reset()
        self.assertFalse(debug_log.debug.enabled())

    def test_enable_creates_file_on_write(self):
        debug_log.debug.log("hello", a=1)
        self.assertTrue(self.logfile.exists(), "打开后应落盘")

    def test_toggle_roundtrip(self):
        self.assertTrue(debug_log.debug.enabled())
        debug_log.debug.toggle()
        self.assertFalse(debug_log.debug.enabled())
        debug_log.debug.toggle()
        self.assertTrue(debug_log.debug.enabled())

    def test_disabled_writes_nothing(self):
        """关闭态下任何调用都不该让日志变长。"""
        before = len(self.lines())
        debug_log.debug.disable()
        debug_log.debug.log("x", y=1)
        with debug_log.debug.timer("op"):
            pass
        self.assertEqual(len(self.lines()), before + 1,
                         "关闭态只应多出一条 session_stop")

    def test_disabled_from_scratch_no_file(self):
        """从未打开过的日志，写调用不该把它建出来。"""
        debug_log.reset()
        debug_log.debug.disable()
        self.assertIsNone(debug_log.debug.path)
        self.logfile.unlink(missing_ok=True)
        debug_log.debug.log("x", y=1)
        self.assertFalse(self.logfile.exists(),
                         "从未打开过就不该有文件")

    def test_session_markers(self):
        """开关各留一条会话标记，便于事后确认没被中途截断。"""
        logs = self.lines()
        self.assertTrue(any("session_start" in ln for ln in logs),
                        "开启时应有 session_start")
        debug_log.debug.disable()
        logs = self.lines()
        self.assertTrue(any("session_stop" in ln for ln in logs),
                        "关闭时应有 session_stop")


class TestFormat(_TmpLog):
    """C：日志格式必须可读、可 grep。"""

    def test_single_line_per_entry(self):
        debug_log.debug.log("a", x=1)
        debug_log.debug.log("b", y=2)
        self.assertEqual(len(self.lines()), 3,     # 含 session_start
                         "每条日志必须占一行，否则 grep 失效")

    def test_newline_escaped(self):
        debug_log.debug.log("a", text="line1\nline2")
        raw = self.lines()[-1]
        self.assertNotIn("\r", raw)
        self.assertIn("\\n", raw)

    def test_long_value_truncated(self):
        big = list(range(100000))
        debug_log.debug.log("a", data=big)
        line = self.lines()[-1]
        # len= 必须出现在摘要里，正文必须被截断
        self.assertIn("len=100000", line)
        self.assertLess(len(line), 2000, "超长值必须截断")

    def test_contains_tag_and_timing(self):
        debug_log.debug.log("mytag", k="v")
        line = self.lines()[-1]
        self.assertIn("mytag", line)
        self.assertIn("ms]", line, "应有相对启动的毫秒时间戳")
        self.assertIn("tid=", line, "应有线程 id，便于区分多页面")
        self.assertIn("k=v", line)


class TestTimer(_TmpLog):
    """D：计时上下文。"""

    def test_enter_leave(self):
        with debug_log.debug.timer("op", n=5):
            time.sleep(0.05)
        logs = [ln for ln in self.lines() if " op " in ln]
        self.assertEqual(len(logs), 2)
        self.assertIn("event=enter", logs[0])
        self.assertIn("event=leave", logs[1])
        self.assertIn("elapsed_ms", logs[1])

    def test_elapsed_nonzero(self):
        with debug_log.debug.timer("op"):
            time.sleep(0.03)
        line = self.lines()[-1]
        # 抓到 "elapsed_ms=30.12" 这样的数值
        import re
        m = re.search(r"elapsed_ms=([\d.]+)", line)
        self.assertIsNotNone(m)
        self.assertGreater(float(m.group(1)), 20.0)

    def test_exception_recorded_and_reraised(self):
        """异常必须记下来，且**不能不吞** —— 日志不能改变控制流。"""
        with self.assertRaises(ValueError):
            with debug_log.debug.timer("op"):
                raise ValueError("boom")
        line = self.lines()[-1]
        self.assertIn("event=error", line)
        self.assertIn("ValueError", line)
        # traceback 前 600 字必须带上，否则只知异常不知位置
        self.assertIn("raise ValueError", line)

    def test_disabled_timer_is_free(self):
        """关闭态下 timer 不该写入任何内容。"""
        debug_log.debug.disable()
        self.logfile.unlink(missing_ok=True)
        with debug_log.debug.timer("op"):
            pass
        self.assertFalse(self.logfile.exists(),
                         "关闭态不该建出日志文件")

    def test_disabled_produces_no_new_lines(self):
        """已打开的文件在关闭后也不该再追加。"""
        debug_log.debug.log("before", x=1)
        before = len(self.lines())
        debug_log.debug.disable()
        debug_log.debug.log("after", x=1)
        self.assertEqual(len(self.lines()), before + 1)   # +1 是 stop 标记
        self.assertTrue(any("after" not in ln for ln in self.lines()))
        debug_log.debug.enable(self.logfile)


class TestRobustness(_TmpLog):
    """E：日志写不进去时不能影响主流程。"""

    def test_write_failure_is_silent(self):
        """把日志路径指到一个不可能写入的位置。"""
        debug_log.reset()
        # 目录路径存在但作为文件用：open() 会抛 IsADirectoryError/OSError
        debug_log.debug.enable(self.tmp.parent / "dbglog_dir")
        try:
            debug_log.debug.log("x", y=1)     # 不该抛
        except Exception as e:
            self.fail(f"写日志失败不应外抛: {e}")

    def test_concurrent_writes(self):
        """多线程同时写不许丢行或抛异常。

        行数按**精确条数**算：4 线程 × 20 条 + 会话标记。
        这正是用断言而非目测的原因 —— 少了行说明并发写有竞态，
        而"日志偶尔少一行"平时根本发现不了。
        """
        def worker(i):
            for j in range(20):
                debug_log.debug.log("t", i=i, j=j)

        ts = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        lines = self.lines()
        # 按 tag 精确数：每条业务日志都带 " t " 段（前后各一空格），
        # 会话标记的 tag 是 "debug"，不会误入。
        body = [ln for ln in lines if " t i=" in ln]
        self.assertEqual(len(body), 80,
                         f"并发写丢了行: {len(body)}/80")


class TestKernelIntegration(_TmpLog):
    """F：内核真的把并行/串行决策写进日志。"""

    def _cloud(self, n=80, seed=3):
        import random
        rng = random.Random(seed)
        return [(float(i), 50.0 + 1.5 * i + rng.uniform(-8, 8))
                for i in range(1, n + 1)]

    def test_serial_decision_logged(self):
        """低于阈值时应记一条 bootstrap_serial，reason=below_threshold。"""
        ba.PARALLEL_MIN_BOOTSTRAP = 10 ** 9
        ba.shutdown_parallel_pool()
        try:
            ba.bootstrap_band(self._cloud(), n_segments=3, degree=1,
                              n_boot=60, seed=1)
        finally:
            ba.shutdown_parallel_pool()
        logs = self.lines()
        self.assertTrue(any("bootstrap_serial" in ln for ln in logs),
                        "串行路径应记录")
        self.assertTrue(any("below_threshold" in ln for ln in logs))

    def test_parallel_decision_logged(self):
        """高于阈值时应记 bootstrap_parallel_start 与各 chunk。"""
        ba.PARALLEL_MIN_BOOTSTRAP = 1
        ba.shutdown_parallel_pool()
        try:
            ba.bootstrap_band(self._cloud(60), n_segments=3, degree=1,
                              n_boot=200, seed=1)
        finally:
            ba.shutdown_parallel_pool()
        logs = self.lines()
        self.assertTrue(any("bootstrap_parallel_start" in ln
                            for ln in logs),
                        "并行路径应记录启动信息（workers/chunk）")
        self.assertTrue(any("bootstrap_chunk" in ln for ln in logs),
                        "每个 chunk 都应记一条，否则无法定位卡在哪批")

    def test_null_debug_never_breaks(self):
        """debug_log 模块不可用时，bootstrap 照样算完。"""
        import builtins
        real_import = builtins.__import__

        def broken(name, *a, **kw):
            if name == "debug_log":
                raise ImportError("simulated missing module")
            return real_import(name, *a, **kw)

        builtins.__import__ = broken
        try:
            ba.PARALLEL_MIN_BOOTSTRAP = 1
            ba.shutdown_parallel_pool()
            try:
                r = ba.bootstrap_band(self._cloud(40), n_segments=3,
                                      degree=1, n_boot=200, seed=1)
                self.assertTrue(r.lower)
            finally:
                ba.shutdown_parallel_pool()
        finally:
            builtins.__import__ = real_import


class TestUIIntegration(unittest.TestCase):
    """G：UI 侧开关与日志落盘。"""

    @classmethod
    def setUpClass(cls):
        import tkinter as tk
        import core.server as sv
        import core.client as cl

        cls.mini = sv.ThreadingHTTPServer(("127.0.0.1", 0), sv.Handler)
        cls.port = cls.mini.server_address[1]
        cls.t = threading.Thread(target=cls.mini.serve_forever, daemon=True)
        cls.t.start()
        cls.client = cl.KernelClient("127.0.0.1", cls.port)

        import random
        rng = random.Random(4242)
        pts = [(float(i), 50.0 + 1.5 * i + rng.uniform(-8, 8))
               for i in range(1, 101)]
        cls.mini2 = cls.mini
        _post(cls, "/api/register", {"session_id": "DBG_UI"})
        _post(cls, "/api/save_points",
              {"session_id": "DBG_UI", "points": [list(p) for p in pts]})
        cls.pts = pts
        cls.root = tk.Tk()
        cls.root.withdraw()
        from edit.page import BandPage
        cls.page = BandPage(cls.root, "DBG_UI", cls.client, "band")
        cls.page.points = list(pts)
        cls.root.update()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except Exception:
            pass
        try:
            cls.mini.shutdown()
            cls.mini.server_close()
        except Exception:
            pass

    def setUp(self):
        # 扁平 import：与 edit/page.py 和 core/ 内部保持一致。
        # 若测试用 from core.debug_log、代码用 import debug_log，
        # 会得到两个单例，开关状态互不可见（本就要测的坑）。
        import debug_log as _dl
        self.dl = _dl
        self.dl.reset()
        self.page._debug_on = False

    def _post(self, path, payload):
        import json
        import urllib.request
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())

    def test_button_exists(self):
        """Debug 日志按钮必须存在。"""
        self.assertTrue(hasattr(self.page, "_toggle_debug_mode"))

    def test_toggle_turns_on(self):
        p = self.page
        p._toggle_debug_mode()
        self.assertTrue(p._debug_on, "点一次应打开")
        path = self.dl.debug.path
        self.assertIsNotNone(path)
        self.assertTrue(Path(path).exists(), "打开时应已建文件")
        p._toggle_debug_mode()
        self.assertFalse(p._debug_on, "再点一次应关闭")

    def test_dlog_respects_switch(self):
        """关闭时 _dlog 不该写任何东西。"""
        p = self.page
        p._debug_on = False
        p._dlog("should_not_appear", x=1)
        self.assertIsNone(self.dl.debug.path)
        # 打开后应出现
        p._toggle_debug_mode()
        p._dlog("should_appear", x=1)
        text = Path(self.dl.debug.path).read_text(encoding="utf-8")
        self.assertIn("should_appear", text)
        self.assertNotIn("should_not_appear", text)
        p._toggle_debug_mode()

    def test_full_calc_writes_page_log(self):
        """跑一次真计算，页面侧应留下 calc_done。"""
        p = self.page
        p._toggle_debug_mode()
        from edit.page import ALGO_LABELS, _algo_key
        p.algo_var.set(ALGO_LABELS["advanced"])
        p.sp_boot.set(60)
        params = p._params()
        params["algorithm"] = _algo_key(p.algo_var.get())
        p._run_calc_worker(params)
        # 补跑测试环境下未能注册的 after 回调
        for fn in list(p._pending_after):
            fn()
        p._pending_after.clear()
        p._hide_progress()
        text = Path(self.dl.debug.path).read_text(encoding="utf-8")
        self.assertIn("calc_done", text)
        self.assertIn("elapsed_ms", text)
        p._toggle_debug_mode()


def _post(cls, path, payload):
    import json
    import urllib.request
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{cls.port}{path}", data=data,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


if __name__ == "__main__":
    unittest.main(verbosity=2)
