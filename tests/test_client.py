"""Account isolation, strict source selection and atomic-save regression tests.

All HTTP calls and the audio exporter are mocked. No account or music is used.
"""
from __future__ import annotations

import json
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

import soda_client as client


TRACK_ID = "1234567890123456789"
SESSION = "a" * 32
CDN = "https://media.example.test/song"


def payload():
    return {
        "status_code": 0,
        "track": {
            "name": "示例歌曲",
            "duration": 180_000,
            "artists": [{"name": "示例歌手"}],
            "album": {"name": "示例专辑"},
        },
        "track_player": {
            "video_model": {
                "video_list": [
                    {"main_url": CDN + ".aac", "video_meta": {"codec_type": "aac", "bitrate": 320_000}},
                    {"main_url": CDN + ".flac", "video_meta": {"codec_type": "flac", "bitrate": 900_000}},
                ]
            }
        },
    }


def lossy_payload(codec="aac", rates=(64_000, 320_000, 128_000)):
    data = payload()
    data["track_player"]["video_model"]["video_list"] = [
        {"main_url": f"{CDN}/{rate}", "video_meta": {"codec_type": codec, "bitrate": rate}}
        for rate in rates
    ]
    return data


class Response:
    def __init__(self, status=200, body=b"", headers=None, data=None, chunks=None):
        self.status_code = status
        self.headers = headers or {}
        self.data = data
        self.body = body
        self.chunks = chunks
        self.closed = False

    def close(self):
        self.closed = True

    def json(self):
        if isinstance(self.data, Exception):
            raise self.data
        return self.data

    def iter_content(self, _size):
        for chunk in self.chunks if self.chunks is not None else [self.body]:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk


