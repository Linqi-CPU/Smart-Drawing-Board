"""智能绘图板 —— UI 层。

本文件只负责界面与交互调度：
    graph_engine  采样 / 自适应缩放（纯计算，无 tkinter 依赖）
    custom_loader 安全加载 functions/ 下的用户函数
    fitting       离散点自动建模/曲线拟合（纯计算，无 tkinter 依赖）

目录约定：
    main.py            本文件（UI）
    graph_engine.py    采样 / 自适应缩放
    custom_loader.py   自定义函数安全加载器
    fitting.py         离散点拟合引擎
    functions/         用户自定义函数目录
    tests/             单元与 GUI 测试
"""

from __future__ import annotations

import os
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# 计算内核：独立包，UI 层只通过它访问算法
from core import band_fit as bf
from core import custom_loader as cl
from core import data_import as di
from core import fitting as ft
from core import graph_engine as ge
from core.client import KernelClient, KernelClientError
import kernel_bridge

APP_NAME = "智能绘图板"
APP_VERSION = "2.2.0"

DEFAULT_WIDTH = 960
DEFAULT_HEIGHT = 720
MIN_WIDTH = 820
MIN_HEIGHT = 580

TAG_MANUAL = "manual_draw"
TAG_AUTO = "auto_draw"
TAG_GUIDE = "guide_line"
TAG_FIT_CURVE = "fit_curve"
TAG_FIT_POINT = "fit_point"

ANIMATION_INTERVAL_MS = 16
ANIMATION_POINTS_PER_TICK = 24

COLOR_MANUAL = "#1f6feb"
COLOR_AUTO = "#2ea043"
COLOR_LIMIT = "#d9534f"
COLOR_FIT = "#8250df"
COLOR_FIT_POINT = "#d4a017"

#: 采集模式下最多保留的离散点数（防止误操作拖出几万个点）
MAX_COLLECTED_POINTS = 400
#: 采样间隔：每 N 个运动事件记一个点
COLLECT_EVERY_N_EVENTS = 3


