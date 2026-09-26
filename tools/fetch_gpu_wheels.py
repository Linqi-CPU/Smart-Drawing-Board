"""下载 torch 离线包到 vendor/gpu/，带进度、断点续传、SHA-256 校验。

为什么不用 pip download：
  pip download 会把整个依赖闭包拉下来，且文件名/版本不受我们控制，
  难以在 vendor/ 里维持一份可复现的清单。这里精确指定要哪些文件。

为什么用 urllib 而不是 requests：
  零依赖承诺，且 urllib 支持 Range 头做断点续传。

校验策略：每个文件都从**官方索引页**取 sha256（下载前已知），
下完立刻核对。不一致就删掉重下，绝不让坏包留在 vendor/ 里 ——
deps.py 安装前也会再校验一次，双保险。

用法：
    python tools/fetch_gpu_wheels.py            # 下载 deps.py 登记的全部
    python tools/fetch_gpu_wheels.py --cpu-only # 只要 CPU 版（小）
    python tools/fetch_gpu_wheels.py --verify   # 只校验不下载

清单来自 core/deps.py 的 spec，不在这里重复维护 ——
两处各写一份必然对不上，而"对不上"的表现是
"明明下载了却说缺包"，且只在断网那次才暴露。
"""
import hashlib
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

#: 项目根：本文件在 tools/ 下，上一级即根。
ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "vendor" / "gpu"

#: 直接从 deps.py 取清单：deps.py 是安装时唯一的真相来源，
#: 这里只是它的"下载侧"。改了 spec 这边自动跟着变。
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "core"))
import deps                                     # noqa: E402

#: (url, 文件名, 官方 sha256)
#: torch 两个变体：CPU 版在 PyPI 上也有，但 cu126 只在
#: download.pytorch.org 的独立索引里 —— PyPI 上那个 torch
#: 是 CPU 构建，只写包名会静默装成 CPU 版。
#: URL 里的 + 必须编码成 %2B，否则服务器按路径分隔符解析。
PYTORCH_INDEX = {
    "cpu": "https://download.pytorch.org/whl/cpu/",
    "cu126": "https://download.pytorch.org/whl/cu126/",
}


def _pytorch_url(flavor: str, filename: str) -> str:
    """按变体拼 wheel 下载地址。

    filename 形如 torch-2.13.0+cu126-cp311-cp311-win_amd64.whl，
    地址里 '+' 要编码成 %2B，其余原样。
    """
    return (PYTORCH_INDEX[flavor]
            + filename.replace("+", "%2B"))


#: 从 spec 取 torch 变体的离线包。deps.TORCH_CPU /
#: deps.TORCH_CUDA 是 DependencySpec，包在 spec.offline[0]。
_CPU_PKG = deps.TORCH_CPU.offline[0]
_CUDA_PKG = deps.TORCH_CUDA.offline[0]

WHEELS: list = [
    (_pytorch_url("cpu", _CPU_PKG.filename),
     _CPU_PKG.filename, _CPU_PKG.sha256),
    (_pytorch_url("cu126", _CUDA_PKG.filename),
     _CUDA_PKG.filename, _CUDA_PKG.sha256),
]

#: 纯 Python 依赖：只从 spec 的文件名解析 name/version，
#: URL 与 sha256 仍由 PyPI 现场查。版本不在本文件硬编码 ——
#: 两处各写一份必然对不上，而"对不上"的表现是
#: 明明下载了却说缺包，且只在断网那次才暴露。
def _parse_name_ver(filename: str) -> tuple:
    """filelock-3.32.4-py3-none-any.whl -> (filelock, 3.32.4)

    wheel 文件名固定是 name-ver-<python tag>-<abi>-<platform>.whl，
    所以结尾三段是 tag，中间全是版本（版本号本身可能带 '-'，
    如 1.0.0-beta.1，故用切片而不是取单段）。
    """
    parts = filename[:-len(".whl")].split("-")
    if len(parts) >= 5:
        return parts[0], "-".join(parts[1:-3])
    if len(parts) >= 2:
        return parts[0], parts[1]
    return parts[0], ""


PY_DEP_NAMES = [_parse_name_ver(p.filename) for p in deps._TORCH_PY_DEPS]


