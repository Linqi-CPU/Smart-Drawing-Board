"""edit 页面包。

这里放所有"独立功能页面"。每个页面：

- 是一个独立的 Tk 窗口（自己的进程）
- 有自己的 session_id（页面独立编号）
- 只通过 core.client 与内核通信，不认识 main.py
- 主 UI 被 kill 后依然存活

新增一个页面只需：在此包加一个模块，然后
1. kernel_bridge.LAUNCHERS 里登记名字
2. edit/__main__.py 里加分发
主 UI 界面会自动列出，无需改动主界面代码。
"""

__all__ = ["page"]
