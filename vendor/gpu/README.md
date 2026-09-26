# vendor/gpu —— GPU 加速离线包

**这里面的 `.whl` 不进 git。** 走 Release Assets，需要的人自己下载。

## 为什么不入库

一条硬约束 + 一条现实约束：

| | |
|---|---|
| GitHub 单文件上限 | 100 MB |
| `torch-2.13.0+cu126` 实际大小 | **2.59 GiB** |
| Git LFS 免费档 | 1 GB 存储 / 1 GB 月流量 |

2.4 GiB 塞不进去；LFS 配额也不够，而且会让每个 clone 的人白付 2.4 GB 流量——99% 的用户不需要 GPU。

## 怎么补齐

```bash
python tools/fetch_gpu_wheels.py            # 全部（含 2.4 GiB 的 cu126）
python tools/fetch_gpu_wheels.py --cpu-only # 只要 CPU 版 + 依赖，约 116 MB
python tools/fetch_gpu_wheels.py --verify   # 只校验，不下载
```

脚本从 `core/deps.py` 的 spec 读清单（文件名、版本、sha256），
不在本目录重复维护——两处各写一份必然对不上，而"对不上"的表现是
"明明下载了却说缺包"，且只在断网那次才暴露。

## 下载的是什么

| 文件 | 大小 | 用途 |
|---|---|---|
| `torch-2.13.0+cpu-…` | 116 MiB | **随 Release 分发**。任何机器都能装上依赖；GPU 加速不生效，会静默回落 CPU |
| `torch-2.13.0+cu126-…` | 2.59 GiB | 单独 Release zip。真正需要 GPU 的那个，仅 NVIDIA + CUDA 12.6 |
| `filelock` / `fsspec` / `jinja2` / `networkx` / `sympy` / `typing_extensions` | ~8 MiB | torch 的纯 Python 依赖，版本锁定 |

**为什么 CPU 版也要**：`deps.py` 的探测是"torch 且 CUDA 可用"。
只装 CPU 版时 `is_available()` 仍为 False，用户会得到明确反馈
"GPU 不可用，已改用 CPU"，而不是静默跑完还显示"GPU 加速"。

## 校验

每个 wheel 都登记官方 sha256（从 PyPI / download.pytorch.org
官方索引页取，**不是估算值**）。安装前 `deps.py` 逐一核对，
不符即拒绝安装；脚本下完也立刻核对，不符删掉重下。

## 分发产物

`build_exe.py` 打出两个 zip：

- `release/SmartDrawingBoard-Windows.zip` —— 主包，内含 `vendor/gpu/` 的 CPU 版
- `release/SmartDrawingBoard-GPU-Offline-CUDA126.zip` —— CUDA 版，单独下

两者同样**不进 git**（见根目录 `.gitignore`）。
