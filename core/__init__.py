"""计算内核包。

这个包**不依赖 tkinter**，也不依赖任何 UI 层，可被以下三者共同调用：

- 主 UI（main.py）：把用户请求转成内核服务调用
- edit 页面进程（edit/__main__.py）：独立窗口发起计算
- 测试与命令行脚本：直接 import 使用

因为内核内部各模块用扁平 import（如 `import fitting as ft`），
在包内运行时需要把本目录放在 sys.path 最前面。
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

__all__ = [
    "fitting",
    "band_fit",
    "band_advanced",
    "gpu_backend",
    "deps",
    "data_import",
    "graph_engine",
    "custom_loader",
    "session",
]

__version__ = "1.1.0"
