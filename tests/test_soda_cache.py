"""Real LMDB-JS/Msgpackr cache-reader tests using exclusively synthetic records."""
from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import soda_cache

TRACK_ID = "1234567890123456789"
OTHER_TRACK_ID = "9876543210987654321"
SPADE = "synthetic-not-a-real-media-key"


def record(codec="aac", quality="highest", rate=256000, track_id=TRACK_ID):
    vid = "synthetic-video-id"
    url = f"https://synthetic.invalid/{quality}?expires=1&token=not-real"
    source = {
        "main_url": url,
        "video_meta": {"codec_type": codec, "quality": quality, "bitrate": rate},
        "encrypt_info": {"spade_a": SPADE},
    }
    playable = {
        "id": track_id, "vid": vid, "type": "track", "duration": 300000,
        "name": "合成缓存", "artists": [{"name": "合成作者"}],
    }
    key = f"{vid}_F_{quality}"
    return key, {
        "resourceId": key,
        "chunkId": str(uuid.uuid4()),
        "size": 18,
        "previousAccessTime": 1,
        "headers": {"content-type": "audio/mp4"},
        "info": {
            "trackId": track_id, "quality": quality, "bitrate": rate,
            "isPreview": False, "urls": [url], "spade": SPADE,
            "mediaDetail": {
                "playable": playable, "video_model_type": 1,
                "video_model": {"video_list": [source]},
                "response": {
                    "status_code": 0, "track": playable,
                    "track_player": {"video_model_type": 1}, "expire_at": 1,
                },
            },
        },
    }


try:
    NODE, BRIDGE, RUNTIME = soda_cache._runtime_paths()
    HAS_RUNTIME = True
except soda_cache.CacheError:
    HAS_RUNTIME = False