class SourceSelectionTests(unittest.TestCase):
    def test_selects_actual_flac_and_passes_full_duration(self):
        selected = client.select_lossless(payload())
        self.assertEqual(selected["source"]["video_meta"]["codec_type"], "flac")
        self.assertEqual(selected["duration"], 180)
        self.assertEqual(selected["title"], "示例歌曲")
        self.assertEqual(selected["artist"], "示例歌手")

    def test_supports_json_encoded_model_and_dictionary_sources(self):
        data = payload()
        model = data["track_player"]["video_model"]
        model["video_list"] = {str(i): item for i, item in enumerate(model["video_list"])}
        data["track_player"]["video_model"] = json.dumps(model)
        self.assertEqual(client.select_lossless(data)["duration"], 180)

    def test_rejects_aac_even_when_url_has_flac_suffix(self):
        data = payload()
        source = data["track_player"]["video_model"]["video_list"][0]
        source["main_url"] = CDN + ".flac"
        data["track_player"]["video_model"]["video_list"] = [source]
        with self.assertRaises(client.DownloadError):
            client.select_lossless(data)

    def test_rejects_explicit_preview_at_each_supported_level(self):
        for location in ("payload", "track", "track_player", "model", "source"):
            for field in ("is_preview", "is_trial", "is_trial_play", "preview_only"):
                with self.subTest(location=location, field=field):
                    data = payload()
                    levels = {
                        "payload": data,
                        "track": data["track"],
                        "track_player": data["track_player"],
                        "model": data["track_player"]["video_model"],
                        "source": data["track_player"]["video_model"]["video_list"][1],
                    }
                    levels[location][field] = True
                    with self.assertRaises(client.DownloadError):
                        client.select_lossless(data)

    def test_requires_finite_positive_full_duration(self):
        for duration in (None, 0, -1, "", "invalid", float("nan"), float("inf"), "Infinity"):
            with self.subTest(duration=duration):
                data = payload()
                data["track"]["duration"] = duration
                with self.assertRaises(client.DownloadError):
                    client.select_lossless(data)

    def test_rejects_service_denial_even_with_embedded_source(self):
        data = payload()
        data["status_code"] = 401
        with self.assertRaises(client.DownloadError):
            client.select_lossless(data)

    def test_rejects_unparseable_model(self):
        data = payload()
        data["track_player"]["video_model"] = "{broken"
        with self.assertRaises(client.DownloadError):
            client.select_lossless(data)

    def test_aac_only_account_can_select_standard_high_or_best(self):
        for quality, bitrate in (("standard", 64_000), ("high", 320_000), ("best", 320_000)):
            with self.subTest(quality=quality):
                selected = client.select_source(lossy_payload(), quality=quality)
                self.assertEqual(selected["codec"], "aac")
                self.assertEqual(selected["bit_rate"], bitrate)
                self.assertEqual(selected["extension"], ".m4a")
                self.assertEqual(selected["quality"], quality)
                self.assertFalse(selected["is_lossless"])
                self.assertEqual(selected["duration"], 180)

    def test_best_prefers_flac_even_if_lossy_bitrate_is_higher(self):
        data = payload()
        data["track_player"]["video_model"]["video_list"][1]["video_meta"]["bitrate"] = 100_000
        selected = client.select_source(data)
        self.assertEqual(selected["codec"], "flac")
        self.assertTrue(selected["is_lossless"])
        for quality in ("standard", "high"):
            with self.subTest(quality=quality):
                self.assertEqual(client.select_source(data, quality=quality)["codec"], "aac")

    def test_explicit_lossless_does_not_fall_back_to_aac(self):
        with self.assertRaisesRegex(client.DownloadError, "FLAC"):
            client.select_source(lossy_payload(), quality="lossless")

    def test_standard_and_high_require_a_lossy_source(self):
        data = payload()
        data["track_player"]["video_model"]["video_list"] = [
            data["track_player"]["video_model"]["video_list"][1]
        ]
        for quality in ("standard", "high"):
            with self.subTest(quality=quality):
                with self.assertRaisesRegex(client.DownloadError, "普通音质"):
                    client.select_source(data, quality=quality)

    def test_preview_sources_are_filtered_without_rejecting_usable_sources(self):
        for location in ("source", "meta"):
            with self.subTest(location=location):
                data = lossy_payload()
                sources = data["track_player"]["video_model"]["video_list"]
                preview = payload()["track_player"]["video_model"]["video_list"][1]
                flagged = preview if location == "source" else preview["video_meta"]
                flagged["is_preview"] = True
                sources.append(preview)
                sources[1]["is_trial"] = True
                selected = client.select_source(data, quality="best")
                self.assertEqual(selected["codec"], "aac")
                self.assertEqual(selected["bit_rate"], 128_000)
                self.assertEqual(client.select_source(data, quality="standard")["bit_rate"], 64_000)

    def test_only_preview_sources_are_rejected_at_every_quality(self):
        data = lossy_payload()
        for source in data["track_player"]["video_model"]["video_list"]:
            source["is_preview"] = True
        for quality in client.QUALITIES:
            with self.subTest(quality=quality):
                with self.assertRaisesRegex(client.DownloadError, "试听"):
                    client.select_source(data, quality=quality)

    def test_unsupported_codec_is_not_renamed_to_supported_audio(self):
        with self.assertRaises(client.DownloadError):
            client.select_source(lossy_payload(codec="vorbis"))

    def test_invalid_quality_is_rejected(self):
        with self.assertRaises(client.DownloadError):
            client.select_source(payload(), quality="ultra")


