"""graph_engine 与 custom_loader 的单元测试。

运行: python -m unittest discover -s tests -v
"""

import math
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import graph_engine as ge
from core import custom_loader as cl


class TestEvaluate(unittest.TestCase):
    def test_sine_endpoints(self):
        w, cy, amp, freq = 100.0, 50.0, 10.0, 1.0
        self.assertAlmostEqual(ge.evaluate(ge.SINE, None, 0, w, cy, amp, freq), cy)
        # 内置公式 sin(freq*pi*x/width*2)：freq=1 时在 x=w/4 达峰、x=w/2 回中
        self.assertAlmostEqual(
            ge.evaluate(ge.SINE, None, w / 4, w, cy, amp, freq), cy + amp
        )
        self.assertAlmostEqual(
            ge.evaluate(ge.SINE, None, w / 2, w, cy, amp, freq), cy
        )

    def test_cosine_start_is_peak(self):
        w, cy, amp, freq = 100.0, 50.0, 10.0, 1.0
        self.assertAlmostEqual(
            ge.evaluate(ge.COSINE, None, 0, w, cy, amp, freq), cy + amp
        )

    def test_parabola_minimum_at_center(self):
        w, cy, amp = 100.0, 50.0, 30.0
        self.assertAlmostEqual(ge.evaluate(ge.PARABOLA, None, 50, w, cy, amp, 1), cy)
        self.assertAlmostEqual(
            ge.evaluate(ge.PARABOLA, None, 0, w, cy, amp, 1), cy + amp
        )

    def test_linear_is_center(self):
        self.assertEqual(
            ge.evaluate(ge.LINEAR, None, 42, 100, 50, 10, 2), 50.0
        )

    def test_custom_complex_takes_real_part(self):
        def cplx(x, width, center_y, amp, freq):
            return complex(center_y + 5, 99)

        self.assertEqual(
            ge.evaluate(ge.CUSTOM, cplx, 0, 100, 50, 1, 1), 55.0
        )

    def test_custom_nan_falls_back_to_center(self):
        def bad(x, width, center_y, amp, freq):
            return float("nan")

        self.assertEqual(ge.evaluate(ge.CUSTOM, bad, 0, 100, 50, 1, 1), 50.0)

    def test_custom_inf_falls_back_to_center(self):
        def bad(x, width, center_y, amp, freq):
            return float("inf")

        self.assertEqual(ge.evaluate(ge.CUSTOM, bad, 0, 100, 50, 1, 1), 50.0)

    def test_unknown_type_raises(self):
        with self.assertRaises(ValueError):
            ge.evaluate("nope", None, 0, 100, 50, 1, 1)

    def test_zero_width_raises(self):
        with self.assertRaises(ValueError):
            ge.evaluate(ge.SINE, None, 0, 0, 50, 1, 1)

    def test_custom_without_func_raises(self):
        with self.assertRaises(ValueError):
            ge.evaluate(ge.CUSTOM, None, 0, 100, 50, 1, 1)


class TestSampleX(unittest.TestCase):
    def test_covers_both_ends(self):
        xs = ge.sample_x(700, 2)
        self.assertEqual(xs[0], 0.0)
        self.assertEqual(xs[-1], 700.0)

    def test_step_respected(self):
        xs = ge.sample_x(10, 3)
        self.assertEqual(xs, [0.0, 3.0, 6.0, 9.0, 10.0])

    def test_invalid_step(self):
        with self.assertRaises(ValueError):
            ge.sample_x(100, 0)


class TestAutoScale(unittest.TestCase):
    def test_only_shrinks(self):
        s = ge.compute_auto_scale([10.0, 20.0], 300)
        self.assertLessEqual(s.scale, 1.0)

    def test_centered(self):
        s = ge.compute_auto_scale([0.0, 100.0], 300)
        # 中点 50 应映射到画布中心 150
        self.assertAlmostEqual(s.to_screen(50.0), 150.0, places=6)

    def test_flat_curve_no_div_zero(self):
        s = ge.compute_auto_scale([50.0, 50.0], 300)
        self.assertTrue(math.isfinite(s.scale))
        self.assertTrue(math.isfinite(s.offset))

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            ge.compute_auto_scale([], 100)

    def test_bad_margin(self):
        with self.assertRaises(ValueError):
            ge.compute_auto_scale([0, 1], 100, margin=0)
        with self.assertRaises(ValueError):
            ge.compute_auto_scale([0, 1], 100, margin=1.5)

    def test_huge_range_shrinks_into_canvas(self):
        # y in [0, 100000]，range=100000，scale = 270/100000 = 0.0027
        # 缩放后区间高度 270，居中对齐到高 300 的画布 -> 最大点 screen_y = 285
        s = ge.compute_auto_scale([0.0, 100000.0], 300, margin=0.9)
        self.assertAlmostEqual(s.to_screen(100000.0), 285.0, places=6)
        self.assertAlmostEqual(s.to_screen(0.0), 15.0, places=6)
        self.assertAlmostEqual(s.to_screen(50000.0), 150.0, places=6)

    def test_curve_fits_with_margin(self):
        """任意振幅下整条曲线都必须落在 [0, height] 内并留出边距。

        margin=0.9 => 曲线占 90% 高度，上下各留 5%（height=500 时即 [25, 475]）。
        """
        for amp in (1, 10, 100, 250, 500, 100000):
            for freq in (0.5, 1, 5, 10):
                with self.subTest(amp=amp, freq=freq):
                    pts = ge.sample_curve(ge.SINE, None, 700, 500, amp, freq)
                    ys = [p.screen_y for p in pts]
                    self.assertLessEqual(max(ys), 475.0 + 1e-6, "超出下边距")
                    self.assertGreaterEqual(min(ys), 25.0 - 1e-6, "超出上边距")


