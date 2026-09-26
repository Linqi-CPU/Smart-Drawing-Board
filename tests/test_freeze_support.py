"""freeze_support() 的位置测试：必须在 main() 之前、__main__ 块里。

为什么值得一个测试：这个 bug 在源码模式下**永远测不出来**。
spawn 的子进程是重新执行 exe，源码模式 spawn 的是 python + 模块路径，
走的是完全不同的分支。所以只能靠静态检查源码里的顺序。

如果顺序错了会怎样：spawn 子进程先跑 main()，起一个 ThreadingHTTPServer、
抢 8765 端口，然后再被 multiprocessing 当成子进程拉起 ——
表现为"界面卡住"或"内核起不来"。这类症状极难回溯到"少了一行 freeze_support"。
"""
import inspect
import os
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(r'D:\桌面\新建文件夹')
sys.path.insert(0, str(ROOT))
KERNEL = ROOT / "core" / "server.py"
html = KERNEL.read_text(encoding="utf-8")


class TestFreezeSupportOrder(unittest.TestCase):

    def test_has_freeze_support(self):
        """入口必须调 freeze_support。

        FreeInstaller spawn 的子进程会重新执行 Kernel exe 入口。
        Windows + spawn 下，没有 freeze_support() 子进程会把 main()
        完整跑一遍（起 HTTP 服务、抢端口、再 spawn）。
        """
        self.assertIn(
            "multiprocessing.freeze_support()", html,
            "core/server.py 入口没有 freeze_support() —— "
            "exe 里 spawn 子进程会递归执行 main()")

    def test_freeze_support_before_main_call(self):
        """必须在 main() 之前。

        反了就白做：freeze_support() 检测"我是不是 spawn 的子进程"，
        如果是就直接进入子进程逻辑并 sys.exit。放在 main() 之后
        的话，main() 已经先把端口占了、线程起了，才轮到它 —— 晚了。
        """
        pos_freeze = html.find("multiprocessing.freeze_support()")
        self.assertGreater(pos_freeze, 0, "没找到 freeze_support 调用")
        # main() 的实际调用（不是 def main）
        m = re.search(r'^\s*raise SystemExit\(main\(\)\)', html,
                      re.MULTILINE)
        self.assertIsNotNone(m, "入口没找到 main() 调用")
        self.assertLess(
            pos_freeze, m.start(),
            "freeze_support() 在 main() 之后执行，等于没加")

    def test_freeze_support_inside_main_guard(self):
        """必须在 `if __name__ == \"__main__\"` 块内。

        放模块顶层会在内核被别的脚本 import 时也执行 ——
        那时 sys.argv 里没有 spawn 的魔术参数，无害，但语义错了：
        freeze_support 只给"作为程序的入口"用。

        注意正则：`(.*?)$` 配 DOTALL 只抓到一行，要
        `(.*)` 配 DOTALL 才会吃掉整个尾部。
        """
        m = re.search(
            r'if __name__ == "__main__":\s*\n(.*)', html,
            re.DOTALL)
        self.assertIsNotNone(m)
        block = m.group(1)
        self.assertIn("multiprocessing.freeze_support()", block,
                      "freeze_support() 不在 __main__ 块内")

    def test_no_fork_context_anywhere(self):
        """全项目不能出现 get_context("fork") 或 Pool()。

        Windows 没有 fork。core/band_advanced.py 显式写了 spawn，
        这里守住的是"将来有人图省事改成 fork"。
        fork 在 Windows 上跑不了，症状是
        OSError/AttributeError 而不是清晰的"不支持"。
        """
        bad = []
        for p in ROOT.rglob("*.py"):
            if any(seg in p.parts for seg in
                   (".git", "__pycache__", "tests", "vendor",
                    "build", "dist")):
                continue
            try:
                txt = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            for pat in ('get_context("fork")', "get_context('fork')",
                        "mp.get_context(\"fork\")"):
                if pat in txt:
                    bad.append(f"{p}: {pat}")
        self.assertEqual(bad, [], f"发现 fork 启动方式，Windows 不支持: {bad}")

    def test_band_advanced_uses_spawn(self):
        """band_advanced 必须显式 spawn。

        fork 在 Windows 上不存在，不写清楚的话
        将来换平台时"莫名失败"的原因能拖很久才找到。
        """
        ba = (ROOT / "core" / "band_advanced.py").read_text(
            encoding="utf-8", errors="replace")
        self.assertIn('get_context("spawn")', ba)


class TestParallelFallbackSignature(unittest.TestCase):
    """并行回落必须被记进 debug 日志，否则线上降级无人知晓。

    这条针对 band_advanced._cpu_fit_batch 的 except 分支：
    并行失败静默回落是对的（不能让加速失败影响结果），
    但"静默"不能等于"无声"。
    """

    def setUp(self):
        sys.path.insert(0, str(ROOT))
        sys.path.insert(0, str(ROOT / "core"))
        for m in list(sys.modules):
            if m in ("band_advanced", "band_fit", "fitting"):
                del sys.modules[m]
        import band_advanced
        self.ba = band_advanced

    def test_fallback_logged(self):
        """并行抛异常时必须写 bootstrap_fallback 日志。"""
        calls = []
        real_dbg = self.ba._dbg if hasattr(self.ba, "_dbg") else None
        self.ba._dbg = lambda: type("D", (), {
            "log": lambda s, tag, **kw: calls.append((tag, kw))
        })()
        try:
            samples = [[1.0, 2.0, 3.0]] * 200
            # 用假的 _parallel_fit 抛异常，触发 except 分支
            orig = self.ba._parallel_fit

            def boom(*a, **kw):
                raise RuntimeError("池起不来")

            self.ba._parallel_fit = boom
            try:
                self.ba._cpu_fit_batch(
                    samples, n_segments=2, degree=1,
                    fitter=lambda *a, **k: None, total=200,
                    report=lambda a, b: None)
            finally:
                self.ba._parallel_fit = orig
        finally:
            if real_dbg is not None:
                self.ba._dbg = real_dbg
        tags = [t for t, _ in calls]
        self.assertIn("bootstrap_fallback", tags,
                      "并行失败没写 bootstrap_fallback 日志 —— "
                      "线上降级了没人知道")
        kw = next(k for t, k in calls if t == "bootstrap_fallback")
        self.assertIn("reason", kw)
        self.assertEqual(kw.get("workers_attempted"), True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
