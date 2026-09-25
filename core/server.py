"""内核 HTTP 服务 —— 计算内核唯一的对外接口。

所有计算请求都走这里，主 UI 与 edit 页面进程都只是客户端。
服务本身不持有 UI 引用，也不依赖 tkinter，所以：

- 主 UI 被 kill 后，本服务仍可继续跑（由 kernel_bridge 托管进程）
- edit 页面进程可以直接连本服务，不需要经过主 UI
- 多个页面同时在线时，用 session_id 区分各自状态

接口（全部 JSON，POST）
-----------------------
POST /api/ping                      心跳
POST /api/register                  页面上报自己的 session_id
POST /api/unregister                页面注销
POST /api/sessions                  列出所有在线页面
POST /api/band_fit                  分段包络估计（需 session_id）
POST /api/dev_series                离散宽度曲线数据
POST /api/band_series               上下界包络曲线数据
POST /api/poly_fit                  单变量多项式拟合（需 session_id）
POST /api/import_table              导入 Excel/CSV 并解析（需 session_id）
POST /api/save_points               保存页面上的点集（需 session_id）
POST /api/get_points                读取页面保存的点集
POST /api/render_band               渲染包络图 + 离散宽度图 PNG 并落盘
POST /api/report                    生成文本报告
POST /api/note                      页面回传一条消息

运行
----
    python -m core.server --port 8765
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict

# 保证本目录在 sys.path 最前（内核内部用扁平 import）
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import band_fit as bf                      # noqa: E402
import data_import as di                  # noqa: E402
import fitting as ft                      # noqa: E402
import session as ses                     # noqa: E402
from session import (SessionRegistry,     # noqa: E402
                     new_session_id,
                     page_report_path)

#: 内核服务默认端口。与 core.client / kernel_bridge 读同一环境变量，
#: 保证"服务起的端口"和"客户端连的端口"永远一致。
DEFAULT_PORT = int(os.environ.get("HERMES_KERNEL_PORT", "8765"))
DEFAULT_HOST = os.environ.get("HERMES_KERNEL_HOST", "127.0.0.1")

#: 各类产物目录。直接复用 session 模块的函数，避免两处规则漂移
#: （内核往里写、清理时往外删，必须是同一个目录）。
#: 按类型分：图表 -> pictures，文本 -> texts。
IMAGE_DIR = ses.image_dir()
REPORT_DIR = ses.report_dir()
OUTPUT_ROOT = ses.output_root()
_PRODUCT_DIRS = (IMAGE_DIR, REPORT_DIR)

_registry = SessionRegistry()
_lock = threading.Lock()


class KernelError(Exception):
    """内核业务错误，会以 {"ok": false, "error": ...} 返回。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


# ------------------------------------------------------------------
# 工具
# ------------------------------------------------------------------
def _ok(payload: Dict[str, Any]) -> Dict[str, Any]:
    out = {"ok": True}
    out.update(payload)
    return out


def _err(message: str, status: int = 400) -> Dict[str, Any]:
    return {"ok": False, "error": message, "status": status}


def _need_session(body: Dict[str, Any]):
    sid = body.get("session_id")
    if not sid:
        raise KernelError("缺少 session_id（页面的独立编号）")
    return _registry.get_or_create(sid)


def _as_points(raw: Any) -> list:
    """把前端传来的点集标准化为 [(x, y), ...]。"""
    if not isinstance(raw, list):
        raise KernelError("points 必须是数组")
    pts = []
    for i, item in enumerate(raw):
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            raise KernelError(f"第 {i + 1} 个点格式应为 [x, y]")
        try:
            pts.append((float(item[0]), float(item[1])))
        except (TypeError, ValueError):
            raise KernelError(f"第 {i + 1} 个点含非数值")
    return pts


def _ints(body: Dict[str, Any], key: str, default: int,
          lo: int, hi: int) -> int:
    """取整型参数。缺省用 default；越界**报错**而不是静默夹紧。

    静默夹紧会让用户以为设置生效了（例如填 degree=99 实际按 10 算），
    所以这里明确拒绝。
    """
    if key not in body:
        return default
    v = body[key]
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise KernelError(f"{key} 必须是整数")
    try:
        v = int(v)
    except (TypeError, ValueError):
        raise KernelError(f"{key} 必须是整数")
    if v < lo or v > hi:
        raise KernelError(f"{key} 必须在 {lo} ~ {hi} 之间，收到 {v}")
    return v


