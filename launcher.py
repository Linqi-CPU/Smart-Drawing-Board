"""启动器（launcher）—— 生命周期最长的进程，整套系统的锚点。

为什么需要它
------------
之前主 UI 直接拉起内核和页面。问题是：主 UI 一关，虽然内核/页面是
detached 的，但它们仍留在创建者所在的 Job Object 里（实测
job(member=1)，CREATE_BREAKAWAY_FROM_JOB 未能跳出），父 session 一结束
就会被连带杀掉——"独立存活"实际不成立。

启动器解决这个：它自己是用户双击的那个进程，生命周期由用户控制，
不受任何 agent/宿主 session 影响。内核和页面都由它创建，它活着，
这些子进程就活着；它退出时才一并收尾。

进程层次
--------
    launcher（用户控制，最长）
      ├─ core.server         内核，被 launcher 托管
      ├─ main.py             主 UI（传统绘图板）
      └─ edit/...            任意多个功能页面

与主 UI 的关系
--------------
主 UI 退化成"启动器的一个选项"，与各功能页面平级：
启动器给一张选择界面，用户点哪个开哪个。关掉某个窗口不影响其他。

界面
----
极简：一个窗口，列出可选功能，点一下就启动对应进程，
并实时显示内核状态与在线页面数量。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import tkinter as tk
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# 任意工作目录下都能 import 到项目模块
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import kernel_bridge as kb          # noqa: E402
from kernel_bridge import (DEFAULT_HOST, DEFAULT_PORT, LAUNCHERS,  # noqa: E402
                           kernel_alive, list_pages)

APP_NAME = "智能绘图板 · 启动器"
APP_VERSION = "1.0.0"

#: 主 UI 的入口（相对项目根）
MAIN_ENTRY = _HERE / "main.py"

#: 状态栏轮询间隔（毫秒）
REFRESH_MS = 2000


class LauncherApp:
    """启动器主界面：一张功能列表 + 内核/页面状态。"""

    def __init__(self, root: tk.Tk):
        self.root = root
        self._closed = False
        self._after = None

        # 由本启动器拉起的进程（不含别处起的），退出时收尾
        self._children: Dict[str, subprocess.Popen] = {}

        self._build_window()
        self._build_ui()

        # 确保内核在跑：这是启动器的第一职责
        self._ensure_kernel()

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._schedule_refresh()

    # ==================================================================
    # 界面
    # ==================================================================
    def _build_window(self) -> None:
        self.root.title(APP_NAME)
        self.root.geometry("520x560")
        self.root.minsize(460, 480)
        try:
            self.root.attributes("-topmost", True)
            self.root.after(300, lambda: self.root.attributes(
                "-topmost", False))
        except Exception:
            pass

    def _build_ui(self) -> None:
        pad = {"padx": 14, "pady": 6}

        # ---------- 标题 ----------
        head = tk.Frame(self.root)
        head.pack(fill=tk.X, **pad)
        tk.Label(head, text="选择要使用的功能",
                 font=("Microsoft YaHei UI", 13, "bold")).pack(anchor=tk.W)
        tk.Label(head, text="各功能是独立窗口，可同时打开、互不影响；"
                            "关掉本启动器会一并结束。",
                 fg="#666666", wraplength=470, justify=tk.LEFT
                 ).pack(anchor=tk.W, pady=(2, 0))

        # ---------- 功能列表 ----------
        box = tk.LabelFrame(self.root, text="功能", padx=10, pady=8)
        box.pack(fill=tk.BOTH, expand=True, **pad)

        entries = self._entries()
        for key, label, desc in entries:
            self._add_entry(box, key, label, desc)

        # ---------- 状态 ----------
        status = tk.Frame(self.root)
        status.pack(fill=tk.X, side=tk.BOTTOM, **pad)
        self.kernel_var = tk.StringVar(value="内核: 检测中…")
        tk.Label(status, textvariable=self.kernel_var, anchor=tk.W,
                 fg="#333333").pack(fill=tk.X)
        self.pages_var = tk.StringVar(value="页面: —")
        tk.Label(status, textvariable=self.pages_var, anchor=tk.W,
                 fg="#666666").pack(fill=tk.X)

        # ---------- 底部按钮 ----------
        btns = tk.Frame(self.root)
        btns.pack(fill=tk.X, side=tk.BOTTOM, **pad)
        tk.Button(btns, text="关闭全部功能页面", width=18,
                  command=self._close_all_pages).pack(side=tk.LEFT)
        tk.Button(btns, text="退出", width=10,
                  command=self.on_close).pack(side=tk.RIGHT)

    def _add_entry(self, parent: tk.Frame, key: str,
                   label: str, desc: str) -> None:
        row = tk.Frame(parent)
        row.pack(fill=tk.X, pady=4)

        left = tk.Frame(row)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tk.Label(left, text=label, font=("Microsoft YaHei UI", 10, "bold")
                 ).pack(anchor=tk.W)
        if desc:
            tk.Label(left, text=desc, fg="#777777", wraplength=330,
                     justify=tk.LEFT).pack(anchor=tk.W)

        tk.Button(row, text="打开", width=8,
                  command=lambda: self._launch(key)).pack(side=tk.RIGHT)

    def _entries(self) -> List[Tuple[str, str, str]]:
        """启动器列出的功能：主 UI + 所有已注册 edit 页面。"""
        out: List[Tuple[str, str, str]] = [
            ("__main__", "主 UI（智能绘图板）",
             "手绘、函数曲线、自动拟合、Excel 导入"),
        ]
        for name, desc in LAUNCHERS.items():
            out.append((f"page:{name}", desc, "独立窗口 · 分段包络估计与离散分析"))
        return out

    # ==================================================================
    # 启动逻辑
    # ==================================================================
    def _ensure_kernel(self) -> None:
        """确保内核在跑（启动器的第一职责）。"""
        ok, msg = kb.start_kernel()
        self.kernel_msg = msg
        if not ok:
            self.kernel_var.set(f"内核: 启动失败（{msg}）")

    def _launch(self, key: str) -> None:
        if key == "__main__":
            self._spawn("main", [sys.executable, "-u", str(MAIN_ENTRY)])
            return
        if key.startswith("page:"):
            page = key.split(":", 1)[1]
            ok, msg = kb.launch_page(page)
            if not ok:
                self._toast(f"启动失败：{msg}")
            return
        self._toast(f"未知功能：{key}")

    def _spawn(self, name: str, cmd: List[str]) -> None:
        """由启动器直接拉起的进程（目前只有主 UI）。"""
        env = dict(os.environ)
        env["HERMES_KERNEL_PORT"] = str(DEFAULT_PORT)
        env["HERMES_KERNEL_HOST"] = DEFAULT_HOST
        try:
            self._children[name] = subprocess.Popen(
                cmd, cwd=str(_HERE), env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL, close_fds=True,
                creationflags=_detached_flags(),
                startupinfo=_hidden_startupinfo(),
            )
            self._toast(f"已启动：{name}")
        except Exception as e:
            self._toast(f"启动 {name} 失败：{type(e).__name__}: {e}")

    # ==================================================================
    # 状态
    # ==================================================================
    def _schedule_refresh(self) -> None:
        if self._closed:
            return
        self._refresh()
        self._after = self.root.after(REFRESH_MS, self._schedule_refresh)

    def _refresh(self) -> None:
        alive = kernel_alive(DEFAULT_PORT)
        self.kernel_var.set(
            f"内核: {'运行中' if alive else '未运行'}"
            f"（127.0.0.1:{DEFAULT_PORT}）")
        if alive:
            pages = list_pages(DEFAULT_PORT)
            self.pages_var.set(
                f"在线页面: {len(pages)} 个"
                + (f"（{', '.join(p['session_id'] for p in pages)}）"
                   if pages else ""))
        else:
            self.pages_var.set("在线页面: —（内核未运行）")

    def _close_all_pages(self) -> None:
        """关闭所有在线页面进程（内核保留）。"""
        if not kernel_alive(DEFAULT_PORT):
            self._toast("内核未运行")
            return
        pages = list_pages(DEFAULT_PORT)
        if not pages:
            self._toast("当前没有在线页面")
            return
        from tkinter import messagebox
        if not messagebox.askyesno(
                APP_NAME, f"确定关闭全部 {len(pages)} 个页面吗？",
                parent=self.root):
            return
        ok, failed = [], []
        for p in pages:
            pid = p.get("pid")
            if not pid:
                continue
            try:
                r = subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                                   capture_output=True, timeout=10)
                (ok if r.returncode == 0 else failed).append(
                    f"{p['session_id']}({pid})")
            except Exception as e:
                failed.append(f"{p['session_id']}({pid}): {e}")
        self._toast(f"已关闭 {len(ok)} 个" +
                    (f"；失败 {len(failed)} 个" if failed else ""))

    # ==================================================================
    # 关闭：先收尾自己拉起的进程，再退出
    # ==================================================================
    def on_close(self) -> None:
        if self._closed:
            return
        self._closed = True

        # 停掉状态轮询，否则 destroy 后仍会派发回调
        if self._after is not None:
            try:
                self.root.after_cancel(self._after)
            except Exception:
                pass
            self._after = None

        # 收尾：关闭所有功能页面（内核保留，下次启动仍可复用）
        try:
            pages = list_pages(DEFAULT_PORT)
        except Exception:
            pages = []
        for p in pages:
            pid = p.get("pid")
            if not pid:
                continue
            try:
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True, timeout=8)
            except Exception:
                pass

        # 等待页面真正退出并从内核注销
        deadline = time.time() + 12
        while time.time() < deadline:
            try:
                if not list_pages(DEFAULT_PORT):
                    break
            except Exception:
                break
            time.sleep(0.2)

        # 自己拉起的进程（主 UI）
        for proc in self._children.values():
            try:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=5)
            except Exception:
                pass

        try:
            self.root.destroy()
        except Exception:
            pass

    # ==================================================================
    # 小工具
    # ==================================================================
    def _toast(self, text: str) -> None:
        """在状态栏闪一条消息。"""
        original = self.kernel_var.get()
        self.kernel_var.set(text)
        self.root.after(2500, lambda: self.kernel_var.set(original))


def _detached_flags() -> int:
    if os.name != "nt":
        return 0
    return (getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0))


def _hidden_startupinfo():
    if os.name != "nt":
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    return si


def main(argv: Optional[List[str]] = None) -> int:
    root = tk.Tk()
    LauncherApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())