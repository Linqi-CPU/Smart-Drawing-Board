"""纯计算引擎：函数采样、自适应缩放、屏幕坐标换算。

本模块不依赖 tkinter，可在任何环境（含单元测试）中独立运行。
所有函数签名统一为 func(x, width, center_y, amp, freq) -> float
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Iterable, List, Sequence, Tuple

# 函数类型标识（与 UI 单选项对应）
SINE = "sine"
COSINE = "cosine"
PARABOLA = "parabola"
LINEAR = "linear"
CUSTOM = "custom"

#: 自定义函数约定的参数个数
CUSTOM_ARITY = 5


@dataclass(frozen=True)
class SamplePoint:
    """一个采样点：逻辑坐标 (x, y_raw) 与缩放后的屏幕坐标 (screen_x, screen_y)。"""

    x: float
    y_raw: float
    screen_x: int
    screen_y: float


@dataclass(frozen=True)
class AutoScale:
    """自适应缩放结果。

    screen_y = y_raw * scale + offset
    """

    scale: float
    offset: float
    y_min: float
    y_max: float

    def to_screen(self, y_raw: float) -> float:
        return y_raw * self.scale + self.offset


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def sample_x(width: float, step: int = 2) -> List[float]:
    """生成从 0 到 width（含）的 x 采样序列。"""
    if step <= 0:
        raise ValueError("step 必须为正数")
    xs: List[float] = []
    x = 0.0
    while x <= width:
        xs.append(float(x))
        x += step
    # 保证右端点一定在序列里，避免函数在边缘处出现半截缺口
    if not xs or xs[-1] < width:
        xs.append(float(width))
    return xs


def evaluate(
    func_type: str,
    func: Callable[..., object] | None,
    x: float,
    width: float,
    center_y: float,
    amp: float,
    freq: float,
) -> float:
    """在逻辑坐标 x 处求值，统一返回实数。

    - 内置类型直接由本模块计算
    - custom 调用用户函数，出错时返回 center_y（由调用方决定如何处理异常）
    - 复数取实部，NaN/inf 回落 center_y
    """
    if width <= 0:
        raise ValueError("width 必须为正数")

    if func_type == SINE:
        return center_y + amp * math.sin(freq * math.pi * x / width * 2)
    if func_type == COSINE:
        return center_y + amp * math.cos(freq * math.pi * x / width * 2)
    if func_type == PARABOLA:
        normalized = (x - width / 2) / (width / 2)
        return center_y + amp * normalized ** 2
    if func_type == LINEAR:
        return center_y
    if func_type == CUSTOM:
        if func is None:
            raise ValueError("自定义函数未加载")
        result = func(x, width, center_y, amp, freq)
        return _sanitize(result, center_y)
    raise ValueError(f"未知函数类型: {func_type!r}")


def _sanitize(value: object, fallback: float) -> float:
    """把用户函数的返回值收敛成可绘制的实数。"""
    if isinstance(value, complex):
        value = value.real
    try:
        y = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    if math.isnan(y) or math.isinf(y):
        return fallback
    return y


def sample_curve(
    func_type: str,
    func: Callable[..., object] | None,
    width: float,
    height: float,
    amp: float,
    freq: float,
    step: int = 2,
) -> List[SamplePoint]:
    """整条曲线采样 + 自适应缩放，返回可用于 create_line 的点序列。"""
    if height <= 0:
        raise ValueError("height 必须为正数")

    center_y = height / 2
    xs = sample_x(width, step)
    ys: List[float] = []
    for x in xs:
        try:
            y = evaluate(func_type, func, x, width, center_y, amp, freq)
        except Exception:
            # 单点失败不应中断整条曲线
            y = center_y
        ys.append(y)

    scale = compute_auto_scale(ys, height)

    points: List[SamplePoint] = []
    for x, y in zip(xs, ys):
        points.append(
            SamplePoint(
                x=x,
                y_raw=y,
                screen_x=int(round(x)),
                screen_y=y * scale.scale + scale.offset,
            )
        )
    return points


def compute_auto_scale(y_values: Sequence[float], height: float, margin: float = 0.9) -> AutoScale:
    """根据 y 值范围计算只缩不放的缩放参数。

    margin: 曲线最多占画布高度的比例，留出边距避免贴边。
    """
    if not y_values:
        raise ValueError("y_values 不能为空")
    if height <= 0:
        raise ValueError("height 必须为正数")
    if not 0 < margin <= 1:
        raise ValueError("margin 必须在 (0, 1] 区间")

    y_min = min(y_values)
    y_max = max(y_values)
    y_range = y_max - y_min
    if y_range < 1e-9:
        y_min -= 0.5
        y_max += 0.5
        y_range = 1.0

    scale = (height * margin) / y_range
    if scale > 1.0:
        scale = 1.0  # 只缩小不放大，保持函数真实比例

    new_center = (y_min + y_max) / 2
    offset = height / 2 - new_center * scale

    return AutoScale(scale=scale, offset=offset, y_min=y_min, y_max=y_max)


def polyline_length(points: Iterable[SamplePoint]) -> float:
    """折线总长度，即"滑动值"。"""
    total = 0.0
    prev: SamplePoint | None = None
    for p in points:
        if prev is not None:
            total += math.hypot(p.screen_x - prev.screen_x, p.screen_y - prev.screen_y)
        prev = p
    return total


def segment_length(p1: SamplePoint, p2: SamplePoint) -> float:
    return math.hypot(p2.screen_x - p1.screen_x, p2.screen_y - p1.screen_y)


def clamp_y(screen_y: float, height: float, margin: int = 0) -> float:
    """把屏幕 y 限制在画布内，防止越界绘制。"""
    return _clamp(screen_y, margin, max(margin, height - margin))


def point_in_canvas(screen_y: float, height: float) -> bool:
    return 0 <= screen_y <= height
