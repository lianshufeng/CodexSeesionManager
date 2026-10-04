from __future__ import annotations

import time
import tkinter as tk
from datetime import datetime
from tkinter import ttk

from app.services.token_history_service import TokenHistoryService


def plot_segments(samples, since: int, until: int, width: int):
    """按像素分桶保留首尾和极值；停机断档不连接。"""
    segments, segment = [], []
    previous = None
    for sample in samples:
        if not since <= sample[0] <= until:
            continue
        if previous is not None and sample[0] - previous > 1:
            segments.append(segment)
            segment = []
        segment.append(sample)
        previous = sample[0]
    if segment:
        segments.append(segment)
    compressed = []
    for segment in segments:
        buckets = {}
        for sample in segment:
            pixel = int((sample[0] - since) * width / max(1, until - since))
            buckets.setdefault(pixel, []).append(sample)
        points = []
        for bucket in buckets.values():
            selected = {bucket[0], bucket[-1], min(bucket, key=lambda p: p[1]), max(bucket, key=lambda p: p[1])}
            points.extend(sorted(selected))
        compressed.append(points)
    return compressed


class TokenHistoryWindow:
    def __init__(self, root: tk.Tk, service: TokenHistoryService) -> None:
        self.service = service
        self.window = tk.Toplevel(root)
        self.window.title("输出速度历史")
        self.window.geometry("1000x520")
        self.window.minsize(560, 320)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self._closed = False
        self._after_id = None
        self._future = None
        self._samples = {}
        self._read_error = ""
        self._last_draw = 0.0
        self._ranges = {"最近 5 分钟": 300, "最近 30 分钟": 1800, "最近 1 小时": 3600,
                        "最近 1 天": 86400, "最近 3 天": 259200, "最近 7 天": 604800}
        controls = ttk.Frame(self.window, padding=12)
        controls.pack(fill="x")
        self._range = tk.StringVar(self.window, value="最近 5 分钟")
        selector = ttk.Combobox(controls, textvariable=self._range, values=list(self._ranges), state="readonly", width=16)
        selector.pack(side="left")
        selector.bind("<<ComboboxSelected>>", lambda _: self._load())
        self._reload_button = ttk.Button(controls, text="重新读取", command=self._load)
        self._reload_button.pack(side="left", padx=8)
        self._status = tk.StringVar(self.window, value="正在读取历史…")
        ttk.Label(controls, textvariable=self._status).pack(side="right")
        self.canvas = tk.Canvas(self.window, background="#ffffff", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=12)
        self.canvas.bind("<Configure>", lambda _: self._draw())
        self._summary = tk.StringVar(self.window, value="累计输出（估算）：正在读取…")
        ttk.Label(self.window, textvariable=self._summary, padding=(12, 8, 12, 0)).pack(anchor="w")
        self._load()
        self._tick()

    def show(self) -> None:
        self.window.deiconify()
        self.window.lift()

    def _load(self) -> None:
        now = int(time.time())
        self._samples = {}
        self._read_error = ""
        self._reload_button.configure(state="disabled")
        self._future = self.service.read_async(now - self._ranges[self._range.get()], now)
        self._status.set("正在读取历史…")
        self._summary.set("累计输出（估算）：正在读取…")
        self._last_draw = 0.0

    def _tick(self) -> None:
        if self._closed:
            return
        loading = self._future is not None
        if loading and self._future.done():
            try:
                self._samples.update(self._future.result())
            except Exception as exc:
                self._read_error = f"读取失败: {exc}"
            self._future = None
            self._reload_button.configure(state="normal")
            loading = False
        now = int(time.time())
        since = now - self._ranges[self._range.get()]
        self._samples.update(self.service.recent(since))
        self._samples = {stamp: speed for stamp, speed in self._samples.items() if since <= stamp <= now}
        if not loading:
            speeds = list(self._samples.values())
            if self.service.error or self._read_error:
                self._status.set(self.service.error or self._read_error)
                self._summary.set("累计输出（估算）：数据不完整，暂不汇总")
            elif speeds:
                latest = max(self._samples)
                current = self._samples[latest] if now - latest <= 2 else 0
                self._status.set(f"当前 {current:.1f} · 平均 {sum(speeds)/len(speeds):.1f} · 最高 {max(speeds):.1f} token/s")
                self._summary.set(f"{self._range.get()} · 累计输出（估算）：{sum(speeds):,.0f} tokens")
            else:
                self._status.set("暂无历史记录")
                self._summary.set(f"{self._range.get()} · 累计输出（估算）：0 tokens（暂无记录）")
        # 多日曲线减少重绘频率，汇总仍每秒更新。
        if time.monotonic() - self._last_draw >= (10 if self._ranges[self._range.get()] >= 86400 else 1):
            self._draw()
        self._after_id = self.window.after(100 if loading else 1000, self._tick)

    def _draw(self) -> None:
        if self._closed:
            return
        canvas = self.canvas
        canvas.delete("all")
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if width < 100 or height < 100:
            return
        self._last_draw = time.monotonic()
        left, top, right, bottom = 58, 25, width - 18, height - 35
        now = int(time.time())
        since = now - self._ranges[self._range.get()]
        maximum = max(1, max(self._samples.values(), default=0) * 1.1)
        for step in range(5):
            y = bottom - (bottom - top) * step / 4
            canvas.create_line(left, y, right, y, fill="#e5e7eb")
            canvas.create_text(left - 8, y, text=f"{maximum * step / 4:.1f}", anchor="e", fill="#555555")
        canvas.create_text(left, 10, text="token/s", anchor="w", fill="#555555")
        for step in range(5):
            stamp = since + (now - since) * step / 4
            x = left + (right - left) * step / 4
            label_format = "%m-%d %H:%M" if now - since >= 86400 else "%H:%M:%S"
            canvas.create_text(x, bottom + 18, text=datetime.fromtimestamp(stamp).strftime(label_format), fill="#555555")
        segments = plot_segments(sorted(self._samples.items()), since, now, int(right - left))
        for segment in segments:
            points = []
            for stamp, speed in segment:
                points.extend((left + (stamp - since) * (right - left) / max(1, now - since), bottom - speed * (bottom - top) / maximum))
            if len(points) >= 4:
                canvas.create_line(*points, fill="#2878d0", width=2)
            elif points:
                x, y = points
                canvas.create_oval(x-2, y-2, x+2, y+2, fill="#2878d0", outline="")
        if not self._samples:
            canvas.create_text(width / 2, height / 2, text="暂无历史记录", fill="#888888")

    def close(self) -> None:
        self._closed = True
        if self._after_id is not None:
            self.window.after_cancel(self._after_id)
        self.window.destroy()
