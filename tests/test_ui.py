"""UI behavior with hidden windows, temporary settings, and a fake backend.

These tests do not read the real clipboard or music cache, start a player, or
inspect screen pixels. They exercise normal Tk APIs and the worker event queue.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import tkinter as tk
import types
import unittest
from unittest.mock import Mock, patch

import customtkinter as ctk

import app


class FakeDownloadError(Exception):
    pass


class UiBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"APPDATA": self.temp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.assets = patch.object(app, "asset_path", return_value=self.directory / "missing-icon.ico")
        self.assets.start()
        self.addCleanup(self.assets.stop)
        self.backend = Mock()
        fake_client = types.ModuleType("soda_client")
        fake_client.DownloadError = FakeDownloadError
        fake_client.download_cached_track = self.backend
        self.module_patch = patch.dict(sys.modules, {"soda_client": fake_client})
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)
        ctk.set_appearance_mode("light")
        self.window = None
        self.ui = None
        self.release = threading.Event()
        self.addCleanup(self._dispose)
        self._construct()

    def _construct(self, settings=None):
        if self.window is not None:
            self._dispose()
        if settings is not None:
            settings_path = self.directory / "SodaLossless" / "settings.json"
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            settings_path.write_text(json.dumps(settings), encoding="utf-8")
        self.window = ctk.CTk()
        self.window.withdraw()
        self.ui = app.LosslessApp(self.window)
        self.ui.output.set(str(self.directory / "downloads"))
        self.backend.return_value = self._result()

    def _dispose(self):
        if self.ui is not None:
            self.ui.cancel_event.set()
        self.release.set()
        if self.ui is not None and self.ui.worker is not None:
            self.ui.worker.join(timeout=3)
            if self.ui.worker.is_alive():
                raise AssertionError("The fake download worker did not stop")
        if self.window is not None:
            try:
                for callback_id in self.window.tk.call("after", "info"):
                    self.window.after_cancel(callback_id)
                if self.ui is not None and not self.ui.closed:
                    self.ui.close()
                else:
                    self.window.destroy()
            except tk.TclError:
                pass
        self.window = None
        self.ui = None

    def _result(self, codec="aac"):
        extension = {"aac": ".m4a", "flac": ".flac", "mp3": ".mp3", "opus": ".opus"}[codec]
        return {
            "path": str(self.directory / "downloads" / ("合成测试" + extension)),
            "codec": codec,
            "codec_name": codec,
            "bit_rate": 192000,
            "duration": 65.7,
            "is_lossless": codec == "flac",
            "verified": True,
        }

    def _drain(self):
        # Queue processing is explicit; suppress only its next polling timer.
        with patch.object(self.window, "after", return_value="test-poll"):
            self.ui._poll_events()

    def _start_fake_busy(self):
        self.ui.busy = True
        self.ui._set_progress(None)
        self.ui._set_controls()

    def _join_worker(self):
        self.ui.worker.join(timeout=3)
        self.assertFalse(self.ui.worker.is_alive())

    def test_main_smoke_constructs_and_closes_hidden_window(self):
        self._dispose()
        real_window = ctk.CTk
        created = []

        def hidden_window():
            window = real_window()
            window.withdraw()
            created.append(window)
            return window

        try:
            with patch.object(app.ctk, "CTk", side_effect=hidden_window):
                with patch.object(sys, "argv", ["app.py", "--smoke-test"]):
                    app.main()
        finally:
            # Tcl's timer queue can outlive a destroyed root when a test process
            # constructs another interpreter. Production main normally exits.
            for window in created:
                try:
                    for callback_id in window.tk.call("after", "info"):
                        window.tk.call("after", "cancel", callback_id)
                except tk.TclError:
                    pass
        self.assertEqual(len(created), 1)
        self.backend.assert_not_called()

    def test_error_event_restores_idle_controls_and_shows_actionable_message(self):
        self._start_fake_busy()
        self.ui.events.put(("progress", "正在读取音源", 42))
        self.ui.events.put(("error", "请在汽水中完整播放这首歌。"))
        self._drain()
        self.assertFalse(self.ui.busy)
        self.assertFalse(self.ui.indeterminate)
        self.assertEqual(self.ui.progress.get(), 0)
        self.assertEqual(self.ui.download_button.cget("state"), "normal")
        self.assertEqual(self.ui.link_entry.cget("state"), "normal")
        self.assertEqual(self.ui.status_title.get(), "暂时无法下载")
        self.assertIn("完整播放", self.ui.status_detail.get())
        self.assertEqual(self.ui.progress_label.get(), "未完成")

    def test_cancellation_ignores_late_progress_and_restores_controls(self):
        self._start_fake_busy()
        self.ui.cancel_download()
        self.assertTrue(self.ui.cancel_event.is_set())
        self.assertEqual(self.ui.cancel_button.cget("state"), "disabled")
        self.ui.events.put(("progress", "不应该覆盖取消状态", 99))
        self._drain()
        self.assertEqual(self.ui.status_title.get(), "正在取消")
        self.ui.events.put(("cancelled",))
        self._drain()
        self.assertFalse(self.ui.busy)
        self.assertEqual(self.ui.status_title.get(), "已取消")
        self.assertEqual(self.ui.download_button.cget("state"), "normal")
        self.assertEqual(self.ui.progress.get(), 0)

    def test_success_displays_actual_filename_codec_and_lossless_status(self):
        for codec in ("flac", "aac", "mp3", "opus"):
            with self.subTest(codec=codec):
                self._start_fake_busy()
                result = self._result(codec)
                self.ui.events.put(("success", result))
                self._drain()
                details = self.ui.status_detail.get()
                self.assertEqual(self.ui.last_download, Path(result["path"]))
                self.assertIn(Path(result["path"]).name, details)
                self.assertIn(codec.upper(), details)
                self.assertIn("无损" if codec == "flac" else "有损", details)
                if codec != "flac":
                    self.assertNotIn("无损", details)
                self.assertIn("192 kbps", details)
                self.assertIn("1:05", details)
                self.assertEqual(self.ui.status_title.get(), "保存成功")
                self.assertEqual(self.ui.progress.get(), 1)
                self.assertFalse(self.ui.busy)

    def test_busy_prevents_duplicate_submit_and_quality_changes(self):
        entered = threading.Event()

        def blocked_backend(*args, **kwargs):
            entered.set()
            if not self.release.wait(timeout=3):
                raise FakeDownloadError("test backend timeout")
            return self._result()

        self.backend.side_effect = blocked_backend
        self.ui.link.set("1234567890123456789")
        self.ui._select_quality("high")
        self.ui.start_download()
        self.assertTrue(entered.wait(timeout=2))
        first_worker = self.ui.worker
        self.ui.start_download()
        self.ui._select_quality("lossless")
        self.assertIs(self.ui.worker, first_worker)
        self.assertEqual(self.backend.call_count, 1)
        self.assertEqual(self.ui.quality.get(), "high")
        for widget in (self.ui.link_entry, self.ui.output_entry, self.ui.paste_button,
                       self.ui.browse_button, self.ui.download_button, *self.ui.quality_buttons.values()):
            self.assertEqual(widget.cget("state"), "disabled")
        self.assertEqual(self.ui.cancel_button.cget("state"), "normal")
        self.release.set()
        self._join_worker()
        self._drain()
        self.assertFalse(self.ui.busy)
        self.assertEqual(self.backend.call_args.kwargs["quality"], "high")
        self.assertTrue(self.backend.call_args.kwargs["open_in_player"])

    def test_quality_keys_load_legacy_settings_and_persist_without_credentials(self):
        self._construct({"output_dir": str(self.directory / "old"), "quality": "high",
                         "aid": "386088", "sessionid": "test-only-do-not-retain"})
        self.assertEqual(self.ui.quality.get(), "high")
        self.assertEqual(self.ui.quality_hint.get(), app.QUALITY_HINTS["high"])
        self.ui._select_quality("lossless")
        self.ui.link.set("1234567890123456789")
        self.ui.start_download()
        self._join_worker()
        self._drain()
        saved = json.loads(self.ui.settings_path.read_text(encoding="utf-8"))
        self.assertEqual(saved, {"output_dir": str(self.directory / "downloads"), "quality": "lossless"})
        self.assertEqual(self.backend.call_args.kwargs["quality"], "lossless")

    def test_unknown_saved_quality_uses_default(self):
        self._construct({"quality": "removed-quality"})
        self.assertEqual(self.ui.quality.get(), "best")
        self.assertEqual(self.ui.quality_hint.get(), app.QUALITY_HINTS["best"])

    def test_empty_or_nontext_clipboard_keeps_existing_link(self):
        self.ui.link.set("keep-existing-link")
        for value in ("", " \n\t ", tk.TclError("CLIPBOARD has no STRING content")):
            with self.subTest(value=type(value).__name__):
                kwargs = {"side_effect": value} if isinstance(value, Exception) else {"return_value": value}
                with patch.object(self.window, "clipboard_get", **kwargs):
                    self.ui._paste_link()
                self.assertEqual(self.ui.link.get(), "keep-existing-link")
                self.assertIn("剪贴板", self.ui.status_detail.get())
                self.assertEqual(self.ui.status_title.get(), "暂时无法下载")
                self.backend.assert_not_called()

    def test_worker_uses_queue_and_hides_unexpected_exception_details(self):
        self.backend.side_effect = RuntimeError("private-token-must-not-reach-ui")
        self.ui.link.set("1234567890123456789")
        self.ui.start_download()
        self._join_worker()
        # The worker has queued its result but has not touched Tk status itself.
        self.assertTrue(self.ui.busy)
        self.assertEqual(self.ui.status_title.get(), "准备音源")
        self._drain()
        self.assertFalse(self.ui.busy)
        self.assertEqual(self.ui.status_title.get(), "暂时无法下载")
        self.assertNotIn("private-token", self.ui.status_detail.get())

    def test_close_signals_active_worker_without_accessing_destroyed_widgets(self):
        entered = threading.Event()
        worker_stopped = threading.Event()

        def waiting_backend(*args, **kwargs):
            entered.set()
            if kwargs["cancel"].wait(timeout=3):
                worker_stopped.set()
                raise FakeDownloadError("cancelled")
            raise FakeDownloadError("test backend timeout")

        self.backend.side_effect = waiting_backend
        self.ui.link.set("1234567890123456789")
        self.ui.start_download()
        self.assertTrue(entered.wait(timeout=2))
        self.ui.close()
        self.assertTrue(self.ui.closed)
        self.assertTrue(self.ui.cancel_event.is_set())
        self._join_worker()
        self.assertTrue(worker_stopped.is_set())
        self.assertEqual(self.ui.events.get_nowait(), ("cancelled",))
        # Scheduled queue polling exits before touching the destroyed window.
        self.ui._poll_events()


if __name__ == "__main__":
    unittest.main()
