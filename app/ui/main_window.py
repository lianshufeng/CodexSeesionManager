import tkinter as tk
from tkinter import messagebox, ttk
from queue import Empty, Queue
from threading import Thread
from typing import Callable

from app.models import SessionViewData
from app.services.chatgpt_service import ChatGPTService
from app.services.chrome_service import ChromeService
from app.services.session_service import SessionService


class MainWindow:
    def __init__(self, root: tk.Tk, session_service: SessionService | None = None) -> None:
        self.root = root
        self.session_service = session_service or SessionService()
        self.chrome_service = ChromeService()
        self.chatgpt_service = ChatGPTService()
        self.open_monitor_job = None
        self.attach_monitor_job = None
        self.attach_active = False
        self.chrome_pid = None
        self._ui_queue: Queue[Callable[[], None]] = Queue()
        self._closing = False
        self.root.title("Codex Session Manager")
        self.root.minsize(800, 600)

        self._build_ui()
        self._center_window(800, 600)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(50, self._drain_ui_queue)

    def _build_ui(self) -> None:
        style = ttk.Style()
        style.configure("Treeview", borderwidth=1, relief="solid", rowheight=28)
        style.configure("Treeview.Heading", borderwidth=1, relief="solid")

        top_frame = ttk.Frame(self.root, padding=(12, 12, 12, 6))
        top_frame.pack(fill="x")

        title_row = ttk.Frame(top_frame)
        title_row.pack(fill="x")

        self.attach_button = ttk.Button(title_row, text="附加", command=self.attach_browser)
        self.attach_button.pack(side="right")
        self.attach_button.pack_forget()
        self.open_button = ttk.Button(title_row, text="打开", command=self.open_browser)
        self.open_button.pack(side="right", padx=(0, 8))

        info_frame = ttk.Frame(top_frame)
        info_frame.pack(fill="x", pady=(12, 0))

        self.user_id_var = tk.StringVar(value="-")
        self.user_name_var = tk.StringVar(value="-")
        self.user_email_var = tk.StringVar(value="-")
        self.expire_time_var = tk.StringVar(value="-")

        self._add_info_row(info_frame, 0, "用户ID", self.user_id_var)
        self._add_info_row(info_frame, 1, "用户名称", self.user_name_var)
        self._add_info_row(info_frame, 2, "用户邮箱", self.user_email_var)
        self._add_info_row(info_frame, 3, "过期时间", self.expire_time_var)

        table_frame = ttk.Frame(self.root, padding=(12, 6, 12, 12))
        table_frame.pack(fill="both", expand=True)

        columns = ("accountId", "planType", "structure", "accessToken", "sessionToken")
        tree_frame = ttk.Frame(table_frame, padding=1)
        tree_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings")
        headers = {
            "accountId": "账户ID",
            "planType": "账户类型",
            "structure": "结构",
            "accessToken": "Access Token",
            "sessionToken": "Session Token",
        }
        widths = {"accountId": 120, "planType": 100, "structure": 120, "accessToken": 200, "sessionToken": 200}
        for column in columns:
            self.tree.heading(column, text=headers[column])
            self.tree.column(column, width=widths[column], anchor="center", stretch=True)

        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def _add_info_row(self, parent: ttk.Frame, row: int, label: str, variable: tk.StringVar) -> None:
        ttk.Label(parent, text=f"{label}：").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=2)
        ttk.Label(parent, textvariable=variable).grid(row=row, column=1, sticky="w", pady=2)
        parent.columnconfigure(1, weight=1)

    def _center_window(self, width: int, height: int) -> None:
        self.root.update_idletasks()
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        x = (screen_width - width) // 2
        y = (screen_height - height) // 2
        self.root.geometry(f"{width}x{height}+{x}+{y}")

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
                    print(f"[MainWindow] UI 回调异常: {exc}", flush=True)
        except Empty:
            pass
        try:
            if not self._closing:
                self.root.after(50, self._drain_ui_queue)
        except tk.TclError:
            pass

    def _on_close(self) -> None:
        self._closing = True
        self.root.destroy()

    def open_browser(self) -> None:
        self.open_button.config(state=tk.DISABLED)
        Thread(target=self._open_worker, daemon=True).start()

    def attach_browser(self) -> None:
        self.attach_button.config(state=tk.DISABLED)
        Thread(target=self._attach_worker, daemon=True).start()

    def _open_worker(self) -> None:
        message = self.chatgpt_service.open_browser(self.chrome_service)
        self._post_ui(lambda: self._apply_open_result(message))

    def _attach_worker(self) -> None:
        view_data, message = self.chatgpt_service.attach_and_fetch(self.chrome_service)
        self._post_ui(lambda: self._apply_attach_result(view_data, message))

    def _apply_open_result(self, message: str) -> None:
        try:
            if message:
                messagebox.showwarning("打开结果", message)
            else:
                self.open_button.pack_forget()
                self.attach_button.pack(side="right")
                self.chrome_pid = self.chrome_service.get_running_chrome_pid()
                self._start_open_monitor()
        finally:
            if message:
                self.open_button.config(state=tk.NORMAL)

    def _start_open_monitor(self) -> None:
        self._cancel_open_monitor()
        self.open_monitor_job = self.root.after(2000, self._check_open_state)

    def _cancel_open_monitor(self) -> None:
        if getattr(self, "open_monitor_job", None) is not None:
            self.root.after_cancel(self.open_monitor_job)
            self.open_monitor_job = None

    def _check_open_state(self) -> None:
        self.open_monitor_job = None
        self.chrome_pid = self.chrome_service.get_running_chrome_pid()
        if self.chrome_service.is_process_running(self.chrome_pid) and self.chrome_service.is_browser_window_open(self.chrome_pid):
            self._start_open_monitor()
            return
        self.chrome_pid = None
        self.attach_active = False
        self.attach_button.pack_forget()
        self.open_button.config(text="打开", state=tk.NORMAL)
        self.open_button.pack(side="right", padx=(0, 8))

    def _apply_attach_result(self, data: SessionViewData, message: str) -> None:
        try:
            if message:
                messagebox.showerror("附加失败", message)
                return
            self._render_profile(data)
            self._render_accounts(data)
            self.attach_active = True
            self.attach_button.config(text="附加成功", state=tk.DISABLED)
            self._start_attach_monitor()
        finally:
            if message:
                self.attach_button.config(state=tk.NORMAL)
            self.root.title("Codex Session Manager")

    def _start_attach_monitor(self) -> None:
        if self.attach_monitor_job is not None:
            self.root.after_cancel(self.attach_monitor_job)
        self.attach_monitor_job = self.root.after(2000, self._check_attach_state)

    def _check_attach_state(self) -> None:
        self.attach_monitor_job = None
        if not self.attach_active:
            return
        self.chrome_pid = self.chrome_service.get_running_chrome_pid()
        if (
            self.chrome_service.is_process_running(self.chrome_pid)
            and self.chrome_service.is_browser_window_open(self.chrome_pid)
            and self.chrome_service.is_debug_session_alive()
        ):
            self._start_attach_monitor()
            return
        self.attach_active = False
        self.attach_button.pack_forget()

    def _render_profile(self, data: SessionViewData) -> None:
        self.user_id_var.set(data.profile.user_id)
        self.user_name_var.set(data.profile.user_name)
        self.user_email_var.set(data.profile.user_email)
        self.expire_time_var.set(data.profile.expire_time)

    def _render_accounts(self, data: SessionViewData) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        for account in data.accounts:
            self.tree.insert(
                "",
                "end",
                values=(
                    account.account_id,
                    account.plan_type,
                    account.structure,
                    account.access_token,
                    account.session_token,
                ),
            )
