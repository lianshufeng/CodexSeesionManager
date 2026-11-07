from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

from app.services.app_server_mqtt_service import (
    AppServerMqttService, MqttSettings, MqttSettingsStore, discover_app_servers,
)


class AppServerMqttWindow:
    def __init__(self, parent: tk.Misc, service: AppServerMqttService) -> None:
        self.service = service
        self.store = MqttSettingsStore()
        self.window = tk.Toplevel(parent)
        self.window.title('远程状态服务')
        self.window.transient(parent)
        self.window.geometry('780x540')
        self.window.minsize(720, 460)
        self.window.protocol('WM_DELETE_WINDOW', self.hide)
        self.window.bind('<Escape>', lambda _: self.hide())
        self._refresh_job = None
        self._last_events = ()
        self._was_busy = False
        frame = ttk.Frame(self.window, padding=12)
        frame.grid(sticky='nsew')
        self.window.rowconfigure(0, weight=1)
        self.window.columnconfigure(0, weight=1)
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(7, weight=1)
        try:
            settings = self.store.load()
        except Exception as exc:
            settings = MqttSettings()
            messagebox.showerror('读取 MQTT 配置失败', str(exc), parent=self.window)
        self.port = tk.StringVar(self.window, value=str(settings.port))
        self.username = tk.StringVar(self.window, value=settings.username)
        self.password = tk.StringVar(self.window, value=settings.password)
        self.host = tk.StringVar(self.window, value=settings.host)
        self.target = tk.StringVar(self.window)
        self.status = tk.StringVar(self.window)
        self.connection = tk.StringVar(self.window)
        self.password_visible = False
        self.entries = []
        for row, (label, variable) in enumerate((('本机端口', self.port), ('账号', self.username), ('密码', self.password))):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky='w', pady=3)
            entry = ttk.Entry(frame, textvariable=variable, width=32, show='*' if variable is self.password else '')
            entry.grid(row=row, column=1, columnspan=1 if variable is self.password else 2,
                       sticky='ew', padx=(10, 0))
            self.entries.append(entry)
            if variable is self.password:
                self.password_button = ttk.Button(frame, text='👁 显示', width=10, command=self.toggle_password)
                self.password_button.grid(row=row, column=2, sticky='e', padx=(6, 0))
        ttk.Label(frame, text='监听地址').grid(row=3, column=0, sticky='w', pady=3)
        host_entry = ttk.Entry(frame, textvariable=self.host)
        host_entry.grid(row=3, column=1, columnspan=2, sticky='ew', padx=(10, 0))
        self.entries.append(host_entry)
        ttk.Label(frame, text='目标进程').grid(row=4, column=0, sticky='w', pady=3)
        self.targets = ttk.Combobox(frame, textvariable=self.target, width=20, state='readonly')
        self.targets.grid(row=4, column=1, sticky='ew', padx=(10, 0))
        self.refresh_button = ttk.Button(frame, text='刷新进程', command=self.refresh)
        self.refresh_button.grid(row=4, column=2, sticky='e', padx=(5, 0))
        ttk.Label(frame, textvariable=self.connection).grid(
            row=5, columnspan=3, sticky='w', pady=(8, 0))
        ttk.Label(frame, textvariable=self.status, wraplength=690).grid(row=6, columnspan=3, sticky='w', pady=8)
        log_frame = ttk.LabelFrame(frame, text='Codex 状态 · 最近 200 条', padding=6)
        log_frame.grid(row=7, columnspan=3, sticky='nsew')
        self.events = ttk.Treeview(log_frame, columns=('time', 'thread', 'event', 'detail'), show='headings', height=10)
        self.events.tag_configure('completed', foreground='#187544')
        for column, label, width in (('time', '时间', 75), ('thread', '会话', 170), ('event', '状态', 100), ('detail', '详情', 330)):
            self.events.heading(column, text=label)
            self.events.column(column, width=width, minwidth=50, stretch=column == 'detail')
        scrollbar = ttk.Scrollbar(log_frame, orient='vertical', command=self.events.yview)
        self.events.configure(yscrollcommand=scrollbar.set)
        self.events.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')
        actions = ttk.Frame(frame)
        actions.grid(row=8, columnspan=3, sticky='ew', pady=(10, 0))
        ttk.Button(actions, text='隐藏窗口', command=self.hide).pack(side='left')
        self.save_button = ttk.Button(actions, text='保存配置', command=self.save)
        self.save_button.pack(side='right')
        self.start_button = ttk.Button(actions, text='启动并注入', command=self.start)
        self.start_button.pack(side='right', padx=6)
        self.stop_button = ttk.Button(actions, text='停止服务', command=self.service.stop)
        self.stop_button.pack(side='right')
        self.refresh()
        self.update_status()

    def toggle_password(self) -> None:
        self.password_visible = not self.password_visible
        self.entries[2].configure(show='' if self.password_visible else '*')
        self.password_button.configure(text='👁 隐藏' if self.password_visible else '👁 显示')

    def show(self) -> None:
        self.window.deiconify()
        self.window.lift()
        self.refresh()
        self.update_status()

    def hide(self) -> None:
        if self._refresh_job is not None:
            self.window.after_cancel(self._refresh_job)
            self._refresh_job = None
        self.window.withdraw()

    def refresh(self) -> None:
        self.processes = {str(target.pid): target for target in discover_app_servers()}
        self.targets.configure(values=list(self.processes))
        if self.service.busy and self.service.target is not None:
            self.target.set(str(self.service.target.pid))
        elif self.target.get() not in self.processes:
            self.target.set(next(iter(self.processes)) if len(self.processes) == 1 else '')

    def save(self) -> MqttSettings | None:
        try:
            settings = MqttSettings(int(self.port.get()), self.username.get().strip(), self.password.get(), self.host.get().strip())
            self.store.save(settings)
        except Exception as exc:
            messagebox.showerror('保存 MQTT 配置失败', str(exc), parent=self.window)
            return None
        return settings

    def start(self) -> None:
        target = self.processes.get(self.target.get())
        if target is None:
            messagebox.showwarning('请选择进程', '请先刷新并选择一个 App Server。', parent=self.window)
            return
        if (settings := self.save()) is not None:
            try:
                self.service.start(settings, target)
            except Exception as exc:
                messagebox.showerror('启动服务失败', str(exc), parent=self.window)
            self.update_status()

    def update_status(self) -> None:
        if self._refresh_job is not None:
            self.window.after_cancel(self._refresh_job)
            self._refresh_job = None
        busy = self.service.busy
        if self._was_busy and not busy:
            self.refresh()
        self._was_busy = busy
        for widget in (*self.entries, self.refresh_button, self.save_button, self.start_button):
            widget.configure(state='disabled' if busy else 'normal')
        self.targets.configure(state='disabled' if busy else 'readonly')
        self.stop_button.configure(state='normal' if busy else 'disabled')
        self.connection.set(f'{self.host.get().strip()}:{self.port.get().strip()} · MQTT 3.1.1 / 5.0')
        writer = '可写' if self.service.writer_ready else '等待确认写端'
        self.status.set(f'{self.service.status} · {writer} · 已发布 {self.service.count} 条'
                        + (f'\n{self.service.error}' if self.service.error else ''))
        rows = self.service.recent_events()
        if rows != self._last_events:
            scroll_position = self.events.yview()[0]
            for item in self.events.get_children():
                self.events.delete(item)
            for row in reversed(rows):
                self.events.insert('', 'end', values=row, tags=('completed',) if row[2] == '轮次完成' else ())
            self.events.yview_moveto(scroll_position)
            self._last_events = rows
        self._refresh_job = self.window.after(300, self.update_status)