# ------------------------------------------------------------------
# 路由
# ------------------------------------------------------------------
def handle_ping(body):
    return _ok({
        "service": "hermes-kernel",
        "version": "1.0.0",
        "sessions": len(_registry.all()),
    })


def handle_register(body):
    """登记一个页面；重复登记返回既有状态（幂等）。

    支持顺带上报 PID：register 的 body 里带 pid 就能一次完成，
    省掉页面启动到 report_pid 之间的窗口期——那个窗口里内核
    既不知道它活着，也不知道它该被回收。
    """
    sid = body.get("session_id") or new_session_id()
    page = _registry.register(sid)
    page.touch()
    if body.get("source"):
        page.source = str(body["source"])
    pid = body.get("pid")
    if isinstance(pid, int) and pid > 0:
        page.pid = pid
    return _ok({"session_id": page.session_id, "page": page.to_dict()})


def handle_unregister(body):
    sid = body.get("session_id")
    if not sid:
        raise KernelError("缺少 session_id")
    removed = _registry.drop(sid)
    return _ok({"removed": removed})


def handle_sessions(body):
    return _ok({"sessions": _registry.summary()})


def handle_save_points(body):
    page = _need_session(body)
    with _lock:
        page.points = _as_points(body.get("points"))
        if body.get("source"):
            page.source = str(body["source"])
        page.touch()
    return _ok({"saved": len(page.points), "page": page.to_dict()})


def handle_get_points(body):
    page = _need_session(body)
    return _ok({"points": page.points, "page": page.to_dict()})


def handle_band_fit(body):
    page = _need_session(body)
    if "points" in body:
        page.points = _as_points(body["points"])
    if not page.points:
        raise KernelError("页面上没有点集，请先传入 points")
    n_seg = _ints(body, "n_segments", bf.DEFAULT_SEGMENTS, 2, 200)
    deg = _ints(body, "degree", bf.DEFAULT_DEGREE, 0, 20)

    try:
        r = bf.band_fit(page.points, n_segments=n_seg, degree=deg)
    except bf.BandFitError as e:
        raise KernelError(str(e))
    except Exception as e:
        raise KernelError(f"内核异常: {type(e).__name__}: {e}", 500)

    with _lock:
        page.last_result = _pack_band_result(r)
        page.touch()
    result = dict(page.last_result)
    result["algorithm"] = "classic"
    return _ok({"result": result})


def handle_band_fit_improved(body):
    page = _need_session(body)
    if "points" in body:
        page.points = _as_points(body["points"])
    if not page.points:
        raise KernelError("页面上没有点集，请先传入 points")
    n_seg = _ints(body, "n_segments", bf.DEFAULT_SEGMENTS, 2, 200)
    deg = _ints(body, "degree", bf.DEFAULT_DEGREE, 0, 20)

    try:
        r = bf.band_fit_improved(page.points, n_segments=n_seg, degree=deg)
    except bf.BandFitError as e:
        raise KernelError(str(e))
    except Exception as e:
        raise KernelError(f"内核异常: {type(e).__name__}: {e}", 500)

    with _lock:
        page.last_result = _pack_band_result(r)
        page.touch()
    result = dict(page.last_result)
    result["algorithm"] = "improved"
    return _ok({"result": result})


def _pack_band_result(r: bf.BandFitResult) -> dict:
    return {
        "expression": r.expression,
        "coefficients": list(r.coefficients),
        "upper": {
            "expression": r.upper.expression,
            "coefficients": list(r.upper.coefficients),
        },
        "lower": {
            "expression": r.lower.expression,
            "coefficients": list(r.lower.coefficients),
        },
        "offset": r.offset,
        "r2": r.r2,
        "rmse": r.rmse,
        "mean_abs_dev": r.mean_abs_dev,
        "n_points": r.n_points,
        "n_segments": r.n_segments,
        "degree_upper": r.degree_upper,
        "degree_lower": r.degree_lower,
        "y_min": r.y_min,
        "y_max": r.y_max,
        "scatter": {
            "d_values": list(r.scatter.d_values),
            "spreads": list(r.scatter.spreads),
            "mean_d": r.scatter.mean_d,
            "min_d": r.scatter.min_d,
            "max_d": r.scatter.max_d,
            "head_mean": r.scatter.head_mean,
            "tail_mean": r.scatter.tail_mean,
            "tail_ratio": r.scatter.tail_ratio,
            "trend": r.scatter.trend,
            "swing": r.scatter.swing,
            "widest_segment": r.scatter.widest_segment,
            "tightest_segment": r.scatter.tightest_segment,
            "description": r.scatter.description,
        },
        "segments": [
            {
                "index": s.index, "x_lo": s.x_lo, "x_hi": s.x_hi,
                "center": s.center, "mean_y": s.mean_y,
                "n_upper": s.n_upper, "n_lower": s.n_lower,
                "d": s.d_mean_points, "spread": s.spread,
            }
            for s in r.segments
        ],
        "caution": r.caution,
    }


