"""执行过程调试日志：页面侧与内核侧共用。

为什么单独一个模块而不是各自 print：
  排查"进阶算法为什么慢"必须把两侧的时间线拼起来 ——
  页面侧记"什么时候点的、传了什么参数、等了多久"，
  内核侧记"并行开了几个进程、单次拟合多少毫秒、失败几次"。
  分散在两处、格式还不同的话，出问题时根本对不上。

设计约束
--------
- 默认**关闭**。打开时才有写盘开销；关闭时 _log() 直接 return，
  连字符串都不拼（f-string 在参数位置仍然会被求值，
  所以热点路径用 _enabled() 提前判断）。
- 只用标准库，与 core/ 的零依赖承诺一致。
- 日志落盘位置与 .kernel_death.log 同目录，排查时一个文件夹看完。

用法
----
    from debug_log import debug
    debug.enable()                      # 打开
    debug.log("bootstrap", n_boot=200)  # 记一条
    debug.disable()

    # 计时上下文：自动记耗时，异常也会记（带 traceback）
    with debug.timer("band_fit"):
        ...
"""
from __future__ import annotations

import atexit
import os
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Optional

#: 日志文件名。与内核的 .kernel_death.log 放在同一目录，
#: 排查时不必翻两个地方。
LOG_NAME = ".smart_board_debug.log"

#: 单条日志最大长度。超长（比如把整个点集 dump 出来）会毁掉可读性，
#: 也会让日志体积失控。
MAX_VALUE_LEN = 400


def _default_root() -> Path:
    """日志目录。跟随内核的输出根目录，保持与死亡日志同处。"""
    env = os.environ.get("HERMES_KERNEL_OUTPUT_ROOT")
    if env:
        return Path(env)
    # 兜底：D:/.hermes_kernel（与 core/server.py 的默认值一致）
    d = Path(r"D:\.hermes_kernel")
    try:
        d.mkdir(parents=True, exist_ok=True)
        return d
    except OSError:
        # 连 D 盘都写不了时退回脚本所在目录，至少别把日志丢掉
        return Path(__file__).resolve().parent


