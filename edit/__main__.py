"""edit 页面入口 —— 独立进程，可脱离主 UI 单独存活。

启动方式
--------
主 UI 通过 kernel_bridge.launch_page() 拉起：

    python -m edit --page band --port 8765

也可以手工直接跑（调试用）：

    python edit/__main__.py --page band --session-id p_deadbeef

运行时行为
----------
1. 解析参数，得到页面类型与内核端口
2. 向内核注册，拿回本页面的 session_id（页面独立编号）
3. 创建 Tk 窗口，进入消息循环
4. 窗口关闭时走 BandPage.on_close()：mark_closing -> cleanup -> 本地兜底
   删除 -> 停定时器 -> destroy()。即**关闭即回收自己的状态与产物文件**，
   只影响本 session_id，其他页面不受牵连。

与主 UI 的关系
--------------
完全解耦：主 UI 只负责"拉起本进程"，本进程不认识 main.py。
主 UI 被 kill 后本进程继续跑；内核被 kill 后本进程会弹提示，
计算功能不可用但窗口仍能打开，不会崩。
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path

# 让 `import edit.page` 在任意工作目录下都可用
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for p in (str(_ROOT), str(_HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="edit",
        description="Hermes 独立功能页面进程",
    )
    ap.add_argument("--page", default="band",
                    help="页面类型（默认 band）")
    ap.add_argument("--port", type=int, default=8765,
                    help="内核 HTTP 端口")
    ap.add_argument("--session-id", default=None,
                    help="指定页面独立编号；不传则自动生成")
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args(argv)

    # ---- 页面独立编号 ----
    # 调用方没给就自己生成，保证每个进程有唯一编号
    sid = args.session_id or ("p_" + uuid.uuid4().hex[:8])

    # ---- 内核连接 ----
    from core.client import KernelClient, KernelClientError
    client = KernelClient(host=args.host, port=args.port)

    kernel_ok = client.ping()
    registered_sid = sid
    try:
        registered_sid = client.register(
            sid, source=f"edit:{args.page}")
    except KernelClientError as e:
        # 内核没起来也要让页面能开，只是提示功能受限
        print(f"[edit] 内核注册失败: {e}", file=sys.stderr)

    # ---- 页面实现 ----
    from edit.page import BandPage

    import tkinter as tk
    root = tk.Tk()
    if not kernel_ok:
        root.after(200, lambda: _warn_no_kernel(root, args.port))
    BandPage(
        root,
        session_id=registered_sid,
        client=client,
        page_name=args.page,
    )
    root.mainloop()
    return 0


def _warn_no_kernel(root, port: int) -> None:
    from tkinter import messagebox
    messagebox.showwarning(
        "内核未连接",
        f"连不上计算内核（127.0.0.1:{port}）。\n\n"
        f"页面可以打开，但计算功能不可用。\n"
        f"请先运行主程序，或手动执行：\n"
        f"    python -m core.server --port {port}",
        parent=root,
    )


if __name__ == "__main__":
    raise SystemExit(main())
