"""关闭与回收生命周期的回归测试。

核心约束（这些测试锁定的就是用户明确要求的语义）：
1. 关闭一个页面，只清它自己的状态与文件，**绝不影响其他页面**
2. 页面进程退出后，内核能回收它的残留（含被强杀、没机会清理的情况）
3. 页面自己落盘的文件在关闭时也要被删掉
"""

import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import session as ses          # noqa: E402
from core.client import KernelClient     # noqa: E402

#: 测试连的内核端口。与 test_kernel_integration 读同一个环境变量，
#: 默认 8765（与正式端口一致），避免两套测试连不同内核。
PORT = int(os.environ.get("HERMES_TEST_KERNEL_PORT", "8765"))
IMG = ses.image_dir()
REP = ses.report_dir()


def _pts(n: int = 20):
    return [[i, float(i)] for i in range(1, n + 1)]


class TestSessionReaper(unittest.TestCase):
    """纯内存测试：不需要内核进程。"""

    def setUp(self):
        self.reg = ses.SessionRegistry()

    def test_drop_removes_page_files_only(self):
        """只删本页面的文件，绝不碰其他页面的。"""
        IMG.mkdir(parents=True, exist_ok=True)
        p = self.reg.register("p_a")
        f1 = IMG / f"band_{p.session_id}.png"
        f2 = IMG / f"band_{p.session_id}_dev.png"
        other = IMG / "band_p_someoneelse.png"
        # 建真文件，测的是实际删除行为而不是路径集合
        for f in (f1, f2, other):
            f.write_bytes(b"x")

        dropped = self.reg.drop(p.session_id)
        self.assertTrue(dropped)

        # 本页面的两个文件必须被删掉
        self.assertFalse(f1.exists())
        self.assertFalse(f2.exists())
        # 别人的文件必须原样保留
        self.assertTrue(other.exists())

        other.unlink(missing_ok=True)

    def test_reap_by_pid_keeps_alive_pages(self):
        """已死的 PID 被回收，活着的 PID 一律保留。"""
        a = self.reg.register("p_alive")
        b = self.reg.register("p_dead")
        self.reg.get(a.session_id).pid = os.getpid()   # 自己：确定活着
        self.reg.get(b.session_id).pid = 999999        # 不存在：确定死了
        reaped = self.reg.reap_dead(alive_pids={os.getpid()})
        self.assertEqual(reaped, ["p_dead"])
        self.assertIsNotNone(self.reg.get("p_alive"))

    def test_reap_by_closing_grace(self):
        """closing 标记后要等宽限期，不会立刻被误删。"""
        a = self.reg.register("p_closing")
        self.reg.mark_closing("p_closing")
        # 刚标记：宽限期内不该被杀
        self.assertEqual(self.reg.reap_dead(), [])
        # 手动把 updated_at 推到很久以前，模拟宽限期已过
        self.reg.get("p_closing").updated_at -= 3600
        self.assertEqual(self.reg.reap_dead(), ["p_closing"])

    def test_reap_idle_without_pid(self):
        """没有 PID 的页面走空闲规则，不会只因没 PID 就被杀。"""
        self.reg.register("p_nopid")
        self.assertEqual(self.reg.reap_dead(), [])
        self.reg.get("p_nopid").updated_at -= 99 * 3600
        self.assertEqual(self.reg.reap_dead(), ["p_nopid"])

    def test_registry_to_dict_exposes_pid(self):
        self.reg.register("p_x")
        self.reg.get("p_x").pid = 4242
        d = self.reg.get("p_x").to_dict()
        self.assertEqual(d["pid"], 4242)
        self.assertFalse(d["closing"])

    def test_normalize_rejects_bad_ids(self):
        for bad in ("", "   ", None, 123, "a" * 200):
            with self.assertRaises(ValueError):
                ses._normalize_session_id(bad)


