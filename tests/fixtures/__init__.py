"""测试数据集 —— 数学建模常用场景的 Excel 文件。

这些是**真实落盘的 .xlsx**（不是内存构造），专测 data_import 的真实路径：
扩展名识别、表头解析、中英文列名、数字与字符串混合、坏数据、多 sheet 等。

约定：
- 所有文件放 tests/fixtures/，由本模块用 openpyxl 一次性生成
- 数据不是随机数，而是带已知规律/已知缺陷的，便于断言具体数值
- 生成函数幂等：重复调用结果一致，不依赖上次运行

数学建模场景覆盖：
| 文件 | 场景 | 用于验证 |
|------|------|----------|
| linear_noisy.xlsx | 一元线性 + 噪声 | 基础解析、数值类型 |
| multi_var.xlsx | 三元自变量 | 多变量、中文列名 |
| sales_forecast.xlsx | 销量预测（含月份文本列） | 非数值列处理/拒绝 |
| infected_cells.xlsx | 含坏单元格 | 空值、文本混入数值列 |
| competition_score.xlsx | 评分数据（大量小数） | 浮点精度 |
"""

from pathlib import Path

#: 本文件所在目录即 fixture 目录（不要再拼一层 "fixtures"）
FIXTURE_DIR = Path(__file__).resolve().parent


def build_linear_noisy(path: Path) -> Path:
    """一元线性 + 确定性噪声（可复现）。

    真实规律 y = 2.5x + 10，加对称波动，便于测试拟合残差范围。
    注意：导入约定是"最后一列为 y，其余全是自变量"，
    所以**不要**加序号列——那会变成第二个自变量，改变模型含义。
    """
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "linear"
    ws.append(["广告投入", "销售额"])
    for i in range(1, 31):
        x = float(i)
        noise = ((-1) ** i) * (i % 5) * 0.7      # 确定性"噪声"
        y = 2.5 * x + 10.0 + noise
        ws.append([x, round(y, 4)])
    wb.save(path)
    return path


def build_multi_var(path: Path) -> Path:
    """三元自变量 + 中文列名（数学建模题常见格式）。"""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "multi"
    ws.append(["施肥量", "浇水量", "光照时长", "产量"])
    for n in range(1, 26):
        a, b, c = float(n), float(n % 7 + 1), float(8 + n % 5)
        y = 0.8 * a + 1.2 * b + 2.0 * c + 5.0
        ws.append([a, b, c, round(y, 4)])
    wb.save(path)
    return path


def build_sales_forecast(path: Path) -> Path:
    """销量预测：月份用**数值**序号。

    导入约定是"最后一列为 y，其余全是自变量"。
    时间文本（'2024-01'）会被导入期拒绝，见 bad_text_column.xlsx。
    """
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "sales"
    ws.append(["月份序号", "线上销量", "线下销量", "总利润"])
    for m in range(1, 19):
        on, off = 100 + 12 * m, 200 - 3 * m
        ws.append([m, on, off, round((on + off) * 0.35, 4)])
    wb.save(path)
    return path


def build_bad_text_column(path: Path) -> Path:
    """坏数据：时间列是 '2024-01' 这种字符串。

    导入器要求除 y 外全为数值，所以这文件应在**导入期**就报
    DataImportError（错误信息带行号与列名）。用于锁定这个严格行为。
    """
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "bad"
    ws.append(["月份", "线上销量", "线下销量", "总利润"])
    for m in range(1, 19):
        on, off = 100 + 12 * m, 200 - 3 * m
        ws.append([f"2024-{m:02d}", on, off, round((on + off) * 0.35, 4)])
    wb.save(path)
    return path


def build_infected_cells(path: Path) -> Path:
    """坏数据：空单元格 + 文本混进数值列。

    同样应在导入期报错，而不是把 None / '缺失' 静默变成 0。
    """
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "messy"
    ws.append(["x1", "x2", "y"])
    rows = [
        (1.0, 2.0, 10.0),
        (2.0, None, 20.0),        # 空单元格
        (3.0, 4.0, "缺失"),        # 文本混进数值列
        (4.0, 6.0, 40.0),
        (None, 8.0, 50.0),        # 空 x1
        (6.0, 10.0, None),        # 空 y
        (7.0, 12.0, 70.0),
    ]
    for r in rows:
        ws.append(list(r))
    wb.save(path)
    return path


def build_competition_score(path: Path) -> Path:
    """评分数据：大量小数，考察浮点往返精度。"""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "scores"
    ws.append(["难度", "创意", "完成度", "总分"])
    import random
    rng = random.Random(20260924)     # 固定 seed
    for _ in range(40):
        a = round(rng.uniform(0, 10), 3)
        b = round(rng.uniform(0, 10), 3)
        c = round(rng.uniform(0, 10), 3)
        total = round(0.3 * a + 0.4 * b + 0.3 * c, 4)
        ws.append([a, b, c, total])
    wb.save(path)
    return path


def build_multi_sheet(path: Path) -> Path:
    """多 sheet 工作簿：第一个 sheet 是数据，第二个是说明。

    验证导入器只读第一个（活动）sheet，不被说明页干扰。
    """
    import openpyxl
    wb = openpyxl.Workbook()
    ws1 = wb.active
    ws1.title = "数据"
    ws1.append(["温度", "压强", "产率"])
    for i in range(20):
        t, p = 20 + i, 1.0 + i * 0.05
        ws1.append([t, round(p, 3), round(50 + 1.5 * i, 2)])

    ws2 = wb.create_sheet("说明")
    ws2.append(["本文件用于测试"])
    ws2.append(["第二页不应被读取"])
    wb.save(path)
    return path


#: 文件名 -> 生成函数
BUILDERS = {
    "linear_noisy.xlsx": build_linear_noisy,
    "multi_var.xlsx": build_multi_var,
    "sales_forecast.xlsx": build_sales_forecast,
    "bad_text_column.xlsx": build_bad_text_column,
    "infected_cells.xlsx": build_infected_cells,
    "competition_score.xlsx": build_competition_score,
    "multi_sheet.xlsx": build_multi_sheet,
}


def ensure_fixtures() -> dict:
    """确保所有 fixture 存在，返回 {文件名: Path}。

    幂等：已存在且非空就直接复用，不重复生成。
    """
    try:
        import openpyxl  # noqa: F401
    except ImportError as exc:      # pragma: no cover
        raise RuntimeError(
            "生成 fixture 需要 openpyxl：pip install openpyxl"
        ) from exc

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    out = {}
    for name, builder in BUILDERS.items():
        p = FIXTURE_DIR / name
        if not p.exists() or p.stat().st_size == 0:
            builder(p)
        out[name] = p
    return out


def path_of(name: str) -> Path:
    """取某个 fixture 的路径（不存在则先生成）。"""
    ensure_fixtures()
    return FIXTURE_DIR / name


if __name__ == "__main__":
    built = ensure_fixtures()
    for name, p in built.items():
        print(f"{name:26s} {p.stat().st_size:6d} bytes")
