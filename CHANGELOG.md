# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.2.0] - 2026-09-26

### 新增

- **算法对比模式**：支持在同一张图上同时展示多种估计算法的结果，每种算法使用不同颜色，便于直观比较。
  - 经典版（Classic）：原分段均值切分 + 上下界拟合 + 系数调和
  - AI 改进版（Improved）：中位数切分 + 中心线直接拟合全量点，对离群点更稳健
- **改进版估计算法**：`core/band_fit.py` 新增 `band_fit_improved()`，相比经典版：
  - 分组改用分段中位数，降低异常值影响
  - 中心线不再依赖上下界反推，而是直接对全量点拟合
- **后端接口**：`/api/band_fit_improved` 支持改进版算法调用

### 修复

- **启动器关闭时页面回收**：`launcher.py` 在 `taskkill` 后增加等待与内核 `reap` 触发，确保页面进程真正退出并从注册表清除，避免僵尸进程。
- **测试补强**：`tests/test_launcher.py` 的 `test_on_close_reaps_pages` 增加手动 `reap` 调用，消除因 30 秒后台巡检延迟导致的测试竞争。

### 变更

- `edit/page.py`：UI 参数区支持算法选择与对比模式切换，绘制逻辑统一支持多结果叠加。
- `core/client.py`：新增 `band_fit_improved()` 客户端方法。

---

## [0.1.0] - 2026-09-24

### 新增

- **启动器**：以"生命周期最长的进程"作为整套系统的锚点，主 UI 与各功能页面平级。
  - `launcher.py`：新的统一入口。启动时确保内核在跑，然后列出所有可选功能（主 UI + 各 edit 页面），点哪个开哪个。
  - 启动器是用户双击的那个进程，生命周期由用户控制，**不随 agent/宿主 session 结束而消失**
  - 状态栏实时显示内核是否运行、在线页面数与编号
  - 关闭启动器时收尾所有功能页面，但**保留内核**（内核是共享资产，下次启动仍可复用）
  - 「关闭全部功能页面」按钮：只杀页面，不动内核
- **`tests/test_launcher.py`**（14 项）：锁定功能列表自动生成（含"新页面加进 `LAUNCHERS` 就自动出现"）、拉起功能、关闭时保留内核、关闭后定时器必须停止。

### 修复

- **内核仍会被 Job Object 连带杀掉**：实测 `CREATE_BREAKAWAY_FROM_JOB` 未能让内核跳出 Job（启动日志持续显示 `job(member=1)`），因此"内核比主 UI 活得久"在这个宿主环境下并不成立。改为由启动器承担锚点角色——它创建内核与页面，自己活着这些子进程就活着。`core/server.py` 的 `_install_death_logger()` 会在启动时记录 Job 归属，便于事后判断。
- **内核死亡无证据可查**：新增 `D:\.hermes_kernel\.kernel_death.log`，记录 START（pid/ppid/job/argv）、CRASH（未捕获异常 traceback）、EXIT（干净退出）。已知平台限制：`taskkill /F`（SIGKILL）抓不到，Windows 上 Python 也收不到 SIGTERM，这两类只有 START 基线可查——由 `tests/test_death_log.py` 把这个事实固定下来。
- **启动器/主 UI 关闭时可能留孤儿子进程**：terminate 后补 wait，避免 ResourceWarning 与僵尸进程。

---

## [2.3.0] - 2026-09-24

架构升级：计算内核与服务化，功能界面改为独立页面进程，主 UI 只做"激活"。

### 新增

- **`core/` 计算内核包**：算法与 UI 彻底分离，`core/` 不 import tkinter，可独立运行与测试。
  - `session.py`：页面独立编号 + `SessionRegistry` 注册表 + 产物目录规则
  - `server.py`：HTTP 服务（`ROUTES` 表 18 个 `/api/*` 接口），内核唯一对外接口
  - `client.py`：UI 侧轻量客户端，仅用标准库 urllib
  - `band_fit.py`：分段包络估计算法（分段均值 → 上/下界拟合 → 系数合并 → 偏移常数 C）
- **`edit/` 独立页面进程**：`BandPage` 分段包络估计趋势与离散分析页面。
  - 独立进程、独立 `session_id`、独立窗口，**主 UI 被 kill 后依然存活**
  - 通过 `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` 启动，与主 UI 解耦
  - 新增页面只需：`edit/` 加模块 + `kernel_bridge.LAUNCHERS` 加一行 + `edit/__main__.py` 加分发，**主 UI 菜单自动列出，无需改主界面代码**