class TestProductDirs(unittest.TestCase):
    """产物按类型分目录：图片进 pictures，文本进 texts。"""

    def test_images_go_to_pictures(self):
        for name in ("a.png", "a.PNG", "a.jpg", "a.svg", "a.webp"):
            self.assertEqual(ses.dir_for(name).name, "pictures")

    def test_texts_go_to_texts(self):
        for name in ("a.txt", "a.csv", "a.log", "a.json", "a"):
            self.assertEqual(ses.dir_for(name).name, "texts")

    def test_root_is_configurable_and_drives_children(self):
        """换根目录时，两个分类目录必须跟着走。"""
        import importlib
        old = os.environ.get("HERMES_KERNEL_OUTPUT_ROOT")
        os.environ["HERMES_KERNEL_OUTPUT_ROOT"] = r"D:\somewhere\else"
        try:
            importlib.reload(ses)
            self.assertEqual(ses.output_root(), Path(r"D:\somewhere\else"))
            self.assertTrue(str(ses.image_dir()).startswith(r"D:\somewhere\else"))
            self.assertTrue(str(ses.report_dir()).startswith(r"D:\somewhere\else"))
            self.assertEqual(ses.image_dir().name, "pictures")
            self.assertEqual(ses.report_dir().name, "texts")
        finally:
            if old is None:
                os.environ.pop("HERMES_KERNEL_OUTPUT_ROOT", None)
            else:
                os.environ["HERMES_KERNEL_OUTPUT_ROOT"] = old
            importlib.reload(ses)

    def test_defaults_not_on_c_drive(self):
        """默认产物目录不能在系统盘上。"""
        root = str(ses.output_root()).lower()
        self.assertFalse(root.startswith("c:"),
                         f"产物不该默认放 C 盘: {root}")


class TestCleanupIsolation(unittest.TestCase):
    """端到端：关一个页面，其他页面必须完好无损。"""

    @classmethod
    def setUpClass(cls):
        cls.k = KernelClient(port=PORT)
        cls.k.ping()

    def test_closing_one_page_leaves_others_intact(self):
        a = self.k.register(source="edit:band")
        b = self.k.register(source="edit:band")
        self.k.save_points(a, _pts())
        self.k.save_points(b, _pts())
        self.k.band_fit(a)
        self.k.band_fit(b)
        pa = self.k.render_band(a)
        pb = self.k.render_band(b)
        fa, fb = Path(pa), Path(pb)
        self.assertTrue(fa.exists() and fb.exists())

        # ---- 关闭 a ----
        self.k.mark_closing(a)
        res = self.k.cleanup(a)

        self.assertTrue(res["dropped"])
        self.assertIn(str(fa), res["removed_files"])
        # a 的图片与报告都没了
        self.assertFalse(fa.exists())
        # b 的东西一样没动
        self.assertTrue(fb.exists())

        pages = {p["session_id"] for p in self.k.sessions()}
        self.assertNotIn(a, pages)
        self.assertIn(b, pages)

        # b 仍然完全可用
        pb2 = self.k.render_band(b)
        self.assertTrue(Path(pb2).exists())

        # 收尾：把 b 也清掉，避免影响其他测试
        self.k.cleanup(b)

    def test_reap_collects_pidless_dead_page(self):
        """上报一个不存在的 PID，内核按 PID 判定死亡并回收。"""
        sid = self.k.register(source="edit:band")
        self.k.report_pid(sid, 999999)
        self.k.save_points(sid, _pts())
        self.k.band_fit(sid)
        png = self.k.render_band(sid)
        self.assertTrue(Path(png).exists())

        reaped = self.k.reap()
        self.assertIn(sid, reaped)
        pages = {p["session_id"] for p in self.k.sessions()}
        self.assertNotIn(sid, pages)
        # 文件也被收掉了
        self.assertFalse(Path(png).exists())

    def test_cleanup_unknown_session_is_harmless(self):
        """清理一个不存在的编号不该报错，也不能影响别人。"""
        r = self.k.cleanup("p_nonexistent")
        self.assertFalse(r["dropped"])
        self.assertEqual(r["removed_files"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