class DrawingApp:
    def __init__(self, root: tk.Tk, functions_dir: str | None = None):
        self.root = root
        self.functions_dir = cl.resolve_functions_dir(functions_dir)

        # ---------- 绘图状态 ----------
        self.is_drawing = False          # 手动绘制中
        self.is_auto_drawing = False     # 自动绘制中
        self.last_x: int | None = None
        self.last_y: int | None = None
        self.slide_distance = 0.0        # 累计滑动值
        self.y_limit = 300               # Y 值上限

        # ---------- 画布尺寸（随窗口实时更新） ----------
        self.canvas_width = 700
        self.canvas_height = 500

        # ---------- 自定义函数 ----------
        self.custom_func_name = ""       # 已验证通过的函数名
        self.custom_func = None          # 已验证通过的可调用对象

        # ---------- 离散点采集 / 自动建模 ----------
        self.collect_mode = False        # 是否处于"采集离散点"模式
        self.collected_points: list = []  # [(x, y), ...] 画布像素坐标
        self._motion_counter = 0
        self.fit_result = None           # 最近一次拟合成功的结果
        self._fit_candidates: list = []
        # Excel 导入的数据集与多变量拟合结果
        self.imported_data = None
        self.fit_multi_result = None
        self._multi_candidates: list = []
        # ---------- 动画队列 ----------
        self._anim_points: list = []
        self._anim_index = 0
        self._anim_job: str | None = None
        # ---------- 内核状态定时器（窗口关闭时必须停） ----------
        self._after_loop = None
        self._closing_down = False

        self._configure_window()
        self._build_menubar()
        self.setup_ui()
        self.populate_file_combobox()
        self.report_function_dir_status()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.bind("<Escape>", lambda e: self.stop_drawing())
        # 定时刷新内核/页面状态到状态栏。
        # 句柄必须登记进 _after_pending：取消时容易漏，漏掉它就会在
        # 窗口关闭后触发，刷 'invalid command name' 噪音。
        self._schedule_status_refresh(2000)

    # ==================================================================
    # 菜单栏：主 UI 只做"激活"，功能都在独立页面里
    # ==================================================================
    def _build_menubar(self) -> None:
        menubar = tk.Menu(self.root)

        # ---- 页面菜单：LAUNCHERS 里登记了什么，这里就自动出现什么 ----
        page_menu = tk.Menu(menubar, tearoff=0)
        for name, desc in kernel_bridge.LAUNCHERS.items():
            page_menu.add_command(
                label=f"{desc}",
                command=lambda n=name: self.open_page(n),
            )
        page_menu.add_separator()
        page_menu.add_command(label="查看在线页面…",
                              command=self.show_sessions)
        page_menu.add_separator()
        page_menu.add_command(label="关闭全部页面进程…",
                              command=self.close_all_pages)
        page_menu.add_command(label="回收孤儿页面状态",
                              command=self.reap_now)
        menubar.add_cascade(label="页面", menu=page_menu)

        # ---- 内核菜单 ----
        kernel_menu = tk.Menu(menubar, tearoff=0)
        kernel_menu.add_command(label="启动/检查内核",
                                command=self.ensure_kernel)
        kernel_menu.add_command(label="内核状态",
                                command=self.show_kernel_status)
        kernel_menu.add_separator()
        kernel_menu.add_command(label="打开内核日志",
                                command=self.open_kernel_log)
        menubar.add_cascade(label="内核", menu=kernel_menu)

        self.root.config(menu=menubar)

    def _refresh_kernel_status(self) -> None:
        """把内核与页面状态显示到状态栏末尾。

        自续期定时器：必须能在窗口销毁后**干净地停下**，否则 Tk 会
        对已销毁的 root 继续派发回调，刷 'invalid command name' 噪音。
        所以这里先查窗口是否还活着，并用 _after_loop 记住当前句柄，
        供 _stop_status_timer / on_close 取消。
        """
        if not self._alive():
            return
        try:
            txt = kernel_bridge.status_text()
            if hasattr(self, "kernel_label"):
                self.kernel_label.config(text=f" | {txt}")
        except Exception:
            pass
        if self._alive():
            self._schedule_status_refresh(4000)
        else:
            self._after_loop = None

    def _alive(self) -> bool:
        """窗口是否还活着。destroy() 后 winfo_exists() 返回 0。

        注意：这里**不能**把 _closing_down 当作"已死"的依据。
        dispose() 先用 _alive() 决定要不要 destroy()，如果把
        _closing_down 算进去，dispose() 一开始置位就让自己失去
        销毁能力——窗口永远关不掉。_closing_down 只用于让自续期
        定时器别再排下一轮，由 _status_loop 自己检查。
        """
        try:
            return bool(self.root.winfo_exists())
        except Exception:
            return False

    def _stop_status_timer(self) -> None:
        """取消**所有**挂起的状态刷新回调。

        幂等：重复调用、或回调已经自行跑完都安全。
        这样窗口关闭后 Tk 队列里不会再有任何指向 _refresh_kernel_status
        的条目，测试输出与 stderr 都不会再有那条噪音。

        为什么要用集合而不是单个 _after_loop：启动路径会在
        __init__（2000ms）、main() 启动钩子、以及每次刷新自排的
        4000ms 里各自 after() 一次。早期版只记最后一个句柄，
        结果先排的那些留在队列里，窗口销毁后照样触发 —— 这正是
        'invalid command name ..._refresh_kernel_status' 噪音的来源。
        集合保证"排过几轮就取消几轮"，不会漏。
        """
        handles = set(getattr(self, "_after_pending", set()))
        handles.discard(None)
        if not handles:
            self._after_pending = set()
            return
        for handle in handles:
            try:
                self.root.after_cancel(handle)
            except Exception:
                # 窗口已销毁 / 句柄已失效：本来就是要达成的状态
                pass
        self._after_pending = set()

    def _schedule_status_refresh(self, delay_ms: int) -> None:
        """排一轮状态刷新，并把句柄登记进 _after_pending。

        所有对 _refresh_kernel_status 的 after() 都必须走这里，
        否则 _stop_status_timer() 取消不到，销毁窗口后仍会触发。

        注意不要在这里 discard 上一次的句柄：自续期定时器会在回调
        执行时排下一轮，而"上一轮"的句柄可能还没来得及跑完取消
        （例如 4000ms 那轮还没到期就又排了一轮 2000ms 的）。
        一律只进不出，取消统一由 _stop_status_timer() 负责清空。
        """
        handle = self.root.after(delay_ms, self._refresh_kernel_status)
        if not hasattr(self, "_after_pending"):
            self._after_pending = set()
        self._after_pending.add(handle)
        # 保留 _after_loop 供旧断言/测试使用（指向最近一次）
        self._after_loop = handle
        return handle

    def ensure_kernel(self) -> None:
        started, msg = kernel_bridge.start_kernel()
        messagebox.showinfo(APP_NAME, msg, parent=self.root)

    def show_kernel_status(self) -> None:
        alive = kernel_bridge.kernel_alive()
        pages = kernel_bridge.list_pages()
        if alive:
            head = "计算内核: 运行中 (127.0.0.1:8765)"
        else:
            head = "计算内核: 未运行"
        if not pages:
            body = "当前没有在线页面。"
        else:
            lines = [f"共 {len(pages)} 个页面在线："]
            for p in pages:
                lines.append(
                    f"  {p['session_id']}  {p.get('source', '')}  "
                    f"点 {p['n_points']}  已算 {'是' if p['has_result'] else '否'}  "
                    f"闲置 {p['idle_seconds']:.0f}s")
            body = "\n".join(lines)
        messagebox.showinfo(APP_NAME, f"{head}\n\n{body}", parent=self.root)

    def open_page(self, name: str) -> None:
        ok, msg = kernel_bridge.launch_page(name)
        if ok:
            self.set_status(f"{msg}")
            self.root.after(1500, self.show_sessions)
        else:
            messagebox.showerror(APP_NAME, msg, parent=self.root)

    def show_sessions(self) -> None:
        # 窗口可能已被关掉（菜单点了"关闭全部页面"后 1.5 秒才到这里），
        # 没有存活检查就会对着已销毁的 root 弹窗，抛 TclError。
        if not self._alive():
            return
        pages = kernel_bridge.list_pages()
        if not pages:
            messagebox.showinfo(APP_NAME, "当前没有在线页面。",
                                parent=self.root)
            return
        lines = [f"共 {len(pages)} 个页面在线：", ""]
        for p in pages:
            state = "关闭中" if p.get("closing") else "运行中"
            lines.append(
                f"{p['session_id']}  [{p.get('source') or '-'}]  {state}\n"
                f"  进程 PID {p.get('pid') or '未知'}；"
                f"点数 {p['n_points']}，多变量行 {p['n_rows']}，"
                f"变量 {', '.join(p['variables']) or '-'}，"
                f"目标 {p['target']}\n"
                f"  已计算: {'是' if p['has_result'] else '否'}；"
                f"图片 {len(p['images'])} 张；闲置 {p['idle_seconds']:.0f}s"
            )
            if p["notes"]:
                lines.append(f"  最近消息: {p['notes'][-1]}")
            lines.append("")
        messagebox.showinfo(APP_NAME, "\n".join(lines), parent=self.root)

    def close_all_pages(self) -> None:
        """关闭所有在线页面进程。

        用于主 UI 退出前的收尾：逐个 terminate 页面进程，
        页面自己的 on_close 会负责清理自己的状态与文件。
        杀不动的（已卡死）才 escalate 到 kill。
        """
        pages = kernel_bridge.list_pages()
        if not pages:
            messagebox.showinfo(APP_NAME, "当前没有在线页面。",
                                parent=self.root)
            return
        if not messagebox.askyesno(
                APP_NAME,
                f"确定关闭全部 {len(pages)} 个页面进程吗？\n"
                f"每个页面会先自行清理状态与产物文件。",
                parent=self.root):
            return

        import subprocess
        ok, failed = [], []
        for p in pages:
            pid = p.get("pid")
            if not pid:
                continue
            try:
                if os.name == "nt":
                    r = subprocess.run(["taskkill", "/PID", str(pid)],
                                       capture_output=True, timeout=10)
                    (ok if r.returncode == 0 else failed).append(
                        f"{p['session_id']}({pid})")
                else:
                    import signal
                    os.kill(pid, signal.SIGTERM)
                    ok.append(f"{p['session_id']}({pid})")
            except Exception as e:
                failed.append(f"{p['session_id']}({pid}): {e}")

        msg = f"已请求关闭 {len(ok)} 个页面"
        if failed:
            msg += f"；失败 {len(failed)} 个: {', '.join(failed)}"
        messagebox.showinfo(APP_NAME, msg, parent=self.root)
        # 等内核收尾后再刷一次状态；窗口这时可能已经被关掉，
        # 所以包一层存活检查，否则又会在 stderr 刷噪音。
        self.root.after(1500, self._refresh_once_if_alive)

    def _refresh_once_if_alive(self) -> None:
        if self._alive():
            self._refresh_kernel_status()

    def reap_now(self) -> None:
        """手动触发一次内核巡检，回收已死页面的状态与文件。"""
        try:
            reaped = kernel_bridge.reap()
        except Exception as e:
            messagebox.showerror(APP_NAME, f"回收失败: {e}",
                                 parent=self.root)
            return
        if reaped:
            messagebox.showinfo(
                APP_NAME,
                f"已回收 {len(reaped)} 个孤儿页面：\n" + "\n".join(reaped),
                parent=self.root)
        else:
            messagebox.showinfo(APP_NAME, "没有发现孤儿页面。",
                                parent=self.root)
        self._refresh_kernel_status()

    def open_kernel_log(self) -> None:
        log = kernel_bridge.ROOT / ".kernel.log"
        if not log.exists():
            messagebox.showinfo(APP_NAME, "还没有内核日志。",
                                parent=self.root)
            return
        try:
            os.startfile(str(log))  # type: ignore[attr-defined]
        except Exception:
            messagebox.showinfo(APP_NAME, f"日志位置: {log}",
                                parent=self.root)

    # ==================================================================
    # 窗口与布局
    # ==================================================================
    def _configure_window(self) -> None:
        self.root.title(f"{APP_NAME} v{APP_VERSION}")
        self.root.geometry(f"{DEFAULT_WIDTH}x{DEFAULT_HEIGHT}")
        self.root.minsize(MIN_WIDTH, MIN_HEIGHT)

    def setup_ui(self) -> None:
        main_frame = ttk.Frame(self.root, padding=6)
        main_frame.pack(fill=tk.BOTH, expand=True)

        self._build_custom_panel(main_frame)
        self._build_control_panel(main_frame)
        self._build_canvas_area(main_frame)
        self._build_status_bar()

    def _build_custom_panel(self, parent: ttk.Frame) -> None:
        panel = ttk.LabelFrame(parent, text="自定义函数", width=252)
        panel.pack(side=tk.LEFT, fill=tk.Y, padx=4, pady=4)
        panel.pack_propagate(False)

        ttk.Label(panel, text="函数名称:").pack(anchor=tk.W, padx=5, pady=(5, 0))
        row = ttk.Frame(panel)
        row.pack(fill=tk.X, padx=5, pady=2)

        self.custom_func_name_var = tk.StringVar(value="my_func")
        ttk.Entry(row, textvariable=self.custom_func_name_var, width=13).pack(
            side=tk.LEFT, fill=tk.X, expand=True
        )

        self.file_combobox = ttk.Combobox(row, width=8, state="readonly")
        self.file_combobox.pack(side=tk.RIGHT)
        self.file_combobox.bind("<<ComboboxSelected>>", self.on_file_select)

        ttk.Label(panel, text="函数代码:").pack(anchor=tk.W, padx=5, pady=(4, 0))
        self.custom_code_text = tk.Text(
            panel, height=10, width=28, font=("Consolas", 9), wrap=tk.NONE
        )
        self.custom_code_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=2)
        self._insert_template(self.custom_func_name_var.get())

        hbar = ttk.Scrollbar(panel, orient=tk.HORIZONTAL,
                             command=self.custom_code_text.xview)
        hbar.pack(fill=tk.X, padx=5)
        self.custom_code_text.config(xscrollcommand=hbar.set)

        ttk.Label(
            panel,
            text=f"目录: {self.functions_dir}",
            foreground="#888888",
            wraplength=210,
            justify=tk.LEFT,
        ).pack(fill=tk.X, padx=5, pady=(6, 0))

        self._build_fit_panel(panel)

        btns = ttk.Frame(panel)
        btns.pack(side=tk.BOTTOM, fill=tk.X, padx=5, pady=6)
        ttk.Button(btns, text="保存函数", command=self.save_custom_function).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 2)
        )
        ttk.Button(btns, text="清除内容", command=self.clear_custom_function).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(2, 2)
        )
        ttk.Button(btns, text="打开目录", command=self.open_functions_dir).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(2, 0)
        )

    # ==================================================================
    # 自动建模面板（离散点拟合）
    # ==================================================================
    def _build_fit_panel(self, parent: ttk.Frame) -> None:
        box = ttk.LabelFrame(parent, text="自动建模（离散点拟合）")
        box.pack(fill=tk.BOTH, expand=False, padx=4, pady=(10, 4))

        ttk.Label(
            box,
            text="开启采集后，用鼠标在画布上画一条大致形状，"
                 "软件会自动拟合并输出函数与图像。",
            wraplength=210,
            justify=tk.LEFT,
            foreground="#666666",
        ).pack(fill=tk.X, padx=4, pady=(4, 2))

        # 采集开关
        self.collect_btn = ttk.Button(box, text="开启离散点采集",
                                      command=self.toggle_collect_mode)
        self.collect_btn.pack(fill=tk.X, padx=5, pady=3)

        self.collect_status_label = ttk.Label(
            box, text="已采集: 0 点", foreground="#666666"
        )
        self.collect_status_label.pack(fill=tk.X, padx=5)

        # 模型选择
        ttk.Label(box, text="模型阶数:").pack(anchor=tk.W, padx=5, pady=(6, 0))
        row = ttk.Frame(box)
        row.pack(fill=tk.X, padx=5, pady=2)
        self.fit_degree_var = tk.StringVar(value="auto")
        ttk.Radiobutton(row, text="自动", value="auto",
                        variable=self.fit_degree_var).pack(side=tk.LEFT)
        for d in (1, 2, 3, 4):
            ttk.Radiobutton(row, text=str(d), value=str(d),
                            variable=self.fit_degree_var).pack(side=tk.LEFT)
        ttk.Label(box, text="阶数越高越贴合数据，但需更多点才有意义",
                  wraplength=210, justify=tk.LEFT, foreground="#888888").pack(
            fill=tk.X, padx=5
        )

        # 拟合按钮
        self.fit_btn = ttk.Button(box, text="开始拟合", command=self.run_fit)
        self.fit_btn.pack(fill=tk.X, padx=5, pady=4)

        # 结果展示
        ttk.Label(box, text="拟合函数:").pack(anchor=tk.W, padx=5, pady=(4, 0))
        res_frame = ttk.Frame(box)
        res_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=2)
        res_frame.rowconfigure(0, weight=1)
        res_frame.columnconfigure(0, weight=1)
        self.fit_result_text = tk.Text(
            res_frame, height=6, font=("Consolas", 9), wrap=tk.WORD,
            state=tk.DISABLED, bg="#f6f8fa",
        )
        self.fit_result_text.grid(row=0, column=0, sticky="nsew")
        ttk.Scrollbar(res_frame, orient=tk.VERTICAL,
                      command=self.fit_result_text.yview).grid(
            row=0, column=1, sticky="ns"
        )
        self.fit_result_text.config(
            yscrollcommand=lambda f, l: self.fit_result_text.yview_moveto(f)
        )

        # 候选模型对比
        self.cand_label = ttk.Label(box, text="", wraplength=210,
                                    justify=tk.LEFT, foreground="#666666")
        self.cand_label.pack(fill=tk.X, padx=5, pady=(0, 4))

        # ---------- Excel 数据导入 ----------
        ttk.Separator(box, orient=tk.HORIZONTAL).pack(fill=tk.X, padx=5, pady=6)
        ttk.Label(box, text="Excel / 表格数据拟合:",
                 font=("", 9, "bold")).pack(anchor=tk.W, padx=5)
        ttk.Label(
            box,
            text="支持 .xlsx / .csv / .tsv / .txt\n"
                 "第一行是数据名，最后一列为 y",
            wraplength=210, justify=tk.LEFT, foreground="#888888",
        ).pack(fill=tk.X, padx=5)
        ttk.Button(box, text="导入数据并拟合",
                   command=self.run_excel_fit).pack(fill=tk.X, padx=5, pady=4)

        self.imported_label = ttk.Label(box, text="", wraplength=210,
                                       justify=tk.LEFT, foreground="#666666")
        self.imported_label.pack(fill=tk.X, padx=5, pady=(0, 4))

        btns = ttk.Frame(box)
        btns.pack(side=tk.BOTTOM, fill=tk.X, padx=5, pady=(0, 5))
        ttk.Button(btns, text="复制函数", command=self.copy_fit_expression).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 2)
        )
        ttk.Button(btns, text="清空数据点", command=self.clear_collected_points).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(2, 2)
        )
        ttk.Button(btns, text="保存报告", command=self.save_fit_report).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(2, 0)
        )

    def _build_control_panel(self, parent: ttk.Frame) -> None:
        panel = ttk.LabelFrame(parent, text="控制面板", width=190)
        panel.pack(side=tk.LEFT, fill=tk.Y, padx=4, pady=4)
        panel.pack_propagate(False)

        # --- 滑动值 ---
        box = ttk.LabelFrame(panel, text="滑动值计算")
        box.pack(fill=tk.X, padx=5, pady=5)
        self.slide_label = ttk.Label(box, text="滑动距离: 0.00",
                                     font=("Arial", 10, "bold"))
        self.slide_label.pack(pady=4)
        ttk.Button(box, text="重置滑动值", command=self.reset_slide).pack(pady=2)

        # --- Y 值上限 ---
        box = ttk.LabelFrame(panel, text="Y值上限设定")
        box.pack(fill=tk.X, padx=5, pady=5)
        self.y_limit_var = tk.IntVar(value=self.y_limit)
        spin = ttk.Spinbox(box, from_=20, to=2000, width=9,
                           textvariable=self.y_limit_var,
                           command=self.update_y_limit)
        spin.pack(pady=4)
        spin.bind("<Return>", lambda e: self.update_y_limit())
        spin.bind("<FocusOut>", lambda e: self.update_y_limit())
        self.y_limit_label = ttk.Label(box, text=f"当前上限: {self.y_limit}")
        self.y_limit_label.pack(pady=2)

        # --- 自动画线函数 ---
        box = ttk.LabelFrame(panel, text="自动画线函数")
        box.pack(fill=tk.X, padx=5, pady=5)

        ttk.Label(box, text="函数类型:").pack(anchor=tk.W, padx=5)
        self.func_type = tk.StringVar(value=ge.SINE)
        for text, value in [
            ("正弦波", ge.SINE),
            ("余弦波", ge.COSINE),
            ("抛物线", ge.PARABOLA),
            ("直线", ge.LINEAR),
            ("自定义", ge.CUSTOM),
        ]:
            ttk.Radiobutton(box, text=text, value=value,
                            variable=self.func_type).pack(anchor=tk.W, padx=10)

        ttk.Label(box, text="振幅:").pack(anchor=tk.W, padx=5, pady=(6, 0))
        self.amplitude_var = tk.DoubleVar(value=100)
        amp_row = ttk.Frame(box)
        amp_row.pack(fill=tk.X, padx=5)
        ttk.Scale(amp_row, from_=5, to=400, variable=self.amplitude_var,
                  orient=tk.HORIZONTAL).pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.amp_value_label = ttk.Label(amp_row, text="100", width=4)
        self.amp_value_label.pack(side=tk.LEFT)
        self.amplitude_var.trace_add("write", self._on_amp_change)

        ttk.Label(box, text="频率:").pack(anchor=tk.W, padx=5, pady=(6, 0))
        self.frequency_var = tk.DoubleVar(value=2)
        freq_row = ttk.Frame(box)
        freq_row.pack(fill=tk.X, padx=5)
        ttk.Scale(freq_row, from_=0.1, to=10, variable=self.frequency_var,
                  orient=tk.HORIZONTAL).pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.freq_value_label = ttk.Label(freq_row, text="2.0", width=4)
        self.freq_value_label.pack(side=tk.LEFT)
        self.frequency_var.trace_add("write", self._on_freq_change)

        ttk.Label(box, text="动画速度:").pack(anchor=tk.W, padx=5, pady=(6, 0))
        self.speed_var = tk.IntVar(value=ANIMATION_POINTS_PER_TICK)
        speed_row = ttk.Frame(box)
        speed_row.pack(fill=tk.X, padx=5)
        ttk.Scale(speed_row, from_=1, to=80, variable=self.speed_var,
                  orient=tk.HORIZONTAL).pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.speed_label = ttk.Label(speed_row, text="24", width=4)
        self.speed_label.pack(side=tk.LEFT)
        self.speed_var.trace_add("write", self._on_speed_change)

        # --- 操作按钮 ---
        btns = ttk.Frame(panel)
        btns.pack(fill=tk.X, padx=5, pady=8)
        ttk.Button(btns, text="清除按钮", command=self.clear_canvas).pack(
            fill=tk.X, pady=2
        )
        ttk.Button(btns, text="自动按钮", command=self.auto_draw).pack(
            fill=tk.X, pady=2
        )
        ttk.Button(btns, text="停止绘画", command=self.stop_drawing).pack(
            fill=tk.X, pady=2
        )
        ttk.Separator(btns, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=6)
        ttk.Button(btns, text="导出 PNG", command=self.export_png).pack(
            fill=tk.X, pady=2
        )
        ttk.Button(btns, text="帮助", command=self.show_help).pack(
            fill=tk.X, pady=2
        )

    def _build_canvas_area(self, parent: ttk.Frame) -> None:
        frame = ttk.Frame(parent)
        frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=4, pady=4)

        self.canvas = tk.Canvas(
            frame, width=self.canvas_width, height=self.canvas_height,
            bg="white", cursor="cross", highlightthickness=1,
            highlightbackground="#cccccc",
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)

        self.canvas.bind("<Button-1>", self.start_draw)
        self.canvas.bind("<B1-Motion>", self.draw)
        self.canvas.bind("<ButtonRelease-1>", self.stop_draw)
        self.canvas.bind("<Configure>", self.on_canvas_resize)

        self.draw_y_limit_line()

    def _build_status_bar(self) -> None:
        # 底部容器：左侧主状态，右侧内核/页面状态
        bar = ttk.Frame(self.root)
        bar.pack(side=tk.BOTTOM, fill=tk.X)

        self.status_bar = ttk.Label(
            bar,
            text="就绪 | 左键拖动可手绘，选好函数后点击'自动按钮'",
            relief=tk.SUNKEN, anchor=tk.W, padding=(6, 2),
        )
        self.status_bar.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.kernel_label = ttk.Label(
            bar, text=" | 内核: 检测中",
            relief=tk.SUNKEN, anchor=tk.E, padding=(6, 2),
            foreground="#666666",
        )
        self.kernel_label.pack(side=tk.RIGHT)

    # ==================================================================
    # 状态栏 / 滑块数值显示
    # ==================================================================
    def set_status(self, text: str) -> None:
        self.status_bar.config(text=text)

    def update_slide_label(self) -> None:
        self.slide_label.config(text=f"滑动距离: {self.slide_distance:.2f}")

    def _on_amp_change(self, *_args) -> None:
        self.amp_value_label.config(text=f"{self.amplitude_var.get():.0f}")

    def _on_freq_change(self, *_args) -> None:
        self.freq_value_label.config(text=f"{self.frequency_var.get():.1f}")

    def _on_speed_change(self, *_args) -> None:
        self.speed_label.config(text=str(self.speed_var.get()))

    # ==================================================================
    # 画布引导线与尺寸
    # ==================================================================
    def draw_y_limit_line(self) -> None:
        """按当前画布真实宽高重绘 Y 上限线。

        必须在画布 <Configure> 之后调用：否则窗口拉伸后，线仍只覆盖旧宽度。
        """
        w, h, y = self.canvas_width, self.canvas_height, self.y_limit
        self.canvas.delete(TAG_GUIDE)
        self.canvas.create_line(0, y, w, y, fill=COLOR_LIMIT, dash=(6, 4),
                                width=1, tags=TAG_GUIDE)
        self.canvas.create_text(8, max(12, y - 8), text=f"Y上限: {y}", anchor=tk.W,
                                fill=COLOR_LIMIT, tags=TAG_GUIDE)

    def on_canvas_resize(self, event) -> None:
        if event.width <= 1 or event.height <= 1:
            return
        self.canvas_width = event.width
        self.canvas_height = event.height
        self.draw_y_limit_line()
        # 自动曲线按旧尺寸采样，缩放后位置已失真，直接作废避免误导
        if self.is_auto_drawing:
            self.stop_drawing()
            self.set_status("画布尺寸已改变，自动绘制已中止，请重新点击'自动按钮'")
        # 拟合曲线同理：数据点坐标是旧画布像素空间，缩放后已不匹配
        elif self.fit_result is not None:
            self.fit_result = None
            self._fit_candidates = []
            self.clear_fit_drawing()
            self.cand_label.config(text="")
            self.set_status(
                "画布尺寸已改变，拟合结果已作废，请重新点击'开始拟合'"
            )

    def update_y_limit(self) -> None:
        try:
            value = int(self.y_limit_var.get())
        except (ValueError, tk.TclError):
            value = self.y_limit
        self.y_limit = max(20, min(2000, value))
        self.y_limit_var.set(self.y_limit)
        self.y_limit_label.config(text=f"当前上限: {self.y_limit}")
        self.draw_y_limit_line()

    # ==================================================================
    # 手动绘制
    # ==================================================================
    def start_draw(self, event) -> None:
        if self.is_auto_drawing:
            self.stop_drawing()
        self.is_drawing = True
        self.last_x, self.last_y = event.x, event.y
        self._motion_counter = 0

        if self.collect_mode:
            # 采集模式下：起点就是一个数据点
            self._record_collected_point(event.x, event.y)
            self.set_status(f"采集中 | 起点 ({event.x}, {event.y})")
        else:
            self.set_status(f"绘制中 | 起点 ({event.x}, {event.y})")

    def draw(self, event) -> None:
        if not self.is_drawing or self.last_x is None or self.last_y is None:
            return

        dist = ((event.x - self.last_x) ** 2 + (event.y - self.last_y) ** 2) ** 0.5
        self.slide_distance += dist

        # 坐标夹到画布内，避免拖到窗口外产生超长线段
        x0 = max(0, min(self.canvas_width, self.last_x))
        y0 = max(0, min(self.canvas_height, self.last_y))
        x1 = max(0, min(self.canvas_width, event.x))
        y1 = max(0, min(self.canvas_height, event.y))

        if self.collect_mode:
            # 采集模式：不画手绘线，只按固定间隔记点，避免点过密
            self.canvas.create_line(
                x0, y0, x1, y1, fill=COLOR_FIT_POINT, width=1,
                dash=(2, 2), tags=TAG_FIT_POINT,
            )
            self._motion_counter += 1
            if self._motion_counter >= COLLECT_EVERY_N_EVENTS:
                self._motion_counter = 0
                self._record_collected_point(x1, y1)
            self.set_status(
                f"采集中 | 坐标: ({event.x}, {event.y}) | "
                f"已采集 {len(self.collected_points)} 点"
            )
        else:
            self.canvas.create_line(x0, y0, x1, y1, fill=COLOR_MANUAL, width=2,
                                    capstyle=tk.ROUND, tags=TAG_MANUAL)
            self.set_status(
                f"绘制中 | 坐标: ({event.x}, {event.y}) | "
                f"滑动值: {self.slide_distance:.2f}"
            )

        self.last_x, self.last_y = event.x, event.y
        self.update_slide_label()

    def stop_draw(self, event=None) -> None:
        self.is_drawing = False
        self.last_x = self.last_y = None
        if self.collect_mode:
            self.set_status(
                f"采集结束 | 共 {len(self.collected_points)} 点，"
                "点击'开始拟合'生成函数与图像"
            )
        elif not self.is_auto_drawing:
            self.set_status(f"绘制结束 | 总滑动值: {self.slide_distance:.2f}")

    def reset_slide(self) -> None:
        self.slide_distance = 0.0
        self.update_slide_label()
        self.set_status("滑动值已重置")

    def clear_canvas(self) -> None:
        """中断一切绘制并清空画布（保留 Y 上限线）。

        注意必须连 TAG_FIT_POINT 一起删：采集模式下鼠标拖出来的
        散点线用的就是 TAG_FIT_POINT，早期版本只删了手绘/自动两类，
        结果点【清除按钮】散点还留在画布上——用户视角就是"清除失灵"。
        """
        self.is_drawing = False
        self._cancel_animation()
        self.is_auto_drawing = False
        self.last_x = self.last_y = None

        self.canvas.delete(TAG_MANUAL)
        self.canvas.delete(TAG_AUTO)
        self.canvas.delete(TAG_FIT_POINT)
        self.canvas.delete(TAG_FIT_CURVE)

        self.slide_distance = 0.0
        self.update_slide_label()
        self.draw_y_limit_line()
        self.set_status("画板已清除")
    # ==================================================================
    # 自动绘制
    # ==================================================================
    def auto_draw(self) -> None:
        """中断并清除，然后按选定函数自动绘制。"""
        self.clear_canvas()

        func_type = self.func_type.get()
        amp = self.amplitude_var.get()
        freq = self.frequency_var.get()

        custom_func = None
        if func_type == ge.CUSTOM:
            custom_func = self._ensure_custom_func_loaded()
            if custom_func is None:
                return

        try:
            points = ge.sample_curve(
                func_type=func_type,
                func=custom_func,
                width=self.canvas_width,
                height=self.canvas_height,
                amp=amp,
                freq=freq,
                step=2,
            )
        except Exception as e:
            self.set_status(f"自动绘计算出失败: {e}")
            messagebox.showerror(APP_NAME, f"计算曲线失败:\n{e}")
            return

        if len(points) < 2:
            self.set_status("画布尺寸过小，无法绘制")
            return

        p0 = points[0]
        self.canvas.create_oval(p0.screen_x - 3, p0.screen_y - 3,
                                p0.screen_x + 3, p0.screen_y + 3,
                                fill=COLOR_AUTO, outline=COLOR_AUTO,
                                tags=TAG_AUTO)

        self.slide_distance = ge.polyline_length(points)
        self.update_slide_label()

        self.is_auto_drawing = True
        self._anim_points = points
        self._anim_index = 1
        self.set_status(f"自动绘画中... | 目标滑动值: {self.slide_distance:.2f}")
        self._anim_job = self.root.after(ANIMATION_INTERVAL_MS, self._anim_step)

    def _anim_step(self) -> None:
        if not self.is_auto_drawing:
            self._anim_job = None
            return

        pts = self._anim_points
        speed = max(1, self.speed_var.get())
        end = min(len(pts), self._anim_index + speed)

        # 一次性批量 create_line：不再逐点 after 调度，避免卡顿
        coords = []
        for i in range(self._anim_index, end):
            coords.extend((pts[i - 1].screen_x, pts[i - 1].screen_y,
                           pts[i].screen_x, pts[i].screen_y))
        if coords:
            self.canvas.create_line(*coords, fill=COLOR_AUTO, width=2,
                                    capstyle=tk.ROUND, tags=TAG_AUTO)

        self._anim_index = end
        if self._anim_index >= len(pts):
            self._anim_job = None
            self.is_auto_drawing = False
            self.set_status(f"自动绘画完成 | 滑动值: {self.slide_distance:.2f}")
            return

        self._anim_job = self.root.after(ANIMATION_INTERVAL_MS, self._anim_step)

    def _cancel_animation(self) -> None:
        if self._anim_job is not None:
            try:
                self.root.after_cancel(self._anim_job)
            except (ValueError, tk.TclError):
                pass
            self._anim_job = None
        self._anim_points = []
        self._anim_index = 0

    def stop_drawing(self) -> None:
        """停止所有绘画。"""
        self.is_drawing = False
        self._cancel_animation()
        self.is_auto_drawing = False
        self.last_x = self.last_y = None
        self.set_status(f"绘画已停止 | 滑动值: {self.slide_distance:.2f}")

    # ==================================================================
    # 自定义函数管理
    # ==================================================================
    def _insert_template(self, func_name: str) -> None:
        self.custom_code_text.delete("1.0", tk.END)
        self.custom_code_text.insert("1.0", cl.function_source_template(func_name))

    def _ensure_custom_func_loaded(self):
        """确保自定义函数可用；不可用时提示并返回 None。"""
        name = self.custom_func_name_var.get().strip()
        if not name:
            self.set_status("错误: 请先设置自定义函数名称")
            messagebox.showwarning(APP_NAME, "请先填写自定义函数名称")
            return None

        if self.custom_func is not None and self.custom_func_name == name:
            return self.custom_func

        path = self.functions_dir / f"{name}.py"
        try:
            lf = cl.load_function(path)
        except cl.FunctionValidationError as e:
            self.set_status(f"函数加载失败: {e}")
            messagebox.showerror(APP_NAME, f"无法加载函数 '{name}':\n{e}")
            return None
        except Exception as e:
            self.set_status(f"函数加载失败: {e}")
            messagebox.showerror(APP_NAME, f"加载函数 '{name}' 时发生未知错误:\n{e}")
            return None

        self.custom_func_name = lf.name
        self.custom_func = lf.func
        self.set_status(f"已加载自定义函数 '{lf.name}'")
        return lf.func

    def save_custom_function(self) -> None:
        name = self.custom_func_name_var.get().strip()
        if not name:
            self.set_status("错误: 函数名称不能为空")
            messagebox.showwarning(APP_NAME, "函数名称不能为空")
            return
        if not name.isidentifier():
            self.set_status("错误: 函数名必须是合法 Python 标识符")
            messagebox.showwarning(
                APP_NAME,
                f"'{name}' 不是合法的 Python 标识符。\n"
                "函数名只能由字母、数字、下划线组成，且不能以数字开头。",
            )
            return

        code = self.custom_code_text.get("1.0", tk.END).strip()
        if not code:
            self.set_status("错误: 函数代码不能为空")
            messagebox.showwarning(APP_NAME, "函数代码不能为空")
            return

        try:
            self.functions_dir.mkdir(parents=True, exist_ok=True)
            target = self.functions_dir / f"{name}.py"
            target.write_text(code + "\n", encoding="utf-8")
        except Exception as e:
            self.set_status(f"保存失败: {e}")
            messagebox.showerror(APP_NAME, f"保存失败:\n{e}")
            return

        self.populate_file_combobox()
        self.file_combobox.set(name)

        # 保存后立刻校验，坏代码当场反馈而不是等到点"自动按钮"才炸
        try:
            lf = cl.load_function(target)
            self.custom_func_name = lf.name
            self.custom_func = lf.func
            self.set_status(f"已保存并验证通过: {target.name}")
        except cl.FunctionValidationError as e:
            self._invalidate_custom_func_cache()
            messagebox.showwarning(
                APP_NAME,
                f"文件已保存，但函数尚未通过校验:\n{e}\n\n"
                "修正后再次点击'保存函数'即可。",
            )

    def clear_custom_function(self) -> None:
        name = self.custom_func_name_var.get().strip() or "my_func"
        self._insert_template(name)
        self.set_status("编辑区已重置为默认模板")

    def populate_file_combobox(self) -> None:
        names = cl.list_function_names(self.functions_dir)
        self.file_combobox["values"] = names
        if names:
            self.file_combobox.current(0)

    def on_file_select(self, _event=None) -> None:
        name = self.file_combobox.get()
        if not name:
            return
        path = self.functions_dir / f"{name}.py"
        try:
            content = path.read_text(encoding="utf-8")
        except Exception as e:
            self.set_status(f"读取文件失败: {e}")
            messagebox.showerror(APP_NAME, f"读取 {path} 失败:\n{e}")
            return

        self.custom_func_name_var.set(name)
        self.custom_code_text.delete("1.0", tk.END)
        self.custom_code_text.insert("1.0", content)
        self._invalidate_custom_func_cache()
        self.set_status(f"已加载文件 '{path.name}'（保存后即可用于绘制）")

    def _invalidate_custom_func_cache(self) -> None:
        """清空已加载函数缓存，强制下次使用时重新校验。"""
        self.custom_func_name = ""
        self.custom_func = None

    def report_function_dir_status(self) -> None:
        """启动时汇总目录里无法加载的文件，避免用户不知道为什么画不出来。"""
        if not self.functions_dir.is_dir():
            self.set_status(f"自定义函数目录不存在，将自动创建: {self.functions_dir}")
            return
        _, errors = cl.load_all(self.functions_dir)
        if not errors:
            return
        detail = "\n".join(f"· {e}" for e in errors[:4])
        if len(errors) > 4:
            detail += f"\n· ...等共 {len(errors)} 个文件有问题"
        self.set_status(f"以下自定义函数无法加载:\n{detail}")

    def open_functions_dir(self) -> None:
        path = self.functions_dir
        try:
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(str(path))
        except Exception as e:
            messagebox.showerror(APP_NAME, f"无法打开目录:\n{e}")

    # ==================================================================
    # 自动建模：采集离散点 -> 拟合 -> 输出函数与图像
    # ==================================================================
    def toggle_collect_mode(self) -> None:
        """开启/关闭离散点采集。

        采集模式下手绘不再画线，而是记录 (x, y) 点，
        供后续拟合使用。
        """
        self.collect_mode = not self.collect_mode
        if self.collect_mode:
            self.collect_btn.config(text="关闭离散点采集")
            # 进入采集前先停掉自动绘制，避免画布上图案干扰
            if self.is_auto_drawing:
                self.stop_drawing()
            self.clear_fit_drawing()
            self._redraw_fit_points()   # 保留已有点的可见轨迹
            self.set_status(
                "离散点采集中 | 在画布上按住左键画一条大致曲线，"
                "松开后点击'开始拟合'"
            )
        else:
            self.collect_btn.config(text="开启离散点采集")
            self.set_status("已退出离散点采集模式")

    def _record_collected_point(self, x: int, y: int) -> None:
        """采集模式下记录一个点（带去重与上限保护）。"""
        if len(self.collected_points) >= MAX_COLLECTED_POINTS:
            return
        point = (float(x), float(y))
        if self.collected_points and self.collected_points[-1] == point:
            return
        self.collected_points.append(point)
        self._draw_fit_point_marker(x, y)
        self.collect_status_label.config(
            text=f"已采集: {len(self.collected_points)} 点"
        )

    def _draw_fit_point_marker(self, x: int, y: int) -> None:
        self.canvas.create_oval(
            x - 2, y - 2, x + 2, y + 2,
            fill=COLOR_FIT_POINT, outline=COLOR_FIT_POINT,
            tags=TAG_FIT_POINT,
        )

    def clear_collected_points(self) -> None:
        self.collected_points = []
        self.fit_result = None
        self._fit_candidates = []
        self._motion_counter = 0
        self.collect_status_label.config(text="已采集: 0 点")
        self.cand_label.config(text="")
        self._set_fit_result_text("")
        self.clear_fit_drawing()
        self.set_status("已清空离散点")

    def clear_fit_drawing(self) -> None:
        """只清除拟合相关的画布内容，不动手绘与自动绘线。

        注意：不要在删完之后立刻按 collected_points 重画标记 ——
        run_fit() 会先调这个方法再自己画一套，否则同一点会被画两遍。
        这里只保留"删除"语义；需要重画时由调用方显式调用 _redraw_fit_points()。
        """
        self.canvas.delete(TAG_FIT_CURVE)
        self.canvas.delete(TAG_FIT_POINT)

    def _redraw_fit_points(self) -> None:
        """把已采集的数据点标记重绘一遍（用于采集过程中的轨迹提示）。"""
        self.canvas.delete(TAG_FIT_POINT)
        for px, py in self.collected_points:
            self._draw_fit_point_marker(int(px), int(py))

    def _current_fit_degree(self):
        """返回用户选择的阶数；'auto' 返回 None。"""
        value = self.fit_degree_var.get()
        return None if value == "auto" else int(value)

    def run_fit(self) -> None:
        """执行拟合，并把函数表达式与图像显示出来。"""
        if len(self.collected_points) < 2:
            messagebox.showwarning(
                APP_NAME,
                "至少需要 2 个离散点。\n"
                "请先点击'开启离散点采集'，然后在画布上拖动鼠标画出大致形状。",
            )
            return

        points = list(self.collected_points)

        try:
            chosen = self._current_fit_degree()
            if chosen is None:
                result = ft.auto_fit(points)
            else:
                result = ft.fit_polynomial(points, chosen)
        except ft.FitError as e:
            self.set_status(f"拟合失败: {e}")
            messagebox.showerror(APP_NAME, f"无法完成拟合:\n{e}")
            return
        except Exception as e:
            self.set_status(f"拟合失败: {e}")
            messagebox.showerror(
                APP_NAME, f"拟合时发生未知错误:\n{type(e).__name__}: {e}"
            )
            return

        try:
            self._fit_candidates = ft.fit_candidates(points)
        except ft.FitError:
            self._fit_candidates = []
        self.fit_result = result

        try:
            display = ft.build_fit_display(
                result, points, self.canvas_width, self.canvas_height
            )
        except ft.FitError as e:
            self.set_status(f"绘制失败: {e}")
            messagebox.showerror(APP_NAME, f"拟合成功但无法绘制:\n{e}")
            return

        self.clear_fit_drawing()

        coords = []
        for p in display.curve:
            coords.extend((float(p.screen_x), p.screen_y))
        self.canvas.create_line(
            *coords, fill=COLOR_FIT, width=3, capstyle=tk.ROUND,
            tags=TAG_FIT_CURVE,
        )
        for p in display.points:
            self.canvas.create_oval(
                p.screen_x - 3, p.screen_y - 3,
                p.screen_x + 3, p.screen_y + 3,
                fill=COLOR_FIT_POINT, outline="#8a6d00", width=1,
                tags=TAG_FIT_POINT,
            )

        self._set_fit_result_text(result.summary)
        self._update_candidate_label(result)
        self.set_status(
            f"拟合完成 | {result.expression} | R²={result.r2:.4f} "
            f"RMSE={result.rmse:.3f}px"
        )

    def _set_fit_result_text(self, text: str) -> None:
        self.fit_result_text.config(state=tk.NORMAL)
        self.fit_result_text.delete("1.0", tk.END)
        if text:
            self.fit_result_text.insert("1.0", text)
        self.fit_result_text.config(state=tk.DISABLED)

    def _update_candidate_label(self, chosen) -> None:
        if not self._fit_candidates:
            self.cand_label.config(text="")
            return
        parts = []
        for c in self._fit_candidates:
            mark = "←选中" if c.degree == chosen.degree else ""
            parts.append(f"{c.degree}阶 R²={c.r2:.3f} {mark}".rstrip())
        self.cand_label.config(text="各阶对比：\n" + "\n".join(parts))

    def copy_fit_expression(self) -> None:
        if self.fit_multi_result is not None:
            text = self.fit_multi_result.summary
        elif self.fit_result is not None:
            text = self.fit_result.summary
        else:
            messagebox.showinfo(APP_NAME, "还没有拟合结果")
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
        except Exception as e:
            messagebox.showerror(APP_NAME, f"复制失败:\n{e}")
            return
        self.set_status("拟合结果已复制到剪贴板")

    def save_fit_report(self) -> None:
        if self.fit_multi_result is not None:
            self._save_multi_report()
            return
        if self.fit_result is None:
            messagebox.showinfo(APP_NAME, "还没有拟合结果")
            return

        path = filedialog.asksaveasfilename(
            parent=self.root,
            title="保存拟合报告",
            defaultextension=".txt",
            initialfile="fit_report.txt",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
        )
        if not path:
            return

        r = self.fit_result
        lines = [
            f"{APP_NAME} v{APP_VERSION} 自动建模报告",
            "=" * 46,
            "",
            f"拟合函数: {r.expression}",
            f"阶数:     {r.degree}",
            f"数据点数: {r.points_used}",
            f"R²:       {r.r2:.6f}",
            f"RMSE:     {r.rmse:.6f} px",
            "",
            "x 空间系数（低次在前）:",
        ]
        for i, c in enumerate(r.coefficients):
            lines.append(f"  a{i} = {c:.10g}")
        lines.append("")
        lines.append(
            f"归一化中心 c = {r.norm_center:.6g}    尺度 s = {r.norm_scale:.6g}"
        )
        lines.append("（t = (x - c) / s，拟合在归一化坐标 t 上进行以保证数值稳定）")
        lines.append("")

        if self._fit_candidates:
            lines.append("候选模型对比:")
            lines.append(f"  {'阶数':<6}{'R²':<12}{'RMSE(px)':<14}函数")
            for c in self._fit_candidates:
                mark = "  ← 选中" if c.degree == r.degree else ""
                lines.append(
                    f"  {c.degree:<6}{c.r2:<12.4f}{c.rmse:<14.4f}"
                    f"{c.expression}{mark}"
                )
            lines.append("")

        lines.append("离散点（画布像素坐标）:")
        for x, y in self.collected_points:
            lines.append(f"  ({x:.2f}, {y:.2f})")

        try:
            Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception as e:
            messagebox.showerror(APP_NAME, f"保存失败:\n{e}")
            return
        self.set_status(f"已保存报告: {path}")
        messagebox.showinfo(APP_NAME, f"报告已保存到:\n{path}")

    # ==================================================================
    # Excel / 表格数据导入拟合
    # ==================================================================
    def _save_multi_report(self) -> None:
        """导出多变量（Excel 导入）拟合报告。"""
        path = filedialog.asksaveasfilename(
            parent=self.root,
            title="保存拟合报告",
            defaultextension=".txt",
            initialfile="fit_report.txt",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
        )
        if not path:
            return

        r = self.fit_multi_result
        d = self.imported_data
        lines = [
            f"{APP_NAME} v{APP_VERSION} 自动建模报告（多变量）",
            "=" * 52,
            "",
            f"数据文件: {d.source.name if d else '未知'}",
            f"行数:     {r.rows_used}",
            f"变量数:   {len(r.variable)}",
            "",
            f"拟合函数: {r.expression}",
            f"R²:       {r.r2:.6f}",
            f"RMSE:     {r.rmse:.6g}",
            "",
            "各变量系数（单位 y / 单位 xi）:",
        ]
        for j, name in enumerate(r.variable):
            col = d.column(j) if d else []
            rng = f"    取值范围 [{min(col):g}, {max(col):g}]" if col else ""
            lines.append(f"  {name}: {r.coefficients[j + 1]:.10g}{rng}")
        lines.append(f"  常数项: {r.coefficients[0]:.10g}")

        if self._multi_candidates:
            lines.append("")
            lines.append("模型对比:")
            for label, res in self._multi_candidates:
                if isinstance(res, ft.FitError):
                    lines.append(f"  {label}: 无法拟合 ({res})")
                else:
                    lines.append(
                        f"  {label:<14} R²={res.r2:<10.4f} "
                        f"RMSE={res.rmse:<12.4g} {res.expression}"
                    )

        if d is not None:
            lines.append("")
            lines.append("原始数据:")
            header = "  " + "\t".join(str(v) for v in d.variables) + "\t" + d.target
            lines.append(header)
            for xs, y in d.rows[:200]:
                lines.append(
                    "  " + "\t".join(f"{v:g}" for v in xs) + f"\t{y:g}"
                )
            if d.n_rows > 200:
                lines.append(f"  ...（共 {d.n_rows} 行，仅导出前 200 行）")

        try:
            Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception as e:
            messagebox.showerror(APP_NAME, f"保存失败:\n{e}")
            return
        self.set_status(f"已保存报告: {path}")
        messagebox.showinfo(APP_NAME, f"报告已保存到:\n{path}")

    def run_excel_fit(self) -> None:
        """选文件 → 导入 → 多变量拟合 → 输出函数与各 xi 的贡献。"""
        path = filedialog.askopenfilename(
            parent=self.root,
            title="选择数据文件",
            filetypes=[
                ("Excel / 表格文件", "*.xlsx *.csv *.tsv *.txt"),
                ("Excel", "*.xlsx"),
                ("CSV", "*.csv"),
                ("文本", "*.txt *.tsv"),
                ("所有文件", "*.*"),
            ],
        )
        if not path:
            return

        try:
            data = di.import_file(path)
        except di.DataImportError as e:
            self.set_status(f"导入失败: {e}")
            messagebox.showerror(APP_NAME, f"无法导入该文件:\n{e}")
            return
        except Exception as e:
            self.set_status(f"导入失败: {e}")
            messagebox.showerror(
                APP_NAME, f"导入时发生未知错误:\n{type(e).__name__}: {e}"
            )
            return

        self.imported_data = data
        self.imported_label.config(text=data.describe())

        # --- 拟合 ---
        try:
            result = ft.fit_multi(data.rows, data.variables, data.target)
        except ft.FitError as e:
            self.set_status(f"拟合失败: {e}")
            self._set_fit_result_text(
                f"数据导入成功（{data.n_rows} 行），但无法拟合：\n{e}"
            )
            self.fit_result = None
            self._fit_candidates = []
            messagebox.showerror(APP_NAME, f"无法拟合:\n{e}")
            return
        except Exception as e:
            self.set_status(f"拟合失败: {e}")
            messagebox.showerror(
                APP_NAME, f"拟合时发生未知错误:\n{type(e).__name__}: {e}"
            )
            return

        self.fit_result = result
        self.fit_multi_result = result

        try:
            cands = ft.multi_candidates(data.rows, data.variables, data.target)
        except Exception:
            cands = []
        self._multi_candidates = cands

        # --- 输出：函数表达式 + 每个变量（xi）的贡献 ---
        lines = [result.expression, ""]
        lines.append("各变量系数与贡献：")
        names = list(data.variables)
        lines.append(f"  {'变量':<10}{'系数':>12}   {'取值范围':>22}")
        for j, name in enumerate(names):
            col = data.column(j)
            coef = result.coefficients[j + 1]
            lines.append(
                f"  {name:<10}{coef:>12.6g}   "
                f"[{min(col):g}, {max(col):g}]"
            )
        lines.append("")
        lines.append(f"R² = {result.r2:.6f}")
        lines.append(f"RMSE = {result.rmse:.6g}")
        lines.append(f"数据点 = {result.rows_used}")
        if len(cands) > 1:
            lines.append("")
            lines.append("各模型对比：")
            for label, res in cands:
                if isinstance(res, ft.FitError):
                    lines.append(f"  {label}: 无法拟合")
                else:
                    mark = "  ← 选中" if isinstance(result, ft.MultiFitResult) \
                        and res is result else ""
                    lines.append(
                        f"  {label:<14} R²={res.r2:<10.4f} {res.expression}{mark}"
                    )

        self._set_fit_result_text("\n".join(lines))
        self.cand_label.config(
            text=f"已导入: {data.n_rows} 行 × {data.n_vars} 变量\n"
                 f"{result.expression}"
        )
        self.set_status(
            f"拟合完成 | {result.expression} | R²={result.r2:.4f}"
        )

    # ==================================================================
    # 导出 / 帮助 / 关闭
    # ==================================================================
    def export_png(self) -> None:
        """把画布内容导出为 PNG（含 Y 上限线）。"""
        try:
            from PIL import ImageGrab
        except ImportError:
            messagebox.showerror(
                APP_NAME,
                "导出 PNG 需要 Pillow。\n请先安装:  pip install pillow",
            )
            return

        self.canvas.update_idletasks()
        x = self.canvas.winfo_rootx()
        y = self.canvas.winfo_rooty()
        w = self.canvas.winfo_width()
        h = self.canvas.winfo_height()
        if w < 2 or h < 2:
            messagebox.showwarning(APP_NAME, "画布尺寸异常，无法导出")
            return

        path = filedialog.asksaveasfilename(
            parent=self.root,
            title="导出为 PNG",
            defaultextension=".png",
            initialfile="drawing.png",
            filetypes=[("PNG 图片", "*.png"), ("所有文件", "*.*")],
        )
        if not path:
            return

        try:
            # 内缩 1px 补偿 highlightthickness=1 的边框
            img = ImageGrab.grab(bbox=(x + 1, y + 1, x + w - 1, y + h - 1))
            img.save(path, "PNG")
        except Exception as e:
            messagebox.showerror(APP_NAME, f"导出失败:\n{e}")
            return

        self.set_status(f"已导出: {path}")
        messagebox.showinfo(APP_NAME, f"已导出到:\n{path}")

    def show_help(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("Math 库帮助")
        win.geometry("640x500")
        win.minsize(440, 320)
        win.transient(self.root)

        top = ttk.Frame(win)
        top.pack(fill=tk.X, padx=8, pady=6)
        ttk.Label(top, text="搜索:").pack(side=tk.LEFT)
        search_var = tk.StringVar()
        entry = ttk.Entry(top, textvariable=search_var)
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        ttk.Button(top, text="清除", width=6,
                   command=lambda: search_var.set("")).pack(side=tk.LEFT)

        body = ttk.Frame(win)
        body.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)

        text = tk.Text(body, font=("Consolas", 10), wrap=tk.WORD)
        text.grid(row=0, column=0, sticky="nsew")
        bar = ttk.Scrollbar(body, orient=tk.VERTICAL, command=text.yview)
        bar.grid(row=0, column=1, sticky="ns")
        text.config(yscrollcommand=bar.set)

        text.tag_config("hit", background="#ffe58f")
        text.tag_config("sec", foreground="#0969da",
                        font=("Consolas", 10, "bold"))

        def render(keyword: str = "") -> None:
            text.config(state=tk.NORMAL)
            text.delete("1.0", tk.END)
            kw = keyword.strip().lower()
            if not kw:
                text.insert(tk.END, HELP_CONTENT)
                # 小节标题形如 "1. 常量" / "6. 自定义函数签名（本程序约定）"
                for i, line in enumerate(HELP_CONTENT.splitlines(), start=1):
                    s = line.strip()
                    if s and s[0].isdigit() and "." in s[:3] and len(s) < 40:
                        text.tag_add("sec", f"{i}.0", f"{i}.end")
            else:
                lines = HELP_CONTENT.splitlines()
                hits = 0
                i = 0
                while i < len(lines):
                    if kw in lines[i].lower():
                        hits += 1
                        start = max(0, i - 1)
                        end = min(len(lines), i + 2)
                        # 重建命中行上下文，命中行单独打标签
                        for j in range(start, end):
                            tag = ("hit",) if j == i else ()
                            text.insert(tk.END, lines[j] + "\n", tag)
                        text.insert(tk.END, "-" * 44 + "\n")
                        i = end
                    else:
                        i += 1
                if hits == 0:
                    text.insert(
                        tk.END,
                        f"没有找到包含 '{keyword}' 的内容。\n\n"
                        "试试这些关键词: math、sin、log、示例、签名、安全",
                    )
            text.config(state=tk.DISABLED)

        def on_search(*_a) -> None:
            render(search_var.get())

        entry.bind("<Return>", on_search)
        search_var.trace_add("write", on_search)

        render()
        entry.focus_set()
        win.bind("<Escape>", lambda e: win.destroy())

    def on_close(self) -> None:
        self.dispose()

    def dispose(self) -> None:
        """彻底关闭：停掉所有定时器与动画，再销毁窗口。

        测试和 on_close 都走这里。关键是先停自续期定时器
        （_closing_down + after_cancel），否则 destroy() 之后 Tk
        队列里残留的回调会继续指向一个已不存在的窗口，刷
        'invalid command name' 噪音。
        """
        # 先置位，让自续期定时器自己停下来，再销毁窗口
        self._closing_down = True
        self._stop_status_timer()
        if self._alive() and (self.is_auto_drawing or self.is_drawing):
            if not messagebox.askyesno(
                APP_NAME, "正在绘制中，确定要退出吗？", parent=self.root
            ):
                self._closing_down = False
                return
        self._cancel_animation()
        self._stop_status_timer()
        if self._alive():
            self.root.destroy()