def pypi_wheel_url(name: str, ver: str) -> tuple:
    """从 PyPI 取指定版本的 win/cp311（或纯 py3）wheel URL 与 sha256。"""
    import json
    with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/json",
                                timeout=60) as r:
        data = json.loads(r.read().decode())
    for f in data["releases"].get(ver, []):
        fn = f["filename"]
        if ("cp311" in fn and "win_amd64" in fn) or (
                "py3-none-any" in fn and "win" not in fn):
            digests = f.get("digests", {})
            return f["url"], fn, digests.get("sha256", "")
    raise SystemExit(f"找不到 {name}=={ver} 的合适 wheel")


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(1 << 20)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def download(url: str, dest: str, expect_sha: str) -> str:
    """下载单个文件，带断点续传与进度。返回最终状态说明。"""
    tmp = dest + ".part"
    have = os.path.getsize(tmp) if os.path.exists(tmp) else 0
    if os.path.exists(dest):
        got = sha256_file(dest)
        if expect_sha and got.lower() != expect_sha.lower():
            print(f"  [重下] 已存在但摘要不符: {os.path.basename(dest)}")
            os.remove(dest)
        else:
            return f"已存在且校验通过 ({got[:16]}…)"

    req = urllib.request.Request(url)
    if have:
        req.add_header("Range", f"bytes={have}-")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            total = int(r.headers.get("Content-Length", 0))
            mode = "ab" if (have and r.status == 206) else "wb"
            if mode == "wb":
                have = 0
            grand = have + total
            done = have
            t0 = time.time()
            last = 0.0
            with open(tmp, mode) as fh:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    now = time.time()
                    if now - last >= 2.0:            # 每 2 秒报一次
                        last = now
                        pct = done / grand * 100 if grand else 0
                        spd = (done - have) / max(0.1, now - t0) / 2 ** 20
                        eta = (grand - done) / 2 ** 20 / max(0.01, spd)
                        print(f"  {pct:5.1f}%  {done/2**30:.2f}/"
                              f"{grand/2**30:.2f} GiB  {spd:6.1f} MiB/s  "
                              f"剩余 {eta:5.0f}s", flush=True)
    except urllib.error.HTTPError as e:
        if e.code == 416 and os.path.exists(tmp):
            pass                              # Range 越界 = 已下完
        else:
            raise

    got = sha256_file(tmp)
    if expect_sha and got.lower() != expect_sha.lower():
        os.remove(tmp)
        return (f"失败: 摘要不符\n    期望 {expect_sha}\n    实际 {got}")
    shutil.move(tmp, dest)
    return f"完成 {os.path.getsize(dest)/2**20:.1f} MiB  sha256={got[:16]}…"


def _full_list() -> list:
    """组装要处理的 (url, 文件名, sha256)；PyPI 依赖现场查。"""
    out = list(WHEELS)
    for name, ver in PY_DEP_NAMES:
        try:
            url, fn, sha = pypi_wheel_url(name, ver)
            out.append((url, fn, sha))
        except Exception as e:
            print(f"  [跳过] {name}=={ver} 查不到 wheel: {e}", flush=True)
    return out


def _verify(files: list) -> int:
    """只校验已下载文件，不联网。返回不符项数。"""
    bad = 0
    for _, fn, sha in files:
        p = os.path.join(DEST, fn)
        if not os.path.exists(p):
            print(f"  [缺失] {fn}")
            continue
        got = sha256_file(p)
        if sha and got.lower() != sha.lower():
            print(f"  [不符] {fn}\n    期望 {sha}\n    实际 {got}")
            bad += 1
        else:
            print(f"  [OK] {fn}  ({os.path.getsize(p)/2**20:.1f} MiB)")
    return bad


def main():
    argv = sys.argv[1:]
    only_verify = "--verify" in argv
    cpu_only = "--cpu-only" in argv

    if not only_verify:
        os.makedirs(DEST, exist_ok=True)

    files = _full_list()
    if cpu_only:
        # 只要 CPU 版 + 纯 Python 依赖，跳过 cu126（省 2.4 GiB 流量）
        files = [f for f in files if "cu126" not in f[1]]

    if only_verify:
        print(f"=== 校验 {len(files)} 个文件（不下载）===")
        bad = _verify(files)
        print("全部通过" if bad == 0 else f"{bad} 项不符")
        return 0 if bad == 0 else 1

    total = len(files)
    for i, (url, fn, sha) in enumerate(files, start=1):
        print(f"[{i}/{total}] {fn}", flush=True)
        try:
            print("  " + download(url, os.path.join(DEST, fn), sha),
                  flush=True)
        except Exception as e:
            print(f"  下载失败: {type(e).__name__}: {e}", flush=True)

    print("\n=== vendor/gpu 目录 ===", flush=True)
    for f in sorted(os.listdir(DEST)):
        p = os.path.join(DEST, f)
        print(f"  {f}  {os.path.getsize(p)/2**20:8.1f} MiB")


if __name__ == "__main__":
    main()