class TestSampleCurve(unittest.TestCase):
    def test_all_points_within_canvas(self):
        pts = ge.sample_curve(ge.SINE, None, 700, 500, 100, 2)
        self.assertTrue(pts)
        for p in pts:
            self.assertGreaterEqual(p.screen_y, 0.0, f"y={p.screen_y} 越上界")
            self.assertLessEqual(p.screen_y, 500.0, f"y={p.screen_y} 越下界")

    def test_point_count_matches_step(self):
        pts = ge.sample_curve(ge.SINE, None, 100, 100, 10, 2)
        self.assertEqual(len(ge.sample_x(100, 2)), len(pts))

    def test_custom_error_at_one_point_does_not_kill_curve(self):
        calls = {"n": 0}

        def flaky(x, width, center_y, amp, freq):
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("boom")
            return center_y

        pts = ge.sample_curve(ge.CUSTOM, flaky, 100, 100, 10, 2)
        self.assertGreater(len(pts), 3)
        # 失败点回落到中心
        self.assertAlmostEqual(pts[2].screen_y, 50.0, places=6)

    def test_bad_height_raises(self):
        with self.assertRaises(ValueError):
            ge.sample_curve(ge.SINE, None, 100, 0, 10, 2)


class TestPolyline(unittest.TestCase):
    def test_straight_line_length(self):
        pts = [
            ge.SamplePoint(0, 0, 0, 0.0),
            ge.SamplePoint(1, 0, 3, 4.0),
        ]
        self.assertAlmostEqual(ge.polyline_length(pts), 5.0)

    def test_empty_is_zero(self):
        self.assertEqual(ge.polyline_length([]), 0.0)

    def test_segment_length(self):
        a = ge.SamplePoint(0, 0, 0, 0.0)
        b = ge.SamplePoint(1, 0, 0, 10.0)
        self.assertAlmostEqual(ge.segment_length(a, b), 10.0)


class TestClamp(unittest.TestCase):
    def test_clamp_y(self):
        self.assertEqual(ge.clamp_y(-5, 100), 0.0)
        self.assertEqual(ge.clamp_y(105, 100), 100.0)
        self.assertEqual(ge.clamp_y(50, 100), 50.0)

    def test_point_in_canvas(self):
        self.assertTrue(ge.point_in_canvas(0, 100))
        self.assertTrue(ge.point_in_canvas(100, 100))
        self.assertFalse(ge.point_in_canvas(-1, 100))
        self.assertFalse(ge.point_in_canvas(101, 100))


