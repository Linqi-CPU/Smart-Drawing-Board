# 架构说明

本文档描述 2.2.0 引入的**内核 / UI 分离架构**：计算跑在独立的内核进程里，每个功能界面是独立的页面进程，主 UI 只负责"激活"。

---

## 1. 为什么这样拆

最初所有东西都在 `main.py` 里：算法、UI、拟合、渲染。带来的问题：

- **算法无法单独测**：要测一个拟合函数，得先创建一个 Tk 窗口
- **功能互相干扰**：新功能往主界面加控件，越加越乱，改一处容易碰坏别的
- **一个卡死全盘死**：某个耗时的计算在主线程跑，整个界面无响应
- **主界面关闭 = 一切归零**：没有"关掉主界面但保留正在跑的活"的可能

所以按职责切成三层，各自独立部署、独立测试、独立存活。

---

## 2. 目录结构

```
新建文件夹/
├── launcher.py         启动器：生命周期最长的进程，整套系统的锚点
├── main.py             主 UI：传统绘图板（启动器的一个选项）
├── kernel_bridge.py    内核托管与页面启动器（LAUNCHERS 注册表）
│
├── core/               计算内核 —— 不 import tkinter，可独立运行
│   ├── __init__.py     路径自举，保证 `from core import xxx` 任何目录下可用
│   ├── session.py      页面独立编号 + 注册表 + 产物目录规则
│   ├── server.py       HTTP 服务，内核唯一对外接口（ROUTES 表分发）
│   ├── client.py       UI 侧轻量客户端（只用标准库 urllib）
│   ├── band_fit.py     分段包络估计算法
│   ├── fitting.py      多项式 / 多变量最小二乘
│   ├── data_import.py  Excel / CSV / TSV / TXT 解析
│   ├── graph_engine.py 函数采样、自适应缩放、折线长度
│   └── custom_loader.py 自定义函数安全加载（AST 白名单）
│
├── edit/               独立功能页面 —— 只通过 core.client 通信，不认识 main.py
│   ├── __init__.py
│   ├── __main__.py     页面进程入口（detached 启动）
│   └── page.py         BandPage：分段包络估计趋势与离散分析页面
│
└── tests/              346 项测试
```

### 进程层次

```
launcher.py            用户控制，生命周期最长（整套系统的锚点）
  ├─ core.server       内核，由启动器托管
  ├─ main.py           主 UI（传统绘图板，与功能页面平级）
  └─ edit/...          任意多个功能页面
```

启动器拉起内核与页面，所以只要它活着，这些子进程就活着；
关掉启动器会收尾所有功能页面，但**保留内核**（共享资产，下次复用）。

> 为什么需要启动器：`DETACHED_PROCESS` 只让进程不继承父控制台，
> **不会**让它脱离 Job Object。实测 `CREATE_BREAKAWAY_FROM_JOB` 也未能跳出，
> 启动日志持续显示 `job(member=1)` —— 意味着父 session 一结束，
> 内核仍会被连带杀掉。启动器是用户双击的进程，不受任何宿主 session 影响，
> 因此由它当锚点，语义才真正成立。

### 依赖方向（不可逆）

```
launcher.py ──> kernel_bridge ──> core.client ──HTTP──> core.server ──> core.*
                                   ↑
main.py ───────────────────────────┘
edit/page.py ──────────────────────┘
```

规则：

- `core/` **不知道** UI 的存在（不 import tkinter）
- `edit/page.py` **不知道** `main.py` 的存在
- 跨进程只能走 `core.client` → `core.server` 的 HTTP

这条依赖方向是架构的核心。破坏了它（比如在 `core/` 里 import tkinter），测试就得跟着建窗口，前面的问题会全部回来。

---

## 3. 三个进程角色

### 3.1 内核（core.server）

- 启动：`python -m core.server`，或用 `kernel_bridge.start_kernel()`
- 职责：算数、按 session 隔离状态、渲染落盘、生成报告、回收死亡页面
- **默认端口 8765**，由 `HERMES_KERNEL_PORT` 控制
- 用 `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` 启动，**主 UI 退出后它继续跑**

### 3.2 页面（edit/*）

- 启动：`python -m edit --page band`，或用 `kernel_bridge.launch_page()`
- 职责：纯粹的界面 + 参数收集，自己不实现任何算法
- 同样 detached 启动，**主 UI 被 kill 后依然存活**
- 每个页面进程有自己的 `session_id`（形如 `p_3feed580`）

### 3.3 主 UI（main.py）

- 职责被刻意限制为"**激活**"：拉起内核、拉起页面、显示哪些页面在线
- **不承载任何具体功能界面**
- `WM_DELETE_WINDOW` 只关自己，不断子进程

---

