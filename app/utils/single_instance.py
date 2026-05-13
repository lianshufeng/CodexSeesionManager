from __future__ import annotations

import ctypes
import hashlib
import json
import os
import socket
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Callable


_ERROR_ALREADY_EXISTS = 183
_ERROR_ACCESS_DENIED = 5
_LOCK_FILE_NAME = ".codex_session_manager.lock"
_RESTORE_COMMAND = b"RESTORE\n"

if sys.platform == "win32":
    ctypes.windll.kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    ctypes.windll.kernel32.CreateMutexW.restype = wintypes.HANDLE
    ctypes.windll.kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    ctypes.windll.kernel32.CloseHandle.restype = wintypes.BOOL


class SingleInstance:
    def __init__(self, app_dir: Path) -> None:
        self.app_dir = app_dir.resolve()
        self.lock_file = self.app_dir / _LOCK_FILE_NAME
        self._mutex_handle: int | None = None
        self._server_socket: socket.socket | None = None
        self._stop_event = threading.Event()
        digest = hashlib.sha256(os.path.normcase(str(self.app_dir)).encode("utf-8")).hexdigest()
        self._mutex_name = f"Local\\CodexSessionManager_{digest}"

    def acquire(self) -> bool:
        if sys.platform != "win32":
            return True
        handle = ctypes.windll.kernel32.CreateMutexW(None, False, self._mutex_name)
        last_error = ctypes.windll.kernel32.GetLastError()
        if not handle:
            return last_error != _ERROR_ACCESS_DENIED
        self._mutex_handle = handle
        return last_error != _ERROR_ALREADY_EXISTS

    def notify_existing(self, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            info = self._read_lock_info()
            port = int(info.get("port") or 0)
            if info.get("app_dir") == str(self.app_dir) and port:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.3) as client:
                        client.sendall(_RESTORE_COMMAND)
                    return True
                except OSError:
                    pass
            time.sleep(0.1)
        return False

    def start_restore_server(self, restore_callback: Callable[[], None]) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        server.settimeout(0.5)
        self._server_socket = server
        self._write_lock_file(server.getsockname()[1])

        thread = threading.Thread(
            target=self._serve_restore_requests,
            args=(restore_callback,),
            name="single-instance-restore",
            daemon=True,
        )
        thread.start()

    def close(self) -> None:
        self._stop_event.set()
        if self._server_socket is not None:
            try:
                self._server_socket.close()
            except OSError:
                pass
            self._server_socket = None
        self._remove_lock_file()
        if self._mutex_handle is not None and sys.platform == "win32":
            ctypes.windll.kernel32.CloseHandle(self._mutex_handle)
            self._mutex_handle = None

    def _serve_restore_requests(self, restore_callback: Callable[[], None]) -> None:
        while not self._stop_event.is_set() and self._server_socket is not None:
            try:
                client, _addr = self._server_socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with client:
                try:
                    command = client.recv(64)
                except OSError:
                    continue
                if command.startswith(_RESTORE_COMMAND.strip()):
                    restore_callback()

    def _read_lock_info(self) -> dict[str, object]:
        try:
            return json.loads(self.lock_file.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}

    def _write_lock_file(self, port: int) -> None:
        data = {
            "pid": os.getpid(),
            "port": port,
            "app_dir": str(self.app_dir),
        }
        self.lock_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def _remove_lock_file(self) -> None:
        info = self._read_lock_info()
        if info.get("pid") != os.getpid():
            return
        try:
            self.lock_file.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
