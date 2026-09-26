#!/usr/bin/env python3
"""可选依赖检测 / 安装 / 离线兜底 一体化脚本。

用法
----
    python tools/check_deps.py              # 只检测并打印状态
    python tools/check_deps.py --install    # 检测 + 缺失则安装
    python tools/check_deps.py --verify     # 只校验 vendor/ 下离线包的哈希
    python tools/check_deps.py --json       # 机器可读输出（供 CI 用）

逻辑（与 core/deps.py 的 ensure_dependency 对应）
------------------------------------------------
    1. 探测 ──已就绪──> 跳过，不下载、不安装
    2. 探测缺失 ──> 尝试 pip 联网安装
    3. 联网失败 ──> 用 vendor/ 里的 .whl 离线安装（--no-index）
                     安装前逐包 SHA-256 校验，不过则拒绝
    4. 仍失败 ──> 给出人工处理建议，以退出码 1 结束

关于 decimal
------------
本项目的"高精度后端"是标准库 ``decimal``，任何正常
CPython 都已内置，因此本脚本对它必然走"跳过"分支。
脚本保留完整的三级架构，是为了在将来把高精度后端
换成 mpmath / gmpy2 / sympy 等真·第三方库时无需重写流程。

离线包放置
----------
把 wheel 放到 <项目根>/vendor/ 下，并在 core/deps.py 的
DependencySpec.offline 里登记 (文件名, sha256)。
sha256 可用 ``sha256sum <file>`` 或
``python tools/check_deps.py --hash <file>`` 取得。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# 让脚本在任意 CWD 下都能 import 项目模块
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import deps  # noqa: E402


def _print(msg: str) -> None:
    """日志回调：与 core/deps.ensure_dependency 的 log 契约一致（单参数）。"""
    print("    " + msg)


def cmd_status() -> int:
    """只检测并打印状态，不做任何安装。"""
    print("=" * 62)
    print("可选依赖检测")
    print("=" * 62)
    print(f"项目根:  {_ROOT}")
    print(f"离线目录: {deps.vendor_dir()}")
    print()

    vendor_exists = os.path.isdir(deps.vendor_dir())
    if not vendor_exists:
        _print(f"离线目录不存在: {deps.vendor_dir()}")

    all_ok = True
    for spec in deps.KNOWN_DEPENDENCIES:
        ok = spec.is_available()
        all_ok = all_ok and ok
        marker = "OK  " if ok else "MISS"
        desc = f" — {spec.description}" if spec.description else ""
        print(f"  [{marker}] {spec.name}{desc}")

        if not ok:
            if spec.packages:
                _print("可安装: pip install " + " ".join(spec.packages))
            for pkg in spec.offline:
                _print(f"离线包: {pkg.filename} (sha256 {pkg.sha256[:12]}...)")

    print()
    print("=" * 62)
    if all_ok:
        print("结果: 全部就绪")
        return 0
    print("结果: 有缺失。带 --install 可尝试安装，或放离线包到 vendor/")
    return 1


def cmd_verify() -> int:
    """只校验 vendor/ 下已登记的离线包，不安装。"""
    print("校验 vendor/ 离线包完整性 (SHA-256)")
    print("-" * 62)
    rc = 0
    any_pkg = False
    for spec in deps.KNOWN_DEPENDENCIES:
        for pkg in spec.offline:
            any_pkg = True
            ok, detail = deps.verify_offline_package(pkg)
            if ok:
                print(f"  [OK]   {pkg.filename}")
                print(f"         {detail}")
            else:
                rc = 1
                print(f"  [FAIL] {pkg.filename}")
                for line in str(detail).splitlines():
                    print(f"         {line.strip()}")
    if not any_pkg:
        print("  （没有任何依赖配置了离线包）")
    print("-" * 62)
    return rc


def cmd_hash(path: str) -> int:
    """打印一个文件的 SHA-256，方便登记到 core/deps.py。"""
    if not os.path.isfile(path):
        print(f"文件不存在: {path}", file=sys.stderr)
        return 1
    print(deps.sha256_file(path))
    return 0


def cmd_install() -> int:
    """检测 + 安装缺失项。"""
    print("=" * 62)
    print("可选依赖检测与安装")
    print("=" * 62)
    failures = []
    for spec in deps.KNOWN_DEPENDENCIES:
        print(f"\n▼ {spec.name}"
              + (f" ({spec.description})" if spec.description else ""))
        ok = deps.ensure_dependency(spec, log=_print)
        if not ok:
            failures.append(spec.name)

    print()
    print("=" * 62)
    if failures:
        print("失败: " + ", ".join(failures))
        print()
        print("人工处理建议:")
        print("  1. 手动联网安装:  pip install " +
              " ".join(p for s in deps.KNOWN_DEPENDENCIES for p in s.packages))
        print(f"  2. 准备离线包:  把 .whl 放进 {deps.vendor_dir()}")
        print("     再用 --hash <file> 取摘要，登记进 core/deps.py 的 offline 字段")
        return 1
    print("结果: 全部就绪")
    return 0


def cmd_json() -> int:
    """机器可读状态，供 CI 断言。"""
    payload = {
        "root": _ROOT,
        "vendor_dir": deps.vendor_dir(),
        "dependencies": [
            {
                "name": s.name,
                "available": s.is_available(),
                "packages": list(s.packages),
                "offline": [
                    {"filename": p.filename, "sha256": p.sha256}
                    for p in s.offline
                ],
            }
            for s in deps.KNOWN_DEPENDENCIES
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if all(d["available"] for d in payload["dependencies"]) else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="可选依赖检测 / 安装 / 离线兜底",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--install", action="store_true",
                   help="检测并对缺失项执行安装流程")
    g.add_argument("--verify", action="store_true",
                   help="只校验 offline 包的 SHA-256，不安装")
    g.add_argument("--hash", metavar="FILE",
                   help="打印某文件的 SHA-256 后退出")
    g.add_argument("--json", action="store_true",
                   help="输出机器可读状态后退出")

    args = ap.parse_args(argv)

    if args.hash:
        return cmd_hash(args.hash)
    if args.verify:
        return cmd_verify()
    if args.json:
        return cmd_json()
    if args.install:
        return cmd_install()
    return cmd_status()


if __name__ == "__main__":
    sys.exit(main())
