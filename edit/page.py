"""分段包络估计页面 —— 独立的 Tk 窗口。

这个页面是独立进程，有自己的 session_id，通过 core.client 与内核通信。
它不认识 main.py，主 UI 关掉不影响它。

界面布局
--------
┌───────────────────────── 顶部：编号 / 连接状态 ─────────────────────────┐
├────────── 左：参数与数据 ──────────┬────────── 右：结果与图像 ──────────┤
│ 分段数 [spin]                      │ 整体走势：y = ...                  │
│ 拟合阶数 [spin]                    │ 上界 / 下界 / C / R²               │
│ 采样步长 [spin]                    │ 离散宽度统计                       │
│ [选文件] [生成示例] [计算]          │ [走势+包络图]                     │
│ 点集状态                           │ [离散宽度图]                       │
└────────────────────────────────────┴───────────────────────────────────┘
"""

from __future__ import annotations

import os
import random
import sys
import time
import tkinter as tk
import threading
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Dict

from core.client import KernelClient, KernelClientError

APP_NAME = "分段包络估计"
PAGE_TAG = "band"

# 算法下拉框的「显示名 → 内部 key」映射。
# 为什么不直接让 Combobox 存中文值：UI 未打开时 algo_var.get() 会返回
# 默认值，而 _run_calc_worker 里是按内部 key 分支的。若把中文直接当值，
# 老配置/旧 session 恢复时会落进 else 分支静默按经典算法跑，用户毫无察觉。
# 因此这里固定用「中文显示 + 英文 key 值」，读取时统一走 _algo_key()。
ALGO_LABELS = {
    "classic": "经典算法",
    "improved": "改进算法",
    "compare": "对比模式",
    "advanced": "进阶算法",
}
#: 下拉框显示顺序（中文），与上面的 key 一一对应
ALGO_DISPLAY = [ALGO_LABELS["classic"],
                ALGO_LABELS["improved"],
                ALGO_LABELS["compare"],
                ALGO_LABELS["advanced"]]
#: 中文显示名 → 内部 key，供 _algo_key() 反查
_ALGO_KEYS = {v: k for k, v in ALGO_LABELS.items()}
DEFAULT_ALGO = "classic"


def _algo_key(raw: str) -> str:
    """把下拉框当前显示值换算成内部算法 key。

    兼容三种输入：中文显示名、英文 key（历史配置/测试直接设值）、
    以及其他值统一回落 DEFAULT_ALGO。未知值不抛异常——下拉框是 UI，
    不该因为一个脏值让整个页面崩掉。
    """
    raw = (raw or "").strip()
    if raw in ALGO_LABELS:          # 已经是内部 key
        return raw
    return _ALGO_KEYS.get(raw, DEFAULT_ALGO)

# 示例数据集：围绕已知曲线散布，便于验证算法
def demo_points(n: int = 60, kind: str = "linear"):
    """示例点集。返回 (点列表, 描述)。"""
    rng = random.Random(3)
    if kind == "linear":
        # y = 1.5x + 50，等宽误差 ±8
        pts = [(float(i), 50.0 + 1.5 * i + rng.uniform(-8, 8))
               for i in range(1, n + 1)]
        return pts, "等宽误差 ±8，真实 y = 1.5x + 50"
    if kind == "quadratic":
        # y = 0.5x - 0.01x² + 20，误差后段收窄
        pts = []
        for i in range(1, n + 1):
            x = float(i)
            w = 60.0 / (x + 2.0)
            pts.append((x, 20.0 + 0.5 * x - 0.01 * x * x
                        + w * ((i * 13) % 7 - 3) / 3.0))
        return pts, "抛物线趋势，误差随 x 收窄，真实 y = 20 + 0.5x - 0.01x²"
    if kind == "widening":
        # 误差随 x 增大
        pts = [(float(i), 100.0 + 2.0 * i + 0.05 * i * i * (1 if i % 2 else -1))
               for i in range(1, n + 1)]
        return pts, "误差随 x 增大（发散），真实 y = 100 + 2x"
    return [], ""


