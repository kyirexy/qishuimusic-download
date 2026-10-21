"""Windows interface for saving complete audio from the local Qishui cache."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog

import customtkinter as ctk

APP_TITLE = "汽水音乐下载"
APP_VERSION = "1.0.0"
FONT = "Microsoft YaHei UI"
BG, SURFACE, FIELD = "#F3F6F4", "#FFFFFF", "#F7F9F8"
INK, MUTED, BORDER = "#1E3029", "#596C63", "#D8E2DC"
ACCENT, HOVER, SOFT = "#176B50", "#10583F", "#E6F0EA"
RED = "#A9363D"
QUALITY_OPTIONS = {"best": "自动", "standard": "标准", "high": "高音质", "lossless": "无损"}
QUALITY_HINTS = {
    "best": "选择已缓存的最高音质，保留音源原始编码。",
    "standard": "选择缓存中较小的有损音源，节省存储空间。",
    "high": "选择缓存中码率最高的有损音源。",
    "lossless": "仅保存 FLAC。请先在汽水中选择账号可用的无损音质。",
}


def asset_path(name: str) -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / name


class LosslessApp:
    def __init__(self, window: ctk.CTk) -> None:
        self.window = window
        window.title(APP_TITLE)
        window.geometry("960x690")
        window.minsize(920, 670)
        window.configure(fg_color=BG)
        window.protocol("WM_DELETE_WINDOW", self.close)
        icon = asset_path("assets/app.ico")
        if icon.is_file():
            window.after(250, lambda: window.iconbitmap(str(icon)))
        self.events: queue.Queue[tuple] = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.closed = self.busy = self.indeterminate = False
        self.last_download: Path | None = None
        self.settings_path = Path(os.environ.get("APPDATA") or Path.home()) / "SodaLossless" / "settings.json"
        settings = self._read_settings()
        self.link = tk.StringVar()
        self.output = tk.StringVar(value=settings.get("output_dir", str(Path.home() / "Music" / "汽水音乐")))
        quality = settings.get("quality", "best")
        self.quality = tk.StringVar(value=quality if quality in QUALITY_OPTIONS else "best")
        self.quality_hint = tk.StringVar(value=QUALITY_HINTS[self.quality.get()])
        self.status_title = tk.StringVar(value="等待开始")
        self.status_detail = tk.StringVar(value="粘贴歌曲链接，选好保存位置。\n准备好后，点击「下载音乐」。")
        self.status_tag = tk.StringVar(value="准备就绪")
        self.progress_label = tk.StringVar(value="尚未开始")
        self._build()
        window.after(90, self._poll_events)
        window.after(200, self.link_entry.focus_set)

    def _font(self, size=13, bold=False):
        return ctk.CTkFont(family=FONT, size=size, weight="bold" if bold else "normal")

    def _label(self, parent, text="", size=13, bold=False, color=INK, **kwargs):
        kwargs.setdefault("anchor", "w")
        kwargs.setdefault("height", 20)
        return ctk.CTkLabel(parent, text=text, font=self._font(size, bold), text_color=color,
                            justify="left", **kwargs)

    def _button(self, parent, text, command, *, primary=False, **kwargs):
        button = ctk.CTkButton(
            parent, text=text, command=command, height=42, corner_radius=9,
            font=self._font(13, primary), fg_color=ACCENT if primary else SURFACE,
            hover_color=HOVER if primary else SOFT,
            text_color="#FFFFFF" if primary else INK, text_color_disabled="#87978E",
            border_color=BORDER, border_width=0 if primary else 1, **kwargs,
        )
        # CustomTkinter draws its buttons on a canvas. Include that canvas in
        # native Tab traversal and retain visible keyboard focus.
        button._canvas.configure(takefocus=1)
        button.bind("<Return>", lambda _event: button.invoke())
        button.bind("<space>", lambda _event: button.invoke())
        button.bind("<FocusIn>", lambda _event: button.configure(border_color=ACCENT, border_width=2))
        button.bind("<FocusOut>", lambda _event: button.configure(border_color=BORDER, border_width=0 if primary else 1))
        return button

    def _entry(self, parent, variable, placeholder=""):
        entry = ctk.CTkEntry(parent, textvariable=variable, placeholder_text=placeholder,
                            height=46, corner_radius=9, border_width=1, border_color=BORDER,
                            fg_color=FIELD, text_color=INK, placeholder_text_color=MUTED,
                            font=self._font(13))
        entry.bind("<FocusIn>", lambda _event: entry.configure(border_color=ACCENT, border_width=2))
        entry.bind("<FocusOut>", lambda _event: entry.configure(border_color=BORDER, border_width=1))
        return entry

    def _build(self):
        self.window.grid_columnconfigure(0, weight=1)
        self.window.grid_rowconfigure(1, weight=1)
        header = ctk.CTkFrame(self.window, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=30, pady=(28, 22))
        mark = ctk.CTkFrame(header, width=44, height=44, corner_radius=13, fg_color=ACCENT)
        mark.pack(side="left", padx=(0, 13))
        mark.pack_propagate(False)
        ctk.CTkLabel(mark, text="S", font=ctk.CTkFont("Segoe UI", 29, "bold"),
                     text_color="#FFFFFF").pack(expand=True)
        title = ctk.CTkFrame(header, fg_color="transparent")
        title.pack(side="left")
        self._label(title, APP_TITLE, size=22, bold=True).pack(anchor="w")
        self._label(title, "把喜欢的音乐，留在电脑里。", color=MUTED, size=12).pack(anchor="w", pady=(2, 0))
        self._label(header, "原编码保存", size=12, color=MUTED).pack(side="right")

        content = ctk.CTkFrame(self.window, fg_color="transparent")
        content.grid(row=1, column=0, sticky="nsew", padx=30)
        content.grid_columnconfigure(0, weight=1, minsize=540)
        content.grid_columnconfigure(1, weight=0, minsize=270)
        content.grid_rowconfigure(0, weight=1)
        form = ctk.CTkFrame(content, fg_color=SURFACE, corner_radius=16, border_width=1, border_color=BORDER)
        form.grid(row=0, column=0, sticky="nsew", padx=(0, 26))
        form.grid_columnconfigure(0, weight=1)
        form.grid_rowconfigure(9, weight=1)

        self._label(form, "歌曲链接", size=15, bold=True).grid(row=0, column=0, sticky="w", padx=26, pady=(25, 10))
        link_row = ctk.CTkFrame(form, fg_color="transparent")
        link_row.grid(row=1, column=0, sticky="ew", padx=26)
        link_row.grid_columnconfigure(0, weight=1)
        self.link_entry = self._entry(link_row, self.link, "歌曲分享链接或歌曲 ID")
        self.link_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.paste_button = self._button(link_row, "粘贴", self._paste_link, width=66)
        self.paste_button.grid(row=0, column=1)
        self._label(form, "整段分享文案也可以直接粘贴。", size=12, color=MUTED).grid(row=2, column=0, sticky="w", padx=26, pady=(5, 0))

        self._label(form, "下载音质", size=15, bold=True).grid(row=3, column=0, sticky="w", padx=26, pady=(25, 11))
        quality_row = ctk.CTkFrame(form, fg_color="transparent")
        quality_row.grid(row=4, column=0, sticky="ew", padx=26)
        self.quality_buttons = {}
        for index, (key, label) in enumerate(QUALITY_OPTIONS.items()):
            quality_row.grid_columnconfigure(index, weight=1, uniform="quality")
            button = self._button(quality_row, label, lambda value=key: self._select_quality(value), width=70)
            button.grid(row=0, column=index, sticky="ew", padx=(0, 7 if index < 3 else 0))
            self.quality_buttons[key] = button
        self._label(form, textvariable=self.quality_hint, size=12, color=MUTED,
                    wraplength=435, height=40).grid(row=5, column=0, sticky="ew", padx=26, pady=(5, 0))

        self._label(form, "保存位置", size=15, bold=True).grid(row=6, column=0, sticky="w", padx=26, pady=(18, 10))
        output_row = ctk.CTkFrame(form, fg_color="transparent")
        output_row.grid(row=7, column=0, sticky="ew", padx=26)
        output_row.grid_columnconfigure(0, weight=1)
        self.output_entry = self._entry(output_row, self.output)
        self.output_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.browse_button = self._button(output_row, "浏览", self._choose_output, width=66)
        self.browse_button.grid(row=0, column=1)
        self._label(form, "同名文件自动编号，原文件会保留。", size=12, color=MUTED).grid(row=8, column=0, sticky="w", padx=26, pady=(5, 10))

        action_row = ctk.CTkFrame(form, fg_color="transparent")
        action_row.grid(row=10, column=0, sticky="ew", padx=26, pady=(16, 25))
        action_row.grid_columnconfigure(0, weight=1)
        self.download_button = self._button(action_row, "下载音乐", self.start_download, primary=True)
        self.download_button.configure(height=48, font=self._font(15, True))
        self.download_button.grid(row=0, column=0, sticky="ew")
        self.cancel_button = self._button(action_row, "取消", self.cancel_download, width=72)
        self.cancel_button.grid(row=0, column=1, padx=(10, 0))
        self.cancel_button.grid_remove()

        status = ctk.CTkFrame(content, fg_color="transparent", width=270)
        status.grid(row=0, column=1, sticky="nsew", pady=(5, 0))
        status.grid_propagate(False)
        status.grid_columnconfigure(0, weight=1)
        status.grid_rowconfigure(8, weight=1)
        self.status_heading = self._label(status, textvariable=self.status_tag, size=13, color=ACCENT, bold=True)
        self.status_heading.grid(row=0, column=0, sticky="ew")
        self._label(status, textvariable=self.status_title, size=25, bold=True,
                    wraplength=260).grid(row=1, column=0, sticky="ew", pady=(13, 9))
        self.detail_label = self._label(status, textvariable=self.status_detail, size=13, color=MUTED,
                                        wraplength=260, height=100, anchor="nw")
        self.detail_label.grid(row=2, column=0, sticky="ew")
        self.progress = ctk.CTkProgressBar(status, height=6, corner_radius=3,
                                          fg_color=BORDER, progress_color=ACCENT)
        self.progress.grid(row=3, column=0, sticky="ew", pady=(12, 7))
        self.progress.set(0)
        self._label(status, textvariable=self.progress_label, size=12, color=MUTED).grid(row=4, column=0, sticky="ew")
        self.open_button = self._button(status, "打开保存文件夹", self._open_output)
        self.open_button.grid(row=5, column=0, sticky="ew", pady=(18, 0))
        ctk.CTkFrame(status, height=1, fg_color=BORDER).grid(row=9, column=0, sticky="ew", pady=(20, 14))
        self._label(status, "下载前", size=13, bold=True).grid(row=10, column=0, sticky="w")
        self._label(status, "保持汽水音乐登录。没有完整缓存时，工具会打开歌曲并等待加载。", size=12,
                    color=MUTED, wraplength=260).grid(row=11, column=0, sticky="ew", pady=(6, 0))
        self._label(status, "无损需先在汽水中切换至账号可用的无损音质。", size=12,
                    color=MUTED, wraplength=260).grid(row=12, column=0, sticky="ew", pady=(8, 6))

        footer = ctk.CTkFrame(self.window, fg_color="transparent")
        footer.grid(row=2, column=0, sticky="ew", padx=30, pady=(16, 18))
        self._label(footer, "保留原始音频 · 不重新压缩", color=MUTED, size=11).pack(side="left")
        self._label(footer, f"v{APP_VERSION}  ·  Windows", color=MUTED, size=11).pack(side="right")
        self._select_quality(self.quality.get())
        self.window.bind("<Control-Return>", lambda _event: self.start_download())
        self.window.bind("<Escape>", lambda _event: self.cancel_download())

    def _select_quality(self, value):
        if self.busy:
            return
        self.quality.set(value)
        self.quality_hint.set(QUALITY_HINTS[value])
        for key, button in self.quality_buttons.items():
            selected = key == value
            button.configure(fg_color=ACCENT if selected else FIELD,
                             hover_color=HOVER if selected else SOFT,
                             text_color="#FFFFFF" if selected else INK,
                             border_color=ACCENT if selected else BORDER)

    def _paste_link(self):
        try:
            value = self.window.clipboard_get().strip()
        except tk.TclError:
            self._show_error("剪贴板里没有文字。请先复制汽水的歌曲分享链接。")
            return
        if not value:
            self._show_error("剪贴板是空的。请先复制歌曲分享链接。")
            return
        if len(value) > 8192:
            self._show_error("这段文字太长了，请只粘贴歌曲分享文案或链接。")
            return
        self.link.set(value)
        self.link_entry.focus_set()

    def _read_settings(self):
        try:
            if self.settings_path.stat().st_size > 16384:
                return {}
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
            return {key: data[key] for key in ("output_dir", "quality")
                    if isinstance(data, dict) and isinstance(data.get(key), str) and data[key].strip()}
        except (OSError, ValueError, TypeError):
            return {}

    def _save_settings(self, output_dir, quality):
        try:
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.settings_path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"output_dir": output_dir, "quality": quality}, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, self.settings_path)
        except OSError:
            pass

    def _choose_output(self):
        current = Path(self.output.get()).expanduser()
        directory = filedialog.askdirectory(parent=self.window, title="选择音乐保存位置",
                                             initialdir=str(current if current.is_dir() else Path.home()), mustexist=False)
        if directory:
            self.output.set(directory)

    def _set_controls(self):
        state = "disabled" if self.busy else "normal"
        for widget in (self.link_entry, self.output_entry, self.browse_button, self.paste_button,
                       *self.quality_buttons.values(), self.download_button):
            widget.configure(state=state)
        self.download_button.configure(text="正在下载…" if self.busy else "下载音乐")
        if self.busy:
            self.cancel_button.grid()
        else:
            self.cancel_button.grid_remove()
        self.cancel_button.configure(state="normal" if self.busy and not self.cancel_event.is_set() else "disabled")

    def _show_error(self, message):
        self.status_tag.set("需要处理")
        self.status_title.set("暂时无法下载")
        self.status_detail.set(message)
        self.status_heading.configure(text_color=RED)
        self.progress.configure(progress_color=RED)
        self.progress_label.set("未完成")

    def start_download(self):
        if self.busy or self.closed:
            return
        text = self.link.get().strip()
        if not text:
            self._show_error("请先粘贴汽水音乐歌曲分享链接或歌曲 ID。")
            self.link_entry.focus_set()
            return
        if not self.output.get().strip():
            self._show_error("请选择音乐保存位置。")
            return
        try:
            output_dir = str(Path(self.output.get().strip()).expanduser().resolve())
        except (OSError, ValueError):
            self._show_error("保存路径无效，请重新选择文件夹。")
            return
        self.output.set(output_dir)
        quality = self.quality.get()
        self._save_settings(output_dir, quality)
        self.cancel_event = threading.Event()
        self.busy, self.last_download = True, None
        self.status_tag.set("正在下载")
        self.status_title.set("准备音源")
        self.status_heading.configure(text_color=ACCENT)
        self.progress.configure(progress_color=ACCENT)
        self.status_detail.set("正在解析歌曲并查找完整音源，需要时会打开汽水播放。")
        self.progress_label.set("处理中")
        self._set_progress(None)
        self._set_controls()
        self.worker = threading.Thread(target=self._run_download,
            args=(text, output_dir, self.cancel_event, quality), name="soda-download", daemon=False)
        self.worker.start()

    def _run_download(self, text, output_dir, cancel, quality):
        try:
            from soda_client import DownloadError, download_cached_track
        except Exception:
            self.events.put(("error", "程序组件加载失败，请重新运行完整的工具文件。"))
            return
        try:
            result = download_cached_track(text, output_dir, quality=quality, cancel=cancel,
                progress=lambda message, percent=None: self.events.put(("progress", message, percent)), open_in_player=True)
        except DownloadError as error:
            self.events.put(("cancelled",) if cancel.is_set() else ("error", str(error)))
        except Exception:
            self.events.put(("cancelled",) if cancel.is_set() else
                ("error", "下载未完成，请确认汽水已登录、歌曲可完整播放，并检查保存位置。"))
        else:
            self.events.put(("success", result))

    def _set_progress(self, percent):
        if percent is None:
            if not self.indeterminate:
                self.progress.configure(mode="indeterminate")
                self.progress.start()
                self.indeterminate = True
        else:
            if self.indeterminate:
                self.progress.stop()
                self.progress.configure(mode="determinate")
                self.indeterminate = False
            self.progress.set(max(0, min(100, float(percent))) / 100)

    def _finish(self):
        self.busy = False
        self.progress.stop()
        self.progress.configure(mode="determinate")
        self.indeterminate = False
        self._set_controls()

    def _poll_events(self):
        if self.closed:
            return
        for _ in range(100):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            kind = event[0]
            if kind == "progress":
                if self.cancel_event.is_set():
                    continue
                message, percent = event[1:]
                self.status_title.set("正在保存" if percent is not None and percent >= 55 else "准备音源")
                self.status_detail.set(message)
                self._set_progress(percent)
                self.progress_label.set(f"{percent:.0f}%" if percent is not None else "等待汽水加载")
            elif kind == "error":
                self._finish()
                self._set_progress(0)
                self._show_error(event[1])
            elif kind == "cancelled":
                self._finish()
                self._set_progress(0)
                self.status_tag.set("任务已停止")
                self.status_title.set("已取消")
                self.status_heading.configure(text_color=MUTED)
                self.status_detail.set("可以更换歌曲，或重新开始下载。")
                self.progress_label.set("等待下载")
            elif kind == "success":
                self._finish()
                result = event[1]
                self._set_progress(100)
                self.progress_label.set("100% · 完整性校验通过")
                self.status_tag.set("下载完成")
                self.status_heading.configure(text_color=ACCENT)
                self.last_download = Path(result["path"])
                self.status_title.set("保存成功")
                codec = str(result.get("codec") or "").lower()
                parts = [codec.upper(), "无损" if codec == "flac" else "有损"]
                bitrate = result.get("bit_rate") or result.get("bitrate")
                if bitrate:
                    parts.append(f"{float(bitrate) / 1000:.0f} kbps")
                if result.get("duration"):
                    seconds = int(float(result["duration"]))
                    parts.append(f"{seconds // 60}:{seconds % 60:02d}")
                self.status_detail.set(f"{self.last_download.name}\n\n{' · '.join(parts)}")
        self.window.after(90, self._poll_events)

    def cancel_download(self):
        if self.busy:
            self.cancel_event.set()
            self.cancel_button.configure(state="disabled")
            self.status_title.set("正在取消")
            self.status_detail.set("正在停止等待并清理临时文件…")

    def _open_output(self):
        try:
            directory = self.last_download.parent if self.last_download else Path(self.output.get()).expanduser().resolve()
            if not directory.is_dir():
                self._show_error("保存目录尚不存在。完成首次下载后，即可打开文件夹。")
                return
            os.startfile(str(directory))
        except (OSError, ValueError, AttributeError):
            self._show_error("暂时无法打开目录，请在文件资源管理器中查看保存位置。")

    def close(self):
        self.closed = True
        self.cancel_event.set()
        self.window.destroy()


def main():
    ctk.set_appearance_mode("light")
    ctk.set_default_color_theme("green")
    window = ctk.CTk()
    app = LosslessApp(window)
    if "--smoke-test" in sys.argv:
        window.after(350, app.close)
    window.mainloop()


if __name__ == "__main__":
    main()
