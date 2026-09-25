"""Excel / 表格数据导入 —— 纯解析层，不依赖 tkinter。

支持格式（按优先级自动识别）：
    .xlsx   openpyxl（可选依赖，装了就支持）
    .csv    标准库 csv，UTF-8-SIG / GBK 自动探测
    .tsv    同上
    .txt    同 csv，按制表符或逗号推断
    .xls    明确报错提示另存为 .xlsx（本环境无 xlrd）

数据约定（用户需求）：
    第一行        → 数据名（表头），最后一列是 y，前面全是 xi
    其余行        → 数值
    列名            → 参与输出表达式，例如 x1、时间、温度
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path
from typing import List, Sequence, Tuple

#: 表头行里允许出现的 y 列名（大小写不敏感）
Y_NAMES = ("y", "Y", "因变量", "目标")
#: 支持的后缀 → 读取方式
SUPPORTED_SUFFIXES = (".xlsx", ".csv", ".tsv", ".txt")


class DataImportError(Exception):
    """导入失败。"""


@dataclass(frozen=True)
class ImportedData:
    """一份导入的数据集。

    variables   自变元名（表头去掉 y 列，原样保留，如 ["x1", "x2"] 或 ["时间", "温度"]）
    target      y 列名（表头最后一个）
    rows        [(x_tuple, y), ...]  数值行
    source      来源路径
    """

    variables: tuple
    target: str
    rows: tuple
    source: Path

    @property
    def n_rows(self) -> int:
        return len(self.rows)

    @property
    def n_vars(self) -> int:
        return len(self.variables)

    @property
    def n_cols(self) -> int:
        return self.n_vars + 1   # 加上 y

    def column(self, index: int) -> List[float]:
        """第 index 个自变元的全部取值。"""
        return [float(r[0][index]) for r in self.rows]

    def targets(self) -> List[float]:
        return [float(r[1]) for r in self.rows]

    def describe(self) -> str:
        xs = ", ".join(self.variables) or "(无)"
        rng = []
        for i, name in enumerate(self.variables):
            col = self.column(i)
            rng.append(f"{name}∈[{min(col):g}, {max(col):g}]")
        return (
            f"文件: {self.source.name}\n"
            f"行数: {self.n_rows}    变量数: {self.n_vars}\n"
            f"变量: {xs}\n"
            f"目标: {self.target}\n"
            f"范围: {'; '.join(rng) if rng else '(无)'}"
        )


# ==================================================================
# 底层读取：xlsx
# ==================================================================
def _read_xlsx(path: Path) -> List[List[str]]:
    """用 openpyxl 读第一个工作表，返回字符串网格（空单元格为 ""）。"""
    try:
        import openpyxl
    except ImportError:
        raise DataImportError(
            "读取 .xlsx 需要 openpyxl。\n请先安装:  pip install openpyxl"
        ) from None

    try:
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    except Exception as e:
        raise DataImportError(f"无法打开 xlsx 文件: {e}") from e

    try:
        ws = wb.worksheets[0]
        grid: List[List[str]] = []
        for row in ws.iter_rows(values_only=True):
            cells = []
            for v in row:
                if v is None:
                    cells.append("")
                elif isinstance(v, float) and v.is_integer():
                    cells.append(str(int(v)))   # 3.0 -> "3"，避免 "3.0" 干扰
                else:
                    cells.append(str(v).strip())
            # 去掉尾部空单元格，避免 final_columns 被撑大
            while cells and cells[-1] == "":
                cells.pop()
            grid.append(cells)
    except Exception as e:
        raise DataImportError(f"解析 xlsx 失败: {e}") from e
    finally:
        wb.close()
    return grid


# ==================================================================
# 底层读取：csv / tsv / txt
# ==================================================================
def _decode(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gbk", "big5"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _detect_delimiter(sample: str) -> str:
    first = next((ln for ln in sample.splitlines() if ln.strip()), "")
    counts = {d: first.count(d) for d in (",", "\t", ";", "|")}
    best = max(counts, key=lambda d: counts[d])
    return best if counts[best] > 0 else ","


def _read_delimited(path: Path) -> List[List[str]]:
    text = _decode(path)
    delim = _detect_delimiter(text)
    grid: List[List[str]] = []
    for row in csv.reader(text.splitlines(), delimiter=delim):
        cells = [c.strip() for c in row]
        while cells and cells[-1] == "":
            cells.pop()
        grid.append(cells)
    return grid


# ==================================================================
# 网格 → ImportedData
# ==================================================================
def _to_float(text: str, where: str) -> float:
    s = text.strip().replace(",", "").replace("，", "")
    if not s:
        raise DataImportError(f"{where} 是空的")
    # 去掉可能的前导货币/百分号，宽容处理
    s = s.lstrip("¥$＄").rstrip("%")
    try:
        v = float(s)
    except ValueError:
        raise DataImportError(f"{where} 不是有效数值: {text!r}") from None
    if not math.isfinite(v):
        raise DataImportError(f"{where} 不是有限数值: {text!r}")
    return v


def parse_grid(grid: Sequence[Sequence[str]], source: Path,
               header_rows: int = 1) -> ImportedData:
    if not grid:
        raise DataImportError("文件是空的")

    # 保留原始行号：先把"全空行"筛掉时记住它们在原文件中的位置，
    # 这样报错时能给出用户看得到的行号，而不是筛完后的序号。
    kept: List[Tuple[int, List[str]]] = []
    for idx, row in enumerate(grid, start=1):
        if any(c != "" for c in row):
            kept.append((idx, list(row)))

    if len(kept) < 2:
        raise DataImportError(
            "至少需要 2 行：第一行是数据名（表头），其余是数值"
        )

    header_line, header = kept[0]
    n_cols = len(header)
    if n_cols < 2:
        raise DataImportError(
            f"至少需要 2 列（x1..xn 与最后一列 y），当前只有 {n_cols} 列"
        )

    # 数据行列数必须与表头一致。不一致时**不能**静默按表头截断：
    # 那会悄悄丢掉一整列数值，用户完全看不出少了数据。
    for line_no, raw in kept[1:]:
        if len(raw) > n_cols:
            raise DataImportError(
                f"源文件第 {line_no} 行有 {len(raw)} 列，"
                f"但第 {header_line} 行表头只有 {n_cols} 列。\n"
                f"多余数据（{raw[n_cols]}）会被忽略——"
                f"请补齐表头或删除多余数据。"
            )
        if len(raw) < n_cols:
            raise DataImportError(
                f"源文件第 {line_no} 行只有 {len(raw)} 列，"
                f"少于表头的 {n_cols} 列。\n"
                f"请补全该行（缺失列的内容为空）。"
            )

    # 表头不能是纯数字（说明没有表头）
    numeric_header = all(_is_numberish(c) for c in header if c != "")
    if numeric_header:
        raise DataImportError(
            "第一行看起来是数值而不是数据名。\n"
            "请确保第一行是表头（如 x1, x2, y）"
        )

    # 表头去重/补空，保证输出表达式可用
    names = _sanitize_names(header)

    variables = names[:-1]
    target = names[-1]

    rows: List[Tuple[Tuple[float, ...], float]] = []
    for line_no, raw in kept[1:]:
        xs = tuple(
            _to_float(raw[j], f"源文件第 {line_no} 行第 {j + 1} 列 ({names[j]})")
            for j in range(n_cols - 1)
        )
        y = _to_float(raw[n_cols - 1],
                      f"源文件第 {line_no} 行最后一列 ({target})")
        rows.append((xs, y))

    if len(rows) < 2:
        raise DataImportError("至少需要 2 行数值")

    # 自变元不能全相同（无法建模）
    for j, name in enumerate(variables):
        col = [r[0][j] for r in rows]
        if max(col) - min(col) <= 0:
            raise DataImportError(f"变量 {name} 的所有取值相同，无法用于建模")

    return ImportedData(
        variables=tuple(variables), target=target, rows=tuple(rows), source=source
    )


def _is_numberish(s: str) -> bool:
    if not s:
        return False
    try:
        float(s.strip().replace(",", ""))
        return True
    except ValueError:
        return False


def _sanitize_names(header: Sequence[str]) -> List[str]:
    """表头清洗：空名补名、重名加序号、去掉首尾空白。"""
    seen: dict = {}
    out: List[str] = []
    for i, raw in enumerate(header):
        name = str(raw).strip()
        if not name:
            name = f"c{i + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 1
        out.append(name)
    return out


def import_file(path) -> ImportedData:
    """导入 xlsx/csv/tsv/txt 文件。"""
    p = Path(path)
    if not p.is_file():
        raise DataImportError(f"文件不存在: {p}")

    suffix = p.suffix.lower()

    if suffix == ".xls":
        raise DataImportError(
            "暂不支持旧版 .xls 格式。\n"
            "请用 Excel 打开该文件，另存为 .xlsx 后再导入。"
        )
    if suffix == ".xlsx":
        grid = _read_xlsx(p)
    elif suffix in (".csv", ".tsv", ".txt"):
        grid = _read_delimited(p)
    else:
        raise DataImportError(
            f"不支持的文件类型: {suffix}\n"
            f"支持: {', '.join(SUPPORTED_SUFFIXES)}"
        )

    return parse_grid(grid, p)


__all__ = [
    "DataImportError",
    "ImportedData",
    "import_file",
    "parse_grid",
    "SUPPORTED_SUFFIXES",
]
