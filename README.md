# 智能绘图板（Smart Drawing Board）

一个基于 Python + Tkinter 的桌面绘图与函数可视化工具，支持鼠标手绘、数学函数自动绘制、散点拟合、Excel 数据导入，以及安全的自定义函数扩展。

## 主要功能

- **鼠标手绘**：左键拖动绘制，实时显示坐标与累计滑动值（折线长度）
- **自动绘线**：正弦 / 余弦 / 抛物线 / 直线 / 自定义函数一键绘制
- **自动建模**：鼠标画大致形状，自动多项式拟合，输出函数与图像
- **Excel 数据导入**：导入 `.xlsx/.csv/.tsv/.txt`，多变量拟合，按表头输出表达式
- **分段包络估计**：估计散点整体走势 + 离散宽度，输出上下界拟合公式
- **自定义函数**：编辑并保存到 `functions/`，下拉框快速切换
- **安全加载**：AST 静态校验 + 运行期容错，禁止危险操作
- **导出 PNG**：一键保存画布内容
- **Math 帮助**：可搜索的 math 库参考

## 架构亮点

- **内核 / UI 分离**：计算跑在独立内核进程，功能界面是独立页面进程，主 UI 被关闭后依然存活
- **启动器锚点**：launcher 统一管理内核与各功能，生命周期最长
- **测试覆盖**：309 项测试，含 GUI 冒烟测试与内核 HTTP 端到端测试

## 下载安装

### Windows 用户（推荐）

前往 [Releases](../../releases) 下载最新版本，解压后直接运行：

- `SmartDrawingBoard-Launcher`：启动器（推荐）
- `SmartDrawingBoard-Main`：主绘图板
- `SmartDrawingBoard-Page`：独立功能页面

无需安装 Python，解压即用。

### 开发者

```bash
git clone https://github.com/Linqi-CPU/Smart-Drawing-Board.git
cd Smart-Drawing-Board
pip install -r requirements.txt  # 如有 requirements.txt
python launcher.py
```

## 快速开始

1. **启动器**：双击 `SmartDrawingBoard-Launcher`，点击「打开」启动主 UI 或功能页面
2. **手绘**：在白色画布上按住左键拖动，右下角显示坐标，左侧显示累计滑动值
3. **自动绘线**：控制面板选择函数类型，调整振幅/频率，点击「自动按钮」
4. **拟合**：进入「分段包络估计」页面，导入或采集散点，自动生成上下界拟合
5. **导出**：点击「导出 PNG」保存画布

## 使用说明

详见 [USAGE.md](USAGE.md)。

## 开发文档

- [ARCHITECTURE.md](ARCHITECTURE.md) — 架构说明、目录结构、HTTP 接口、生命周期回收
- [CHANGELOG.md](CHANGELOG.md) — 版本更新日志

## 运行测试

```bash
python -m unittest discover -s tests -v
```

## 打包

```bash
pip install pyinstaller
python build_exe.py
```

详见 [ARCHITECTURE.md §打包](ARCHITECTURE.md)。

## 常见问题

**Q：导出的 ZIP 里有很多文件？**  
这是 `--onedir` 模式，包含运行所需依赖。直接解压运行即可，不要删除文件夹内的其他文件。

**Q：启动器报错「内核启动失败」？**  
检查端口是否被占用，或手动启动内核：`python -m core.server`。

**Q：自定义函数白屏？**  
函数返回值可能超出画布范围，检查是否返回了 `NaN` 或极大值。建议使用有界函数，参考 [USAGE.md](USAGE.md)。

## 贡献

欢迎 Issue / PR。提交代码前请先运行测试：

```bash
python -m unittest discover -s tests -v
```

## 许可证

MIT
