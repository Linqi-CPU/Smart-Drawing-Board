"""内核 HTTP 服务的集成测试。

用真实起服务 + 真实 HTTP 调用的方式验证端到端行为，
不 mock 任何一层。
"""

import json
import os
import random
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import session as ses  # noqa: E402
from core.client import KernelClient, KernelClientError  # noqa: E402
import kernel_bridge  # noqa: E402

#: 集成测试连的内核端口。可用环境变量覆盖：
#:   HERMES_TEST_KERNEL_PORT=8801 pytest tests/test_kernel_integration.py
#: 默认 8765（默认端口）。注意：内核必须是**当前代码**起的，
#: 否则测到的是旧行为。
TEST_PORT = int(os.environ.get("HERMES_TEST_KERNEL_PORT", "8765"))


def _points(n=50, seed=3):
    rng = random.Random(seed)
    return [(float(i), 50.0 + 1.5 * i + rng.uniform(-8, 8))
            for i in range(1, n + 1)]


@unittest.skipUnless(
    kernel_bridge.kernel_alive(TEST_PORT),
    f"内核服务未在端口 {TEST_PORT} 运行。"
    f"请先运行 python -m core.server --port {TEST_PORT}",
)
class TestKernelHTTP(unittest.TestCase):
    """依赖真实内核服务的集成测试。"""

    @classmethod
    def setUpClass(cls):
        cls.client = KernelClient(port=TEST_PORT)
        cls.sid = cls.client.register(source="integration-test")
        # 内核按 session 隔离状态：必须先存点才能算
        cls.client.save_points(cls.sid, _points(50), source="setup")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.client.unregister(cls.sid)
        except KernelClientError:
            pass

    # ---------------- 会话 ----------------
    def test_ping(self):
        self.assertTrue(self.client.ping())

    def test_register_is_idempotent(self):
        sid1 = self.client.register("p_test_a")
        sid2 = self.client.register("p_test_a")
        self.assertEqual(sid1, sid2)
        self.client.unregister("p_test_a")

    def test_registry_rejects_bad_ids(self):
        """明确该拒绝的输入必须报错。"""
        for bad in ("", "   ", None, 123, "a" * 200):
            with self.assertRaises(ValueError):
                ses._normalize_session_id(bad)

    def test_registry_sanitizes_injection(self):
        """含路径分隔符的输入应被清洗成安全串，而不是报错。

        这是有意设计：页面可能把文件名之类的东西嵌进编号，
        清洗后仍得到一个可用的唯一键。
        """
        out = ses._normalize_session_id("../../etc/passwd")
        self.assertEqual(out, "etcpasswd")
        self.assertNotIn("/", out)
        self.assertNotIn("..", out)

    def test_session_id_format(self):
        sid = ses.new_session_id()
        self.assertTrue(sid.startswith("p_"))
        self.assertEqual(len(sid), 10)

    def test_sessions_lists_registered(self):
        sessions = self.client.sessions()
        ids = [s["session_id"] for s in sessions]
        self.assertIn(self.sid, ids)

    # ---------------- 数据 ----------------
    def test_save_and_get_points(self):
        pts = _points(20)
        n = self.client.save_points(self.sid, pts, source="t")
        self.assertEqual(n, 20)
        got = self.client.get_points(self.sid)
        self.assertEqual(len(got), 20)
        self.assertAlmostEqual(got[0][1], pts[0][1])

    def test_bad_points_rejected(self):
        with self.assertRaises(KernelClientError):
            self.client.save_points(self.sid, [("a", "b")])
        with self.assertRaises(KernelClientError):
            self.client.save_points(self.sid, ["not a point"])

    def test_import_table_csv(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.csv"
            p.write_text("x1,x2,y\n" + "".join(
                f"{float(i)},{30.0+i*i},{5.0+2.0*i}\n"
                for i in range(1, 16)), encoding="utf-8")
            info = self.client.import_table(self.sid, str(p))
            self.assertEqual(info["n_vars"], 2)
            self.assertEqual(info["target"], "y")
            self.assertEqual(info["variables"], ["x1", "x2"])

    def test_import_missing_file(self):
        with self.assertRaises(KernelClientError) as ctx:
            self.client.import_table(self.sid, "Z:\\no_such_file_zz.xlsx")
        self.assertIn("不存在", str(ctx.exception))

    # ---------------- 计算 ----------------
    def test_band_fit_recovers_trend(self):
        res = self.client.band_fit(self.sid, n_segments=8, degree=1)
        self.assertIn("x", res["expression"])
        self.assertAlmostEqual(res["coefficients"][1], 1.5, delta=0.1)
        self.assertAlmostEqual(res["coefficients"][0], 50.0, delta=3.0)
        self.assertGreater(res["r2"], 0.9)
        self.assertIn("scatter", res)
        self.assertIn("segments", res)
        self.assertEqual(len(res["segments"]), res["n_segments"])

    def test_band_fit_scatter_fields(self):
        res = self.client.band_fit(self.sid, n_segments=8, degree=1)
        s = res["scatter"]
        for key in ("d_values", "spreads", "mean_d", "min_d", "max_d",
                    "tail_ratio", "trend", "description"):
            self.assertIn(key, s)
        self.assertGreaterEqual(s["min_d"], 0.0)
        self.assertLessEqual(s["min_d"], s["max_d"])
        self.assertIn(s["trend"],
                      ("narrowing", "stable", "widening", "mixed"))

    def test_band_fit_too_few_points(self):
        with self.assertRaises(KernelClientError) as ctx:
            self.client.band_fit(self.sid, points=[(1.0, 1.0)])
        self.assertIn("至少", str(ctx.exception))

    def test_band_fit_single_point_segment_error(self):
        with self.assertRaises(KernelClientError):
            self.client.band_fit(self.sid, points=[(1.0, 1.0), (2.0, 2.0)],
                                 n_segments=50)

    def test_poly_fit(self):
        res = self.client.poly_fit(self.sid, degree=1)
        self.assertIn("x", res["expression"])
        # 数据噪声 ±8、真实 R² ≈ 0.9 附近，普通最小二乘比分段包络略低
        self.assertGreater(res["r2"], 0.8)

    def test_dev_series_nonneg(self):
        """离散宽度必须恒非负。

        用自己的独立 session 而不用 self.sid：前面的测试会往共享
        session 里塞异常点集（单点、越界参数），那些是测校验用的，
        会让这里的区间退化，测的就不是算法本身了。
        """
        sid = self.client.register(source="test-dev-series")
        try:
            self.client.save_points(sid, _points(50), source="setup")
            self.client.band_fit(sid)
            ds = self.client.dev_series(sid, step=5)
        finally:
            self.client.unregister(sid)

        self.assertTrue(ds["d"])
        for d in ds["d"]:
            self.assertGreaterEqual(d, 0.0)

    def test_band_series_ordering(self):
        bs = self.client.band_series(self.sid, step=5)
        self.assertEqual(len(bs["xs"]), len(bs["upper"]))
        for u, l in zip(bs["upper"], bs["lower"]):
            self.assertGreaterEqual(u, l)

    def test_bad_params_rejected(self):
        with self.assertRaises(KernelClientError):
            self.client.band_fit(self.sid, n_segments=0)
        with self.assertRaises(KernelClientError):
            self.client.band_fit(self.sid, degree=99)

    # ---------------- 渲染与报告 ----------------
    def test_render_band_png(self):
        path = self.client.render_band(self.sid, n_segments=8,
                                       degree=1, step=3)
        self.assertTrue(os.path.exists(path), path)
        self.assertGreater(os.path.getsize(path), 5000)

    def test_report_created(self):
        path = self.client.report(self.sid)
        self.assertTrue(os.path.exists(path), path)
        text = Path(path).read_text(encoding="utf-8")
        self.assertIn("分段包络估计报告", text)
        self.assertIn("整体走势", text)
        self.assertIn("离散程度", text)
        self.assertIn(self.sid, text)

    def test_note_roundtrip(self):
        self.client.save_points(self.sid, _points(20))
        self.client.band_fit(self.sid, n_segments=8, degree=1)
        notes = self.client.note(self.sid, "集成测试消息")
        self.assertIn("集成测试消息", notes)


class TestKernelContract(unittest.TestCase):
    """不依赖活服务：只验证协议层面的错误处理。"""

    def test_unknown_endpoint_raises(self):
        client = KernelClient()
        with self.assertRaises(KernelClientError):
            client.post("/api/does_not_exist", {})

    def test_offline_client_raises_friendly(self):
        client = KernelClient(port=1)   # 65535 端口一定没人监听
        with self.assertRaises(KernelClientError) as ctx:
            client.band_fit("p_x", points=[(1.0, 2.0)])
        self.assertIn("连不上内核", str(ctx.exception))

    def test_missing_session_id(self):
        client = KernelClient()
        if not kernel_bridge.kernel_alive(8765):
            self.skipTest("内核未运行")
        with self.assertRaises(KernelClientError):
            client.post("/api/band_fit", {"points": [[1, 2], [3, 4]]})


class TestBridge(unittest.TestCase):
    def test_launchers_registered(self):
        self.assertIn("band", kernel_bridge.LAUNCHERS)

    def test_kernel_alive_probe(self):
        # 无论内核是否在跑，探测本身不应抛异常
        result = kernel_bridge.kernel_alive(8765)
        self.assertIsInstance(result, bool)

    def test_status_text_no_crash(self):
        txt = kernel_bridge.status_text(8765)
        self.assertIsInstance(txt, str)
        self.assertTrue(txt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
