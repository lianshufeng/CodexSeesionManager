from __future__ import annotations

import os
import json
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
import re
import socket
import subprocess
import shutil
import sys
import tempfile
import time
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
import zipfile
from dataclasses import dataclass, replace
from queue import Empty, Queue
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener, urlopen

import pystray
import psutil
from PIL import Image

from app.version import APP_VERSION
from app.services.auth_login_service import AuthLoginResult, AuthLoginService
from app.services.auth_sync_service import AuthFileRow, AuthSyncService
from app.services.app_config_service import AppConfig, AppConfigService
from app.services.auth_token_refresh_service import AuthTokenRefreshResult, AuthTokenRefreshService
from app.services.auth_usage_service import QUOTA_REFRESH_FAILED, AuthQuotaItem, AuthUsageService
from app.services.cloud_sync_service import CloudSyncConfig, CloudSyncFile, CloudSyncService, CloudSyncVersion
from app.services.low_price_account_service import (
    LowPriceAccount,
    LowPriceAccountService,
    LowPriceProductInfo,
    LowPriceProductVariant,
    LowPriceSellerInfo,
)
from app.utils.path_utils import app_root
from tkinter import messagebox, ttk

from app.services.proxy_service import ProxyConfig, ProxyService


_AUTO_LOAD_MISMATCH_WINDOW_SECONDS = 60.0
_QUOTA_DROP_EPSILON = 0.01
_QUOTA_DROP_GRACE_SECONDS = 10.0
_TRAY_ICON_TIP = "Codex 账户管理"
_TRAY_ICON_MAX_ROWS = 4
_TRAY_ICON_MAX_TIP_LENGTH = 120
_TRAY_WATCHDOG_INTERVAL_MS = 5000
_CHINA_TIMEZONE = timezone(timedelta(hours=8))
_LATEST_RELEASE_API_URL = "https://api.github.com/repos/lianshufeng/CodexSeesionManager/releases/latest"
_RELEASES_URL = "https://github.com/lianshufeng/CodexSeesionManager/releases"
_UPDATE_CHUNK_SIZE = 1024 * 256
_UPDATES_DIR_NAME = ".updates"
_UPDATE_EXTRACT_DIR_NAME = "extracted"
_UPDATE_SCRIPT_NAME = "apply_update.bat"
_LOW_PRICE_FETCH_PAGE_COUNT = 8


@dataclass
class CodexInstallRow:
    name: str
    path: str
    display_path: str
    size: str
    version: str


@dataclass
class CodexProcessRow:
    pid: int
    name: str
    children: list["CodexProcessRow"]


