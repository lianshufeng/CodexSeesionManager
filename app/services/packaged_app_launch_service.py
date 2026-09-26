from __future__ import annotations

import ctypes
import subprocess
import uuid
from pathlib import Path


class _GUID(ctypes.Structure):
    _fields_ = [
        ("data1", ctypes.c_uint32),
        ("data2", ctypes.c_uint16),
        ("data3", ctypes.c_uint16),
        ("data4", ctypes.c_ubyte * 8),
    ]


def _guid(value: str) -> _GUID:
    return _GUID.from_buffer_copy(uuid.UUID(value).bytes_le)


def packaged_app_id(exe: Path) -> str | None:
    if exe.name.lower() != "chatgpt.exe" or exe.parent.name.lower() != "app":
        return None
    package_folder = exe.parent.parent.name
    if not package_folder.startswith("OpenAI.") or "__" not in package_folder:
        return None
    package_name = package_folder.split("_", 1)[0]
    publisher_id = package_folder.rsplit("__", 1)[1]
    return f"{package_name}_{publisher_id}!App" if publisher_id else None


def activate_packaged_app(app_id: str, arguments: list[str]) -> int:
    ole32 = ctypes.OleDLL("ole32")
    ole32.CoInitializeEx.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoCreateInstance.argtypes = (
        ctypes.POINTER(_GUID), ctypes.c_void_p, ctypes.c_uint32,
        ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p),
    )
    ole32.CoCreateInstance.restype = ctypes.c_long
    initialized = ole32.CoInitializeEx(None, 2)
    if initialized not in (0, 1, -2147417850):
        raise OSError(f"COM 初始化失败: 0x{initialized & 0xffffffff:08X}")
    manager = ctypes.c_void_p()
    try:
        clsid = _guid("45BA127D-10A8-46EA-8AB7-56EA9078943C")
        iid = _guid("2E941141-7F97-4756-BA1D-9DECDE894A3D")
        result = ole32.CoCreateInstance(ctypes.byref(clsid), None, 4, ctypes.byref(iid), ctypes.byref(manager))
        if result != 0:
            raise OSError(f"创建应用激活器失败: 0x{result & 0xffffffff:08X}")
        methods = ctypes.cast(manager, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        activate = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
            ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
        )(methods[3])
        process_id = ctypes.c_uint32()
        result = activate(manager, app_id, subprocess.list2cmdline(arguments), 0, ctypes.byref(process_id))
        if result != 0:
            raise OSError(f"启动打包应用失败: 0x{result & 0xffffffff:08X}")
        return process_id.value
    finally:
        if manager.value:
            methods = ctypes.cast(manager, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
            ctypes.WINFUNCTYPE(ctypes.c_uint32, ctypes.c_void_p)(methods[2])(manager)
        if initialized in (0, 1):
            ole32.CoUninitialize()
