"""本地打包脚本：把 Smart Drawing Board 打包为 Windows exe。

使用方式：
    python build_exe.py

产出：
    dist/SmartDrawingBoard-Launcher.exe
    dist/SmartDrawingBoard-Main.exe
    dist/SmartDrawingBoard-Page.exe
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent


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
    dist = _HERE / "dist"
    if dist.exists():
        shutil.rmtree(dist)
    dist.mkdir(parents=True, exist_ok=True)


def _common_options() -> list[str]:
    return [
        "--noconfirm",
        "--clean",
        "--onedir",
        "--windowed",
        f"--add-data={_HERE / 'functions'};functions",
        f"--hidden-import=core",
        f"--hidden-import=core.server",
        f"--hidden-import=core.session",
        f"--hidden-import=edit",
        f"--hidden-import=edit.page",
        "--exclude-module=matplotlib",
        "--exclude-module=numpy",
        "--exclude-module=pandas",
        "--exclude-module=PIL",
        "--exclude-module=tkinter.test",
        "--distpath",
        str(_HERE / "dist"),
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
    import subprocess

    proc = subprocess.run(args, cwd=str(_HERE))
    if proc.returncode != 0:
        raise SystemExit(f"打包失败：{name}")


def main() -> int:
    _ensure_pyinstaller()
    _clean_dist()

    _build_entry("SmartDrawingBoard-Launcher", _HERE / "launcher.py")
    _build_entry("SmartDrawingBoard-Main", _HERE / "main.py")
    _build_entry("SmartDrawingBoard-Page", _HERE / "edit" / "__main__.py")

    print("\nBuild complete. Artifacts under dist/:")
    for p in sorted((_HERE / "dist").iterdir()):
        print(" - " + p.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