@unittest.skipUnless(HAS_RUNTIME, "Bundled Node LMDB-JS runtime required")
class CacheReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cache = self.root / "LunaCacheV2"
        self.cache.mkdir()

    def write_cache(self, records, *, media=True):
        writer = r"""
const fs = require('node:fs');
const path = require('node:path');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const {open} = require(path.join(input.runtime, 'node_modules', 'lmdb'));
const db = open({path: path.join(input.cache, 'entries.db')});
for (const [key, value] of input.records) {
  // Exercise real Msgpackr record extensions and undefined values.
  value.optional = undefined;
  db.putSync(key, value);
}
db.close().then(() => process.stdout.write('ok'));
"""
        completed = subprocess.run(
            [str(NODE), "-e", writer],
            input=json.dumps({"runtime": str(RUNTIME), "cache": str(self.cache), "records": records}).encode(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(completed.stdout, b"ok")
        if media:
            for _key, value in records:
                chunk = value.get("chunkId")
                if chunk and "/" not in chunk and "\\" not in chunk:
                    (self.cache / (chunk + ".bin")).write_bytes(b"synthetic audio!!!")

    def find(self, **kwargs):
        return soda_cache.find_cached_track(TRACK_ID, cache_dir=self.cache, **kwargs)

    def test_native_v2_and_msgpackr_records_read_without_modifying_database(self):
        item = record()
        self.write_cache([item])
        database = self.cache / "entries.db"
        before = hashlib.sha256(database.read_bytes()).hexdigest()
        result = self.find()
        after = hashlib.sha256(database.read_bytes()).hexdigest()
        self.assertEqual(before, after)
        self.assertEqual(result["codec"], "aac")
        self.assertEqual(result["extension"], ".m4a")
        self.assertEqual(result["duration"], 300)
        self.assertEqual(result["cache_size"], 18)
        self.assertEqual(result["spade"], SPADE)
        self.assertEqual(result["cache_quality"], "highest")
        self.assertEqual(Path(result["path"]).name, item[1]["chunkId"] + ".bin")
        self.assertNotIn("https://", json.dumps(result))
        self.assertNotIn("not-real", json.dumps(result))

    def test_bridge_excludes_signed_urls_and_unrelated_track_data(self):
        item = record()
        other = record(track_id=OTHER_TRACK_ID, quality="other")
        other[1]["info"]["mediaDetail"]["playable"]["name"] = "UNRELATED_PRIVATE_TITLE"
        self.write_cache([item, other])
        result = soda_cache._read_bridge(self.cache, TRACK_ID)
        serialized = json.dumps(result)
        self.assertEqual(len(result["matches"]), 1)
        self.assertNotIn("https://", serialized)
        self.assertNotIn(OTHER_TRACK_ID, serialized)
        self.assertNotIn("UNRELATED_PRIVATE_TITLE", serialized)
        self.assertNotIn("not-real", serialized)

    def test_inspection_returns_counts_only(self):
        self.write_cache([record()])
        result = soda_cache.inspect_cache(self.cache)
        self.assertEqual(result, {"entries": 1, "with_metadata": 1, "full_marked": 1, "preview_marked": 0})

    def test_quality_selection_uses_only_complete_cached_sources(self):
        self.write_cache([
            record(codec="aac", quality="medium", rate=64000),
            record(codec="aac", quality="highest", rate=320000),
            record(codec="flac", quality="lossless", rate=120000),
        ])
        self.assertEqual(self.find(quality="best")["codec"], "flac")
        self.assertEqual(self.find(quality="standard")["bit_rate"], 64000)
        self.assertEqual(self.find(quality="high")["bit_rate"], 320000)
        self.assertEqual(self.find(quality="lossless")["codec"], "flac")

    def test_requested_lossless_missing_is_retryable_and_not_fabricated(self):
        self.write_cache([record()])
        with self.assertRaisesRegex(soda_cache.CacheError, "FLAC") as error:
            self.find(quality="lossless")
        self.assertTrue(error.exception.retryable)

    def test_preview_and_model_type_two_are_rejected(self):
        for field in ("info", "detail", "response"):
            with self.subTest(field=field):
                key, value = record()
                if field == "info":
                    value["info"]["isPreview"] = True
                elif field == "detail":
                    value["info"]["mediaDetail"]["video_model_type"] = 2
                else:
                    value["info"]["mediaDetail"]["response"]["track_player"]["video_model_type"] = 2
                self.write_cache([(key, value)])
                with self.assertRaisesRegex(soda_cache.CacheError, "试听") as error:
                    self.find()
                self.assertTrue(error.exception.retryable)

    def test_resource_marker_must_match_full_version(self):
        key, value = record()
        preview_key = key.replace("_F_", "_P_")
        value["resourceId"] = preview_key
        self.write_cache([(preview_key, value)])
        with self.assertRaises(soda_cache.CacheError) as error:
            self.find()
        self.assertTrue(error.exception.retryable)

    def test_incomplete_or_missing_media_is_retryable(self):
        item = record()
        self.write_cache([item])
        media = self.cache / (item[1]["chunkId"] + ".bin")
        media.write_bytes(b"partial")
        with self.assertRaises(soda_cache.CacheError) as error:
            self.find()
        self.assertTrue(error.exception.retryable)
        media.unlink()
        with self.assertRaises(soda_cache.CacheError) as error:
            self.find()
        self.assertTrue(error.exception.retryable)

    def test_chunk_path_cannot_traverse_outside_cache(self):
        key, value = record()
        value["chunkId"] = "../outside"
        self.write_cache([(key, value)], media=False)
        with self.assertRaises(soda_cache.CacheError) as error:
            self.find()
        self.assertFalse(error.exception.retryable)

    def test_stale_urls_are_never_requested_and_are_not_returned(self):
        self.write_cache([record()])
        # A deliberately expired URL is metadata only; the local .bin is used.
        with patch("requests.request", side_effect=AssertionError("network forbidden")):
            result = self.find()
        self.assertEqual(result["codec"], "aac")
        self.assertTrue(Path(result["path"]).is_file())

    def test_metadata_track_identity_and_duration_are_required(self):
        for field in ("identity", "duration"):
            with self.subTest(field=field):
                key, value = record()
                playable = value["info"]["mediaDetail"]["playable"]
                if field == "identity":
                    playable["id"] = OTHER_TRACK_ID
                else:
                    playable["duration"] = 0
                self.write_cache([(key, value)])
                with self.assertRaises(soda_cache.CacheError) as error:
                    self.find()
                self.assertFalse(error.exception.retryable)

    def test_missing_track_is_retryable(self):
        self.write_cache([record(track_id=OTHER_TRACK_ID)])
        with self.assertRaises(soda_cache.CacheError) as error:
            self.find()
        self.assertTrue(error.exception.retryable)

    def test_corrupt_database_error_does_not_expose_content(self):
        secret = b"DO_NOT_EXPOSE_DATABASE_CONTENT"
        (self.cache / "entries.db").write_bytes(secret)
        with self.assertRaises(soda_cache.CacheError) as error:
            self.find()
        self.assertNotIn(secret.decode(), str(error.exception))
        self.assertFalse(error.exception.retryable)

    def test_cancellation_prevents_bridge_launch(self):
        cancel = threading.Event()
        cancel.set()
        with patch.object(soda_cache.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(soda_cache.CacheError, "取消"):
                self.find(cancel=cancel)
            popen.assert_not_called()


class CacheInputTests(unittest.TestCase):
    def test_missing_cache_is_retryable(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(soda_cache.CacheError) as error:
                soda_cache.find_cached_track(TRACK_ID, cache_dir=directory)
            self.assertTrue(error.exception.retryable)

    def test_invalid_id_and_quality_do_not_launch_reader(self):
        with patch.object(soda_cache, "_read_bridge") as reader:
            for track_id, quality in (("invalid", "best"), (TRACK_ID, "ultra")):
                with self.subTest(track_id=track_id, quality=quality):
                    with self.assertRaises(soda_cache.CacheError):
                        soda_cache.find_cached_track(track_id, quality=quality)
            reader.assert_not_called()


if __name__ == "__main__":
    unittest.main()
