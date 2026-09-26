"""子进程启动：一份逻辑同时服务源码运行与 PyInstaller exe。

为什么需要这个模块
------------------
早前 `launcher.py` 用 `[sys.executable, "-u", str(MAIN_ENTRY)]`、
`kernel_bridge.py` 用 `[_python_exe(), "-u", str(PAGE_ENTRY)]`
分别拼命令，两边各写一遍。源码模式下都正常，但打成 exe 后**同时坏掉**：

- `sys.executable` 在 exe 里是**当前这个 exe 自己**，不是 Python 解释器
- `edit/__main__.py`、`core/server.py`、`main.py` 从未作为入口被打包，
  产物目录里根本没有这些文件

于是用户在解压目录里点「Band 页面」，得到的是：

    启动失败: 找不到页面入口: D:\\...\\SmartDrawingBoard-Page.exe
    （PAGE_ENTRY 指向不存在的 edit\\__main__.py）

这不是"路径配错了"，是**进程模型在 exe 形态下不成立**：
源码模式是「一个解释器 + 多个脚本」，exe 模式必须是
「一个 exe 拉起另一个 exe」。写安装器、改环境变量都救不了，
因为缺的不是路径，是入口本身。

本模块的做法
-----------
`build_exe.py` 把 `launcher.py` / `main.py` / `edit/__main__.py`
分别打成独立 exe，放在同一目录。启动时按以下顺序解析：

    1. 同目录是否存在对应的独立 exe ？（PyInstaller 产物形态）
    2. 否则用 sys.executable + 脚本路径      （源码开发形态）

两种形态共用同一份判断，启动命令只在一处构造，
新增进程类型不会再漏掉一侧。

优先级说明：**exe 优先**。因为源码运行时同目录通常没有那些 exe，
不会误判；反之一旦检测到 exe 就说明这是分发产物，
再去用 sys.executable 跑脚本只会得到"找不到 main.py"。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

#: 独立的子进程入口：逻辑名 -> (exe 文件名, 源码脚本相对路径)
#: exe 名由 build_exe.py 的 --name 决定，两边必须一致
#: （build_exe.py 里有断言守着这个约束，改了会构建失败而不是静默出错）
CHILD_TARGETS: Dict[str, Dict[str, str]] = {
    "kernel": {
        "exe": "SmartDrawingBoard-Kernel",
        "script": "core/server.py",
    },
    "page": {
        "exe": "SmartDrawingBoard-Page",
        "script": "edit/__main__.py",
    },
    "main": {
        "exe": "SmartDrawingBoard-Main",
        "script": "main.py",
    },
}

#: 分发目录名（build_exe.py 的 BUNDLE_NAME）。四个 exe 必须同目录，
#: 因为本模块按"当前 exe 所在目录"找兄弟 exe；拆成多个目录就会
#: 「找不到入口」。
BUNDLE_DIR_NAME = "SmartDrawingBoard"


def bundle_root() -> Path:
    """产物根目录。

    源码运行：本文件（core/spawn.py）的上级目录，即项目根。
    exe 运行：exe 所在目录。PyInstaller --onedir 下
    `sys.frozen` 为真，`sys.executable` 是 `.../` 下的启动器 exe，
    其所在目录就是用户解压出来的那个目录。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    # 源码：core/spawn.py -> 项目根
    return Path(__file__).resolve().parent.parent


def _candidate_exe(name: str) -> Optional[Path]:
    root = bundle_root()
    for cand in (root / (name + ".exe"), root / name):
        if cand.exists():
            return cand
    return None


def _candidate_script(rel: str) -> Optional[Path]:
    root = bundle_root()
    cand = root / rel
    return cand if cand.exists() else None


def resolve_argv(key: str, extra_args: Optional[List[str]] = None
                 ) -> Optional[List[str]]:
    """构造启动某个子进程的 argv；两侧都找不到时返回 None。

    key 取 CHILD_TARGETS 的逻辑名（kernel / page / main）。
    extra_args 是追加在该入口自己的参数之后的命令行参数。
    """
    spec = CHILD_TARGETS.get(key)
    if spec is None:
        raise KeyError("未知的子进程类型: %r（可用: %s）"
                       % (key, ", ".join(CHILD_TARGETS)))

    args = list(extra_args or [])

    exe = _candidate_exe(spec["exe"])
    if exe is not None:
        # 分发形态：直接跑那个 exe，它就是完整入口
        return [str(exe)] + args

    script = _candidate_script(spec["script"])
    if script is not None:
        # 开发形态：解释器 + 脚本。-u 关缓冲，日志实时可见
        return [sys.executable, "-u", str(script)] + args

    return None


def missing_message(key: str) -> str:
    """两侧都没找到时给用户的提示，明确说清缺什么、怎么补。"""
    spec = CHILD_TARGETS[key]
    root = bundle_root()
    return (
        "找不到 %s 的入口。当前目录：\n"
        "  %s\n"
        "已寻找 %s.exe，未找到。\n"
        "分发版需保证 %s 目录下四个 exe 齐全"
        "（Launcher / Kernel / Main / Page），\n"
        "且它们是解压到同一目录的——不要只解压其中一个。"
        % (key, root, spec["exe"], BUNDLE_DIR_NAME)
    )


def spawn(key: str, extra_args: Optional[List[str]] = None,
          env: Optional[dict] = None, **popen_kw):
    """启动子进程，返回 (Popen, 说明)；启动不了时 Popen 为 None。

    与原先散在各处的启动代码保持同样的 detached 语义：
    新建进程组、隐藏控制台窗口（Windows）。
    """
    argv = resolve_argv(key, extra_args)
    if argv is None:
        return None, missing_message(key)

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

    proc = subprocess.Popen(
        argv,
        creationflags=creationflags,
        startupinfo=startupinfo,
        env=dict(env) if env else None,
        **popen_kw,
    )
    return proc, "已启动 %s（pid=%d）" % (key, proc.pid)


def python_exe() -> str:
    """兼容旧调用点：返回可直接执行的解释器路径。

    exe 环境下 sys.executable 不是解释器，这个函数只用于
    "必须用解释器跑一段内联代码"的场景（例如 -c），
    不要用它去启动项目脚本——那正是本模块要修掉的坑。
    """
    return sys.executable or "python"
