from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes

import psutil


class _Overlapped(ctypes.Structure):
    _fields_ = [('internal', ctypes.c_size_t), ('internal_high', ctypes.c_size_t),
                ('offset', wintypes.DWORD), ('offset_high', wintypes.DWORD), ('event', wintypes.HANDLE)]


class AppServerPipeWriter:
    """Write through a confirmed parent handle without using its IO completion port."""

    def __init__(self, parent_pid: int, created: float) -> None:
        self.parent_pid = parent_pid
        self.created = created
        self._handle = wintypes.HANDLE()
        self._lock = threading.Lock()
        self._kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel = self._kernel
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.DuplicateHandle.argtypes = [wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
                                          ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateEventW.restype = wintypes.HANDLE
        kernel.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                    ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(_Overlapped)]
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.GetOverlappedResult.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Overlapped),
                                              ctypes.POINTER(wintypes.DWORD), wintypes.BOOL]
        kernel.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Overlapped)]

    @property
    def ready(self) -> bool:
        return bool(self._handle.value)

    def confirm(self, source_handle: str) -> None:
        with self._lock:
            if self.ready:
                return
            self._check_parent()
            process = self._kernel.OpenProcess(0x40, False, self.parent_pid)
            if not process:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                if not self._kernel.DuplicateHandle(process, int(source_handle, 16),
                                                    self._kernel.GetCurrentProcess(), ctypes.byref(self._handle),
                                                    0, False, 2):
                    raise ctypes.WinError(ctypes.get_last_error())
            finally:
                self._kernel.CloseHandle(process)

    def _check_parent(self) -> None:
        if psutil.Process(self.parent_pid).create_time() != self.created:
            raise RuntimeError('桌面客户端已经重启，请重新附加')

    def write(self, payload: bytes) -> None:
        if not payload.endswith(b'\n') or len(payload) > 4096:
            raise ValueError('命令必须为完整 JSONL，且不超过 4 KiB')
        with self._lock:
            self._check_parent()
            if not self.ready:
                raise RuntimeError('尚未确认管道写端，请在 Codex 中切换一次会话')
            event = self._kernel.CreateEventW(None, True, False, None)
            if not event:
                raise ctypes.WinError(ctypes.get_last_error())
            operation = _Overlapped()
            # Low bit suppresses IOCP notification into the original client's loop.
            operation.event = event | 1
            written = wintypes.DWORD()
            buffer = ctypes.create_string_buffer(payload)
            try:
                success = self._kernel.WriteFile(self._handle, buffer, len(payload),
                                                 ctypes.byref(written), ctypes.byref(operation))
                if not success:
                    error = ctypes.get_last_error()
                    if error != 997:
                        raise ctypes.WinError(error)
                    timed_out = self._kernel.WaitForSingleObject(event, 5000) != 0
                    if timed_out:
                        self._kernel.CancelIoEx(self._handle, ctypes.byref(operation))
                    # Always finish the operation before releasing its native buffer.
                    success = self._kernel.GetOverlappedResult(self._handle, ctypes.byref(operation),
                                                              ctypes.byref(written), True)
                    if not success:
                        raise RuntimeError('管道写入未完成，投递状态未确认')
                if written.value != len(payload):
                    raise RuntimeError('管道发生部分写入，请重新附加后检查会话状态')
            finally:
                self._kernel.CloseHandle(event)

    def close(self) -> None:
        with self._lock:
            if self.ready:
                self._kernel.CloseHandle(self._handle)
                self._handle = wintypes.HANDLE()
