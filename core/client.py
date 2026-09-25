"""内核客户端 —— UI 侧（主 UI 与 edit 页面）共用的轻量客户端。

只依赖标准库 urllib，不引入 requests。
所有方法返回解析后的 JSON dict；失败抛 KernelClientError。

用法
----
    from core.client import KernelClient

    k = KernelClient()                 # 默认连 127.0.0.1:8765
    sid = k.register(session_id=None)  # 不传则由内核分配
    res = k.band_fit(sid, points, n_segments=10, degree=1)
    print(res["result"]["expression"])
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_HOST = os.environ.get("HERMES_KERNEL_HOST", "127.0.0.1")
#: 端口统一由环境变量控制，代码里不写死。
#: 这样同一台机器可以同时跑多个内核（例如测试用 8804，正式用 8765），
#: UI、内核服务端、测试三方读同一个环境变量，不会出现"客户端连 8765、
#: 服务却起在 8804"的分歧。
DEFAULT_PORT = int(os.environ.get("HERMES_KERNEL_PORT", "8765"))


class KernelClientError(Exception):
    """内核调用失败（网络层或内核返回 ok=false）。"""


class KernelClient:
    """同步 HTTP 客户端。UI 调用时应放到子线程，避免阻塞界面。"""

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 timeout: float = 30.0):
        self.base = f"http://{host}:{port}"
        self.timeout = timeout

    # --------------------------------------------------------------
    # 底层
    # --------------------------------------------------------------
    def post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = self.base + path
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            # 内核返回的错误体通常是 JSON，尽量取出 message
            try:
                body = e.read().decode("utf-8")
                obj = json.loads(body)
                raise KernelClientError(obj.get("error") or str(e))
            except KernelClientError:
                raise
            except Exception:
                raise KernelClientError(f"HTTP {e.code}: {e.reason}")
        except urllib.error.URLError as e:
            raise KernelClientError(
                f"连不上内核 {url}（{e.reason}）。请确认内核服务已启动。"
            )
        except TimeoutError:
            raise KernelClientError(f"内核响应超时（{self.timeout}s）")
        try:
            obj = json.loads(body)
        except ValueError:
            raise KernelClientError(f"内核返回了非 JSON 内容: {body[:200]}")
        if not obj.get("ok", False):
            raise KernelClientError(obj.get("error", "未知错误"))
        return obj

    def get(self, path: str) -> Dict[str, Any]:
        try:
            with urllib.request.urlopen(self.base + path,
                                        timeout=self.timeout) as resp:
                obj = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as e:
            raise KernelClientError(
                f"连不上内核 {self.base}{path}（{e.reason}）"
            )
        if not obj.get("ok", False):
            raise KernelClientError(obj.get("error", "未知错误"))
        return obj

    def ping(self) -> bool:
        try:
            self.get("/api/ping")
            return True
        except KernelClientError:
            return False

    # --------------------------------------------------------------
    # 会话
    # --------------------------------------------------------------
    def register(self, session_id: Optional[str] = None,
                 source: str = "") -> str:
        payload: Dict[str, Any] = {}
        if session_id:
            payload["session_id"] = session_id
        if source:
            payload["source"] = source
        # 顺手带上自己的 PID：注册完成的那一刻内核就知道该用谁判断存活，
        # 不存在"已注册但未上报 PID"的窗口期。
        payload["pid"] = os.getpid()
        return self.post("/api/register", payload)["session_id"]

    def unregister(self, session_id: str) -> bool:
        return self.post("/api/unregister",
                         {"session_id": session_id}).get("removed", False)

    def sessions(self) -> List[dict]:
        return self.post("/api/sessions", {}).get("sessions", [])

    # --------------------------------------------------------------
    # 数据
    # --------------------------------------------------------------
    def save_points(self, session_id: str,
                    points: List[Tuple[float, float]],
                    source: str = "") -> int:
        return self.post("/api/save_points", {
            "session_id": session_id,
            "points": [list(p) for p in points],
            "source": source,
        }).get("saved", 0)

    def get_points(self, session_id: str) -> List[list]:
        return self.post("/api/get_points",
                         {"session_id": session_id}).get("points", [])

    def import_table(self, session_id: str, path: str) -> dict:
        return self.post("/api/import_table",
                         {"session_id": session_id, "path": path})

    # --------------------------------------------------------------
    # 计算
    # --------------------------------------------------------------
    def band_fit(self, session_id: str,
                 points: Optional[List[Tuple[float, float]]] = None,
                 n_segments: int = 8, degree: int = 2) -> dict:
        payload: Dict[str, Any] = {
            "session_id": session_id,
            "n_segments": n_segments,
            "degree": degree,
        }
        if points is not None:
            payload["points"] = [list(p) for p in points]
        return self.post("/api/band_fit", payload).get("result", {})

    def band_fit_improved(self, session_id: str,
                          points: Optional[List[Tuple[float, float]]] = None,
                          n_segments: int = 8, degree: int = 2) -> dict:
        payload: Dict[str, Any] = {
            "session_id": session_id,
            "n_segments": n_segments,
            "degree": degree,
        }
        if points is not None:
            payload["points"] = [list(p) for p in points]
        return self.post("/api/band_fit_improved", payload).get("result", {})

    def poly_fit(self, session_id: str,
                 points: Optional[List[Tuple[float, float]]] = None,
                 degree: int = 2) -> dict:
        payload: Dict[str, Any] = {
            "session_id": session_id, "degree": degree}
        if points is not None:
            payload["points"] = [list(p) for p in points]
        return self.post("/api/poly_fit", payload).get("result", {})

    def dev_series(self, session_id: str, step: int = 5,
                   points: Optional[List[Tuple[float, float]]] = None,
                   n_segments: int = 8, degree: int = 2) -> dict:
        payload: Dict[str, Any] = {
            "session_id": session_id, "step": step,
            "n_segments": n_segments, "degree": degree}
        if points is not None:
            payload["points"] = [list(p) for p in points]
        return self.post("/api/dev_series", payload)

    def band_series(self, session_id: str, step: int = 5,
                    points: Optional[List[Tuple[float, float]]] = None,
                    n_segments: int = 8, degree: int = 2) -> dict:
        payload: Dict[str, Any] = {
            "session_id": session_id, "step": step,
            "n_segments": n_segments, "degree": degree}
        if points is not None:
            payload["points"] = [list(p) for p in points]
        return self.post("/api/band_series", payload)

    def render_band(self, session_id: str, n_segments: int = 8,
                    degree: int = 2, step: int = 5,
                    points: Optional[List[Tuple[float, float]]] = None) -> str:
        payload: Dict[str, Any] = {
            "session_id": session_id, "n_segments": n_segments,
            "degree": degree, "step": step}
        if points is not None:
            payload["points"] = [list(p) for p in points]
        return self.post("/api/render_band", payload).get("path", "")

    def report(self, session_id: str) -> str:
        return self.post("/api/report",
                         {"session_id": session_id}).get("path", "")

    def note(self, session_id: str, text: str) -> List[str]:
        return self.post("/api/note", {
            "session_id": session_id, "text": text}).get("notes", [])

    # --------------------------------------------------------------
    # 生命周期管理
    # --------------------------------------------------------------
    def report_pid(self, session_id: str, pid: int) -> None:
        """页面上报自己的 PID，让内核能在页面消失后回收其残留。"""
        self.post("/api/report_pid",
                  {"session_id": session_id, "pid": pid})

    def mark_closing(self, session_id: str) -> None:
        self.post("/api/mark_closing", {"session_id": session_id})

    def cleanup(self, session_id: str) -> dict:
        """关闭前清理：删掉本页面的图片/报告，并注销状态。

        只影响调用方自己的 session_id，其他页面不受影响。
        内核不可达时返回空 dict（进程反正要退了，磁盘残留由巡检兜底）。
        """
        try:
            return self.post("/api/cleanup",
                             {"session_id": session_id})
        except KernelClientError:
            return {"dropped": False, "removed_files": [],
                    "n_removed": 0}

    def reap(self) -> List[str]:
        """手动触发一次孤儿回收，返回被回收的编号。"""
        try:
            return self.post("/api/reap", {}).get("reaped", [])
        except KernelClientError:
            return []


__all__ = ["KernelClient", "KernelClientError",
           "DEFAULT_HOST", "DEFAULT_PORT"]
