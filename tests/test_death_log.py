"""内核死亡取证日志的行为测试。

锁定三件事：
1. 启动时写 START 基线（含 pid / ppid / job 归属 / argv）
2. 正常退出 / SystemExit 必须写 EXIT（证明钩子链路是通的）
3. taskkill /F（SIGKILL）确实没有记录 —— 这是平台盲区，
   测试把它固定下来，防止以后有人误以为钩子能抓到

job_info() 的单测也在这里：它返回的字符串必须能看出
"是否在 Job 里"，因为这是内核跟着 session 消失的头号嫌疑。
"""

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import server as srv          # noqa: E402


def _death_log_path(root: Path) -> Path:
    return root / ".kernel_death.log"


class TestJobInfo(unittest.TestCase):
    """Job 归属探测：内核'凭空消失'的头号嫌疑。"""

    def test_returns_descriptive_string(self):
        info = srv._job_info()
        self.assertIsInstance(info, str)
        # 必须能看出是否在 Job 里
        self.assertTrue(
            ("no-job" in info) or ("member=1" in info) or ("failed" in info),
            f"无法从返回值判断 Job 归属: {info}")

    def test_never_raises(self):
        """探测失败也不能让内核起不来。"""
        try:
            srv._job_info()
        except Exception as e:               # pragma: no cover
            self.fail(f"_job_info 抛了异常: {e}")

    def test_parent_pid(self):
        p = srv._parent_pid()
        # Windows 下可能拿不到，但拿到了必须是正整数
        if p is not None:
            self.assertIsInstance(p, int)
            self.assertGreater(p, 0)


class TestDeathLogger(unittest.TestCase):
    """取证钩子的实际行为。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        # 把产物根指到临时目录，日志就写到这里，不污染真目录
        self._saved = os.environ.get("HERMES_KERNEL_OUTPUT_ROOT")
        os.environ["HERMES_KERNEL_OUTPUT_ROOT"] = str(self.tmp)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("HERMES_KERNEL_OUTPUT_ROOT", None)
        else:
            os.environ["HERMES_KERNEL_OUTPUT_ROOT"] = self._saved
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _spawn(self, code: str) -> subprocess.Popen:
        env = dict(os.environ)
        env["HERMES_KERNEL_OUTPUT_ROOT"] = str(self.tmp)
        p = subprocess.Popen(
            [sys.executable, "-c", code],
            cwd=str(ROOT), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.PIPE)
        self.addCleanup(p.stdin.close)     # 否则报 ResourceWarning
        self.addCleanup(lambda: p.poll() is None and p.kill())
        return p

    def test_start_records_baseline(self):
        """启动就必须留下 pid/ppid/job/argv。"""
        code = (
            "import sys, time; sys.path.insert(0, r'%s'); "
            "from core import server as srv; "
            "srv._install_death_logger(0); "
            "time.sleep(1.0)" % ROOT
        )
        p = self._spawn(code)
        p.wait(timeout=20)
        log = _death_log_path(self.tmp)
        self.assertTrue(log.exists(), "没生成死亡日志")
        text = log.read_text(encoding="utf-8", errors="replace")
        self.assertIn("START", text)
        self.assertIn(f"pid={p.pid}", text)
        self.assertIn("ppid=", text)
        self.assertIn("job=", text)
        self.assertIn("argv=", text)

    def test_normal_exit_records_exit(self):
        """干净退出必须记 EXIT —— 证明钩子链路是通的。"""
        code = (
            "import sys; sys.path.insert(0, r'%s'); "
            "from core import server as srv; "
            "srv._install_death_logger(0); "
            "raise SystemExit(0)" % ROOT
        )
        p = self._spawn(code)
        p.wait(timeout=20)
        text = _death_log_path(self.tmp).read_text(
            encoding="utf-8", errors="replace")
        self.assertIn("START", text)
        self.assertIn("EXIT", text)

    def test_uncaught_exception_records_crash(self):
        """未捕获异常必须记 traceback。"""
        code = (
            "import sys; sys.path.insert(0, r'%s'); "
            "from core import server as srv; "
            "srv._install_death_logger(0); "
            "raise ValueError('内核故意崩给你看')" % ROOT
        )
        p = self._spawn(code)
        p.wait(timeout=20)
        text = _death_log_path(self.tmp).read_text(
            encoding="utf-8", errors="replace")
        self.assertIn("CRASH", text)
        self.assertIn("内核故意崩给你看", text)

    def test_kill_f9_leaves_no_exit_record(self):
        """taskkill /F 是 SIGKILL：确实抓不到，这是平台盲区。

        把这个事实固定下来，防止以后有人以为钩子能抓到强杀。
        注意：EXIT 不出现，但 START 一定在（启动基线先写好）。
        """
        code = (
            "import sys, time; sys.path.insert(0, r'%s'); "
            "from core import server as srv; "
            "srv._install_death_logger(0); "
            "time.sleep(30)" % ROOT
        )
        p = self._spawn(code)
        time.sleep(2.0)
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(p.pid)],
                           capture_output=True)
        else:                                  # pragma: no cover
            p.kill()
        p.wait(timeout=20)
        text = _death_log_path(self.tmp).read_text(
            encoding="utf-8", errors="replace")
        self.assertIn("START", text)
        # 关键断言：强杀没有留下 EXIT / SIGNAL / CRASH
        for marker in ("EXIT", "SIGNAL", "CRASH"):
            self.assertNotIn(marker, text,
                             f"强杀后出现了 {marker}，与平台行为不符")


if __name__ == "__main__":
    unittest.main(verbosity=2)