HELP_CONTENT = """Math 库常用函数和常量
=======================

1. 常量
-------
math.pi       π（圆周率），约 3.14159
math.e        自然对数的底，约 2.71828
math.tau      2π，约 6.28318
math.inf      正无穷
math.nan      非数值

2. 三角函数（输入为弧度）
------------------------
math.sin(x)        正弦
math.cos(x)        余弦
math.tan(x)        正切
math.asin(x)       反正弦，返回弧度
math.acos(x)       反余弦，返回弧度
math.atan(x)       反正切，返回弧度
math.atan2(y, x)   根据坐标求角度
math.degrees(r)    弧度转角度
math.radians(d)    角度转弧度

3. 指数与对数
-------------
math.exp(x)        e 的 x 次方
math.log(x)        自然对数（以 e 为底）
math.log(x, b)     以 b 为底的对数
math.log2(x)       以 2 为底
math.log10(x)      以 10 为底
math.pow(x, y)     x 的 y 次方
math.sqrt(x)       x 的平方根
math.hypot(x, y)   sqrt(x²+y²)，滑动值计算同源

4. 取整与数值处理
-----------------
math.ceil(x)    向上取整
math.floor(x)   向下取整
math.trunc(x)   截断取整
math.fabs(x)    浮点绝对值
math.fmod(x, y)     浮点取模
math.isnan(x)   是否为 NaN
math.isinf(x)   是否为无穷

5. 组合与统计
-------------
math.factorial(n)   阶乘
math.comb(n, k)      组合数 C(n,k)
math.perm(n, k)      排列数 P(n,k)
math.gcd(a, b)       最大公约数
math.lcm(a, b)       最小公倍数

6. 自定义函数签名（本程序约定）
------------------------------
def my_func(x, width, center_y, amp, freq):
    x        当前 x 坐标（0 ~ width，像素）
    width    画布宽度（像素）
    center_y 画布垂直中心（像素）
    amp      振幅滑块当前值
    freq     频率滑块当前值
    return y 屏幕 y 坐标（像素，向下为正）

7. 示例
-------
# 归一化正弦波（推荐写法：任何画布宽度下比例一致）
def sine_norm(x, width, center_y, amp, freq):
    t = x / width
    return center_y + amp * math.sin(2 * math.pi * freq * t)

# 阻尼振荡：越往右振幅越小
def damped(x, width, center_y, amp, freq):
    t = x / width
    return center_y + amp * (1 - t) * math.sin(2 * math.pi * freq * t)

# 方波（用 sign 制造跳变）
def square(x, width, center_y, amp, freq):
    t = x / width
    s = math.sin(2 * math.pi * freq * t)
    v = 1.0 if s > 0 else (-1.0 if s < 0 else 0.0)
    return center_y + amp * v

# 螺旋线
def spiral(x, width, center_y, amp, freq):
    t = x / width
    return center_y + (amp * t) * math.sin(freq * 2 * math.pi * t)

8. 安全限制
-----------
自定义函数只允许 import: math, cmath, random, statistics,
fractions, decimal, numpy
不允许使用: open / eval / exec / __import__ / 私有属性访问等。
"""