def handle_poly_fit(body):
    page = _need_session(body)
    if "points" in body:
        page.points = _as_points(body["points"])
    if len(page.points) < 2:
        raise KernelError("至少需要 2 个点")
    deg = _ints(body, "degree", bf.DEFAULT_DEGREE, 0, 20)
    try:
        r = ft.fit_polynomial(page.points, deg)
    except ft.FitError as e:
        raise KernelError(str(e))
    with _lock:
        page.touch()
    return _ok({"result": {
        "expression": r.expression,
        "coefficients": list(r.coefficients),
        "degree": r.degree,
        "r2": r.r2,
        "rmse": r.rmse,
    }})


def handle_import_table(body):
    page = _need_session(body)
    path = body.get("path")
    if not path:
        raise KernelError("缺少 path（文件绝对路径）")
    p = Path(str(path))
    if not p.exists():
        raise KernelError(f"文件不存在: {p}")
    try:
        data = di.import_file(p)
    except di.DataImportError as e:
        raise KernelError(str(e))
    except Exception as e:
        raise KernelError(f"导入失败: {type(e).__name__}: {e}", 500)

    with _lock:
        page.variables = list(data.variables)
        page.target = data.target
        page.rows = list(data.rows)
        # data.rows 的元素是 ((x1, x2, ...), y)：
        # 前半是自变元元组，后半是目标值。取首元组塞入 points。
        pts = []
        for vars_, y in data.rows:
            if vars_:
                pts.append((float(vars_[0]), float(y)))
        page.points = pts
        page.source = p.name
        page.touch()
    return _ok({
        "n_rows": data.n_rows,
        "n_vars": data.n_vars,
        "variables": list(data.variables),
        "target": data.target,
        "describe": data.describe(),
    })


def handle_dev_series(body):
    """返回离散宽度曲线；参数取页面保存的点与最近一次拟合结果。"""
    page = _need_session(body)
    if "points" in body:
        page.points = _as_points(body["points"])
        need_fit = True
    else:
        need_fit = not page.last_result
    if not page.points:
        raise KernelError("页面上没有点集")

    n_seg = _ints(body, "n_segments", bf.DEFAULT_SEGMENTS, 2, 200)
    deg = _ints(body, "degree", bf.DEFAULT_DEGREE, 0, 20)
    step = _ints(body, "step", 5, 1, 1000)

    try:
        if need_fit or not page.last_result:
            r = bf.band_fit(page.points, n_segments=n_seg, degree=deg)
        else:
            # 用已保存结果重建，避免重复计算
            r = _rebuild_band_result(page.last_result)
    except bf.BandFitError as e:
        raise KernelError(str(e))

    x_lo = min(p[0] for p in page.points)
    x_hi = max(p[0] for p in page.points)
    xs, dy = bf.dev_series(r, x_lo, x_hi, step)
    return _ok({
        "xs": xs, "d": dy,
        "x_from": x_lo, "x_to": x_hi,
    })


def handle_band_series(body):
    page = _need_session(body)
    if "points" in body:
        page.points = _as_points(body["points"])
    if not page.points:
        raise KernelError("页面上没有点集")

    n_seg = _ints(body, "n_segments", bf.DEFAULT_SEGMENTS, 2, 200)
    deg = _ints(body, "degree", bf.DEFAULT_DEGREE, 0, 20)
    step = _ints(body, "step", 5, 1, 1000)

    try:
        if not page.last_result:
            r = bf.band_fit(page.points, n_segments=n_seg, degree=deg)
        else:
            r = _rebuild_band_result(page.last_result)
    except bf.BandFitError as e:
        raise KernelError(str(e))

    x_lo = min(p[0] for p in page.points)
    x_hi = max(p[0] for p in page.points)
    xs, up, lo = bf.band_series(r, x_lo, x_hi, step)
    return _ok({
        "xs": xs, "upper": up, "lower": lo,
        "x_from": x_lo, "x_to": x_hi,
    })


