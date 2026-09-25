"""data_import 与多变量拟合测试。"""

import math
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import data_import as di
from core import fitting as ft

try:
    import openpyxl
    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False


class TestGridParsing(unittest.TestCase):
    def test_basic(self):
        grid = [["x1", "x2", "y"], ["1", "2", "3"], ["4", "5", "6"]]
        d = di.parse_grid(grid, Path("t.csv"))
        self.assertEqual(d.variables, ("x1", "x2"))
        self.assertEqual(d.target, "y")
        self.assertEqual(d.n_rows, 2)
        self.assertEqual(d.n_vars, 2)
        self.assertEqual(d.rows[0], ((1.0, 2.0), 3.0))

    def test_chinese_headers(self):
        grid = [["时间", "温度", "利润"], ["1", "20", "100"],
                ["2", "25", "120"], ["3", "30", "140"]]
        d = di.parse_grid(grid, Path("t.csv"))
        self.assertEqual(d.variables, ("时间", "温度"))
        self.assertEqual(d.target, "利润")

    def test_single_variable(self):
        grid = [["面积", "价格"], ["50", "100"], ["80", "160"]]
        d = di.parse_grid(grid, Path("t.csv"))
        self.assertEqual(d.variables, ("面积",))
        self.assertEqual(d.target, "价格")

    def test_header_only_rejected(self):
        with self.assertRaises(di.DataImportError):
            di.parse_grid([["x1", "y"]], Path("t.csv"))

    def test_empty_grid_rejected(self):
        with self.assertRaises(di.DataImportError):
            di.parse_grid([], Path("t.csv"))

    def test_single_column_rejected(self):
        with self.assertRaises(di.DataImportError):
            di.parse_grid([["x1"], ["1"], ["2"]], Path("t.csv"))

    def test_numeric_header_rejected(self):
        """第一行是数值时应明确报错，而不是把它当变量名。"""
        with self.assertRaises(di.DataImportError) as ctx:
            di.parse_grid([["1", "2", "3"], ["4", "5", "6"]], Path("t.csv"))
        self.assertIn("数据名", str(ctx.exception))

    def test_blank_row_skipped(self):
        grid = [["x1", "y"], ["1", "2"], ["", ""], ["3", "4"]]
        d = di.parse_grid(grid, Path("t.csv"))
        self.assertEqual(d.n_rows, 2)

    def test_invalid_number_reports_location(self):
        """错误信息应指向用户在源文件里能看到的行号。"""
        with self.assertRaises(di.DataImportError) as ctx:
            di.parse_grid([["x1", "y"], ["1", "2"], ["abc", "3"]], Path("t.csv"))
        msg = str(ctx.exception)
        self.assertIn("第 3 行", msg)
        self.assertIn("x1", msg)
        self.assertIn("abc", msg)

    def test_invalid_number_row_number_after_blank_rows(self):
        """全空行被跳过时，行号仍应指向源文件行号。"""
        grid = [["x1", "y"], ["", ""], ["", ""], ["1", "2"], ["bad", "3"]]
        with self.assertRaises(di.DataImportError) as ctx:
            di.parse_grid(grid, Path("t.csv"))
        self.assertIn("第 5 行", str(ctx.exception))

    def test_nan_rejected(self):
        with self.assertRaises(di.DataImportError):
            di.parse_grid([["x1", "y"], ["1", "2"], ["nan", "3"]], Path("t.csv"))

    def test_inf_rejected(self):
        with self.assertRaises(di.DataImportError):
            di.parse_grid([["x1", "y"], ["1", "2"], ["inf", "3"]], Path("t.csv"))

    def test_duplicate_headers_get_suffix(self):
        grid = [["x", "x", "y"], ["1", "2", "3"], ["4", "5", "6"]]
        d = di.parse_grid(grid, Path("t.csv"))
        self.assertEqual(d.variables, ("x", "x_2"))

    def test_extra_column_not_silently_dropped(self):
        """数据行比表头多列时必须报错，绝不能静默截断。

        静默截断会悄悄丢掉一整列数值，用户完全看不出少了数据，
        拟合结果也就跟着错了 —— 这是实测踩到的真实缺陷。
        """
        grid = [["x1", "x2", "y"],
                ["1", "2", "3", "4"],
                ["5", "6", "7", "8"],
                ["9", "10", "11", "12"]]
        with self.assertRaises(di.DataImportError) as ctx:
            di.parse_grid(grid, Path("t.csv"))
        msg = str(ctx.exception)
        self.assertIn("第 2 行", msg)
        self.assertIn("3 列", msg)      # 表头列数
        self.assertIn("4", msg)         # 数据行列数

    def test_missing_column_rejected(self):
        """数据行列数少于表头也应报错。"""
        grid = [["x1", "x2", "y"], ["1", "2"], ["3", "4"]]
        with self.assertRaises(di.DataImportError) as ctx:
            di.parse_grid(grid, Path("t.csv"))
        self.assertIn("少于", str(ctx.exception))

    def test_blank_header_filled(self):
        grid = [["", "y"], ["1", "2"], ["3", "4"], ["5", "6"]]
        d = di.parse_grid(grid, Path("t.csv"))
        self.assertEqual(d.variables[0], "c1")

    def test_thousands_separator_ok(self):
        grid = [["x1", "y"], ["1,000", "2"], ["2,000", "4"], ["3,000", "6"]]
        d = di.parse_grid(grid, Path("t.csv"))
        self.assertEqual(d.rows[0][0][0], 1000.0)

    def test_currency_prefix_ok(self):
        grid = [["x1", "y"], ["¥100", "2"], ["¥200", "4"], ["¥300", "6"]]
        d = di.parse_grid(grid, Path("t.csv"))
        self.assertEqual(d.rows[0][0][0], 100.0)

    def test_constant_variable_rejected(self):
        grid = [["x1", "x2", "y"], ["1", "5", "2"], ["2", "5", "4"],
                ["3", "5", "6"]]
        with self.assertRaises(di.DataImportError) as ctx:
            di.parse_grid(grid, Path("t.csv"))
        self.assertIn("x2", str(ctx.exception))

    def test_column_and_targets(self):
        grid = [["x1", "x2", "y"], ["1", "10", "100"], ["2", "20", "200"]]
        d = di.parse_grid(grid, Path("t.csv"))
        self.assertEqual(d.column(0), [1.0, 2.0])
        self.assertEqual(d.column(1), [10.0, 20.0])
        self.assertEqual(d.targets(), [100.0, 200.0])


