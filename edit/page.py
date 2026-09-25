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
import tkinter as tk
import threading
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from core.client import KernelClient, KernelClientError

APP_NAME = "分段包络估计"
PAGE_TAG = "band"

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
        ttk.Button(left, text="渲染图像", command=self._render).pack(
            fill=tk.X, pady=3)
        ttk.Button(left, text="生成报告", command=self._make_report).pack(
            fill=tk.X, pady=3)
        ttk.Button(left, text="重连内核", command=self._reconnect).pack(
            fill=tk.X, pady=3)

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
        right.rowconfigure(1, weight=3)
        right.rowconfigure(3, weight=2)

        # ---- 结果文本 ----
        res = ttk.LabelFrame(right, text="计算结果", padding=8)
        res.grid(row=0, column=0, sticky="nsew")
        self.result_text = tk.Text(res, height=10, wrap=tk.WORD,
                                   font=("Consolas", 10))
        sb = ttk.Scrollbar(res, orient=tk.VERTICAL,
                           command=self.result_text.yview)
        self.result_text.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.result_text.pack(fill=tk.BOTH, expand=True)

        # ---- 走势+包络图 ----
        img1 = ttk.LabelFrame(right, text="整体走势与离散区间", padding=4)
        img1.grid(row=1, column=0, sticky="nsew", pady=6)
        self.canvas1 = tk.Canvas(img1, bg="#fafafa", highlightthickness=0)
        self.canvas1.pack(fill=tk.BOTH, expand=True)

        # ---- 离散宽度图 ----
        img2 = ttk.LabelFrame(right, text="离散宽度 d(x)", padding=4)
        img2.grid(row=3, column=0, sticky="nsew", pady=(0, 6))
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

        return {
            "n_segments": _int(self.sp_seg, 8),
            "degree": _int(self.sp_deg, 1),
            "step": _int(self.sp_step, 3),
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
        threading.Thread(target=self._run_calc_worker,
                         args=(p,), daemon=True).start()

    def _run_calc_worker(self, p: dict) -> None:
        try:
            res = self.client.band_fit(self.session_id,
                                       n_segments=p["n_segments"],
                                       degree=p["degree"])
            self.root.after(0, lambda: self._on_calc_done(res, p))
        except KernelClientError as e:
            self.root.after(0, lambda: self._on_calc_error(e))
        except Exception as e:
            self.root.after(0, lambda: self._on_calc_error(
                KernelClientError(f"{type(e).__name__}: {e}")))

    def _on_calc_error(self, err) -> None:
        self.root.config(cursor="")
        self._set_status(f"计算失败: {err}")
        messagebox.showerror(APP_NAME, f"计算失败:\n{err}",
                             parent=self.root)

    def _on_calc_done(self, res: dict, p: dict) -> None:
        self.root.config(cursor="")
        self.result = res
        self._render_text(res)
        self._fill_tree(res)
        self._set_status(f"计算完成 | {res['expression']}")

        # 自动顺手把图画了
        try:
            self._draw_band()
            self._draw_dev()
        except Exception as e:
            self._set_status(f"曲线绘制失败: {e}")

    def _render_text(self, res: dict) -> None:
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
