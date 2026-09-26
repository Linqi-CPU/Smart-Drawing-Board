"""主 UI 的内核托管与页面启动中心。

主 UI 的职责被刻意限制为"**激活**"：
- 启动时拉起内核 HTTP 服务（若未在跑）
- 提供按钮拉起独立的 edit 页面进程
- 显示当前有哪些页面在线、它们各自算出了什么

主 UI **不**承载任何具体的功能界面。新功能一律做成 edit/ 下的独立页面：
独立窗口、独立编号、独立进程。这样：

- 主 UI 被 kill，页面与内核继续存活（页面是 detached 子进程）
- 新增功能不用反复改主 UI 界面，只要加页面 + 在 LAUNCHERS 注册
- 页面之间也互不干扰，各有自己的 session_id

进程模型
--------
    主 UI (main.py)
      ├── 拉起 → 内核服务 (core/server.py)   独立进程，端口 8765
      └── 拉起 → 页面进程 (edit/__main__.py) 独立进程，detached

内核与页面都是独立进程，主 UI 退出不影响它们；
再次打开主 UI 时会发现内核已在跑、页面仍在跑（端口探测 + 会话列表）。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# 子进程启动：源码模式用解释器+脚本，exe 模式用同目录 exe。
# 两种形态的判断收敛在这一处，launcher / kernel_bridge 共用。
# core/spawn.py 在子目录，这里显式把 core/ 加进 sys.path 再扁平 import，
# 与 core/ 内部一致的扁平风格（core/__init__.py 也是这么自举的）。
_CORE_DIR = Path(__file__).resolve().parent / "core"
if _CORE_DIR.is_dir() and str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))
import spawn  # noqa: E402

#: 项目根目录（本文件所在目录）
ROOT = Path(__file__).resolve().parent

#: 内核服务可执行入口
KERNEL_ENTRY = ROOT / "core" / "server.py"

#: 页面进程入口
PAGE_ENTRY = ROOT / "edit" / "__main__.py"

#: 端口与 host 统一由环境变量控制，与 core.client 读同一组变量，
#: 保证 UI 起的和内核 listen 的是同一个端口。
DEFAULT_PORT = int(os.environ.get("HERMES_KERNEL_PORT", "8765"))
DEFAULT_HOST = os.environ.get("HERMES_KERNEL_HOST", "127.0.0.1")

#: 已注册的页面启动器：页面名 -> 说明
#: 新增页面时在这里加一行即可，主 UI 界面会自动列出来
LAUNCHERS: Dict[str, str] = {
    "band": "分段包络估计走势与离散分析",
}


def _python_exe() -> str:
    """优先复用当前解释器，保证依赖一致。"""
    return sys.executable or "python"


def kernel_url(port: int = DEFAULT_PORT) -> str:
    return f"http://127.0.0.1:{port}"


def kernel_alive(port: int = DEFAULT_PORT, timeout: float = 1.5) -> bool:
    """探测内核是否在跑。"""
    try:
        with urllib.request.urlopen(kernel_url(port) + "/api/ping",
                                    timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def start_kernel(port: int = DEFAULT_PORT) -> Tuple[bool, str]:
    """确保内核在跑。返回 (是否本次新启动, 说明)。"""
    if kernel_alive(port):
        return False, "内核已在运行"

    if not KERNEL_ENTRY.exists():
        return False, f"找不到内核入口: {KERNEL_ENTRY}"

    # detached 启动：主 UI 退出后内核继续存活
    creationflags = 0
    startupinfo = None
    if os.name == "nt":
        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
        )
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0  # SW_HIDE

    log = ROOT / ".kernel.log"
    argv = spawn.resolve_argv("kernel", ["--port", str(port)])
    if argv is None:
        return False, spawn.missing_message("kernel")
    try:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(f"\n--- start kernel {time.strftime('%F %T')} ---\n")
            fh.write("argv: %r\n" % (argv,))
            fh.flush()
            subprocess.Popen(
                argv,
                cwd=str(ROOT),
                stdout=fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=creationflags,
                startupinfo=startupinfo,
                close_fds=True,
            )
    except Exception as e:
        return False, f"内核启动失败: {type(e).__name__}: {e}"

    # 等它就绪
    for _ in range(40):
        if kernel_alive(port, timeout=0.5):
            return True, "内核已启动"
        time.sleep(0.15)
    return False, "内核启动超时，请查看 .kernel.log"


def launch_page(page: str = "band", port: int = DEFAULT_PORT,
                session_id: Optional[str] = None,
                host: str = DEFAULT_HOST) -> Tuple[bool, str]:
    """启动一个独立的 edit 页面进程。

    返回 (是否成功拉起, 说明)。
    页面本身会自己向内核注册，故这里不等待注册完成。
    """
    if page not in LAUNCHERS:
        return False, f"未知页面 '{page}'，可用: {', '.join(LAUNCHERS)}"
    argv = spawn.resolve_argv(
        "page", ["--page", page, "--port", str(port)])
    if argv is None:
        return False, spawn.missing_message("page")
    if session_id:
        argv += ["--session-id", session_id]

    alive, msg = start_kernel(port)
    if not alive and not kernel_alive(port):
        return False, f"内核未就绪: {msg}"

    cmd = argv

    creationflags = 0
    startupinfo = None
    if os.name == "nt":
        creationflags = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 5  # SW_SHOW

    # 显式把端口塞进子进程环境变量：页面内 core.client 读的就是这个，
    # 不依赖命令行参数的解析顺序，也不会漏传。
    env = dict(os.environ)
    env["HERMES_KERNEL_PORT"] = str(port)
    env["HERMES_KERNEL_HOST"] = host

    try:
        subprocess.Popen(
            cmd, cwd=str(ROOT), env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
            startupinfo=startupinfo,
            close_fds=True,
        )
    except Exception as e:
        return False, f"页面启动失败: {type(e).__name__}: {e}"

    return True, f"已拉起页面 {page}（{LAUNCHERS[page]}）"


def list_pages(port: int = DEFAULT_PORT) -> List[dict]:
    """向内核查询在线页面列表。"""
    try:
        req = urllib.request.Request(
            kernel_url(port) + "/api/sessions",
            data=b"{}", method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            import json
            obj = json.loads(resp.read().decode("utf-8"))
            return obj.get("sessions", [])
    except Exception:
        return []


def reap(port: int = DEFAULT_PORT) -> List[str]:
    """让内核立即巡检一次，返回被回收的孤儿页面编号。"""
    try:
        req = urllib.request.Request(
            kernel_url(port) + "/api/reap",
            data=b"{}", method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            import json
            obj = json.loads(resp.read().decode("utf-8"))
            return obj.get("reaped", [])
    except Exception:
        return []


def status_text(port: int = DEFAULT_PORT) -> str:
    """给主 UI 状态栏用的一句话状态。"""
    if not kernel_alive(port):
        return "内核: 未运行"
    pages = list_pages(port)
    if not pages:
        return "内核: 运行中 | 页面: 无"
    return f"内核: 运行中 | 页面: {len(pages)} 个在线"


__all__ = [
    "LAUNCHERS",
    "DEFAULT_PORT",
    "kernel_alive",
    "kernel_url",
    "start_kernel",
    "launch_page",
    "list_pages",
    "reap",
    "status_text",
]