## 4. 独立编号与状态隔离

页面启动时生成 `p_` + uuid4 前 8 位作为编号，向内核 `POST /api/register`。

内核用 `SessionRegistry` 按编号隔离每个页面的：

| 状态 | 说明 |
|------|------|
| `points` | 点集 |
| `rows` / `variables` | Excel 多变量行与表头 |
| `last_result` | 最近一次计算结果 |
| `images` / `notes` | 生成的图片路径、回传消息 |
| `pid` / `closing` | 进程 PID 与关闭标记（用于回收） |

编号清洗规则（`core/session.py: _normalize_session_id`）：

- 只保留 `[A-Za-z0-9_-]`，其它字符一律剔除
- 空串 / 全非法字符 / 超 64 字符 → **报错**，不静默截断（截断会让两个不同长编号变成同一个键，导致状态互相串扰）

---

## 5. 新增一个 edit 页面

这是架构的核心承诺：**后续加功能不用反复改主 UI 界面**。只需要三步。

### 第 1 步：写页面本体

在 `edit/` 下新建 `page_xxx.py`，实现一个类：

```python
from core.client import KernelClient

class XxxPage:
    def __init__(self, root, session_id: str, client: KernelClient):
        ...
```

约定：

- 有自己的 `session_id`，全程只操作自己的编号
- 算法一律 `client.xxx(...)` 调内核，不自己实现
- 关闭时实现 `dispose()`：停定时器、取消挂起的 `after`、再销毁窗口
  （参考 `edit/page.py: BandPage.dispose` 与 `main.py: DrawingApp.dispose`）

### 第 2 步：登记启动器

`kernel_bridge.py` 的 `LAUNCHERS` 加一行：

```python
LAUNCHERS = {
    "band": "分段包络估计走势与离散分析",
    "xxx":  "你的功能说明",          # ← 加这行
}
```

### 第 3 步：加分发

`edit/__main__.py` 里把 `--page xxx` 映射到你的类。

**主 UI 的「页面」菜单会自动列出 `LAUNCHERS` 里的所有项**，不需要改 `main.py` 的任何界面代码。

---

## 6. 生命周期与资源回收

页面可能被正常关闭，也可能被任务管理器强杀。三条路径都要保证"关掉的那个被回收，其他页面不受影响"。

### 正常关闭

`BandPage.on_close()` 的顺序：

1. `mark_closing(session_id)` —— 只给自己打关闭标记，给内核留宽限期
2. `cleanup(session_id)` —— 内核删掉**自己**的图片与报告，注销自己的状态
3. 本地兜底删除 —— 内核不可达时，自己按 session_id 匹配文件名删掉产物
4. 停定时器 → `root.destroy()` —— **不用 `os._exit()`**，它会跳过 atexit 和缓冲区 flush

### 被强杀

页面上报了自己的 PID，内核 `_pids_alive()` 用 `os.kill(pid, 0)`（POSIX）或 `OpenProcess`（Windows）探测：

- PID 已死 → 回收其状态与文件
- PID 探测失败 → **保守认为活着**（宁可晚回收，不可错杀）

### 三种死亡判定（`SessionRegistry.reap_dead`）

| 判定 | 场景 |
|------|------|
| PID 已死 | 被任务管理器杀掉，没机会调 cleanup |
| `closing` + 宽限期已过（5s） | 正常关闭流程的兜底 |
| 无 PID + 空闲超时（6h） | 老版本页面未上报 PID |

`_reaper_loop` 每 30 秒跑一次（daemon 线程），只回收已死的，**活着一律不动**。

### 重启后的孤儿文件

`reap_dead` 只能回收"注册表里已知的页面"。内核重启后注册表是空的，旧会话文件就成了谁都不认识的真孤儿。所以启动时会跑一次 `_sweep_orphan_files()`：

- 只删 `band_p_*.png` / `report_p_*.txt` 这种**能反推出 sid** 的命名
- 文件名含任一活着的 sid 就跳过，绝不误删

---

## 7. 产物目录：按类型分开放

```
D:\.hermes_kernel\
├── pictures\   ← 图表（.png/.jpg/.svg/.webp/...）
└── texts\      ← 文本（.txt/.csv/.log/.json/...）
```

- 默认在 **D 盘**，不占系统盘
- 归属由单一入口 `core.session.dir_for(path)` 按扩展名决定，避免多处规则漂移
- 根目录可用 `HERMES_KERNEL_OUTPUT_ROOT` 整体迁移，两个子目录自动派生
- 测试 `test_defaults_not_on_c_drive` 锁定"默认不许在 C 盘"

---

## 8. HTTP 接口一览

全部 POST `/api/<name>`，body 为 JSON，返回 `{"ok": true, ...}` 或 `{"ok": false, "error": "..."}`。