def _rebuild_band_result(saved: Dict[str, Any]) -> bf.BandFitResult:
    """从保存的字典还原 BandFitResult，供曲线类接口复用。

    只需要 upper/lower 的系数和离散序列；其余字段用占位值填充，
    因为曲线绘制只用得到两条拟合线的表达式。
    """
    class _FakeFit:
        def __init__(self, expr, coefs):
            self.expression = expr
            self.coefficients = coefs

        def predict(self, x):
            acc = 0.0
            for c in reversed(self.coefficients):
                acc = acc * float(x) + c
            return acc

    class _FakeScatter:
        def __init__(self, d_values):
            self.d_values = tuple(d_values)

    r = bf.BandFitResult(
        upper=_FakeFit(saved["upper"]["expression"],
                       saved["upper"]["coefficients"]),
        lower=_FakeFit(saved["lower"]["expression"],
                       saved["lower"]["coefficients"]),
        reconciled=tuple(saved["coefficients"]),
        offset=saved.get("offset", 0.0),
        coefficients=tuple(saved["coefficients"]),
        expression=saved["expression"],
        segments=(),
        scatter=_FakeScatter(saved["scatter"]["d_values"]),
        degree_upper=saved.get("degree_upper", 1),
        degree_lower=saved.get("degree_lower", 1),
        n_points=saved.get("n_points", 0),
        n_segments=saved.get("n_segments", 0),
        y_min=saved.get("y_min", 0.0),
        y_max=saved.get("y_max", 0.0),
        r2=saved.get("r2", 0.0),
        rmse=saved.get("rmse", 0.0),
        mean_abs_dev=saved.get("mean_abs_dev", 0.0),
        caution=saved.get("caution", ""),
    )
    return r


def handle_render_band(body):
    """渲染走势图 + 离散宽度图，落盘并返回路径（供 UI 打开）。"""
    page = _need_session(body)
    if "points" in body:
        page.points = _as_points(body["points"])
    if not page.points:
        raise KernelError("页面上没有点集")

    n_seg = _ints(body, "n_segments", bf.DEFAULT_SEGMENTS, 2, 200)
    deg = _ints(body, "degree", bf.DEFAULT_DEGREE, 0, 20)
    step = _ints(body, "step", 5, 1, 1000)

    try:
        r = bf.band_fit(page.points, n_segments=n_seg, degree=deg)
    except bf.BandFitError as e:
        raise KernelError(str(e))

    safe_sid = page.session_id
    out = ses.dir_for(f"band_{safe_sid}.png") / f"band_{safe_sid}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        _render_band_png(page.points, r, out, step)
    except Exception as e:
        raise KernelError(f"渲染失败: {type(e).__name__}: {e}", 500)
    with _lock:
        page.images["band"] = str(out)
        page.touch()
    return _ok({"path": str(out), "session_id": page.session_id})


