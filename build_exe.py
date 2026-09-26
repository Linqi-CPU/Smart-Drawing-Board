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
from typing import List

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

#: 三个可执行入口：(产物目录名, 入口脚本)
APP_ENTRIES = [
    ("SmartDrawingBoard-Launcher", _HERE / "launcher.py"),
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
    if _DIST.exists():
        shutil.rmtree(_DIST)
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
    """自动扫描 core/ 下所有模块，生成 --hidden-import 列表。

    为什么不用手写列表：早前只列了 core / core.server / core.session /
    edit / edit.page 五个，新增 band_advanced / gpu_backend / deps 时
    忘了同步，结果本地 `python -m` 一切正常，exe 里 import 直接崩。

    PyInstaller 的静态分析对**运行期条件 import** 漏检率很高
    （gpu_backend 的 try/except 回落、deps 的按需 import 都在此列），
    所以 core/ 下的模块一律显式列出。扫描而非手列，加模块不再漏。

    子包（core/xxx/ 目录）会递归展开为 core.xxx.yyy。
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
                    mod = f"{prefix}.{entry.name}"
                    out.append(f"--hidden-import={mod}")
                    _walk(entry, mod)
                continue
            if entry.suffix == ".py":
                out.append(f"--hidden-import={prefix}.{entry.stem}")

    _walk(core_dir, "core")
    return out


def _common_options() -> list[str]:
    return [
        "--noconfirm",
        "--clean",
        "--onedir",
        "--windowed",
        f"--add-data={_HERE / 'functions'};functions",
        *_core_hidden_imports(),
        "--hidden-import=edit",
        "--hidden-import=edit.page",
        "--exclude-module=matplotlib",
        "--exclude-module=numpy",
        "--exclude-module=pandas",
        "--exclude-module=PIL",
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

    proc = subprocess.run(args, cwd=str(_HERE))
    if proc.returncode != 0:
        raise SystemExit(f"打包失败：{name}")


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


def _make_binary_zips() -> List[Path]:
    """把三个 exe 目录各自打成 zip。

    root_dir 指向目录本身，zip 内不带多余顶层目录，
    用户解压直接看到 SmartDrawingBoard-Launcher.exe。
    """
    outs: List[Path] = []
    for name, _script in APP_ENTRIES:
        src = _DIST / name
        if not src.is_dir():
            raise SystemExit(f"缺少打包产物: {src}")
        out = _RELEASE / f"{name}.zip"
        if out.exists():
            out.unlink()
        shutil.make_archive(str(out.with_suffix("")), "zip", root_dir=str(src))
        outs.append(out)
        print(f">>> Binary zip: {out.name}")
    return outs


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
    print("  dist/ (exe dirs):")
    for line in _rows(_DIST):
        print(line)
    print("  release/ (zips):")
    for line in _rows(_RELEASE):
        print(line)
    print()


def main() -> int:
    _ensure_pyinstaller()
    _clean_dist()
    _prepare_release_dir()

    for name, script in APP_ENTRIES:
        _build_entry(name, script)

    print("\n>>> Packaging release zips (源码与成品分开)")
    _make_source_zip()
    _make_binary_zips()

    _report()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
