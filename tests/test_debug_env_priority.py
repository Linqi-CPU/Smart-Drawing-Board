"""_setup_debug_logging 的开关优先级测试。

为什么值得测：exe 里排查"为什么慢"全靠这条链路。
如果环境变量被忽略，spawn 出去的 bootstrap 子进程就没有日志，
而你看到的现象是"父进程有日志、并行结果却看不出对错" ——
不会有人联想到"子进程没开日志"。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(r'D:\桌面\新建文件夹')
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'core'))


class TestDebugEnvPriority(unittest.TestCase):

    def setUp(self):
        # **不要** pop 已导入的模块。pop 会让后续 import 得到
        # 一个全新的模块对象 + 全新单例，而其他测试文件早已
        # import 并持有旧对象 —— 从此一个进程里有两个 `debug`，
        # 一边 enable 到临时文件，另一边还在旧状态。
        # 实测：pop 之后反序跑 test_debug_log 就两条失败。
        # 一律复用同一个模块对象，绝不 pop。
        import server                      # 已导入过则复用，不 pop
        import debug_log as dl
        self.server = server
        self.debug_log = dl
        # 复位到"未配置"状态，再按用例打开
        dl.reset()
        dl.debug.disable()
        self._saved = os.environ.pop("HERMES_DEBUG_LOG", None)

    def tearDown(self):
        self.debug_log.debug.disable()
        self.debug_log.reset()
        if self._saved is not None:
            os.environ["HERMES_DEBUG_LOG"] = self._saved
        else:
            os.environ.pop("HERMES_DEBUG_LOG", None)

    def test_env_beats_flag(self):
        """环境变量里给了路径，就该用它，无视 --debug 参数。

        spawn 子进程只继承环境变量，看不到命令行。父进程用
        环境变量是唯一能让两侧同时记日志的办法。
        """
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "env-wins.log"
            os.environ["HERMES_DEBUG_LOG"] = str(target)
            self.server._setup_debug_logging("ignored-by-env.log")
            self.assertTrue(self.debug_log.debug.enabled())
            self.assertEqual(self.debug_log.debug.path, target)
            self.assertIn("debug", target.read_text(encoding="utf-8"))

    def test_env_off_disables(self):
        """HERMES_DEBUG_LOG=0 显式关闭，即使 --debug 也无效。"""
        os.environ["HERMES_DEBUG_LOG"] = "0"
        self.server._setup_debug_logging("")
        self.assertFalse(self.debug_log.debug.enabled())

    def test_no_env_no_flag_disables(self):
        """两个都没有 = 关闭。关闭态必须零成本。"""
        os.environ.pop("HERMES_DEBUG_LOG", None)
        self.server._setup_debug_logging(None)
        self.assertFalse(self.debug_log.debug.enabled())

    def test_flag_alone_enables(self):
        """没环境变量时，--debug 单独能开。"""
        os.environ.pop("HERMES_DEBUG_LOG", None)
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "flag.log"
            self.server._setup_debug_logging(str(target))
            self.assertTrue(self.debug_log.debug.enabled())
            self.assertEqual(self.debug_log.debug.path, target)

    def test_flag_empty_uses_default(self):
        """--debug 不带路径（const=""）时应落到默认位置。"""
        os.environ.pop("HERMES_DEBUG_LOG", None)
        self.server._setup_debug_logging("")
        self.assertTrue(self.debug_log.debug.enabled())
        self.assertIsNotNone(self.debug_log.debug.path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
