"""会话编号与注册表 —— 内核侧。

每个 edit 页面进程启动时会拿到一个 session_id（uuid4 短前缀）。
内核用这个编号把请求路由到对应的页面状态，包括：

- 页面声明的数据集（页面上导入的 Excel / 绘制的点）
- 页面最近一次的计算结果（走势线、上下界、离散画像）
- 页面输出的图片（走势图、离散宽度图）

设计要点
--------
- session_id 由**页面进程**生成并主动上报，内核不预设；
  这样页面可以离线启动、稍后再连内核，不受主 UI 生命周期影响。
- 注册表纯内存，进程退出即失效；不落盘，避免遗留脏状态。
- 所有状态带 updated_at，便于 UI 显示"该页面是否还活着"。
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set


def new_session_id() -> str:
    """生成一个页面进程的独立编号，如 'p_3f9a1c2b'。"""
    return "p_" + uuid.uuid4().hex[:8]


#: 无 PID 上报的页面，空闲多久视为死亡（秒）
DEFAULT_MAX_IDLE_SECONDS = 6 * 3600
#: 标记 closing 后，宽限多久再真正回收（秒）
#: 给页面留时间把最后的图片/报告写完，避免删掉正在用的文件
CLOSING_GRACE_SECONDS = 5.0


def _cleanup_page_files(page: "PageState") -> list:
    """删除该页面在磁盘上的产物（图片、报告）。

    两个来源都清，不依赖调用方"记得登记"：
    1. page.images 里登记过的路径
    2. 按 session_id 直接扫 images/ 与 reports/ 目录

    第 2 条是关键：登记是脆弱的，任何新接口忘了登记就会永久残留文件，
    而按 sid 扫描只按文件名判断归属，天然只删自己的、不碰别人的。
    （文件名形如 band_<sid>.png / report_<sid>.txt）
    """
    removed = []
    seen = set()

    def _try(path):
        if not path:
            return
        path = str(path)
        if path in seen:
            return
        seen.add(path)
        try:
            p = Path(path)
            if p.is_file():
                p.unlink()
                removed.append(path)
        except OSError:
            pass

    # 1) 登记过的路径
    for path in list(page.images.values()):
        _try(path)
    _try(str(page_report_path(page.session_id)))

    # 2) 按 session_id 兜底扫描（不依赖登记）
    #    扫全部产物目录，不依赖调用方记得把文件放进 images/reports。
    sid = page.session_id
    for base in (image_dir(), report_dir()):
        try:
            if not base.is_dir():
                continue
            for f in base.iterdir():
                if sid in f.name:
                    _try(f)
        except OSError:
            pass

    return removed


def page_report_path(session_id: str) -> Path:
    """该页面的报告落盘路径，与 server.py 的规则保持一致。"""
    return report_dir() / f"report_{session_id}.txt"


def output_root() -> Path:
    """所有产物的根目录。

    默认放 D 盘：这些是用户主动生成的图表和报告，体积可能不小，
    而且过期后会清理，不该占系统盘。
    需要迁移时设一个环境变量 HERMES_KERNEL_OUTPUT_ROOT 即可，
    下面的 pictures / texts 子目录会自动派生。
    """
    return Path(os.environ.get(
        "HERMES_KERNEL_OUTPUT_ROOT",
        r"D:\.hermes_kernel"))


def image_dir() -> Path:
    """图片类产物目录（png/jpg/svg 等）。"""
    return output_root() / "pictures"


def report_dir() -> Path:
    """文本类产物目录（txt/report/csv/log 等）。"""
    return output_root() / "texts"


def dir_for(path: str) -> Path:
    """按扩展名决定一个产物该放哪个分类目录。

    单个入口决定归属，避免各处各写一套归类规则而漂移。
    图片类 -> pictures，其余一律 -> texts（报告、导出数据、日志）。
    """
    ext = Path(str(path)).suffix.lower()
    if ext in (".png", ".jpg", ".jpeg", ".gif", ".bmp",
               ".svg", ".webp", ".tif", ".tiff"):
        return image_dir()
    return report_dir()


@dataclass
class PageState:
    """一个 edit 页面的完整状态。"""

    session_id: str
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    # 页面声明的数据集
    points: List[tuple] = field(default_factory=list)   # [(x, y), ...]
    variables: List[str] = field(default_factory=list)  # Excel 表头
    target: str = "y"
    rows: List[tuple] = field(default_factory=list)     # Excel 多变量行
    source: str = ""                                    # 数据来源描述
    # 最近一次计算结果
    last_result: Dict[str, Any] = field(default_factory=dict)
    # 页面生成的图片 {name: png_path}
    images: Dict[str, str] = field(default_factory=dict)
    # 页面想回传给 UI 的消息
    notes: List[str] = field(default_factory=list)
    # 页面进程自己的 PID，由页面上报。关窗口时用来精确终止本进程，
    # 不影响其他页面进程。
    pid: Optional[int] = None
    # 页面是否已请求关闭。用于内核区分"空闲页面"与"已关闭的孤儿"。
    closing: bool = False

    def touch(self) -> None:
        self.updated_at = time.time()

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "age_seconds": round(time.time() - self.created_at, 1),
            "idle_seconds": round(time.time() - self.updated_at, 1),
            "n_points": len(self.points),
            "n_rows": len(self.rows),
            "variables": list(self.variables),
            "target": self.target,
            "source": self.source,
            "has_result": bool(self.last_result),
            "images": dict(self.images),
            "notes": list(self.notes),
            "pid": self.pid,
            "closing": self.closing,
        }


class SessionRegistry:
    """内核内存中的页面注册表。线程安全由调用方（HTTP 单线程）保证。"""

    def __init__(self) -> None:
        self._pages: Dict[str, PageState] = {}

    def register(self, session_id: str) -> PageState:
        """登记一个页面；重复登记返回既有状态（幂等）。"""
        sid = _normalize_session_id(session_id)
        page = self._pages.get(sid)
        if page is None:
            page = PageState(session_id=sid)
            self._pages[sid] = page
        else:
            page.touch()
        return page

    def get(self, session_id: str) -> Optional[PageState]:
        return self._pages.get(_normalize_session_id(session_id))

    def get_or_create(self, session_id: str) -> PageState:
        page = self._pages.get(_normalize_session_id(session_id))
        return page if page is not None else self.register(session_id)

    def drop(self, session_id: str) -> bool:
        sid = _normalize_session_id(session_id)
        page = self._pages.pop(sid, None)
        if page is None:
            return False
        _cleanup_page_files(page)
        return True

    def mark_closing(self, session_id: str) -> Optional[PageState]:
        """标记页面正在关闭（尚未真正注销）。"""
        page = self.get(session_id)
        if page is not None:
            page.closing = True
            page.touch()
        return page

    def reap_dead(self, alive_pids: Optional[set] = None) -> List[str]:
        """回收"已死"页面的状态与文件。

        判定死亡的条件（满足其一）：
        - closing=True 且已过宽限期
        - 上报了 PID，而该 PID 已不在 alive_pids 里
        - 没有 PID 且空闲超过 max_idle_seconds

        返回被回收的 session_id 列表。
        不会碰 alive_pids 之外的任何东西——其他页面完全不受影响。
        """
        import time as _t
        now = _t.time()
        dead: List[str] = []
        for sid, page in list(self._pages.items()):
            pid_dead = (
                alive_pids is not None
                and page.pid is not None
                and page.pid not in alive_pids
            )
            idle_dead = (
                page.pid is None
                and (now - page.updated_at) > DEFAULT_MAX_IDLE_SECONDS
            )
            closing_dead = (
                page.closing
                and (now - page.updated_at) > CLOSING_GRACE_SECONDS
            )
            if pid_dead or idle_dead or closing_dead:
                dead.append(sid)

        for sid in dead:
            page = self._pages.pop(sid, None)
            if page is not None:
                _cleanup_page_files(page)
        return dead

    def all(self) -> List[PageState]:
        """按最近活跃排序。"""
        return sorted(self._pages.values(),
                       key=lambda p: p.updated_at, reverse=True)

    def summary(self) -> List[dict]:
        return [p.to_dict() for p in self.all()]

    def prune(self, max_idle_seconds: float = 6 * 3600) -> List[str]:
        """清理长时间无活动的页面，返回被移除的编号列表。

        内部走 reap_dead，保证"清内存"和"删文件"行为一致。
        """
        dead = self.reap_dead()
        if not dead and max_idle_seconds != DEFAULT_MAX_IDLE_SECONDS:
            # 调用方指定了非默认阈值时才做二次扫描
            now = time.time()
            for sid, p in list(self._pages.items()):
                if now - p.updated_at > max_idle_seconds:
                    if self.drop(sid):
                        dead.append(sid)
        return dead


def _normalize_session_id(sid: str) -> str:
    """清洗编号：只保留安全字符，并拒绝空/超长输入。

    为什么要显式报错而不是静默截断：截断会让两个不同的长编号
    变成同一个键，页面状态就会互相串扰。宁可拒绝，也不能混淆。
    """
    if not isinstance(sid, str):
        raise ValueError(f"session_id 必须是字符串，收到 {type(sid).__name__}")
    cleaned = "".join(c for c in sid if c.isalnum() or c in "_-")
    if not cleaned:
        raise ValueError("session_id 不能为空或只含非法字符")
    if len(cleaned) > 64:
        raise ValueError(f"session_id 过长（{len(cleaned)} > 64）")
    return cleaned


__all__ = [
    "new_session_id",
    "PageState",
    "SessionRegistry",
    "_normalize_session_id",
    "page_report_path",
    "output_root",
    "image_dir",
    "report_dir",
    "dir_for",
    "DEFAULT_MAX_IDLE_SECONDS",
    "CLOSING_GRACE_SECONDS",
]