- **生命周期与资源回收**：
  - 关闭页面时`mark_closing` → `cleanup` → 本地兜底删除 → 停定时器 → `destroy()`，**只清自己的状态与文件，不影响其他页面**
  - 上报 PID，内核按 `os.kill(pid,0)` / `OpenProcess` 判定存活；探测失败保守认为活着（宁可晚回收不可错杀）
  - 三种死亡判定（PID 已死 / closing 宽限 / 无 PID 空闲超时）+ 30 秒后台巡检
  - 启动时 `_sweep_orphan_files()` 扫掉重启后无人认领的孤儿产物
- **产物按类型分目录**：`D:\.hermes_kernel\pictures`（图表）与 `\texts`（文本），默认放 D 盘不占系统盘；根目录可用 `HERMES_KERNEL_OUTPUT_ROOT` 整体迁移。
- **端口统一由 `HERMES_KERNEL_PORT` 环境变量控制**，client / server / bridge 三方同源，多内核可并存不冲突。
- **`main.py` 菜单栏**：「页面」菜单（启动页面 / 查看在线 / 关闭全部 / 回收孤儿）与「内核」菜单；状态栏实时显示内核与在线页面数。
- **文档**：`ARCHITECTURE.md`（目录结构、如何新增页面、生命周期回收、HTTP 接口一览、环境变量、已知限制）。
- **测试**：由 76 项增至 **309 项**。

### 修复

- **参数越界被静默夹紧**：原先 `max(lo, min(hi, v))`，用户填 `degree=99` 实际按 10 算却毫无提示。改为明确报错（`degree 必须在 0 ~ 20 之间，收到 99`）；页面侧同样去掉夹紧，把校验交给内核。
- **session_id 超长被静默截断**：`[:64]` 会让两个不同长编号变成同一个键，导致页面状态互相串扰。改为超过 64 字符显式报错。
- **`handle_import_table` 500 错误**：`data.rows` 元素是 `((x1,x2), y)` 结构，`float(r[0])` 把元组当数值转换。改为正确解包。
- **GUI 关闭后刷 `invalid command name "xxxx_refresh_kernel_status"`**：根因是构造时的**首次**状态定时器（`after(2000, ...)`）句柄从未被保存，`_stop_status_timer()` 只能取消自续期句柄，漏掉的那个永久留在 Tk 队列里，窗口销毁后仍被派发。现已把首句柄也存入 `_after_loop` 统一取消；`dispose()` 统一收口（停定时器 → 取消动画 → destroy），测试 `tearDownClass` 改调 `dispose()` 而非直接 `root.destroy()`。
- **文件清理依赖"调用方记得登记"**：`_cleanup_page_files` 原先只删 `page.images` 里登记过的路径，任何新接口忘登记就永久残留文件。改为双来源（登记路径 + **按 session_id 扫目录**），目录规则统一由 `session.image_dir()` / `report_dir()` 提供，与内核写入端同源。
- **重启后孤儿文件无人回收**：`reap_dead` 只能回收注册表内已知页面，而重启后注册表为空。新增启动时 `_sweep_orphan_files()`，只删能反推出 sid 的命名（`band_p_*` / `report_p_*`），活着的 sid 一律跳过。

### 变更

- 计算模块从顶层移入 `core/` 包（`band_fit` / `fitting` / `data_import` / `graph_engine` / `custom_loader`），相关测试 import 同步改为 `from core import xxx`。
- 主 UI `WM_DELETE_WINDOW` 只关自己，不再影响内核与页面进程。

---

## [2.0.0] - 2026-09-24

重大重构：UI 与计算逻辑分离，修复多个影响使用的缺陷，新增测试与文档。

### 新增

- **`graph_engine.py`**：纯计算层，提供函数采样、自适应缩放（`compute_auto_scale`）、折线长度（`polyline_length`）、坐标夹取等，不依赖 Tkinter，可独立测试。
- **`custom_loader.py`**：自定义函数安全加载器。
  - AST 静态校验，白名单 import，拒绝 `os`/`sys`/`open`/`eval`/`__import__`/私有属性访问
  - 签名校验（必须 5 个位置参数）
  - 文件大小上限 256 KB
  - 运行期容错：单点异常、`NaN`/`inf`/复数自动回落中心线，不中断整条曲线
  - 加载时设置 `sys.dont_write_bytecode`，不再在 `functions/` 留下 `__pycache__`