class BandPage:
    """分段包络估计页面主类。"""

    def __init__(self, root: tk.Tk, session_id: str,
                 client: KernelClient, page_name: str = "band"):
        self.root = root
        self.session_id = session_id
        self.client = client
        self.page_name = page_name

        # 页面本地数据（不与内核强同步，内核侧另有一份）
        self.points: list = []
        self.result: dict = {}
        self.last_png: str = ""
        self.last_report: str = ""
        self._closed = False        # 防止 on_close 被重复触发

        self._build_window()
        self._build_layout()
        self._refresh_status()

        # ---- 生命周期：把自己的 PID 告诉内核 ----
        # 内核据此判断本页面是否还活着。被任务管理器强杀时，
        # 巡检线程也能靠这个 PID 把残留收掉。
        self._announce_pid()

        # ---- 关闭钩子 ----
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        # Alt+F4 / 窗口菜单关闭在某些主题下不走 protocol，补一个绑定
        self.root.bind("<Alt-F4>", lambda e: self.on_close())

        if self.client.ping():
            self._post_note("页面已打开")

    def _announce_pid(self) -> None:
        try:
            self.client.report_pid(self.session_id, os.getpid())
        except KernelClientError:
            pass

    # --------------------------------------------------------------
    # 窗口
    # --------------------------------------------------------------
    def _build_window(self) -> None:
        self.root.title(
            f"{APP_NAME} · 页面 {self.session_id}")
        self.root.geometry("1180x760")
        self.root.minsize(900, 600)

    def _build_layout(self) -> None:
        # 顶部：编号与连接状态
        top = ttk.Frame(self.root, padding=6)
        top.pack(fill=tk.X)

        ttk.Label(top, text=f"页面编号 {self.session_id}",
                  font=("", 10, "bold")).pack(side=tk.LEFT)
        self.conn_label = ttk.Label(top, text="", foreground="#666666")
        self.conn_label.pack(side=tk.RIGHT)

        ttk.Separator(self.root, orient=tk.HORIZONTAL).pack(
            fill=tk.X, padx=6, pady=4)

        body = ttk.Frame(self.root, padding=6)
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(0, weight=1, minsize=340)
        body.columnconfigure(1, weight=3)
        body.rowconfigure(0, weight=1)

        self._build_left(body)
        self._build_right(body)

        # 状态栏
        self.status = ttk.Label(self.root, text="就绪",
                                anchor=tk.W, relief=tk.SUNKEN,
                                foreground="#444444")
        self.status.pack(fill=tk.X, side=tk.BOTTOM)

    def _build_left(self, parent: ttk.Frame) -> None:
        left = ttk.LabelFrame(parent, text="参数与数据", padding=8)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

        # ---- 参数 ----
        params = ttk.Frame(left)
        params.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(params, text="分段数").grid(row=0, column=0, sticky=tk.W,
                                              pady=3)
        self.sp_seg = ttk.Spinbox(params, from_=2, to=60, width=8)
        self.sp_seg.set(8)
        self.sp_seg.grid(row=0, column=1, sticky=tk.E)

        ttk.Label(params, text="拟合阶数").grid(row=1, column=0, sticky=tk.W,
                                                pady=3)
        self.sp_deg = ttk.Spinbox(params, from_=0, to=10, width=8)
        self.sp_deg.set(1)
        self.sp_deg.grid(row=1, column=1, sticky=tk.E)

        ttk.Label(params, text="采样步长").grid(row=2, column=0, sticky=tk.W,
                                                pady=3)
        self.sp_step = ttk.Spinbox(params, from_=1, to=50, width=8)
        self.sp_step.set(3)
        self.sp_step.grid(row=2, column=1, sticky=tk.E)

        ttk.Label(params, text="算法选择").grid(row=3, column=0, sticky=tk.W,
                                                pady=3)
        self.algo_var = tk.StringVar(value=DEFAULT_ALGO)
        algo_cb = ttk.Combobox(params, textvariable=self.algo_var,
                               values=ALGO_DISPLAY,
                               state="readonly", width=8)
        algo_cb.grid(row=3, column=1, sticky=tk.E)
        algo_cb.set(ALGO_LABELS[DEFAULT_ALGO])
        # 联动：切到「进阶算法」才展开进阶参数区。
        # 用 bind 而不是 command，Combobox 的 state="readonly"
        # 下 command 不会在鼠标选择时触发。
        algo_cb.bind("<<ComboboxSelected>>", self._sync_adv_visibility)

        # ---- 进阶算法参数（仅「进阶算法」用到）----
        # 用一个可折叠的 LabelFrame 装着：三种经典模式完全不看这些值，
        # 常驻展开只会让界面变噪音。选到「进阶算法」时自动展开。
        self.adv_frame = ttk.LabelFrame(params, text="进阶参数")
        self.adv_frame.grid(row=4, column=0, columnspan=2, sticky=tk.EW,
                            pady=(6, 0))
        self._build_adv_controls(self.adv_frame)
        # 默认 classic：进阶区先收起来
        self._sync_adv_visibility()

        params.columnconfigure(1, weight=1)

        # ---- 数据来源 ----
        ttk.Label(left, text="数据来源").pack(anchor=tk.W, pady=(4, 2))
        btns = ttk.Frame(left)
        btns.pack(fill=tk.X, pady=(0, 6))
        ttk.Button(btns, text="示例: 线性", width=12,
                   command=lambda: self._load_demo("linear")).pack(
            side=tk.LEFT, padx=(0, 3))
        ttk.Button(btns, text="示例: 抛物线", width=12,
                   command=lambda: self._load_demo("quadratic")).pack(
            side=tk.LEFT)
        btns2 = ttk.Frame(left)
        btns2.pack(fill=tk.X, pady=(0, 6))
        ttk.Button(btns2, text="示例: 发散", width=12,
                   command=lambda: self._load_demo("widening")).pack(
            side=tk.LEFT, padx=(0, 3))
        ttk.Button(btns2, text="导入表格…", width=12,
                   command=self._import_table).pack(side=tk.LEFT)

        self.data_label = ttk.Label(left, text="未载入数据",
                                    wraplength=300, justify=tk.LEFT,
                                    foreground="#666666")
        self.data_label.pack(fill=tk.X, pady=(0, 8))

        # ---- 动作 ----
        ttk.Separator(left, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=4)
        ttk.Button(left, text="开始计算", command=self._run_calc).pack(
            fill=tk.X, pady=3)

        # 进度条原本放在这里，实测看不见：本栏还有参数区/导入导出/
        # 分段明细，总高度超过常见窗口高度，进度条被挤到可视区外
        # （760px 高的窗口里按下拉框后就看不到了）。
        # 已改到右侧「计算结果」区顶部，见 _build_right。

        ttk.Button(left, text="渲染图像", command=self._render).pack(
            fill=tk.X, pady=3)
        # worker 线程里 root.after() 注册失败时的回调暂存区。
        # 真机上 main loop 一直活着，永远不会用到；
        # 但**不能丢**——丢了会让"计算成功界面没反应"与
        # "根本没算"长得一模一样。
        self._pending_after = []
        # Debug 日志开关（页面侧）。内核侧的开关在 debug_log.debug 里，
        # 两边独立：内核可能跑了多个页面，不该被单个页面的开关影响。
        self._debug_on = False
        # 内核进度轮询句柄。None 表示当前没有待触发的轮询。
        self._progress_poll = None

        ttk.Button(left, text="生成报告", command=self._make_report).pack(
            fill=tk.X, pady=3)
        ttk.Button(left, text="重连内核", command=self._reconnect).pack(
            fill=tk.X, pady=3)
        # ---- Debug 日志 ----
        # 与"重连内核"并列：两者都是**事后取证**动作，不是业务流程。
        # 排查"进阶算法为什么慢"时，慢的根因在内核（Bootstrap 并行），
        # 所以日志必须横跨两侧：页面侧记点击/参数/耗时，内核侧记
        # 并行状态、单次拟合耗时、失败数。只记一侧等于没记。
        ttk.Button(left, text="Debug 日志", command=self._toggle_debug_mode
                   ).pack(fill=tk.X, pady=3)

        # ---- 分段明细 ----
        ttk.Label(left, text="分段明细").pack(anchor=tk.W, pady=(10, 2))
        cols = ("段", "x 区间", "均值", "离散宽", "上下组")
        self.tree = ttk.Treeview(left, columns=cols, show="headings",
                                 height=10)
        for c, w in zip(cols, (34, 130, 66, 66, 60)):
            self.tree.heading(c, text=c)
            self.tree.column(c, width=w, anchor=tk.E if c not in ("段", "x 区间") else tk.W)
        self.tree.pack(fill=tk.BOTH, expand=True, pady=(0, 4))

    def _build_right(self, parent: ttk.Frame) -> None:
        right = ttk.Frame(parent)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(2, weight=3)
        right.rowconfigure(4, weight=2)

        # ---- 计算进度 ----
        # 位置是实测出来的，不是设计出来的：原本放在左侧「开始计算」
        # 按钮下方，但本栏还有参数区/导入导出/分段明细，总高度超过
        # 常见窗口高度 —— 760px 高的窗口里按下拉框后就看不见了。
        # 右侧永远可见，且它本就是"计算过程"的反馈，与结果放在一起
        # 比藏在参数栏里更自然：用户看的是结果，过程就该在旁边。
        prog = ttk.Frame(right)
        prog.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        self.prog_bar = ttk.Progressbar(prog, mode="determinate",
                                        maximum=100, value=0)
        self.prog_bar.pack(fill=tk.X)
        self.prog_label = ttk.Label(prog, text="", wraplength=420,
                                    justify=tk.LEFT, foreground="#666666")
        self.prog_label.pack(fill=tk.X, pady=(2, 0))
        # 只藏进度条与文字，不藏 prog 骨架：反复 forget/pack 布局骨架
        # 会让周围控件来回跳动。
        # 代价是空一小条，远比布局抖动可接受。
        self._progress_widgets = (self.prog_bar, self.prog_label)
        self._hide_progress()

        # ---- 结果文本 ----
        res = ttk.LabelFrame(right, text="计算结果", padding=8)
        res.grid(row=2, column=0, sticky="nsew")
        self.result_text = tk.Text(res, height=10, wrap=tk.WORD,
                                   font=("Consolas", 10))
        sb = ttk.Scrollbar(res, orient=tk.VERTICAL,
                           command=self.result_text.yview)
        self.result_text.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.result_text.pack(fill=tk.BOTH, expand=True)

        # ---- 走势+包络图 ----
        img1 = ttk.LabelFrame(right, text="整体走势与离散区间", padding=4)
        img1.grid(row=4, column=0, sticky="nsew", pady=6)
        self.canvas1 = tk.Canvas(img1, bg="#fafafa", highlightthickness=0)
        self.canvas1.pack(fill=tk.BOTH, expand=True)

        # ---- 离散宽度图 ----
        img2 = ttk.LabelFrame(right, text="离散宽度 d(x)", padding=4)
        img2.grid(row=6, column=0, sticky="nsew", pady=(0, 6))
        self.canvas2 = tk.Canvas(img2, bg="#fafafa", highlightthickness=0)
        self.canvas2.pack(fill=tk.BOTH, expand=True)

        # 用 PIL 显示图片
        self._tk_img = None

    # --------------------------------------------------------------
    # 关闭：清理自己的残留并终止自己的进程，不影响其他页面
    # --------------------------------------------------------------
    def on_close(self) -> None:
        """关窗口时做完整回收。

        顺序很重要：
        1. 通知内核"我要关了"（标记 closing，给内核留宽限期）
        2. 请求内核删掉**本页面**的图片与报告，并注销本 session
        3. 销毁窗口、退出进程

        为什么不用 os._exit：
        root.destroy() 之后 mainloop 自然返回，进程正常退出，
        句柄和 Python 资源都会释放。os._exit 会跳过 atexit，
        可能留下未 flush 的缓冲区，所以只在兜底路径用。

        其他页面完全不受影响：内核按 session_id 隔离，
        这里从头到尾只碰 self.session_id。
        """
        if self._closed:
            return
        self._closed = True

        # 进度轮询必须先停：它持有 root.after 句柄，窗口销毁后
        # 仍会被 Tk 派发，刷 invalid command name 噪音
        #（main.py 的状态定时器踩过同一个坑，见 CHANGELOG）。
        self._stop_progress_poll()
        # 关窗后没人要这些结果了。清掉，免得测试补跑时
        # 对着已销毁的窗口执行回调。
        self._pending_after.clear()

        # 1 + 2：让内核清掉本页面的状态与落盘文件
        try:
            self.client.mark_closing(self.session_id)
            self.client.cleanup(self.session_id)
        except KernelClientError:
            # 内核已不在（可能被单独杀掉）：进程照样要退，
            # 磁盘残留由本地兜底清理 + 下次内核巡检处理。
            pass

        # 3：本地兜底删除——内核没连上时，自己把自己那两个文件删了。
        #    路径规则与内核一致，只匹配本 session_id，不会误删别人的。
        self._local_cleanup()

        # 4：销毁窗口，让 mainloop 自然返回、进程正常退出。
        #    这里**不调用** os._exit：它会跳过 atexit 与缓冲区 flush，
        #    也就跳过了 root.destroy() 的清理。正常路径足够。
        try:
            self.root.destroy()
        except Exception:
            # 窗口已被外力销毁等异常情况，确保进程一定退出
            import os as _os
            _os._exit(0)

    def _local_cleanup(self) -> list:
        """本地删除本页面的图片与报告（内核不可达时的兜底）。

        用 session_id 精确匹配文件名，绝不碰其他页面的产物。
        目录规则与内核共用 session 模块，两端不会漂移。
        """
        import glob

        from core import session as ses

        # 只匹配文件名里含本 session_id 的产物，例如 band_p_abc12345.png
        patterns = [
            str(ses.image_dir() / f"*{self.session_id}*.png"),
            str(ses.report_dir() / f"*{self.session_id}*.txt"),
        ]
        removed = []
        for pat in patterns:
            for path in glob.glob(pat):
                try:
                    os.unlink(path)
                    removed.append(path)
                except OSError:
                    pass
        return removed

    # --------------------------------------------------------------
    # 状态
    # --------------------------------------------------------------
    def _set_status(self, text: str) -> None:
        self.status.config(text=text)

    def _refresh_status(self) -> None:
        ok = self.client.ping()
        if ok:
            self.conn_label.config(
                text="● 内核已连接", foreground="#27ae60")
        else:
            self.conn_label.config(
                text="● 内核未连接", foreground="#c0392b")
        self.root.after(4000, self._refresh_status)

    def _reconnect(self) -> None:
        try:
            self.client.register(self.session_id, source="edit:band")
            self._refresh_status()
            self._set_status("内核已重连")
        except KernelClientError as e:
            messagebox.showerror(APP_NAME, str(e), parent=self.root)
            self._set_status(f"重连失败: {e}")

    def _post_note(self, text: str) -> None:
        try:
            self.client.note(self.session_id, text)
        except KernelClientError:
            pass

    # --------------------------------------------------------------
    # 数据
    # --------------------------------------------------------------
    def _load_demo(self, kind: str) -> None:
        pts, desc = demo_points(60, kind)
        self.points = pts
        self.data_label.config(text=f"示例数据（{len(pts)} 点）\n{desc}")
        self._push_points()
        self._set_status(f"已载入示例: {kind}")

    def _import_table(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.root,
            title="选择数据文件",
            filetypes=[("Excel / 表格", "*.xlsx *.csv *.tsv *.txt"),
                       ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            info = self.client.import_table(self.session_id, path)
        except KernelClientError as e:
            messagebox.showerror(APP_NAME, str(e), parent=self.root)
            return
        self.points = self.client.get_points(self.session_id)
        self.data_label.config(
            text=f"{Path(path).name}\n"
                 f"{info['n_rows']} 行 × {info['n_vars']} 变量\n"
                 f"变量: {', '.join(info['variables'])}\n"
                 f"目标: {info['target']}")
        self._set_status(f"已导入 {path}")

    def _push_points(self) -> None:
        if not self.points:
            return
        try:
            self.client.save_points(self.session_id, self.points,
                                    source="edit:band")
        except KernelClientError as e:
            messagebox.showerror(APP_NAME, f"上传点集失败: {e}",
                                 parent=self.root)

    # --------------------------------------------------------------
    # 进阶算法控件
    # --------------------------------------------------------------
    def _build_adv_controls(self, parent) -> None:
        """构造「进阶参数」区的控件。

        GPU 开关**默认关闭**，且勾选时给出明确说明：
          项目承诺零第三方依赖、Release 解压即用，而 torch 会让
          产物体积翻数倍。所以 GPU 是显式 opt-in，
          点了才通过 core.deps 走「探测 → PyPI → 离线包」的安装流程。
        """
        self.var_select_deg = tk.BooleanVar(value=False)
        ttk.Checkbutton(parent, text="AIC/BIC 自动选阶",
                        variable=self.var_select_deg
                        ).grid(row=0, column=0, columnspan=2,
                               sticky=tk.W, pady=2)

        self.var_adaptive = tk.BooleanVar(value=False)
        ttk.Checkbutton(parent, text="自适应分段",
                        variable=self.var_adaptive
                        ).grid(row=1, column=0, columnspan=2,
                               sticky=tk.W, pady=2)

        ttk.Label(parent, text="Bootstrap 次数").grid(row=2, column=0,
                                                     sticky=tk.W, pady=2)
        self.sp_boot = ttk.Spinbox(parent, from_=0, to=2000, width=8)
        self.sp_boot.set(0)
        self.sp_boot.grid(row=2, column=1, sticky=tk.E)

        ttk.Label(parent, text="置信水平 1-α").grid(row=3, column=0,
                                                   sticky=tk.W, pady=2)
        self.sp_alpha = ttk.Spinbox(parent, from_=0.01, to=0.5, increment=0.01,
                                    width=8)
        self.sp_alpha.set(0.05)
        self.sp_alpha.grid(row=3, column=1, sticky=tk.E)

        ttk.Label(parent, text="分位点 τ").grid(row=4, column=0,
                                               sticky=tk.W, pady=2)
        self.sp_tau = ttk.Spinbox(parent, from_=0.05, to=0.95, increment=0.05,
                                  width=8)
        self.sp_tau.set(0.5)
        self.sp_tau.grid(row=4, column=1, sticky=tk.E)

        self.var_use_gpu = tk.BooleanVar(value=False)
        gpu_cb = ttk.Checkbutton(parent, text="GPU 加速 Bootstrap",
                                 variable=self.var_use_gpu)
        gpu_cb.grid(row=5, column=0, columnspan=2, sticky=tk.W, pady=2)
        # 悬停提示：GPU 是 opt-in，且首次使用会触发安装
        self._tooltip(gpu_cb,
                      "默认关闭。勾选后需要额外安装 torch（约几百 MB），\n"
                      "内核会尝试自动安装；不可用时自动回落到 CPU。\n"
                      "仅对 Bootstrap 有加速作用（实测约 1.7 倍）。")

        parent.columnconfigure(1, weight=1)

    @staticmethod
    def _tooltip(widget, text: str) -> None:
        """给控件挂一个简单的悬停提示。"""
        tip = None

        def _enter(_):
            nonlocal tip
            try:
                x = widget.winfo_rootx() + 20
                y = widget.winfo_rooty() + widget.winfo_height() + 4
                tip = tk.Toplevel(widget)
                tip.wm_overrideredirect(True)
                tip.wm_geometry(f"+{x}+{y}")
                tk.Label(tip, text=text, justify=tk.LEFT,
                         background="#ffffe0", foreground="#000000",
                         relief=tk.SOLID, borderwidth=1,
                         font=("Microsoft YaHei UI", 8)).pack()
            except Exception:
                tip = None

        def _leave(_):
            nonlocal tip
            if tip is not None:
                try:
                    tip.destroy()
                except Exception:
                    pass
                tip = None

        widget.bind("<Enter>", _enter)
        widget.bind("<Leave>", _leave)

    def _sync_adv_visibility(self, _event=None) -> None:
        """按当前算法显示/隐藏进阶参数区。"""
        algo = _algo_key(getattr(self, "algo_var", None) and self.algo_var.get())
        mgr = getattr(self.adv_frame, "grid", None) if hasattr(self, "adv_frame") else None
        if mgr is None:
            return
        if algo == "advanced":
            self.adv_frame.grid()
        else:
            self.adv_frame.grid_remove()

    # --------------------------------------------------------------
    # 参数读取
    # --------------------------------------------------------------
    def _params(self) -> dict:
        """读参数。范围校验交给内核，这里只负责转成整数。

        页面侧不做夹紧：如果夹紧，用户填 99 实际按 10 算却看不出来。
        内核会返回明确错误，页面直接显示。
        """
        def _int(widget, default):
            try:
                return int(widget.get())
            except (ValueError, AttributeError):
                return default

        def _float(widget, default):
            """读浮点参数。空值/非法值一律回落到默认，
            不在页面侧夹紧范围（与 _int 同一策略：
            校验交给内核，用户能看到明确报错而不是静默被改）。"""
            try:
                return float(widget.get())
            except (ValueError, AttributeError):
                return default

        return {
            "n_segments": _int(self.sp_seg, 8),
            "degree": _int(self.sp_deg, 1),
            "step": _int(self.sp_step, 3),
            # ---- 进阶算法参数（仅 algorithm=advanced 时使用）----
            # 全部有保守默认值：不开 Bootstrap 就不会慢，
            # 不开 GPU 就不会触发 torch 安装流程。
            "n_boot": _int(self.sp_boot, 0),
            "alpha": _float(self.sp_alpha, 0.05),
            "select_degree": bool(self.var_select_deg.get()),
            "adaptive": bool(self.var_adaptive.get()),
            "quantile_tau": _float(self.sp_tau, 0.5),
            "use_gpu": bool(self.var_use_gpu.get()),
            "seed": None,
        }

    @staticmethod
    def _adv_params_from(body: Dict[str, Any]) -> Dict[str, Any]:
        """从一段已保存的请求体（如 session 恢复）取进阶参数。"""
        return {
            "n_boot": int(body.get("n_boot", 0) or 0),
            "alpha": float(body.get("alpha", 0.05) or 0.05),
            "select_degree": bool(body.get("select_degree", False)),
            "adaptive": bool(body.get("adaptive", False)),
            "quantile_tau": float(body.get("quantile_tau", 0.5) or 0.5),
            "use_gpu": bool(body.get("use_gpu", False)),
            "seed": body.get("seed"),
        }

    # --------------------------------------------------------------
    # 计算（放子线程，避免卡界面）
    # --------------------------------------------------------------
    def _run_calc(self) -> None:
        if not self.points:
            messagebox.showinfo(APP_NAME, "请先载入数据", parent=self.root)
            return
        self._set_status("计算中…")
        self.root.config(cursor="watch")
        p = self._params()
        # 算法 key 必须在这里定下来随 params 一起传走。
        # 早前是 worker 线程里读 self.algo_var.get() —— Tkinter 变量
        # 不是线程安全的，worker 在 main loop 之外 get() 会抛
        # "RuntimeError: main thread is not in main loop"，
        # 于是计算根本没跑，UI 却显示上一轮的旧结果。
        p["algorithm"] = _algo_key(
            getattr(self, "algo_var", None) and self.algo_var.get())
        # 进度条出场 + 开始轮询内核的进度快照。
        # 只有进阶算法会真报进度（其它算法秒回，轮询一两次就结束），
        # 但轮询逻辑是通用的，不必为算法分叉。
        self._show_progress()
        self._start_progress_poll()
        self._dlog("calc_clicked", algorithm=p["algorithm"],
                   n_points=len(self.points), **{
                       k: p[k] for k in ("n_segments", "degree", "n_boot",
                                         "alpha", "adaptive", "use_gpu")
                       if k in p})
        threading.Thread(target=self._run_calc_worker,
                         args=(p,), daemon=True).start()

    # ==================================================================
    # Debug 日志
    # ==================================================================
    # 排查"为什么慢"必须把两侧时间线拼起来：页面侧记点击/参数/总耗时，
    # 内核侧记并行进程数、每个 chunk 的耗时。只记一侧等于没记 ——
    # 页面侧只能看到"等了 45 秒"，看不出是串行跑满的还是并行没生效。
    #
    # 默认关闭：_log() 在关闭态直接 return，不拼字符串不写盘。
    def _toggle_debug_mode(self) -> None:
        """开关执行过程日志。返回切换后的状态。"""
        try:
            # 扁平 import，与 core/ 内其他模块的约定一致。
            # 不能用 `from core.debug_log import debug`：edit/ 侧用
            # 包形式导入、core/ 侧用扁平导入，同一进程会得到两个
            # 模块对象、两个单例 —— 开关状态互不可见。
            import debug_log
            on = debug_log.debug.toggle()
            if on:
                path = debug_log.debug.path or ""
                # 目录可能还不存在（首次打开），补一次
                try:
                    Path(path).parent.mkdir(parents=True, exist_ok=True)
                except OSError:
                    pass
                self._debug_on = True
                self._set_status(f"Debug 日志已开启：{path}")
            else:
                self._debug_on = False
                self._set_status("Debug 日志已关闭")
        except Exception as e:
            # 日志模块不可用不能挡住主流程
            messagebox.showerror(APP_NAME, f"无法切换日志开关:\n{e}",
                                 parent=self.root)
            self._debug_on = False

    def _dlog(self, tag: str, **fields) -> None:
        """页面侧记一条。内部已做开关判断，热点路径可直接调。"""
        if not getattr(self, "_debug_on", False):
            return
        try:
            import debug_log
            debug_log.debug.log(tag, session=self.session_id, **fields)
        except Exception:
            pass

    def _open_debug_log(self) -> None:
        """用系统默认程序打开日志文件，便于直接看。"""
        path = None
        try:
            import debug_log
            path = debug_log.debug.path
        except Exception:
            path = None
        if not path or not Path(path).exists():
            messagebox.showinfo(APP_NAME,
                                "还没有日志文件。请先点一次「Debug 日志」"
                                "打开开关，再跑一次计算。",
                                parent=self.root)
            return
        try:
            os.startfile(str(path))       # noqa: E606  (Windows only)
        except Exception as e:
            messagebox.showerror(APP_NAME, f"打不开日志文件:\n{e}",
                                 parent=self.root)

    # ==================================================================
    # 计算进度
    # ==================================================================
    # 架构说明：计算跑在**内核进程**里，本页面只是个 HTTP 客户端，
    # 两边没有长连接。所以进度由内核写进 PageState.progress，
    # UI 用 /api/pages 轮询来取 —— 复用已有的接口，不新增路由。
    _PHASE_LABELS = {
        "select_degree": "自动选阶",
        "segments": "自适应分段",
        "quantile": "分位数回归",
        "bootstrap": "Bootstrap 置信带",
    }
    _PROGRESS_POLL_MS = 250

    def _show_progress(self) -> None:
        # 恢复各自的**原始** pack 参数。重新 pack 时不带参数会让
        # 间距、fill 全部回默认值，进度区会突然变窄/错位。
        self.prog_bar.pack(fill=tk.X)
        self.prog_label.pack(fill=tk.X, pady=(2, 0))
        self.prog_label.configure(text="计算中…")
        self.prog_bar.configure(mode="indeterminate", value=0)
        self.prog_bar.start(12)

    def _hide_progress(self) -> None:
        try:
            self.prog_bar.stop()
        except Exception:
            pass
        # 只 forget 这两个；prog 骨架常驻（见 __init__ 里的说明）
        self.prog_bar.pack_forget()
        self.prog_label.pack_forget()
        self.prog_label.configure(text="")
        self._stop_progress_poll()

    def _start_progress_poll(self) -> None:
        self._stop_progress_poll()
        self._progress_poll = self.root.after(
            self._PROGRESS_POLL_MS, self._poll_progress)

    def _stop_progress_poll(self) -> None:
        if getattr(self, "_progress_poll", None) is not None:
            try:
                self.root.after_cancel(self._progress_poll)
            except Exception:
                pass
            self._progress_poll = None

    def _poll_progress(self) -> None:
        """查一次内核的进度快照并刷新控件；计算结束就收起。

        复用既有的 /api/sessions（客户端方法 sessions()）而不是新增
        一个"取进度"接口：PageState.progress 已经在 to_dict() 里，
        再为它开一条路由纯粹是重复造轮子。
        """
        self._progress_poll = None
        try:
            items = self.client.sessions() or []
            mine = None
            for it in items:
                if str(it.get("session_id")) == str(self.session_id):
                    mine = it
                    break
            prog = (mine or {}).get("progress") or {}
            if prog.get("active"):
                self._apply_progress(prog)
                # 还没结束，继续轮
                self._progress_poll = self.root.after(
                    self._PROGRESS_POLL_MS, self._poll_progress)
                return
        except Exception:
            # 轮询失败不打扰用户：进度是观察性的，
            # 内核正忙时接口也可能暂时无响应。
            pass
        # 到这里说明计算已结束（或查不到）——收起进度条。
        # 真正的结果显示由 _on_calc_done 负责，这里只管进度 UI。
        self._hide_progress()

    def _apply_progress(self, prog: dict) -> None:
        # 容错转换：进度数据来自另一个进程，本就把"坏了"当作要处理
        # 的情况之一。int() 失败时退回 0，进度最多显示得不准，
        # 但不能让一次轮询的异常把整个回调链打断。
        try:
            done = int(prog.get("done") or 0)
        except (TypeError, ValueError):
            done = 0
        try:
            total = int(prog.get("total") or 0)
        except (TypeError, ValueError):
            total = 0
        phase = str(prog.get("phase") or "")
        name = self._PHASE_LABELS.get(phase, phase)
        prefix = f"{name}：" if name else "计算中"

        if total > 0:
            pct = min(100, max(0, round(done * 100.0 / total)))
            # 切到 determinate 才能显示百分比。
            # mode 是可以反复 configure 的，没必要为两种模式建两个控件。
            try:
                self.prog_bar.configure(mode="determinate")
                self.prog_bar.stop()
            except Exception:
                pass
            self.prog_bar.configure(value=pct)
            self.prog_label.configure(text=f"{prefix}{pct}%（{done}/{total}）")
        else:
            # 总数未知（选阶、分段这类轻量阶段）→ 走 indeterminate，
            # 至少让用户知道"在动"
            try:
                if str(self.prog_bar.cget("mode")) != "indeterminate":
                    self.prog_bar.configure(mode="indeterminate")
                    self.prog_bar.start(12)
            except Exception:
                pass
            self.prog_label.configure(text=prefix)

    # ==================================================================
    # 线程安全回跳
    # ==================================================================
    # root.after() 与 Tkinter 变量一样不是线程安全的：在 worker 线程里
    # 调用会抛 "RuntimeError: main thread is not in main loop"。
    # 一旦抛了，异常被 worker 的 except 吞掉，计算结果就静默丢失 ——
    # 用户点完按钮界面毫无变化，是最难排查的一类"假死"。
    def _safe_after(self, ms: int, fn) -> None:
        """确保 fn 在主线程运行。

        root.after() 与 Tkinter 变量一样不是线程安全的：在 worker
        线程里调用会抛 "RuntimeError: main thread is not in main loop"
        （或窗口已销毁时的 TclError）。真机上 main loop 一直活着，
        不会走到这里；测试环境没有 main loop，注册必然失败 ——
        那种情况下不能再把结果静默丢掉，否则"计算成功界面没反应"
        与"根本没算"长得一模一样。改为入队 _pending_after，
        由测试手动补跑。
        """
        def _run():
            try:
                fn()
            except tk.TclError:
                # 窗口已销毁：不是错误，只是没人要这个结果了
                pass
            except Exception:
                # 这里不能再向 root.after 跳了（可能仍在关窗流程中），
                # 否则递归；打印出来便于从死亡日志发现
                traceback.print_exc()

        try:
            self.root.after(ms, _run)
        except (RuntimeError, tk.TclError) as e:
            if getattr(self, "_closed", False):
                return          # 正在关窗，没人要结果了
            self._pending_after.append(_run)
            # 用 stderr 而非 print，避免污染 stdout 抓取
            print(f"[band] after() 注册失败，结果入队: {type(e).__name__}",
                  file=sys.stderr)

    def _run_calc_worker(self, p: dict) -> None:
        # 算法从 params 取，不在这个线程里读 Tkinter 变量 ——
        # 它们是线程不安全的，这里 get() 会抛 RuntimeError。
        algo = _algo_key(p.get("algorithm"))
        t0 = time.perf_counter()
        try:
            if algo == "improved":
                res = self.client.band_fit_improved(self.session_id,
                                                    n_segments=p["n_segments"],
                                                    degree=p["degree"])
                self._safe_after(0, lambda: self._on_calc_done(
                    res, p, "improved"))
            elif algo == "compare":
                classic = self.client.band_fit(self.session_id,
                                               n_segments=p["n_segments"],
                                               degree=p["degree"])
                improved = self.client.band_fit_improved(
                    self.session_id, n_segments=p["n_segments"],
                    degree=p["degree"])
                self._safe_after(0, lambda: self._on_compare_done(
                    classic, improved, p))
            elif algo == "advanced":
                res = self.client.band_fit_advanced(
                    self.session_id,
                    n_segments=p["n_segments"],
                    degree=p["degree"],
                    n_boot=p["n_boot"],
                    alpha=p["alpha"],
                    select_degree=p["select_degree"],
                    adaptive=p["adaptive"],
                    quantile_tau=p["quantile_tau"],
                    use_gpu=p["use_gpu"],
                    seed=p["seed"],
                )
                self._safe_after(0, lambda: self._on_calc_done(
                    res, p, "advanced"))
                self._dlog("calc_done", algorithm="advanced",
                           elapsed_ms=round(
                               (time.perf_counter() - t0) * 1000, 1),
                           n_boot=p.get("n_boot"),
                           r2=res.get("r2"),
                           boot_backend=(res.get("advanced") or {}).get(
                               "boot_backend"))
            else:
                res = self.client.band_fit(self.session_id,
                                           n_segments=p["n_segments"],
                                           degree=p["degree"])
                self._safe_after(0, lambda: self._on_calc_done(
                    res, p, "classic"))
                self._dlog("calc_done", algorithm=algo,
                           elapsed_ms=round(
                               (time.perf_counter() - t0) * 1000, 1),
                           r2=res.get("r2"))
        except KernelClientError as e:
            self._dlog("calc_error", algorithm=algo,
                       elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
                       err=str(e)[:200])
            self._safe_after(0, lambda: self._on_calc_error(e))
        except Exception as e:
            self._dlog("calc_error", algorithm=algo,
                       elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
                       err=f"{type(e).__name__}: {e}"[:200])
            self._safe_after(0, lambda: self._on_calc_error(
                KernelClientError(f"{type(e).__name__}: {e}")))

    def _on_compare_done(self, classic: dict, improved: dict, p: dict) -> None:
        self.root.config(cursor="")
        self._hide_progress()
        self.result = classic
        self.result_improved = improved
        self._render_compare_text(classic, improved)
        self._set_status(f"对比完成 | 经典 R²={classic.get('r2', 0):.3f} / 改进 R²={improved.get('r2', 0):.3f}")
        try:
            self._draw_compare_band(classic, improved)
            self._draw_compare_dev(classic, improved)
        except Exception as e:
            self._set_status(f"对比曲线绘制失败: {e}")

    def _render_compare_text(self, classic: dict, improved: dict) -> None:
        lines = [
            f"页面编号: {self.session_id}",
            f"点数 {classic.get('n_points', 0)}，分段 {classic.get('n_segments', 0)}",
            "",
            "=== 经典算法 ===",
            f"走势: {classic.get('expression', '')}",
            f"R² = {classic.get('r2', 0):.6f}   RMSE = {classic.get('rmse', 0):.6g}",
            "",
            "=== 改进算法 ===",
            f"走势: {improved.get('expression', '')}",
            f"R² = {improved.get('r2', 0):.6f}   RMSE = {improved.get('rmse', 0):.6g}",
        ]
        self.result_text.delete("1.0", tk.END)
        self.result_text.insert(tk.END, "\n".join(lines))

    def _on_calc_error(self, err) -> None:
        self.root.config(cursor="")
        self._hide_progress()
        self._set_status(f"计算失败: {err}")
        messagebox.showerror(APP_NAME, f"计算失败:\n{err}",
                             parent=self.root)

    def _on_calc_done(self, res: dict, p: dict, algo: str = "classic") -> None:
        self.root.config(cursor="")
        self._hide_progress()
        self.result = res
        self._render_text(res, algo)
        self._fill_tree(res)
        self._set_status(f"计算完成 | {res['expression']}")

        # 自动顺手把图画了
        try:
            self._draw_band()
            self._draw_dev()
        except Exception as e:
            self._set_status(f"曲线绘制失败: {e}")

    def _render_advanced_summary(self, res: dict, lines: list) -> None:
        """把 advanced 子字典渲染成若干行，追加到 lines。

        放在 try 里跑：渲染摘要失败不该让整个结果变空白 ——
        经典那部分（走势/离散度）才是主菜。
        """
        adv = res.get("advanced") or {}
        if not adv:
            return

        lines += ["", "─" * 34, "进阶分析"]

        sel = adv.get("selection") or {}
        if sel:
            lines += [
                f"  自动选阶: {sel.get('criterion', '')} 选中 "
                f"{sel.get('best_degree')} 阶",
                "  候选: " + "  ".join(
                    f"{c.get('degree')}阶(AIC {c.get('aic'):.1f}/"
                    f"BIC {c.get('bic'):.1f})"
                    for c in sel.get("candidates", [])
                    if c.get("aic") is not None
                ) or "  （无）",
            ]
        else:
            lines.append(f"  实际阶数: {adv.get('effective_degree')}")

        lines.append(f"  分段策略: {adv.get('segment_strategy')}"
                     f"（{len(adv.get('segments') or [])} 段）")

        q = adv.get("quantile") or {}
        if q:
            conv = "已收敛" if q.get("converged") else "未收敛"
            head = (f"  分位数回归 τ={q.get('tau')}: {conv}，"
                    f"{q.get('iters')} 次迭代")
            if q.get("r2") is not None:
                head += f"，R²={q['r2']:.4f}"
            lines.append(head)
        else:
            lines.append("  分位数回归: 未启用")

        b = adv.get("bootstrap") or {}
        if b:
            lines += [
                f"  Bootstrap: {b.get('n_boot')} 次，"
                f"置信水平 {1 - (b.get('alpha') or 0.05):.0%}",
                f"    后端 {b.get('backend')}，失败 {b.get('n_fail')} 次",
            ]
        else:
            lines.append("  Bootstrap: 未启用")

        lines.append("  GPU: " + ("已启用" if adv.get("gpu_used") else "未启用"))

        warns = adv.get("warnings") or []
        if warns:
            lines += ["", "  提示:"]
            lines += [f"    · {w}" for w in warns]

    def _render_text(self, res: dict, algo: str = "classic") -> None:
        s = res["scatter"]
        lines = [
            f"页面编号: {self.session_id}",
            f"点数 {res['n_points']}，分段 {res['n_segments']}，"
            f"上/下界阶数 {res['degree_upper']}/{res['degree_lower']}",
            "",
            f"整体走势:  {res['expression']}",
            f"上界 f_up: {res['upper']['expression']}",
            f"下界 f_lo: {res['lower']['expression']}",
            f"偏移常数 C = {res['offset']:.6g}",
            "",
            f"R² = {res['r2']:.6f}   RMSE = {res['rmse']:.6g}",
            f"平均绝对偏差 = {res['mean_abs_dev']:.6g}",
            "",
            "离散程度:",
            f"  {s['description']}",
            f"  平均宽度 {s['mean_d']:.6g}，"
            f"范围 [{s['min_d']:.6g}, {s['max_d']:.6g}]",
            f"  尾部/首部 = {s['tail_ratio']:.3f}",
        ]
        if res.get("caution"):
            lines += ["", f"注意: {res['caution']}"]

        if algo == "advanced":
            try:
                self._render_advanced_summary(res, lines)
            except Exception:
                # 摘要渲染失败不能吃掉主结果，最多丢一小段信息
                pass

        self.result_text.delete("1.0", tk.END)
        self.result_text.insert(tk.END, "\n".join(lines))

    def _fill_tree(self, res: dict) -> None:
        self.tree.delete(*self.tree.get_children())
        for seg in res.get("segments", []):
            iv = f"[{seg['x_lo']:.4g}, {seg['x_hi']:.4g}]"
            self.tree.insert(
                "", tk.END,
                values=(seg["index"], iv,
                        f"{seg['mean_y']:.4g}",
                        f"{seg['d']:.4g}",
                        f"{seg['n_upper']}/{seg['n_lower']}"))

    # --------------------------------------------------------------
    # 图像
    # --------------------------------------------------------------
    def _draw_band(self) -> None:
        """画走势+包络+散点。"""
        if not self.result:
            return
        bs = self.client.band_series(self.session_id, step=self._params()["step"])
        xs, up, lo = bs["xs"], bs["upper"], bs["lower"]
        xs_all = [p[0] for p in self.points]
        ys_all = [p[1] for p in self.points]
        trend = [self.result["coefficients"][i] * 1.0
                 for i in range(len(self.result["coefficients"]))]
        # 走势线用内核给的系数本地求值，避免再次请求
        trend_y = [_poly(trend, x) for x in xs]

        self._plot(self.canvas1, [
            ("scatter", xs_all, ys_all, "#4a9eff", "离散点"),
            ("line", xs, up, "#e74c3c", "上界 f_up"),
            ("line", xs, lo, "#27ae60", "下界 f_lo"),
            ("band", xs, lo, up, "#f1c40f", "离散区间"),
            ("line", xs, trend_y, "#2c3e50", "整体走势"),
        ], title=self.result["expression"])

    def _draw_compare_band(self, classic: dict, improved: dict) -> None:
        """对比模式：同一张图里叠经典/改进两条走势。"""
        if not classic or not improved:
            return
        bs = self.client.band_series(self.session_id, step=self._params()["step"])
        xs, up, lo = bs["xs"], bs["upper"], bs["lower"]
        xs_all = [p[0] for p in self.points]
        ys_all = [p[1] for p in self.points]

        classic_trend = [classic["coefficients"][i] for i in range(len(classic["coefficients"]))]
        improved_trend = [improved["coefficients"][i] for i in range(len(improved["coefficients"]))]
        classic_y = [_poly(classic_trend, x) for x in xs]
        improved_y = [_poly(improved_trend, x) for x in xs]

        self._plot(self.canvas1, [
            ("scatter", xs_all, ys_all, "#4a9eff", "离散点"),
            ("line", xs, up, "#e74c3c", "上界 f_up"),
            ("line", xs, lo, "#27ae60", "下界 f_lo"),
            ("band", xs, lo, up, "#f1c40f", "离散区间"),
            ("line", xs, classic_y, "#2c3e50", "经典走势"),
            ("line", xs, improved_y, "#9b59b6", "改进走势"),
        ], title=f"对比 | 经典 R²={classic.get('r2', 0):.3f} / 改进 R²={improved.get('r2', 0):.3f}")

    def _draw_compare_dev(self, classic: dict, improved: dict) -> None:
        if not classic or not improved:
            return
        ds = self.client.dev_series(self.session_id,
                                    step=self._params()["step"])
        xs, d = ds["xs"], ds["d"]
        mean_d = classic.get("scatter", {}).get("mean_d", improved.get("scatter", {}).get("mean_d", 0))
        self._plot(self.canvas2, [
            ("line", xs, d, "#8e44ad", "离散宽度 d(x)"),
            ("hline", xs, mean_d, "#7f8c8d", f"平均 {mean_d:.4g}"),
            ("area", xs, 0, d, "#8e44ad", ""),
        ], title=f"离散程度 经典={classic.get('scatter', {}).get('trend', '')} / 改进={improved.get('scatter', {}).get('trend', '')}",
            yzero=True)

    def _draw_dev(self) -> None:
        """画离散宽度曲线。"""
        if not self.result:
            return
        ds = self.client.dev_series(self.session_id,
                                    step=self._params()["step"])
        xs, d = ds["xs"], ds["d"]
        mean_d = self.result["scatter"]["mean_d"]
        self._plot(self.canvas2, [
            ("line", xs, d, "#8e44ad", "离散宽度 d(x)"),
            ("hline", xs, mean_d, "#7f8c8d", f"平均 {mean_d:.4g}"),
            ("area", xs, 0, d, "#8e44ad", ""),
        ], title=f"离散程度 {self.result['scatter']['trend']}",
            yzero=True)

    def _plot(self, canvas: tk.Canvas, series: list,
              title: str = "", yzero: bool = False) -> None:
        """极简 Canvas 绘图：自带坐标缩放与图例。

        series 元素:
          ("scatter", xs, ys, color, label)
          ("line",    xs, ys, color, label)
          ("band",    xs, lo, up, color, label)  填充区间
          ("area",    xs, base, top, color, label) 填充到基线
          ("hline",   xs, y,  color, label)       水平参考线
        """
        canvas.delete("all")
        w = max(10, canvas.winfo_width())
        h = max(10, canvas.winfo_height())
        if w < 50 or h < 50:
            # 尚未布局完成，稍后重画
            canvas.after(80, lambda: self._plot(canvas, series, title, yzero))
            return

        pad_l, pad_r, pad_t, pad_b = 58, 14, 22, 34
        pw = w - pad_l - pad_r
        ph = h - pad_t - pad_b
        if pw <= 10 or ph <= 10:
            return

        # 数据范围
        all_x = [v for s in series if s[0] in ("scatter", "line", "band", "area")
                 for v in s[1]]
        all_y = []
        for s in series:
            if s[0] == "scatter" or s[0] == "line":
                all_y.extend(s[2])
            elif s[0] == "band":
                all_y.extend(s[2]); all_y.extend(s[3])
            elif s[0] == "area":
                all_y.extend(s[2]); all_y.extend(s[3])
            elif s[0] == "hline":
                all_y.append(s[2])
        if not all_x or not all_y:
            return

        x_min, x_max = min(all_x), max(all_x)
        if yzero:
            y_min = 0.0
        else:
            y_min = min(all_y)
        y_max = max(all_y)
        if x_max - x_min < 1e-12:
            x_max = x_min + 1.0
        pad_y = (y_max - y_min) * 0.06 or 1.0
        y_min -= pad_y
        y_max += pad_y

        def X(v):
            return pad_l + (v - x_min) / (x_max - x_min) * pw

        def Y(v):
            return pad_t + ph - (v - y_min) / (y_max - y_min) * ph

        # 网格与坐标轴
        for i in range(6):
            gy = y_min + (y_max - y_min) * i / 5
            y = Y(gy)
            canvas.create_line(pad_l, y, pad_l + pw, y,
                               fill="#e6e6e6", width=1)
            canvas.create_text(pad_l - 6, y, text=f"{gy:.4g}",
                               anchor=tk.E, font=("", 8), fill="#888888")
        for i in range(6):
            gx = x_min + (x_max - x_min) * i / 5
            x = X(gx)
            canvas.create_line(x, pad_t, x, pad_t + ph,
                               fill="#e6e6e6", width=1)
            canvas.create_text(x, pad_t + ph + 14, text=f"{gx:.4g}",
                               anchor=tk.N, font=("", 8), fill="#888888")
        canvas.create_rectangle(pad_l, pad_t, pad_l + pw, pad_t + ph,
                                outline="#cccccc", width=1)

        # 数据
        for s in series:
            kind = s[0]
            if kind == "scatter":
                _, xs, ys, color, _ = s
                for xv, yv in zip(xs, ys):
                    r = 1.6
                    canvas.create_oval(X(xv) - r, Y(yv) - r,
                                       X(xv) + r, Y(yv) + r,
                                       fill=color, outline=color)
            elif kind == "line":
                _, xs, ys, color, _ = s
                pts = []
                for xv, yv in zip(xs, ys):
                    pts.extend((X(xv), Y(yv)))
                if len(pts) >= 4:
                    canvas.create_line(*pts, fill=color, width=2,
                                       smooth=True)
            elif kind == "band":
                _, xs, lo, up, color, _ = s
                poly = []
                for xv, yv in zip(xs, up):
                    poly.extend((X(xv), Y(yv)))
                for xv, yv in reversed(list(zip(xs, lo))):
                    poly.extend((X(xv), Y(yv)))
                if len(poly) >= 6:
                    canvas.create_polygon(poly, fill=color,
                                          outline="", stipple="gray50")
            elif kind == "area":
                _, xs, base, top, color, _ = s
                poly = []
                for xv, yv in zip(xs, top):
                    poly.extend((X(xv), Y(yv)))
                for xv in reversed(list(xs)):
                    poly.extend((X(xv), Y(base)))
                if len(poly) >= 6:
                    canvas.create_polygon(poly, fill=color,
                                          outline="", stipple="gray50")
            elif kind == "hline":
                _, xs, yv, color, _ = s
                y = Y(yv)
                canvas.create_line(pad_l, y, pad_l + pw, y,
                                   fill=color, width=1, dash=(4, 3))

        # 图例
        lx = pad_l + 8
        ly = pad_t + 6
        for s in series:
            label = s[-1]
            if not label:
                continue
            color = s[-2]
            if s[0] == "scatter":
                canvas.create_oval(lx - 3, ly - 3, lx + 3, ly + 3,
                                   fill=color, outline=color)
            elif s[0] == "hline":
                canvas.create_line(lx - 6, ly, lx + 6, ly,
                                   fill=color, dash=(4, 3))
            else:
                canvas.create_line(lx - 6, ly, lx + 6, ly,
                                   fill=color, width=2)
            canvas.create_text(lx + 10, ly, text=label, anchor=tk.W,
                               font=("", 8), fill="#555555")
            ly += 14

        if title:
            canvas.create_text(pad_l + 4, pad_t - 12, text=title,
                               anchor=tk.W, font=("", 9, "bold"),
                               fill="#333333")

    # --------------------------------------------------------------
    # 渲染 / 报告
    # --------------------------------------------------------------
    def _render(self) -> None:
        if not self.points:
            messagebox.showinfo(APP_NAME, "请先载入数据", parent=self.root)
            return
        p = self._params()
        self._set_status("渲染中…")

        def work():
            try:
                path = self.client.render_band(
                    self.session_id, n_segments=p["n_segments"],
                    degree=p["degree"], step=p["step"])
                self.root.after(0, lambda: self._on_render_done(path))
            except KernelClientError as e:
                self.root.after(0, lambda: self._on_calc_error(e))
            except Exception as e:
                self.root.after(0, lambda: self._on_calc_error(
                    KernelClientError(f"{type(e).__name__}: {e}")))

        threading.Thread(target=work, daemon=True).start()

    def _on_render_done(self, path: str) -> None:
        self.last_png = path
        self._set_status(f"图像已生成: {path}")
        # 用系统默认程序打开
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except AttributeError:
            for opener in ("xdg-open", "open"):
                try:
                    import subprocess
                    subprocess.Popen([opener, path])
                    break
                except Exception:
                    continue

    def _make_report(self) -> None:
        try:
            path = self.client.report(self.session_id)
        except KernelClientError as e:
            messagebox.showerror(APP_NAME, str(e), parent=self.root)
            return
        self.last_report = path
        self._set_status(f"报告已生成: {path}")
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except AttributeError:
            pass


def _poly(coeffs, x: float) -> float:
    """低次在前的系数求值。"""
    acc = 0.0
    for c in reversed(coeffs):
        acc = acc * float(x) + c
    return acc
