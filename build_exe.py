"""本地打包脚本：把 Smart Drawing Board 打成 Windows exe，并分出源码/成品 zip。

使用方式：
    python build_exe.py

产出：
    dist/SmartDrawingBoard-Launcher/   成品（解压即用）
    dist/SmartDrawingBoard-Main/
    dist/SmartDrawingBoard-Page/
    release/SmartDrawingBoard-Source.zip      源码
    release/SmartDrawingBoard-Launcher.zip    成品
    release/SmartDrawingBoard-Main.zip        成品
    release/SmartDrawingBoard-Page.zip        成品

源码与成品严格分开，各有独立 zip：
- 源码包只含"应进版本库"的文件，本地缓存、构建产物、
  误建的空文件（如 Windows 下手滑写出的 nul）都不会混进去，
  使用者拿到的就是一份干净的代码，不是一堆 __pycache__。
- 成品包内不带多余顶层目录，解压即可运行。

GitHub Actions 与本地都调用本脚本，打包行为完全一致。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import List, Optional

# ------------------------------------------------------------------
# 输出编码：Windows 控制台默认 cp1252，中文 print 会直接 UnicodeEncodeError
# ------------------------------------------------------------------
# GitHub Actions 的 windows runner 不设 PYTHONIOENCODING，
# Python 3.11 的 sys.stdout 会按 cp1252（或 GBK）编码，
# 本脚本里任何中文提示都会让整个打包挂在最后一步。
# 历史上也踩过：见 CHANGELOG 里 d968685「remove non-ASCII output」，
# 那是靠删中文绕过，这里改成无条件重配为 UTF-8，中文可以放心写。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    # 老版本 Python 或已被外部重定向时没有 reconfigure，退回收紧输出
    pass

_HERE = Path(__file__).resolve().parent
_DIST = _HERE / "dist"
_RELEASE = _HERE / "release"
#: 各入口的独立构建暂存区，最终会合并进 _DIST/BUNDLE_NAME
_STAGING = _HERE / "build" / "staging"
#: 合并后单一分发目录名 —— 用户解压 zip 后看到的唯一目录。
#: 四个 exe（Launcher/Kernel/Main/Page）与共享的 _internal/ 都在这里面，
#: 因为 spawn.bundle_root() 按"当前 exe 所在目录"找兄弟 exe。
BUNDLE_NAME = "SmartDrawingBoard"

#: 四个可执行入口：(产物目录名, 入口脚本)
#:
#: 四个都必须是独立 exe，一个都不能少。原因见 core/spawn.py 的说明：
#: exe 环境下 sys.executable 是 exe 自己而非解释器，Launcher 想拉起
#: 内核/页面/主 UI 时只能"一个 exe 起另一个 exe"。少打一个，
#: 对应功能在 exe 里就是"找不到入口"而源码下完全正常。
APP_ENTRIES = [
    ("SmartDrawingBoard-Launcher", _HERE / "launcher.py"),
    ("SmartDrawingBoard-Kernel", _HERE / "core" / "server.py"),
    ("SmartDrawingBoard-Main", _HERE / "main.py"),
    ("SmartDrawingBoard-Page", _HERE / "edit" / "__main__.py"),
]

#: 源码 zip 内的顶层目录名，解压后不会散落一地
SOURCE_PREFIX = "Smart-Drawing-Board-Source"

#: 回退扫描源码时要排除的目录（.gitignore 的镜像，供无 .git 时使用）
_SOURCE_EXCLUDE_DIRS = {
    ".git", "build", "dist", "release", "__pycache__",
    ".pytest_cache", ".venv", "venv", "env", "node_modules",
}
#: 回退扫描时排除的后缀
_SOURCE_EXCLUDE_SUFFIX = {".pyc", ".pyo", ".tmp", ".bak", ".log"}
#: 回退扫描时排除的文件名（含 Windows 误建的 nul 与本地待办）
_SOURCE_EXCLUDE_NAMES = {"nul", "desktop.ini", ".ds_store", "todo.md"}


def _ensure_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401
    except Exception as e:
        raise SystemExit(
            "缺少 PyInstaller，请先执行：\n"
            "    pip install pyinstaller\n"
            f"原异常：{e}"
        ) from e


def _clean_dist() -> None:
    """清空 dist/。删不动时给出可操作的提示，而不是甩 PermissionError。

    Windows 下 dist/ 里的 exe 正被运行（用户开着 Launcher 调试）时，
    rmtree 会 PermissionError。此时应告诉用户关掉哪个进程，
    而不是让人对着一堆 traceback 猜。
    """
    if not _DIST.exists():
        _DIST.mkdir(parents=True, exist_ok=True)
        return
    try:
        shutil.rmtree(_DIST)
    except PermissionError as e:
        raise SystemExit(
            "无法清空 dist/，多半是里面的 exe 正在运行：\n"
            f"  {e.filename or _DIST}\n"
            "请关闭 SmartDrawingBoard-* 进程后重试。\n"
            "（Windows 下运行中的 exe 会被文件锁占用，删不掉也覆盖不掉。）"
        ) from e
    _DIST.mkdir(parents=True, exist_ok=True)


def _prepare_release_dir() -> None:
    """清空 release/，保证每次打包出来的 zip 列表是确定的。

    不清的话，改了产物名之后旧的 zip 会留下来，上传步骤
    会把过期文件一起塞进 Release，用户下到历史残留。
    """
    if _RELEASE.exists():
        shutil.rmtree(_RELEASE)
    _RELEASE.mkdir(parents=True, exist_ok=True)


def _core_hidden_imports() -> List[str]:
    """扫描 core/ 下所有模块，生成 --hidden-import 列表。

    两个坑（都是实测踩出来的）：

    1) **core/ 内部用扁平 import**（`import fitting as ft`，
       不是 `from core import fitting`），靠 core/__init__.py 把本目录
       插到 sys.path[0] 实现。所以 hidden-import 必须用**不带前缀的
       扁平名**：写 `core.fitting` 的话 PyInstaller 会按包名去找，
       与运行时真正 import 的 `fitting` 对不上，exe 里 ImportError。
       两种名字都列，双保险。

    2) **torch 会被 hook-torch.py 拖进来**：gpu_backend.py 里
       `import torch` 是函数内延迟 import（GPU 不可用时根本不会执行），
       但 PyInstaller 的静态分析穿透函数体，触发 hook-torch，
       连带 hook-cv2 / hook-transformers / hook-av / hook-lxml，
       把整个 ML 生态塞进 exe，体积翻数十倍。
       上面 --exclude-module=torch 就是为这个，见 gpu_backend.py 的
       设计注释（零第三方依赖）。

    扫描而非手列，加模块不再漏。
    """
    core_dir = _HERE / "core"
    if not core_dir.is_dir():
        return ["--hidden-import=core"]

    out: List[str] = ["--hidden-import=core"]

    def _walk(pkg_dir: Path, prefix: str) -> None:
        for entry in sorted(pkg_dir.iterdir()):
            if entry.name.startswith(("_", ".")):
                continue
            if entry.name == "__pycache__":
                continue
            if entry.is_dir():
                if (entry / "__init__.py").exists():
                    _walk(entry, f"{prefix}.{entry.name}")
                continue
            if entry.suffix == ".py":
                stem = entry.stem
                out.append(f"--hidden-import={stem}")       # 扁平名，运行时用的
                out.append(f"--hidden-import={prefix}.{stem}")  # 包名，双保险

    _walk(core_dir, "core")
    return out


def _common_options() -> list[str]:
    # core/ 必须进 --paths：core 内部是扁平 import（import fitting as ft），
    # 靠 sys.path[0] 定位同目录模块。PyInstaller 只在源码树里能自动
    # 追到这些扁平名，但 frozen 后它们的搜索路径来自 --paths，
    # 少了这一项，exe 里 `import band_advanced` 直接
    # ModuleNotFoundError —— 内核 exe 跑不起来，而 CI 仍显示构建成功。
    core_paths = []
    if (_HERE / "core").is_dir():
        core_paths = ["--paths", str(_HERE / "core")]
    edit_paths = []
    if (_HERE / "edit").is_dir():
        edit_paths = ["--paths", str(_HERE / "edit")]

    return [
        "--noconfirm",
        "--clean",
        "--onedir",
        "--windowed",
        f"--add-data={_HERE / 'functions'};functions",
        *core_paths,
        *edit_paths,
        *_core_hidden_imports(),
        "--hidden-import=edit",
        "--hidden-import=edit.page",
        "--exclude-module=matplotlib",
        "--exclude-module=numpy",
        "--exclude-module=pandas",
        "--exclude-module=PIL",
        # gpu_backend 的 `import torch` 是函数内延迟 import，但 PyInstaller
        # 静态分析穿透函数体，会触发 hook-torch 并连带 cv2/transformers/av/lxml，
        # 把整个 ML 生态打进 exe（体积数十倍）。GPU 路径本就设计为可选，
        # 缺 torch 时自动回落 CPU，故这里必须排除。
        "--exclude-module=torch",
        "--exclude-module=torchvision",
        "--exclude-module=torchaudio",
        "--exclude-module=cv2",
        "--exclude-module=transformers",
        "--exclude-module=av",
        "--exclude-module=tkinter.test",
        "--distpath",
        str(_DIST),
        "--workpath",
        str(_HERE / "build" / "pyi-work"),
        "--specpath",
        str(_HERE / "build" / "pyi-spec"),
    ]


def _build_entry(name: str, script: Path) -> None:
    if not script.exists():
        raise SystemExit(f"找不到入口文件：{script}")

    args = [
        sys.executable,
        "-m",
        "PyInstaller",
        str(script),
        f"--name={name}",
        *_common_options(),
    ]
    print(f"\n>>> Building: {name}")
    print("    Entry: " + str(script))

    # 每个入口先各自构建到 _STAGING/<name>/，最后再合并进一个总目录。
    # 为什么不直接构建到总目录：--onedir 模式下每个入口会产出
    # 自己的一套 _internal/（Python 运行时 + 依赖），四个入口并排放进
    # 同一目录时这些 _internal 会互相覆盖，产物直接坏掉。
    staging = _STAGING / name
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)
    args += ["--distpath", str(staging)]

    proc = subprocess.run(args, cwd=str(_HERE))
    if proc.returncode != 0:
        raise SystemExit(f"打包失败：{name}")


def _entry_output_dir(name: str) -> Path:
    """某个入口的实际产出目录。

    PyInstaller 的 --name=X 与 --distpath=Y 组合会在 Y/X/ 下再建一层
    同名目录（实测：--distpath=build/staging/SmartDrawingBoard-Kernel
    产出 build/staging/SmartDrawingBoard-Kernel/SmartDrawingBoard-Kernel/）。
    早前按 Y/ 直接找 exe，报了"暂存产物里没有 exe"。
    """
    base = _STAGING / name
    nested = base / name
    if nested.is_dir():
        return nested
    return base


def _merge_into_bundle() -> Path:
    """把 _STAGING/ 下各入口的 exe 合并成 dist/<包名>/ 单个目录。

    每个入口的 _internal/ 只保留一份：四个 exe 共享同一个 Python 运行时
    与依赖树（版本与内容完全一致，因为同一份代码、同一份排除名单）。
    分开保留四份会让体积翻四倍，没有任何收益。

    合并后 spawn.bundle_root() 指向这里，四个 exe 天然同目录，
    "一个 exe 起另一个 exe" 的判断直接成立。
    """
    bundle = _DIST / BUNDLE_NAME
    if bundle.exists():
        shutil.rmtree(bundle)
    bundle.mkdir(parents=True, exist_ok=True)

    internal_copied = False
    for name, _script in APP_ENTRIES:
        src = _entry_output_dir(name)
        if not src.is_dir():
            raise SystemExit(f"缺少暂存产物: {src}")
        # exe 提升到 bundle 根
        exe = src / f"{name}.exe"
        if not exe.exists():
            raise SystemExit(f"暂存产物里没有 exe: {exe}")
        shutil.copy2(exe, bundle / exe.name)

        # _internal/ 只搬第一份（四个入口的运行时内容相同）
        src_internal = src / "_internal"
        if src_internal.is_dir() and not internal_copied:
            shutil.copytree(src_internal, bundle / "_internal")
            internal_copied = True

    if not internal_copied:
        raise SystemExit("四个入口都没有产出 _internal/，打包结果不可用")

    # functions/ 数据目录（如果有入口带了它，同样只保留一份）
    for name, _script in APP_ENTRIES:
        funcs = _entry_output_dir(name) / "functions"
        if funcs.is_dir() and not (bundle / "functions").exists():
            shutil.copytree(funcs, bundle / "functions")
            break

    print(f">>> 合并为单一目录: dist/{BUNDLE_NAME}/")
    return bundle


def _clean_staging() -> None:
    if _STAGING.exists():
        shutil.rmtree(_STAGING)


# ==================================================================
# Release 打包：源码与成品分开
# ==================================================================
def _is_git_dir(name: str) -> bool:
    """判断是否是 git 元数据目录。

    不只用精确的 ".git"：被重命名/另存过的仓库里它可能叫
    ".git-disabled"、".git.old" 之类，一律排除更稳。
    """
    return name.lower().startswith(".git")


def _iter_source_files(root: Path):
    """回退方案：扫描工作区，产出该进源码包的相对路径。"""
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        # 只要任一上级目录在排除名单里，就跳过
        if any(part.lower() in _SOURCE_EXCLUDE_DIRS or _is_git_dir(part)
               for part in rel.parts[:-1]):
            continue
        if p.name.lower() in _SOURCE_EXCLUDE_NAMES:
            continue
        if p.suffix.lower() in _SOURCE_EXCLUDE_SUFFIX:
            continue
        yield rel


def _make_source_zip() -> Path:
    """打源码 zip。

    首选 git archive：它只取被 git 跟踪的文件，是最准确的
    "应进版本库"定义，也不会把本地临时文件带进去。

    回退到工作区扫描：从 Release 下载的源码没有 .git，
    此时按 _SOURCE_EXCLUDE_* 名单过滤，保证仍能打包。
    """
    out = _RELEASE / "SmartDrawingBoard-Source.zip"
    if out.exists():
        out.unlink()

    try:
        subprocess.run(
            ["git", "archive", "--format=zip",
             f"--prefix={SOURCE_PREFIX}/",
             "-o", str(out), "HEAD"],
            cwd=str(_HERE), check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if out.exists() and out.stat().st_size > 0:
            print(f">>> Source zip (git archive): {out.name}")
            return out
    except Exception:
        pass  # 没有 .git 或 git 不可用，走回退

    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in _iter_source_files(_HERE):
            zf.write(_HERE / rel, f"{SOURCE_PREFIX}/{rel.as_posix()}")
            n += 1
    if n == 0:
        raise SystemExit("源码包为空，请检查打包逻辑")
    print(f">>> Source zip (workspace scan): {out.name} ({n} files)")
    return out


def _vendor_gpu_dir() -> Path:
    """GPU 离线包目录（torch wheel 等）。"""
    return _HERE / "vendor" / "gpu"


def _copy_gpu_wheels_into_bundle(bundle: Path) -> List[str]:
    """把 CPU 版 torch wheel 拷进 bundle 的 vendor/gpu/。

    为什么只拷 CPU 版：CUDA 版约 2.4 GiB，塞进 Release 会让
    19 MB 的 zip 变成 2.4 GB，"解压即用"这条承诺就没了。
    CPU 版约 118 MB，可接受，且它让**任何**机器都能装 GPU 依赖
    （只是 GPU 加速不生效，会静默回落 CPU）——
    这正好是 deps.py 里 TORCH_CPU 的用途。

    CUDA 版由 _make_gpu_offline_zip() 单独打成一个 zip，
    不进 Release，由需要 GPU 的用户另外下载。
    """
    src = _vendor_gpu_dir()
    if not src.is_dir():
        print(f"!!! GPU 离线包目录不存在: {src}（跳过，GPU 依赖将只能联网安装）")
        return []
    dest = bundle / "vendor" / "gpu"
    dest.mkdir(parents=True, exist_ok=True)
    copied: List[str] = []
    for whl in sorted(src.glob("*.whl")):
        # 只带 CPU 版 + 纯 Python 依赖；CUDA 版单独分发
        if "+cu" in whl.name or "+rocm" in whl.name:
            continue
        shutil.copyfile(whl, dest / whl.name)
        copied.append(whl.name)
    return copied


def _make_gpu_offline_zip() -> Optional[Path]:
    """把 CUDA 版 torch 打成单独的大 zip（不进 Release）。

    单独分发的原因：2.4 GiB。GitHub Release 放得下，
    但让它跟着 19 MB 的主包走会很荒谬 —— 99% 的用户不需要 GPU。
    """
    src = _vendor_gpu_dir()
    if not src.is_dir():
        return None
    cuda = sorted(src.glob("torch-*+cu*.whl"))
    deps_only = sorted(
        p for p in src.glob("*.whl")
        if "+cu" not in p.name and not p.name.startswith("torch-2.13.0+cpu")
    )
    if not cuda:
        print("!!! 未找到 CUDA 版 torch，跳过 GPU 离线包")
        return None
    _RELEASE.mkdir(parents=True, exist_ok=True)
    out = _RELEASE / "SmartDrawingBoard-GPU-Offline-CUDA126.zip"
    if out.exists():
        out.unlink()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in cuda + deps_only:
            z.write(p, arcname=p.name)
            print(f"    + {p.name}  ({p.stat().st_size / 1048576:.1f} MB)")
    print(f">>> GPU offline zip: {out.name} "
          f"({out.stat().st_size / 1048576:.1f} MB)")
    return out


def _make_binary_zip() -> Path:
    """把合并后的整套产物打成一个 zip。

    从"四个入口各自一个 zip"改为"一个 zip"：四个 exe 必须同目录才能
    互相拉起（spawn.bundle_root 按当前 exe 所在目录找兄弟 exe），
    分成四个 zip 的话用户解压会得到四个平级目录，Launcher 又找不到
    Page —— 这个坑已经实际踩过一次。

    root_dir 指向 bundle 目录本身，zip 内不带多余顶层目录，
    用户解压直接看到 SmartDrawingBoard-Launcher.exe 等四个文件。
    """
    src = _DIST / BUNDLE_NAME
    if not src.is_dir():
        raise SystemExit(f"缺少合并产物: {src}")

    # CPU 版 torch 随主包分发（~118 MB），断网也能装 GPU 依赖
    wheels = _copy_gpu_wheels_into_bundle(src)
    if wheels:
        total = sum((src / "vendor" / "gpu" / w).stat().st_size
                    for w in wheels)
        print(f">>> bundle/vendor/gpu: {len(wheels)} 个 wheel "
              f"({total / 1048576:.1f} MB)")

    out = _RELEASE / f"{BUNDLE_NAME}-Windows.zip"
    if out.exists():
        out.unlink()
    shutil.make_archive(str(out.with_suffix("")), "zip", root_dir=str(src))
    print(f">>> Binary zip: {out.name} "
          f"({out.stat().st_size / 1048576:.1f} MB)")
    return [out]


def _report() -> None:
    def _rows(d: Path) -> List[str]:
        out = []
        for p in sorted(d.iterdir()):
            if p.is_dir():
                out.append(f"   {p.name}/")
            else:
                out.append(f"   {p.name}  ({p.stat().st_size / 1048576:.1f} MB)")
        return out

    print("\nBuild complete.")
    print(f"  dist/ (单一分发目录 {BUNDLE_NAME}/):")
    for line in _rows(_DIST / BUNDLE_NAME):
        print(line)
    print("  release/ (zips):")
    for line in _rows(_RELEASE):
        print(line)
    print()


def _check_entry_names_match_spawn() -> None:
    """校验 APP_ENTRIES 与 core/spawn.py 的 CHILD_TARGETS 一致。

    这是硬性校验，不是提示。两处名单对不上时**直接构建失败**，
    因为对不上的后果是静默的：源码模式一切正常，exe 里那个功能
    "找不到入口"——用户下载解压后才能发现，而 CI 只会显示构建成功。
    """
    core_dir = _HERE / "core"
    if not core_dir.is_dir():
        return
    sys_path_added = str(core_dir) not in sys.path
    if sys_path_added:
        sys.path.insert(0, str(core_dir))
    try:
        import spawn as spawn_mod
        want = {spec["exe"] for spec in spawn_mod.CHILD_TARGETS.values()}
    except Exception as e:  # pragma: no cover - 只在 spawn.py 自身坏掉时
        raise SystemExit(f"无法读取 core/spawn.py 的 CHILD_TARGETS: {e}")
    finally:
        if sys_path_added and sys.path and sys.path[0] == str(core_dir):
            sys.path.pop(0)

    have = {name for name, _ in APP_ENTRIES}
    missing = want - have
    if missing:
        raise SystemExit(
            "APP_ENTRIES 缺少 core/spawn.py 声明要拉起的 exe: "
            + ", ".join(sorted(missing))
            + "\n这些入口不打包，对应功能在 exe 里会报「找不到入口」，"
              "而源码模式完全正常，CI 也不会失败。")
    print("入口名单校验通过：%d 个 exe，与 core/spawn.py 一致"
          % len(APP_ENTRIES))


def main() -> int:
    _ensure_pyinstaller()
    _clean_dist()
    _prepare_release_dir()
    _check_entry_names_match_spawn()

    for name, script in APP_ENTRIES:
        _build_entry(name, script)

    _merge_into_bundle()
    _clean_staging()

    print("\n>>> Packaging release zips (源码与成品分开)")
    _make_source_zip()
    _make_binary_zip()
    # GPU 离线包（CUDA 版，约 2.4 GiB）单独打，不进主 Release。
    # 只在 vendor/gpu 里确有 CUDA wheel 时才产出，
    # 没下载过就跳过 —— 不影响主流程。
    _make_gpu_offline_zip()

    _report()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