def main(argv: list | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    workdir: str | None = None
    if "--workdir" in argv:
        i = argv.index("--workdir")
        if i + 1 >= len(argv):
            print("错误: --workdir 需要一个目录参数", file=sys.stderr)
            return 2
        workdir = argv[i + 1]
        try:
            os.chdir(workdir)
        except OSError as e:
            print(f"错误: 无法切换到工作目录 {workdir}: {e}", file=sys.stderr)
            return 2

    if "--version" in argv or "-V" in argv:
        print(f"{APP_NAME} v{APP_VERSION}")
        return 0

    # 内核托管：主 UI 启动时确保计算内核在跑。
    # 内核是独立进程，主 UI 退出后它继续存活。
    kernel_ok, kernel_msg = kernel_bridge.start_kernel()
    if not kernel_ok:
        # 不能只往 stderr 打印：双击 exe 的用户看不到控制台，
        # 会以为"能开窗口就一切正常"，直到点计算才莫名失败。
        print(f"[kernel] {kernel_msg}", file=sys.stderr)

    root = tk.Tk()
    DrawingApp(root, functions_dir=workdir)
    if kernel_ok:
        root.after(1200, lambda: _announce_kernel_ready(root))
    else:
        # 延迟到 root 建好后再弹，否则没有父窗口；也不能阻塞启动。
        root.after(400, lambda: _warn_kernel_down(root, kernel_msg))
    root.mainloop()
    return 0


def _warn_kernel_down(root: tk.Tk, msg: str) -> None:
    """内核没起来时明确告知用户，而不是让他盲用到失败。"""
    try:
        messagebox.showwarning(
            APP_NAME,
            "计算内核启动失败，页面功能暂不可用。\n\n"
            f"原因：{msg}\n\n"
            "可以从菜单「内核 → 启动/检查内核」重试，"
            "或手动执行：\n    python -m core.server",
            parent=root,
        )
    except Exception:
        pass


def _announce_kernel_ready(root: tk.Tk) -> None:
    """内核就绪后，提示用户可以用独立页面。"""
    try:
        pages = kernel_bridge.LAUNCHERS
        if not pages:
            return
        detail = "\n".join(f"  • {name}：{desc}" for name, desc in pages.items())
        messagebox.showinfo(
            APP_NAME,
            f"计算内核已就绪。\n\n"
            f"可以在菜单「页面」中打开独立功能页面：\n{detail}\n\n"
            f"这些页面是独立进程，关掉本窗口后它们仍会继续运行。",
            parent=root,
        )
    except Exception:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