### 会话

| 接口 | 说明 |
|------|------|
| `/api/ping` | 探活 |
| `/api/register` | 登记页面，可顺带 `pid`；返回 `session_id` |
| `/api/unregister` | 注销（只清内存，不删文件） |
| `/api/sessions` | 列出所有在线页面 |

### 数据与计算

| 接口 | 说明 |
|------|------|
| `/api/save_points` | 存点集 |
| `/api/get_points` | 取点集 |
| `/api/import_table` | 导入 Excel/CSV，解析表头数据名 |
| `/api/band_fit` | 分段包络估计 |
| `/api/poly_fit` | 普通多项式最小二乘 |
| `/api/band_series` | 上下界包络序列 |
| `/api/dev_series` | 离散宽度序列 |
| `/api/render_band` | 渲染走势图 + 离散宽度图，落盘 pictures |
| `/api/report` | 生成文本报告，落盘 texts |
| `/api/note` | 页面回传消息给 UI |

### 生命周期

| 接口 | 说明 |
|------|------|
| `/api/report_pid` | 上报 PID，便于清理时精确终止 |
| `/api/mark_closing` | 标记关闭中（宽限期） |
| `/api/cleanup` | 删本页面产物 + 注销，只影响调用方编号 |
| `/api/reap` | 立即巡检一次，回收已死页面 |

---

## 9. 参数校验：报错而不是夹紧

内核对越界参数**明确报错**，不静默夹紧：

```python
>>> band_fit(sid, degree=99)
KernelClientError: degree 必须在 0 ~ 20 之间，收到 99
```

理由：夹紧会让用户以为设置生效了（填 99 实际按 10 算却毫无提示）。页面侧 `_params()` 同样不做夹紧，把校验完全交给内核，错误直接显示给用户。

---

## 10. 环境变量

| 变量 | 默认值 | 作用 |
|------|--------|------|
| `HERMES_KERNEL_PORT` | `8765` | 内核端口，client/server/bridge 三方同源 |
| `HERMES_KERNEL_HOST` | `127.0.0.1` | 内核 host |
| `HERMES_KERNEL_OUTPUT_ROOT` | `D:\.hermes_kernel` | 产物根目录 |
| `HERMES_TEST_KERNEL_PORT` | `8765` | 测试连的内核端口 |
| `DRAWING_FUNCTIONS_DIR` | `./functions` | 自定义函数目录 |

端口统一由环境变量控制（不在代码里写死），所以同一台机器可以同时跑多个内核（测试用 8804、正式用 8765）互不冲突。

---

## 11. 测试

```bash
python -m unittest discover -s tests        # 309 项
```

| 文件 | 内容 |
|------|------|
| `test_band_fit.py` | 包络估计算法 |
| `test_fitting.py` | 多项式 / 多变量拟合 |
| `test_data_import.py` | Excel/CSV 解析 |
| `test_graph_engine.py` | 采样 / 缩放 / 折线长度 |
| `test_gui_smoke.py` | 主 UI 冒烟（真实建窗口驱动） |
| `test_fit_gui.py` | 拟合界面 |
| `test_excel_gui.py` | Excel 导入界面 |
| `test_kernel_integration.py` | 内核 HTTP 端到端 |
| `test_lifecycle.py` | 关闭回收 + 产物目录分类 |

集成测试需要内核在跑：

```bash
python -m core.server &                                    # 默认 8765
python -m unittest tests.test_kernel_integration tests.test_lifecycle
```

### GUI 测试的一个坑

`tearDownClass` 必须调 `app.dispose()`，**不能直接 `root.destroy()`**。

直接 destroy 会跳过定时器取消，Tk 对已销毁的解释器继续派发回调，刷
`invalid command name "..._refresh_kernel_status"` 噪音
（见 `main.py: DrawingApp.dispose` 的注释，以及两个回归测试
`test_90_on_close_destroys` / `test_91_dispose_stops_status_timer`）。

---

## 12. 已知限制

- **`dev_series` 在区间退化时返回空数组**：`x_from >= x_to` 时 `band_series` 直接早退，返回空。这是正确的边界行为，调用方需自行处理空结果。
- **最后一个 Tk 根窗口 destroy 后 Tcl 解释器未必立刻消失**：`winfo_exists()` 仍可能返回 1。断言关闭状态时，用自己维护的标志（`_closing_down`）+ Tk 的 `after info` 队列，**不要依赖 `winfo_*` 报错**。
- **产物清理依赖 session_id 命名**：`_sweep_orphan_files` 只认 `band_p_*` / `report_p_*`。新增产物类型时，确保文件名带 `_p_` + sid，否则重启后不会被自动清理。
