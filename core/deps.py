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

#: GPU 离线包的子目录。torch 的 wheel 有 2.4 GiB，
#: 与"主包 ~19 MB"完全不同量级，单独放一个子目录便于
#: 分开分发（CPU 版随 Release，CUDA 版单独 zip）与清理。
_VENDOR_GPU_SUBDIR = "gpu"

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
    subdir    vendor/ 下的子目录。GPU 的 torch wheel 放在 gpu/ 下，
              与普通依赖分开（体积差两个量级，且分发策略不同）。
    """

    filename: str
    sha256: str = ""
    subdir: str = ""

    @property
    def path(self) -> str:
        if self.subdir:
            return os.path.join(_VENDOR_DIR, self.subdir, self.filename)
        return os.path.join(_VENDOR_DIR, self.filename)


@dataclass
class DependencySpec:
    """一个可选依赖的完整描述。

    name        import 名（可能是 . 分隔的子模块）
    probe       探测函数，返回 bool。默认用 importlib。
    packages    pip 安装时要指定的分发名（可能不止一个，
                例如 pynput 在 Windows 上还需要 pywin32）
    index_url   可选的 pip 索引地址。默认 PyPI。
                CUDA 版 torch 不在 PyPI 上，必须指向
                https://download.pytorch.org/whl/cu126/torch
    offline     离线兜底包列表（按依赖顺序）
    required    True 时安装失败会抛 DependencyError（用于硬依赖）；
                False 时只告警（可选依赖，调用方应自行降级）。
    description 人类可读用途说明。
    """

    name: str
    probe: Optional[Callable[[], bool]] = None
    packages: Sequence[str] = ()
    index_url: str = ""
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


# ==================================================================
# GPU 加速（torch）—— 可选，默认完全不需要
# ==================================================================
# torch 的体积与依赖让它**不可能**塞进 Release zip：
# 完整 CUDA 版约 2.4 GiB，会让 19 MB 的 Release 翻上百倍，
# 直接破坏"解压即用"这条承诺。所以分发策略是二选一：
#
#   vendor/gpu/torch-...+cpu-....whl    ~118 MiB
#       随 Release 一起走（zip 里多 118 MB，可接受），
#       任何机器都能装，但只提供 CPU 张量 —— GPU 加速不生效。
#
#   vendor/gpu/torch-...+cu126-....whl  ~2.4 GiB
#       单独分发（不进 Release zip），仅 NVIDIA + CUDA 12.6 可用，
#       是 GPU 加速真正需要的那个。
#
# 用户勾"GPU 加速 Bootstrap"时由 ensure_dependency 按
# "探测 → PyPI → 离线"走：torch 已在环境里（多数情况，
# 因为本项目的 CPU 回落其实不依赖 torch，只有勾了 GPU 才需要）
# 就直接跳过；没有则先试联网装 cu126 版，失败再用离线包。


def _probe_torch_cuda() -> bool:
    """torch 可用**且** CUDA 可用，才认为 GPU 依赖就绪。

    只装到 CPU 版 torch 时这仍然返回 False —— 那正是本函数的意义：
    用户勾了 GPU 却只有 CPU 版，应该让他知道要补 CUDA 版，
    而不是静默地用 CPU 算完还显示"GPU 加速"。
    """
    try:
        import torch
        return bool(torch.cuda.is_available())
    except Exception:
        return False


#: PyTorch 官方的 cu126 wheel 索引（不在 PyPI 上）。
TORCH_CU126_INDEX = "https://download.pytorch.org/whl/cu126"

#: 离线包清单。版本与 Python 版本写进 sha256 注释：
#: torch 的 wheel 是按 Python 版本 + 平台 + CUDA 版本三重锁定的，
#: 换任何一个都要重新下载，所以这里的 sha256 是**精确匹配**用的，
#: 不是"大致校验"。
_TORCH_CPU = OfflinePackage(
    filename="torch-2.13.0+cpu-cp311-cp311-win_amd64.whl",
    sha256="10717d8b3b67c45a4788bf7ffc0bab1ea1e5ebbedd24466be6100102d141fac1",
    subdir=_VENDOR_GPU_SUBDIR,
)
_TORCH_CU126 = OfflinePackage(
    filename="torch-2.13.0+cu126-cp311-cp311-win_amd64.whl",
    sha256="8095729db14e7fd5178a39676fdd679208eff4041407ea34e3d898336c90f5c5",
    subdir=_VENDOR_GPU_SUBDIR,
)

#: torch 的纯 Python 依赖。版本锁定：断网环境装到不兼容组合仍会失败。
#: 摘要来自 vendor/gpu 里实际文件（D:/.cache/e2e/dump_vendor_sha.py 输出），
#: 与 PyPI 登记的官方 sha256 一致。
_TORCH_PY_DEPS = (
    OfflinePackage(
        filename="filelock-3.32.4-py3-none-any.whl",
        sha256="22e58ca3b1ae3b98993b762d7338367ae64fe50252bf78d59da3bfebcdf1cedd",
        subdir=_VENDOR_GPU_SUBDIR),
    OfflinePackage(
        filename="fsspec-2026.7.0-py3-none-any.whl",
        sha256="b57ddbafedfaef7018c1ecab32aa200a9d7ca26b77965f64e48b70061249d279",
        subdir=_VENDOR_GPU_SUBDIR),
    OfflinePackage(
        filename="jinja2-3.1.6-py3-none-any.whl",
        sha256="85ece4451f492d0c13c5dd7c13a64681a86afae63a5f347908daf103ce6d2f67",
        subdir=_VENDOR_GPU_SUBDIR),
    OfflinePackage(
        filename="networkx-3.6.1-py3-none-any.whl",
        sha256="d47fbf302e7d9cbbb9e2555a0d267983d2aa476bac30e90dfbe5669bd57f3762",
        subdir=_VENDOR_GPU_SUBDIR),
    OfflinePackage(
        filename="sympy-1.14.0-py3-none-any.whl",
        sha256="e091cc3e99d2141a0ba2847328f5479b05d94a6635cb96148ccb3f34671bd8f5",
        subdir=_VENDOR_GPU_SUBDIR),
    OfflinePackage(
        filename="typing_extensions-4.16.0-py3-none-any.whl",
        sha256="481caa481374e813c1b176ada14e97f1f67a4539ce9cfeb3f350d78d6370c2e8",
        subdir=_VENDOR_GPU_SUBDIR),
)


def _torch_index_url(extra: str = "") -> str:
    """拼出 torch 的安装索引地址。空 extra 时退化为 PyPI 默认。"""
    return TORCH_CU126_INDEX if not extra else f"{TORCH_CU126_INDEX}/{extra}"


TORCH_CPU = DependencySpec(
    name="torch",
    probe=_probe_torch_cuda,
    # CPU 版走 PyPI 即可（PyPI 上那个 118 MiB 的就是 CPU 版）
    packages=("torch",),
    offline=(_TORCH_CPU,) + _TORCH_PY_DEPS,
    required=False,
    description="GPU 加速 Bootstrap（CPU 版，GPU 加速不生效）",
)

TORCH_CUDA = DependencySpec(
    name="torch",
    probe=_probe_torch_cuda,
    packages=("torch",),
    # cu126 版**不能**只写包名：PyPI 上那个 torch 是 CPU 版，
    # 装了也跑不了 CUDA。必须显式指向 PyTorch 自己的索引。
    index_url=f"{TORCH_CU126_INDEX}/torch",
    offline=(_TORCH_CU126,) + _TORCH_PY_DEPS,
    required=False,
    description="GPU 加速 Bootstrap（CUDA 12.6，需要 NVIDIA 显卡）",
)


#: 已知依赖清单。供 install 脚本遍历。
KNOWN_DEPENDENCIES: List[DependencySpec] = [
    DECIMAL,
    TORCH_CPU,
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
    args = [*spec.packages, "--disable-pip-version-check"]
    # index_url 非空时改从该索引装。CUDA 版 torch 在 PyPI 上
    # 只有 CPU 构建，不指向 PyTorch 自己的索引就装不出 GPU 版。
    if spec.index_url:
        args += ["--index-url", spec.index_url]
    return _pip_install(args)


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
    "TORCH_CPU",
    "TORCH_CUDA",
    "TORCH_CU126_INDEX",
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
