"""自定义函数加载器。

把用户放在 functions/ 目录下的 .py 文件加载为可绘图函数。
设计要点：

1. 路径解析与"当前工作目录"解耦 —— 脚本运行、源码运行、PyInstaller 打包
   三种情况都能找到同一个目录（见 resolve_functions_dir）。
2. 加载前先做静态校验（AST），拒绝导入 os/sys/subprocess/open 等危险模块，
   拒绝 exec/eval/__import__，拒绝 dunder 属性访问。
3. 加载后再做签名校验，参数个数必须是 5。
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from graph_engine import CUSTOM_ARITY, CUSTOM

#: 自定义函数目录名
FUNCTIONS_DIRNAME = "functions"

#: 允许用户代码 import 的模块白名单
ALLOWED_IMPORTS = frozenset(
    {
        "math",
        "cmath",
        "random",
        "statistics",
        "fractions",
        "decimal",
        "numpy",
    }
)

#: 明确禁止的内置调用
FORBIDDEN_CALLS = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "open",
        "input",
        "__import__",
        "globals",
        "locals",
        "vars",
        "getattr",
        "setattr",
        "delattr",
        "breakpoint",
        "exit",
        "quit",
    }
)

MAX_FILE_BYTES = 256 * 1024  # 256KB，防止塞入巨型文件


class FunctionValidationError(Exception):
    """自定义函数不合法。"""


@dataclass(frozen=True)
class LoadedFunction:
    name: str
    path: Path
    func: Callable[..., object]
    source: str


def app_root() -> Path:
    """返回应用根目录（源码运行或打包后均可正确定位）。"""
    if getattr(sys, "frozen", False):
        # PyInstaller onefile：资源在 sys._MEIPASS
        base = getattr(sys, "_MEIPASS", None)
        if base:
            return Path(base)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resolve_functions_dir(override: Optional[str] = None) -> Path:
    """确定 functions 目录位置。

    优先级：显式 override > 环境变量 DRAWING_FUNCTIONS_DIR > app_root()/functions
    """
    import os

    if override:
        return Path(override).expanduser().resolve()

    env = os.environ.get("DRAWING_FUNCTIONS_DIR")
    if env:
        return Path(env).expanduser().resolve()

    return app_root() / FUNCTIONS_DIRNAME


def _validate_source(source: str, name: str) -> None:
    """静态校验用户函数源码。"""
    try:
        tree = ast.parse(source, filename=name)
    except SyntaxError as e:
        raise FunctionValidationError(f"语法错误: {e}") from e

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root not in ALLOWED_IMPORTS:
                    raise FunctionValidationError(f"不允许导入模块: {alias.name}")

        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root not in ALLOWED_IMPORTS:
                raise FunctionValidationError(f"不允许导入模块: {node.module}")

        elif isinstance(node, ast.Call):
            func = node.func
            fn_name = None
            if isinstance(func, ast.Name):
                fn_name = func.id
            elif isinstance(func, ast.Attribute):
                fn_name = func.attr
            if fn_name in FORBIDDEN_CALLS:
                raise FunctionValidationError(f"不允许调用: {fn_name}()")

        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__") and node.attr.endswith("__"):
                raise FunctionValidationError(f"不允许访问私有属性: {node.attr}")

        elif isinstance(node, ast.Name):
            if node.id in FORBIDDEN_CALLS:
                raise FunctionValidationError(f"不允许使用: {node.id}")


def load_function(path: Path) -> LoadedFunction:
    """加载单个函数文件并做全量校验。"""
    path = Path(path)
    if not path.is_file():
        raise FunctionValidationError(f"文件不存在: {path}")

    if path.stat().st_size > MAX_FILE_BYTES:
        raise FunctionValidationError(
            f"文件过大 ({path.stat().st_size} 字节)，上限 {MAX_FILE_BYTES} 字节"
        )

    name = path.stem
    if not name.isidentifier():
        raise FunctionValidationError(f"文件名不是合法标识符: {name}")

    source = path.read_text(encoding="utf-8")

    _validate_source(source, name)

    module_name = f"drawing_functions_{name}"
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if spec is None or spec.loader is None:
        raise FunctionValidationError(f"无法创建模块规格: {path}")

    module = importlib.util.module_from_spec(spec)
    # 注册到 sys.modules，让 dataclasses / functools 等标准机制能正常工作
    sys.modules[module_name] = module

    # 关键：禁止 importlib 在用户的 functions/ 目录里写 __pycache__。
    # 否则会污染用户目录、让目录列表出现非 .py 文件、打包时还可能因只读而告警。
    prev_dont_write = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        sys.modules.pop(module_name, None)
        raise FunctionValidationError(f"执行模块失败: {e}") from e
    finally:
        sys.dont_write_bytecode = prev_dont_write

    if not hasattr(module, name):
        sys.modules.pop(module_name, None)
        raise FunctionValidationError(f"文件中未找到同名函数 {name}()")

    func = getattr(module, name)
    if not callable(func):
        sys.modules.pop(module_name, None)
        raise FunctionValidationError(f"{name} 不是可调用对象")

    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError) as e:
        sys.modules.pop(module_name, None)
        raise FunctionValidationError(f"无法读取函数签名: {e}") from e

    required = [
        p
        for p in sig.parameters.values()
        if p.default is inspect.Parameter.empty
        and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    ]
    if len(required) != CUSTOM_ARITY:
        sys.modules.pop(module_name, None)
        raise FunctionValidationError(
            f"函数 {name} 需要 {CUSTOM_ARITY} 个位置参数 "
            f"(x, width, center_y, amp, freq)，实际 {len(required)} 个"
        )

    return LoadedFunction(name=name, path=path, func=func, source=source)


def list_function_files(directory: Optional[Path] = None) -> List[Path]:
    """列出目录下全部可用函数文件，按名称排序。"""
    directory = directory or resolve_functions_dir()
    if not directory.is_dir():
        return []
    return sorted(
        p
        for p in directory.glob("*.py")
        if p.is_file() and not p.name.startswith("_") and p.name != "__init__.py"
    )


def list_function_names(directory: Optional[Path] = None) -> List[str]:
    return [p.stem for p in list_function_files(directory)]


def load_all(directory: Optional[Path] = None) -> tuple[List[LoadedFunction], List[str]]:
    """加载目录下全部函数，返回 (成功列表, 错误信息列表)。"""
    loaded: List[LoadedFunction] = []
    errors: List[str] = []
    for path in list_function_files(directory):
        try:
            loaded.append(load_function(path))
        except FunctionValidationError as e:
            errors.append(f"{path.name}: {e}")
        except Exception as e:  # 兜底，单个文件失败不影响其他文件
            errors.append(f"{path.name}: {type(e).__name__}: {e}")
    return loaded, errors


def function_source_template(func_name: str = "my_func") -> str:
    """给"清除内容"按钮用的默认模板 —— 有界函数，不会画出界。"""
    return (
        "import math\n"
        f"def {func_name}(x, width, center_y, amp, freq):\n"
        "    # x: 当前 x 坐标（0 ~ width）\n"
        "    # width: 画布宽度（像素）\n"
        "    # center_y: 画布垂直中心（像素）\n"
        "    # amp: 振幅（像素）\n"
        "    # freq: 频率\n"
        "    # 返回: 屏幕 y 坐标（像素，向下为正）\n"
        "\n"
        "    # 归一化到 0 ~ 1，保证任何 width 下图形比例一致\n"
        "    t = x / width\n"
        "    # 平滑起伏的正弦波，振幅可达 amp\n"
        "    return center_y + amp * math.sin(2 * math.pi * freq * t)\n"
    )


__all__ = [
    "CUSTOM",
    "FUNCTIONS_DIRNAME",
    "FunctionValidationError",
    "LoadedFunction",
    "ALLOWED_IMPORTS",
    "app_root",
    "resolve_functions_dir",
    "load_function",
    "load_all",
    "list_function_files",
    "list_function_names",
    "function_source_template",
]
