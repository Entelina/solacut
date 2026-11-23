"""原生无边框桌面应用壳（src/desktop_app.py）单元测试。

验证端口冲突自动避让、内嵌 FastAPI 线程优雅启停、/api/health 探活与桌面窗口桥接状态机。
"""

from __future__ import annotations

import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.desktop_app import (  # noqa: E402
    BRAND_NAME,
    DesktopWindowBridge,
    EmbeddedBackendServer,
    _resolve_app_icon_path,
    find_available_port,
    wait_for_backend_ready,
)


class TestDesktopApp(unittest.TestCase):
    """Phase 1 原生无边框桌面壳核心模块单元测试。"""

    def test_find_available_port_skips_occupied(self) -> None:
        """测试 1：首选端口被占用时自动顺延返回空闲端口。"""
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        occupied_port = blocker.getsockname()[1]
        try:
            picked = find_available_port("127.0.0.1", preferred=occupied_port, max_tries=10)
            self.assertNotEqual(picked, occupied_port)
            self.assertGreater(picked, 0)
        finally:
            blocker.close()

    def test_embedded_backend_server_health_and_graceful_stop(self) -> None:
        """测试 2：内嵌 FastAPI 后台服务启动后 /api/health 返回就绪，并可优雅停止。"""
        port = find_available_port("127.0.0.1", preferred=18765)
        server = EmbeddedBackendServer("127.0.0.1", port)
        server.start()
        try:
            ready = wait_for_backend_ready(server.url, timeout_sec=8.0)
            self.assertTrue(ready)
        finally:
            server.stop()

    def test_desktop_window_bridge_state_transitions(self) -> None:
        """测试 3：验证窗口控制桥最小化、全屏切换、最大化回退与关闭回调。"""
        closed_flag = {"called": False}
        bridge = DesktopWindowBridge(on_close_cb=lambda: closed_flag.__setitem__("called", True))

        fake_win = MagicMock()
        fake_win.native = None  # 走纯跨平台逻辑分支便于离线单测
        bridge.bind_window(fake_win, start_fullscreen=False)

        st0 = bridge.get_window_state()
        self.assertTrue(st0["is_desktop"])
        self.assertFalse(st0["fullscreen"])
        self.assertFalse(st0["maximized"])

        st_max = bridge.toggle_maximize()
        self.assertTrue(st_max["maximized"])
        fake_win.maximize.assert_called_once()

        st_fs = bridge.toggle_fullscreen()
        self.assertTrue(st_fs["fullscreen"])
        self.assertFalse(st_fs["maximized"])
        fake_win.toggle_fullscreen.assert_called_once()

        bridge.minimize()
        fake_win.minimize.assert_called_once()

        bridge.close()
        fake_win.destroy.assert_called_once()
        self.assertTrue(closed_flag["called"])

    def test_embedded_backend_safe_under_pythonw_none_stdio(self) -> None:
        """测试 4：模拟 Windows pythonw.exe (stdout/stderr 为 None) 下内嵌 uvicorn 不崩溃。"""
        orig_out, orig_err = sys.stdout, sys.stderr
        try:
            sys.stdout = None  # type: ignore[assignment]
            sys.stderr = None  # type: ignore[assignment]
            port = find_available_port("127.0.0.1", preferred=18820)
            server = EmbeddedBackendServer("127.0.0.1", port)
            server.start()
            try:
                self.assertTrue(wait_for_backend_ready(server.url, timeout_sec=8.0))
            finally:
                server.stop()
        finally:
            sys.stdout, sys.stderr = orig_out, orig_err

    def test_brand_and_native_app_icon_assets(self) -> None:
        """测试 5：验证品牌名统一为 Solacut 且原生应用多尺寸 .ico 与左上角 PNG 资产完整就绪。"""
        self.assertEqual(BRAND_NAME, "Solacut")
        ico = _resolve_app_icon_path()
        self.assertIsNotNone(ico)
        assert ico is not None
        self.assertTrue(ico.is_file())
        self.assertGreater(ico.stat().st_size, 4096)
        self.assertTrue((PROJECT_ROOT / "web" / "brand" / "logo.png").is_file())
        self.assertTrue((PROJECT_ROOT / "web" / "brand" / "favicon.ico").is_file())


if __name__ == "__main__":
    unittest.main()
