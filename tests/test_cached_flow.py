"""Official-cache pipeline tests using synthetic files, never a user's cache.

The cache discovery boundary is mocked even when soda_cache is installed. The
one audio integration test creates its own AAC tone and compares packet hashes.
"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
import importlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch

import soda_client as client


TRACK_ID = "1234567890123456789"


class FakeCacheError(Exception):
    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


@contextmanager
def fake_cache(finder):
    """Patch the real module when present, or supply its documented interface."""
    if "soda_cache" in sys.modules or importlib.util.find_spec("soda_cache") is not None:
        module = importlib.import_module("soda_cache")
        with patch.object(module, "CacheError", FakeCacheError), patch.object(
            module, "find_cached_track", finder
        ):
            yield
    else:
        module = types.ModuleType("soda_cache")
        module.CacheError = FakeCacheError
        module.find_cached_track = finder
        with patch.dict(sys.modules, {"soda_cache": module}):
            yield


def media_tool(name):
    located = shutil.which(name)
    if located and Path(located).stat().st_size > 1024:
        return located
    # This is an installed executable directory, never account or cache data.
    packages = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    for candidate in packages.glob(f"Gyan.FFmpeg_*/ffmpeg-*/bin/{name}.exe"):
        if candidate.stat().st_size > 1024:
            return str(candidate)
    return None


FFMPEG = media_tool("ffmpeg")
FFPROBE = media_tool("ffprobe")


def packet_hashes(path):
    result = subprocess.run(
        [
            FFPROBE, "-v", "error", "-select_streams", "a:0", "-show_packets",
            "-show_data_hash", "sha256", "-show_entries", "packet=data_hash",
            "-of", "json", str(path),
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return [packet["data_hash"] for packet in json.loads(result.stdout)["packets"]]


class CachedFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cache_path = self.root / "synthetic-cache.bin"
        self.cache_path.write_bytes(b"synthetic cached source")
        self.output = self.root / "output"
        self.cache_dir = self.root / "isolated-cache"
        self.info = {
            "path": str(self.cache_path),
            "cache_size": self.cache_path.stat().st_size,
            "codec": "aac",
            "extension": ".m4a",
            "title": "本地合成测试",
            "artist": "测试",
            "duration": 2.0,
            "spade": "",
            "bit_rate": 128_000,
            "quality": "best",
            "is_lossless": False,
        }
        self.guards = ExitStack()
        self.addCleanup(self.guards.close)
        self.network_guards = []
        for name in (
            "soda_client._request",
            "soda_client.requests.request",
            "soda_client.read_local_session",
            "soda_client.get_track",
            "soda_client._download_media",
        ):
            guard = self.guards.enter_context(
                patch(name, side_effect=AssertionError("Cache flow must not access account or network"))
            )
            self.network_guards.append(guard)
        self.tools = self.guards.enter_context(
            patch("soda_client.find_media_tool", side_effect=lambda name: f"unused-{name}")
        )
        self.opener = self.guards.enter_context(patch("soda_client.open_track_in_player"))
        self.exporter = self.guards.enter_context(
            patch("soda_audio.export_audio", side_effect=self.fake_export)
        )

    def tearDown(self):
        for guard in self.network_guards:
            guard.assert_not_called()

    @staticmethod
    def fake_export(source, checked, _key, _ffmpeg, _ffprobe, **kwargs):
        Path(checked).write_bytes(Path(source).read_bytes())
        return {
            "verified": True,
            "codec": kwargs["expected_codec"],
            "codec_name": kwargs["expected_codec"],
            "duration": kwargs["expected_duration"],
            "bit_rate": 128_000,
        }

    def run_flow(self, finder, **kwargs):
        with fake_cache(finder):
            return client.download_cached_track(
                TRACK_ID, self.output, cache_dir=self.cache_dir, **kwargs
            )

    def assert_output_empty(self):
        self.assertFalse(self.output.exists() and any(self.output.iterdir()))

    def test_ready_cache_uses_no_login_or_api_and_passes_verification_inputs(self):
        finder = Mock(return_value=self.info)
        cancel = threading.Event()
        result = self.run_flow(finder, quality="high", cancel=cancel)
        finder.assert_called_once_with(
            TRACK_ID, quality="high", cache_dir=self.cache_dir, cancel=cancel
        )
        self.opener.assert_not_called()
        self.assertEqual(result["source"], "official_cache")
        self.assertEqual(result["quality"], "high")
        self.assertFalse(result["is_lossless"])
        self.assertEqual(Path(result["path"]).suffix, ".m4a")
        self.assertEqual(Path(result["path"]).read_bytes(), self.cache_path.read_bytes())
        self.assertEqual(self.exporter.call_args.kwargs["expected_duration"], 2.0)
        self.assertEqual(self.exporter.call_args.kwargs["expected_codec"], "aac")
        self.assertEqual(list(self.output.iterdir()), [Path(result["path"])])

    def test_precancelled_does_not_discover_cache_or_launch_player(self):
        cancel = threading.Event()
        cancel.set()
        finder = Mock(return_value=self.info)
        with self.assertRaises(client.DownloadError):
            self.run_flow(finder, cancel=cancel)
        finder.assert_not_called()
        self.opener.assert_not_called()
        self.exporter.assert_not_called()
        self.assert_output_empty()

    def test_cache_size_changed_after_discovery_is_rejected(self):
        def changed_file(*_args, **_kwargs):
            with self.cache_path.open("ab") as file:
                file.write(b"changed")
            return self.info

        with self.assertRaises(client.DownloadError):
            self.run_flow(Mock(side_effect=changed_file))
        self.exporter.assert_not_called()
        self.assert_output_empty()

    def test_cache_growth_while_copying_is_rejected(self):
        self.cache_path.write_bytes(b"a" * (1024 * 1024 + 64))
        self.info["cache_size"] = self.cache_path.stat().st_size
        original_open = Path.open
        cache_path = self.cache_path

        class GrowingReader:
            def __init__(self, stream):
                self.stream = stream
                self.grew = False

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.stream.close()

            def fileno(self):
                return self.stream.fileno()

            def read(self, size=-1):
                data = self.stream.read(size)
                if data and not self.grew:
                    self.grew = True
                    with original_open(cache_path, "ab") as append:
                        append.write(b"unexpected growth")
                return data

        def open_snapshot(path, mode="r", *args, **kwargs):
            stream = original_open(path, mode, *args, **kwargs)
            return GrowingReader(stream) if path == cache_path and mode == "rb" else stream

        with patch.object(Path, "open", open_snapshot):
            with self.assertRaises(client.DownloadError):
                self.run_flow(Mock(return_value=self.info))
        self.exporter.assert_not_called()
        self.assert_output_empty()

    def test_cancel_after_validation_does_not_commit_output(self):
        cancel = threading.Event()

        def validate_then_cancel(*args, **kwargs):
            result = self.fake_export(*args, **kwargs)
            cancel.set()
            return result

        self.exporter.side_effect = validate_then_cancel
        with self.assertRaises(client.DownloadError):
            self.run_flow(Mock(return_value=self.info), cancel=cancel)
        self.assert_output_empty()

    def test_retryable_miss_opens_official_player_once_then_uses_arriving_cache(self):
        finder = Mock(
            side_effect=[
                FakeCacheError("尚未缓存", retryable=True),
                FakeCacheError("仍在加载", retryable=True),
                self.info,
            ]
        )
        cancel = threading.Event()
        with patch.object(cancel, "wait", return_value=False) as wait:
            result = self.run_flow(finder, cancel=cancel, wait_seconds=30)
        self.opener.assert_called_once_with(TRACK_ID)
        self.assertEqual(finder.call_count, 3)
        self.assertEqual(wait.call_count, 2)
        self.assertTrue(Path(result["path"]).is_file())
        for call in finder.call_args_list:
            self.assertEqual(call.kwargs["quality"], "best")

    def test_nonretryable_denial_does_not_open_or_export(self):
        finder = Mock(side_effect=FakeCacheError("当前账号只获准试听", retryable=False))
        with self.assertRaisesRegex(client.DownloadError, "试听"):
            self.run_flow(finder)
        finder.assert_called_once()
        self.opener.assert_not_called()
        self.exporter.assert_not_called()
        self.assert_output_empty()

    def test_disabled_player_launch_does_not_retry_a_miss(self):
        finder = Mock(side_effect=FakeCacheError("尚未缓存", retryable=True))
        with self.assertRaises(client.DownloadError):
            self.run_flow(finder, open_in_player=False)
        finder.assert_called_once()
        self.opener.assert_not_called()
        self.exporter.assert_not_called()

    def test_cancel_during_cache_wait_stops_before_another_discovery(self):
        finder = Mock(side_effect=FakeCacheError("尚未缓存", retryable=True))
        cancel = threading.Event()
        with patch.object(cancel, "wait", side_effect=lambda _seconds: cancel.set()):
            with self.assertRaises(client.DownloadError):
                self.run_flow(finder, cancel=cancel)
        finder.assert_called_once()
        self.opener.assert_called_once_with(TRACK_ID)
        self.exporter.assert_not_called()
        self.assert_output_empty()

    def test_explicit_lossless_rejects_a_mismatched_lossy_cache_result(self):
        finder = Mock(return_value=self.info)
        with self.assertRaises(client.DownloadError):
            self.run_flow(finder, quality="lossless")
        finder.assert_called_once()
        self.assertEqual(finder.call_args.kwargs["quality"], "lossless")
        self.opener.assert_not_called()
        self.exporter.assert_not_called()
        self.assert_output_empty()

    def test_explicit_lossless_never_retries_with_best_or_standard(self):
        finder = Mock(side_effect=FakeCacheError("没有账号可用的无损缓存", retryable=True))
        with self.assertRaises(client.DownloadError):
            self.run_flow(finder, quality="lossless", wait_seconds=0)
        finder.assert_called_once()
        self.assertEqual(finder.call_args.kwargs["quality"], "lossless")
        self.exporter.assert_not_called()
        self.assert_output_empty()


@unittest.skipUnless(FFMPEG and FFPROBE, "FFmpeg and FFprobe required for synthetic AAC")
class CachedAudioIntegrationTests(unittest.TestCase):
    def test_complete_synthetic_aac_preserves_packet_sha256_without_network(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original = root / "synthetic.m4a"
            subprocess.run(
                [
                    FFMPEG, "-v", "error", "-f", "lavfi", "-i",
                    "sine=frequency=587.33:duration=2:sample_rate=48000",
                    "-c:a", "aac", "-b:a", "128k", str(original),
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            info = {
                "path": str(original), "cache_size": original.stat().st_size,
                "codec": "aac", "title": "合成音", "artist": "测试",
                "duration": 2.0, "spade": "", "bit_rate": 128_000,
            }
            finder = Mock(return_value=info)
            guards = []
            with ExitStack() as stack:
                for name in (
                    "soda_client.requests.request", "soda_client._request",
                    "soda_client.read_local_session", "soda_client.get_track",
                    "soda_client._download_media", "soda_client.open_track_in_player",
                ):
                    guards.append(stack.enter_context(patch(name, side_effect=AssertionError(name))))
                stack.enter_context(
                    patch("soda_client.find_media_tool", side_effect=lambda name: FFMPEG if name == "ffmpeg" else FFPROBE)
                )
                stack.enter_context(fake_cache(finder))
                result = client.download_cached_track(
                    TRACK_ID, root / "output", quality="standard", cache_dir=root / "fake-cache"
                )
            for guard in guards:
                guard.assert_not_called()
            target = Path(result["path"])
            original_hashes = packet_hashes(original)
            self.assertGreater(len(original_hashes), 1)
            self.assertEqual(original_hashes, packet_hashes(target))
            self.assertTrue(result["verified"])
            self.assertEqual(result["codec_name"], "aac")
            self.assertEqual(result["quality"], "standard")
            self.assertEqual(result["source"], "official_cache")
            self.assertFalse(result["is_lossless"])
            self.assertEqual(target.suffix, ".m4a")
            self.assertEqual(list((root / "output").iterdir()), [target])


class OfficialPlaybackContractTests(unittest.TestCase):
    def test_open_track_uses_only_official_track_deeplink(self):
        with patch.object(client.sys, "platform", "win32"), patch.object(
            client.os, "startfile", create=True
        ) as start:
            client.open_track_in_player(TRACK_ID)
        start.assert_called_once_with(
            f"luna://luna.com/playing?track_id={TRACK_ID}&media_type=track"
        )

    def test_open_track_rejects_non_id_before_launch(self):
        with patch.object(client.os, "startfile", create=True) as start:
            for value in ("", TRACK_ID + "&quality=flac", "luna://example", "../track"):
                with self.subTest(value=value), self.assertRaises(client.DownloadError):
                    client.open_track_in_player(value)
        start.assert_not_called()

    def test_video_model_type_two_is_rejected_even_with_full_flac_metadata(self):
        for value in (2, "2"):
            for quality in ("best", "standard", "high", "lossless"):
                payload = {
                    "status_code": 0,
                    "track": {"name": "假完整时长", "duration": 180_000},
                    "track_player": {
                        "video_model_type": value,
                        "video_model": {"video_list": [
                            {"main_url": "https://invalid.example/full.flac",
                             "video_meta": {"codec_type": "flac", "bitrate": 900_000}},
                            {"main_url": "https://invalid.example/full.m4a",
                             "video_meta": {"codec_type": "aac", "bitrate": 128_000}},
                        ]},
                    },
                }
                with self.subTest(value=value, quality=quality), self.assertRaisesRegex(
                    client.DownloadError, "试听"
                ):
                    client.select_source(payload, quality=quality)


if __name__ == "__main__":
    unittest.main()