- **`functions/` 目录**：自定义函数集中管理，路径解析支持源码运行与 PyInstaller 打包（`sys._MEIPASS`），并可用环境变量 `DRAWING_FUNCTIONS_DIR` 或 `--workdir` 覆盖。
- **动画速度滑块**：可调节渐入速度（1~80 点/帧）。
- **导出 PNG** 按钮（需 Pillow）。
- **可搜索的 Math 帮助窗口**，含自定义函数签名、**推荐的有界写法示例**与安全限制说明。
- **`--workdir` / `--version` 命令行参数**。
- **测试套件**：76 项（47 项引擎/加载器单元测试 + 29 项 GUI 冒烟测试，真实创建窗口驱动）。
- **文档**：`README.md`、`CHANGELOG.md`、`.gitignore`。

### 修复

- **Y 上限线窗口拉伸后只覆盖旧宽度**：`draw_y_limit_line` 原先用启动时缓存的 `canvas_width` 画死。改为按当前画布真实宽高重绘，并在 `<Configure>` 回调中更新尺寸，测试 `test_31_y_limit_line_after_resize` 覆盖此回归。
- **自动动画逐点 `after(10)` 调度导致卡顿**：700px 需 350 帧，每帧一次回调。改为按速度批量生成坐标、一次性 `create_line`，动画 16ms 一帧且每帧绘入多段，测试断言整条曲线动画能正常收敛结束。
- **自定义函数依赖"当前工作目录"**：原先用相对路径 `f"{func_name}.py"` + `os.listdir('.')`，从其它目录启动 exe 或打包后 CWD 变化即功能失效。改为基于 `Path(__file__)` / `sys._MEIPASS` 的绝对路径解析。
- **默认自定义函数模板无界**：`clear_custom_function` 注入的模板是 `return x * math.pi`，示例文件 `test_func.py` / `test_custom.py` 是 `x ** math.pi`，x≈700 时 y≈10^5，超出画布导致看起来"白屏"。模板改为归一化正弦波，并删除两个坏示例文件。
- **窗口尺寸过小导致布局挤压**：默认窗口 900x700 且 `pack_propagate(False)` 面板宽度偏窄。默认尺寸调为 960x720，新增 `minsize(820, 580)`，面板加宽，滑块旁增加实时数值标签。
- **拖动到画布外产生超长线段**：`draw` 现在把端点夹到画布范围内。
- **保存函数后无反馈**：保存后立即做校验，坏代码当场弹窗提示，而不是等到点击"自动按钮"才报错。
- **保存函数不校验函数名合法性**：新增 `isidentifier()` 检查，非法名称拒绝写入。
- **下拉框关键时刻缺失**：自定义函数加载失败时启动无提示。新增 `report_function_dir_status()`，启动时汇总无法加载的文件。
- **无法打开函数目录**：新增"打开目录"按钮。
- **关闭窗口不停止动画**：新增 `WM_DELETE_WINDOW` 处理，绘制中退出需确认，并取消挂起的 `after` 任务。
- **帮助窗口无法定位内容**：新增搜索框与"清除"按钮，命中行高亮。

### 变更

- `main.py` 从单文件 589 行重构为 UI 层（约 830 行 + 帮助文本），业务逻辑全部外移；旧版备份为 `main_legacy_v1.py.bak`。
- `spiral_func.py` / `wave_func.py` 补全参数文档字符串，说明每个参数含义与返回值语义。
- 画布颜色语义化常量化（`COLOR_MANUAL` / `COLOR_AUTO` / `COLOR_LIMIT`），Canvas item tag 常量化（`TAG_MANUAL` / `TAG_AUTO` / `TAG_GUIDE`）。
- `auto_draw` 现在会取消上一次未完成的动画后再启动，连续两次点击不会叠加滑动值（测试 `test_28_rapid_auto_draw_twice` 覆盖）。
- 画布尺寸变化时中止正在进行的自动绘制，避免失真图形误导（测试 `test_33_resize_aborts_auto_draw` 覆盖）。

### 移除

- `test_func.py`、`test_custom.py`：无界函数示例，是"切到自定义就白屏"的直接原因。

---

## [1.0.0] - 2026-02-26（历史版本）

初版功能：

- Tkinter 手绘画布 + 滑动值统计
- 正弦/余弦/抛物线/直线自动绘制
- 自定义函数保存与加载（相对路径）
- Y 值上限线
- 纯文本 Math 帮助窗口
- PyInstaller 打包（`main.spec`）

已知问题（均已在 2.0.0 修复）：Y 上限线不随窗口拉伸更新、自动动画逐帧调度卡顿、自定义函数路径依赖工作目录、默认模板返回值无界、打包后 `datas` 未包含函数目录。
