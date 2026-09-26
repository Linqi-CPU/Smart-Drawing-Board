"""可选依赖的探测 / 安装 / 离线兜底。

设计动机
--------
本项目有两条硬承诺：
  1. **零第三方依赖**：build_exe.py 显式 exclude numpy/torch/pandas，
     Release 解压即用（~12 MB），用户不用装 Python、不用配环境。
  2. **不因可选功能而崩**：某些增强（高精度系数还原、GPU 加速）
     在依赖缺失时必须静默降级，而不是抛 ImportError。

本模块把"可选依赖"的获取流程标准化，供 install 脚本与核心模块共用：

    探测 ──有──> 跳过（什么都不做）
      │
      └──无──> 尝试联网安装（pip）
                │
                ├──成功──> 完成
                └──失败──> 本地离线包安装（--no-index --find-links）
                          │
                          ├──成功──> 完成
                          └──失败──> 报错并给出人工提示

其中离线包必须是**事先随项目分发的 .whl**，
安装前用 SHA-256 校验完整性，校验不过拒绝安装（防止
下载损坏或离线包被篡改后静默引入行为不一致的二进制）。

关于 decimal 的说明
-------------------
``decimal`` 是 Python 标准库，理论上永远存在，
所以本文件对它是"空跑"（探测即通过）。
本框架的价值在于：当未来把高精度后端换成 mpmath / gmpy2 /
sympy 等真·第三方库时，安装、离线兜底、校验逻辑无需重写。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence

#: 离线包目录：随 Release 一起分发的第三方 wheel。
#: 放在项目根的 vendor/ 下，PyInstaller 可通过 --add-data 打包。
#: 用绝对路径，避免依赖 CWD。
_VENDOR_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "vendor",
)

#: pip 安装超时（秒）。仓库源偶发慢，给足但别无限等。
PIP_TIMEOUT = 180


class DependencyError(Exception):
    """可选依赖获取失败。"""


@dataclass(frozen=True)
class OfflinePackage:
    """一个本地离线 wheel 及其完整性期望值。

    filename  仓库中的文件名（如 mpmath-1.3.0-py3-none-any.whl）
    sha256    该文件的 SHA-256 十六进制摘要，**小写**。
              留空表示"不校验"（仅建议用于测试）。
    """

    filename: str
    sha256: str = ""

    @property
    def path(self) -> str:
        return os.path.join(_VENDOR_DIR, self.filename)


@dataclass
class DependencySpec:
    """一个可选依赖的完整描述。

    name        import 名（可能是 . 分隔的子模块）
    probe       探测函数，返回 bool。默认用 importlib。
    packages    pip 安装时要指定的分发名（可能不止一个，
                例如 pynput 在 Windows 上还需要 pywin32）
    offline     离线兜底包列表（按依赖顺序）
    required    True 时安装失败会抛 DependencyError（用于硬依赖）；
                False 时只告警（可选依赖，调用方应自行降级）。
    description 人类可读用途说明。
    """

    name: str
    probe: Optional[Callable[[], bool]] = None
    packages: Sequence[str] = ()
    offline: Sequence[OfflinePackage] = ()
    required: bool = False
    description: str = ""

    # ---------- 探测 ----------
    def is_available(self) -> bool:
        """依赖是否已可用（先调用自定义 probe，否则尝试 import）。"""
        if self.probe is not None:
            try:
                if self.probe():
                    return True
            except Exception:
                return False
            # probe 返回 False 时不再尝试裸 import：
            # 自定义 probe 的存在意味着它有额外判据
            # （例如 decimal 的情况是"能 import 且能真算一次"）。
            return False
        try:
            import importlib
            importlib.import_module(self.name)
            return True
        except Exception:
            return False

    # ---------- 状态描述 ----------
    def status(self) -> str:
        ok = self.is_available()
        tag = "已就绪" if ok else "缺失"
        why = f"（{self.description}）" if self.description else ""
        return f"{self.name}: {tag}{why}"


# ==================================================================
# 预置的依赖清单
# ==================================================================
def _probe_decimal() -> bool:
    from core import fitting
    return fitting.high_precision_available()


#: decimal —— 标准库，恒可用。留在清单里是为了让
#: install 脚本输出中含它，未来换第三方库时改这里即可。
DECIMAL = DependencySpec(
    name="decimal",
    probe=_probe_decimal,
    packages=(),                     # 标准库，无需安装
    offline=(),
    required=False,
    description="高精度系数还原（t→x 空间）",
)


#: 已知依赖清单。供 install 脚本遍历。
KNOWN_DEPENDENCIES: List[DependencySpec] = [
    DECIMAL,
]


# ==================================================================
# SHA-256 校验
# ==================================================================
def sha256_file(path: str, chunk_size: int = 1 << 20) -> str:
    """计算文件 SHA-256，返回小写十六进制。

    分块读，避免把几百 MB 的 wheel 全读进内存。
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def verify_offline_package(pkg: OfflinePackage) -> tuple:
    """校验一个离线 wheel。返回 (ok, detail)。

    detail 是人类可读的说明，成功时是摘要本身，
    失败时是不匹配的原因或文件缺失提示。
    """
    path = pkg.path
    if not os.path.isfile(path):
        return False, f"离线包不存在: {path}"

    actual = sha256_file(path)

    if not pkg.sha256:
        # 未提供期望值：允许安装但明确告知未经校验。
        # 仅用于测试；正式分发必须填 sha256。
        return True, f"未设定期望摘要，跳过校验（实际 {actual}）"

    if actual.lower() != pkg.sha256.lower():
        return (
            False,
            "摘要不匹配\n"
            f"    文件:   {path}\n"
            f"    期望:   {pkg.sha256.lower()}\n"
            f"    实际:   {actual}\n"
            "    该离线包可能下载损坏或被篡改，已拒绝安装。",
        )
    return True, actual