def _render_band_png(points, r, out_path: Path, step: int) -> None:
    """用 matplotlib 画：左=走势+包络+散点，右=离散宽度曲线。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    # 中文字体：找不到时退化到默认（避免 matplotlib 找不到字体崩掉）
    for cand in (r"C:\Windows\Fonts\msyh.ttc",
                 r"C:\Windows\Fonts\simhei.ttf"):
        if os.path.exists(cand):
            try:
                font_manager.fontManager.addfont(cand)
                plt.rcParams["font.family"] = font_manager.FontProperties(
                    fname=cand).get_name()
                break
            except Exception:
                pass
    plt.rcParams["axes.unicode_minus"] = False

    xs_all = [p[0] for p in points]
    ys_all = [p[1] for p in points]
    x_lo, x_hi = min(xs_all), max(xs_all)

    xs, up, lo = bf.band_series(r, x_lo, x_hi, step)
    dx, dy = bf.dev_series(r, x_lo, x_hi, step)

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 5))
    fig.suptitle(
        f"分段包络估计（页面 {r.n_points} 点，{r.n_segments} 段，"
        f"上/下界各 {r.degree_upper} 阶）",
        fontsize=12,
    )

    # 左：走势 + 包络 + 散点
    ax1.scatter(xs_all, ys_all, s=18, c="#4a9eff", alpha=0.75,
                label="离散点", zorder=2)
    ax1.plot(xs, up, "-", color="#e74c3c", lw=1.6, label="上界 f_up", zorder=3)
    ax1.plot(xs, lo, "-", color="#27ae60", lw=1.6, label="下界 f_lo", zorder=3)
    ax1.fill_between(xs, lo, up, color="#f1c40f", alpha=0.18, zorder=1,
                     label="离散区间")
    trend_x = xs
    trend_y = [r.predict(x) for x in xs]
    ax1.plot(trend_x, trend_y, "-", color="#2c3e50", lw=2.4,
             label=f"整体走势 {r.expression}", zorder=4)
    ax1.set_xlabel("x"); ax1.set_ylabel("y")
    ax1.set_title("整体走势与离散区间")
    ax1.legend(loc="best", fontsize=8)
    ax1.grid(alpha=0.25)

    # 右：离散宽度
    ax2.plot(dx, dy, "-", color="#8e44ad", lw=2.0, label="离散宽度 d(x)")
    ax2.fill_between(dx, 0, dy, color="#8e44ad", alpha=0.15)
    ax2.axhline(r.scatter.mean_d, ls="--", color="#7f8c8d", lw=1.2,
                label=f"平均宽度 {r.scatter.mean_d:.4g}")
    ax2.set_xlabel("x"); ax2.set_ylabel("d(x) = |f_up - f_lo|")
    ax2.set_title(f"离散程度（{r.scatter.trend}）")
    ax2.legend(loc="best", fontsize=8)
    ax2.grid(alpha=0.25)

    fig.tight_layout()
    fig.savefig(out_path, dpi=110, bbox_inches="tight")
    plt.close(fig)


def handle_report(body):
    page = _need_session(body)
    if not page.last_result:
        raise KernelError("还没有计算过，请先调用 band_fit")
    out = page_report_path(page.session_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    s = page.last_result
    lines = [
        "分段包络估计报告",
        "=" * 56,
        f"页面编号:   {page.session_id}",
        f"数据来源:   {page.source or '页面点集'}",
        f"点数:       {len(page.points)}",
        f"分段数:     {s['n_segments']}",
        f"上界阶数:   {s['degree_upper']}",
        f"下界阶数:   {s['degree_lower']}",
        "",
        f"整体走势:   {s['expression']}",
        f"上界:       {s['upper']['expression']}",
        f"下界:       {s['lower']['expression']}",
        f"偏移常数 C = {s['offset']:.6g}",
        f"R² = {s['r2']:.6f}   RMSE = {s['rmse']:.6g}",
        f"平均绝对偏差 = {s['mean_abs_dev']:.6g}",
        "",
        "系数推导：",
        f"  上界系数 {tuple(s['upper']['coefficients'])}",
        f"  下界系数 {tuple(s['lower']['coefficients'])}",
        f"  平均后   {tuple(s['coefficients'])}",
        f"  加 C 后  {tuple(s['coefficients'])}",
        "",
        "离散程度:",
        f"  {s['scatter']['description']}",
        f"  平均宽度 {s['scatter']['mean_d']:.6g}",
        f"  范围 [{s['scatter']['min_d']:.6g}, {s['scatter']['max_d']:.6g}]",
        f"  尾部/首部 = {s['scatter']['tail_ratio']:.3f}",
        "",
        "分段统计:",
        f"  {'段':<4}{'x区间':<22}{'均值m':>12}"
        f"{'离散宽d':>12}{'极差':>12}{'上组':>6}{'下组':>6}",
    ]
    for seg in s["segments"]:
        iv = f"[{seg['x_lo']:.4g}, {seg['x_hi']:.4g}]"
        lines.append(
            f"  {seg['index']:<4}{iv:<22}{seg['mean_y']:>12.4g}"
            f"{seg['d']:>12.4g}{seg['spread']:>12.4g}"
            f"{seg['n_upper']:>6}{seg['n_lower']:>6}"
        )
    if s.get("caution"):
        lines += ["", f"注意: {s['caution']}"]

    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with _lock:
        page.touch()
    return _ok({"path": str(out)})


def handle_note(body):
    page = _need_session(body)
    text = str(body.get("text", "")).strip()
    if not text:
        raise KernelError("缺少 text")
    with _lock:
        page.notes.append(text)
        page.touch()
    return _ok({"notes": list(page.notes)})


def handle_report_pid(body):
    """页面上报自己的 PID，便于内核在页面消失后回收其状态。"""
    page = _need_session(body)
    pid = body.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        raise KernelError("pid 必须是正整数")
    with _lock:
        page.pid = pid
        page.touch()
    return _ok({"session_id": page.session_id, "pid": pid})


def handle_mark_closing(body):
    """页面声明"我要关了"。

    只打标记 + 给宽限期，不立即删状态：页面可能还要写完最后的
    图片/报告。真正回收由后续 reap 完成。
    """
    page = _need_session(body)
    with _lock:
        page.closing = True
        page.touch()
    return _ok({"session_id": page.session_id, "closing": True})


def handle_cleanup(body):
    """页面关闭前的最后一次清理请求。

    与 /api/unregister 的区别：这个接口**同步删掉该页面的图片与报告文件**，
    而不是只清内存。页面在 on_close 里先调它，再退出进程。
    只影响调用方自己的 session_id，其他页面不受牵连。
    """
    sid = body.get("session_id")
    if not sid:
        raise KernelError("缺少 session_id")
    with _lock:
        page = _registry.get(sid)
        removed = []
        if page is not None:
            _registry.mark_closing(sid)
            removed = ses._cleanup_page_files(page)
        dropped = _registry.drop(sid)
    return _ok({
        "dropped": dropped,
        "removed_files": removed,
        "n_removed": len(removed),
    })


def _pids_alive(pids: set) -> set:
    """返回 pids 中仍然存活的子集。只用标准库，不引 psutil。

    优先用 os.kill(pid, 0)：发空信号探测进程是否存在，
    权限不足时说明进程存在但不属于我们，也算活着。
    """
    alive = set()
    for pid in pids:
        if pid <= 0:
            continue
        if os.name == "nt":
            # Windows：用 tasklist 逐个查太慢，改用 OpenProcess 语义的
            # 简化判据——ctypes 不可用时保守认为活着（宁可晚回收，不可错杀）
            if _win_pid_alive(pid):
                alive.add(pid)
        else:
            try:
                os.kill(pid, 0)
                alive.add(pid)
            except ProcessLookupError:
                pass
            except PermissionError:
                alive.add(pid)
            except OSError:
                pass
    return alive


def _win_pid_alive(pid: int) -> bool:
    """Windows 下探测 PID 是否存活。"""
    try:
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        SYNCHRONIZE = 0x00100000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        err = kernel32.GetLastError()
        # ERROR_INVALID_PARAMETER(87) 表示 PID 不存在
        return err != 87
    except Exception:
        return True   # 探测失败时保守认为活着


def _sweep_orphan_files() -> list:
    """扫掉注册表里已不存在、但磁盘上还留着的产物文件。

    为什么需要这个：reap_dead 只能回收"注册表里已知的页面"。
    但内核重启后注册表是空的，旧会话的文件就成了谁都不认识的真孤儿，
    reap 永远碰不到它们。启动时扫一次，把这些清掉。

    安全性：只删文件名里带 'p_' 前缀 session_id 模式的文件
    （band_<sid>.png / report_<sid>.txt）。不认识的一律不动。
    另外绝不删掉当前注册表里活着的页面的文件。
    """
    removed = []
    live_sids = {p.session_id for p in _registry.all()}

    def _looks_like_page_artifact(name: str) -> bool:
        """形如 band_p_xxxxxxxx.png / report_p_xxxxxxxx.txt。

        只认这个模式：既保证是内核产物（前缀 + _p_ 分隔），
        又保证能反推出归属的 session_id。
        """
        root, dot, ext = name.rpartition(".")
        if not dot or ext.lower() not in ("png", "txt"):
            return False
        return root.startswith(("band_", "report_")) and "_p_" in root

    for base in _PRODUCT_DIRS:
        try:
            if not base.is_dir():
                continue
            for f in base.iterdir():
                if not f.is_file() or not _looks_like_page_artifact(f.name):
                    continue
                # 只要文件名里含任一活着的 sid 就跳过，绝不误删
                if any(s in f.name for s in live_sids):
                    continue
                try:
                    f.unlink()
                    removed.append(str(f))
                except OSError:
                    pass
        except OSError:
            pass
    return removed


def handle_reap(body):
    """立即巡检一次，回收已死页面的状态与文件。

    这个接口存在的意义：页面可能是被任务管理器杀掉的，
    没机会调 cleanup，内核就靠这个把它的残留收掉。
    只回收"已死"的页面，活着的页面一律不动。
    """
    with _lock:
        pids = {p.pid for p in _registry.all() if p.pid is not None}
        alive = _pids_alive(pids) if pids else set()
        reaped = _registry.reap_dead(alive_pids=alive)
    return _ok({"reaped": reaped, "n_alive_pages": len(_registry.all())})


def _reaper_loop(interval: float = 30.0) -> None:
    """后台巡检线程：定期回收孤儿页面，防止内存与磁盘无限增长。"""
    import time as _t
    while True:
        _t.sleep(interval)
        try:
            with _lock:
                pids = {p.pid for p in _registry.all() if p.pid is not None}
                alive = _pids_alive(pids) if pids else set()
                reaped = _registry.reap_dead(alive_pids=alive)
            if reaped:
                print(f"[kernel] reaped dead pages: {reaped}", flush=True)
        except Exception as e:
            print(f"[kernel] reaper error: {e}", flush=True)


ROUTES = {
    "/api/ping": handle_ping,
    "/api/register": handle_register,
    "/api/unregister": handle_unregister,
    "/api/sessions": handle_sessions,
    "/api/save_points": handle_save_points,
    "/api/get_points": handle_get_points,
    "/api/band_fit": handle_band_fit,
    "/api/band_fit_improved": handle_band_fit_improved,
    "/api/poly_fit": handle_poly_fit,
    "/api/import_table": handle_import_table,
    "/api/dev_series": handle_dev_series,
    "/api/band_series": handle_band_series,
    "/api/render_band": handle_render_band,
    "/api/report": handle_report,
    "/api/note": handle_note,
    "/api/report_pid": handle_report_pid,
    "/api/mark_closing": handle_mark_closing,
    "/api/cleanup": handle_cleanup,
    "/api/reap": handle_reap,
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "HermesKernel/1.0"

    def log_message(self, fmt, *args):  # 静默默认访问日志
        pass

    def _send_json(self, obj: Dict[str, Any], status: int = 200) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods",
                         "POST, OPTIONS, GET")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self._send_json({"ok": True})

    def do_GET(self):
        if self.path in ("/", "/health", "/api/ping"):
            self._send_json(_ok({"service": "hermes-kernel",
                                 "routes": sorted(ROUTES)}))
            return
        self._send_json(_err(f"未知路径: {self.path}", 404), 404)

    def do_POST(self):
        path = self.path.split("?")[0]
        handler = ROUTES.get(path)
        if handler is None:
            self._send_json(_err(f"未知接口: {path}", 404), 404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError) as e:
                raise KernelError(f"请求体不是合法 JSON: {e}")
            if not isinstance(body, dict):
                raise KernelError("请求体必须是 JSON 对象")
            result = handler(body)
            self._send_json(result)
        except KernelError as e:
            self._send_json(_err(e.message, e.status), e.status)
        except BrokenPipeError:
            pass
        except Exception as e:
            traceback.print_exc()
            self._send_json(
                _err(f"服务内部错误: {type(e).__name__}: {e}", 500), 500)


def _install_death_logger(port: int) -> None:
    """装上"为什么会死"的取证钩子。

    内核是 detached 进程，死了之后没人知道原因（被杀？异常？oom？）。
    这里记三类证据到 .kernel_death.log：

    1. 启动时记一次基线（时间、PID、ppid、命令行、Job 归属）
    2. excepthook：未捕获异常 —— 记 traceback
    3. atexit + SIGTERM/SIGBREAK 处理器：正常退出与被信号杀都记

    Windows 上 DETACHED_PROCESS 并不等于脱离 Job：如果父进程在某个
    Job Object 里且带了 JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE，父进程一死
    Job 关闭，整个 Job 的进程会被连带杀掉。这是 detached 进程仍然
    "跟着 session 消失"的最常见原因。所以启动时就查明 Job 归属，
    事后一眼能判断是不是这个原因。
    """
    import atexit
    import signal
    import traceback

    root = Path(os.environ.get(
        "HERMES_KERNEL_OUTPUT_ROOT", r"D:\.hermes_kernel"))
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        root = Path(__file__).resolve().parent
    log_path = root / ".kernel_death.log"
    pid = os.getpid()

    def _write(kind: str, detail: str = "") -> None:
        try:
            with open(log_path, "a", encoding="utf-8") as fh:
                now = time.strftime("%F %T")
                fh.write(f"[{now}] {kind} pid={pid} {detail}\n")
                fh.flush()
        except OSError:
            pass

    def _startup() -> None:
        ppid = os.getpid() and _parent_pid() or "?"
        job = _job_info()
        argv = " ".join(sys.argv)
        _write("START", f"ppid={ppid} job={job} argv={argv}")

    _startup()

    # 未捕获异常：记 traceback 再交给默认行为
    _prev_hook = sys.excepthook

    def _hook(exc_type, exc, tb):
        _write("CRASH", "".join(
            traceback.format_exception(exc_type, exc, tb))[-2000:])
        return _prev_hook(exc_type, exc, tb)

    sys.excepthook = _hook

    # 正常退出（含未捕获异常的默认收尾）
    atexit.register(lambda: _write("EXIT", "normal atexit"))

    # 信号：被杀时留证据。Windows 只有这几个可用。
    if os.name == "nt":
        for signame, sig in (("SIGTERM", getattr(signal, "SIGTERM", None)),
                             ("SIGBREAK", getattr(signal, "SIGBREAK", None)),
                             ("SIGINT", getattr(signal, "SIGINT", None))):
            if sig is None:
                continue

            def _make(name):
                def _handler(signum, frame):
                    _write("SIGNAL", f"received {name} ({signum})")
                    raise SystemExit(0)
                return _handler

            try:
                signal.signal(sig, _make(signame))
            except (ValueError, OSError):
                pass      # 不在主线程等情况，忽略


def _parent_pid():
    """父进程 PID。取不到返回 None。"""
    try:
        import ctypes
        # 用 CreateToolhelp32Snapshot 太重；直接读环境变量不靠谱，
        # 这里用 os.getppid()，POSIX 可用；Windows 下 Python 3.11 也提供。
        if hasattr(os, "getppid"):
            return os.getppid()
    except Exception:
        pass
    return None


def _job_info() -> str:
    """是否归属 Job Object，以及该 Job 的致命限制。

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE=0x00002000 正是
    "父进程死去就连带杀死整个 Job" 的那个开关。
    """
    if os.name != "nt":
        return "n/a"
    try:
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
        # JobObjectExtendedLimitInformation = 2（比 Basic 多内存信息，
        # 但 BasicLimitInformation 是它的前缀，能正确取到 LimitFlags）
        JobObjectExtendedLimitInformation = 2

        kernel32 = ctypes.windll.kernel32

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation",
                 JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", ctypes.c_uint64 * 6),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        # 先问"在不在 Job 里"：调用简单、结果可靠。
        # 返回 FALSE 且 last error 为 0 = 不在任何 Job 里。
        # 注意：句柄参数必须显式声明成 HANDLE，否则 ctypes 默认按
        # c_int 传，64 位下句柄被截断 -> ERROR_INVALID_HANDLE(6)。
        kernel32.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE,
                                            ctypes.POINTER(wintypes.BOOL)]
        kernel32.IsProcessInJob.restype = wintypes.BOOL
        kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE, wintypes.INT, ctypes.c_void_p,
            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        kernel32.QueryInformationJobObject.restype = wintypes.BOOL

        in_job = wintypes.BOOL(0)
        ok = kernel32.IsProcessInJob(
            kernel32.GetCurrentProcess(), None, ctypes.byref(in_job))
        if not ok:
            return f"query-failed(err={kernel32.GetLastError()})"
        if not in_job.value:
            return "no-job"

        # 在 Job 里已是关键信息；再尝试取 LimitFlags 确认
        # 是否带 KILL_ON_JOB_CLOSE，取不到就标注 limits=unknown
        # （ctypes 结构体布局不匹配时也不要紧，成员身份已经确认）
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        written = wintypes.DWORD(0)
        ok = kernel32.QueryInformationJobObject(
            None,
            JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
            ctypes.byref(written),
        )
        if not ok:
            return "job(member=1, limits=unknown)"
        flags = info.BasicLimitInformation.LimitFlags
        kill_on_close = bool(flags & JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
        return f"job(member=1 kill_on_close={int(kill_on_close)} flags={flags:#x})"
    except Exception as e:
        return f"job-unknown({type(e).__name__})"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Hermes 计算内核 HTTP 服务")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", default=DEFAULT_HOST)
    args = ap.parse_args(argv)

    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)

    # 死亡取证：先装钩子，后面的崩溃/被杀才有证据
    _install_death_logger(args.port)

    for d in _PRODUCT_DIRS:
        d.mkdir(parents=True, exist_ok=True)

    # 扫掉上一轮遗留的孤儿产物文件（重启后注册表里已不认识它们）
    orphans = _sweep_orphan_files()
    if orphans:
        print(f"[kernel] swept {len(orphans)} orphan files", flush=True)

    # 后台巡检：回收被强杀、没机会清理的孤儿页面
    t = threading.Thread(target=_reaper_loop, daemon=True)
    t.daemon = True
    t.start()

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    print(f"[kernel] listening on http://{args.host}:{args.port}")
    print(f"[kernel] output root -> {OUTPUT_ROOT}")
    for name, d in (("pictures", IMAGE_DIR), ("texts", REPORT_DIR)):
        print(f"[kernel]   {name} -> {d}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[kernel] stopped")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