class TestLoaderValidation(unittest.TestCase):
    def _write(self, dirpath: Path, name: str, content: str) -> Path:
        p = dirpath / f"{name}.py"
        p.write_text(content, encoding="utf-8")
        return p

    def test_valid_function_loads(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            p = self._write(
                d,
                "ok_func",
                "import math\n"
                "def ok_func(x, width, center_y, amp, freq):\n"
                "    return center_y + amp * math.sin(x)\n",
            )
            lf = cl.load_function(p)
            self.assertEqual(lf.name, "ok_func")
            self.assertTrue(callable(lf.func))
            self.assertAlmostEqual(lf.func(0, 100, 50, 10, 1), 50.0)

    def test_rejects_import_os(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            p = self._write(
                d,
                "bad",
                "import os\n"
                "def bad(x, width, center_y, amp, freq):\n"
                "    return center_y\n",
            )
            with self.assertRaises(cl.FunctionValidationError):
                cl.load_function(p)

    def test_rejects_open(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            p = self._write(
                d,
                "bad",
                "def bad(x, width, center_y, amp, freq):\n"
                "    return open(x).read()\n",
            )
            with self.assertRaises(cl.FunctionValidationError):
                cl.load_function(p)

    def test_rejects_eval(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            p = self._write(
                d,
                "bad",
                "def bad(x, width, center_y, amp, freq):\n"
                "    return eval('1')\n",
            )
            with self.assertRaises(cl.FunctionValidationError):
                cl.load_function(p)

    def test_rejects_dunder(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            p = self._write(
                d,
                "bad",
                "def bad(x, width, center_y, amp, freq):\n"
                "    return x.__class__\n",
            )
            with self.assertRaises(cl.FunctionValidationError):
                cl.load_function(p)

    def test_rejects_wrong_arity(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            p = self._write(
                d,
                "short",
                "def short(x, width):\n    return width\n",
            )
            with self.assertRaises(cl.FunctionValidationError):
                cl.load_function(p)

    def test_rejects_syntax_error(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            p = self._write(d, "syn", "def syn(:\n  pass\n")
            with self.assertRaises(cl.FunctionValidationError):
                cl.load_function(p)

    def test_rejects_name_mismatch(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            p = self._write(
                d,
                "mismatch",
                "def other(x, width, center_y, amp, freq):\n    return center_y\n",
            )
            with self.assertRaises(cl.FunctionValidationError):
                cl.load_function(p)

    def test_missing_file(self):
        with self.assertRaises(cl.FunctionValidationError):
            cl.load_function(Path("no_such_file_xyz.py"))

    def test_oversize_file(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            p = self._write(
                d,
                "big",
                "# " + "a" * (cl.MAX_FILE_BYTES + 10) + "\n"
                "def big(x, width, center_y, amp, freq):\n    return center_y\n",
            )
            with self.assertRaises(cl.FunctionValidationError):
                cl.load_function(p)

    def test_numpy_allowed_but_not_required(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            p = self._write(
                d,
                "np_func",
                "def np_func(x, width, center_y, amp, freq):\n"
                "    return center_y + x * 0.0\n",
            )
            self.assertTrue(cl.load_function(p))


class TestLoaderDiscovery(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / "a_func.py").write_text(
            "def a_func(x, width, center_y, amp, freq):\n    return center_y\n",
            encoding="utf-8",
        )
        (self.dir / "b_func.py").write_text(
            "def b_func(x, width, center_y, amp, freq):\n    return center_y\n",
            encoding="utf-8",
        )
        (self.dir / "_private.py").write_text("", encoding="utf-8")
        (self.dir / "__init__.py").write_text("", encoding="utf-8")
        (self.dir / "notes.txt").write_text("x", encoding="utf-8")

    def tearDown(self):
        for p in self.dir.iterdir():
            p.unlink()
        self.dir.rmdir()

    def test_lists_only_public_py(self):
        names = cl.list_function_names(self.dir)
        self.assertEqual(names, ["a_func", "b_func"])

    def test_load_all_returns_errors_not_raise(self):
        (self.dir / "c_func.py").write_text(
            "import os\ndef c_func(x, width, center_y, amp, freq):\n    return 0\n",
            encoding="utf-8",
        )
        loaded, errors = cl.load_all(self.dir)
        self.assertEqual(len(loaded), 2)
        self.assertEqual(len(errors), 1)
        self.assertIn("c_func.py", errors[0])

    def test_nonexistent_dir_returns_empty(self):
        self.assertEqual(cl.list_function_names(Path("no_such_dir_zzz")), [])

    def test_resolve_override(self):
        r = cl.resolve_functions_dir(str(self.dir))
        self.assertEqual(r, self.dir.resolve())

    def test_template_is_valid_python(self):
        src = cl.function_source_template("demo_func")
        ns: dict = {}
        exec(compile(src, "<template>", "exec"), ns)  # noqa: S102 - 测试自产模板
        self.assertIn("demo_func", ns)
        self.assertAlmostEqual(ns["demo_func"](0, 100, 50, 10, 1), 50.0)

    def test_app_root_exists(self):
        self.assertTrue(cl.app_root().is_dir())

    def test_loading_leaves_no_pycache_in_functions_dir(self):
        """加载函数不得在用户函数目录留下 __pycache__ 等副产物。"""
        before = {p.name for p in self.dir.iterdir()}
        (self.dir / "d_func.py").write_text(
            "import math\n"
            "def d_func(x, width, center_y, amp, freq):\n"
            "    return center_y + amp * math.sin(x)\n",
            encoding="utf-8",
        )
        loaded, errors = cl.load_all(self.dir)
        self.assertEqual(errors, [])
        self.assertEqual(len(loaded), 3)
        after = {p.name for p in self.dir.iterdir()}
        self.assertEqual(
            after - before,
            {"d_func.py"},
            f"产生了预期外的副产物: {after - before}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