class LinkAndCredentialTests(unittest.TestCase):
    def test_direct_id_and_share_id_need_no_network(self):
        with patch.object(client.requests, "request") as request:
            for text in (TRACK_ID, f"https://qishui.douyin.com/track?track_id={TRACK_ID}"):
                self.assertEqual(client.parse_share(text)["track_id"], TRACK_ID)
            request.assert_not_called()

    def test_share_hosts_are_exact_and_https_only(self):
        for url in (
            f"https://qishui.com.evil.example/?track_id={TRACK_ID}",
            f"https://qishui.com@evil.example/?track_id={TRACK_ID}",
            f"http://qishui.com/?track_id={TRACK_ID}",
            f"https://qishui.com:444/?track_id={TRACK_ID}",
            f"https://music.douyin.com.evil.example/?track_id={TRACK_ID}",
            f"https://music.douyin.com@evil.example/?track_id={TRACK_ID}",
        ):
            with self.subTest(url=url), patch.object(client.requests, "request") as request:
                with self.assertRaises(client.DownloadError):
                    client.parse_share(url)
                request.assert_not_called()

    def test_share_redirect_to_music_douyin_parses_track_without_credentials(self):
        landing = "https://music.douyin.com/qishui/share/track"
        data = {
            "loaderData": {"track_page": {
                "track_id": TRACK_ID,
                "audioWithLyricsOption": {"trackName": "跳转测试", "artistName": "测试歌手"},
            }}
        }
        first = Response(302, headers={"Location": landing})
        second = Response(body=("<script>window._ROUTER_DATA=" + json.dumps(data) + ";</script>").encode())
        with patch.object(client.requests, "request", side_effect=[first, second]) as request:
            selected = client.parse_share("分享歌曲 https://qishui.douyin.com/s/example")
        self.assertEqual(selected, {"track_id": TRACK_ID, "title": "跳转测试", "artist": "测试歌手"})
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args_list[1].args, ("GET", landing))
        for call in request.call_args_list:
            self.assertNotIn("Cookie", call.kwargs.get("headers", {}))
            self.assertFalse(call.kwargs["allow_redirects"])
        self.assertTrue(first.closed and second.closed)

    def test_music_douyin_direct_track_id_needs_no_network(self):
        with patch.object(client.requests, "request") as request:
            selected = client.parse_share(f"https://music.douyin.com/qishui/share?track_id={TRACK_ID}")
        self.assertEqual(selected["track_id"], TRACK_ID)
        request.assert_not_called()

    def test_share_redirect_to_untrusted_host_is_not_requested(self):
        response = Response(302, headers={"Location": "https://evil.example/collect"})
        with patch.object(client.requests, "request", return_value=response) as request:
            with self.assertRaises(client.DownloadError):
                client.parse_share("https://qishui.douyin.com/s/abc")
        self.assertEqual(request.call_count, 1)
        self.assertFalse(request.call_args.kwargs["allow_redirects"])
        self.assertNotIn("Cookie", request.call_args.kwargs["headers"])
        self.assertTrue(response.closed)

    def test_session_is_sent_only_to_fixed_api_not_its_redirect(self):
        response = Response(302, headers={"Location": "https://evil.example/collect"})
        with patch.object(client.requests, "request", return_value=response) as request:
            with self.assertRaises(client.DownloadError):
                client.get_track({"track_id": TRACK_ID}, SESSION)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args, ("POST", client.API))
        self.assertEqual(request.call_args.kwargs["headers"]["Cookie"], "sessionid=" + SESSION)
        self.assertFalse(request.call_args.kwargs["allow_redirects"])
        self.assertTrue(response.closed)

    def test_invalid_cookie_is_rejected_before_request(self):
        for value in ("short", SESSION + "\r\nX-Injected: yes", "sessionid=" + SESSION):
            with self.subTest(value=value), patch.object(client.requests, "request") as request:
                with self.assertRaises(client.DownloadError):
                    client.get_track({"track_id": TRACK_ID}, value)
                request.assert_not_called()

    def test_api_errors_do_not_expose_response_or_token(self):
        for response in (
            Response(403, body=("secret=" + SESSION).encode()),
            Response(500, body=("secret=" + SESSION).encode()),
            Response(data=ValueError("secret=" + SESSION)),
        ):
            with self.subTest(status=response.status_code):
                with patch.object(client.requests, "request", return_value=response):
                    with self.assertRaises(client.DownloadError) as error:
                        client.get_track({"track_id": TRACK_ID}, SESSION)
                self.assertNotIn(SESSION, str(error.exception))
                self.assertTrue(response.closed)

    def test_network_exception_is_sanitized(self):
        with patch.object(client.requests, "request", side_effect=requests.ConnectionError("token=" + SESSION)):
            with self.assertRaises(client.DownloadError) as error:
                client.get_track({"track_id": TRACK_ID}, SESSION)
        self.assertNotIn(SESSION, str(error.exception))

    def test_get_track_passes_quality_without_changing_account_permission(self):
        for quality, bitrate in (("standard", 64_000), ("high", 320_000)):
            response = Response(data=lossy_payload())
            with self.subTest(quality=quality):
                with patch.object(client.requests, "request", return_value=response) as request:
                    selected = client.get_track({"track_id": TRACK_ID}, SESSION, quality=quality)
                self.assertEqual(selected["bit_rate"], bitrate)
                self.assertEqual(request.call_args.kwargs["headers"]["Cookie"], "sessionid=" + SESSION)
                self.assertEqual(request.call_args.kwargs["json"]["track_id"], TRACK_ID)
                self.assertTrue(response.closed)