# ==================================================================
# 安装
# ==================================================================
def _pip_install(args: Sequence[str], timeout: int = PIP_TIMEOUT) -> tuple:
    """调用 pip 安装。返回 (ok, output)。

    用 sys.executable -m pip 而不是裸 "pip"：
    Windows 上可能装了多个 Python，裸 pip 指哪个不确定。
    """
    cmd = [sys.executable, "-m", "pip", "install", *args]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return False, f"pip 超时（>{timeout}s）: {' '.join(cmd)}"
    except FileNotFoundError as e:
        return False, f"找不到 pip: {e}"

    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, out.strip()


def install_from_pypi(spec: DependencySpec) -> tuple:
    """尝试从 PyPI 安装。返回 (ok, output)。"""
    if not spec.packages:
        return True, "无需安装（标准库或已内置）"
    return _pip_install([*spec.packages, "--disable-pip-version-check"])


def install_from_offline(spec: DependencySpec) -> tuple:
    """尝试从本地离线包安装。返回 (ok, output)。

    关键点：
      - 逐个包做 SHA-256 校验，**全部通过才继续安装**。
        校验失败的包直接判整个依赖为失败，不装其他包
        —— 装一半的依赖比完全没装更难排查。
      - 用 --no-index --find-links 严格禁止走网络，
        否则"离线安装"会在网络恢复时悄悄变成联网安装，
        背离"断网可装"的初衷。
      - 用临时副本安装而非直接指向 vendor/ 里的文件：
        pip 会把 wheel 缓存/解压到自身缓存目录，
        直接操作 vendor/ 可能留下垃圾文件，且在只读
        安装目录（Program Files 下的 Python）上会失败。
    """
    if not spec.offline:
        return False, "没有配置离线包"

    # 1) 先校验全部包
    tmpdir = tempfile.mkdtemp(prefix="hermes_vendor_")
    try:
        verified: List[str] = []
        for pkg in spec.offline:
            ok, detail = verify_offline_package(pkg)
            if not ok:
                return False, detail
            # 复制到临时目录，避免 pip 弄脏 vendor/
            dest = os.path.join(tmpdir, pkg.filename)
            shutil.copyfile(pkg.path, dest)
            verified.append(dest)

        # 2) 严格离线安装
        return _pip_install([
            *verified,
            "--no-index",
            "--find-links", tmpdir,
            "--disable-pip-version-check",
        ])
    except Exception as e:
        return False, f"离线安装异常: {e}"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def ensure_dependency(spec: DependencySpec, log: Optional[Callable[[str], None]] = None) -> bool:
    """按"探测 → PyPI → 离线"的顺序确保依赖可用。

    有就跳过；没有先试联网；联网失败试离线包。
    返回 True 表示最终可用（或本就可用）。
    required=True 且最终失败时抛 DependencyError。
    """
    def say(msg: str) -> None:
        if log is not None:
            log(msg)

    if spec.is_available():
        say(f"[跳过] {spec.name} 已就绪")
        return True

    say(f"[安装] {spec.name} 缺失，尝试从 PyPI 安装 ...")
    ok, out = install_from_pypi(spec)
    if ok:
        _reset_probe_caches()
        if spec.is_available():
            say(f"[完成] {spec.name} 安装成功")
            return True
        say(f"[警告] {spec.name} 安装命令成功但导入仍失败")

    say(f"[回退] {spec.name} 联网安装失败，尝试本地离线包 ...")
    ok2, out2 = install_from_offline(spec)
    if ok2:
        _reset_probe_caches()
        if spec.is_available():
            say(f"[完成] {spec.name} 离线安装成功")
            return True
        say(f"[警告] {spec.name} 离线安装命令成功但导入仍失败")

    msg = (
        f"{spec.name} 安装失败。\n"
        f"  PyPI 输出: {out[-800:] if out else '(空)'}\n"
        f"  离线输出: {out2[-800:] if out2 else '(空)'}"
    )
    if spec.required:
        raise DependencyError(msg)
    say(f"[失败] {msg}")
    return False


_PROBE_CACHE_RESETS: Dict[str, Callable[[], None]] = {}


def _register_probe_cache_reset(fn: Callable[[], None]) -> None:
    """让依赖安装后能刷新核心模块的可用性缓存。

    fitting._HP_CACHE 一旦为 False 就不会再探测，
    装完 decimal 必须清掉，否则本次运行内仍判定缺失。
    """
    _PROBE_CACHE_RESETS[fn.__name__] = fn


def _reset_probe_caches() -> None:
    for fn in list(_PROBE_CACHE_RESETS.values()):
        try:
            fn()
        except Exception:
            pass


def _register_default_resets() -> None:
    from core import fitting
    _register_probe_cache_reset(fitting._reset_hp_cache)


_register_default_resets()


__all__ = [
    "DependencyError",
    "DependencySpec",
    "OfflinePackage",
    "KNOWN_DEPENDENCIES",
    "DECIMAL",
    "sha256_file",
    "verify_offline_package",
    "install_from_pypi",
    "install_from_offline",
    "ensure_dependency",
    "vendor_dir",
]


def vendor_dir() -> str:
    """离线包目录路径（供安装脚本与文档引用）。"""
    return _VENDOR_DIR
