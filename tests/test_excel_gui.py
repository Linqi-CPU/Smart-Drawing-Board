"""Excel 导入 + 多变量拟合的 GUI 冒烟测试。

真实创建窗口，用 stub 掉 filedialog 的方式喂文件路径，
驱动 run_excel_fit() 走完整 UI 路径，断言输出内容。
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DRAWING_FUNCTIONS_DIR", str(ROOT / "functions"))

import tkinter as tk

from core import data_import as di
from core import fitting as ft
import main as app_mod

try:
    import openpyxl
    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tk.Tk()
        cls.app = app_mod.DrawingApp(cls.root)
        cls.root.update()
        cls.tmp = Path(tempfile.mkdtemp())

    @classmethod
    def tearDownClass(cls):
        try:
            cls.app.dispose()
        except Exception:
            pass

    def pump(self, ms=100):
        deadline = time.time() + ms / 1000.0
        while time.time() < deadline:
            self.root.update()
            time.sleep(0.004)
        self.root.update()
        self.root.update_idletasks()

    def feed_file(self, path):
        """把对话框 stub 掉，直接给 run_excel_fit 喂路径。"""
        orig = app_mod.filedialog.askopenfilename
        app_mod.filedialog.askopenfilename = lambda *a, **k: str(path)
        try:
            self.app.run_excel_fit()
        finally:
            app_mod.filedialog.askopenfilename = orig
        self.pump(30)

    def make_xlsx(self, name, header, rows):
        if not HAS_OPENPYXL:
            self.skipTest("openpyxl 未安装")
        p = self.tmp / name
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(list(header))
        for r in rows:
            ws.append(list(r))
        wb.save(p)
        return p

    def make_csv(self, name, text):
        p = self.tmp / name
        p.write_text(text, encoding="utf-8")
        return p


class TestExcelFitFlow(_Base):
    def setUp(self):
        self.app.fit_result = None
        self.app.fit_multi_result = None
        self.app.imported_data = None
        self.app._multi_candidates = []
        self.app.imported_label.config(text="")
        self.app._set_fit_result_text("")
        self.pump(20)

    @unittest.skipUnless(HAS_OPENPYXL, "openpyxl 未安装")
    def test_01_xlsx_two_vars(self):
        rows = []
        for i in range(1, 16):
            x1 = float(i)
            x2 = 30.0 + i * i      # 与 x1 不成比例
            y = 5.0 + 2.0 * x1 - 0.5 * x2
            rows.append((x1, x2, y))
        p = self.make_xlsx("t.xlsx", ("x1", "x2", "y"), rows)
        self.feed_file(p)
        r = self.app.fit_multi_result
        self.assertIsNotNone(r, "应产生多变量拟合结果")
        self.assertEqual(r.variable, ("x1", "x2"))
        self.assertEqual(r.target, "y")
        self.assertGreater(r.r2, 0.9999)
        # 表达式里必须出现每个 xi
        self.assertIn("x1", r.expression)
        self.assertIn("x2", r.expression)
        self.assertIn("y =", r.expression)
        # 每个数据点都能被该函数准确预测
        for xs, y in self.app.imported_data.rows:
            self.assertAlmostEqual(r.predict(tuple(xs)), y, delta=1e-6)

    @unittest.skipUnless(HAS_OPENPYXL, "openpyxl 未安装")
    def test_02_result_text_lists_each_xi(self):
        """结果区必须列出"数据名对应的 xi"及其系数。"""
        rows = [(float(i), 30.0 + i * i, 5.0 + 2.0 * i - 0.5 * (30.0 + i * i))
                for i in range(1, 16)]
        p = self.make_xlsx("t2.xlsx", ("x1", "x2", "y"), rows)
        self.feed_file(p)
        text = self.app.fit_result_text.get("1.0", tk.END)
        self.assertIn("各变量系数", text)
        self.assertIn("x1", text)
        self.assertIn("x2", text)
        self.assertIn("取值范围", text)
        self.assertIn("R²", text)

    @unittest.skipUnless(HAS_OPENPYXL, "openpyxl 未安装")
    def test_03_chinese_headers(self):
        rows = []
        for i in range(1, 13):
            面积 = 50.0 + 12 * i
            卧室 = 1 + ((i * 5) % 4)
            房价 = 10.0 + 0.4 * 面积 + 12.0 * 卧室
            rows.append((面积, 卧室, 房价))
        p = self.make_xlsx("房价.xlsx", ("面积", "卧室", "房价"), rows)
        self.feed_file(p)
        r = self.app.fit_multi_result
        self.assertEqual(r.variable, ("面积", "卧室"))
        self.assertEqual(r.target, "房价")
        # 输出表达式用的是数据名，不是 x1/x2
        self.assertIn("面积", r.expression)
        self.assertIn("卧室", r.expression)
        self.assertIn("房价 =", r.expression)
        text = self.app.fit_result_text.get("1.0", tk.END)
        self.assertIn("面积", text)
        self.assertIn("卧室", text)

    @unittest.skipUnless(HAS_OPENPYXL, "openpyxl 未安装")
    def test_04_status_bar_shows_function(self):
        rows = [(float(i), 30.0 + i * i, 5.0 + 2.0 * i - 0.5 * (30.0 + i * i))
                for i in range(1, 16)]
        p = self.make_xlsx("t3.xlsx", ("x1", "x2", "y"), rows)
        self.feed_file(p)
        status = self.app.status_bar.cget("text")
        self.assertIn("拟合完成", status)
        self.assertIn("R²=", status)

    @unittest.skipUnless(HAS_OPENPYXL, "openpyxl 未安装")
    def test_05_imported_label_shows_shape(self):
        rows = [(float(i), 30.0 + i * i, 5.0 + 2.0 * i - 0.5 * (30.0 + i * i))
                for i in range(1, 16)]
        p = self.make_xlsx("t4.xlsx", ("x1", "x2", "y"), rows)
        self.feed_file(p)
        txt = self.app.imported_label.cget("text")
        self.assertIn("行数", txt)
        self.assertIn("变量数", txt)
        self.assertIn("x1", txt)

    @unittest.skipUnless(HAS_OPENPYXL, "openpyxl 未安装")
    def test_06_single_variable_table(self):
        rows = [(float(i * 10), 20.0 + 0.5 * i * 10) for i in range(1, 12)]
        p = self.make_xlsx("single.xlsx", ("面积", "价格"), rows)
        self.feed_file(p)
        r = self.app.fit_multi_result
        self.assertIsNotNone(r)
        self.assertEqual(r.n_vars if hasattr(r, "n_vars") else len(r.variable), 1)
        self.assertIn("面积", r.expression)
        self.assertIn("价格 =", r.expression)

    def test_07_csv_import(self):
        text = "x1,x2,y\n" + "".join(
            f"{float(i)},{30.0 + i * i},{5.0 + 2.0 * i - 0.5 * (30.0 + i * i)}\n"
            for i in range(1, 16)
        )
        p = self.make_csv("t.csv", text)
        self.feed_file(p)
        self.assertIsNotNone(self.app.fit_multi_result)
        self.assertEqual(self.app.fit_multi_result.target, "y")

    def test_08_tsv_import(self):
        text = "面积\t卧室\t房价\n" + "".join(
            f"{50.0 + 12 * i}\t{1 + (i % 4)}\t{10 + 0.4 * (50 + 12 * i) + 12 * (1 + i % 4)}\n"
            for i in range(1, 13)
        )
        p = self.make_csv("t.tsv", text)
        self.feed_file(p)
        self.assertIsNotNone(self.app.fit_multi_result)
        self.assertIn("房价 =", self.app.fit_multi_result.expression)

    def test_09_bad_file_shows_error_and_keeps_old_result(self):
        rows = [(float(i), 30.0 + i * i, 5.0 + 2.0 * i) for i in range(1, 16)]
        good = self.make_xlsx("good.xlsx", ("x1", "x2", "y"), rows)
        self.feed_file(good)
        self.assertIsNotNone(self.app.fit_multi_result)

        bad = self.make_csv("bad.csv", "1,2,3\n4,5,6\n")
        shown = []
        orig = app_mod.messagebox.showerror
        app_mod.messagebox.showerror = lambda *a, **k: shown.append(a)
        try:
            self.feed_file(bad)
        finally:
            app_mod.messagebox.showerror = orig
        self.assertTrue(shown, "坏文件应报错")
        # 旧结果不应被清掉（用户还看得到上一次的分析）
        self.assertIsNotNone(self.app.fit_multi_result)

    def test_10_collinear_rejected_with_names(self):
        rows = [(float(i), 2.0 * i, 3.0 * i) for i in range(1, 16)]
        p = self.make_csv("col.csv", "长度,宽度2倍,结果\n" + "".join(
            f"{a},{b},{c}\n" for a, b, c in rows))
        shown = []
        orig = app_mod.messagebox.showerror
        app_mod.messagebox.showerror = lambda *a, **k: shown.append(a)
        try:
            self.feed_file(p)
        finally:
            app_mod.messagebox.showerror = orig
        self.assertTrue(shown)
        msg = str(shown[0])
        self.assertIn("长度", msg)
        self.assertIn("宽度2倍", msg)
        self.assertIn("线性相关", msg)

    def test_11_column_count_mismatch_rejected(self):
        """多列无表头不是静默丢列，而应报错（实测回归）。"""
        text = "x1,x2,y\n1,2,3,4\n5,6,7,8\n9,10,11,12\n"
        p = self.make_csv("extra.csv", text)
        shown = []
        orig = app_mod.messagebox.showerror
        app_mod.messagebox.showerror = lambda *a, **k: shown.append(a)
        try:
            self.feed_file(p)
        finally:
            app_mod.messagebox.showerror = orig
        self.assertTrue(shown)
        self.assertIn("列", str(shown[0]))

    def test_12_numeric_header_rejected(self):
        text = "1,2,3\n4,5,6\n7,8,9\n"
        p = self.make_csv("nohdr.csv", text)
        shown = []
        orig = app_mod.messagebox.showerror
        app_mod.messagebox.showerror = lambda *a, **k: shown.append(a)
        try:
            self.feed_file(p)
        finally:
            app_mod.messagebox.showerror = orig
        self.assertTrue(shown)
        self.assertIn("数据名", str(shown[0]))

    def test_13_xls_rejected_with_hint(self):
        p = self.tmp / "old.xls"
        p.write_bytes(b"\xd0\xcf\x11\xe0" + b"\x00" * 64)
        shown = []
        orig = app_mod.messagebox.showerror
        app_mod.messagebox.showerror = lambda *a, **k: shown.append(a)
        try:
            self.feed_file(p)
        finally:
            app_mod.messagebox.showerror = orig
        self.assertTrue(shown)
        self.assertIn("另存为", str(shown[0]))

    def test_14_unsupported_suffix(self):
        p = self.tmp / "a.docx"
        p.write_bytes(b"x")
        shown = []
        orig = app_mod.messagebox.showerror
        app_mod.messagebox.showerror = lambda *a, **k: shown.append(a)
        try:
            self.feed_file(p)
        finally:
            app_mod.messagebox.showerror = orig
        self.assertTrue(shown)
        self.assertIn("不支持", str(shown[0]))

    def test_15_missing_file(self):
        shown = []
        orig = app_mod.messagebox.showerror
        app_mod.messagebox.showerror = lambda *a, **k: shown.append(a)
        try:
            self.feed_file(self.tmp / "no_such_file_zzz.xlsx")
        finally:
            app_mod.messagebox.showerror = orig
        self.assertTrue(shown)
        self.assertIn("不存在", str(shown[0]))

    def test_16_too_few_rows_for_vars(self):
        text = "x1,x2,x3,y\n1,2,3,4\n5,6,7,8\n"
        p = self.make_csv("few.csv", text)
        shown = []
        orig = app_mod.messagebox.showerror
        app_mod.messagebox.showerror = lambda *a, **k: shown.append(a)
        try:
            self.feed_file(p)
        finally:
            app_mod.messagebox.showerror = orig
        self.assertTrue(shown)
        # 错误信息应说清需要几行，而不是"矩阵奇异"
        self.assertNotIn("奇异", str(shown[0]))

    def test_17_copy_uses_multi_result(self):
        rows = [(float(i), 30.0 + i * i, 5.0 + 2.0 * i - 0.5 * (30.0 + i * i))
                for i in range(1, 16)]
        p = self.make_xlsx("c.xlsx", ("x1", "x2", "y"), rows)
        self.feed_file(p)
        # 剪贴板在无窗口环境可能失败，只要不抛异常即通过
        try:
            self.app.copy_fit_expression()
        except Exception as e:
            self.fail(f"copy_fit_expression 抛异常: {e}")

    def test_18_report_guard(self):
        self.app.fit_multi_result = None
        shown = []
        orig = app_mod.messagebox.showinfo
        app_mod.messagebox.showinfo = lambda *a, **k: shown.append(a)
        try:
            self.app.save_fit_report()
        finally:
            app_mod.messagebox.showinfo = orig
        self.assertTrue(shown)


if __name__ == "__main__":
    unittest.main(verbosity=2)