class MediaTransferTests(unittest.TestCase):
    def test_redirected_cdn_requests_never_receive_cookie(self):
        first = Response(302, headers={"Location": "https://cdn2.example.test/song"})
        second = Response(body=b"flac", headers={"Content-Length": "4"})
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "source.bin"
            with patch.object(client.requests, "request", side_effect=[first, second]) as request:
                client._download_media(CDN, target, lambda *args: None, None)
            self.assertEqual(target.read_bytes(), b"flac")
        self.assertEqual(request.call_count, 2)
        for call in request.call_args_list:
            self.assertNotIn("headers", call.kwargs)
            self.assertNotIn("cookies", call.kwargs)
            self.assertNotIn("auth", call.kwargs)
            self.assertFalse(call.kwargs["allow_redirects"])
        self.assertTrue(first.closed and second.closed)

    def test_insecure_cdn_redirect_is_not_followed(self):
        response = Response(302, headers={"Location": "http://cdn.example.test/song"})
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(client.requests, "request", return_value=response) as request:
                with self.assertRaises(client.DownloadError):
                    client._download_media(CDN, Path(directory) / "source", lambda *args: None, None)
            self.assertEqual(request.call_count, 1)

    def test_incomplete_and_partial_responses_are_rejected(self):
        for response in (
            Response(body=b"abc", headers={"Content-Length": "4"}),
            Response(body=b""),
            Response(206, body=b"abc"),
            Response(body=b"abc", headers={"Content-Length": str(client.MAX_BYTES + 1)}),
            Response(chunks=[b"abc", requests.ConnectionError("secret=" + SESSION)]),
        ):
            with self.subTest(status=response.status_code), tempfile.TemporaryDirectory() as directory:
                with patch.object(client.requests, "request", return_value=response):
                    with self.assertRaises(client.DownloadError) as error:
                        client._download_media(CDN, Path(directory) / "source", lambda *args: None, None)
                self.assertNotIn(SESSION, str(error.exception))
                self.assertTrue(response.closed)

    def test_download_does_not_overwrite_raw_destination(self):
        response = Response(body=b"new")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "source"
            target.write_bytes(b"original")
            with patch.object(client.requests, "request", return_value=response):
                with self.assertRaises(FileExistsError):
                    client._download_media(CDN, target, lambda *args: None, None)
            self.assertEqual(target.read_bytes(), b"original")
        self.assertTrue(response.closed)

    def test_cancel_before_request_avoids_network(self):
        cancel = threading.Event()
        cancel.set()
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(client.requests, "request") as request:
                with self.assertRaisesRegex(client.DownloadError, "取消"):
                    client._download_media(CDN, Path(directory) / "source", lambda *args: None, cancel)
                request.assert_not_called()

    def test_cancel_during_transfer_stops_before_next_chunk(self):
        response = Response(chunks=[b"first", b"second"])
        cancel = threading.Event()

        def progress(*_args):
            cancel.set()

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "source"
            with patch.object(client.requests, "request", return_value=response):
                with self.assertRaisesRegex(client.DownloadError, "取消"):
                    client._download_media(CDN, target, progress, cancel)
            self.assertEqual(target.read_bytes(), b"first")
        self.assertTrue(response.closed)


class SavePipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)
        self.calls = []

        def fake_download(url, target, progress, cancel):
            target.write_bytes(b"source")

        def fake_export(source, checked, key, ffmpeg, ffprobe, **kwargs):
            self.calls.append((source, checked, key, kwargs))
            checked.write_bytes(b"verified flac")
            return {"codec": kwargs["expected_codec"], "duration": 180}

        self.exporter = types.ModuleType("soda_audio")
        self.exporter.export_audio = fake_export
        for patcher in (
            patch.object(client, "find_media_tool", side_effect=lambda name: name),
            patch.object(client, "get_track", return_value=client.select_lossless(payload())),
            patch.object(client, "_download_media", side_effect=fake_download),
            patch.dict("sys.modules", {"soda_audio": self.exporter}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_download(self, **kwargs):
        return client.download_track(TRACK_ID, self.output, sessionid=SESSION, **kwargs)

    def test_full_duration_is_required_before_export_and_final_save(self):
        result = self.run_download()
        self.assertEqual(self.calls[0][3]["expected_duration"], 180)
        self.assertEqual(self.calls[0][3]["expected_codec"], "flac")
        self.assertEqual(Path(result["path"]).read_bytes(), b"verified flac")
        self.assertEqual([p.name for p in self.output.iterdir()], ["示例歌曲 - 示例歌手.flac"])
        for item in self.output.iterdir():
            self.assertNotIn(SESSION.encode(), item.read_bytes())

    def test_existing_song_is_preserved_and_new_name_is_used(self):
        first = self.output / "示例歌曲 - 示例歌手.flac"
        first.write_bytes(b"original")
        second = self.output / "示例歌曲 - 示例歌手 (1).flac"
        second.write_bytes(b"also original")
        result = self.run_download()
        self.assertEqual(Path(result["path"]).name, "示例歌曲 - 示例歌手 (2).flac")
        self.assertEqual(first.read_bytes(), b"original")
        self.assertEqual(second.read_bytes(), b"also original")

    def test_validation_failure_leaves_no_final_or_temporary_file(self):
        self.exporter.export_audio = lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("secret=" + SESSION))
        with self.assertRaises(client.DownloadError) as error:
            self.run_download()
        self.assertNotIn(SESSION, str(error.exception))
        self.assertEqual(list(self.output.iterdir()), [])

    def test_cancel_after_export_leaves_no_final_or_temporary_file(self):
        cancel = threading.Event()
        original_exporter = self.exporter.export_audio

        def cancel_at_end(*args, **kwargs):
            result = original_exporter(*args, **kwargs)
            cancel.set()
            return result

        self.exporter.export_audio = cancel_at_end
        with self.assertRaisesRegex(client.DownloadError, "取消"):
            self.run_download(cancel=cancel)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_song_title_cannot_escape_output_directory(self):
        info = client.select_lossless(payload())
        info["title"] = r"..\..\outside/file"
        info["artist"] = ""
        with patch.object(client, "get_track", return_value=info):
            result = self.run_download()
        self.assertEqual(Path(result["path"]).parent, self.output.resolve())
        self.assertTrue(Path(result["path"]).is_file())

    def test_lossy_outputs_use_correct_extension_and_preserve_existing_files(self):
        for codec, extension in (("aac", ".m4a"), ("mp3", ".mp3"), ("opus", ".opus")):
            with self.subTest(codec=codec):
                info = client.select_source(lossy_payload(codec), quality="high")
                original = self.output / ("示例歌曲 - 示例歌手" + extension)
                original.write_bytes(b"original")
                with patch.object(client, "get_track", return_value=info) as get_track:
                    result = self.run_download(quality="high")
                self.assertEqual(Path(result["path"]).name, "示例歌曲 - 示例歌手 (1)" + extension)
                self.assertEqual(original.read_bytes(), b"original")
                self.assertEqual(self.calls[-1][1].suffix, extension)
                self.assertEqual(self.calls[-1][3]["expected_codec"], codec)
                self.assertEqual(get_track.call_args.kwargs["quality"], "high")
                self.assertEqual(result["quality"], "high")
                self.assertFalse(result["is_lossless"])
                self.assertFalse(any(p.name.startswith(".soda-") for p in self.output.iterdir()))


if __name__ == "__main__":
    unittest.main()
