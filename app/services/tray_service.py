from __future__ import annotations

import ctypes
import sys

import pystray


class RecoverableTrayIcon(pystray.Icon):
    """用 Windows 的修改操作确认图标注册仍有效，并在失效时重试添加。"""

    def ensure_registered(self) -> bool:
        if sys.platform != "win32":
            return self.visible
        from pystray._util import win32

        hwnd = getattr(self, "_hwnd", None)
        thread = getattr(self, "_thread", None)
        if not hwnd or thread is None or not thread.is_alive():
            return False
        data = win32.NOTIFYICONDATAW(
            cbSize=ctypes.sizeof(win32.NOTIFYICONDATAW), hWnd=hwnd,
            hID=id(self), uFlags=win32.NIF_TIP, szTip=self.title,
        )
        if win32.Shell_NotifyIcon(win32.NIM_MODIFY, data):
            return True
        self._assert_icon_handle()
        data.uFlags = win32.NIF_MESSAGE | win32.NIF_ICON | win32.NIF_TIP
        data.uCallbackMessage = win32.WM_NOTIFY
        data.hIcon = self._icon_handle
        if win32.Shell_NotifyIcon(win32.NIM_ADD, data):
            self._visible = True
            print("[Tray] 托盘图标注册已恢复", flush=True)
        else:
            print("[Tray] 托盘注册暂时失败，10 秒后重试", flush=True)
        # 失败可能是 Explorer 尚未就绪，下个检查周期继续重试。
        return True