class ProxyWindow:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.config_service = AppConfigService()
        loaded_config = self.config_service.load()
        self.service = ProxyService()
        if loaded_config is not None:
            self.service.config.port = loaded_config.port
            self.service.config.upstream_proxy = loaded_config.upstream_proxy
            self.service.config.use_upstream_proxy = loaded_config.use_upstream_proxy
        self.auth_sync_service = AuthSyncService()
        self.auth_login_service = AuthLoginService(self.auth_sync_service)
        self.auth_token_refresh_service = AuthTokenRefreshService(self.auth_sync_service)
        self.auth_usage_service = AuthUsageService(self.auth_sync_service)
        self.low_price_account_service = LowPriceAccountService()
        self.root.title("Codex 账户管理工具")
        self.root.minsize(1040, 660)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Escape>", lambda _event: self._on_close())

        self.port_var = tk.StringVar(value=str(self.service.config.port))
        self.upstream_proxy_var = tk.StringVar(value=self.service.config.upstream_proxy)
        self.use_upstream_proxy_var = tk.BooleanVar(value=self.service.config.use_upstream_proxy)
        self.cloud_s3_address_var = tk.StringVar(
            value=loaded_config.cloud_s3_address if loaded_config is not None else ""
        )
        self.cloud_bucket_name_var = tk.StringVar(
            value=loaded_config.cloud_bucket_name if loaded_config is not None else ""
        )
        self.cloud_account_var = tk.StringVar(value=loaded_config.cloud_account if loaded_config is not None else "")
        self.cloud_password_var = tk.StringVar(value=loaded_config.cloud_password if loaded_config is not None else "")
        self._rows_by_item: dict[str, CodexInstallRow] = {}
        self._launch_buttons: dict[str, ttk.Button] = {}
        self._install_tree_wrap: ttk.Frame | None = None
        self._refresh_launch_buttons_after_id: str | None = None
        self._auth_rows_by_item: dict[str, AuthFileRow] = {}
        self._tooltip: tk.Toplevel | None = None
        self._tooltip_label: ttk.Label | None = None
        self._auth_menu: tk.Menu | None = None
        self._auth_load_strategy_menu: tk.Menu | None = None
        self._auth_load_strategy_var = tk.StringVar(value="normal")
        self._auto_load_lock = Lock()
        self._proxy_lock = Lock()
        auto_load_enabled = loaded_config.auto_load if loaded_config is not None else True
        self.auto_load_var = tk.BooleanVar(value=auto_load_enabled)
        self._auto_load_enabled = auto_load_enabled
        self._use_upstream_proxy = self.service.config.use_upstream_proxy
        self._upstream_proxy = self.service.config.upstream_proxy
        self._auto_load_target_refresh_token = ""
        self._auto_load_target_access_token = ""
        self._auto_load_target_account_id = ""
        self._last_used_access_token = ""
        self._proxy_kill_pending = False
        self._proxy_kill_pending_access_token = ""
        self._proxy_kill_pending_used_access_token = ""
        self._proxy_kill_pending_reason = ""
        self._proxy_kill_next_allowed_at = 0.0
        self._proxy_kill_attempt_in_flight = False
        self._manual_proxy_kill_pending = False
        self._correct_traffic_button: ttk.Button | None = None
        self._correct_traffic_refreshing = False
        self._clean_auth_button: ttk.Button | None = None
        self._clean_auth_refreshing = False
        self._update_auth_button: ttk.Button | None = None
        self._refresh_tokens_button: ttk.Button | None = None
        self._refresh_tokens_running = False
        self._low_price_window: tk.Toplevel | None = None
        self._low_price_tree: ttk.Treeview | None = None
        self._low_price_refresh_button: ttk.Button | None = None
        self._low_price_search_var = tk.StringVar(value="")
        self._low_price_search_status_var = tk.StringVar(value="")
        self._low_price_search_var.trace_add("write", self._on_low_price_search_changed)
        self._low_price_refreshing = False
        self._low_price_refresh_generation = 0
        self._low_price_items_by_product_id: dict[str, LowPriceAccount] = {}
        self._low_price_product_id_by_item: dict[str, str] = {}
        self._low_price_item_by_product_id: dict[str, str] = {}
        self._low_price_titles_by_item: dict[str, str] = {}
        self._low_price_details_by_item: dict[str, str] = {}
        self._low_price_details_by_product_id: dict[str, str] = {}
        self._low_price_seller_info_by_product_id: dict[str, LowPriceSellerInfo] = {}
        self._low_price_seller_info_inflight: set[str] = set()
        self._low_price_seller_info_updated: set[str] = set()
        self._low_price_seller_info_lock = Lock()
        self._low_price_seller_info_executor = ThreadPoolExecutor(max_workers=5, thread_name_prefix="low-price-seller")
        self._low_price_visible_update_after_id: str | None = None
        self._correct_traffic_after_id: str | None = None
        self._auto_load_target_selected_at = 0.0
        self._quota_priority_by_refresh_token: dict[str, float] = {}
        self._auto_load_control_stop = Event()
        self._auto_load_control_socket: socket.socket | None = None
        self._auto_load_control_port = self._start_auto_load_control_server()
        self._proxy_restart_lock = Lock()
        self._proxy_restart_pending = False
        self._port_entry: ttk.Entry | None = None
        self._upstream_entry: ttk.Entry | None = None
        self._traffic_status_var = tk.StringVar(value="上行: 0  下行: 0")
        self._version_var = tk.StringVar(value=f"版本: {APP_VERSION}")
        self._update_status_var = tk.StringVar(value="")
        self._check_update_button: ttk.Button | None = None
        self._cloud_storage_window: tk.Toplevel | None = None
        self._cloud_sync_tree: ttk.Treeview | None = None
        self._cloud_sync_refresh_button: ttk.Button | None = None
        self._cloud_sync_upload_button: ttk.Button | None = None
        self._cloud_sync_pull_button: ttk.Button | None = None
        self._cloud_sync_delete_button: ttk.Button | None = None
        self._cloud_sync_versions_by_item: dict[str, CloudSyncVersion] = {}
        self._cloud_sync_running = False
        self._cloud_sync_config_vars: tuple[tk.StringVar, tk.StringVar, tk.StringVar, tk.StringVar] | None = None
        self._cloud_sync_config_trace_enabled = True
        self._cloud_file_window: tk.Toplevel | None = None
        self._cloud_file_tree: ttk.Treeview | None = None
        self._cloud_file_delete_button: ttk.Button | None = None
        self._cloud_file_refresh_button: ttk.Button | None = None
        self._cloud_file_version: CloudSyncVersion | None = None
        self._cloud_file_key_by_item: dict[str, str] = {}
        self._cloud_file_prefix_by_item: dict[str, str] = {}
        self._cloud_file_running = False
        self._checking_update = False
        self._downloading_update = False
        self._traffic_refresh_pending = False
        self._quota_refresh_pending = False
        self._ui_queue: Queue[Callable[[], None]] = Queue()
        self._closing = False
        self._last_install_rows_signature: tuple[tuple[str, str, str, str], ...] | None = None
        self._last_auth_rows_signature: tuple[
            tuple[bool, bool, bool, str, str, str, str, str, str, int], ...
        ] | None = None
        self._tray_icon_visible = False
        self._tray_icon: pystray.Icon | None = None

        self._build_ui()
        self._cleanup_old_update_files()
        self.root.after(50, self._drain_ui_queue)
        self.root.after(_TRAY_WATCHDOG_INTERVAL_MS, self._tray_icon_watchdog)
        self.auth_sync_service.set_change_callback(lambda: self._post_ui(self._schedule_refresh_auth_files))
        self.auth_usage_service.set_change_callback(lambda: self._post_ui(self._schedule_refresh_auth_files))
        self.auth_usage_service.set_quota_change_callback(
            lambda: (
                self._post_ui(self._schedule_quota_refresh_auth_files),
                self._post_ui(self._recompute_auto_load_target),
            )
        )
        self.auth_usage_service.set_proxy_provider(self._get_proxy_for_usage_request)
        self.auth_sync_service.start()
        self.auth_usage_service.start()
        self._center_window(1040, 660)
        self._load_or_probe_initial_config(loaded_config is not None)
        self._sync_proxy_config_cache()
        self._set_config_editable(True)
        self.refresh_all()
        self._recompute_auto_load_target()
        self._add_tray_icon()
        self.root.after(500, self._auto_start_with_certificate_check)

    def _build_ui(self) -> None:
        main = ttk.Frame(self.root, padding=14)
        main.pack(fill="both", expand=True)

        top = ttk.Frame(main)
        top.pack(fill="x")

        port_row = ttk.Frame(top)
        port_row.pack(side="left", anchor="w")
        ttk.Label(port_row, text="监听端口").pack(side="left")
        self._port_entry = ttk.Entry(port_row, textvariable=self.port_var, width=10)
        self._port_entry.pack(side="left", padx=(8, 0))

        upstream_row = ttk.Frame(main)
        upstream_row.pack(fill="x", pady=(8, 0))
        ttk.Checkbutton(
            upstream_row,
            text="二级代理",
            variable=self.use_upstream_proxy_var,
            command=self._on_use_upstream_proxy_toggled,
        ).pack(side="left")
        self._upstream_entry = ttk.Entry(upstream_row, textvariable=self.upstream_proxy_var, width=28)
        self._upstream_entry.pack(side="left", padx=(8, 0))

        actions = ttk.Frame(top)
        actions.pack(side="right", anchor="e")

        cloud_button = ttk.Button(actions, text="☁", width=3, command=self.open_cloud_storage_window)
        cloud_button.pack(side="right", padx=(8, 0))
        self._check_update_button = ttk.Button(actions, text="↻", width=3, command=self.check_for_updates)
        self._check_update_button.pack(side="right", padx=(8, 0))
        ttk.Button(actions, text="一键安装证书", command=self.install_certificate).pack(side="right")
        self.toggle_button = ttk.Button(actions, text="一键启动服务器", command=self.toggle_server)
        self.toggle_button.pack(side="right", padx=(0, 8))
        self._bind_widget_tooltip(
            cloud_button,
            "云同步",
        )
        self._bind_widget_tooltip(
            self._check_update_button,
            "检查更新",
        )

        tables = ttk.Frame(main)
        tables.pack(fill="both", expand=True, pady=(12, 0))

        table_frame = ttk.LabelFrame(tables, text="安装项", padding=8)
        table_frame.pack(fill="x")

        install_actions = ttk.Frame(table_frame)
        install_actions.pack(fill="x")
        ttk.Button(install_actions, text="刷新", command=self.refresh_all).pack(side="right")
        ttk.Button(install_actions, text="结束进程", command=self.kill_codex_processes).pack(side="right", padx=(0, 8))

        tree_wrap = ttk.Frame(table_frame)
        tree_wrap.pack(fill="x", pady=(6, 0))
        self._install_tree_wrap = tree_wrap

        columns = ("name", "path", "size", "version", "action")
        style = ttk.Style()
        style.configure("Install.Treeview", rowheight=32)
        self.tree = ttk.Treeview(
            tree_wrap,
            columns=columns,
            show="headings",
            selectmode="none",
            height=4,
            style="Install.Treeview",
        )
        self.tree.heading("name", text="名称")
        self.tree.heading("path", text="路径")
        self.tree.heading("size", text="大小")
        self.tree.heading("version", text="版本")
        self.tree.heading("action", text="操作")
        self.tree.column("name", width=100, anchor="w", stretch=False)
        self.tree.column("path", width=420, anchor="w", stretch=True)
        self.tree.column("size", width=90, anchor="center", stretch=False)
        self.tree.column("version", width=100, anchor="center", stretch=False)
        self.tree.column("action", width=82, anchor="center", stretch=False)
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<Motion>", self._on_tree_motion)
        self.tree.bind("<Leave>", lambda _event: self._hide_tooltip())
        self.tree.bind("<Configure>", lambda _event: self._schedule_refresh_launch_buttons())
        tree_wrap.bind("<Configure>", lambda _event: self._schedule_refresh_launch_buttons())

        y_scroll = ttk.Scrollbar(
            tree_wrap,
            orient="vertical",
            command=lambda *args: (self.tree.yview(*args), self._schedule_refresh_launch_buttons()),
        )
        self.tree.configure(
            yscrollcommand=lambda first, last: (y_scroll.set(first, last), self._schedule_refresh_launch_buttons())
        )
        self.tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        tree_wrap.rowconfigure(0, weight=1)
        tree_wrap.columnconfigure(0, weight=1)

        auth_frame = ttk.LabelFrame(tables, text="授权文件 - 双击切换", padding=8)
        auth_frame.pack(fill="both", expand=True, pady=(10, 0))

        auth_options = ttk.Frame(auth_frame)
        auth_options.pack(fill="x", pady=(0, 6))
        ttk.Checkbutton(auth_options, text="自动负载", variable=self.auto_load_var, command=self._on_auto_load_toggled).pack(side="left")
        self._correct_traffic_button = ttk.Button(auth_options, text="矫正流量", command=self.correct_traffic)
        self._correct_traffic_button.pack(side="right")
        self._clean_auth_button = ttk.Button(auth_options, text="清理授权", command=self.clean_auth_files)
        self._clean_auth_button.pack(side="right", padx=(0, 8))
        self._update_auth_button = ttk.Button(auth_options, text="更新授权", command=self.update_auth)
        self._update_auth_button.pack(side="right", padx=(0, 8))
        low_price_button = ttk.Button(auth_options, text="低价购号", command=self.open_low_price_window)
        low_price_button.pack(side="right", padx=(0, 8))
        self._refresh_tokens_button = ttk.Button(auth_options, text="一键刷新令牌", command=self.refresh_all_tokens)
        self._refresh_tokens_button.pack(side="right", padx=(0, 8))
        self._bind_widget_tooltip(
            low_price_button,
            "查看低价账号",
        )
        self._bind_widget_tooltip(
            self._refresh_tokens_button,
            "遍历授权文件并刷新访问令牌",
        )
        self._bind_widget_tooltip(
            self._update_auth_button,
            "更新授权文件",
        )
        self._bind_widget_tooltip(
            self._clean_auth_button,
            "清理同账户重复授权",
        )

        auth_wrap = ttk.Frame(auth_frame)
        auth_wrap.pack(fill="both", expand=True)

        auth_columns = (
            "currentMark",
            "loadMark",
            "loadStrategy",
            "accountId",
            "email",
            "tokenRefreshTime",
            "quotaRefreshTime",
            "quota",
            "planType",
        )
        self.auth_tree = ttk.Treeview(auth_wrap, columns=auth_columns, show="headings", selectmode="browse", height=5)
        self.auth_tree.heading("currentMark", text="当前")
        self.auth_tree.heading("loadMark", text="负载")
        self.auth_tree.heading("loadStrategy", text="策略")
        self.auth_tree.heading("accountId", text="账户id")
        self.auth_tree.heading("email", text="邮箱")
        self.auth_tree.heading("tokenRefreshTime", text="令牌刷新时间")
        self.auth_tree.heading("quotaRefreshTime", text="额度刷新时间")
        self.auth_tree.heading("quota", text="额度(5h/7d)")
        self.auth_tree.heading("planType", text="类型")
        self.auth_tree.column("currentMark", width=46, anchor="center", stretch=False)
        self.auth_tree.column("loadMark", width=46, anchor="center", stretch=False)
        self.auth_tree.column("loadStrategy", width=58, anchor="center", stretch=False)
        self.auth_tree.column("accountId", width=240, anchor="w", stretch=True)
        self.auth_tree.column("email", width=210, anchor="w", stretch=True)
        self.auth_tree.column("tokenRefreshTime", width=132, anchor="center", stretch=False)
        self.auth_tree.column("quotaRefreshTime", width=132, anchor="center", stretch=False)
        self.auth_tree.column("quota", width=126, anchor="center", stretch=False)
        self.auth_tree.column("planType", width=86, anchor="center", stretch=False)
        self.auth_tree.bind("<Double-1>", self._on_auth_tree_double_click)
        self.auth_tree.bind("<Button-3>", self._on_auth_tree_right_click)
        self.auth_tree.bind("<Delete>", self._on_auth_tree_delete_key)
        self.auth_tree.bind("<KP_Delete>", self._on_auth_tree_delete_key)
        self.auth_tree.bind("<Motion>", self._on_auth_tree_motion)
        self.auth_tree.bind("<Leave>", lambda _event: self._hide_tooltip())
        self._auth_menu = tk.Menu(self.root, tearoff=0)
        self._auth_menu.add_command(label="切换", command=self._activate_selected_auth_row)
        self._auth_menu.add_command(label="删除", command=self._delete_selected_auth_row)
        self._auth_menu.add_command(label="备注", command=self._edit_selected_auth_note)
        self._auth_load_strategy_menu = tk.Menu(self._auth_menu, tearoff=0)
        self._auth_load_strategy_menu.add_radiobutton(
            label="正常",
            variable=self._auth_load_strategy_var,
            value="normal",
            command=lambda: self._set_selected_auth_load_strategy("normal"),
        )
        self._auth_load_strategy_menu.add_radiobutton(
            label="优先",
            variable=self._auth_load_strategy_var,
            value="priority",
            command=lambda: self._set_selected_auth_load_strategy("priority"),
        )
        self._auth_load_strategy_menu.add_radiobutton(
            label="禁用",
            variable=self._auth_load_strategy_var,
            value="disabled",
            command=lambda: self._set_selected_auth_load_strategy("disabled"),
        )
        self._auth_menu.add_cascade(label="负载策略", menu=self._auth_load_strategy_menu)

        auth_scroll = ttk.Scrollbar(auth_wrap, orient="vertical", command=self.auth_tree.yview)
        self.auth_tree.configure(yscrollcommand=auth_scroll.set)
        self.auth_tree.grid(row=0, column=0, sticky="nsew")
        auth_scroll.grid(row=0, column=1, sticky="ns")
        auth_wrap.rowconfigure(0, weight=1)
        auth_wrap.columnconfigure(0, weight=1)

        traffic_frame = ttk.Frame(main)
        traffic_frame.pack(fill="x", pady=(8, 0))
        ttk.Label(traffic_frame, textvariable=self._version_var).pack(side="left")
        ttk.Label(traffic_frame, textvariable=self._update_status_var).pack(side="left", padx=(12, 0))
        ttk.Label(traffic_frame, textvariable=self._traffic_status_var).pack(side="right")

    def _center_window(self, width: int, height: int) -> None:
        self.root.update_idletasks()
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        x = (screen_width - width) // 2
        y = (screen_height - height) // 2
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _center_child_window(self, window: tk.Toplevel, width: int, height: int) -> None:
        self.root.update_idletasks()
        window.update_idletasks()
        root_x = self.root.winfo_rootx()
        root_y = self.root.winfo_rooty()
        root_width = self.root.winfo_width()
        root_height = self.root.winfo_height()
        x = root_x + max((root_width - width) // 2, 0)
        y = root_y + max((root_height - height) // 2, 0)
        window.geometry(f"{width}x{height}+{x}+{y}")

    def _center_child_window_on_screen(self, window: tk.Toplevel, width: int, height: int) -> None:
        self.root.update_idletasks()
        window.update_idletasks()
        screen_width = window.winfo_screenwidth()
        screen_height = window.winfo_screenheight()
        width = min(width, max(screen_width - 40, 320))
        height = min(height, max(screen_height - 80, 240))
        x = max((screen_width - width) // 2, 0)
        y = max((screen_height - height) // 2, 0)
        window.geometry(f"{width}x{height}+{x}+{y}")

    def _bind_dialog_close_keys(self, dialog: tk.Toplevel) -> None:
        def close_dialog(_event: tk.Event | None = None) -> str:
            if dialog.winfo_exists():
                dialog.destroy()
            return "break"

        dialog.bind("<Escape>", close_dialog)
        dialog.bind_all("<Escape>", close_dialog)
        dialog.bind("<Destroy>", lambda _event: dialog.unbind_all("<Escape>"), add="+")

    def _post_ui(self, callback: Callable[[], None]) -> None:
        if self._closing:
            return
        self._ui_queue.put(callback)

    def _drain_ui_queue(self) -> None:
        if self._closing:
            return
        try:
            while True:
                callback = self._ui_queue.get_nowait()
                try:
                    callback()
                except Exception as exc:
                    print(f"[ProxyWindow] UI 回调异常: {exc}", flush=True)
        except Empty:
            pass
        try:
            if not self._closing:
                self.root.after(50, self._drain_ui_queue)
        except tk.TclError:
            pass

    def _refresh_config(self) -> bool:
        try:
            port = int(self.port_var.get().strip())
        except ValueError:
            messagebox.showerror("端口错误", "请输入有效的端口号。")
            return False
        use_upstream_proxy = self.use_upstream_proxy_var.get()
        upstream_proxy = self.upstream_proxy_var.get().strip()
        if use_upstream_proxy and not upstream_proxy:
            messagebox.showerror("代理错误", "请输入二级代理地址。")
            return False
        self.service.config = ProxyConfig(
            port=port,
            upstream_proxy=upstream_proxy,
            use_upstream_proxy=use_upstream_proxy,
        )
        self._sync_proxy_config_cache()
        self._persist_config()
        return True

    def _prime_default_port(self) -> None:
        try:
            port = self.service.find_available_port()
        except RuntimeError:
            return
        self.port_var.set(str(port))

    def _load_or_probe_initial_config(self, has_config: bool) -> None:
        if has_config:
            return
        self._prime_default_port()
        self._persist_config()

    def _auto_start(self) -> None:
        print("启动中...")
        self._set_busy(True)
        Thread(target=self._auto_start_worker, daemon=True).start()

    def _auto_start_worker(self) -> None:
        self._post_ui(self.start_server)

    def _auto_start_with_certificate_check(self) -> None:
        Thread(target=self._auto_start_with_certificate_check_worker, daemon=True).start()

    def _auto_start_with_certificate_check_worker(self) -> None:
        try:
            cert_path = self.service.certificate_files()["cer"]
            if cert_path.exists() and not self.service.is_certificate_installed():
                ok, message = self.service.install_certificate()
                if ok:
                    print(f"[ProxyWindow] 启动时自动安装证书成功: {cert_path}", flush=True)
                else:
                    print(f"[ProxyWindow] 启动时自动安装证书失败: {message}", flush=True)
            elif cert_path.exists():
                print(f"[ProxyWindow] 证书已安装: {cert_path}", flush=True)
            else:
                print(f"[ProxyWindow] 未找到证书文件，跳过自动安装: {cert_path}", flush=True)
        except Exception as exc:
            print(f"[ProxyWindow] 自动检查证书异常: {exc}", flush=True)
        finally:
            self._post_ui(self._auto_start)

    def _auto_start_failed(self, message: str) -> None:
        print("代理未启动")
        self._set_busy(False)
        messagebox.showerror("端口错误", message)

    def _schedule_refresh_auth_files(self) -> None:
        try:
            self._apply_pending_quota_items()
            self._post_ui(self.refresh_auth_files)
        except tk.TclError:
            pass

    def _schedule_quota_refresh_auth_files(self) -> None:
        try:
            if not self._apply_pending_quota_items():
                return
            if self._quota_refresh_pending:
                return
            self._quota_refresh_pending = True
            self._post_ui(self._refresh_quota_table)
        except tk.TclError:
            self._quota_refresh_pending = False

    def _refresh_quota_table(self) -> None:
        self._quota_refresh_pending = False
        try:
            self.refresh_auth_files(update_status=False)
        except tk.TclError:
            pass

    def _apply_pending_quota_items(self) -> bool:
        quota_items = self.auth_usage_service.pop_pending_quota_items()
        if not quota_items:
            return False
        quota_snapshot = self.auth_usage_service.quota_snapshot()
        self._update_quota_drop_state(quota_snapshot)
        self.auth_sync_service.update_usage_cache(
            quota_snapshot,
            self.auth_usage_service.plan_type_snapshot(),
            self.auth_usage_service.user_id_snapshot(),
            self.auth_usage_service.email_snapshot(),
            self.auth_usage_service.quota_refresh_time_5h_snapshot(),
            self.auth_usage_service.quota_refresh_time_7d_snapshot(),
        )
        return True

    def _update_quota_drop_state(self, quota_snapshot: dict[str, str]) -> None:
        current_priorities: dict[str, float] = {}
        dropped_items: list[tuple[str, float, float, str]] = []
        for refresh_token, quota in quota_snapshot.items():
            priority = self._quota_priority(quota)
            if priority < 0:
                continue
            current_priorities[refresh_token] = priority
            previous = self._quota_priority_by_refresh_token.get(refresh_token)
            if previous is None:
                continue
            if priority < previous - _QUOTA_DROP_EPSILON:
                dropped_items.append((refresh_token, previous, priority, quota))

        if not current_priorities:
            return

        quota_drop_message = ""
        target_drop_message = ""
        with self._auto_load_lock:
            self._quota_priority_by_refresh_token.update(current_priorities)
            if not dropped_items or not self._auto_load_enabled:
                return

            target_refresh_token = self._auto_load_target_refresh_token
            if not target_refresh_token:
                return

            now = time.monotonic()
            if now - self._auto_load_target_selected_at < _QUOTA_DROP_GRACE_SECONDS:
                return

            for refresh_token, previous, current, quota in dropped_items:
                if refresh_token == target_refresh_token:
                    if self._proxy_kill_pending_reason == "quota_drop":
                        self._clear_proxy_kill_pending_locked()
                        target_drop_message = (
                            "[AutoLoad] 当前选举 token 已出现额度下降，取消因额度未命中的断连等待"
                        )
                    continue

                state_changed = (
                    not self._proxy_kill_pending
                    or self._proxy_kill_pending_reason != "quota_drop"
                    or self._proxy_kill_pending_used_access_token != refresh_token
                    or self._proxy_kill_pending_access_token != target_refresh_token
                )
                self._proxy_kill_pending = True
                self._proxy_kill_pending_reason = "quota_drop"
                self._proxy_kill_pending_access_token = target_refresh_token
                self._proxy_kill_pending_used_access_token = refresh_token
                if state_changed:
                    self._proxy_kill_attempt_in_flight = False
                    quota_drop_message = (
                        "[AutoLoad] 额度下降命中非当前选举 token，等待下一次 WebSocket ping/pong 断开旧连接: "
                        f"actual_refresh_token={refresh_token} target_refresh_token={target_refresh_token} "
                        f"quota={previous:g}%->{current:g}% raw={quota}"
                    )
                break

        if target_drop_message:
            print(target_drop_message, flush=True)
        if quota_drop_message:
            print(quota_drop_message, flush=True)

    def _on_auto_load_access_token_used(self, access_token: str) -> None:
        self._update_proxy_kill_pending_for_used_token(access_token)
        if not self.auth_sync_service.increment_traffic_by_access_token(access_token):
            return
        self._schedule_refresh_traffic()

    def _on_proxy_traffic_update(self, up_bytes: int, down_bytes: int) -> None:
        try:
            self._post_ui(lambda: self._update_traffic_status(up_bytes, down_bytes))
        except tk.TclError:
            pass

    def _update_traffic_status(self, up_bytes: int, down_bytes: int) -> None:
        self._traffic_status_var.set(
            f"上行: {self._format_traffic_bytes(up_bytes, 3)}  下行: {self._format_traffic_bytes(down_bytes, 3)}"
        )

    def _on_auto_load_toggled(self) -> None:
        enabled = self.auto_load_var.get()
        with self._auto_load_lock:
            self._auto_load_enabled = enabled
        self._persist_config()
        if enabled:
            self._recompute_auto_load_target()
            return
        self._set_auto_load_target("", "")
        self._clear_proxy_kill_pending()
        self.refresh_auth_files(update_status=False)

    def _recompute_auto_load_target(self) -> None:
        with self._auto_load_lock:
            if not self._auto_load_enabled:
                return
            current_refresh_token = self._auto_load_target_refresh_token
            current_access_token = self._auto_load_target_access_token
        rows = self.auth_sync_service.list_auth_rows()
        rows = [row for row in rows if not row.disabled and self._is_auto_load_quota_usable(row.quota)]
        if not rows:
            self._set_auto_load_target("", "")
            self._clear_proxy_kill_pending()
            print("[AutoLoad] 当前没有可选授权文件", flush=True)
            return
        priority_rows = [
            row
            for row in rows
            if row.load_strategy == "priority"
        ]
        if priority_rows:
            rows = priority_rows
        row = max(rows, key=lambda item: (self._quota_priority(item.quota), item.refresh_token))
        target_changed = row.refresh_token != current_refresh_token or row.access_token != current_access_token
        self._set_auto_load_target(row.refresh_token, row.access_token, row.account_id)
        if target_changed:
            print(
                f"[AutoLoad] 负载目标: account_id={row.account_id} refresh_token={row.refresh_token} quota={row.quota}",
                flush=True,
            )
        self._sync_proxy_kill_pending_with_target(row.access_token)
        self.refresh_auth_files(update_status=False)

    def _set_auto_load_target(self, refresh_token: str, access_token: str, account_id: str = "") -> None:
        with self._auto_load_lock:
            target_changed = (
                refresh_token != self._auto_load_target_refresh_token
                or access_token != self._auto_load_target_access_token
                or account_id != self._auto_load_target_account_id
            )
            self._auto_load_target_refresh_token = refresh_token
            self._auto_load_target_access_token = access_token
            self._auto_load_target_account_id = account_id
            if target_changed:
                self._auto_load_target_selected_at = time.monotonic()
                if self._proxy_kill_pending_reason == "quota_drop":
                    self._clear_proxy_kill_pending_locked()

    def _restart_proxy_server_async(self) -> bool:
        with self._proxy_restart_lock:
            if self._proxy_restart_pending:
                return False
            if not (self.service.process and self.service.process.poll() is None):
                return False
            self._proxy_restart_pending = True
        Thread(target=self._restart_proxy_server_worker, daemon=True).start()
        return True

    def _restart_proxy_server_worker(self) -> None:
        try:
            self.service.stop()
            time.sleep(0.3)
            ok, message = self.service.run()
            if not ok:
                print(f"[ProxyWindow] 代理重启失败: {message}", flush=True)
        except Exception as exc:
            print(f"[ProxyWindow] 代理重启异常: {exc}", flush=True)
        finally:
            with self._proxy_restart_lock:
                self._proxy_restart_pending = False
            try:
                self._post_ui(self._update_toggle_button)
            except tk.TclError:
                pass

    def _update_proxy_kill_pending_for_used_token(self, used_access_token: str) -> None:
        used_access_token = used_access_token.strip()
        if not used_access_token:
            return
        mismatch_detected = False
        mismatch_resolved = False
        with self._auto_load_lock:
            self._last_used_access_token = used_access_token
            if not self._auto_load_enabled:
                self._clear_proxy_kill_pending_locked()
                return
            target_access_token = self._auto_load_target_access_token.strip()
            if not target_access_token:
                self._clear_proxy_kill_pending_locked()
                return
            if used_access_token == target_access_token:
                if self._proxy_kill_pending_reason != "quota_drop":
                    mismatch_resolved = self._proxy_kill_pending
                    self._clear_proxy_kill_pending_locked()
            else:
                state_changed = (
                    not self._proxy_kill_pending
                    or self._proxy_kill_pending_access_token != target_access_token
                    or self._proxy_kill_pending_used_access_token != used_access_token
                )
                self._proxy_kill_pending = True
                self._proxy_kill_pending_reason = "token_mismatch"
                self._proxy_kill_pending_access_token = target_access_token
                self._proxy_kill_pending_used_access_token = used_access_token
                if state_changed:
                    self._proxy_kill_attempt_in_flight = False
                    mismatch_detected = True
        if mismatch_resolved:
            print("[AutoLoad] 实际消耗 token 已与当前选举目标一致，停止等待断连", flush=True)
            return
        if mismatch_detected:
            print(
                "[AutoLoad] 检测到 token 消耗未命中当前选举目标，等待下一次 WebSocket ping/pong 断开旧连接",
                flush=True,
            )

    def _sync_proxy_kill_pending_with_target(self, target_access_token: str) -> None:
        with self._auto_load_lock:
            if not self._auto_load_enabled or not target_access_token:
                self._clear_proxy_kill_pending_locked()
                return
            if (
                self._proxy_kill_pending_reason != "quota_drop"
                and self._last_used_access_token
                and self._last_used_access_token == target_access_token
            ):
                self._clear_proxy_kill_pending_locked()

    def _clear_proxy_kill_pending(self) -> None:
        with self._auto_load_lock:
            self._clear_proxy_kill_pending_locked()

    def _consume_proxy_kill_pending(self) -> bool:
        kill_message = ""
        with self._auto_load_lock:
            if not self._is_proxy_kill_pending_locked():
                return False
            if self._proxy_kill_attempt_in_flight:
                return False
            now = time.monotonic()
            if now < self._proxy_kill_next_allowed_at:
                return False
            self._proxy_kill_attempt_in_flight = True
            self._proxy_kill_next_allowed_at = now + _AUTO_LOAD_MISMATCH_WINDOW_SECONDS
            kill_message = (
                "[AutoLoad] WebSocket ping/pong 触发断开旧连接: "
                f"reason={self._proxy_kill_pending_reason or '-'} "
                f"target={self._proxy_kill_pending_access_token or '-'} "
                f"actual={self._proxy_kill_pending_used_access_token or '-'}"
            )
        if kill_message:
            print(kill_message, flush=True)
        return True

    def _record_proxy_kill_result(self, killed: int) -> None:
        with self._auto_load_lock:
            self._proxy_kill_attempt_in_flight = False
            if killed > 0:
                self._clear_proxy_kill_pending_locked(preserve_cooldown=True)
                return
            if self._proxy_kill_pending:
                print("[AutoLoad] 本次未找到可断开的旧连接，保留断连等待并按冷却时间重试", flush=True)

    def _request_manual_proxy_kill(self) -> None:
        with self._auto_load_lock:
            self._manual_proxy_kill_pending = True
        print("[ProxyWindow] 已请求断开代理长连接", flush=True)

    def _consume_manual_proxy_kill_pending(self) -> bool:
        with self._auto_load_lock:
            if not self._manual_proxy_kill_pending:
                return False
            self._manual_proxy_kill_pending = False
        return True

    def _record_manual_proxy_kill_result(self, killed: int, tracked: int, reset: int) -> None:
        print(f"[ProxyWindow] 已断开代理连接: tracked={tracked} reset={reset} killed={killed}", flush=True)

    def _is_proxy_kill_pending_locked(self) -> bool:
        return self._proxy_kill_pending

    def _clear_proxy_kill_pending_locked(self, preserve_cooldown: bool = False) -> None:
        next_allowed_at = self._proxy_kill_next_allowed_at if preserve_cooldown else 0.0
        self._proxy_kill_pending = False
        self._proxy_kill_pending_access_token = ""
        self._proxy_kill_pending_used_access_token = ""
        self._proxy_kill_pending_reason = ""
        self._proxy_kill_next_allowed_at = next_allowed_at
        self._proxy_kill_attempt_in_flight = False

    def _persist_config(self) -> None:
        try:
            port = int(self.port_var.get().strip())
        except ValueError:
            return
        upstream_proxy = self.upstream_proxy_var.get().strip()
        if self.use_upstream_proxy_var.get() and not upstream_proxy:
            return
        self.config_service.save(
            AppConfig(
                port=port,
                upstream_proxy=upstream_proxy,
                use_upstream_proxy=self.use_upstream_proxy_var.get(),
                auto_load=self.auto_load_var.get(),
                cloud_s3_address=self.cloud_s3_address_var.get().strip(),
                cloud_bucket_name=self.cloud_bucket_name_var.get().strip(),
                cloud_account=self.cloud_account_var.get().strip(),
                cloud_password=self.cloud_password_var.get(),
            )
        )

    def open_cloud_storage_window(self) -> None:
        if self._cloud_storage_window is not None and self._cloud_storage_window.winfo_exists():
            self._cloud_storage_window.lift()
            self._cloud_storage_window.focus_force()
            return

        dialog = tk.Toplevel(self.root)
        self._cloud_storage_window = dialog
        dialog.title("云同步")
        dialog.transient(self.root)
        dialog.resizable(True, True)
        dialog.protocol("WM_DELETE_WINDOW", self._close_cloud_storage_window)
        self._bind_dialog_close_keys(dialog)
        dialog.bind("<Destroy>", lambda event: self._on_cloud_storage_window_destroy(event, dialog), add="+")
        self._center_child_window(dialog, 720, 460)

        s3_address_var = tk.StringVar(value=self.cloud_s3_address_var.get())
        bucket_name_var = tk.StringVar(value=self.cloud_bucket_name_var.get())
        account_var = tk.StringVar(value=self.cloud_account_var.get())
        password_var = tk.StringVar(value=self.cloud_password_var.get())

        def persist_cloud_storage(*_args: object) -> None:
            if not self._cloud_sync_config_trace_enabled:
                return
            self.cloud_s3_address_var.set(s3_address_var.get().strip())
            self.cloud_bucket_name_var.set(bucket_name_var.get().strip())
            self.cloud_account_var.set(account_var.get().strip())
            self.cloud_password_var.set(password_var.get())
            self._persist_config()

        s3_address_var.trace_add("write", persist_cloud_storage)
        bucket_name_var.trace_add("write", persist_cloud_storage)
        account_var.trace_add("write", persist_cloud_storage)
        password_var.trace_add("write", persist_cloud_storage)
        self._cloud_sync_config_vars = (s3_address_var, bucket_name_var, account_var, password_var)

        body = ttk.Frame(dialog, padding=14)
        body.pack(fill="both", expand=True)
        body.rowconfigure(1, weight=1)
        body.columnconfigure(0, weight=1)

        config_frame = ttk.LabelFrame(body, text="连接配置", padding=10)
        config_frame.grid(row=0, column=0, sticky="ew")
        config_frame.columnconfigure(1, weight=1)
        config_frame.columnconfigure(3, weight=1)

        ttk.Label(config_frame, text="(s3)地址：").grid(row=0, column=0, sticky="e", pady=(0, 8), padx=(0, 8))
        s3_entry = ttk.Entry(config_frame, textvariable=s3_address_var, width=36)
        s3_entry.grid(row=0, column=1, columnspan=3, sticky="ew", pady=(0, 8))
        ttk.Label(config_frame, text="桶名：").grid(row=1, column=0, sticky="e", pady=(0, 8), padx=(0, 8))
        ttk.Entry(config_frame, textvariable=bucket_name_var, width=24).grid(row=1, column=1, sticky="ew", pady=(0, 8), padx=(0, 12))
        ttk.Label(config_frame, text="账号：").grid(row=1, column=2, sticky="e", pady=(0, 8), padx=(0, 8))
        ttk.Entry(config_frame, textvariable=account_var, width=24).grid(row=1, column=3, sticky="ew", pady=(0, 8))
        ttk.Label(config_frame, text="密码：").grid(row=2, column=0, sticky="e", padx=(0, 8))
        ttk.Entry(config_frame, textvariable=password_var, width=24, show="*").grid(row=2, column=1, sticky="ew", padx=(0, 12))

        list_frame = ttk.LabelFrame(body, text="云端版本", padding=10)
        list_frame.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        list_frame.rowconfigure(0, weight=1)
        list_frame.columnconfigure(0, weight=1)

        columns = ("name", "object_count", "size", "last_modified")
        self._cloud_sync_tree = ttk.Treeview(list_frame, columns=columns, show="headings", selectmode="browse")
        self._cloud_sync_tree.heading("name", text="版本")
        self._cloud_sync_tree.heading("object_count", text="文件数")
        self._cloud_sync_tree.heading("size", text="大小")
        self._cloud_sync_tree.heading("last_modified", text="最后修改")
        self._cloud_sync_tree.column("name", width=260, anchor="w", stretch=True)
        self._cloud_sync_tree.column("object_count", width=80, anchor="center", stretch=False)
        self._cloud_sync_tree.column("size", width=90, anchor="center", stretch=False)
        self._cloud_sync_tree.column("last_modified", width=150, anchor="center", stretch=False)
        self._cloud_sync_tree.bind("<Double-1>", self._on_cloud_sync_tree_double_click)
        self._cloud_sync_tree.grid(row=0, column=0, sticky="nsew")
        y_scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self._cloud_sync_tree.yview)
        self._cloud_sync_tree.configure(yscrollcommand=y_scroll.set)
        y_scroll.grid(row=0, column=1, sticky="ns")

        button_row = ttk.Frame(body)
        button_row.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        button_row.columnconfigure(0, weight=1)

        self._cloud_sync_refresh_button = ttk.Button(
            button_row,
            text="刷新",
            command=lambda: self._run_cloud_sync_action("refresh"),
        )
        self._cloud_sync_refresh_button.pack(side="right")
        self._cloud_sync_upload_button = ttk.Button(
            button_row,
            text="同步",
            command=lambda: self._run_cloud_sync_action("upload"),
        )
        self._cloud_sync_upload_button.pack(side="right", padx=(0, 8))
        self._cloud_sync_pull_button = ttk.Button(
            button_row,
            text="拉取",
            command=lambda: self._run_cloud_sync_action("pull"),
        )
        self._cloud_sync_pull_button.pack(side="right", padx=(0, 8))
        self._cloud_sync_delete_button = ttk.Button(
            button_row,
            text="删除",
            command=lambda: self._run_cloud_sync_action("delete"),
        )
        self._cloud_sync_delete_button.pack(side="right", padx=(0, 8))
        s3_entry.focus_set()

    def _close_cloud_storage_window(self) -> None:
        if self._cloud_storage_window is None:
            return
        try:
            self._cloud_storage_window.destroy()
        except tk.TclError:
            pass
        self._cloud_storage_window = None

    def _on_cloud_storage_window_destroy(self, event: tk.Event, dialog: tk.Toplevel) -> None:
        if event.widget is dialog:
            self._cloud_storage_window = None
            self._cloud_sync_tree = None
            self._cloud_sync_refresh_button = None
            self._cloud_sync_upload_button = None
            self._cloud_sync_pull_button = None
            self._cloud_sync_delete_button = None
            self._cloud_sync_versions_by_item.clear()
            self._cloud_sync_config_vars = None

    def _run_cloud_sync_action(self, action: str) -> None:
        if self._cloud_sync_running:
            return
        service = self._create_cloud_sync_service()
        if service is None:
            return

        selected_version = self._get_selected_cloud_sync_version()
        if action in {"pull", "delete"} and selected_version is None:
            messagebox.showwarning("云同步", "请先选择一个云端版本。", parent=self._cloud_storage_window)
            return
        if action == "delete" and selected_version is not None:
            if not messagebox.askyesno("云同步", f"确认删除云端版本 {selected_version.name} 吗？", parent=self._cloud_storage_window):
                return
        if action == "pull" and selected_version is not None:
            message = f"确认拉取云端版本 {selected_version.name} 并替换本地对应数据吗？"
            if not messagebox.askyesno("云同步", message, parent=self._cloud_storage_window):
                return

        self._cloud_sync_running = True
        self._set_cloud_sync_buttons_state()
        Thread(target=self._cloud_sync_worker, args=(service, action, selected_version), daemon=True).start()

    def _create_cloud_sync_service(self) -> CloudSyncService | None:
        config = CloudSyncConfig(
            s3_address=self.cloud_s3_address_var.get().strip(),
            bucket_name=self.cloud_bucket_name_var.get().strip(),
            account=self.cloud_account_var.get().strip(),
            password=self.cloud_password_var.get(),
        )
        if not config.s3_address or not config.bucket_name or not config.account or not config.password:
            messagebox.showwarning("云同步", "请先填写云同步配置。", parent=self._cloud_storage_window)
            return None
        return CloudSyncService(config, root_dir=app_root())

    def _get_selected_cloud_sync_version(self) -> CloudSyncVersion | None:
        if self._cloud_sync_tree is None:
            return None
        selection = self._cloud_sync_tree.selection()
        if not selection:
            return None
        return self._cloud_sync_versions_by_item.get(selection[0])

    def _on_cloud_sync_tree_double_click(self, event: tk.Event) -> None:
        if self._cloud_sync_tree is None:
            return
        row_id = self._cloud_sync_tree.identify_row(event.y)
        if not row_id:
            return
        version = self._cloud_sync_versions_by_item.get(row_id)
        if version is None:
            return
        self.open_cloud_file_window(version)

    def open_cloud_file_window(self, version: CloudSyncVersion) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title(f"云同步文件 - {version.name}")
        dialog.transient(self.root)
        dialog.resizable(True, True)
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        self._bind_dialog_close_keys(dialog)
        self._center_child_window(dialog, 760, 460)

        body = ttk.Frame(dialog, padding=12)
        body.pack(fill="both", expand=True)
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)

        tree_frame = ttk.Frame(body)
        tree_frame.grid(row=0, column=0, sticky="nsew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)

        columns = ("size", "last_modified", "key")
        tree = ttk.Treeview(tree_frame, columns=columns, selectmode="browse")
        tree.heading("#0", text="文件")
        tree.heading("size", text="大小")
        tree.heading("last_modified", text="最后修改")
        tree.heading("key", text="云端路径")
        tree.column("#0", width=340, anchor="w", stretch=True)
        tree.column("size", width=90, anchor="center", stretch=False)
        tree.column("last_modified", width=150, anchor="center", stretch=False)
        tree.column("key", width=320, anchor="w", stretch=True)
        tree.grid(row=0, column=0, sticky="nsew")
        y_scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=y_scroll.set)
        y_scroll.grid(row=0, column=1, sticky="ns")

        button_row = ttk.Frame(body)
        button_row.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        button_row.columnconfigure(0, weight=1)
        key_by_item: dict[str, str] = {}
        prefix_by_item: dict[str, str] = {}
        running = False
        delete_button: ttk.Button | None = None
        refresh_button: ttk.Button | None = None

        def set_buttons_state() -> None:
            state = "disabled" if running else "normal"
            for button in (delete_button, refresh_button):
                if button is not None:
                    button.config(state=state)

        def render_files(files: list[CloudSyncFile]) -> None:
            if not dialog.winfo_exists():
                return
            for item_id in tree.get_children():
                tree.delete(item_id)
            key_by_item.clear()
            prefix_by_item.clear()
            folder_items: dict[str, str] = {"": ""}
            for file_item in files:
                parts = [part for part in file_item.relative_path.split("/") if part]
                if not parts:
                    continue
                parent_key = ""
                parent_item = ""
                for folder in parts[:-1]:
                    folder_key = f"{parent_key}{folder}/"
                    folder_item = folder_items.get(folder_key)
                    if folder_item is None:
                        folder_item = tree.insert(parent_item, "end", text=folder, values=("", "", ""))
                        folder_items[folder_key] = folder_item
                        prefix_by_item[folder_item] = f"{version.prefix}{folder_key}"
                    parent_key = folder_key
                    parent_item = folder_item
                item_id = tree.insert(
                    parent_item,
                    "end",
                    text=parts[-1],
                    values=(self._format_traffic_bytes(file_item.size), file_item.last_modified, file_item.key),
                )
                key_by_item[item_id] = file_item.key
            for item_id in tree.get_children():
                tree.item(item_id, open=True)

        def finish_refresh(files: list[CloudSyncFile], error: str) -> None:
            nonlocal running
            running = False
            set_buttons_state()
            if not dialog.winfo_exists():
                return
            if error:
                messagebox.showerror("云同步失败", error, parent=dialog)
                return
            render_files(files)

        def refresh_files() -> None:
            nonlocal running
            if running:
                return
            service = self._create_cloud_sync_service()
            if service is None:
                return
            running = True
            set_buttons_state()

            def worker() -> None:
                files: list[CloudSyncFile] = []
                error = ""
                try:
                    files = service.list_version_files(version.prefix)
                except Exception as exc:
                    error = str(exc)
                self._post_ui(lambda: finish_refresh(files, error))

            Thread(target=worker, daemon=True).start()

        def finish_preview(error: str) -> None:
            nonlocal running
            running = False
            set_buttons_state()
            if dialog.winfo_exists() and error:
                messagebox.showerror("云同步失败", error, parent=dialog)

        def preview_file(key: str) -> None:
            nonlocal running
            if running:
                return
            service = self._create_cloud_sync_service()
            if service is None:
                return
            running = True
            set_buttons_state()

            def worker() -> None:
                error = ""
                try:
                    suffix = Path(key).suffix or ".txt"
                    preview_dir = Path(tempfile.gettempdir()) / "codex_cloud_sync_preview"
                    preview_dir.mkdir(parents=True, exist_ok=True)
                    preview_path = preview_dir / Path(key).name
                    if preview_path.exists():
                        preview_path = preview_dir / f"{Path(key).stem}_{int(time.time())}{suffix}"
                    service.download_file_to(key, preview_path)
                    subprocess.Popen(["notepad.exe", str(preview_path)])
                except Exception as exc:
                    error = str(exc)
                self._post_ui(lambda: finish_preview(error))

            Thread(target=worker, daemon=True).start()

        def on_tree_double_click(event: tk.Event) -> None:
            row_id = tree.identify_row(event.y)
            if not row_id:
                return
            key = key_by_item.get(row_id)
            if key:
                preview_file(key)

        def on_tree_motion(event: tk.Event) -> None:
            row_id = tree.identify_row(event.y)
            if not row_id:
                tree.configure(cursor="")
                self._hide_tooltip()
                return
            key = key_by_item.get(row_id) or prefix_by_item.get(row_id)
            if not key:
                tree.configure(cursor="")
                self._hide_tooltip()
                return
            tree.configure(cursor="hand2" if row_id in key_by_item else "")
            self._show_tooltip(event.x_root, event.y_root, key)

        def finish_delete(files: list[CloudSyncFile], versions: list[CloudSyncVersion], error: str) -> None:
            nonlocal running
            running = False
            set_buttons_state()
            if not dialog.winfo_exists():
                return
            if error:
                messagebox.showerror("云同步失败", error, parent=dialog)
                return
            render_files(files)
            self._render_cloud_sync_versions(versions)

        def delete_selected() -> None:
            nonlocal running
            if running:
                return
            selection = tree.selection()
            if not selection:
                messagebox.showwarning("云同步", "请先选择要删除的云端文件。", parent=dialog)
                return
            item_id = selection[0]
            key = key_by_item.get(item_id)
            prefix = prefix_by_item.get(item_id)
            name = tree.item(item_id, "text")
            if not key and not prefix:
                return
            message = f"确认删除云端文件 {name} 吗？" if key else f"确认删除云端目录 {name} 及其所有文件吗？"
            if not messagebox.askyesno("云同步", message, parent=dialog):
                return
            service = self._create_cloud_sync_service()
            if service is None:
                return
            running = True
            set_buttons_state()

            def worker() -> None:
                files: list[CloudSyncFile] = []
                versions: list[CloudSyncVersion] = []
                error = ""
                try:
                    if key:
                        service.delete_file(key)
                    elif prefix:
                        service.delete_prefix(prefix)
                    files = service.list_version_files(version.prefix)
                    versions = service.list_versions()
                except Exception as exc:
                    error = str(exc)
                self._post_ui(lambda: finish_delete(files, versions, error))

            Thread(target=worker, daemon=True).start()

        tree.bind("<Double-1>", on_tree_double_click)
        tree.bind("<Motion>", on_tree_motion)
        tree.bind("<Leave>", lambda _event: self._hide_tooltip())
        delete_button = ttk.Button(button_row, text="删除", command=delete_selected)
        delete_button.pack(side="right")
        refresh_button = ttk.Button(button_row, text="刷新", command=refresh_files)
        refresh_button.pack(side="right", padx=(0, 8))
        refresh_files()

    def _close_cloud_file_window(self) -> None:
        if self._cloud_file_window is None:
            return
        try:
            self._cloud_file_window.destroy()
        except tk.TclError:
            pass
        self._cloud_file_window = None

    def _on_cloud_file_window_destroy(self, event: tk.Event, dialog: tk.Toplevel) -> None:
        if event.widget is dialog:
            self._cloud_file_window = None
            self._cloud_file_tree = None
            self._cloud_file_delete_button = None
            self._cloud_file_refresh_button = None
            self._cloud_file_version = None
            self._cloud_file_key_by_item.clear()
            self._cloud_file_prefix_by_item.clear()
            self._cloud_file_running = False

    def _cloud_sync_worker(
        self,
        service: CloudSyncService,
        action: str,
        selected_version: CloudSyncVersion | None,
    ) -> None:
        versions: list[CloudSyncVersion] = []
        message = ""
        error = ""
        try:
            if action == "refresh":
                versions = service.list_versions()
            elif action == "upload":
                service.sync_auth()
                versions = service.list_versions()
            elif action == "pull":
                assert selected_version is not None
                service.pull_version(selected_version.prefix)
                versions = service.list_versions()
            elif action == "delete":
                assert selected_version is not None
                service.delete_version(selected_version.prefix)
                versions = service.list_versions()
        except Exception as exc:
            error = str(exc)
        self._post_ui(lambda: self._finish_cloud_sync_action(versions, message, error, action))

    def _finish_cloud_sync_action(
        self,
        versions: list[CloudSyncVersion],
        message: str,
        error: str,
        action: str,
    ) -> None:
        self._cloud_sync_running = False
        self._set_cloud_sync_buttons_state()
        if error:
            messagebox.showerror("云同步失败", error, parent=self._cloud_storage_window)
            return
        self._render_cloud_sync_versions(versions)
        if action == "pull":
            self._reload_config_from_disk()
            self.auth_sync_service.invalidate_cached_state()
            self.refresh_auth_files(update_status=False)
            messagebox.showinfo("云同步", "拉取完成，已替换本地对应数据。", parent=self._cloud_storage_window)
        elif action == "upload":
            messagebox.showinfo("云同步", "同步完成。", parent=self._cloud_storage_window)

    def _render_cloud_sync_versions(self, versions: list[CloudSyncVersion]) -> None:
        if self._cloud_sync_tree is None:
            return
        for item_id in self._cloud_sync_tree.get_children():
            self._cloud_sync_tree.delete(item_id)
        self._cloud_sync_versions_by_item.clear()
        for version in versions:
            item_id = self._cloud_sync_tree.insert(
                "",
                "end",
                values=(
                    version.name,
                    version.object_count,
                    self._format_traffic_bytes(version.size),
                    version.last_modified,
                ),
            )
            self._cloud_sync_versions_by_item[item_id] = version

    def _set_cloud_sync_buttons_state(self) -> None:
        buttons = (
            self._cloud_sync_refresh_button,
            self._cloud_sync_upload_button,
            self._cloud_sync_pull_button,
            self._cloud_sync_delete_button,
        )
        state = "disabled" if self._cloud_sync_running else "normal"
        for button in buttons:
            if button is not None:
                button.config(state=state)

    def _refresh_cloud_file_tree(self) -> None:
        if self._cloud_file_running:
            return
        service = self._create_cloud_sync_service()
        if service is None or self._cloud_file_version is None:
            return
        self._cloud_file_running = True
        self._set_cloud_file_buttons_state()
        Thread(target=self._cloud_file_refresh_worker, args=(service, self._cloud_file_version), daemon=True).start()

    def _cloud_file_refresh_worker(self, service: CloudSyncService, version: CloudSyncVersion) -> None:
        files: list[CloudSyncFile] = []
        error = ""
        try:
            files = service.list_version_files(version.prefix)
        except Exception as exc:
            error = str(exc)
        self._post_ui(lambda: self._finish_cloud_file_refresh(files, error))

    def _finish_cloud_file_refresh(self, files: list[CloudSyncFile], error: str) -> None:
        self._cloud_file_running = False
        self._set_cloud_file_buttons_state()
        if error:
            messagebox.showerror("云同步失败", error, parent=self._cloud_file_window)
            return
        self._render_cloud_files(files)

    def _render_cloud_files(self, files: list[CloudSyncFile]) -> None:
        if self._cloud_file_tree is None or self._cloud_file_version is None:
            return
        for item_id in self._cloud_file_tree.get_children():
            self._cloud_file_tree.delete(item_id)
        self._cloud_file_key_by_item.clear()
        self._cloud_file_prefix_by_item.clear()

        folder_items: dict[str, str] = {"": ""}
        for file_item in files:
            parts = [part for part in file_item.relative_path.split("/") if part]
            if not parts:
                continue
            parent_key = ""
            parent_item = ""
            for folder in parts[:-1]:
                folder_key = f"{parent_key}{folder}/"
                folder_item = folder_items.get(folder_key)
                if folder_item is None:
                    folder_item = self._cloud_file_tree.insert(parent_item, "end", text=folder, values=("", "", ""))
                    folder_items[folder_key] = folder_item
                    self._cloud_file_prefix_by_item[folder_item] = f"{self._cloud_file_version.prefix}{folder_key}"
                parent_key = folder_key
                parent_item = folder_item

            item_id = self._cloud_file_tree.insert(
                parent_item,
                "end",
                text=parts[-1],
                values=(
                    self._format_traffic_bytes(file_item.size),
                    file_item.last_modified,
                    file_item.key,
                ),
            )
            self._cloud_file_key_by_item[item_id] = file_item.key
        for item_id in self._cloud_file_tree.get_children():
            self._cloud_file_tree.item(item_id, open=True)

    def _on_cloud_file_tree_double_click(self, event: tk.Event) -> None:
        if self._cloud_file_tree is None:
            return
        row_id = self._cloud_file_tree.identify_row(event.y)
        if not row_id:
            return
        key = self._cloud_file_key_by_item.get(row_id)
        if not key:
            return
        self._preview_cloud_file(key)

    def _preview_cloud_file(self, key: str) -> None:
        if self._cloud_file_running:
            return
        service = self._create_cloud_sync_service()
        if service is None:
            return
        self._cloud_file_running = True
        self._set_cloud_file_buttons_state()
        Thread(target=self._cloud_file_preview_worker, args=(service, key), daemon=True).start()

    def _cloud_file_preview_worker(self, service: CloudSyncService, key: str) -> None:
        error = ""
        try:
            suffix = Path(key).suffix or ".txt"
            preview_dir = Path(tempfile.gettempdir()) / "codex_cloud_sync_preview"
            preview_dir.mkdir(parents=True, exist_ok=True)
            preview_path = preview_dir / Path(key).name
            if preview_path.exists():
                preview_path = preview_dir / f"{Path(key).stem}_{int(time.time())}{suffix}"
            service.download_file_to(key, preview_path)
            subprocess.Popen(["notepad.exe", str(preview_path)])
        except Exception as exc:
            error = str(exc)
        self._post_ui(lambda: self._finish_cloud_file_preview(error))

    def _finish_cloud_file_preview(self, error: str) -> None:
        self._cloud_file_running = False
        self._set_cloud_file_buttons_state()
        if error:
            messagebox.showerror("云同步失败", error, parent=self._cloud_file_window)

    def _delete_selected_cloud_file(self) -> None:
        if self._cloud_file_tree is None or self._cloud_file_running:
            return
        selection = self._cloud_file_tree.selection()
        if not selection:
            messagebox.showwarning("云同步", "请先选择要删除的云端文件。", parent=self._cloud_file_window)
            return
        item_id = selection[0]
        key = self._cloud_file_key_by_item.get(item_id)
        prefix = self._cloud_file_prefix_by_item.get(item_id)
        name = self._cloud_file_tree.item(item_id, "text")
        if not key and not prefix:
            return
        message = f"确认删除云端文件 {name} 吗？" if key else f"确认删除云端目录 {name} 及其所有文件吗？"
        if not messagebox.askyesno("云同步", message, parent=self._cloud_file_window):
            return
        service = self._create_cloud_sync_service()
        if service is None:
            return
        self._cloud_file_running = True
        self._set_cloud_file_buttons_state()
        Thread(target=self._cloud_file_delete_worker, args=(service, key, prefix), daemon=True).start()

    def _cloud_file_delete_worker(self, service: CloudSyncService, key: str | None, prefix: str | None) -> None:
        versions: list[CloudSyncVersion] = []
        files: list[CloudSyncFile] = []
        error = ""
        try:
            if key:
                service.delete_file(key)
            elif prefix:
                service.delete_prefix(prefix)
            if self._cloud_file_version is not None:
                files = service.list_version_files(self._cloud_file_version.prefix)
            versions = service.list_versions()
        except Exception as exc:
            error = str(exc)
        self._post_ui(lambda: self._finish_cloud_file_delete(files, versions, error))

    def _finish_cloud_file_delete(
        self,
        files: list[CloudSyncFile],
        versions: list[CloudSyncVersion],
        error: str,
    ) -> None:
        self._cloud_file_running = False
        self._set_cloud_file_buttons_state()
        if error:
            messagebox.showerror("云同步失败", error, parent=self._cloud_file_window)
            return
        self._render_cloud_files(files)
        self._render_cloud_sync_versions(versions)

    def _set_cloud_file_buttons_state(self) -> None:
        state = "disabled" if self._cloud_file_running else "normal"
        for button in (self._cloud_file_delete_button, self._cloud_file_refresh_button):
            if button is not None:
                button.config(state=state)

    def _get_proxy_for_usage_request(self) -> str:
        with self._proxy_lock:
            if not self._use_upstream_proxy:
                return ""
            return self._upstream_proxy

    def _sync_proxy_config_cache(self) -> None:
        with self._proxy_lock:
            self._use_upstream_proxy = self.use_upstream_proxy_var.get()
            self._upstream_proxy = self.upstream_proxy_var.get().strip()

    def _reload_config_from_disk(self) -> None:
        loaded_config = self.config_service.load()
        if loaded_config is None:
            return
        self.port_var.set(str(loaded_config.port))
        self.upstream_proxy_var.set(loaded_config.upstream_proxy)
        self.use_upstream_proxy_var.set(loaded_config.use_upstream_proxy)
        self.auto_load_var.set(loaded_config.auto_load)
        self.cloud_s3_address_var.set(loaded_config.cloud_s3_address)
        self.cloud_bucket_name_var.set(loaded_config.cloud_bucket_name)
        self.cloud_account_var.set(loaded_config.cloud_account)
        self.cloud_password_var.set(loaded_config.cloud_password)
        self._auto_load_enabled = loaded_config.auto_load
        self._sync_proxy_config_cache()
        self._refresh_cloud_sync_config_fields()

    def _refresh_cloud_sync_config_fields(self) -> None:
        if self._cloud_sync_config_vars is None:
            return
        s3_address_var, bucket_name_var, account_var, password_var = self._cloud_sync_config_vars
        self._cloud_sync_config_trace_enabled = False
        try:
            s3_address_var.set(self.cloud_s3_address_var.get())
            bucket_name_var.set(self.cloud_bucket_name_var.get())
            account_var.set(self.cloud_account_var.get())
            password_var.set(self.cloud_password_var.get())
        finally:
            self._cloud_sync_config_trace_enabled = True

    def _on_use_upstream_proxy_toggled(self) -> None:
        if not self._refresh_config():
            return
        if self.service.process and self.service.process.poll() is None:
            print("[ProxyWindow] 二级代理配置已变更，正在重启代理使配置立即生效", flush=True)
            self._restart_proxy_server_async()

    def _get_auto_load_target_refresh_token(self) -> str:
        with self._auto_load_lock:
            return self._auto_load_target_refresh_token if self._auto_load_enabled else ""

    def _get_auto_load_marks(self) -> tuple[str, str]:
        current_refresh_token = ""
        for row in self.auth_sync_service.list_auth_rows():
            if row.current:
                current_refresh_token = row.refresh_token
                break
        return current_refresh_token, self._get_auto_load_target_refresh_token()

    def _get_auto_load_target_access_token(self) -> str:
        with self._auto_load_lock:
            return self._auto_load_target_access_token if self._auto_load_enabled else ""

    def _get_auto_load_target_auth_payload(self) -> str:
        with self._auto_load_lock:
            if not self._auto_load_enabled:
                token = ""
                account_id = ""
            else:
                token = self._auto_load_target_access_token
                account_id = self._auto_load_target_account_id
        return json.dumps({"access_token": token, "account_id": account_id}, ensure_ascii=False)

    def _start_auto_load_control_server(self) -> int:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(5)
        server.settimeout(0.5)
        self._auto_load_control_socket = server
        port = server.getsockname()[1]
        Thread(target=self._run_auto_load_control_server, args=(server,), daemon=True).start()
        return port

    def _run_auto_load_control_server(self, server: socket.socket) -> None:
        while not self._auto_load_control_stop.is_set():
            try:
                conn, _addr = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                try:
                    conn.settimeout(0.1)
                    try:
                        payload = conn.recv(65536).decode("utf-8", errors="ignore").strip()
                    except socket.timeout:
                        payload = ""
                    if payload.startswith("USED "):
                        parts = payload.split()
                        token = parts[1] if len(parts) >= 2 else ""
                        if token:
                            self._post_ui(lambda value=token: self._on_auto_load_access_token_used(value))
                        continue
                    if payload.startswith("KILL_RESULT "):
                        parts = payload.split()
                        try:
                            killed = int(parts[1]) if len(parts) >= 2 else 0
                        except ValueError:
                            killed = 0
                        self._post_ui(lambda value=killed: self._record_proxy_kill_result(value))
                        continue
                    if payload.startswith("MANUAL_KILL_RESULT "):
                        parts = payload.split()
                        try:
                            killed = int(parts[1]) if len(parts) >= 2 else 0
                            tracked = int(parts[2]) if len(parts) >= 3 else killed
                            reset = int(parts[3]) if len(parts) >= 4 else killed
                        except ValueError:
                            killed = 0
                            tracked = 0
                            reset = 0
                        self._post_ui(
                            lambda value=killed, total=tracked, reset_count=reset: self._record_manual_proxy_kill_result(
                                value,
                                total,
                                reset_count,
                            )
                        )
                        continue
                    if payload.startswith("TRAFFIC "):
                        parts = payload.split()
                        if len(parts) == 3:
                            try:
                                up_bytes = int(parts[1])
                                down_bytes = int(parts[2])
                            except ValueError:
                                continue
                            self._post_ui(lambda up=up_bytes, down=down_bytes: self._on_proxy_traffic_update(up, down))
                        continue
                    if payload == "RESELECT":
                        self._post_ui(self._recompute_auto_load_target)
                        continue
                    if payload == "PINGPONG":
                        conn.sendall(("1\n" if self._consume_proxy_kill_pending() else "0\n").encode("utf-8"))
                        continue
                    if payload == "MANUAL_KILL":
                        conn.sendall(("1\n" if self._consume_manual_proxy_kill_pending() else "0\n").encode("utf-8"))
                        continue
                    if payload == "IDLE_TIMEOUT":
                        continue
                    if payload == "AUTH":
                        conn.sendall((self._get_auto_load_target_auth_payload() + "\n").encode("utf-8"))
                        continue
                    token = self._get_auto_load_target_access_token()
                    conn.sendall((token + "\n").encode("utf-8"))
                except OSError:
                    continue

    def _schedule_refresh_traffic(self) -> None:
        if self._traffic_refresh_pending:
            return
        self._traffic_refresh_pending = True
        try:
            self._post_ui(self._refresh_traffic_table)
        except tk.TclError:
            self._traffic_refresh_pending = False

    def _refresh_traffic_table(self) -> None:
        self._traffic_refresh_pending = False
        try:
            self.refresh_auth_files(update_status=False)
        except tk.TclError:
            pass

    def _set_busy(self, busy: bool) -> None:
        self.toggle_button.config(state="disabled" if busy else "normal")
        if busy:
            self.toggle_button.config(text="启动中...")
        self._set_config_editable(not busy)

    def _set_config_editable(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        if self._port_entry is not None:
            self._port_entry.config(state=state)
        if self._upstream_entry is not None:
            self._upstream_entry.config(state=state)

    def _update_toggle_button(self) -> None:
        if self.service.process and self.service.process.poll() is None:
            self.toggle_button.config(text="停止服务器")
        else:
            self.toggle_button.config(text="一键启动服务器")

    def toggle_server(self) -> None:
        if self.service.process and self.service.process.poll() is None:
            self.stop_server()
            return
        self.start_server()

    def start_server(self) -> None:
        print("启动中...")
        if not self._refresh_config():
            print("代理未启动")
            self._set_busy(False)
            return
        if self.service.process and self.service.process.poll() is None:
            print(f"代理正在运行: {self.service.config.host}:{self.service.config.port}")
            self._update_toggle_button()
            self._set_busy(False)
            self._post_ui(self.refresh_installs)
            return
        self._set_busy(True)
        Thread(target=self._start_server_worker, daemon=True).start()

    def _start_server_worker(self) -> None:
        try:
            os.environ["AUTOLOAD_CONTROL_PORT"] = str(self._auto_load_control_port)
            ok, message = self.service.run()
        except Exception as exc:
            self._post_ui(lambda: self._start_failed(str(exc)))
            return
        if not ok:
            self._post_ui(lambda: self._start_failed(message))
            return
        self._post_ui(self._start_succeeded)

    def _start_failed(self, message: str) -> None:
        print("代理未启动")
        self._update_toggle_button()
        self._set_busy(False)
        messagebox.showerror("启动失败", message)

    def _start_succeeded(self) -> None:
        print(f"代理已启动: {self.service.config.host}:{self.service.config.port}")
        self._update_toggle_button()
        self._set_busy(False)
        self._set_config_editable(False)
        self.refresh_installs()

    def stop_server(self) -> None:
        self.service.stop()
        print("代理已停止")
        self._update_toggle_button()
        self._set_busy(False)
        self._set_config_editable(True)

    def install_certificate(self) -> None:
        ok, message = self.service.install_certificate()
        if ok:
            messagebox.showinfo("安装结果", message)
        else:
            messagebox.showerror("安装失败", message)

    def check_for_updates(self) -> None:
        if self._checking_update or self._downloading_update:
            return
        self._checking_update = True
        self._update_status_var.set("检查更新中...")
        self._set_update_button_busy("...")
        Thread(target=self._check_for_updates_worker, daemon=True).start()

    def _check_for_updates_worker(self) -> None:
        latest_tag = ""
        latest_url = _RELEASES_URL
        asset_name = ""
        asset_url = ""
        error = ""
        try:
            request = Request(
                _LATEST_RELEASE_API_URL,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": f"CodexSeesionManager/{APP_VERSION}",
                },
            )
            with self._open_update_url(request, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
            if isinstance(payload, dict):
                latest_tag = str(payload.get("tag_name") or "").strip()
                latest_url = str(payload.get("html_url") or "").strip() or _RELEASES_URL
                asset = self._select_release_asset(payload)
                if asset is not None:
                    asset_name = str(asset.get("name") or "").strip()
                    asset_url = str(asset.get("browser_download_url") or "").strip()
            if not latest_tag:
                error = "未获取到最新版本号。"
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            error = str(exc)

        try:
            self._post_ui(
                lambda tag=latest_tag,
                url=latest_url,
                download_name=asset_name,
                download_url=asset_url,
                message=error: self._finish_check_for_updates(
                    tag,
                    url,
                    download_name,
                    download_url,
                    message,
                )
            )
        except tk.TclError:
            pass

    def _finish_check_for_updates(
        self,
        latest_tag: str,
        latest_url: str,
        asset_name: str,
        asset_url: str,
        error: str,
    ) -> None:
        self._checking_update = False
        self._set_update_button_idle()

        if error:
            self._update_status_var.set("")
            messagebox.showerror("检查更新失败", error)
            return

        current_version = self._parse_semver(APP_VERSION)
        latest_version = self._parse_semver(latest_tag)
        if current_version is None:
            self._update_status_var.set("")
            messagebox.showerror("检查更新失败", f"当前版本号格式不正确: {APP_VERSION}")
            return
        if latest_version is None:
            self._update_status_var.set("")
            messagebox.showerror("检查更新失败", f"远端版本号格式不正确: {latest_tag}\n\n版本号需要使用 0.0.0 格式。")
            return
        if latest_version <= current_version:
            self._update_status_var.set("")
            messagebox.showinfo("检查更新", f"当前已是最新版本。\n\n当前版本: {APP_VERSION}")
            return

        if not asset_url:
            self._update_status_var.set("")
            if messagebox.askyesno(
                "发现新版本",
                f"当前版本: {APP_VERSION}\n最新版本: {latest_tag}\n\n未找到可下载文件，是否打开发布页面？",
            ):
                webbrowser.open(latest_url or _RELEASES_URL)
            return

        if messagebox.askyesno(
            "发现新版本",
            f"当前版本: {APP_VERSION}\n最新版本: {latest_tag}\n\n是否立即下载更新？",
        ):
            self._download_update(asset_name, asset_url)
        else:
            self._update_status_var.set("")

    def _parse_semver(self, version: str) -> tuple[int, int, int] | None:
        match = re.fullmatch(r"[vV]?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?", version.strip())
        if not match:
            return None
        return int(match.group(1)), int(match.group(2)), int(match.group(3))

    def _open_update_url(self, request: Request, timeout: float):
        normalized_proxy = self._normalize_update_proxy_url(self._get_proxy_for_usage_request())
        if not normalized_proxy:
            return urlopen(request, timeout=timeout)
        opener = build_opener(ProxyHandler({"http": normalized_proxy, "https": normalized_proxy}))
        return opener.open(request, timeout=timeout)

    def _normalize_update_proxy_url(self, proxy_url: str) -> str:
        value = proxy_url.strip()
        if not value:
            return ""
        if "://" not in value:
            value = f"http://{value}"
        return value

    def _select_release_asset(self, payload: dict[str, object]) -> dict[str, object] | None:
        assets = payload.get("assets")
        if not isinstance(assets, list):
            return None

        candidates = [asset for asset in assets if isinstance(asset, dict) and asset.get("browser_download_url")]
        if not candidates:
            return None

        for asset in candidates:
            name = str(asset.get("name") or "").lower()
            if name.endswith(".zip") and "windows" in name:
                return asset
        for asset in candidates:
            name = str(asset.get("name") or "").lower()
            if name.endswith(".zip"):
                return asset
        return candidates[0]

    def _download_update(self, asset_name: str, asset_url: str) -> None:
        if self._downloading_update:
            return
        self._downloading_update = True
        self._update_status_var.set("下载更新中...")
        self._set_update_button_busy("↓")
        Thread(target=self._download_update_worker, args=(asset_name, asset_url), daemon=True).start()

    def _download_update_worker(self, asset_name: str, asset_url: str) -> None:
        download_path: Path | None = None
        error = ""
        try:
            safe_name = Path(asset_name or "CodexSeesionManager-update.zip").name
            updates_dir = self._updates_dir()
            updates_dir.mkdir(parents=True, exist_ok=True)
            download_path = updates_dir / safe_name
            temp_path = download_path.with_suffix(download_path.suffix + ".tmp")

            request = Request(
                asset_url,
                headers={"User-Agent": f"CodexSeesionManager/{APP_VERSION}"},
            )
            with self._open_update_url(request, timeout=60) as response, temp_path.open("wb") as target:
                total_size = int(response.headers.get("Content-Length") or 0)
                downloaded = 0
                last_percent = -1
                while True:
                    chunk = response.read(_UPDATE_CHUNK_SIZE)
                    if not chunk:
                        break
                    target.write(chunk)
                    downloaded += len(chunk)
                    if total_size > 0:
                        percent = min(100, int(downloaded * 100 / total_size))
                        if percent != last_percent:
                            last_percent = percent
                            self._post_update_progress(percent)
            temp_path.replace(download_path)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            error = str(exc)

        try:
            self._post_ui(lambda path=download_path, message=error: self._finish_download_update(path, message))
        except tk.TclError:
            pass

    def _finish_download_update(self, download_path: Path | None, error: str) -> None:
        self._downloading_update = False
        self._set_update_button_idle()

        if error or download_path is None:
            messagebox.showerror("更新下载失败", error or "下载文件不存在。")
            return

        self._update_status_var.set("更新包下载完成")
        if messagebox.askyesno("更新下载完成", f"更新包已下载到：\n{download_path}\n\n是否立即安装并重启？"):
            self._install_update(download_path)

    def _install_update(self, download_path: Path) -> None:
        error = ""
        script_path: Path | None = None
        try:
            source_dir = self._extract_update_package(download_path)
            script_path = self._write_update_script(source_dir)
        except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
            error = str(exc)

        if error or script_path is None:
            messagebox.showerror("更新安装失败", error or "未能创建更新脚本。")
            return

        if not messagebox.askyesno("准备安装更新", "程序将退出，等待进程释放后自动覆盖安装并重新启动。\n\n是否继续？"):
            return

        try:
            subprocess.Popen(
                ["cmd.exe", "/c", str(script_path)],
                cwd=str(app_root()),
                creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
            )
        except OSError as exc:
            messagebox.showerror("更新安装失败", str(exc))
            return
        self._shutdown_for_update()

    def _extract_update_package(self, download_path: Path) -> Path:
        updates_dir = self._updates_dir()
        extract_root = updates_dir / _UPDATE_EXTRACT_DIR_NAME
        if extract_root.exists():
            shutil.rmtree(extract_root)
        extract_root.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(download_path) as archive:
            archive.extractall(extract_root)

        candidates = [path for path in extract_root.iterdir() if path.is_dir()]
        files = [path for path in extract_root.iterdir() if path.is_file()]
        if len(candidates) == 1 and not files:
            return candidates[0]
        return extract_root

    def _write_update_script(self, source_dir: Path) -> Path:
        root_dir = app_root()
        script_path = self._updates_dir() / _UPDATE_SCRIPT_NAME
        script_path.parent.mkdir(parents=True, exist_ok=True)
        restart_command = self._build_update_restart_command(root_dir)
        script = f"""@echo off
setlocal
chcp 65001 >nul
set "PID={os.getpid()}"
set "SRC={source_dir}"
set "DST={root_dir}"
set "LOG={root_dir}\\{_UPDATES_DIR_NAME}\\update-error.log"
set /a WAIT_SECONDS=0

:wait_app
tasklist /FI "PID eq %PID%" /NH | findstr /R /C:" %PID% " >nul
if not errorlevel 1 (
  set /a WAIT_SECONDS+=1
  if %WAIT_SECONDS% GEQ 120 goto continue_update
  timeout /t 1 /nobreak >nul
  goto wait_app
)

:continue_update
if not exist "%SRC%\\" (
  echo Update source does not exist: %SRC%>"%LOG%"
  pause
  exit /b 1
)
if not exist "%DST%\\" (
  echo Update destination does not exist: %DST%>"%LOG%"
  pause
  exit /b 1
)

robocopy "%SRC%" "%DST%" /E /COPY:DAT /R:10 /W:1 /XD "{_UPDATES_DIR_NAME}" >nul
set "RC=%ERRORLEVEL%"
if %RC% GEQ 8 (
  echo Update failed with robocopy code %RC%>"%LOG%"
  pause
  exit /b %RC%
)

rmdir /s /q "%DST%\\{_UPDATES_DIR_NAME}\\{_UPDATE_EXTRACT_DIR_NAME}" 2>nul
del /q "%DST%\\{_UPDATES_DIR_NAME}\\*.zip" 2>nul
del /q "%DST%\\{_UPDATES_DIR_NAME}\\*.tmp" 2>nul
{restart_command}
del "%~f0" >nul 2>nul
"""
        script_path.write_text(script, encoding="utf-8")
        return script_path

    def _build_update_restart_command(self, root_dir: Path) -> str:
        if getattr(sys, "frozen", False):
            exe_path = root_dir / "codex_session.exe"
            return f'start "" /D "{root_dir}" "{exe_path}"'
        script_path = Path(sys.argv[0]).resolve()
        return f'start "" /D "{root_dir}" "{sys.executable}" "{script_path}"'

    def _post_update_progress(self, percent: int) -> None:
        try:
            self._post_ui(lambda value=percent: self._set_update_progress(value))
        except tk.TclError:
            pass

    def _set_update_progress(self, percent: int) -> None:
        value = max(0, min(percent, 100))
        self._update_status_var.set(f"下载更新 {value}%")
        if self._check_update_button is not None:
            self._check_update_button.config(text=f"{value}%")

    def _updates_dir(self) -> Path:
        return app_root() / _UPDATES_DIR_NAME

    def _cleanup_old_update_files(self) -> None:
        updates_dir = self._updates_dir()
        if not updates_dir.exists():
            return
        for path in updates_dir.iterdir():
            try:
                if path.is_dir():
                    shutil.rmtree(path)
                elif path.name != "update-error.log":
                    path.unlink()
            except OSError as exc:
                print(f"[Update] 清理旧更新文件失败: {path} {exc}", flush=True)

    def _open_file_location(self, path: Path) -> None:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", str(path)])
            return
        webbrowser.open(path.parent.as_uri())

    def _set_update_button_busy(self, text: str) -> None:
        if self._check_update_button is not None:
            self._check_update_button.config(text=text, state="disabled")

    def _set_update_button_idle(self) -> None:
        if self._check_update_button is not None:
            self._check_update_button.config(text="↻", state="normal")

    def refresh_all(self) -> None:
        install_count = self.refresh_installs(update_status=False)
        auth_count = self.refresh_auth_files(update_status=False)
        print(f"已刷新 {install_count} 项 / {auth_count} 个文件")

    def update_auth(self) -> None:
        if not self._refresh_config():
            return
        proxy_url = self._upstream_proxy if self._use_upstream_proxy else ""
        Thread(target=self._update_auth_worker, args=(proxy_url,), daemon=True).start()

    def _update_auth_worker(self, proxy_url: str) -> None:
        result: AuthLoginResult | None = None
        error = ""
        try:
            result = self.auth_login_service.login_and_save(proxy_url, log=lambda message: print(message, flush=True))
        except Exception as exc:
            error = str(exc)
        try:
            self._post_ui(lambda value=result, message=error: self._finish_update_auth(value, message))
        except tk.TclError:
            pass

    def _finish_update_auth(self, result: AuthLoginResult | None, error: str) -> None:
        if error:
            messagebox.showerror("更新授权失败", error)
            return

        self.refresh_auth_files(update_status=False)
        self.auth_usage_service.request_refresh()
        self._recompute_auto_load_target()
        account_text = result.account_id if result is not None and result.account_id else "未知账号"
        print(f"[ProxyWindow] 授权已保存: {account_text}", flush=True)

    def correct_traffic(self) -> None:
        if self._correct_traffic_refreshing:
            return
        self._correct_traffic_refreshing = True
        if self._correct_traffic_button is not None:
            self._correct_traffic_button.config(text="刷新额度中", state="disabled")
        Thread(target=self._refresh_quota_before_correct_traffic_worker, daemon=True).start()

    def _refresh_quota_before_correct_traffic_worker(self) -> None:
        error = ""
        try:
            self.auth_usage_service.refresh_once()
        except Exception as exc:
            error = str(exc)
        try:
            self._post_ui(lambda value=error: self._finish_correct_traffic_quota_refresh(value))
        except tk.TclError:
            pass

    def _finish_correct_traffic_quota_refresh(self, error: str) -> None:
        self._correct_traffic_refreshing = False
        if error:
            print(f"[ProxyWindow] 矫正流量前刷新额度失败: {error}", flush=True)
        self._apply_pending_quota_items()
        self.refresh_auth_files(update_status=False)
        self._request_manual_proxy_kill()
        self._start_correct_traffic_countdown()

    def clean_auth_files(self) -> None:
        if self._clean_auth_refreshing:
            return
        self._clean_auth_refreshing = True
        if self._clean_auth_button is not None:
            self._clean_auth_button.config(text="刷新额度中", state="disabled")
        Thread(target=self._clean_auth_files_worker, daemon=True).start()

    def _clean_auth_files_worker(self) -> None:
        refreshed_items: list[AuthQuotaItem] = []
        error = ""
        try:
            refreshed_items = self.auth_usage_service.refresh_once()
        except Exception as exc:
            error = str(exc)
        try:
            self._post_ui(lambda items=refreshed_items, value=error: self._finish_clean_auth_refresh(items, value))
        except tk.TclError:
            pass

    def _finish_clean_auth_refresh(self, refreshed_items: list[AuthQuotaItem], error: str) -> None:
        self._clean_auth_refreshing = False
        if self._clean_auth_button is not None:
            self._clean_auth_button.config(text="清理授权", state="normal")
        if error:
            messagebox.showerror("清理授权失败", f"刷新额度失败: {error}")
            return

        self._apply_pending_quota_items()
        rows = self.auth_sync_service.list_auth_rows()
        refreshed_tokens = {
            item.refresh_token
            for item in refreshed_items
            if item.refresh_token and self._is_normal_quota(item.quota)
        }
        delete_rows = self._build_clean_auth_delete_rows(rows, refreshed_tokens)
        self._print_clean_auth_plan(rows, refreshed_tokens, delete_rows)
        self.refresh_auth_files(update_status=False)

        if not delete_rows:
            messagebox.showinfo("清理授权", "没有发现需要清理的无效或重复授权。")
            return

        message = self._build_clean_auth_confirm_message(delete_rows)
        if not messagebox.askyesno("清理授权", message):
            return
        self._delete_clean_auth_rows(delete_rows)

    def refresh_all_tokens(self) -> None:
        if self._refresh_tokens_running:
            return
        if not self._refresh_config():
            return
        if not messagebox.askyesno("一键刷新令牌", "将尝试刷新所有本地授权文件的令牌。\n\n确认现在执行吗？"):
            return
        self._refresh_tokens_running = True
        if self._refresh_tokens_button is not None:
            self._refresh_tokens_button.config(text="刷新中", state="disabled")
        proxy_url = self._upstream_proxy if self._use_upstream_proxy else ""
        Thread(target=self._refresh_all_tokens_worker, args=(proxy_url,), daemon=True).start()

    def _refresh_all_tokens_worker(self, proxy_url: str) -> None:
        result = AuthTokenRefreshResult()
        error = ""
        try:
            result = self.auth_token_refresh_service.refresh_all(proxy_url)
        except Exception as exc:
            error = str(exc)
        try:
            self._post_ui(lambda value=result, message=error: self._finish_refresh_all_tokens(value, message))
        except tk.TclError:
            pass

    def _finish_refresh_all_tokens(self, result: AuthTokenRefreshResult, error: str) -> None:
        self._refresh_tokens_running = False
        if self._refresh_tokens_button is not None:
            self._refresh_tokens_button.config(text="一键刷新令牌", state="normal")
        if error:
            messagebox.showerror("刷新令牌失败", error)
            return

        self.refresh_auth_files(update_status=False)
        self._recompute_auto_load_target()
        message = (
            f"刷新完成。\n\n"
            f"总数: {result.total}\n"
            f"成功: {result.refreshed}\n"
            f"跳过: {result.skipped}\n"
            f"失败: {result.failed}"
        )
        if result.errors:
            message += "\n\n失败详情:\n" + "\n".join(result.errors[:8])
            if len(result.errors) > 8:
                message += f"\n... 还有 {len(result.errors) - 8} 条"
        if result.failed:
            messagebox.showwarning("刷新令牌完成", message)
        else:
            messagebox.showinfo("刷新令牌完成", message)

    def open_low_price_window(self) -> None:
        if self._low_price_window is not None and self._low_price_window.winfo_exists():
            self._low_price_window.lift()
            self._low_price_window.focus_force()
            return

        dialog = tk.Toplevel(self.root)
        self._low_price_window = dialog
        dialog.title("低价购号")
        dialog.transient(self.root)
        dialog.resizable(True, True)
        dialog.protocol("WM_DELETE_WINDOW", self._close_low_price_window)
        self._bind_dialog_close_keys(dialog)
        self._center_child_window_on_screen(dialog, 1320, 520)

        body = ttk.Frame(dialog, padding=12)
        body.pack(fill="both", expand=True)

        action_row = ttk.Frame(body)
        action_row.pack(fill="x", pady=(0, 8))
        self._low_price_refresh_button = ttk.Button(action_row, text="刷新", command=self.refresh_low_price_accounts)
        self._low_price_refresh_button.pack(side="right")

        search_wrap = ttk.Frame(action_row)
        search_wrap.pack(side="left", fill="x", expand=True, padx=(0, 12))
        ttk.Label(search_wrap, text="标题检索").pack(side="left", padx=(0, 8))
        search_entry = ttk.Entry(search_wrap, textvariable=self._low_price_search_var, width=44)
        search_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(search_wrap, text="清空", command=lambda: self._low_price_search_var.set("")).pack(
            side="left", padx=(8, 0)
        )
        ttk.Label(action_row, textvariable=self._low_price_search_status_var).pack(side="right", padx=(0, 12))
        self._low_price_search_var.set("")

        tree_frame = ttk.Frame(body)
        tree_frame.pack(fill="both", expand=True)

        columns = (
            "title",
            "details",
            "price",
            "sales",
            "credit",
            "reviews",
            "marketplaceYears",
            "positiveFeedback",
            "negativeFeedback",
            "storeSales",
        )
        self._low_price_tree = ttk.Treeview(tree_frame, columns=columns, show="headings", selectmode="browse")
        self._low_price_tree.heading("title", text="标题")
        self._low_price_tree.heading("details", text="规格")
        self._low_price_tree.heading("price", text="价格")
        self._low_price_tree.heading("sales", text="销量")
        self._low_price_tree.heading("credit", text="信用")
        self._low_price_tree.heading("reviews", text="评数")
        self._low_price_tree.heading("marketplaceYears", text="店龄")
        self._low_price_tree.heading("positiveFeedback", text="好评")
        self._low_price_tree.heading("negativeFeedback", text="差评")
        self._low_price_tree.heading("storeSales", text="店销")
        self._low_price_tree.column("title", width=430, anchor="w", stretch=True)
        self._low_price_tree.column("details", width=300, anchor="w", stretch=False)
        self._low_price_tree.column("price", width=70, anchor="center", stretch=False)
        self._low_price_tree.column("sales", width=70, anchor="center", stretch=False)
        self._low_price_tree.column("credit", width=60, anchor="center", stretch=False)
        self._low_price_tree.column("reviews", width=70, anchor="center", stretch=False)
        self._low_price_tree.column("marketplaceYears", width=70, anchor="center", stretch=False)
        self._low_price_tree.column("positiveFeedback", width=70, anchor="center", stretch=False)
        self._low_price_tree.column("negativeFeedback", width=60, anchor="center", stretch=False)
        self._low_price_tree.column("storeSales", width=80, anchor="center", stretch=False)
        self._low_price_tree.tag_configure("planKeyword", background="#fff4d6", foreground="#5c4300")
        self._low_price_tree.bind("<Motion>", self._on_low_price_tree_motion)
        self._low_price_tree.bind("<Leave>", lambda _event: self._hide_tooltip())
        self._low_price_tree.bind("<Double-1>", self._on_low_price_tree_double_click)
        self._low_price_tree.bind("<Configure>", lambda _event: self._refresh_low_price_title_cells())
        y_scroll = ttk.Scrollbar(
            tree_frame,
            orient="vertical",
            command=lambda *args: self._low_price_tree_yview(*args),
        )
        self._low_price_tree.configure(
            yscrollcommand=lambda first, last: self._low_price_tree_yscroll(y_scroll, first, last)
        )
        self._low_price_tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        dialog.bind("<Destroy>", self._on_low_price_window_destroy, add="+")
        self.refresh_low_price_accounts()

    def refresh_low_price_accounts(self) -> None:
        if self._low_price_tree is None or self._low_price_refreshing:
            return
        self._low_price_refresh_generation += 1
        refresh_generation = self._low_price_refresh_generation
        self._clear_low_price_detail_cache()
        self._low_price_refreshing = True
        self._set_low_price_buttons_state()
        proxy_url = self._get_proxy_for_usage_request()
        Thread(target=self._refresh_low_price_accounts_worker, args=(proxy_url, refresh_generation), daemon=True).start()

    def _refresh_low_price_accounts_worker(self, proxy_url: str, refresh_generation: int) -> None:
        try:
            self.low_price_account_service.start_refresh_cycle(proxy_url)
            items_by_product_id: dict[str, LowPriceAccount] = {}
            with ThreadPoolExecutor(max_workers=10) as executor:
                html_futures = [
                    executor.submit(self.low_price_account_service.fetch_accounts, proxy_url, page=page)
                    for page in range(1, _LOW_PRICE_FETCH_PAGE_COUNT + 1)
                ]
                catalog_futures = [
                    executor.submit(self.low_price_account_service.fetch_catalog_accounts, proxy_url, page=page)
                    for page in range(1, _LOW_PRICE_FETCH_PAGE_COUNT + 1)
                ]
                wait(html_futures + catalog_futures)
                for future in html_futures + catalog_futures:
                    for item in future.result():
                        items_by_product_id.setdefault(item.product_id, item)
            items = list(items_by_product_id.values())
            error = ""
        except Exception as exc:
            items = []
            error = str(exc)
        try:
            self._post_ui(lambda: self._finish_low_price_refresh(items, error, refresh_generation))
        except tk.TclError:
            pass

    def _finish_low_price_refresh(self, items: list[LowPriceAccount], error: str, refresh_generation: int) -> None:
        if refresh_generation != self._low_price_refresh_generation:
            return
        self._low_price_refreshing = False
        self._set_low_price_buttons_state()
        if self._low_price_tree is None:
            return
        if error:
            messagebox.showerror("低价购号刷新失败", error)
            return
        self._low_price_items_by_product_id.clear()
        for item in items:
            seller_info = self._low_price_seller_info_by_product_id.get(item.product_id)
            if seller_info is not None:
                item = self._apply_low_price_seller_info_to_item(item, seller_info)
            self._low_price_items_by_product_id[item.product_id] = item
        self._render_low_price_items()
        self._schedule_low_price_visible_seller_update()

    def _set_low_price_buttons_state(self) -> None:
        if self._low_price_refresh_button is None:
            return
        if not self._low_price_refreshing:
            self._low_price_refresh_button.config(text="刷新", state="normal")
            return
        self._low_price_refresh_button.config(text="刷新中", state="disabled")

    def _clear_low_price_detail_cache(self) -> None:
        self._low_price_seller_info_by_product_id.clear()
        self._low_price_details_by_product_id.clear()
        with self._low_price_seller_info_lock:
            self._low_price_seller_info_inflight.clear()
            self._low_price_seller_info_updated.clear()

    def _render_low_price_items(self) -> None:
        if self._low_price_tree is None:
            return
        for item_id in self._low_price_tree.get_children():
            self._low_price_tree.delete(item_id)
        self._low_price_product_id_by_item.clear()
        self._low_price_item_by_product_id.clear()
        self._low_price_titles_by_item.clear()
        self._low_price_details_by_item.clear()
        filtered_items = self._filtered_low_price_items()
        self._low_price_search_status_var.set(f"{len(filtered_items)} / {len(self._low_price_items_by_product_id)}")
        for item in sorted(filtered_items, key=self._low_price_sort_key):
            details = self._low_price_details_by_product_id.get(item.product_id, "")
            item_id = self._low_price_tree.insert(
                "",
                "end",
                values=self._low_price_row_values(item, details),
                tags=("planKeyword",) if self._is_low_price_plan_title(item.title) else (),
            )
            self._low_price_product_id_by_item[item_id] = item.product_id
            self._low_price_item_by_product_id[item.product_id] = item_id
            self._low_price_titles_by_item[item_id] = item.title
            if details:
                self._low_price_details_by_item[item_id] = details
        self._schedule_low_price_visible_seller_update()

    def _filtered_low_price_items(self) -> list[LowPriceAccount]:
        keyword = self._low_price_search_var.get().strip().casefold()
        items = list(self._low_price_items_by_product_id.values())
        if not keyword:
            return items
        return [item for item in items if keyword in item.title.casefold()]

    def _on_low_price_search_changed(self, *_args: object) -> None:
        if self._low_price_tree is None:
            return
        self._render_low_price_items()

    def _low_price_row_values(
        self,
        item: LowPriceAccount,
        details: str = "",
    ) -> tuple[str, str, str, str, str, str, str, str, str, str]:
        return (
            self._format_low_price_title(item.title),
            self._format_low_price_details_summary(details),
            item.price,
            item.sales,
            item.credit,
            item.reviews,
            item.marketplace_years,
            item.positive_feedback,
            item.negative_feedback,
            item.store_sales,
        )

    def _low_price_tree_yview(self, *args: object) -> None:
        if self._low_price_tree is None:
            return
        self._low_price_tree.yview(*args)
        self._schedule_low_price_visible_seller_update()

    def _low_price_tree_yscroll(self, scrollbar: ttk.Scrollbar, first: str, last: str) -> None:
        scrollbar.set(first, last)
        self._schedule_low_price_visible_seller_update()

    def _schedule_low_price_visible_seller_update(self) -> None:
        if self._low_price_tree is None or self._low_price_refreshing:
            return
        if self._low_price_visible_update_after_id is not None:
            try:
                self.root.after_cancel(self._low_price_visible_update_after_id)
            except tk.TclError:
                pass
        try:
            self._low_price_visible_update_after_id = self.root.after(120, self._update_visible_low_price_seller_info)
        except tk.TclError:
            self._low_price_visible_update_after_id = None

    def _update_visible_low_price_seller_info(self) -> None:
        self._low_price_visible_update_after_id = None
        if self._low_price_tree is None or self._low_price_refreshing:
            return
        visible_product_ids = self._get_visible_low_price_product_ids()
        if not visible_product_ids:
            self._schedule_low_price_visible_seller_update()
            return
        proxy_url = self._get_proxy_for_usage_request()
        refresh_generation = self._low_price_refresh_generation
        for product_id in visible_product_ids:
            item = self._low_price_items_by_product_id.get(product_id)
            if item is None or not item.href:
                continue
            cached_info = self._low_price_seller_info_by_product_id.get(product_id)
            cached_details = self._low_price_details_by_product_id.get(product_id)
            if cached_info is not None and cached_details is not None:
                self._apply_low_price_seller_info(product_id, cached_info)
                continue
            with self._low_price_seller_info_lock:
                if (
                    product_id in self._low_price_seller_info_updated
                    or product_id in self._low_price_seller_info_inflight
                ):
                    continue
                self._low_price_seller_info_inflight.add(product_id)
            self._low_price_seller_info_executor.submit(
                self._fetch_low_price_product_info_worker,
                product_id,
                item.href,
                proxy_url,
                refresh_generation,
            )

    def _get_visible_low_price_product_ids(self) -> list[str]:
        if self._low_price_tree is None:
            return []
        visible_product_ids: list[str] = []
        for item_id in self._low_price_tree.get_children():
            if not self._low_price_tree.bbox(item_id):
                continue
            product_id = self._low_price_product_id_by_item.get(item_id, "")
            if product_id:
                visible_product_ids.append(product_id)
        return visible_product_ids

    def _fetch_low_price_product_info_worker(
        self,
        product_id: str,
        href: str,
        proxy_url: str,
        refresh_generation: int,
    ) -> None:
        try:
            with self._low_price_seller_info_lock:
                if refresh_generation != self._low_price_refresh_generation:
                    self._low_price_seller_info_inflight.discard(product_id)
                    return
                if product_id in self._low_price_seller_info_updated:
                    self._low_price_seller_info_inflight.discard(product_id)
                    return
            product_info = self.low_price_account_service.fetch_product_info(href, proxy_url)
            error = ""
        except Exception as exc:
            product_info = LowPriceProductInfo(seller_info=LowPriceSellerInfo())
            error = str(exc)
        try:
            self._post_ui(
                lambda value=product_id, info=product_info, err=error, generation=refresh_generation: self._finish_low_price_product_info_update(
                    value,
                    info,
                    err,
                    generation,
                )
            )
        except tk.TclError:
            pass

    def _finish_low_price_product_info_update(
        self,
        product_id: str,
        product_info: LowPriceProductInfo,
        error: str,
        refresh_generation: int,
    ) -> None:
        with self._low_price_seller_info_lock:
            self._low_price_seller_info_inflight.discard(product_id)
            if refresh_generation != self._low_price_refresh_generation:
                return
            if not error:
                self._low_price_seller_info_updated.add(product_id)
        if error:
            print(f"[ProxyWindow] 更新低价购号详情失败 product_id={product_id}: {error}", flush=True)
            self._apply_low_price_details_text(product_id, "")
            return
        self._low_price_seller_info_by_product_id[product_id] = product_info.seller_info
        self._low_price_details_by_product_id[product_id] = self._format_low_price_details(product_info.variants)
        self._apply_low_price_seller_info(product_id, product_info.seller_info)

    def _apply_low_price_seller_info(self, product_id: str, seller_info: LowPriceSellerInfo) -> None:
        item = self._low_price_items_by_product_id.get(product_id)
        if item is None or self._low_price_tree is None:
            return
        item = self._apply_low_price_seller_info_to_item(item, seller_info)
        self._low_price_items_by_product_id[product_id] = item
        item_id = self._low_price_item_by_product_id.get(product_id, "")
        if item_id and self._low_price_tree.exists(item_id):
            details = self._low_price_details_by_product_id.get(product_id, "")
            if details:
                self._low_price_details_by_item[item_id] = details
            self._low_price_tree.item(
                item_id,
                values=self._low_price_row_values(item, details),
            )

    def _apply_low_price_details_text(self, product_id: str, details: str) -> None:
        item = self._low_price_items_by_product_id.get(product_id)
        if item is None or self._low_price_tree is None:
            return
        self._low_price_details_by_product_id[product_id] = details
        item_id = self._low_price_item_by_product_id.get(product_id, "")
        if item_id and self._low_price_tree.exists(item_id):
            self._low_price_details_by_item[item_id] = details
            self._low_price_tree.item(item_id, values=self._low_price_row_values(item, details))

    def _apply_low_price_seller_info_to_item(
        self,
        item: LowPriceAccount,
        seller_info: LowPriceSellerInfo,
    ) -> LowPriceAccount:
        return replace(
            item,
            credit=seller_info.credit,
            reviews=seller_info.reviews,
            marketplace_years=seller_info.marketplace_years,
            positive_feedback=seller_info.positive_feedback,
            negative_feedback=seller_info.negative_feedback,
            store_sales=seller_info.store_sales,
        )

    def _format_low_price_details(self, variants: tuple[LowPriceProductVariant, ...]) -> str:
        parts: list[str] = []
        for variant in variants:
            name = variant.name.strip()
            if not name:
                continue
            price = variant.price.strip() or "-"
            stock = variant.stock.strip() or "未知"
            parts.append(f"{name} / {price} / {stock}")
        return "\n".join(parts)

    def _format_low_price_details_summary(self, details: str) -> str:
        if not details:
            return ""
        if "/" not in details:
            return details
        values: list[str] = []
        for line in details.splitlines()[:3]:
            parts = [part.strip() for part in line.split("/")]
            if len(parts) < 2 or not parts[0]:
                continue
            name = self._compact_low_price_variant_name(parts[0])
            price = parts[1] if parts[1] != "-" else ""
            stock = parts[2] if len(parts) > 2 else ""
            if price and stock:
                values.append(f"{name}:{price}({stock})")
            elif stock:
                values.append(f"{name}:{stock}")
            else:
                values.append(f"{name}:{price}")
        return "; ".join(values)

    def _compact_low_price_variant_name(self, name: str) -> str:
        replacements = (
            ("ChatGPT Plus", "Plus"),
            ("ChatGPT", "GPT"),
            ("Business Team", "Team"),
            ("GPTMail:Pass:Email acces", "Mail"),
        )
        value = name
        for old, new in replacements:
            value = value.replace(old, new)
        value = " ".join(value.split())
        return value if len(value) <= 18 else f"{value[:18]}..."

    def _low_price_sort_key(self, item: LowPriceAccount) -> tuple[int, int, str]:
        plan_priority = 0 if self._is_low_price_plan_title(item.title) else 1
        return (plan_priority, self._price_sort_value(item.price), -self._sales_sort_value(item.sales), item.product_id)

    def _price_sort_value(self, price: str) -> float:
        normalized = price.replace(",", ".")
        match = re.search(r"\d+(?:\.\d+)?", normalized)
        if not match:
            return float("inf")
        return float(match.group(0))

    def _sales_sort_value(self, sales: str) -> int:
        digits = "".join(re.findall(r"\d+", sales))
        if not digits:
            return 0
        return int(digits)

    def _format_low_price_title(self, title: str) -> str:
        if self._low_price_tree is None:
            return title
        available_width = max(int(self._low_price_tree.column("title", "width")) - 16, 40)
        font = tkfont.nametofont("TkDefaultFont")
        if font.measure(title) <= available_width:
            return title

        ellipsis = "..."
        ellipsis_width = font.measure(ellipsis)
        if ellipsis_width >= available_width:
            return ellipsis

        low = 0
        high = len(title)
        while low < high:
            mid = (low + high + 1) // 2
            if font.measure(title[:mid]) + ellipsis_width <= available_width:
                low = mid
            else:
                high = mid - 1
        return f"{title[:low]}{ellipsis}"

    def _refresh_low_price_title_cells(self) -> None:
        if self._low_price_tree is None:
            return
        for item_id, product_id in self._low_price_product_id_by_item.items():
            item = self._low_price_items_by_product_id.get(product_id)
            if item is None or not self._low_price_tree.exists(item_id):
                continue
            details = self._low_price_details_by_product_id.get(product_id, "")
            self._low_price_tree.item(item_id, values=self._low_price_row_values(item, details))

    def _is_low_price_plan_title(self, title: str) -> bool:
        return re.search(r"\b(team|plus|pro|business)\b", title, re.IGNORECASE) is not None

    def _on_low_price_tree_motion(self, event: tk.Event) -> None:
        if self._low_price_tree is None:
            self._hide_tooltip()
            return
        row_id = self._low_price_tree.identify_row(event.y)
        column = self._low_price_tree.identify_column(event.x)
        title = self._low_price_titles_by_item.get(row_id, "")
        details = self._low_price_details_by_item.get(row_id, "")
        if row_id and column == "#1" and title:
            self._show_tooltip(event.x_root, event.y_root, title)
            return
        if row_id and column == "#2" and details:
            self._show_tooltip(event.x_root, event.y_root, details)
            return
        self._hide_tooltip()

    def _on_low_price_tree_double_click(self, event: tk.Event) -> None:
        if self._low_price_tree is None:
            return
        row_id = self._low_price_tree.identify_row(event.y)
        product_id = self._low_price_product_id_by_item.get(row_id, "")
        item = self._low_price_items_by_product_id.get(product_id)
        if item is None or not item.href:
            return
        webbrowser.open(self._build_low_price_url(item.href))

    def _build_low_price_url(self, href: str) -> str:
        path = href if href.startswith("/") else f"/{href}"
        separator = "&" if "?" in path else "?"
        return f"https://plati.market{path}{separator}ai=1426781"

    def _close_low_price_window(self) -> None:
        if self._low_price_window is not None and self._low_price_window.winfo_exists():
            self._low_price_window.destroy()

    def _on_low_price_window_destroy(self, event: tk.Event) -> None:
        if event.widget is self._low_price_window:
            self._low_price_refresh_generation += 1
            self._low_price_refreshing = False
            if self._low_price_visible_update_after_id is not None:
                try:
                    self.root.after_cancel(self._low_price_visible_update_after_id)
                except tk.TclError:
                    pass
                self._low_price_visible_update_after_id = None
            self._low_price_window = None
            self._low_price_tree = None
            self._low_price_refresh_button = None
            self._low_price_items_by_product_id.clear()
            self._low_price_product_id_by_item.clear()
            self._low_price_item_by_product_id.clear()
            self._low_price_titles_by_item.clear()
            self._low_price_details_by_item.clear()
            self._low_price_details_by_product_id.clear()
            self._low_price_seller_info_by_product_id.clear()
            with self._low_price_seller_info_lock:
                self._low_price_seller_info_inflight.clear()
                self._low_price_seller_info_updated.clear()

    def _start_correct_traffic_countdown(self) -> None:
        if self._correct_traffic_after_id is not None:
            try:
                self.root.after_cancel(self._correct_traffic_after_id)
            except tk.TclError:
                pass
            self._correct_traffic_after_id = None
        self._update_correct_traffic_countdown(5)

    def _update_correct_traffic_countdown(self, seconds: int) -> None:
        if self._correct_traffic_button is None:
            return
        if seconds <= 0:
            self._correct_traffic_button.config(text="矫正流量", state="normal")
            self._correct_traffic_after_id = None
            return
        self._correct_traffic_button.config(text=f"重置中 {seconds}", state="disabled")
        self._correct_traffic_after_id = self.root.after(
            1000,
            lambda: self._update_correct_traffic_countdown(seconds - 1),
        )

    def _kill_process_by_image_name(self, image_name: str) -> None:
        result = subprocess.run(
            ["taskkill", "/im", image_name, "/f"],
            capture_output=True,
            text=True,
            encoding="gbk",
            errors="replace",
        )
        output = (result.stdout or result.stderr).strip()
        if result.returncode == 0:
            print(f"[ProxyWindow] 已结束进程: {image_name}", flush=True)
        else:
            print(f"[ProxyWindow] 结束进程跳过: {image_name} {output}", flush=True)

    def _get_default_codex_launch_row(self) -> CodexInstallRow | None:
        rows = list(self._rows_by_item.values()) or self._scan_codex_installs()
        if not rows:
            return None
        for row in rows:
            if row.path.replace("/", "\\").endswith("\\app\\Codex.exe"):
                return row
        return rows[0]

    def kill_codex_processes(self) -> None:
        process_rows = self._list_codex_process_rows()
        dialog = tk.Toplevel(self.root)
        dialog.title("结束进程")
        dialog.transient(self.root)
        dialog.resizable(True, True)
        dialog.grab_set()
        self._bind_dialog_close_keys(dialog)
        self._center_child_window(dialog, 520, 430)
        dialog.minsize(520, 430)
        dialog.rowconfigure(0, weight=1)
        dialog.columnconfigure(0, weight=1)

        body = ttk.Frame(dialog, padding=12)
        body.grid(row=0, column=0, sticky="nsew")
        body.rowconfigure(1, weight=1)
        body.columnconfigure(0, weight=1)

        ttk.Label(body, text="选择要强制结束的进程").grid(row=0, column=0, sticky="w")
        tree_frame = ttk.Frame(body)
        tree_frame.grid(row=1, column=0, sticky="nsew", pady=(8, 10))

        process_tree = ttk.Treeview(tree_frame, columns=("pid",), selectmode="extended")
        process_tree.heading("#0", text="进程名称")
        process_tree.heading("pid", text="PID")
        process_tree.column("#0", width=280, anchor="w")
        process_tree.column("pid", width=120, anchor="center", stretch=False)
        y_scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=process_tree.yview)
        process_tree.configure(yscrollcommand=y_scroll.set)
        process_tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)

        item_depths: dict[str, int] = {}

        def insert_process(parent: str, row: CodexProcessRow, depth: int) -> None:
            item_id = str(row.pid)
            if item_id in item_depths:
                return
            process_tree.insert(parent, "end", iid=item_id, text=row.name, values=(row.pid,), open=True)
            item_depths[item_id] = depth
            for child in row.children:
                insert_process(item_id, child, depth + 1)

        for process_row in process_rows:
            insert_process("", process_row, 0)
        all_items = tuple(item_depths)
        root_items = process_tree.get_children("")
        if all_items:
            def select_root_process_items() -> None:
                process_tree.selection_set(root_items)
                process_tree.focus(root_items[0])
                process_tree.see(root_items[0])
                process_tree.focus_set()

            dialog.after(0, select_root_process_items)
        else:
            process_tree.insert("", "end", text="未找到 Codex.exe 或 codex.exe 进程", values=("",))

        button_row = ttk.Frame(dialog, padding=(12, 0, 12, 12))
        button_row.grid(row=1, column=0, sticky="ew")
        button_row.columnconfigure(0, weight=1)
        button_row.columnconfigure(1, weight=0)
        button_row.columnconfigure(2, weight=1)

        def kill_selected() -> None:
            selected_items = [item for item in process_tree.selection() if item in item_depths]
            if not selected_items:
                messagebox.showwarning("结束进程", "请先选择要结束的进程。", parent=dialog)
                return
            messages = self._kill_process_ids([int(item) for item in selected_items], item_depths)
            if messages:
                messagebox.showerror("结束进程失败", "\n".join(messages), parent=dialog)
            dialog.destroy()

        end_button = ttk.Button(button_row, text="结束选中进程", command=kill_selected)
        end_button.grid(row=0, column=1)
        if not all_items:
            end_button.config(state="disabled")

        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.wait_window()

    def _list_codex_process_rows(self) -> list[CodexProcessRow]:
        target_names = {"Codex.exe", "codex.exe"}
        root_processes: list[psutil.Process] = []
        for process in psutil.process_iter(["name"]):
            try:
                if process.info.get("name") in target_names:
                    root_processes.append(process)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        child_pids = {
            child.pid
            for process in root_processes
            for child in self._safe_children(process, recursive=True)
        }
        roots = [process for process in root_processes if process.pid not in child_pids]
        return [self._build_process_row(process) for process in sorted(roots, key=lambda item: item.pid)]

    def _build_process_row(self, process: psutil.Process) -> CodexProcessRow:
        try:
            process_name = process.name()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            process_name = "<无法读取>"
        children = [self._build_process_row(child) for child in self._safe_children(process)]
        children.sort(key=lambda row: row.pid)
        return CodexProcessRow(pid=process.pid, name=process_name, children=children)

    def _safe_children(self, process: psutil.Process, recursive: bool = False) -> list[psutil.Process]:
        try:
            return process.children(recursive=recursive)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return []

    def _kill_process_ids(self, process_ids: list[int], item_depths: dict[str, int]) -> list[str]:
        failed_messages: list[str] = []
        ordered_process_ids = sorted(process_ids, key=lambda pid: item_depths.get(str(pid), 0), reverse=True)
        for process_id in ordered_process_ids:
            result = subprocess.run(
                ["taskkill", "/pid", str(process_id), "/t", "/f"],
                capture_output=True,
                text=True,
                encoding="gbk",
                errors="replace",
            )
            output = (result.stdout or result.stderr).strip()
            if result.returncode != 0:
                failed_messages.append(f"PID {process_id}: {output or '结束失败'}")
        return failed_messages

    def refresh_installs(self, update_status: bool = True) -> int:
        self._destroy_launch_buttons()
        for item in self.tree.get_children():
            self.tree.delete(item)
        self._rows_by_item.clear()
        rows = self._scan_codex_installs()
        for row in rows:
            item = self.tree.insert("", "end", values=(row.name, row.display_path, row.size, row.version, ""))
            self._rows_by_item[item] = row
        self._schedule_refresh_launch_buttons()
        signature = tuple((row.name, row.display_path, row.size, row.version) for row in rows)
        if update_status and signature != self._last_install_rows_signature:
            print(f"已刷新 {len(rows)} 项")
        self._last_install_rows_signature = signature
        return len(rows)

    def refresh_auth_files(self, update_status: bool = True) -> int:
        selected_row = self._get_selected_auth_row()
        selected_refresh_token = selected_row.refresh_token if selected_row is not None else ""
        selected_item = ""
        for item in self.auth_tree.get_children():
            self.auth_tree.delete(item)
        self._auth_rows_by_item.clear()
        rows = self.auth_sync_service.list_auth_rows()
        load_refresh_token = self._get_auto_load_target_refresh_token()
        for row in rows:
            item = self.auth_tree.insert(
                "",
                "end",
                values=(
                    "★" if row.current else "",
                    self._format_auth_load_mark(row, load_refresh_token),
                    self._format_auth_load_strategy(row.load_strategy),
                    self._shorten_middle(row.account_id, 16, 10),
                    self._shorten_middle(row.email, 18, 12),
                    self._format_last_refresh(row.last_refresh),
                    self._format_last_refresh(row.quota_refresh_time_5h),
                    row.quota,
                    row.plan_type or "",
                ),
            )
            self._auth_rows_by_item[item] = row
            if row.refresh_token == selected_refresh_token:
                selected_item = item
        if selected_item:
            self.auth_tree.selection_set(selected_item)
            self.auth_tree.focus(selected_item)
        signature = tuple(
            (
                row.current,
                row.disabled,
                row.load_strategy,
                row.refresh_token == load_refresh_token,
                row.account_id,
                row.email,
                row.note,
                row.last_refresh,
                row.quota_refresh_time_5h,
                row.quota,
                row.plan_type or "",
            )
            for row in rows
        )
        if update_status and signature != self._last_auth_rows_signature:
            print(f"已刷新 {len(rows)} 个文件")
        self._last_auth_rows_signature = signature
        self._refresh_tray_icon_tooltip(rows)
        return len(rows)

    def _scan_codex_installs(self) -> list[CodexInstallRow]:
        rows: list[CodexInstallRow] = []
        rows.extend(self._scan_windowsapps_codex())
        rows.extend(self._scan_node_codex())
        return [row for row in rows if self._is_supported_codex_executable(row.path)]

    def _is_supported_codex_executable(self, path: str) -> bool:
        normalized_path = path.replace("/", "\\")
        return (
            normalized_path.endswith("\\app\\Codex.exe")
            or normalized_path.endswith("\\codex\\codex.exe")
            or normalized_path.endswith("\\Code.exe")
            or normalized_path.endswith("\\code.exe")
        )

    def _scan_windowsapps_codex(self) -> list[CodexInstallRow]:
        base_dir = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "WindowsApps"
        if not base_dir.exists():
            return []

        rows: list[CodexInstallRow] = []
        for app_dir in sorted(base_dir.iterdir(), key=lambda p: p.name):
            if not app_dir.is_dir() or not app_dir.name.startswith("OpenAI.Codex"):
                continue
            for exe_path in self._iter_codex_executables(app_dir):
                rows.append(
                    CodexInstallRow(
                        name=self._shorten_name(exe_path.name),
                        path=str(exe_path),
                        display_path=self._shorten_path(str(exe_path)),
                        size=self._format_size(exe_path.stat().st_size),
                        version=self._extract_version(app_dir.name),
                    )
                )
        return rows

    def _scan_node_codex(self) -> list[CodexInstallRow]:
        node_exe = self._locate_node_exe()
        if not node_exe:
            return []

        codex_root = node_exe.parent / "node_modules" / "@openai" / "codex"
        if not codex_root.exists():
            return []

        rows: list[CodexInstallRow] = []
        for exe_path in self._iter_code_executables(codex_root):
            rows.append(
                CodexInstallRow(
                    name=self._shorten_name(exe_path.name),
                    path=str(exe_path),
                    display_path=self._shorten_path(str(exe_path)),
                    size=self._format_size(exe_path.stat().st_size),
                    version="",
                )
            )
        return rows

    def _locate_node_exe(self) -> Path | None:
        located = shutil.which("node.exe") or shutil.which("node")
        if located:
            return Path(located)
        path_value = os.environ.get("PATH", "")
        for raw_dir in path_value.split(os.pathsep):
            if not raw_dir:
                continue
            candidate = Path(raw_dir) / "node.exe"
            if candidate.exists():
                return candidate
        return None

    def _shorten_path(self, path: str, max_length: int = 72) -> str:
        if len(path) <= max_length:
            return path
        parts = Path(path).parts
        if len(parts) >= 4:
            tail = Path(*parts[-4:])
            short = f"...\\{tail}"
            if len(short) <= max_length:
                return short
        if len(parts) >= 3:
            tail = Path(*parts[-3:])
            short = f"...\\{tail}"
            if len(short) <= max_length:
                return short
        keep = max_length - 3
        return f"{path[:keep]}..."

    def _shorten_name(self, name: str, max_length: int = 10) -> str:
        if len(name) <= max_length:
            return name
        stem = Path(name).stem
        suffix = Path(name).suffix
        if len(stem) > 6:
            stem = f"{stem[:4]}..."
        return f"{stem}{suffix}"

    def _shorten_middle(self, text: str, keep_prefix: int = 14, keep_suffix: int = 10) -> str:
        if len(text) <= keep_prefix + keep_suffix + 3:
            return text
        return f"{text[:keep_prefix]}...{text[-keep_suffix:]}"

    def _quota_priority(self, quota: str) -> float:
        percentages = self._quota_percentages(quota)
        if not percentages:
            return -1.0
        return percentages[0]

    def _quota_percentages(self, quota: str) -> list[float]:
        if not quota:
            return []
        values: list[float] = []
        for match in re.finditer(r"(\d+(?:\.\d+)?)%", quota):
            try:
                values.append(float(match.group(1)))
            except ValueError:
                continue
        return values

    def _is_auto_load_quota_usable(self, quota: str) -> bool:
        percentages = self._quota_percentages(quota)
        if not percentages or percentages[0] <= 0:
            return False
        if len(percentages) >= 2 and percentages[1] <= 0:
            return False
        return True

    def _format_auth_load_mark(self, row: AuthFileRow, load_refresh_token: str) -> str:
        if row.refresh_token == load_refresh_token:
            return "●"
        return ""

    def _format_auth_load_strategy(self, load_strategy: str) -> str:
        if load_strategy == "priority":
            return "优先"
        if load_strategy == "disabled":
            return "禁用"
        return "正常"

    def _is_normal_quota(self, quota: str) -> bool:
        value = quota.strip()
        return bool(value and value not in {"—", QUOTA_REFRESH_FAILED})

    def _last_refresh_sort_key(self, text: str) -> float:
        if not text:
            return 0.0
        try:
            return self._parse_display_datetime(text).timestamp()
        except ValueError:
            return 0.0

    def _build_clean_auth_delete_rows(
        self,
        rows: list[AuthFileRow],
        refreshed_tokens: set[str],
    ) -> list[AuthFileRow]:
        delete_rows_by_token: dict[str, AuthFileRow] = {
            row.refresh_token: row
            for row in rows
            if row.refresh_token and row.refresh_token not in refreshed_tokens
        }
        rows_by_account_id: dict[str, list[AuthFileRow]] = {}
        for row in rows:
            if not row.account_id:
                continue
            rows_by_account_id.setdefault(row.account_id, []).append(row)

        for account_rows in rows_by_account_id.values():
            if len(account_rows) <= 1:
                continue
            normal_rows = [row for row in account_rows if row.refresh_token in refreshed_tokens]
            if not normal_rows:
                continue
            keep_row = max(normal_rows, key=lambda item: (self._last_refresh_sort_key(item.last_refresh), item.refresh_token))
            for row in account_rows:
                if row.refresh_token != keep_row.refresh_token:
                    delete_rows_by_token[row.refresh_token] = row
        return list(delete_rows_by_token.values())

    def _print_clean_auth_plan(
        self,
        rows: list[AuthFileRow],
        refreshed_tokens: set[str],
        delete_rows: list[AuthFileRow],
    ) -> None:
        delete_tokens = {row.refresh_token for row in delete_rows}
        print("[ProxyWindow] 清理授权刷新成功队列:", flush=True)
        for row in rows:
            if row.refresh_token not in refreshed_tokens:
                continue
            print(
                f"  account_id={row.account_id} refresh_token={row.refresh_token} "
                f"last_refresh={row.last_refresh} quota={row.quota}",
                flush=True,
            )
        print("[ProxyWindow] 清理授权待删除队列:", flush=True)
        if not delete_rows:
            print("  无", flush=True)
            return
        for row in delete_rows:
            print(
                f"  account_id={row.account_id} refresh_token={row.refresh_token} "
                f"last_refresh={row.last_refresh} quota={row.quota} "
                f"{'current' if row.current else ''} load_strategy={row.load_strategy}",
                flush=True,
            )
        kept_rows = [row for row in rows if row.account_id and row.refresh_token not in delete_tokens]
        print("[ProxyWindow] 清理授权保留队列:", flush=True)
        for row in kept_rows:
            if any(item.account_id == row.account_id for item in delete_rows):
                print(
                    f"  account_id={row.account_id} refresh_token={row.refresh_token} "
                    f"last_refresh={row.last_refresh} quota={row.quota}",
                    flush=True,
                )

    def _build_clean_auth_confirm_message(self, delete_rows: list[AuthFileRow]) -> str:
        lines = [
            "确认清理无效授权吗？",
            "",
            f"将删除 {len(delete_rows)} 个授权文件：额度刷新失败的授权会被清理；同账户多个授权都有效时，仅保留令牌刷新时间最新的授权。",
            "",
            "待删除:",
        ]
        for row in delete_rows[:12]:
            lines.append(
                f"- {self._shorten_middle(row.account_id, 16, 10)} "
                f"{self._format_last_refresh(row.last_refresh)} "
                f"{self._redact_middle(row.refresh_token)}"
            )
        if len(delete_rows) > 12:
            lines.append(f"... 其余 {len(delete_rows) - 12} 个已打印到日志")
        return "\n".join(lines)

    def _delete_clean_auth_rows(self, delete_rows: list[AuthFileRow]) -> None:
        deleted_tokens: set[str] = set()
        errors: list[str] = []
        for row in delete_rows:
            ok, message = self.auth_sync_service.delete_auth_file(row.refresh_token)
            if ok:
                deleted_tokens.add(row.refresh_token)
                continue
            errors.append(f"{row.refresh_token}: {message}")

        if self._get_auto_load_target_refresh_token() in deleted_tokens:
            self._set_auto_load_target("", "")
        self.auth_usage_service.remove_tokens(deleted_tokens)
        for refresh_token in deleted_tokens:
            self._quota_priority_by_refresh_token.pop(refresh_token, None)
        if self.auto_load_var.get():
            self._recompute_auto_load_target()
        self.refresh_auth_files(update_status=False)

        if errors:
            messagebox.showerror("清理授权失败", "\n".join(errors[:8]))
            return
        messagebox.showinfo("清理授权", f"已清理 {len(deleted_tokens)} 个授权文件。")

    def _format_last_refresh(self, text: str) -> str:
        return self._format_display_datetime(text, "%Y-%m-%d %H:%M")

    def _format_tooltip_time(self, text: str) -> str:
        return self._format_display_datetime(text, "%Y-%m-%d %H:%M:%S")

    def _format_display_datetime(self, text: str, fmt: str) -> str:
        if not text:
            return ""
        try:
            value = self._parse_display_datetime(text)
        except ValueError:
            return text
        return value.strftime(fmt)

    def _parse_display_datetime(self, text: str) -> datetime:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if value.tzinfo is None:
            return value
        return value.astimezone(_CHINA_TIMEZONE)

    def _redact_middle(self, text: str, keep_prefix: int = 12, keep_suffix: int = 8) -> str:
        if not text:
            return "-"
        if len(text) <= keep_prefix + keep_suffix + 3:
            return text
        return f"{text[:keep_prefix]}...{text[-keep_suffix:]}"

    def _build_auth_tooltip(self, row: AuthFileRow) -> str:
        quota = row.quota or ""
        lines = [
            f"账户ID: {row.account_id or '-'}",
            f"用户ID: {row.user_id or ''}",
            f"邮箱: {row.email or ''}",
            f"备注: {row.note or ''}",
            f"刷新令牌: {row.refresh_token or '-'}",
            f"访问令牌: {row.access_token or ''}",
            f"令牌刷新时间: {self._format_tooltip_time(row.last_refresh)}",
            f"额度刷新时间(5小时): {self._format_tooltip_time(row.quota_refresh_time_5h)}",
            f"额度刷新时间(7天): {self._format_tooltip_time(row.quota_refresh_time_7d)}",
            f"额度(5h/7d): {quota}",
            f"类型: {row.plan_type or ''}",
            f"负载策略: {self._format_auth_load_strategy(row.load_strategy)}",
        ]
        return "\n".join(lines)

    def _on_tree_motion(self, event: tk.Event) -> None:
        row_id = self.tree.identify_row(event.y)
        column = self.tree.identify_column(event.x)
        if not row_id:
            self.tree.configure(cursor="")
            self._hide_tooltip()
            return
        row = self._rows_by_item.get(row_id)
        if not row:
            self.tree.configure(cursor="")
            self._hide_tooltip()
            return
        if column == "#5":
            self.tree.configure(cursor="hand2")
            self._show_tooltip(event.x_root, event.y_root, "启动")
            return
        if column in {"#1", "#2", "#3", "#4"}:
            self.tree.configure(cursor="")
            self._show_tooltip(event.x_root, event.y_root, row.path if column == "#2" else row.name)
            return
        self.tree.configure(cursor="")
        self._hide_tooltip()

    def _on_auth_tree_motion(self, event: tk.Event) -> None:
        row_id = self.auth_tree.identify_row(event.y)
        if not row_id:
            self.auth_tree.configure(cursor="")
            self._hide_tooltip()
            return
        row = self._auth_rows_by_item.get(row_id)
        if not row:
            self.auth_tree.configure(cursor="")
            self._hide_tooltip()
            return
        self.auth_tree.configure(cursor="")
        self._show_tooltip(event.x_root, event.y_root, self._build_auth_tooltip(row))

    def _on_auth_tree_double_click(self, event: tk.Event) -> None:
        row_id = self.auth_tree.identify_row(event.y)
        if not row_id:
            return
        row = self._auth_rows_by_item.get(row_id)
        if not row:
            return
        self._activate_auth_row(row)

    def _on_auth_tree_right_click(self, event: tk.Event) -> None:
        row_id = self.auth_tree.identify_row(event.y)
        if not row_id:
            return
        row = self._auth_rows_by_item.get(row_id)
        if not row or self._auth_menu is None:
            return
        self.auth_tree.selection_set(row_id)
        self.auth_tree.focus(row_id)
        self._auth_load_strategy_var.set(row.load_strategy)
        try:
            self._auth_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._auth_menu.grab_release()

    def _on_auth_tree_delete_key(self, _event: tk.Event) -> str:
        self._delete_selected_auth_row()
        return "break"

    def _activate_selected_auth_row(self) -> None:
        row = self._get_selected_auth_row()
        if row is not None:
            self._activate_auth_row(row)

    def _delete_selected_auth_row(self) -> None:
        row = self._get_selected_auth_row()
        if row is not None:
            self._delete_auth_row(row)

    def _set_selected_auth_load_strategy(self, load_strategy: str) -> None:
        row = self._get_selected_auth_row()
        if row is not None:
            self._set_auth_row_load_strategy(row, load_strategy)

    def _edit_selected_auth_note(self) -> None:
        row = self._get_selected_auth_row()
        if row is None:
            return
        note = self._ask_auth_note(row.note)
        if note is None:
            return
        ok, message = self.auth_sync_service.set_auth_note(row.refresh_token, note)
        if not ok:
            messagebox.showerror("更新备注失败", message)
            return
        self.refresh_auth_files()

    def _ask_auth_note(self, initial_note: str) -> str | None:
        dialog = tk.Toplevel(self.root)
        dialog.title("备注")
        dialog.transient(self.root)
        dialog.resizable(False, False)

        result: dict[str, str | None] = {"value": None}

        body = ttk.Frame(dialog, padding=12)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="备注（留空清除）").pack(anchor="w")

        text = ttk.Entry(body, width=64)
        text.pack(fill="x", pady=(6, 10))
        if initial_note:
            text.insert(0, self._normalize_auth_note(initial_note))

        buttons = ttk.Frame(body)
        buttons.pack(fill="x")

        def save() -> None:
            result["value"] = self._normalize_auth_note(text.get())
            dialog.destroy()

        def cancel() -> None:
            dialog.destroy()

        ttk.Button(buttons, text="保存", command=save).pack(side="right")
        ttk.Button(buttons, text="取消", command=cancel).pack(side="right", padx=(0, 8))
        dialog.bind("<Escape>", lambda _event: cancel())
        dialog.bind("<Return>", lambda _event: save())
        dialog.protocol("WM_DELETE_WINDOW", cancel)
        dialog.update_idletasks()
        self._center_child_window_to_content(dialog)
        dialog.grab_set()
        text.focus_set()
        text.selection_range(0, "end")
        self.root.wait_window(dialog)
        return result["value"]

    def _normalize_auth_note(self, note: str) -> str:
        return " ".join(note.split())

    def _center_child_window_to_content(self, window: tk.Toplevel) -> None:
        self.root.update_idletasks()
        width = window.winfo_width()
        height = window.winfo_height()
        root_x = self.root.winfo_rootx()
        root_y = self.root.winfo_rooty()
        root_width = self.root.winfo_width()
        root_height = self.root.winfo_height()
        x = root_x + max((root_width - width) // 2, 0)
        y = root_y + max((root_height - height) // 2, 0)
        window.geometry(f"+{x}+{y}")

    def _get_selected_auth_row(self) -> AuthFileRow | None:
        selection = self.auth_tree.selection()
        if not selection:
            return None
        return self._auth_rows_by_item.get(selection[0])

    def _activate_auth_row(self, row: AuthFileRow) -> None:
        if not messagebox.askyesno("切换授权文件", f"确认切换到该授权文件吗？\n\n账户ID: {row.account_id}\n刷新令牌: {row.refresh_token}"):
            return
        ok, message = self.auth_sync_service.activate_auth_file(row.refresh_token)
        if not ok:
            messagebox.showerror("切换失败", message)
            return
        self.refresh_auth_files()

    def _delete_auth_row(self, row: AuthFileRow) -> None:
        if not messagebox.askyesno("删除授权文件", f"确认删除该授权文件吗？\n\n账户ID: {row.account_id}\n刷新令牌: {row.refresh_token}"):
            return
        ok, message = self.auth_sync_service.delete_auth_file(row.refresh_token)
        if not ok:
            messagebox.showerror("删除失败", message)
            return
        if row.refresh_token == self._get_auto_load_target_refresh_token():
            self._set_auto_load_target("", "")
            if self.auto_load_var.get():
                self._recompute_auto_load_target()
        self.refresh_auth_files()

    def _set_auth_row_load_strategy(self, row: AuthFileRow, load_strategy: str) -> None:
        ok, message = self.auth_sync_service.set_auth_load_strategy(row.refresh_token, load_strategy)
        if not ok:
            messagebox.showerror("更新负载策略失败", message)
            return
        if load_strategy == "disabled" and row.refresh_token == self._get_auto_load_target_refresh_token():
            self._set_auto_load_target("", "")
            self._clear_proxy_kill_pending()
        if self.auto_load_var.get():
            self._recompute_auto_load_target()
        self.refresh_auth_files()

    def _show_tooltip(self, x: int, y: int, text: str) -> None:
        if self._tooltip is None:
            self._tooltip = tk.Toplevel(self.root)
            self._tooltip.overrideredirect(True)
            self._tooltip.attributes("-topmost", True)
            self._tooltip_label = ttk.Label(
                self._tooltip,
                text=text,
                padding=(8, 4),
                relief="solid",
                justify="left",
                anchor="w",
            )
            self._tooltip_label.pack()
        else:
            assert self._tooltip_label is not None
            self._tooltip_label.config(text=text)
        self._tooltip.geometry(f"+{x + 12}+{y + 12}")
        self._tooltip.deiconify()

    def _hide_tooltip(self) -> None:
        if self._tooltip is not None:
            self._tooltip.withdraw()

    def _bind_widget_tooltip(self, widget: tk.Widget, text: str) -> None:
        widget.bind("<Enter>", lambda event: self._show_tooltip(event.x_root, event.y_root, text))
        widget.bind("<Motion>", lambda event: self._show_tooltip(event.x_root, event.y_root, text))
        widget.bind("<Leave>", lambda _event: self._hide_tooltip())

    def _schedule_refresh_launch_buttons(self) -> None:
        if self._refresh_launch_buttons_after_id is not None:
            try:
                self.root.after_cancel(self._refresh_launch_buttons_after_id)
            except tk.TclError:
                pass
        try:
            self._refresh_launch_buttons_after_id = self.root.after_idle(self._refresh_launch_buttons)
        except tk.TclError:
            self._refresh_launch_buttons_after_id = None

    def _on_tree_click(self, event: tk.Event) -> None:
        self.tree.selection_remove(self.tree.selection())
        region = self.tree.identify_region(event.x, event.y)
        if region != "cell":
            return
        row_id = self.tree.identify_row(event.y)
        column = self.tree.identify_column(event.x)
        if not row_id or column != "#5":
            return
        row = self._rows_by_item.get(row_id)
        if not row:
            return
        self._launch_codex(row)

    def _destroy_launch_buttons(self) -> None:
        if self._refresh_launch_buttons_after_id is not None:
            try:
                self.root.after_cancel(self._refresh_launch_buttons_after_id)
            except tk.TclError:
                pass
            self._refresh_launch_buttons_after_id = None
        for button in self._launch_buttons.values():
            button.destroy()
        self._launch_buttons.clear()

    def _refresh_launch_buttons(self) -> None:
        self._refresh_launch_buttons_after_id = None
        if self._install_tree_wrap is None or not hasattr(self, "tree"):
            return
        try:
            self.tree.update_idletasks()
        except tk.TclError:
            return

        current_items = set(self.tree.get_children())
        stale_items = set(self._launch_buttons) - current_items
        for item in stale_items:
            self._launch_buttons.pop(item).destroy()

        tree_x = self.tree.winfo_x()
        tree_y = self.tree.winfo_y()
        for item in self.tree.get_children():
            row = self._rows_by_item.get(item)
            if row is None:
                continue
            button = self._launch_buttons.get(item)
            if button is None:
                button = ttk.Button(
                    self._install_tree_wrap,
                    text="启动",
                    command=lambda launch_row=row: self._launch_codex(launch_row),
                )
                self._launch_buttons[item] = button
            bbox = self.tree.bbox(item, "action")
            if not bbox:
                button.place_forget()
                continue
            x, y, width, height = bbox
            button_width = min(56, max(width - 8, 36))
            button_height = min(24, max(height - 6, 20))
            button.place(
                x=tree_x + x + max((width - button_width) // 2, 0),
                y=tree_y + y + max((height - button_height) // 2, 0),
                width=button_width,
                height=button_height,
            )

    def _launch_codex(self, row: CodexInstallRow) -> None:
        exe = Path(row.path)
        if not exe.exists():
            messagebox.showerror("启动失败", f"找不到文件 {row.path}")
            return
        ok, message = self.service.ensure_launch_permissions(exe)
        if not ok:
            messagebox.showerror("启动失败", message)
            return
        proxy_port = self.service.config.port
        env = os.environ.copy()
        env["HTTP_PROXY"] = f"http://127.0.0.1:{proxy_port}"
        env["HTTPS_PROXY"] = f"http://127.0.0.1:{proxy_port}"
        env["ALL_PROXY"] = f"http://127.0.0.1:{proxy_port}"
        env["NO_PROXY"] = "localhost,127.0.0.1"
        current_dir = str(exe.parent)
        print(f"[ProxyWindow] 启动外部程序: {exe.name}", flush=True)
        if self._is_cli_codex_executable(exe):
            subprocess.Popen(
                f'cmd.exe /k ""{exe}""',
                cwd=current_dir,
                env=env,
                creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
            )
            return
        subprocess.Popen(
            [str(exe)],
            cwd=current_dir,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def _is_cli_codex_executable(self, path: Path) -> bool:
        normalized_path = str(path).replace("/", "\\").lower()
        return path.name.lower() == "codex.exe" and normalized_path.endswith("\\codex\\codex.exe")

    def _iter_codex_executables(self, root: Path) -> list[Path]:
        matches: list[Path] = []
        for path in root.rglob("*"):
            if path.is_file() and path.name in {"codex.exe", "Codex.exe", "Code"}:
                matches.append(path)
        return matches

    def _iter_code_executables(self, root: Path) -> list[Path]:
        matches: list[Path] = []
        for path in root.rglob("*"):
            if path.is_file() and path.name in {"code.exe", "codex.exe", "Code", "Codex.exe"}:
                matches.append(path)
        return matches

    def _extract_version(self, folder_name: str) -> str:
        match = re.match(r"^OpenAI\.Codex_(\d+(?:\.\d+)*)_", folder_name)
        if match:
            return match.group(1)
        return ""

    def _format_size(self, size_bytes: int) -> str:
        if size_bytes < 1024:
            return f"{size_bytes} B"
        if size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f} KB"
        return f"{size_bytes / (1024 * 1024):.1f} MB"

    def _format_traffic_bytes(self, size_bytes: int, precision: int = 1) -> str:
        if size_bytes < 1024:
            return f"{size_bytes}B"
        if size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.{precision}f}K"
        if size_bytes < 1024 * 1024 * 1024:
            return f"{size_bytes / (1024 * 1024):.{precision}f}M"
        return f"{size_bytes / (1024 * 1024 * 1024):.{precision}f}G"

    def _tray_icon_watchdog(self) -> None:
        if self._closing:
            return
        self._recover_tray_icon_if_needed()
        self.root.after(_TRAY_WATCHDOG_INTERVAL_MS, self._tray_icon_watchdog)

    def _recover_tray_icon_if_needed(self) -> None:
        if self._closing or self.root.state() != "withdrawn":
            return
        if self._tray_icon is None or not getattr(self._tray_icon, "visible", False):
            if self._tray_icon is not None:
                try:
                    self._tray_icon.stop()
                except Exception:
                    pass
                self._tray_icon = None
            self._tray_icon_visible = False
            self._add_tray_icon()

    def _hide_to_tray(self) -> None:
        self._add_tray_icon()
        self.root.withdraw()

    def request_restore_window(self) -> None:
        self._post_ui(self.restore_window)

    def restore_window(self) -> None:
        self._restore_from_tray()

    def _restore_from_tray(self) -> None:
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.attributes("-topmost", True)
            self.root.after(200, lambda: self.root.attributes("-topmost", False))
        except tk.TclError:
            pass
        self.root.focus_force()

    def _add_tray_icon(self) -> None:
        if self._tray_icon_visible:
            return
        if self._tray_icon is not None:
            try:
                self._tray_icon.stop()
            except Exception:
                pass
            self._tray_icon = None
        menu = pystray.Menu(
            pystray.MenuItem("显示", self._on_tray_show, default=True),
            pystray.MenuItem("退出", self._on_tray_exit),
        )
        self._tray_icon = pystray.Icon(_TRAY_ICON_TIP, self._load_tray_icon(), self._build_tray_icon_tip(), menu)
        self._tray_icon.run_detached()
        self._tray_icon_visible = True

    def _remove_tray_icon(self) -> None:
        if not self._tray_icon_visible and self._tray_icon is None:
            return
        if self._tray_icon is not None:
            self._tray_icon.stop()
            self._tray_icon = None
        self._tray_icon_visible = False

    def _on_tray_show(self, _icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        self._post_ui(self._restore_from_tray)

    def _on_tray_exit(self, _icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        self._post_ui(self._exit_app)

    def _refresh_tray_icon_tooltip(self, rows: list[AuthFileRow] | None = None) -> None:
        if not self._tray_icon_visible or self._tray_icon is None:
            return
        self._tray_icon.title = self._build_tray_icon_tip(rows)

    def _build_tray_icon_tip(self, rows: list[AuthFileRow] | None = None) -> str:
        auth_rows = rows if rows is not None else self.auth_sync_service.list_auth_rows()
        if not auth_rows:
            return _TRAY_ICON_TIP
        current_refresh_token, load_refresh_token = self._get_auto_load_marks()
        lines = [_TRAY_ICON_TIP]
        for row in auth_rows[:_TRAY_ICON_MAX_ROWS]:
            marks = []
            if row.refresh_token == current_refresh_token:
                marks.append("当前")
            if row.refresh_token == load_refresh_token:
                marks.append("负载")
            mark_text = f"[{'/'.join(marks)}] " if marks else ""
            account_id = self._shorten_middle(row.account_id, 6, 4) if row.account_id else "-"
            quota = row.quota or "-"
            lines.append(f"{mark_text}{account_id} {quota}")
        if len(auth_rows) > _TRAY_ICON_MAX_ROWS:
            lines.append("...")
        tip = "\n".join(lines)
        if len(tip) <= _TRAY_ICON_MAX_TIP_LENGTH:
            return tip
        truncated_lines = [lines[0]]
        for line in lines[1:]:
            candidate = "\n".join(truncated_lines + [line])
            if len(candidate) > _TRAY_ICON_MAX_TIP_LENGTH - 4:
                truncated_lines.append("...")
                break
            truncated_lines.append(line)
        return "\n".join(truncated_lines)

    def _load_tray_icon(self) -> Image.Image:
        icon_path = self._get_icon_path()
        if icon_path.exists():
            return Image.open(icon_path)
        return Image.new("RGBA", (64, 64), "#2d8cff")

    def _get_icon_path(self) -> Path:
        if getattr(sys, "frozen", False):
            base_dir = Path(sys.executable).resolve().parent
        else:
            base_dir = Path(__file__).resolve().parents[2]
        return base_dir / "icon" / "tray_icon.ico"

    def _on_close(self) -> None:
        self._hide_to_tray()

    def _exit_app(self) -> None:
        if not messagebox.askyesno("退出确认", "确认退出程序吗？"):
            return
        self._shutdown_app()

    def _shutdown_for_update(self) -> None:
        self._shutdown_app()

    def _shutdown_app(self) -> None:
        self._closing = True
        self._remove_tray_icon()
        self._persist_config()
        self.auth_usage_service.stop()
        self.auth_sync_service.stop()
        self._low_price_seller_info_executor.shutdown(wait=False, cancel_futures=True)
        self.service.stop()
        self._auto_load_control_stop.set()
        if self._auto_load_control_socket is not None:
            try:
                self._auto_load_control_socket.close()
            except OSError:
                pass
        self.root.destroy()
