"""离散点自动建模（曲线拟合）—— 纯计算层。

只依赖标准库，复用 graph_engine 的采样 / 缩放工具，不依赖 tkinter，
因此可以在无窗口环境下完整测试。

设计要点
--------
1. 最小二乘用**正态方程 + 高斯消元（部分选主元）**求解，不引入 numpy 依赖。
2. 拟合前先把 x 归一到 [-1, 1]（t = (x - c) / s）。
   像素坐标下 x 可达 1000，六阶的 x^12 是 1e36，正态方程会彻底病态；
   归一化后 t ∈ [-1, 1]，数值稳定。fit.polynomial 已验证
   x ∈ [0, 700] 时二次系数可恢复到 ~1e-6 相对误差。
3. 拟合完再把系数从 t 空间还原到 x 空间，用于展示表达式；
   predict() 始终在 t 空间求值，避免大数幂运算。
4. 模型选择默认**从简**：在 1 ~ max_degree 阶里挑 R² 与最优相差不超过
   tol 的最低阶。少量数据点配高阶模型只会得到插值，R² 恒为 1 但没有意义。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

import graph_engine as ge

#: 默认最高阶数（下拉/自动建模都用它）
DEFAULT_MAX_DEGREE = 4
#: 绝对上限，防止用户手滑选过高阶
MAX_DEGREE = 8
#: 选"更高阶"的门槛：R² 需再提升这么多
R2_TOLERANCE = 0.005
#: 相对改进门槛：RMSE 需再降低这个比例（0.15 = 至少好 15%）。
#: 真实手绘数据普遍带噪声，用纯 R² 绝对差做阈值会让"自动"一路爬到最高阶，
#: 给用户一个过度拟合、没法用的复杂表达式。因此同时要求"误差有实质下降"。
RMSE_RELATIVE_GAIN = 0.15
#: 表达式保留小数位
COEF_DECIMALS = 3

_SUPERSCRIPTS = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")


class FitError(Exception):
    """无法完成拟合。"""


@dataclass(frozen=True)
class FitResult:
    """一次拟合的结果。

    coefficients         x 空间系数，低次在前（[a0, a1, ...]），仅供展示
    norm_coeffs          t 空间系数，低次在前，predict 实际用它求值
    r2                   决定系数，可能为负（拟合比平均值还差）
    rmse                 均方根误差（像素）
    """

    degree: int
    coefficients: tuple
    norm_coeffs: tuple
    norm_center: float
    norm_scale: float
    r2: float
    rmse: float
    points_used: int
    expression: str

    def predict(self, x: float) -> float:
        """在像素 x 处求值。"""
        t = (float(x) - self.norm_center) / self.norm_scale
        acc = 0.0
        for c in reversed(self.norm_coeffs):
            acc = acc * t + c
        return acc

    def predict_many(self, xs: Iterable[float]) -> list:
        return [self.predict(x) for x in xs]

    @property
    def summary(self) -> str:
        return (
            f"{self.expression}\n"
            f"R² = {self.r2:.4f}    RMSE = {self.rmse:.3f} px\n"
            f"数据点: {self.points_used}    阶数: {self.degree}"
        )


@dataclass(frozen=True)
class FitDisplay:
    """拟合结果在画布上的呈现形式（点与曲线共用同一缩放比例）。"""

    curve: list       # list[ge.SamplePoint]
    points: list      # list[ge.SamplePoint]，已按同一 scale/offset 映射
    scale: float
    offset: float


# ==================================================================
# 输入校验
# ==================================================================
def _validate_points(points: Sequence) -> tuple:
    """把 [(x, y), ...] 转成 (xs, ys) 两个 float 列表，并做基本校验。"""
    if not points:
        raise FitError("还没有采集到离散点")
    if len(points) == 1:
        raise FitError("至少需要 2 个离散点才能拟合")

    xs: list = []
    ys: list = []
    for p in points:
        try:
            x, y = float(p[0]), float(p[1])
        except (TypeError, ValueError, IndexError) as e:
            raise FitError(f"点格式应为 (x, y)，收到: {p!r}") from e
        if not (math.isfinite(x) and math.isfinite(y)):
            raise FitError(f"点坐标必须是有限数值: ({x}, {y})")
        xs.append(x)
        ys.append(y)

    if max(xs) - min(xs) <= 0.0:
        raise FitError("所有点的 x 相同，无法拟合")
    return xs, ys


def max_useful_degree(n_points: int, distinct_x: int, max_degree: int) -> int:
    """数据量允许的最高阶数。

    - 2 个点：只能定一条直线
    - 其余：阶数 ≤ min(max_degree, 点数 - 2, 不同 x 数 - 1)
      留一个自由度做残差，否则 R² 恒等于 1（纯插值，没有泛化意义）
    """
    if n_points < 2:
        return 0
    if n_points == 2:
        return 1
    return max(1, min(max_degree, n_points - 2, distinct_x - 1))


# ==================================================================
# 线性代数：正态方程 + 高斯消元
# ==================================================================
def _solve_linear(matrix: list, rhs: list) -> list:
    """解 n×n 线性方程组（部分选主元高斯消元）。"""
    n = len(matrix)
    a = [list(row) + [rhs[i]] for i, row in enumerate(matrix)]

    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-12:
            raise FitError("系数矩阵奇异，无法求解（可能 x 值重复过多）")
        a[col], a[pivot] = a[pivot], a[col]
        for r in range(col + 1, n):
            factor = a[r][col] / a[col][col]
            if factor == 0.0:
                continue
            for c in range(col, n + 1):
                a[r][c] -= factor * a[col][c]

    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        acc = a[i][n]
        for j in range(i + 1, n):
            acc -= a[i][j] * x[j]
        x[i] = acc / a[i][i]
    return x


def _solve_normal_equations(ts: Sequence[float], ys: Sequence[float], degree: int) -> list:
    """在归一化坐标 t 上解最小二乘，返回低次在前的系数。"""
    size = degree + 1
    power_sums = [0.0] * (2 * degree + 1)
    rhs = [0.0] * size

    for t, y in zip(ts, ys):
        tp = 1.0
        for k in range(2 * degree + 1):
            power_sums[k] += tp
            if k < size:
                rhs[k] += tp * y
            tp *= t

    matrix = [
        [power_sums[row + col] for col in range(size)] for row in range(size)
    ]
    return _solve_linear(matrix, rhs)


def _denormalize(coeffs_t: Sequence[float], center: float, scale: float) -> list:
    """把 t 空间系数还原成 x 空间系数（低次在前）。

    t = (x - c) / s  →  Σ b_j t^j 展开成 Σ a_m x^m
    a_m = Σ_{j≥m} b_j · C(j, m) · (-c)^(j-m) / s^j
    """
    d = len(coeffs_t) - 1
    a = [0.0] * (d + 1)
    for j, bj in enumerate(coeffs_t):
        sj = scale ** j
        if sj == 0.0:
            continue
        for m in range(j + 1):
            a[m] += bj * math.comb(j, m) * ((-center) ** (j - m)) / sj
    return a


# ==================================================================
# 表达式格式化
# ==================================================================
def _num_str(value: float, decimals: int = COEF_DECIMALS) -> str:
    """把数值转成紧凑字符串：去掉多余的 0，-0 归一成 0。"""
    s = f"{value:.{decimals}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    if s in ("", "-", "-0"):
        return "0"
    return s


def format_polynomial(coeffs: Sequence[float], decimals: int = COEF_DECIMALS) -> str:
    """把低次在前的系数格式化为 "y = 2x² - 1.5x + 0.7"。"""
    rounded = [round(float(c), decimals) for c in coeffs]
    if all(c == 0.0 for c in rounded):
        return "y = 0"

    terms: list = []
    for power in range(len(rounded) - 1, -1, -1):
        coeff = rounded[power]
        if coeff == 0.0:
            continue
        mag = abs(coeff)
        if power == 0:
            body = _num_str(mag, decimals)
        elif power == 1:
            body = "x" if mag == 1.0 else f"{_num_str(mag, decimals)}x"
        else:
            sup = str(power).translate(_SUPERSCRIPTS)
            body = f"x{sup}" if mag == 1.0 else f"{_num_str(mag, decimals)}x{sup}"
        terms.append((coeff > 0.0, body))

    out = "y = "
    for i, (positive, body) in enumerate(terms):
        if i == 0:
            out += ("-" if not positive else "") + body
        else:
            out += (" + " if positive else " - ") + body
    return out


# ==================================================================
# 拟合入口
# ==================================================================
def _rmse(ys: Sequence[float], preds: Sequence[float]) -> float:
    """均方根误差，同样做防溢出降幅。"""
    scale_down = max((abs(y) for y in ys), default=1.0)
    scaled = scale_down if scale_down > 1.0 else 1.0
    ss_res = math.fsum(((y - p) / scaled) ** 2 for y, p in zip(ys, preds))
    val = math.sqrt(ss_res / len(ys)) * scaled
    return val if math.isfinite(val) else float("inf")


def _r_squared(ys: Sequence[float], preds: Sequence[float]) -> float:
    n = len(ys)
    mean = sum(ys) / n
    # 用 math.fsum 分项累加；对超大 y 做预降幅，避免 (y-mean)**2 溢出成 inf
    devs = [y - mean for y in ys]
    scale_down = max((abs(d) for d in devs), default=1.0)
    scaled = scale_down if scale_down > 1.0 else 1.0
    ss_tot = math.fsum((d / scaled) ** 2 for d in devs)
    ss_res = math.fsum(((y - p) / scaled) ** 2 for y, p in zip(ys, preds))

    if not math.isfinite(ss_tot) or not math.isfinite(ss_res):
        # 数值上溢：直接判定拟合不算成功，交给上层拒绝
        return 0.0
    if ss_tot <= 1e-12:
        # 所有 y 相同：完全拟合算满分，否则 0 分
        return 1.0 if ss_res <= 1e-9 else 0.0
    return 1.0 - ss_res / ss_tot


def fit_polynomial(points: Sequence, degree: int) -> FitResult:
    """对离散点做指定阶数的多项式最小二乘拟合。"""
    if degree < 1:
        raise FitError("阶数至少为 1")
    if degree > MAX_DEGREE:
        raise FitError(f"阶数上限为 {MAX_DEGREE}，当前 {degree}")

    xs, ys = _validate_points(points)
    if len(set(xs)) <= degree:
        raise FitError(f"{degree} 阶拟合需要至少 {degree + 1} 个 x 值不同的点")

    center = sum(xs) / len(xs)
    half_range = max(abs(x - center) for x in xs)
    scale = half_range if half_range > 0.0 else 1.0
    ts = [(x - center) / scale for x in xs]

    coeffs_t = _solve_normal_equations(ts, ys, degree)

    def _eval_t(t: float, coeffs: Sequence[float]) -> float:
        acc = 0.0
        for c in reversed(coeffs):
            acc = acc * t + c
        return acc

    preds = [_eval_t(t, coeffs_t) for t in ts]
    if not all(math.isfinite(p) for p in preds):
        raise FitError("拟合结果出现非有限值，请尝试更低阶数")

    r2 = _r_squared(ys, preds)
    rmse = _rmse(ys, preds)

    coeffs_x = _denormalize(coeffs_t, center, scale)
    if not all(math.isfinite(c) for c in coeffs_x):
        raise FitError("系数还原失败")

    return FitResult(
        degree=degree,
        coefficients=tuple(coeffs_x),
        norm_coeffs=tuple(coeffs_t),
        norm_center=center,
        norm_scale=scale,
        r2=r2,
        rmse=rmse,
        points_used=len(xs),
        expression=format_polynomial(coeffs_x),
    )


def fit_candidates(points: Sequence, max_degree: int = DEFAULT_MAX_DEGREE) -> list:
    """列出所有可行阶数的拟合结果（阶数升序）。

    供 UI 下拉框展示"每阶的 R²"，让用户自行选择。
    """
    if max_degree < 1:
        raise FitError("max_degree 至少为 1")
    max_degree = min(max_degree, MAX_DEGREE)

    xs, _ = _validate_points(points)
    cap = max_useful_degree(len(xs), len(set(xs)), max_degree)

    results: list = []
    for degree in range(1, cap + 1):
        try:
            results.append(fit_polynomial(points, degree))
        except FitError:
            break  # 阶数更高也不可能成功
    if not results:
        raise FitError("数据点不足，连直线都无法拟合")
    return results


def select_model(results: Sequence, tol: float = R2_TOLERANCE) -> FitResult:
    """在候选模型里挑"最简单且足够好"的那个。

    逐阶升阶，满足任一条件才接受更高阶：
      A. R² 提升超过 R2_TOLERANCE，或
      B. RMSE 相对下降超过 RMSE_RELATIVE_GAIN
    否则停在使用当前阶。

    实测修正记录：
    - 只用条件 A + `best_r2 - tol` 的"绝对达标"写法时，真实手绘噪声数据
      会从 2 阶一路爬到 4 阶（R² 仅 0.46→0.755），表达式复杂到不可用；
    - 只用条件 B 时，含高次小系数的干净三次数据会被 1 阶截住
      （R²=0.9986 但表达式错成直线）。
    因此两条件取"或"，兼顾噪声抑制与高次项识别。
    """
    if not results:
        raise FitError("没有候选模型")

    ordered = sorted(results, key=lambda r: r.degree)
    best = ordered[0]

    for r in ordered[1:]:
        r2_gain = r.r2 - best.r2
        rmse_gain = 1.0 - (r.rmse / best.rmse) if best.rmse > 0 else 0.0
        if r2_gain >= tol or rmse_gain >= RMSE_RELATIVE_GAIN:
            best = r        # 有实质收益，升阶
        else:
            break           # 收益不足，停在这里

    return best


def auto_fit(points: Sequence, max_degree: int = DEFAULT_MAX_DEGREE) -> FitResult:
    """自动建模：一步完成候选 + 选择。"""
    return select_model(fit_candidates(points, max_degree))


# ==================================================================
# 画布呈现
# ==================================================================
def build_fit_display(
    fit: FitResult,
    points: Sequence,
    width: float,
    height: float,
    step: int = 2,
) -> FitDisplay:
    """把拟合曲线与数据点都换算成画布坐标。

    关键点：曲线与点必须用**同一个** scale/offset，
    否则缩放后点会偏离曲线，"拟合效果"就看不出来了。
    """
    if width <= 0 or height <= 0:
        raise FitError("画布尺寸无效")

    px = [float(p[0]) for p in points]
    py = [float(p[1]) for p in points]

    curve_raw = [(float(x), fit.predict(float(x))) for x in ge.sample_x(width, step)]
    if not curve_raw:
        raise FitError("无法采样曲线")

    if not all(math.isfinite(y) for _, y in curve_raw):
        raise FitError("拟合曲线出现非有限值")

    all_y = py + [y for _, y in curve_raw]
    scale = ge.compute_auto_scale(all_y, height)

    def _map(x: float, y_raw: float) -> ge.SamplePoint:
        return ge.SamplePoint(
            x=x,
            y_raw=y_raw,
            screen_x=int(round(x)),
            screen_y=y_raw * scale.scale + scale.offset,
        )

    return FitDisplay(
        curve=[_map(x, y) for x, y in curve_raw],
        points=[_map(x, y) for x, y in zip(px, py)],
        scale=scale.scale,
        offset=scale.offset,
    )


# ==================================================================
# 多变量最小二乘（Excel 表格数据集拟合）
# ==================================================================
@dataclass(frozen=True)
class MultiFitResult:
    """多变量线性最小二乘结果。

    coefficients  低次/低位在前，长度为 1 + n（首位常数项）
    variable      自变元名，与 data_import.ImportedData.variables 同源
    target        y 列名
    """

    variable: tuple
    target: str
    coefficients: tuple
    r2: float
    rmse: float
    rows_used: int
    expression: str

    def predict(self, xs) -> float:
        """按 variable 顺序传入数值，返回预测 y。"""
        if len(xs) != len(self.variable):
            raise FitError(
                f"需要 {len(self.variable)} 个输入值 "
                f"({', '.join(self.variable)})，收到 {len(xs)} 个"
            )
        return self.coefficients[0] + sum(
            float(a) * float(b) for a, b in zip(self.coefficients[1:], xs)
        )

    def predict_all(self, rows) -> list:
        return [self.predict(r) for r in rows]

    @property
    def summary(self) -> str:
        return (
            f"{self.expression}\n"
            f"R² = {self.r2:.4f}    RMSE = {self.rmse:.3f}\n"
            f"数据点: {self.rows_used}    变量: {len(self.variable)}"
        )


def _normalize_column(values: Sequence[float]) -> Tuple[list, float, float]:
    """把一列数值归一到 [-1, 1]，返回 (归一化值, 中心, 尺度)。

    与单变量同理：像素/物理量级下不做归一化会让正态方程病态。
    """
    if not values:
        raise FitError("数值列为空")
    center = sum(values) / len(values)
    half = max(abs(v - center) for v in values)
    scale = half if half > 0.0 else 1.0
    return [(v - center) / scale for v in values], center, scale


def format_multi(coefficients, variables: Sequence[str],
                 target: str = "y", decimals: int = COEF_DECIMALS) -> str:
    """把多变量系数格式化为可读表达式，如:

        y = 3.2 + 1.5x1 - 0.8x2
    """
    rounded = [round(float(c), decimals) for c in coefficients]
    names = list(variables)

    if all(c == 0.0 for c in rounded):
        return f"{target} = 0"

    parts = []
    const = rounded[0]
    for i, coeff in enumerate(rounded[1:], start=1):
        if coeff == 0.0:
            continue
        name = names[i - 1] if i - 1 < len(names) else f"x{i}"
        parts.append((coeff > 0.0, coeff, name))

    out = f"{target} = "
    if const != 0.0 or not parts:
        out += _num_str(const, decimals)
    for i, (positive, coeff, name) in enumerate(parts):
        mag = abs(coeff)
        body = name if mag == 1.0 else f"{_num_str(mag, decimals)}{name}"
        if i == 0 and const == 0.0:
            out += ("-" if not positive else "") + body
        else:
            out += (" + " if positive else " - ") + body
    return out


def fit_multi(
    rows: Sequence,
    variables: Sequence[str],
    target: str = "y",
) -> MultiFitResult:
    """对多变量数据集做线性最小二乘。

    rows       [(x_tuple, y), ...]，与 data_import.ImportedData.rows 同构
    variables  自变元名，仅用于输出表达式；数量必须与 x_tuple 长度一致
    """
    if not rows:
        raise FitError("没有可用的数据行")
    if not variables:
        raise FitError("没有自变元")

    n_vars = len(variables)
    for i, row in enumerate(rows):
        if len(row) != 2:
            raise FitError(f"第 {i + 1} 行格式应为 (x_tuple, y)")
        if len(row[0]) != n_vars:
            raise FitError(
                f"第 {i + 1} 行有 {len(row[0])} 个输入值，"
                f"与变量数 {n_vars} 不符"
            )

    xs_all = [tuple(float(v) for v in r[0]) for r in rows]
    ys = [float(r[1]) for r in rows]

    if len(ys) < 2:
        raise FitError("至少需要 2 行数据")

    # 线性模型有 n_vars + 1 个待定参数（含常数项）。
    # 样本数 <= 参数个数时无法确定唯一解（会得到插值或多解），
    # 这里提前拒绝，避免下面抛出卖弄术语的"系数矩阵奇异"。
    if len(ys) <= n_vars + 1:
        raise FitError(
            f"{n_vars} 个变量需要至少 {n_vars + 2} 行数据"
            f"（还要留 1 行自由度），当前只有 {len(ys)} 行"
        )

    # 每列独立归一化
    norm_cols = []
    centers = []
    scales = []
    for j in range(n_vars):
        col = [r[j] for r in xs_all]
        if max(col) - min(col) <= 0.0:
            raise FitError(f"变量 {variables[j]} 的所有取值相同，无法建模")
        nc, c, s = _normalize_column(col)
        norm_cols.append(nc)
        centers.append(c)
        scales.append(s)

    # 设计矩阵：[1, t1, t2, ...]（常数项 + 归一化自变元）
    size = n_vars + 1
    pts = []
    for i in range(len(ys)):
        row = [1.0] + [norm_cols[j][i] for j in range(n_vars)]
        pts.append(row)

    # 共线检测要放在求解之前：提前给出可操作的提示，
    # 而不是让高斯消元抛出一句"系数矩阵奇异"让用户无从下手。
    collinear = _detect_collinear(norm_cols, variables)
    if collinear:
        a_name, b_name = collinear
        raise FitError(
            f"自变量 '{a_name}' 与 '{b_name}' 完全成比例（线性相关），"
            f"信息重复，无法同时进入模型。\n"
            f"请去掉其中一个，或补充彼此独立的变量。"
        )

    coeffs = _solve_normal_equations_2d(pts, ys)

    preds = [sum(c * v for c, v in zip(coeffs, row)) for row in pts]
    if not all(math.isfinite(p) for p in preds):
        raise FitError("拟合结果出现非有限值，请检查数据")

    r2 = _r_squared(ys, preds)
    rmse = _rmse(ys, preds)

    # 系数还原为原始尺度：b0 + Σ bj * (xj - cj) / sj
    const = coeffs[0]
    linear = [coeffs[j + 1] / scales[j] for j in range(n_vars)]
    for j in range(n_vars):
        const -= linear[j] * centers[j]

    expression = format_multi([const] + linear, variables, target)

    return MultiFitResult(
        variable=tuple(variables),
        target=target,
        coefficients=tuple([const] + linear),
        r2=r2,
        rmse=rmse,
        rows_used=len(ys),
        expression=expression,
    )


def _solve_normal_equations_2d(pts: Sequence[Sequence[float]],
                               ys: Sequence[float]) -> list:
    """解 [1, t1..tn] 形式的最小二乘，部分选主元。"""
    size = len(pts[0])
    matrix = [[0.0] * size for _ in range(size)]
    rhs = [0.0] * size
    for row, y in zip(pts, ys):
        for a in range(size):
            rhs[a] += row[a] * y
            for b in range(size):
                matrix[a][b] += row[a] * row[b]
    return _solve_linear(matrix, rhs)


def _detect_collinear(norm_cols: Sequence[Sequence[float]],
                      variables: Sequence[str]) -> str | None:
    """找出互相完全线性相关的列，用于给出可操作的错误信息。

    归一化后两列若相同（相关系数为 ±1），说明这两个自变量提供的信息
    完全重复，共同参与建模必然奇异。
    """
    n = len(norm_cols)
    for a in range(n):
        for b in range(a + 1, n):
            ca, cb = norm_cols[a], norm_cols[b]
            if len(ca) < 2:
                continue
            ma = sum(ca) / len(ca)
            mb = sum(cb) / len(cb)
            cov = sum((x - ma) * (y - mb) for x, y in zip(ca, cb))
            va = sum((x - ma) ** 2 for x in ca)
            vb = sum((y - mb) ** 2 for y in cb)
            if va <= 1e-12 or vb <= 1e-12:
                continue
            if abs(cov) >= 0.999999 * math.sqrt(va * vb):
                return variables[a], variables[b]
    return None


def multi_candidates(
    rows: Sequence,
    variables: Sequence[str],
    target: str = "y",
) -> list:
    """返回各"单变量 + 全变量"候选，便于判断哪个变量真正有用。

    对每个自变元单独做一元拟合，再加上全部变量一起的多元拟合，
    返回 (说明, 拟合结果) 列表，按 R² 降序。

    "全部变量"失败时不静默跳过：把它作为 (说明, None) 放进结果，
    让 UI 能把原因显示出来，而不是让用户疑惑"为什么最多的那个模型不在列表里"。
    """
    out = []
    for j, name in enumerate(variables):
        sub = [((r[0][j],), r[1]) for r in rows]
        try:
            out.append((f"仅用 {name}", fit_multi(sub, (name,), target)))
        except FitError:
            pass
    try:
        out.append(("全部变量", fit_multi(rows, variables, target)))
    except FitError as e:
        out.append(("全部变量", e))
    out.sort(key=lambda item: _cand_sort_key(item))
    return out


def _cand_sort_key(item):
    """R² 降序；失败项（FitError）排在最后。"""
    r = item[1]
    if isinstance(r, FitError):
        return (1, 0.0)     # 失败项排最后
    return (0, -r.r2)       # 成功项按 R² 降序


__all__ = [
    "FitError",
    "FitResult",
    "FitDisplay",
    "MultiFitResult",
    "DEFAULT_MAX_DEGREE",
    "MAX_DEGREE",
    "R2_TOLERANCE",
    "RMSE_RELATIVE_GAIN",
    "format_polynomial",
    "format_multi",
    "fit_polynomial",
    "fit_candidates",
    "select_model",
    "auto_fit",
    "fit_multi",
    "multi_candidates",
    "max_useful_degree",
    "build_fit_display",
]
