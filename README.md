# 智能绘图板（Smart Drawing Board）

一个基于 Python + Tkinter 的桌面绘图/函数可视化工具：支持鼠标手绘（带滑动值统计）与按数学函数自动绘制曲线，内置 math 帮助与安全的自定义函数加载机制。

![平台](https://img.shields.io/badge/platform-Windows-blue) ![Python](https://img.shields.io/badge/python-3.8%2B-green) ![测试](https://img.shields.io/badge/tests-309%20passed-brightgreen) ![架构](https://img.shields.io/badge/arch-内核%2FUI%20分离-blueviolet)

---

## 功能特性

| 功能 | 说明 |
|------|------|
| 鼠标手绘 | 左键拖动绘制，实时显示坐标与累计滑动值（折线长度） |
| 自动绘线 | 正弦 / 余弦 / 抛物线 / 直线 / 自定义函数一键绘制 |
| 自适应缩放 | 曲线自动缩放进画布并居中，只缩小不放大，任何振幅都不会画出界 |
| Y 值上限线 | 红色虚线标注上限位置，随窗口拉伸实时重绘 |
| 自定义函数 | 编辑并保存到 `functions/`，下拉框快速切换 |
| 安全加载 | AST 静态校验 + 调用时容错，禁止 `os`/`sys`/`open`/`eval` 等 |
| 动画速度 | 可调节的渐入动画，可随时停止 |
| 导出 PNG | 一键把画布内容存成图片 |
| Math 帮助 | 可搜索的 math 库参考，含自定义函数签名与示例 |
| **自动建模（单变量）** | 鼠标画大致形状 → 多项式拟合，自动选阶，输出函数与图像 |
| **Excel 数据导入** | 导入 `.xlsx/.csv/.tsv/.txt` 表格数据，多变量拟合，按**表头数据名**输出表达式 |
| **分段包络估计** | 独立页面进程：估计散点整体走势 + 离散宽度，输出上下界拟合公式与图像 |
| **内核 / UI 分离** | 计算跑在独立内核进程，功能界面是独立页面进程，**主 UI 被 kill 后依然存活** |

> 📐 架构细节（目录结构、如何新增页面、生命周期回收、HTTP 接口）见 [ARCHITECTURE.md](ARCHITECTURE.md)。

---

## 环境要求

- **Python 3.8+**（开发与测试使用 3.13）
- **Tkinter**：随 Python 官方安装包提供（安装时勾选 "tcl/tk and IDLE"）
- **Pillow**：仅"导出 PNG"功能需要，缺失时程序其余功能仍可正常使用

检查环境：

```bash
python -c "import tkinter; print(tkinter.TkVersion)"
python -c "import PIL; print(PIL.__version__)"   # 可选
```

安装可选依赖：

```bash
pip install pillow
```

---

## 快速开始

```bash
# 推荐入口：启动器（生命周期最长，统一管理内核与各功能）
python launcher.py

# 主 UI（会自动拉起内核与页面）
python main.py

# 单独跑计算内核（默认 8765，可用 HERMES_KERNEL_PORT 覆盖）
python -m core.server

# 单独跑一个功能页面（脱离主 UI 也能活）
python -m edit --page band

# 指定自定义函数目录（默认 ./functions）
python main.py --workdir D:\path\to\dir

# 查看版本
python main.py --version
```

**启动器**是整套系统的锚点：它自己创建内核和各功能页面，所以只要启动器活着，
这些就活着；关掉启动器会收尾所有功能页面，但保留内核（下次还能复用）。
启动器的功能列表是自动生成的——在 `kernel_bridge.LAUNCHERS` 里加一行，
界面立刻多一项，不需要改启动器代码。

### 基本操作

1. **手绘**：在白色画布上按住左键拖动，右下角状态栏实时显示坐标；左侧"滑动值计算"显示累计折线长度。
2. **自动绘线**：在"控制面板 → 自动画线函数"选择类型，调整振幅/频率/动画速度，点击 **自动按钮**。
3. **清除**：点击 **清除按钮**，中断所有绘制并清空画布（保留 Y 上限线）。
4. **停止**：绘制过程中点击 **停止绘画**，或按 `Esc` 键。
5. **导出**：点击 **导出 PNG** 保存当前画布。
6. **调帮助**：点击 **帮助** 打开可搜索的 math 参考。

---

## 项目结构

```
智能绘图板/
├── main.py                 # UI 层：界面与交互调度
├── graph_engine.py         # 纯计算层：采样、自适应缩放、折线长度
├── custom_loader.py        # 自定义函数安全加载器
├── functions/              # 用户自定义函数目录
│   ├── spiral_func.py      #   螺旋线示例
│   └── wave_func.py        #   复合波示例
├── tests/                  # 测试
│   ├── __init__.py
│   ├── test_graph_engine.py    # 引擎 + 加载器单元测试（47 项）
│   └── test_gui_smoke.py      # GUI 冒烟测试（29 项）
├── main.spec               # PyInstaller 打包配置
├── README.md
├── CHANGELOG.md
└── .gitignore
```

### 分层说明

```
┌──────────────────────────────────┐
│  main.py（UI 层）                 │  只做界面布局与事件调度
│  · Tk 控件、绑定事件、状态栏      │
└───────────────┬──────────────────┘
                │ 调用
        ┌───────┴────────┐
        ▼                ▼
┌───────────────┐ ┌──────────────────┐
│ graph_engine  │ │ custom_loader    │
│ （纯计算）     │ │ （安全加载）      │
│ 无 tkinter 依赖│ │ 无 tkinter 依赖  │
│ 可独立单测     │ │ AST 白名单校验    │
└───────────────┘ └──────────────────┘
```

**设计原则**：所有数值逻辑都不依赖 Tkinter，因此可以在无显示环境、无窗口的情况下做单元测试；UI 层只负责把计算结果画到画布上。

---

## 自定义函数

### 函数签名

```python
def my_func(x, width, center_y, amp, freq):
    """
    x        当前 x 坐标（0 ~ width，像素）
    width    画布宽度（像素）
    center_y 画布垂直中心（像素）
    amp      振幅滑块当前值
    freq     频率滑块当前值

    返回：屏幕 y 坐标（像素，向下为正）
    """
    return center_y + amp * math.sin(2 * math.pi * freq * x / width)
```

### 编写步骤

1. 在"自定义函数"面板的 **函数名称** 框填入函数名（同时也是文件名）。
2. 在 **函数代码** 框编写代码。
3. 点击 **保存函数**。
4. 在"控制面板"把函数类型选为 **自定义**。
5. 点击 **自动按钮**。

也可以直接用文本编辑器在 `functions/` 下新建 `.py` 文件，重启程序即出现在下拉框中；或从下拉框选择后编辑。

### 示例

```python
import math

def damped(x, width, center_y, amp, freq):
    """阻尼振荡：越往右振幅越小"""
    t = x / width
    return center_y + amp * (1 - t) * math.sin(2 * math.pi * freq * t)
```

```python
import math

def square(x, width, center_y, amp, freq):
    """方波"""
    t = x / width
    s = math.sin(2 * math.pi * freq * t)
    v = 1.0 if s > 0 else (-1.0 if s < 0 else 0.0)
    return center_y + amp * v
```

### 安全限制

自定义函数来自用户输入，因此加载前会做 AST 静态校验：

| 允许 | 说明 |
|------|------|
| `import math`, `cmath`, `random`, `statistics`, `fractions`, `decimal`, `numpy` | 数学计算相关白名单 |

| 拒绝 | 原因 |
|------|------|
| `import os` / `sys` / `subprocess` / `socket` 等 | 文件与进程操作 |
| `open()` / `input()` | 文件与控制台访问 |
| `eval()` / `exec()` / `compile()` / `__import__()` | 动态代码执行 |
| `getattr` / `setattr` / `globals` / `vars` | 反射逃逸 |
| `__xxx__` 私有属性访问 | 沙箱逃逸 |
| 参数个数不为 5 | 签名不符约定 |
| 文件超过 256 KB | 防止塞入巨型文件 |

运行期还有一层保护：单个点求值抛异常或返回 `NaN`/`inf`/复数时，自动回落到中心线，不会中断整条曲线。

---

## 运行测试

```bash
# 全部测试（309 项）
python -m unittest discover -s tests -v

# 集成测试需要内核在跑
python -m core.server &
python -m unittest tests.test_kernel_integration tests.test_lifecycle -v
```

测试覆盖：

- **内核 HTTP**：真实起服务 + 真实 HTTP 调用的端到端行为，不 mock 任何一层
- **生命周期**：关一个页面只清自己的状态与文件、其他页面完好；被强杀的页面也能被回收
- **引擎**：各函数类型求值、端点与极值、复数/NaN/inf 处理、自适应缩放数值、折线长度、坐标夹取
- **算法**：分段包络估计、多变量最小二乘、Excel/CSV 解析
- **加载器**：合法函数加载、各类恶意代码拒绝、参数个数校验、文件大小限制、目录扫描、`__pycache__` 污染防护
- **GUI**：真实创建窗口后驱动手绘/自动绘线/停止/清除/拉伸窗口/保存函数/帮助窗口，断言画布 item 数量、坐标范围与滑动值

> ⚠️ GUI 测试的 `tearDownClass` 必须调 `app.dispose()`，不能直接 `root.destroy()`，
> 否则会残留定时器回调、刷 `invalid command name` 噪音。详见 [ARCHITECTURE.md §11](ARCHITECTURE.md#11-测试)。

> GUI 测试通过 `event_generate` + `root.after` 驱动，无需人工点击；注意 `after` 回调需要真实墙钟时间才会触发，测试中用循环 `update()` + `sleep` 推进。

---

## 打包为 EXE

`main.spec` 已配置好：

```bash
pip install pyinstaller
python build_exe.py
```

产出目录：`dist/`
- `SmartDrawingBoard-Launcher`：启动器
- `SmartDrawingBoard-Main`：主绘图板
- `SmartDrawingBoard-Page`：独立页面进程（如分段包络估计）

```bash
# 只打启动器
python -m PyInstaller launcher.py --name SmartDrawingBoard-Launcher ...
```

`main.spec` 已配置好：

```bash
pyinstaller main.spec
```

产出：`dist/main.exe`（单文件，约 10 MB）。

### spec 要点

```python
datas=[('functions', 'functions')],   # 必须带上函数目录，否则打包后自定义功能失效
```

已验证行为：

- 打包后 `functions/` 从 `sys._MEIPASS` 读取，无需放在 exe 同级目录
- 也可通过环境变量覆盖：
  ```bash
  set DRAWING_FUNCTIONS_DIR=D:\my_functions
  main.exe
  ```

---

## 常见问题

**Q：点击"自动按钮"提示函数不存在？**
把函数类型切到"自定义"时会去 `functions/<函数名>.py` 找与**函数名同名的文件**，且文件内必须有同名函数。检查"函数名称"框内容与文件名是否一致。

**Q：曲线画出来只有一条直线？**
函数类型选到了"直线"。或者自定义函数返回了常数（例如 `return center_y`），此时 `compute_auto_scale` 会按平坦曲线处理并居中。

**Q：自定义函数一切换就白屏？**
旧模板 `return x * math.pi` 在 x≈700 时 y≈2200，远超画布高度。v2.0 已改为有界模板（`sin` 归一化），同时自适应缩放保证任何返回值都不画出界。若仍白屏，检查函数是否返回了 `NaN`。

**Q：导出的 PNG 只有画布？**
导出的是画布区域内容（含 Y 上限线），不含左右侧边面板，这是预期行为。

**Q：窗口拉伸后曲线变形了？**
自动曲线按绘制时的画布尺寸采样，拉伸后位置会失真。程序会检测到尺寸变化并自动中止绘制，重新点击"自动按钮"即可按新尺寸重绘。

---

## 参与开发

```bash
git clone <repo>
cd 智能绘图板
python -m unittest discover -s tests -v   # 提交前先跑测试
```

新增数学函数时：修改 `core/graph_engine.py` 的 `evaluate()`，在 `main.py` 的单选项列表里加一项，并在 `tests/test_graph_engine.py` 补对应用例。

**新增一个功能页面时**（不用改主 UI 界面）：见 [ARCHITECTURE.md §5](ARCHITECTURE.md#5-新增一个-edit-页面)——只需在 `edit/` 加模块、`kernel_bridge.LAUNCHERS` 加一行、`edit/__main__.py` 加分发，主 UI 菜单会自动列出来。

---

## 更新日志

见 [CHANGELOG.md](CHANGELOG.md)。

## 开源说明

本项目已开源至 GitHub：https://github.com/Linqi-CPU/Smart-Drawing-Board

欢迎 Issue / PR，也欢迎基于此项目做二次开发。

## 许可证

MIT
