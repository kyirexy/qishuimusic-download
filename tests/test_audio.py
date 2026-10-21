"""Synthetic local fixtures only; no network requests or copyrighted audio."""
from __future__ import annotations

import base64
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from Crypto.Cipher import AES

from soda_audio import AudioCancelledError, AudioExportError, export_audio, export_flac, _decode_key


FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
KEY = bytes.fromhex("00112233445566778899aabbccddeeff")


def run(*args: str) -> str:
    return subprocess.check_output(
        args, stderr=subprocess.PIPE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
    ).decode()


def box(kind: bytes, data: bytes, large: bool = False) -> bytes:
    return (struct.pack(">I4sQ", 1, kind, len(data) + 16) if large else struct.pack(">I4s", len(data) + 8, kind)) + data


def packet_hashes(path: Path) -> list[str]:
    """Compare compressed packets, independently of rewritten container tags."""
    result = json.loads(run(
        FFPROBE, "-v", "error", "-select_streams", "a:0", "-show_packets",
        "-show_data_hash", "sha256", "-show_entries", "packet=data_hash",
        "-of", "json", str(path),
    ))
    return [packet["data_hash"] for packet in result["packets"]]


def encrypted_mp4(flac: Path, *, co64=False, iv_length=8, subsamples=False, malformed=False) -> bytes:
    """Build independently authored CENC with padding between real chunks."""
    raw = flac.read_bytes()
    end = 4
    while True:
        header = raw[end : end + 4]
        end += 4 + int.from_bytes(header[1:], "big")
        if header[0] & 128:
            break
    metadata = raw[4:end]
    packets = json.loads(run(
        FFPROBE, "-v", "error", "-show_packets", "-show_entries", "packet=pos,size",
        "-of", "json", str(flac),
    ))["packets"]
    frames = [raw[int(packet["pos"]):int(packet["pos"]) + int(packet["size"])] for packet in packets]
    ftyp = box(b"ftyp", b"isom\x00\x00\x02\x00isomiso2")
    payload = bytearray(b"PADDING-START")
    offsets = []
    sizes = []
    senc_entries = bytearray()
    for index, frame in enumerate(frames):
        if index % 2 == 0:
            offsets.append(len(ftyp) + 8 + len(payload))
        iv = (index + 1).to_bytes(iv_length, "big")
        cipher = AES.new(KEY, AES.MODE_CTR, nonce=b"", initial_value=int.from_bytes(iv.ljust(16, b"\0"), "big"))
        if subsamples:
            clear = min(9, len(frame))
            protected = len(frame) - clear
            sample = frame[:clear] + cipher.encrypt(frame[clear:])
            senc_entries += iv + struct.pack(">HHI", 1, clear, protected)
        else:
            sample = cipher.encrypt(frame)
            senc_entries += iv
        sizes.append(len(sample))
        payload += sample
        if index % 2 == 1:
            payload += b"IGNORED-CHUNK-PADDING"
    mdat = box(b"mdat", bytes(payload))
    audio_header = b"\0" * 6 + b"\0\1" + b"\0" * 8 + struct.pack(">HH", 1, 16) + b"\0" * 4 + struct.pack(">I", 44100 << 16)
    tenc = box(b"tenc", b"\0" * 6 + bytes([1, iv_length]) + b"\x22" * 16)
    sinf = box(b"sinf", box(b"frma", b"fLaC") + box(b"schm", b"\0" * 4 + b"cenc" + b"\0\x01\0\0") + box(b"schi", tenc))
    entry = box(b"enca", audio_header + box(b"dfLa", b"\0" * 4 + metadata) + sinf)
    stsd = box(b"stsd", b"\0" * 4 + struct.pack(">I", 1) + entry)
    stsz = box(b"stsz", b"\0" * 8 + struct.pack(">I", len(sizes)) + struct.pack(f">{len(sizes)}I", *sizes))
    entries = [(1, 2, 1)]
    if len(sizes) % 2:
        if len(sizes) == 1:
            entries = [(1, 1, 1)]
        else:
            entries.append((len(offsets), 1, 1))
    stsc = box(b"stsc", b"\0" * 4 + struct.pack(">I", len(entries)) + b"".join(struct.pack(">III", *entry) for entry in entries))
    if malformed:
        offsets[-1] = 2**31
    chunk_offsets = box(b"co64" if co64 else b"stco", b"\0" * 4 + struct.pack(">I", len(offsets)) + struct.pack(f">{len(offsets)}{'Q' if co64 else 'I'}", *offsets))
    senc = box(b"senc", (2 if subsamples else 0).to_bytes(4, "big") + struct.pack(">I", len(sizes)) + senc_entries)
    stbl = box(b"stbl", stsd + stsz + stsc + chunk_offsets + senc)
    hdlr = box(b"hdlr", b"\0" * 8 + b"soun" + b"\0" * 12)
    trak = box(b"trak", box(b"mdia", hdlr + box(b"minf", stbl)))
    return ftyp + mdat + box(b"moov", trak, large=co64)


