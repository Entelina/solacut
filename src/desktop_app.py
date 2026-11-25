"""Solacut 原生无边框桌面应用启动壳（FastAPI 后台线程 + pywebview Edge WebView2）。

特性：
1. 原生 Windows Edge WebView2 无边框窗口（`frameless=True`），零浏览器标签栏与地址栏干扰；
2. 完整支持自定义标题栏拖拽、双击最大化、Win32 原生 8 向边缘/角落缩放、工作区最大化（不遮挡任务栏）与 `F11` 真·无边框全屏（覆盖任务栏）；
3. 启动即自动注入多尺寸原生 `.ico` 图标至 WinForms 窗体与 Win32 任务栏句柄（`WM_SETICON`），任务栏 / Alt-Tab / 窗口左上角全端一致；
4. 后台守护线程托管 FastAPI 服务，自动探测空闲端口并健康检查就绪后再显示窗口，关闭窗口优雅停服零残留；
5. 保留 `--browser` 命令行参数随时切回普通浏览器开发调试。

命令行示例：
    pythonw src/desktop_app.py                  # 静默无黑窗启动原生桌面工作台（推荐）
    python src/desktop_app.py --fullscreen      # 直接以无边框全屏模式启动
    python src/desktop_app.py --browser         # 回退到默认浏览器打开调试
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import socket
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path
from typing import Any

_IS_FROZEN = bool(getattr(sys, "frozen", False))
_PROJECT_ROOT = (
    Path(sys.executable).resolve().parent
    if _IS_FROZEN
    else Path(__file__).resolve().parent.parent
)
_RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", _PROJECT_ROOT)) if _IS_FROZEN else _PROJECT_ROOT
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

BRAND_NAME = "Solacut"
_WINDOW_STATE_PATH = _PROJECT_ROOT / ".window_state.json"


def _ensure_stdio_streams() -> None:
    """在 Windows pythonw.exe 无控制台模式下为 None 的 stdout/stderr 挂载文件流，彻底防止 print/uvicorn 崩溃。"""
    if sys.stdout is not None and sys.stderr is not None:
        return
    log_path = _PROJECT_ROOT / "output" / ".desktop_runtime.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    stream = log_path.open("a", encoding="utf-8", errors="replace", buffering=1)
    if sys.stdout is None:
        sys.stdout = stream  # type: ignore[assignment]
    if sys.stderr is None:
        sys.stderr = stream  # type: ignore[assignment]


_ensure_stdio_streams()

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from src.config import redact_secrets_in_text  # noqa: E402
from src.step1_audio import terminate_active_media_processes  # noqa: E402
from src.web_app import app as fastapi_app  # noqa: E402

# Win32 原生非客户区点击消息常量（用于触发无延迟硬件缩放与 Aero 拖拽）
_WM_NCLBUTTONDOWN = 0x00A1
_HTCAPTION = 2
_EDGE_HIT_MAP: dict[str, int] = {
    "left": 10,         # HTLEFT
    "right": 11,        # HTRIGHT
    "top": 12,          # HTTOP
    "top-left": 13,     # HTTOPLEFT
    "top-right": 14,    # HTTOPRIGHT
    "bottom": 15,       # HTBOTTOM
    "bottom-left": 16,  # HTBOTTOMLEFT
    "bottom-right": 17, # HTBOTTOMRIGHT
}


def find_available_port(host: str = "127.0.0.1", preferred: int = 8765, max_tries: int = 40) -> int:
    """优先使用 preferred 端口，若已被占用则顺延寻找空闲本地端口，彻底避免端口冲突闪退。"""
    for offset in range(max_tries):
        candidate = preferred + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, candidate))
                return candidate
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def wait_for_backend_ready(url: str, timeout_sec: float = 12.0) -> bool:
    """轮询本地 FastAPI `/api/health` 确认后端就绪后再显示桌面窗口，杜绝初始白屏。"""
    health_url = f"{url.rstrip('/')}/api/health"
    deadline = time.perf_counter() + timeout_sec
    with httpx.Client(timeout=1.0) as client:
        while time.perf_counter() < deadline:
            try:
                resp = client.get(health_url)
                if resp.status_code == 200:
                    return True
            except Exception:
                pass
            time.sleep(0.08)
    return False


class EmbeddedBackendServer:
    """运行在后台线程中的可优雅停止 Uvicorn FastAPI 服务。"""

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self._config = uvicorn.Config(
            fastapi_app,
            host=host,
            port=port,
            log_level="warning",
            access_log=False,
            use_colors=False,
            log_config=None,
        )
        self._server = uvicorn.Server(self._config)
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._server.run,
            name="solacut-backend",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        terminate_active_media_processes()
        self._server.should_exit = True
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.5)


class DesktopWindowBridge:
    """暴露给前端 `window.pywebview.api` 的原生桌面窗口控制桥。"""

    def __init__(self, on_close_cb: Any = None, state_path: Path | None = None) -> None:
        self._window: Any = None
        self._on_close_cb = on_close_cb
        self._state_path: Path = state_path or _WINDOW_STATE_PATH
        self._is_maximized: bool = False
        self._is_fullscreen: bool = False
        self._saved_bounds: tuple[int, int, int, int] | None = None
        self._lock = threading.RLock()
        self._resize_hooked: bool = False

    def bind_window(self, window: Any, start_fullscreen: bool = False) -> None:
        self._window = window
        self._is_fullscreen = bool(start_fullscreen)

    def _run_on_ui_thread(self, fn: Any) -> Any:
        """在 Windows WinForms 主 UI 线程安全执行窗体操作。"""
        win = self._window
        if win is None:
            return None
        native = getattr(win, "native", None)
        if native is None:
            return fn(None)
        if getattr(native, "InvokeRequired", False):
            from System import Action

            result_box: dict[str, Any] = {}
            done = threading.Event()

            def _wrapped() -> None:
                try:
                    result_box["value"] = fn(native)
                finally:
                    done.set()

            native.BeginInvoke(Action(_wrapped))
            done.wait(timeout=3.0)
            return result_box.get("value")
        return fn(native)

    def _begin_on_ui_thread(self, fn: Any) -> None:
        """异步在 WinForms UI 线程投递非阻塞消息（专用于 SendMessage 拖拽与缩放，绝不卡死 JS Bridge）。"""
        win = self._window
        if win is None:
            return
        native = getattr(win, "native", None)
        if native is None:
            return
        if getattr(native, "InvokeRequired", False):
            from System import Action

            native.BeginInvoke(Action(lambda: fn(native)))
        else:
            fn(native)

    def _sync_native_state_from_form(self, native: Any) -> bool:
        """同步 WinForms 原生最大化状态（含 Aero Snap / Win+↑ 与 WorkingArea 铺满），返回 maximized 是否发生变化。"""
        if native is None or sys.platform != "win32" or self._is_fullscreen:
            return False
        if getattr(native, "IsDisposed", False) or getattr(native, "Disposing", False):
            return False
        try:
            import System.Windows.Forms as WinForms

            ws = native.WindowState
            if ws == WinForms.FormWindowState.Minimized:
                return False

            prev_max = self._is_maximized
            if ws == WinForms.FormWindowState.Maximized:
                self._is_maximized = True
                if self._saved_bounds is None:
                    rb = getattr(native, "RestoreBounds", None)
                    if rb is not None and int(rb.Width) >= 800 and int(rb.Height) >= 500:
                        self._saved_bounds = (
                            int(rb.X),
                            int(rb.Y),
                            int(rb.Width),
                            int(rb.Height),
                        )
            else:
                nx = int(native.Location.X)
                ny = int(native.Location.Y)
                nw = int(native.Size.Width)
                nh = int(native.Size.Height)
                area = WinForms.Screen.FromControl(native).WorkingArea
                is_workarea_max = (
                    abs(nx - int(area.X)) <= 4
                    and abs(ny - int(area.Y)) <= 4
                    and abs(nw - int(area.Width)) <= 4
                    and abs(nh - int(area.Height)) <= 4
                )
                if is_workarea_max:
                    self._is_maximized = True
                else:
                    self._is_maximized = False
                    if nw >= 800 and nh >= 500:
                        self._saved_bounds = (nx, ny, nw, nh)
            return self._is_maximized != prev_max
        except Exception:
            return False

    def _notify_frontend_window_state(self) -> None:
        """在后台线程安全将宿主最新窗口状态推送至前端（不阻塞 WinForms UI 消息泵）。"""
        win = self._window
        if win is None:
            return
        payload = {
            "is_desktop": True,
            "maximized": self._is_maximized,
            "fullscreen": self._is_fullscreen,
            "platform": sys.platform,
        }
        script = f"window.syncDesktopStateFromHost?.({json.dumps(payload)})"

        def _push() -> None:
            try:
                win.run_js(script)
            except Exception:
                pass

        threading.Thread(target=_push, name="solacut-ui-sync", daemon=True).start()

    def apply_initial_maximize_and_hooks(self, start_maximized: bool = False) -> None:
        """在窗口显示后应用首次/恢复最大化状态，并挂载 WinForms SizeChanged 监听实时同步系统 Snap 状态。"""
        win = self._window
        if win is None:
            return

        def _setup(native: Any) -> None:
            if native is None or sys.platform != "win32":
                if start_maximized and not self._is_fullscreen:
                    win.maximize()
                    self._is_maximized = True
                return

            import System.Windows.Forms as WinForms
            from System.Drawing import Point, Size

            if start_maximized and not self._is_fullscreen:
                if self._saved_bounds is None:
                    nx = int(native.Location.X)
                    ny = int(native.Location.Y)
                    nw = int(native.Size.Width)
                    nh = int(native.Size.Height)
                    if nw >= 800 and nh >= 500:
                        self._saved_bounds = (nx, ny, nw, nh)
                area = WinForms.Screen.FromControl(native).WorkingArea
                native.WindowState = WinForms.FormWindowState.Normal
                native.Location = Point(int(area.X), int(area.Y))
                native.Size = Size(int(area.Width), int(area.Height))
                self._is_maximized = True
                self._notify_frontend_window_state()

            if not self._resize_hooked:
                def _on_size_changed(_sender: Any, _evt: Any) -> None:
                    if self._sync_native_state_from_form(native):
                        self._notify_frontend_window_state()

                native.SizeChanged += _on_size_changed
                self._resize_hooked = True

        self._run_on_ui_thread(_setup)

    def get_window_state(self) -> dict[str, Any]:
        """返回当前桌面窗口状态供前端同步按钮图标与边框热区。"""
        win = self._window
        if win is not None and sys.platform == "win32" and getattr(win, "native", None) is not None:
            self._run_on_ui_thread(self._sync_native_state_from_form)
        return {
            "is_desktop": True,
            "maximized": self._is_maximized,
            "fullscreen": self._is_fullscreen,
            "platform": sys.platform,
        }

    def save_window_state(self, state_path: Path | None = None) -> dict[str, Any] | None:
        """采集并持久化当前非最小化窗口几何状态至 .window_state.json。"""
        win = self._window
        if win is None:
            return None

        target_path = state_path or self._state_path
        captured: dict[str, Any] = {}

        def _collect(native: Any) -> None:
            if native is not None and sys.platform == "win32":
                if getattr(native, "IsDisposed", False) or getattr(native, "Disposing", False):
                    if self._saved_bounds is not None:
                        sx, sy, sw, sh = self._saved_bounds
                        captured.update(
                            {"x": sx, "y": sy, "width": sw, "height": sh, "maximized": bool(self._is_maximized)}
                        )
                    return
                import System.Windows.Forms as WinForms

                ws = native.WindowState
                if ws == WinForms.FormWindowState.Minimized:
                    if self._saved_bounds is not None:
                        sx, sy, sw, sh = self._saved_bounds
                        captured.update(
                            {"x": sx, "y": sy, "width": sw, "height": sh, "maximized": bool(self._is_maximized)}
                        )
                    return

                self._sync_native_state_from_form(native)
                if self._is_maximized or self._is_fullscreen:
                    bounds = self._saved_bounds
                    if bounds is None:
                        rb = getattr(native, "RestoreBounds", None)
                        if rb is not None and int(rb.Width) >= 800 and int(rb.Height) >= 500:
                            bounds = (int(rb.X), int(rb.Y), int(rb.Width), int(rb.Height))
                    if bounds is not None:
                        sx, sy, sw, sh = bounds
                        captured.update(
                            {"x": sx, "y": sy, "width": sw, "height": sh, "maximized": bool(self._is_maximized)}
                        )
                else:
                    nx = int(native.Location.X)
                    ny = int(native.Location.Y)
                    nw = int(native.Size.Width)
                    nh = int(native.Size.Height)
                    if nw >= 800 and nh >= 500:
                        captured.update({"x": nx, "y": ny, "width": nw, "height": nh, "maximized": False})
                return

            # 跨平台或无原生句柄兜底
            if self._is_maximized and self._saved_bounds is not None:
                sx, sy, sw, sh = self._saved_bounds
                captured.update({"x": sx, "y": sy, "width": sw, "height": sh, "maximized": True})
                return
            wx = getattr(win, "x", None)
            wy = getattr(win, "y", None)
            ww = getattr(win, "width", None)
            wh = getattr(win, "height", None)
            if (
                _is_strict_int(ww)
                and _is_strict_int(wh)
                and ww >= 800
                and wh >= 500
            ):
                captured.update(
                    {
                        "x": wx if _is_strict_int(wx) else None,
                        "y": wy if _is_strict_int(wy) else None,
                        "width": ww,
                        "height": wh,
                        "maximized": bool(self._is_maximized),
                    }
                )
            elif self._saved_bounds is not None:
                sx, sy, sw, sh = self._saved_bounds
                captured.update(
                    {"x": sx, "y": sy, "width": sw, "height": sh, "maximized": bool(self._is_maximized)}
                )

        try:
            self._run_on_ui_thread(_collect)
        except Exception:
            return None

        if not captured:
            return None
        w = captured.get("width")
        h = captured.get("height")
        if not (_is_strict_int(w) and _is_strict_int(h) and 800 <= w <= 16384 and 500 <= h <= 16384):
            return None
        try:
            _write_window_state_atomic(captured, target_path)
            return captured
        except Exception:
            return None

    def minimize(self) -> dict[str, Any]:
        if self._window is not None:
            self._window.minimize()
        return self.get_window_state()

    def toggle_maximize(self) -> dict[str, Any]:
        """切换工作区最大化/还原（精准贴合 WorkingArea，绝不遮挡 Windows 任务栏）。"""
        win = self._window
        if win is None:
            return self.get_window_state()

        with self._lock:
            if self._is_fullscreen:
                win.toggle_fullscreen()
                self._is_fullscreen = False

            def _do_toggle(native: Any) -> None:
                if native is None or sys.platform != "win32":
                    if self._is_maximized:
                        win.restore()
                        self._is_maximized = False
                    else:
                        win.maximize()
                        self._is_maximized = True
                    return

                import System.Windows.Forms as WinForms
                from System.Drawing import Point, Size

                # 实时同步系统 Aero Snap / Win+Up 的真实最大化状态
                self._sync_native_state_from_form(native)

                if not self._is_maximized:
                    self._saved_bounds = (
                        int(native.Location.X),
                        int(native.Location.Y),
                        int(native.Size.Width),
                        int(native.Size.Height),
                    )
                    area = WinForms.Screen.FromControl(native).WorkingArea
                    native.WindowState = WinForms.FormWindowState.Normal
                    native.Location = Point(int(area.X), int(area.Y))
                    native.Size = Size(int(area.Width), int(area.Height))
                    self._is_maximized = True
                else:
                    native.WindowState = WinForms.FormWindowState.Normal
                    if self._saved_bounds is not None:
                        sx, sy, sw, sh = self._saved_bounds
                        native.Location = Point(sx, sy)
                        native.Size = Size(sw, sh)
                    else:
                        win.restore()
                    self._is_maximized = False

            self._run_on_ui_thread(_do_toggle)
        return self.get_window_state()

    def toggle_fullscreen(self) -> dict[str, Any]:
        """切换真·无边框全屏（覆盖任务栏）/ 退出全屏。"""
        win = self._window
        if win is None:
            return self.get_window_state()

        with self._lock:
            win.toggle_fullscreen()
            self._is_fullscreen = not self._is_fullscreen
            if self._is_fullscreen:
                self._is_maximized = False
        return self.get_window_state()

    def start_drag(self) -> dict[str, Any]:
        """触发 Windows 原生标题栏拖拽（若处于最大化态先轻柔还原再跟随鼠标拖动）。"""
        if sys.platform != "win32" or self._window is None or self._is_fullscreen:
            return self.get_window_state()

        def _do_drag(native: Any) -> None:
            if native is None:
                return
            import System.Windows.Forms as WinForms

            self._sync_native_state_from_form(native)
            if (self._is_maximized or native.WindowState == WinForms.FormWindowState.Maximized) and self._saved_bounds is not None:
                from System.Drawing import Point, Size

                cursor = WinForms.Cursor.Position
                area = WinForms.Screen.FromPoint(cursor).WorkingArea
                _, _, sw, sh = self._saved_bounds
                # 保留多屏负坐标副屏：按当前鼠标所在显示器的 WorkingArea 边界贴合，绝不用 max(0, ...) 拉回主屏
                new_x = max(int(area.X), min(int(area.Right - sw), int(cursor.X - sw * 0.5)))
                new_y = max(int(area.Y), min(int(area.Bottom - 60), int(cursor.Y - 18)))
                native.WindowState = WinForms.FormWindowState.Normal
                native.Location = Point(new_x, new_y)
                native.Size = Size(sw, sh)
                self._is_maximized = False

            hwnd = int(native.Handle.ToInt64())
            ctypes.windll.user32.ReleaseCapture()
            ctypes.windll.user32.SendMessageW(hwnd, _WM_NCLBUTTONDOWN, _HTCAPTION, 0)

        self._begin_on_ui_thread(_do_drag)
        return self.get_window_state()

    def start_resize(self, edge: str) -> dict[str, Any]:
        """触发 Windows 原生 8 向边框/角落拖拽缩放（60/120Hz 零延迟硬件缩放）。"""
        hit_code = _EDGE_HIT_MAP.get((edge or "").strip().lower())
        if hit_code is None or sys.platform != "win32" or self._window is None:
            return self.get_window_state()
        if self._is_fullscreen or self._is_maximized:
            return self.get_window_state()

        def _do_resize(native: Any) -> None:
            if native is None:
                return
            hwnd = int(native.Handle.ToInt64())
            ctypes.windll.user32.ReleaseCapture()
            ctypes.windll.user32.SendMessageW(hwnd, _WM_NCLBUTTONDOWN, hit_code, 0)

        self._begin_on_ui_thread(_do_resize)
        return self.get_window_state()

    def close(self) -> None:
        try:
            self.save_window_state()
        except Exception:
            pass
        if self._window is not None:
            self._window.destroy()
        if callable(self._on_close_cb):
            self._on_close_cb()


def _is_strict_int(val: Any) -> bool:
    return isinstance(val, int) and not isinstance(val, bool)


def _get_screen_working_areas() -> list[tuple[int, int, int, int]]:
    """返回当前所有显示器 WorkingArea 列表 [(x, y, right, bottom), ...]。"""
    if sys.platform != "win32":
        return []
    try:
        import System.Windows.Forms as WinForms

        areas: list[tuple[int, int, int, int]] = []
        for scr in WinForms.Screen.AllScreens:
            wa = scr.WorkingArea
            areas.append((int(wa.X), int(wa.Y), int(wa.Right), int(wa.Bottom)))
        return areas
    except Exception:
        return []


def _is_rect_visible_on_screens(x: int, y: int, width: int, height: int) -> bool:
    """校验窗口矩形是否至少部分位于任意有效显示器工作区内（支持多屏负坐标）。"""
    if width < 800 or height < 500 or width > 16384 or height > 16384:
        return False
    areas = _get_screen_working_areas()
    if not areas:
        return -32000 <= x <= 32000 and -32000 <= y <= 32000
    for ax, ay, aright, abottom in areas:
        overlap_w = min(x + width, aright) - max(x, ax)
        overlap_h = min(y + height, abottom) - max(y, ay)
        titlebar_visible = min(y + 48, abottom) > ay and y < abottom - 32
        if overlap_w >= 120 and overlap_h >= 60 and titlebar_visible:
            return True
    return False


def _load_window_state(state_path: Path | None = None) -> dict[str, Any] | None:
    """安全加载历史窗口几何状态，自动过滤损坏数据或脱离有效屏幕工作区的坐标。"""
    path = state_path or _WINDOW_STATE_PATH
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None

    width = raw.get("width")
    height = raw.get("height")
    if not (_is_strict_int(width) and _is_strict_int(height)):
        return None
    if width < 800 or height < 500 or width > 16384 or height > 16384:
        return None

    x = raw.get("x")
    y = raw.get("y")
    valid_pos: tuple[int | None, int | None] = (None, None)
    if _is_strict_int(x) and _is_strict_int(y):
        if _is_rect_visible_on_screens(x, y, width, height):
            valid_pos = (x, y)

    return {
        "x": valid_pos[0],
        "y": valid_pos[1],
        "width": max(1024, width),
        "height": max(640, height),
        "maximized": bool(raw.get("maximized", False)),
    }


def _write_window_state_atomic(payload: dict[str, Any], state_path: Path) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = state_path.with_name(f"{state_path.name}.tmp.{os.getpid()}.{threading.get_ident()}")
    try:
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, state_path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass


def _compute_initial_window_geometry() -> tuple[int, int, bool]:
    """根据当前主屏幕分辨率计算初始桌面窗口逻辑尺寸及是否自动铺满工作区。"""
    default_w, default_h = 1440, 860
    if sys.platform != "win32":
        return default_w, default_h, False
    try:
        user32 = ctypes.windll.user32
        screen_w = int(user32.GetSystemMetrics(0))
        screen_h = int(user32.GetSystemMetrics(1))
        if screen_w > 0 and screen_h > 0:
            if screen_w <= 1600 or screen_h <= 920:
                w = max(1120, int(screen_w * 0.92))
                h = max(680, int(screen_h * 0.90))
                return w, h, True
            w = max(1120, int(screen_w * 0.90))
            h = max(680, int(screen_h * 0.88))
            return w, h, False
    except Exception:
        pass
    return default_w, default_h, False


_WM_SETICON = 0x0080
_ICON_SMALL = 0
_ICON_BIG = 1
_IMAGE_ICON = 1
_LR_LOADFROMFILE = 0x0010


def _resolve_app_icon_path() -> Path | None:
    """按优先级解析应用多尺寸 .ico 图标路径（兼容源码运行与 PyInstaller 冻结便携包）。"""
    candidates = (
        _PROJECT_ROOT / "assets" / "brand" / "app_icon.ico",
        _RESOURCE_ROOT / "assets" / "brand" / "app_icon.ico",
        _PROJECT_ROOT / "web" / "brand" / "favicon.ico",
        _RESOURCE_ROOT / "web" / "brand" / "favicon.ico",
    )
    for path in candidates:
        if path.is_file():
            return path
    return None


def _apply_native_window_icon(bridge: DesktopWindowBridge, icon_path: Path | None) -> bool:
    """将应用 .ico 同步注入 Windows WinForms 窗体与 Win32 HWND 任务栏/Alt-Tab 图标。"""
    if sys.platform != "win32" or icon_path is None or not icon_path.is_file():
        return False

    ico_str = str(icon_path.resolve())
    applied_box: dict[str, bool] = {"ok": False}

    def _do_set_icon(native: Any) -> None:
        if native is None:
            return
        try:
            from System.Drawing import Icon

            native.Icon = Icon(ico_str)
            applied_box["ok"] = True
        except Exception:
            pass
        try:
            hwnd = int(native.Handle.ToInt64())
            user32 = ctypes.windll.user32
            user32.LoadImageW.restype = ctypes.c_void_p
            user32.LoadImageW.argtypes = [
                ctypes.c_void_p,
                ctypes.c_wchar_p,
                ctypes.c_uint,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_uint,
            ]
            h_small = user32.LoadImageW(None, ico_str, _IMAGE_ICON, 16, 16, _LR_LOADFROMFILE)
            h_big = user32.LoadImageW(None, ico_str, _IMAGE_ICON, 48, 48, _LR_LOADFROMFILE)
            if h_small:
                user32.SendMessageW(hwnd, _WM_SETICON, _ICON_SMALL, h_small)
            if h_big:
                user32.SendMessageW(hwnd, _WM_SETICON, _ICON_BIG, h_big)
            if h_small or h_big:
                # 持有句柄引用，避免窗体生命周期内图标失效
                handles = getattr(bridge, "_icon_handles", [])
                handles.extend([h for h in (h_small, h_big) if h])
                setattr(bridge, "_icon_handles", handles)
                applied_box["ok"] = True
        except Exception:
            pass

    bridge._run_on_ui_thread(_do_set_icon)
    return applied_box["ok"]


def _write_crash_log(exc: BaseException) -> Path:
    log_path = _PROJECT_ROOT / "output" / ".desktop_crash.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    raw_trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    text = (
        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {BRAND_NAME} 桌面窗口启动异常\n"
        f"{redact_secrets_in_text(raw_trace)}\n"
    )
    log_path.write_text(text, encoding="utf-8")
    return log_path


def _show_native_error_box(title: str, message: str) -> None:
    if sys.platform == "win32":
        try:
            ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)
            return
        except Exception:
            pass
    print(f"[{title}] {message}", file=sys.stderr)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=f"启动 {BRAND_NAME} 原生无边框桌面剪辑审片工作台")
    parser.add_argument("--host", default="127.0.0.1", help="本地后端监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8765, help="首选本地端口（占用时自动顺延）")
    parser.add_argument("--fullscreen", action="store_true", help="启动时直接进入无边框全屏模式")
    parser.add_argument("--browser", action="store_true", help="回退到默认浏览器标签页打开（开发调试用）")
    parser.add_argument("--debug", action="store_true", help="在桌面 WebView2 中启用开发者工具 (F12)")
    return parser


def _run_desktop_app(args: argparse.Namespace) -> int:
    port = find_available_port(args.host, preferred=args.port)
    backend = EmbeddedBackendServer(host=args.host, port=port)
    backend.start()

    if not wait_for_backend_ready(backend.url, timeout_sec=12.0):
        backend.stop()
        _show_native_error_box(f"{BRAND_NAME} 启动失败", f"本地服务 {backend.url} 启动超时，请检查端口或防火墙设置。")
        return 1

    if args.browser:
        print(f"{BRAND_NAME} 调试模式已启动：{backend.url}（按 Ctrl+C 退出）")
        webbrowser.open(backend.url)
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            backend.stop()
            return 0

    try:
        import webview
    except ImportError:
        backend.stop()
        _show_native_error_box(
            "缺少桌面窗口依赖",
            "尚未安装 pywebview。请先在虚拟环境中运行：\n"
            "uv pip install --python .venv\\Scripts\\python.exe pywebview",
        )
        return 1

    bridge = DesktopWindowBridge(on_close_cb=backend.stop)
    saved_state = _load_window_state()
    init_w, init_h, auto_max = _compute_initial_window_geometry()
    init_x: int | None = None
    init_y: int | None = None
    start_maximized = auto_max
    if saved_state is not None:
        init_w = int(saved_state["width"])
        init_h = int(saved_state["height"])
        init_x = saved_state["x"]
        init_y = saved_state["y"]
        start_maximized = bool(saved_state["maximized"])
        if init_x is not None and init_y is not None:
            bridge._saved_bounds = (init_x, init_y, init_w, init_h)

    storage_dir = _PROJECT_ROOT / "output" / ".webview_profile"
    storage_dir.mkdir(parents=True, exist_ok=True)
    icon_path = _resolve_app_icon_path()

    window = webview.create_window(
        title=BRAND_NAME,
        url=backend.url,
        js_api=bridge,
        width=init_w,
        height=init_h,
        x=init_x,
        y=init_y,
        min_size=(1024, 640),
        resizable=True,
        frameless=True,
        easy_drag=False,
        shadow=True,
        fullscreen=bool(args.fullscreen),
        background_color="#141414",
        text_select=True,
    )
    bridge.bind_window(window, start_fullscreen=bool(args.fullscreen))

    def _on_shown() -> None:
        _apply_native_window_icon(bridge, icon_path)
        bridge.apply_initial_maximize_and_hooks(start_maximized=start_maximized and not bool(args.fullscreen))

    def _on_closing() -> None:
        bridge.save_window_state()

    window.events.shown += _on_shown
    window.events.loaded += lambda: _apply_native_window_icon(bridge, icon_path)
    window.events.closing += _on_closing
    window.events.closed += lambda: backend.stop()

    try:
        gui_backend = "edgechromium" if sys.platform == "win32" else None
        webview.start(
            gui=gui_backend,
            debug=bool(args.debug),
            private_mode=False,
            storage_path=str(storage_dir),
            icon=str(icon_path) if icon_path else None,
        )
    finally:
        backend.stop()

    return 0


def main(argv: list[str] | None = None) -> int:
    _ensure_stdio_streams()
    try:
        args = build_arg_parser().parse_args(argv)
        return _run_desktop_app(args)
    except SystemExit:
        raise
    except BaseException as exc:
        log_file = _write_crash_log(exc)
        _show_native_error_box(
            f"{BRAND_NAME} 启动异常",
            f"原生桌面窗口运行出错：{exc}\n详细日志已保存至：{log_file}",
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
