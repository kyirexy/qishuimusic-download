"""Real synthetic FLAC/AAC processing with a mocked service and no account."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests
import soda_client


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class IntegrationTests(unittest.TestCase):
    def make_aac(self, root):
        original = root / "synthetic.m4a"
        subprocess.run([
            shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi",
            "-i", "sine=frequency=659.25:duration=2:sample_rate=48000",
            "-c:a", "aac", "-b:a", "128k", str(original),
        ], check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return original

    def fake_service(self, original, reported_codec):
        audio = original.read_bytes()
        data = {
            "status_code": 0,
            "track": {"name": "合成测试音", "duration": 2000, "artists": [{"name": "测试"}]},
            "track_player": {"video_model": {"video_list": [{
                "main_url": "https://audio.example.test/synthetic",
                "video_meta": {"codec_type": reported_codec, "bitrate": 128000},
            }]}},
        }
        sent = []

        def request(method, url, **kwargs):
            sent.append((url, kwargs))
            self.assertFalse(kwargs["allow_redirects"])
            response = requests.Response()
            response.status_code = 200
            response._content_consumed = True
            if url == soda_client.API:
                self.assertEqual(method, "POST")
                self.assertEqual(kwargs["headers"]["Cookie"], "sessionid=" + "f" * 32)
                response._content = json.dumps(data).encode()
            else:
                self.assertEqual(url, "https://audio.example.test/synthetic")
                self.assertEqual(method, "GET")
                self.assertNotIn("Cookie", kwargs.get("headers", {}))
                response._content = audio
                response.headers["Content-Length"] = str(len(audio))
            return response

        return request, sent

    def packet_hashes(self, path):
        completed = subprocess.run([
            shutil.which("ffprobe"), "-v", "error", "-select_streams", "a:0",
            "-show_packets", "-show_data_hash", "sha256", "-show_entries",
            "packet=data_hash", "-of", "json", str(path),
        ], check=True, stdout=subprocess.PIPE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return [packet["data_hash"] for packet in json.loads(completed.stdout)["packets"]]

    def test_api_to_verified_flac_without_transcoding(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original = root / "synthetic.flac"
            subprocess.run([
                shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi",
                "-i", "sine=frequency=523.25:duration=2:sample_rate=48000",
                "-c:a", "flac", str(original),
            ], check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            audio = original.read_bytes()
            payload = {
                "status_code": 0,
                "track": {"name": "合成测试音", "duration": 2000, "artists": [{"name": "测试"}]},
                "track_player": {"video_model": json.dumps({"video_list": [{
                    "main_url": "https://audio.example.test/synthetic",
                    "video_meta": {"codec_type": "flac", "bitrate": 100000},
                }]})},
            }
            sent = []

            def request(method, url, **kwargs):
                sent.append((url, kwargs))
                response = requests.Response()
                response.status_code = 200
                response._content_consumed = True
                if url == soda_client.API:
                    response._content = json.dumps(payload).encode()
                else:
                    self.assertNotIn("Cookie", kwargs.get("headers", {}))
                    response._content = audio
                    response.headers["Content-Length"] = str(len(audio))
                return response

            with patch("soda_client.requests.request", side_effect=request):
                result = soda_client.download_track(
                    "1234567890123456789", root / "output", sessionid="f" * 32,
                )
            self.assertTrue(result["verified"])
            self.assertEqual(result["codec_name"], "flac")
            self.assertEqual(result["sample_rate"], 48000)
            self.assertAlmostEqual(result["duration"], 2, places=2)
            self.assertTrue(Path(result["path"]).is_file())
            self.assertEqual(len(sent), 2)
            self.assertEqual(list((root / "output").iterdir()), [Path(result["path"])])

            def pcm(path):
                return subprocess.run([
                    shutil.which("ffmpeg"), "-v", "error", "-i", str(path),
                    "-f", "s16le", "-c:a", "pcm_s16le", "-",
                ], check=True, stdout=subprocess.PIPE,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            self.assertEqual(pcm(original), pcm(result["path"]))

    def test_aac_only_account_exports_m4a_without_reencoding_packets(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original = self.make_aac(root)
            request, sent = self.fake_service(original, reported_codec="aac")
            with patch("soda_client.requests.request", side_effect=request):
                result = soda_client.download_track(
                    "1234567890123456789", root / "output", sessionid="f" * 32, quality="standard",
                )
            self.assertTrue(result["verified"])
            self.assertEqual(result["codec_name"], "aac")
            self.assertEqual(result["sample_rate"], 48000)
            self.assertEqual(result["quality"], "standard")
            self.assertFalse(result["is_lossless"])
            self.assertAlmostEqual(result["duration"], 2, delta=0.1)
            target = Path(result["path"])
            self.assertEqual(target.suffix, ".m4a")
            self.assertEqual(list((root / "output").iterdir()), [target])
            self.assertEqual(len(sent), 2)
            original_packets = self.packet_hashes(original)
            self.assertGreater(len(original_packets), 1)
            self.assertEqual(original_packets, self.packet_hashes(target))

    def test_claimed_flac_containing_aac_is_rejected_and_cleaned_up(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original = self.make_aac(root)
            request, sent = self.fake_service(original, reported_codec="flac")
            with patch("soda_client.requests.request", side_effect=request):
                with self.assertRaises(soda_client.DownloadError) as error:
                    soda_client.download_track(
                        "1234567890123456789", root / "output", sessionid="f" * 32, quality="lossless",
                    )
            self.assertNotIn("f" * 32, str(error.exception))
            self.assertEqual(len(sent), 2)
            self.assertEqual(list((root / "output").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