class TestFileImport(unittest.TestCase):
    def _write(self, path, text):
        path.write_text(text, encoding="utf-8")
        return path

    def test_csv_utf8(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._write(Path(td) / "a.csv",
                            "x1,y\n1,2\n3,4\n5,6\n")
            d = di.import_file(p)
            self.assertEqual(d.n_rows, 3)

    def test_csv_gbk(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "gbk.csv"
            p.write_text("变量,结果\n1,2\n3,4\n5,6\n", encoding="gbk")
            d = di.import_file(p)
            self.assertEqual(d.variables, ("变量",))
            self.assertEqual(d.target, "结果")

    def test_utf8_bom(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bom.csv"
            p.write_bytes("\ufeffx1,y\n1,2\n3,4\n5,6\n".encode("utf-8"))
            d = di.import_file(p)
            self.assertEqual(d.variables, ("x1",), "BOM 应被剥离")

    def test_tsv(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._write(Path(td) / "a.tsv",
                            "x1\tx2\ty\n1\t10\t100\n2\t20\t200\n3\t30\t300\n")
            d = di.import_file(p)
            self.assertEqual(d.variables, ("x1", "x2"))

    def test_semicolon(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._write(Path(td) / "s.csv",
                            "x1;x2;y\n1;10;100\n2;20;200\n3;30;300\n")
            d = di.import_file(p)
            self.assertEqual(d.n_vars, 2)

    def test_txt(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._write(Path(td) / "a.txt", "x1,y\n1,2\n3,4\n5,6\n")
            d = di.import_file(p)
            self.assertEqual(d.n_rows, 3)

    def test_xls_rejected_with_hint(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "old.xls"
            p.write_bytes(b"\xd0\xcf\x11\xe0")
            with self.assertRaises(di.DataImportError) as ctx:
                di.import_file(p)
            self.assertIn("另存为", str(ctx.exception))

    def test_unknown_suffix(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.docx"
            p.write_bytes(b"x")
            with self.assertRaises(di.DataImportError) as ctx:
                di.import_file(p)
            self.assertIn("不支持", str(ctx.exception))

    def test_missing_file(self):
        with self.assertRaises(di.DataImportError) as ctx:
            di.import_file(Path("no_such_file_zzz.csv"))
        self.assertIn("不存在", str(ctx.exception))

    @unittest.skipUnless(HAS_OPENPYXL, "openpyxl 未安装")
    def test_xlsx(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "data.xlsx"
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.append(["x1", "x2", "y"])
            for row in [(1, 10, 100), (2, 20, 200), (3, 30, 300)]:
                ws.append(list(row))
            wb.save(p)
            d = di.import_file(p)
            self.assertEqual(d.variables, ("x1", "x2"))
            self.assertEqual(d.target, "y")
            self.assertEqual(d.n_rows, 3)
            self.assertEqual(d.rows[1], ((2.0, 20.0), 200.0))

    @unittest.skipUnless(HAS_OPENPYXL, "openpyxl 未安装")
    def test_xlsx_float_rendered_int(self):
        """3.0 应读成 3 而不是 '3.0'（后者不影响数值但影响错误信息）。"""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "f.xlsx"
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.append(["x1", "y"])
            ws.append([1.0, 2.0])
            ws.append([2.0, 4.0])
            wb.save(p)
            d = di.import_file(p)
            self.assertEqual(d.column(0), [1.0, 2.0])


class TestFitMulti(unittest.TestCase):
    def test_exact_linear_recovery(self):
        rows = [((float(x),), 3.0 + 2.0 * x) for x in range(1, 11)]
        r = ft.fit_multi(rows, ("x1",))
        self.assertAlmostEqual(r.r2, 1.0, places=9)
        self.assertLess(r.rmse, 1e-8)
        self.assertAlmostEqual(r.coefficients[0], 3.0, delta=1e-6)
        self.assertAlmostEqual(r.coefficients[1], 2.0, delta=1e-6)

    def test_two_variables_exact(self):
        rows = []
        for a in range(1, 6):
            for b in range(1, 6):
                rows.append(((float(a), float(b)), 1.0 + 2.0 * a - 0.5 * b))
        r = ft.fit_multi(rows, ("x1", "x2"))
        self.assertAlmostEqual(r.r2, 1.0, places=9)
        self.assertAlmostEqual(r.coefficients[0], 1.0, delta=1e-5)
        self.assertAlmostEqual(r.coefficients[1], 2.0, delta=1e-5)
        self.assertAlmostEqual(r.coefficients[2], -0.5, delta=1e-5)

    def test_three_variables_with_chinese_names(self):
        # 三列必须彼此独立，否则数学上奇异（这不是缺陷，是共识）
        rows = []
        for i in range(1, 13):
            时间 = float(i)
            温度 = 20.0 + 3.0 * i + (1.0 if i % 3 == 0 else 0.0)
            湿度 = 50.0 - 2.0 * i + (2.0 if i % 4 == 0 else 0.0)
            利润 = 10.0 + 5.0 * 时间 - 0.3 * 温度 + 0.2 * 湿度
            rows.append(((时间, 温度, 湿度), 利润))
        r = ft.fit_multi(rows, ("时间", "温度", "湿度"), "利润")
        self.assertEqual(r.target, "利润")
        self.assertEqual(r.rows_used, len(rows))
        self.assertGreater(r.r2, 0.9999)
        self.assertIn("利润 =", r.expression)
        # 表达式必须真的能反推出每个 xi 的贡献
        for xs, y in rows:
            self.assertAlmostEqual(r.predict(xs), y, delta=1e-5)
        self.assertAlmostEqual(r.coefficients[1], 5.0, delta=1e-4)
        self.assertAlmostEqual(r.coefficients[2], -0.3, delta=1e-4)
        self.assertAlmostEqual(r.coefficients[3], 0.2, delta=1e-4)

    def test_predict_roundtrip(self):
        rows = [((float(x), float(y)), 2.0 + x - y)
                for x in range(1, 8) for y in range(1, 8)]
        r = ft.fit_multi(rows, ("a", "b"))
        self.assertAlmostEqual(r.predict((3.0, 4.0)), 2.0 + 3.0 - 4.0,
                               delta=1e-6)

    def test_predict_wrong_arity(self):
        rows = [((float(x),), 1.0 + x) for x in range(1, 6)]
        r = ft.fit_multi(rows, ("x1",))
        with self.assertRaises(ft.FitError):
            r.predict((1.0, 2.0))

    def test_large_scale_stable(self):
        """量级差异大且彼此独立时，归一化保证数值稳定。

        注意：x1=1000·i 与 x2=0.001·(41-i) 归一化后仍是线性关系（共线），
        必须用真正不同步的序列。
        """
        rows = []
        for i in range(1, 41):
            x1 = 1000.0 * i
            # 步长不等 + 摆动，保证与 x1 不成比例
            x2 = 0.001 * (i * i + 3 * i)
            y = 2.0 + 3.0 * x1 - 5.0 * x2
            rows.append(((x1, x2), y))
        r = ft.fit_multi(rows, ("x1", "x2"))
        self.assertAlmostEqual(r.r2, 1.0, places=6)
        self.assertAlmostEqual(r.coefficients[1], 3.0, delta=1e-8)
        self.assertAlmostEqual(r.coefficients[2], -5.0, delta=1e-5)

    def test_empty_rows(self):
        with self.assertRaises(ft.FitError):
            ft.fit_multi([], ("x1",))

    def test_no_variables(self):
        with self.assertRaises(ft.FitError):
            ft.fit_multi([((1.0,), 2.0)], ())

    def test_arity_mismatch(self):
        with self.assertRaises(ft.FitError):
            ft.fit_multi([((1.0, 2.0), 3.0)], ("x1",))

    def test_constant_column_rejected(self):
        """行数足够时，常数列仍应被拒绝。"""
        rows = [((1.0, 5.0), 2.0), ((2.0, 5.0), 4.0), ((3.0, 5.0), 6.0),
                ((4.0, 5.0), 8.0), ((5.0, 5.0), 10.0)]
        with self.assertRaises(ft.FitError) as ctx:
            ft.fit_multi(rows, ("x1", "x2"))
        self.assertIn("x2", str(ctx.exception))

    def test_noisy_data(self):
        import random
        random.seed(7)
        rows = []
        for x in range(1, 61):
            y = 1.5 + 0.8 * x + 0.02 * x * x
            rows.append(((float(x),), y + random.uniform(-2, 2)))
        r = ft.fit_multi(rows, ("x1",))
        # 对二次数据做线性拟合：R² 应高但不为 1
        self.assertGreater(r.r2, 0.97)
        self.assertLess(r.r2, 1.0)
        self.assertGreater(r.rmse, 0.0)

    def test_too_few_rows_rejected_clearly(self):
        """行数 <= 变量数+1 时应给可读错误，而不是裸的"矩阵奇异"。"""
        rows = [((1.0, 20.0, 5.0), 10.0), ((2.0, 25.0, 6.0), 15.0),
                ((3.0, 30.0, 7.0), 20.0), ((4.0, 35.0, 8.0), 25.0)]
        with self.assertRaises(ft.FitError) as ctx:
            ft.fit_multi(rows, ("时间", "温度", "湿度"), "利润")
        msg = str(ctx.exception)
        self.assertIn("5 行", msg)
        self.assertIn("4 行", msg)
        self.assertNotIn("奇异", msg)


class TestFormatMulti(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(
            ft.format_multi([3.2, 1.5, -0.8], ("x1", "x2")),
            "y = 3.2 + 1.5x1 - 0.8x2",
        )

    def test_target_name_used(self):
        self.assertEqual(
            ft.format_multi([0.0, 2.0], ("时间",), "利润"),
            "利润 = 2时间",
        )

    def test_unit_coeff_omitted(self):
        self.assertEqual(
            ft.format_multi([0.0, 1.0, -1.0], ("a", "b")),
            "y = a - b",
        )

    def test_zero_coeff_skipped(self):
        self.assertEqual(
            ft.format_multi([5.0, 0.0, 2.0], ("a", "b")),
            "y = 5 + 2b",
        )

    def test_all_zero(self):
        self.assertEqual(ft.format_multi([0.0, 0.0], ("a",)), "y = 0")

    def test_negative_const_no_leading_plus(self):
        self.assertEqual(
            ft.format_multi([-3.0, 1.0], ("a",)),
            "y = -3 + a",
        )

    def test_negative_first_term_zero_const(self):
        self.assertEqual(
            ft.format_multi([0.0, -2.0], ("a",)),
            "y = -2a",
        )

    def test_expression_matches_recompute(self):
        """表达式字符串应与 predict 结果一致（直接保证"输出正确"）。"""
        rows = [((float(x), float(y)), 1.0 + 2.0 * x - 3.0 * y)
                for x in range(1, 12) for y in range(1, 12)]
        r = ft.fit_multi(rows, ("x1", "x2"))
        for (a, b), _ in rows[:20]:
            self.assertAlmostEqual(r.predict((a, b)), 1.0 + 2 * a - 3 * b,
                                   delta=1e-6)


class TestMultiCandidates(unittest.TestCase):
    def test_ranks_by_r2(self):
        """无关变量应排后，全部变量参与时 R² 最高。"""
        rows = [((float(i), float(i) + 0.001 * i), 5.0 + 2.0 * i)
                for i in range(1, 31)]
        cands = ft.multi_candidates(rows, ("无关", "相关"))
        self.assertTrue(cands)
        ok = [r.r2 for _, r in cands if not isinstance(r, ft.FitError)]
        self.assertEqual(ok, sorted(ok, reverse=True))
        self.assertAlmostEqual(max(ok), 1.0, places=6)

    def test_includes_all_variables_entry_even_when_it_fails(self):
        """'全部变量'即使失败也必须出现在结果里（不能让用户凭空少一个模型）。"""
        rows = [((float(i), float(i)), 1.0 + i) for i in range(1, 21)]
        cands = ft.multi_candidates(rows, ("a", "b"))
        labels = [label for label, _ in cands]
        self.assertIn("全部变量", labels)
        # 两列完全相同 -> 多元拟合奇异，应作为错误项出现并被排在最后
        all_entry = dict(cands)["全部变量"]
        self.assertIsInstance(all_entry, ft.FitError)

    def test_failed_entry_sorted_last(self):
        rows = [((float(i), float(i)), 1.0 + i) for i in range(1, 21)]
        cands = ft.multi_candidates(rows, ("a", "b"))
        self.assertIsInstance(cands[-1][1], ft.FitError)

    def test_two_identical_columns_report_collinearity(self):
        """两列完全相同时，应明确指出是哪两列共线，而不是裸的'奇异'。"""
        rows = [((float(i), float(i)), 1.0 + i) for i in range(1, 21)]
        cands = ft.multi_candidates(rows, ("a", "b"))
        err = dict(cands)["全部变量"]
        self.assertIsInstance(err, ft.FitError)
        msg = str(err)
        self.assertIn("a", msg)
        self.assertIn("b", msg)
        self.assertIn("线性相关", msg)
        self.assertNotIn("奇异", msg)


class TestEndToEndFromExcelLikeData(unittest.TestCase):
    """走完整链路：表格网格 → 导入 → 拟合 → 表达式。"""

    def test_full_pipeline(self):
        grid = []
        grid.append(["x1", "x2", "y"])
        for a in range(1, 31):
            for b in range(1, 31):
                grid.append([str(a), str(b), str(2.0 + 3.0 * a - 0.5 * b)])
        d = di.parse_grid(grid, Path("demo.csv"))
        r = ft.fit_multi(d.rows, d.variables, d.target)
        self.assertEqual(d.variables, ("x1", "x2"))
        self.assertEqual(d.target, "y")
        self.assertAlmostEqual(r.r2, 1.0, places=8)
        self.assertAlmostEqual(r.coefficients[0], 2.0, delta=1e-5)
        self.assertAlmostEqual(r.coefficients[1], 3.0, delta=1e-5)
        self.assertAlmostEqual(r.coefficients[2], -0.5, delta=1e-5)
        # 每个数据行都能被该函数准确预测 —— 这是"输出对应 xi 的函数"的核心
        for xs, y in d.rows:
            self.assertAlmostEqual(r.predict(xs), y, delta=1e-6)

    def test_single_var_from_table(self):
        grid = [["面积", "价格"]]
        for a in (1, 2, 3, 4, 5, 6, 7, 8):
            grid.append([str(a * 10), str(a * 10 * 3 + 5)])
        d = di.parse_grid(grid, Path("house.csv"))
        r = ft.fit_multi(d.rows, d.variables, d.target)
        self.assertEqual(d.variables, ("面积",))
        self.assertEqual(d.target, "价格")
        self.assertAlmostEqual(r.r2, 1.0, places=8)


if __name__ == "__main__":
    unittest.main(verbosity=2)