@unittest.skipUnless(FFMPEG and FFPROBE, "FFmpeg and FFprobe required")
class AudioExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture_dir = tempfile.TemporaryDirectory()
        cls.flac = Path(cls.fixture_dir.name) / "generated.flac"
        cls.aac = Path(cls.fixture_dir.name) / "generated.m4a"
        cls.mp3 = Path(cls.fixture_dir.name) / "generated.mp3"
        cls.opus = Path(cls.fixture_dir.name) / "generated.opus"
        run(FFMPEG, "-v", "error", "-f", "lavfi", "-i", "sine=frequency=523:sample_rate=44100:duration=1.7", "-c:a", "flac", str(cls.flac))
        run(FFMPEG, "-v", "error", "-i", str(cls.flac), "-c:a", "aac", str(cls.aac))
        run(FFMPEG, "-v", "error", "-i", str(cls.flac), "-c:a", "libmp3lame", str(cls.mp3))
        run(FFMPEG, "-v", "error", "-i", str(cls.flac), "-c:a", "libopus", str(cls.opus))

    @classmethod
    def tearDownClass(cls):
        cls.fixture_dir.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.target = self.directory / "verified.flac"

    def tearDown(self):
        self.tmp.cleanup()

    def export(self, source=None, **kwargs):
        return export_flac(source or self.flac, self.target, kwargs.pop("spade_a", ""), FFMPEG, FFPROBE, **kwargs)

    def assert_no_output(self):
        self.assertFalse(self.target.exists())
        self.assertFalse(list(self.directory.glob(".soda-audio-*")))

    def test_plain_flac_preserves_pcm(self):
        result = self.export(expected_duration=1.7)
        self.assertTrue(result["verified"])
        self.assertEqual(result["codec"], "flac")
        self.assertEqual(result["sample_rate"], 44100)
        self.assertEqual(result["bits_per_sample"], 16)
        self.assertAlmostEqual(result["duration"], 1.7, places=4)
        hashes = [
            run(FFMPEG, "-v", "error", "-i", str(path), "-map", "0:a:0", "-f", "hash", "-hash", "sha256", "-").strip()
            for path in (self.flac, self.target)
        ]
        self.assertEqual(*hashes)

    def test_aac_is_rejected(self):
        with self.assertRaises(AudioExportError):
            self.export(self.aac)
        self.assert_no_output()

    def test_preview_duration_is_rejected(self):
        with self.assertRaisesRegex(AudioExportError, "试听"):
            self.export(expected_duration=180)
        self.assert_no_output()

    def test_existing_target_is_preserved(self):
        self.target.write_bytes(b"user-file")
        with self.assertRaisesRegex(AudioExportError, "已存在"):
            self.export()
        self.assertEqual(self.target.read_bytes(), b"user-file")

    def test_precancelled_leaves_no_output(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(AudioCancelledError):
            self.export(cancel=cancel)
        self.assert_no_output()

    def test_cancellation_during_process_is_prompt(self):
        cancel = threading.Event()
        from soda_audio import _run
        import sys
        timer = threading.Timer(0.2, cancel.set)
        timer.start()
        try:
            with self.assertRaises(AudioCancelledError):
                _run([sys.executable, "-c", "import time; time.sleep(30)"], cancel, timeout=5)
        finally:
            timer.cancel()

    def test_invalid_key_error_does_not_expose_key(self):
        secret = "not-a-real-secret-value"
        with self.assertRaises(AudioExportError) as raised:
            self.export(spade_a=secret)
        self.assertNotIn(secret, str(raised.exception))
        self.assert_no_output()

    def test_hex_key(self):
        self.assertEqual(_decode_key(KEY.hex().upper()), KEY)

    def test_spade_key_decoder(self):
        plain = b"0" + KEY.hex().encode("ascii")
        encoded = bytearray()
        for index, value in enumerate(plain):
            previous = b"\xfa\x55"[index] if index < 2 else encoded[index - 2]
            encoded.append((value + index.bit_count() + 21) ^ previous)
        header = 48 ^ encoded[0] ^ encoded[1]
        wrapped = base64.b64encode(bytes([header]) + encoded).decode("ascii")
        self.assertEqual(_decode_key(wrapped), KEY)

    def test_native_ffmpeg_cenc_flac(self):
        source = self.directory / "native-cenc.mp4"
        run(
            FFMPEG, "-v", "error", "-i", str(self.flac), "-c:a", "copy", "-strict", "experimental",
            "-encryption_scheme", "cenc-aes-ctr", "-encryption_key", KEY.hex(),
            "-encryption_kid", "22" * 16, str(source),
        )
        with patch("soda_audio._extract_mp4_flac", side_effect=AssertionError("Native FFmpeg should handle this fixture")):
            result = self.export(source, spade_a=KEY.hex(), expected_duration=1.7)
        self.assertTrue(result["verified"])

    def test_fallback_correct_chunk_tables_and_subsamples(self):
        for co64, iv_length, subsamples in [(False, 8, False), (True, 16, True)]:
            with self.subTest(co64=co64, iv_length=iv_length, subsamples=subsamples):
                source = self.directory / "synthetic-cenc.mp4"
                source.write_bytes(encrypted_mp4(self.flac, co64=co64, iv_length=iv_length, subsamples=subsamples))
                # Ensure the independently assembled sample-table path is tested.
                with patch("soda_audio._remux", side_effect=AudioExportError("测试封装")):
                    self.export(source, spade_a=KEY.hex(), expected_duration=1.7)
                self.assertEqual(self.target.read_bytes(), self.flac.read_bytes())
                self.target.unlink()

    def test_out_of_bounds_chunk_is_rejected(self):
        source = self.directory / "bad-offset.mp4"
        source.write_bytes(encrypted_mp4(self.flac, malformed=True))
        with patch("soda_audio._remux", side_effect=AudioExportError("测试封装")):
            with self.assertRaises(AudioExportError):
                self.export(source, spade_a=KEY.hex())
        self.assert_no_output()

    def test_wrong_decryption_key_is_rejected(self):
        source = self.directory / "wrong-key.mp4"
        source.write_bytes(encrypted_mp4(self.flac))
        with patch("soda_audio._remux", side_effect=AudioExportError("测试封装")):
            with self.assertRaises(AudioExportError):
                self.export(source, spade_a="ff" * 16)
        self.assert_no_output()

    def test_truncated_flac_is_rejected(self):
        source = self.directory / "truncated.flac"
        raw = self.flac.read_bytes()
        source.write_bytes(raw[:-800])
        with self.assertRaises(AudioExportError):
            self.export(source, expected_duration=1.7)
        self.assert_no_output()

    def test_complete_frame_truncation_is_rejected_without_external_duration(self):
        packets = json.loads(run(
            FFPROBE, "-v", "error", "-show_packets", "-show_entries", "packet=pos,size",
            "-of", "json", str(self.flac),
        ))["packets"]
        source = self.directory / "missing-last-frame.flac"
        source.write_bytes(self.flac.read_bytes()[:int(packets[-1]["pos"])])
        with self.assertRaises(AudioExportError):
            self.export(source)
        self.assert_no_output()

    def test_cancel_during_commit_removes_only_our_target(self):
        import soda_audio
        original = soda_audio._cancelled
        cancel = threading.Event()

        def cancel_after_creation(event):
            if self.target.exists():
                cancel.set()
            original(event)

        with patch("soda_audio._cancelled", side_effect=cancel_after_creation):
            with self.assertRaises(AudioCancelledError):
                self.export(cancel=cancel)
        self.assert_no_output()

    def test_concurrent_target_is_preserved(self):
        import soda_audio
        original = soda_audio._validate

        def another_writer(*args):
            result = original(*args)
            self.target.write_bytes(b"concurrently-created-user-file")
            return result

        with patch("soda_audio._validate", side_effect=another_writer):
            with self.assertRaisesRegex(AudioExportError, "已存在"):
                self.export()
        self.assertEqual(self.target.read_bytes(), b"concurrently-created-user-file")
        self.assertFalse(list(self.directory.glob(".soda-audio-*")))

    def test_export_audio_lossy_codecs_preserve_compressed_packets(self):
        for codec, source in (("aac", self.aac), ("mp3", self.mp3), ("opus", self.opus)):
            with self.subTest(codec=codec):
                target = self.directory / f"original{source.suffix}"
                result = export_audio(
                    source, target, "", FFMPEG, FFPROBE,
                    expected_duration=1.7, expected_codec=codec,
                )
                self.assertEqual(result["codec"], codec)
                self.assertEqual(result["extension"], source.suffix)
                self.assertFalse(result["is_lossless"])
                self.assertTrue(result["verified"])
                self.assertGreater(result["bit_rate"], 0)
                self.assertGreater(result["sample_rate"], 0)
                self.assertGreaterEqual(result["bits_per_sample"], 0)
                self.assertEqual(packet_hashes(source), packet_hashes(target))

    def test_export_audio_detects_actual_codec(self):
        for source in (self.flac, self.aac, self.mp3, self.opus):
            with self.subTest(suffix=source.suffix):
                target = self.directory / f"autodetected{source.suffix}"
                result = export_audio(source, target, "", FFMPEG, FFPROBE, expected_duration=1.7)
                self.assertEqual(result["is_lossless"], source.suffix == ".flac")
                self.assertEqual(result["extension"], source.suffix)

    def test_native_ffmpeg_cenc_aac_preserves_original_packets(self):
        source = self.directory / "native-cenc-aac.mp4"
        target = self.directory / "decrypted.m4a"
        run(
            FFMPEG, "-v", "error", "-i", str(self.aac), "-c:a", "copy",
            "-encryption_scheme", "cenc-aes-ctr", "-encryption_key", KEY.hex(),
            "-encryption_kid", "22" * 16, str(source),
        )
        with patch("soda_audio._extract_mp4_flac", side_effect=AssertionError("AAC must never use the FLAC fallback")):
            result = export_audio(
                source, target, KEY.hex(), FFMPEG, FFPROBE,
                expected_duration=1.7, expected_codec="aac",
            )
        self.assertEqual(result["codec_name"], "aac")
        self.assertFalse(result["is_lossless"])
        self.assertEqual(packet_hashes(self.aac), packet_hashes(target))

    def test_expected_codec_mismatch_is_rejected(self):
        target = self.directory / "mismatch.mp3"
        with self.assertRaises(AudioExportError):
            export_audio(self.aac, target, "", FFMPEG, FFPROBE, expected_codec="mp3")
        self.assertFalse(target.exists())
        self.assertFalse(list(self.directory.glob(".soda-audio-*")))

    def test_wrong_extension_does_not_disguise_lossy_audio(self):
        with self.assertRaisesRegex(AudioExportError, "扩展名"):
            export_audio(self.aac, self.target, "", FFMPEG, FFPROBE)
        self.assert_no_output()

    def test_lossy_preview_is_rejected(self):
        target = self.directory / "preview.m4a"
        with self.assertRaisesRegex(AudioExportError, "试听"):
            export_audio(
                self.aac, target, "", FFMPEG, FFPROBE,
                expected_duration=180, expected_codec="aac",
            )
        self.assertFalse(target.exists())
        self.assertFalse(list(self.directory.glob(".soda-audio-*")))

    def test_aac_decryption_failure_does_not_try_flac_fallback(self):
        target = self.directory / "unsupported.m4a"
        with patch("soda_audio._remux", side_effect=AudioExportError("secret-key-from-ffmpeg-log")):
            with patch("soda_audio._extract_mp4_flac", side_effect=AssertionError("Unexpected fallback")):
                with self.assertRaises(AudioExportError) as raised:
                    export_audio(self.aac, target, "", FFMPEG, FFPROBE, expected_codec="aac")
        self.assertNotIn("secret-key", str(raised.exception))
        self.assertIn("暂不支持", str(raised.exception))
        self.assertFalse(target.exists())
        self.assertFalse(list(self.directory.glob(".soda-audio-*")))


if __name__ == "__main__":
    unittest.main()