class _DebugLog:
    """模块级单例。线程安全：多个工作线程会同时往里写。"""

    def __init__(self) -> None:
        self._enabled = False
        self._path: Optional[Path] = None
        self._lock = threading.Lock()
        self._t0 = time.perf_counter()

    # ---------------- 开关 ----------------
    def enabled(self) -> bool:
        return self._enabled

    def enable(self, path: Optional[Path] = None) -> Path:
        """打开日志，返回日志文件路径。

        可以重复调用：已打开时只换路径（或保持原路径）。
        """
        # 先确定性路径（持锁），再在锁外写会话标记。
        # 绝不能在持锁期间调 _write()/_log() —— 它们内部要拿同一把
        # 锁，而 threading.Lock 不可重入，那样会当场死锁。
        with self._lock:
            if path is not None:
                self._path = Path(path)
            elif self._path is None:
                root = _default_root()
                try:
                    root.mkdir(parents=True, exist_ok=True)
                except OSError:
                    root = Path(__file__).resolve().parent
                self._path = root / LOG_NAME
            self._enabled = True
            target = self._path
        # 锁外写：_write 自己会加锁，不能在这里持锁调用。
        self.log("debug", event="session_start", pid=os.getpid(),
                 argv0=sys.argv[0])
        return target

    def disable(self) -> None:
        # 与 enable() 同理：不能持锁调 _write()（锁不可重入）。
        # 先读状态，再在锁外写结束标记，最后置关闭。
        with self._lock:
            was_on = self._enabled
            self._enabled = False
        if was_on:
            # 落一条结束标记，便于事后确认没有中途崩溃截断。
            # 注意：此刻 _enabled 已为 False，必须直接调 _write，
            # 走 log() 会被开关挡掉。
            self._write("debug", {"event": "session_stop",
                                  "pid": os.getpid()})

    def toggle(self) -> bool:
        """切换开关，返回切换后的状态。"""
        if self._enabled:
            self.disable()
        else:
            self.enable()
        return self._enabled

    @property
    def path(self) -> Optional[Path]:
        return self._path

    # ---------------- 记录 ----------------
    def _fmt_val(self, v: Any) -> str:
        """把值压成单行，供 `grep key=value` 直接检索。

        字符串**不加 repr 引号**：写成 event='enter' 的话
        `grep "event=enter"` 匹配不到,而这是排查时最常用的入口。
        只有纯字符串才会裸输出；其余类型仍走 repr。
        """
        if isinstance(v, str):
            s = v
        else:
            s = repr(v)
        if len(s) > MAX_VALUE_LEN:
            # 序列类型只留规模：逐点位 dump 会让日志瞬间涨到几 MB
            try:
                n = len(v)                      # type: ignore[arg-type]
                s = f"<{type(v).__name__} len={n}> {s[:64]}…"
            except TypeError:
                s = f"{s[:MAX_VALUE_LEN]}…"
        # 换行会把"一行一条"的结构毁掉，也让 grep 失效
        return s.replace("\n", "\\n").replace("\r", "\\r")

    def log(self, tag: str, **fields: Any) -> None:
        """记一条日志。关闭状态下**几乎零成本**。

        注意：调用方应避免在热点位置做昂贵的参数计算，
        例如 debug.log("x", data=expensive()) 里 expensive() 仍会执行。
        """
        if not self._enabled:
            return
        payload = {k: self._fmt_val(v) for k, v in fields.items()}
        self._write(tag, payload)

    def _write(self, tag: str, payload: dict) -> None:
        path = self._path
        if path is None:
            return
        # 相对进程启动的毫秒数：比绝对时间更容易看出"哪段拖了"
        ms = (time.perf_counter() - self._t0) * 1000.0
        try:
            body = " ".join(f"{k}={payload[k]}" for k in sorted(payload))
        except Exception:
            body = repr(payload)[:MAX_VALUE_LEN]
        line = (f"[{ms:10.1f}ms] {time.strftime('%H:%M:%S')} "
                f"tid={threading.get_ident()} {tag} {body}\n")
        # 写盘可能失败（磁盘满、文件被占用）。日志是观察性的，
        # 写不进去也不能把计算搞崩 —— 与进度回调同一原则。
        try:
            with self._lock:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(line)
                    fh.flush()
        except OSError:
            pass

    # ---------------- 计时 ----------------
    def timer(self, tag: str, **fields: Any):
        """计时上下文：进入/退出各记一条，异常带 traceback。

        with debug.timer("bootstrap", n_boot=200):
            ...
        """
        return _Timer(self, tag, fields)


class _Timer:
    def __init__(self, log: _DebugLog, tag: str, fields: dict) -> None:
        self._log = log
        self._tag = tag
        self._fields = fields
        self._t0 = 0.0

    def __enter__(self) -> "_Timer":
        self._t0 = time.perf_counter()
        if self._log.enabled():
            self._log.log(self._tag, event="enter", **self._fields)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        # 返回 False：绝不在 __exit__ 里吞异常。日志不能改变控制流。
        if self._log.enabled():
            dt = (time.perf_counter() - self._t0) * 1000.0
            if exc_type is not None:
                detail = "".join(
                    traceback.format_exception(exc_type, exc, tb))
                self._log.log(self._tag, event="error",
                              elapsed_ms=round(dt, 2),
                              exc=f"{exc_type.__name__}: {exc}",
                              tb=detail[:600])
            else:
                self._log.log(self._tag, event="leave",
                              elapsed_ms=round(dt, 2))
        return False


#: 模块级单例，跨进程无状态（每个进程各写自己的那份，靠 tid/pid 区分）
debug = _DebugLog()


def reset() -> None:
    """关掉并清掉路径。仅测试用：避免用例之间串味。"""
    global debug
    debug.disable()
    debug._path = None


@atexit.register
def _flush_on_exit() -> None:
    """进程退出前确保关闭态写盘。"""
    if debug.enabled():
        debug.disable()
