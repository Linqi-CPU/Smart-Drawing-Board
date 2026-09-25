"""基于真实 .xlsx 文件的数据导入与拟合测试。

与 test_data_import.py 的区别：那里用内存构造的 grid 测解析逻辑，
这里用**真实落盘的 Excel 文件**（数学建模常见场景）测完整导入路径，
包括扩展名识别、sheet 选择、数字/文本/空单元格的真实表现。

fixture 由 tests/fixtures/__init__.py 用 openpyxl 生成，
数据是确定性构造的（固定 seed），因此可以断言具体数值。
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import data_import as di
from core import fitting as ft

try:
    import openpyxl  # noqa: F401
    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False

needs_xlsx = unittest.skipUnless(HAS_OPENPYXL, "openpyxl 未安装")


def fx(name: str) -> Path:
    """取一个 fixture 的路径（不存在则先生成）。"""
    from tests.fixtures import ensure_fixtures
    return ensure_fixtures()[name]


@needs_xlsx
class TestFixtureImport(unittest.TestCase):
    """真实 .xlsx 文件的导入。"""

    @classmethod
    def setUpClass(cls):
        from tests.fixtures import ensure_fixtures
        cls.files = ensure_fixtures()

    # ---------------- 一元线性 + 噪声 ----------------
    def test_linear_noisy_headers_and_rows(self):
        d = di.import_file(fx("linear_noisy.xlsx"))
        # 约定：第一行是表头（数据名），最后一列为 y，其余全是自变量
        self.assertEqual(d.variables, ("广告投入",))
        self.assertEqual(d.target, "销售额")
        self.assertEqual(d.n_rows, 30)
        self.assertEqual(d.n_vars, 1)

    def test_linear_noisy_values(self):
        d = di.import_file(fx("linear_noisy.xlsx"))
        # 第 1 行：x=1, y = 2.5*1 + 10 - 0.7 = 11.8
        self.assertEqual(d.column(0)[0], 1.0)
        self.assertAlmostEqual(d.rows[0][1], 11.8, places=6)
        # 第 2 行：x=2, y = 2.5*2 + 10 + 1.4 = 16.4
        self.assertAlmostEqual(d.rows[1][1], 16.4, places=6)
        # 最后一行 i=30：噪声 ((-1)^30)*(30%5)*0.7 = 0
        self.assertAlmostEqual(d.rows[-1][1], 85.0, places=6)

    def test_linear_noisy_fitting_recovers_slope(self):
        """拟合应还原出接近真实斜率 2.5 的系数。"""
        d = di.import_file(fx("linear_noisy.xlsx"))
        r = ft.fit_multi(list(d.rows), d.variables, d.target)
        # 噪声是对称的，斜率应非常接近 2.5
        self.assertAlmostEqual(r.coefficients[1], 2.5, delta=0.05)
        self.assertAlmostEqual(r.coefficients[0], 10.0, delta=1.5)
        self.assertGreater(r.r2, 0.95)

    # ---------------- 三元 + 中文列名 ----------------
    def test_multi_var_chinese_headers(self):
        d = di.import_file(fx("multi_var.xlsx"))
        self.assertEqual(d.variables, ("施肥量", "浇水量", "光照时长"))
        self.assertEqual(d.target, "产量")
        self.assertEqual(d.n_rows, 25)
        self.assertEqual(d.n_vars, 3)

    def test_multi_var_exact_recovery(self):
        """构造数据 y = 0.8a + 1.2b + 2.0c + 5，应被精确还原。"""
        d = di.import_file(fx("multi_var.xlsx"))
        r = ft.fit_multi(list(d.rows), d.variables, d.target)
        self.assertEqual(r.variable, ("施肥量", "浇水量", "光照时长"))
        self.assertEqual(r.target, "产量")
        self.assertEqual(len(r.coefficients), 4)      # 常数项 + 3 个系数
        self.assertAlmostEqual(r.r2, 1.0, places=8)
        self.assertAlmostEqual(r.coefficients[0], 5.0, delta=1e-6)
        self.assertAlmostEqual(r.coefficients[1], 0.8, delta=1e-6)
        self.assertAlmostEqual(r.coefficients[2], 1.2, delta=1e-6)
        self.assertAlmostEqual(r.coefficients[3], 2.0, delta=1e-6)

    # ---------------- 含非数值列（坏数据：导入期必须拒绝） ----------------
    def test_bad_text_column_rejected_at_import(self):
        """时间文本列必须在**导入期**就报错，带行号列名。

        不能静默把 '2024-01' 当 0 参与拟合——那会污染整个模型。
        """
        with self.assertRaises(di.DataImportError) as ctx:
            di.import_file(fx("bad_text_column.xlsx"))
        msg = str(ctx.exception)
        self.assertIn("月份", msg)          # 指出是哪一列
        self.assertIn("2024-01", msg)       # 指出是什么值
        self.assertIn("第 2 行", msg)        # 指出是哪一行

    def test_infected_cells_rejected_at_import(self):
        """空单元格与文本混入也必须在导入期报错。"""
        with self.assertRaises(di.DataImportError):
            di.import_file(fx("infected_cells.xlsx"))

    def test_infected_cells_never_becomes_zero(self):
        """坏数据绝不能变成 0.0 —— 0 是个合法数值，会静默扭曲拟合。"""
        # 由于导入期直接拒绝，这里验证的是"没有 import 成功"这个事实：
        # 只要能 import 成功，回头就要检查有没有 0 混进来。
        try:
            d = di.import_file(fx("infected_cells.xlsx"))
        except di.DataImportError:
            return
        # 走到这里说明产品改为"跳过脏行"，那就必须跳过而非填零
        self.fail(f"导入器放行了脏数据，返回了 {d.n_rows} 行：{d.rows}")

    # ---------------- 好的销量预测数据 ----------------
    def test_sales_forecast_headers(self):
        d = di.import_file(fx("sales_forecast.xlsx"))
        self.assertEqual(d.variables, ("月份序号", "线上销量", "线下销量"))
        self.assertEqual(d.target, "总利润")
        self.assertEqual(d.n_rows, 18)

    def test_sales_forecast_first_row(self):
        d = di.import_file(fx("sales_forecast.xlsx"))
        xs, y = d.rows[0]
        self.assertEqual(xs, (1.0, 112.0, 197.0))
        self.assertAlmostEqual(y, (112 + 197) * 0.35, places=3)

    # ---------------- 评分数据（浮点精度） ----------------
    def test_competition_score_precision(self):
        d = di.import_file(fx("competition_score.xlsx"))
        self.assertEqual(d.n_rows, 40)
        self.assertEqual(d.variables, ("难度", "创意", "完成度"))
        # 三位小数应被完整保留
        first = d.rows[0]
        self.assertAlmostEqual(first[1], 6.5439, places=4)
        # 列内所有值都应在 [0,10]
        for xs, y in d.rows:
            for v in xs:
                self.assertGreaterEqual(v, 0.0)
                self.assertLessEqual(v, 10.0)
            self.assertAlmostEqual(y, 0.3 * xs[0] + 0.4 * xs[1] + 0.3 * xs[2],
                                   delta=1e-3)

    # ---------------- 多 sheet ----------------
    def test_multi_sheet_reads_first_sheet_only(self):
        """只读第一个（活动）sheet，不被「说明」页干扰。"""
        d = di.import_file(fx("multi_sheet.xlsx"))
        self.assertEqual(d.variables, ("温度", "压强"))
        self.assertEqual(d.target, "产率")
        self.assertEqual(d.n_rows, 20)
        # 第二页的文本不该出现在数据里
        for xs, y in d.rows:
            for v in xs:
                self.assertIsInstance(v, float)

    def test_multi_sheet_linear_trend(self):
        """产率 = 50 + 1.5*(温度-20)，温度从 20 起。"""
        d = di.import_file(fx("multi_sheet.xlsx"))
        temps = d.column(0)
        yields = [y for _, y in d.rows]
        self.assertEqual(temps[0], 20.0)
        self.assertAlmostEqual(yields[0], 50.0, places=6)
        self.assertAlmostEqual(yields[-1], 50.0 + 1.5 * 19, places=6)

    # ---------------- 文件级行为 ----------------
    def test_all_fixtures_are_real_files(self):
        for name, p in self.files.items():
            self.assertTrue(p.exists(), f"{name} 未生成")
            self.assertGreater(p.stat().st_size, 1000, f"{name} 太小")
            self.assertEqual(p.suffix, ".xlsx")

    def test_expected_fixture_set(self):
        """fixture 集合应符合预期（防止漏加或误删）。"""
        self.assertEqual(set(self.files), {
            "linear_noisy.xlsx",
            "multi_var.xlsx",
            "sales_forecast.xlsx",
            "bad_text_column.xlsx",
            "infected_cells.xlsx",
            "competition_score.xlsx",
            "multi_sheet.xlsx",
        })

    def test_import_rejects_non_existent(self):
        from tests.fixtures import FIXTURE_DIR
        with self.assertRaises(di.DataImportError):
            di.import_file(FIXTURE_DIR / "不存在.xlsx")


if __name__ == "__main__":
    unittest.main(verbosity=2)
