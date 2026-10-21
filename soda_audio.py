"""Validate and save original audio returned for the user's own account.

No network calls, credential lookup, lossy-to-lossless transcoding, or permission
changes happen here. FFmpeg is the normal demuxer; the restricted MP4 fallback
handles non-fragmented FLAC/CENC files only and fails closed on other layouts.

The spade key decoding is adapted from MIT-licensed baizeyv/SodaDownloader,
copyright (c) 2026 baizeyv. See the application's third-party license notice.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import mmap
import os
from pathlib import Path
import re
import struct
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Iterator


class AudioExportError(Exception):
    """A safe, user-displayable error; never contains media keys or raw logs."""


class AudioCancelledError(AudioExportError):
    """The caller requested cancellation."""


_FORMATS = {
    "flac": (".flac", "flac"),
    "aac": (".m4a", "ipod"),
    "mp3": (".mp3", "mp3"),
    "opus": (".opus", "opus"),
}


def _cancelled(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise AudioCancelledError("已取消下载。")


def _run(
    args: list[str], cancel: threading.Event | None, timeout: float = 900
) -> str:
    """Drain both pipes while polling cancellation; never expose command/logs."""
    _cancelled(cancel)
    try:
        proc = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, ValueError):
        raise AudioExportError("无法启动音频处理组件，请检查 FFmpeg 安装。") from None
    started = time.monotonic()
    try:
        while True:
            _cancelled(cancel)
            if time.monotonic() - started > timeout:
                raise AudioExportError("音频处理超时，请重试。")
            try:
                stdout, _stderr = proc.communicate(timeout=0.15)
                break
            except subprocess.TimeoutExpired:
                continue
        _cancelled(cancel)
        if proc.returncode:
            raise AudioExportError("音频无法完整解析或解码，未保存文件。")
        return stdout.decode("utf-8", errors="replace")
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.communicate()


def _decode_key(value: str) -> bytes | None:
    value = value.strip()
    if not value:
        return None
    if re.fullmatch(r"[0-9a-fA-F]{32}", value):
        return bytes.fromhex(value)
    try:
        raw = base64.b64decode(value, validate=True)
        if len(raw) < 4 or len(raw) > 4096:
            raise ValueError
        padding = (raw[0] ^ raw[1] ^ raw[2]) - 48
        if padding < 0 or padding > len(raw) - 3:
            raise ValueError
        inner = raw[1 : len(raw) - padding]
        previous = b"\xfa\x55" + inner
        decoded = bytearray()
        for i, current in enumerate(inner):
            v = (current ^ previous[i]) - i.bit_count() - 21
            while v < 0:
                v += 255
            decoded.append(v & 255)
        skip_char = chr(decoded[0])
        if skip_char not in "0123456789abcdefghijklmnopqrstuvwxyz":
            raise ValueError
        skip = int(skip_char, 36)
        end = len(raw) - padding - 1 - skip
        if end < 1 or end > len(decoded):
            raise ValueError
        hex_key = bytes(decoded[1:end]).decode("ascii")
        if not re.fullmatch(r"[0-9a-fA-F]{32}", hex_key):
            raise ValueError
        return bytes.fromhex(hex_key)
    except (ValueError, IndexError, UnicodeError, binascii.Error):
        raise AudioExportError("服务端返回的音频密钥格式不受支持，请重新获取歌曲。") from None


def _probe(
    source: Path,
    ffprobe: str,
    cancel: threading.Event | None,
    key: bytes | None = None,
) -> dict:
    args = [ffprobe, "-v", "error"]
    if key:
        args += ["-decryption_key", key.hex()]
    args += ["-show_streams", "-show_format", "-of", "json", str(source)]
    try:
        return json.loads(_run(args, cancel))
    except (json.JSONDecodeError, TypeError):
        raise AudioExportError("无法读取音频信息，未保存文件。") from None


def _remux(
    source: Path,
    target: Path,
    key: bytes | None,
    ffmpeg: str,
    ffprobe: str,
    cancel: threading.Event | None,
    expected_codec: str | None = "flac",
) -> dict:
    info = _probe(source, ffprobe, cancel, key)
    stream = next(
        (
            s for s in info.get("streams", [])
            if s.get("codec_type") == "audio" and s.get("codec_name") in _FORMATS
            and (expected_codec is None or s.get("codec_name") == expected_codec)
        ),
        None,
    )
    if stream is None:
        if expected_codec:
            raise AudioExportError("音源实际编码与所选音质不一致，或该编码暂不支持。")
        raise AudioExportError("音源编码暂不支持；目前支持 FLAC、AAC、MP3 和 Opus。")
    codec = stream["codec_name"]
    args = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
    if key:
        args += ["-decryption_key", key.hex()]
    # Stream-copy is intentional: no encoder is ever selected in this module.
    args += [
        "-i", str(source), "-map", f"0:{int(stream['index'])}",
        "-vn", "-sn", "-dn", "-c:a", "copy", "-f", _FORMATS[codec][1], str(target),
    ]
    _run(args, cancel)
    return {
        "codec": codec,
        "source_duration": _positive_float(stream.get("duration")) or _positive_float(info.get("format", {}).get("duration")),
    }


@dataclass(frozen=True)
class _Box:
    kind: bytes
    start: int
    payload: int
    end: int


def _bad() -> AudioExportError:
    return AudioExportError("该音源的无损封装或加密方式暂不支持，未保存文件。")


def _boxes(data: mmap.mmap, start: int, end: int) -> Iterator[_Box]:
    if start < 0 or end > len(data) or start > end:
        raise _bad()
    pos = start
    while pos < end:
        if end - pos < 8:
            raise _bad()
        size, kind = struct.unpack_from(">I4s", data, pos)
        header = 8
        if size == 1:
            if end - pos < 16:
                raise _bad()
            size = struct.unpack_from(">Q", data, pos + 8)[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or size > end - pos:
            raise _bad()
        yield _Box(kind, pos, pos + header, pos + size)
        pos += size


def _children(data: mmap.mmap, parent: _Box) -> list[_Box]:
    return list(_boxes(data, parent.payload, parent.end))


def _one(boxes: list[_Box], kind: bytes, required: bool = True) -> _Box | None:
    found = [box for box in boxes if box.kind == kind]
    if len(found) > 1 or (not found and required):
        raise _bad()
    return found[0] if found else None


def _body(data: mmap.mmap, box: _Box, minimum: int = 0) -> bytes:
    if box.end - box.payload < minimum:
        raise _bad()
    return data[box.payload : box.end]


def _metadata(body: bytes) -> bytes:
    """Validate dfLa metadata without inventing or altering audio samples."""
    if len(body) < 42 or body[:4] != b"\x00\x00\x00\x00":
        raise _bad()
    raw = bytearray(body[4:])
    pos = 0
    count = 0
    while pos < len(raw):
        if len(raw) - pos < 4:
            raise _bad()
        kind = raw[pos] & 127
        size = int.from_bytes(raw[pos + 1 : pos + 4], "big")
        if (count == 0 and (kind != 0 or size != 34)) or kind == 127:
            raise _bad()
        end = pos + 4 + size
        if end > len(raw) or (raw[pos] & 128 and end != len(raw)):
            raise _bad()
        # dfLa may not set the standalone FLAC "last metadata block" bit.
        raw[pos] = kind | (128 if end == len(raw) else 0)
        pos = end
        count += 1
    return b"fLaC" + raw


def _sample_sizes(data: mmap.mmap, box: _Box) -> list[int]:
    raw = _body(data, box, 12)
    fixed, count = struct.unpack_from(">II", raw, 4)
    if not count or count > 5_000_000:
        raise _bad()
    if fixed:
        if len(raw) != 12:
            raise _bad()
        sizes = [fixed] * count
    else:
        if len(raw) != 12 + count * 4:
            raise _bad()
        sizes = list(struct.unpack_from(f">{count}I", raw, 12))
    if any(size == 0 or size > 64 * 1024 * 1024 for size in sizes):
        raise _bad()
    return sizes


def _sample_positions(
    data: mmap.mmap,
    stbl: list[_Box],
    sizes: list[int],
    description: int,
    mdats: list[_Box],
) -> list[int]:
    co32, co64 = _one(stbl, b"stco", False), _one(stbl, b"co64", False)
    if (co32 is None) == (co64 is None):
        raise _bad()
    offsets_box = co32 or co64
    raw = _body(data, offsets_box, 8)
    count = struct.unpack_from(">I", raw, 4)[0]
    width = 4 if co32 else 8
    if not count or count > len(sizes) or len(raw) != 8 + count * width:
        raise _bad()
    offsets = struct.unpack_from(f">{count}{'I' if co32 else 'Q'}", raw, 8)
    raw = _body(data, _one(stbl, b"stsc"), 8)
    entry_count = struct.unpack_from(">I", raw, 4)[0]
    if not entry_count or len(raw) != 8 + entry_count * 12:
        raise _bad()
    entries = [struct.unpack_from(">III", raw, 8 + i * 12) for i in range(entry_count)]
    last_first = 0
    for first, per_chunk, desc in entries:
        if first <= last_first or first > count or not per_chunk or desc != description:
            raise _bad()
        last_first = first
    if entries[0][0] != 1:
        raise _bad()
    positions: list[int] = []
    entry_index = 0
    for chunk_number, offset in enumerate(offsets, 1):
        if entry_index + 1 < len(entries) and chunk_number == entries[entry_index + 1][0]:
            entry_index += 1
        per_chunk = entries[entry_index][1]
        if per_chunk > len(sizes) - len(positions):
            raise _bad()
        chunk_end = offset + sum(sizes[len(positions) : len(positions) + per_chunk])
        if not any(box.payload <= offset < chunk_end <= box.end for box in mdats):
            raise _bad()
        for _ in range(per_chunk):
            positions.append(offset)
            offset += sizes[len(positions) - 1]
    if len(positions) != len(sizes):
        raise _bad()
    return positions


def _encryption(
    data: mmap.mmap,
    entry_boxes: list[_Box],
    senc_box: _Box,
    sizes: list[int],
) -> list[tuple[bytes, list[tuple[int, int]]]]:
    iv_size: int | None = None
    constant_iv: bytes | None = None
    sinf = _one(entry_boxes, b"sinf", False)
    if sinf:
        sinf_children = _children(data, sinf)
        schm = _one(sinf_children, b"schm", False)
        if schm and _body(data, schm, 12)[4:8] != b"cenc":
            raise _bad()
        schi = _one(sinf_children, b"schi", False)
        tenc = _one(_children(data, schi), b"tenc", False) if schi else None
        if tenc:
            raw = _body(data, tenc, 24)
            if raw[0] not in (0, 1) or raw[5] != 0 or raw[6] != 1:
                raise _bad()
            iv_size = raw[7]
            if iv_size == 0:
                if len(raw) < 25 or raw[24] not in (8, 16) or len(raw) != 25 + raw[24]:
                    raise _bad()
                constant_iv = raw[25:]
            elif iv_size not in (8, 16):
                raise _bad()
    raw = _body(data, senc_box, 8)
    flags = int.from_bytes(raw[1:4], "big")
    if raw[0] != 0 or flags & ~3:
        raise _bad()
    pos = 4
    if flags & 1:
        if len(raw) < 28 or int.from_bytes(raw[pos : pos + 3], "big") != 1:
            raise _bad()
        iv_size = raw[pos + 3]
        if iv_size not in (8, 16):
            raise _bad()
        pos += 20
    if len(raw) < pos + 4 or int.from_bytes(raw[pos : pos + 4], "big") != len(sizes):
        raise _bad()
    pos += 4
    if iv_size is None:
        if flags & 2:
            raise _bad()
        remaining = len(raw) - pos
        iv_size = remaining // len(sizes)
        if iv_size not in (8, 16) or remaining != iv_size * len(sizes):
            raise _bad()
    result: list[tuple[bytes, list[tuple[int, int]]]] = []
    for size in sizes:
        if pos + iv_size > len(raw):
            raise _bad()
        iv = raw[pos : pos + iv_size] if iv_size else constant_iv
        pos += iv_size
        if iv is None or len(iv) not in (8, 16):
            raise _bad()
        subsamples: list[tuple[int, int]] = []
        if flags & 2:
            if pos + 2 > len(raw):
                raise _bad()
            count = int.from_bytes(raw[pos : pos + 2], "big")
            pos += 2
            if count == 0 or pos + count * 6 > len(raw):
                raise _bad()
            for _ in range(count):
                clear, encrypted = struct.unpack_from(">HI", raw, pos)
                pos += 6
                subsamples.append((clear, encrypted))
            if sum(clear + encrypted for clear, encrypted in subsamples) != size:
                raise _bad()
        else:
            subsamples = [(0, size)]
        result.append((iv.ljust(16, b"\0"), subsamples))
    if pos != len(raw):
        raise _bad()
    return result


def _extract_mp4_flac(
    source: Path,
    target: Path,
    key: bytes | None,
    cancel: threading.Event | None,
) -> None:
    """Small fallback: FLAC in non-fragmented MP4, with strict table bounds."""
    _cancelled(cancel)
    try:
        from Crypto.Cipher import AES
    except ImportError:
        raise AudioExportError("该无损封装需要 PyCryptodome 音频处理依赖。") from None
    with source.open("rb") as input_file:
        if not source.stat().st_size:
            raise _bad()
        with mmap.mmap(input_file.fileno(), 0, access=mmap.ACCESS_READ) as data:
            top = list(_boxes(data, 0, len(data)))
            if any(box.kind == b"moof" for box in top):
                raise _bad()
            moov = _one(top, b"moov")
            moov_children = _children(data, moov)
            mdats = [box for box in top if box.kind == b"mdat"]
            if not mdats:
                raise _bad()
            selection = None
            for trak in (box for box in moov_children if box.kind == b"trak"):
                mdia = _one(_children(data, trak), b"mdia")
                mdia_children = _children(data, mdia)
                hdlr = _one(mdia_children, b"hdlr", False)
                if hdlr and _body(data, hdlr, 12)[8:12] != b"soun":
                    continue
                minf = _one(mdia_children, b"minf")
                stbl = _children(data, _one(_children(data, minf), b"stbl"))
                stsd = _one(stbl, b"stsd")
                header = _body(data, stsd, 8)
                descriptions = list(_boxes(data, stsd.payload + 8, stsd.end))
                if len(descriptions) != int.from_bytes(header[4:8], "big"):
                    raise _bad()
                for index, entry in enumerate(descriptions, 1):
                    if entry.kind not in (b"fLaC", b"enca"):
                        continue
                    entry_header = _body(data, entry, 28)
                    version = int.from_bytes(entry_header[8:10], "big")
                    if version not in (0, 1, 2):
                        raise _bad()
                    child_start = entry.payload + {0: 28, 1: 44, 2: 64}[version]
                    entry_boxes = list(_boxes(data, child_start, entry.end))
                    dfla = _one(entry_boxes, b"dfLa", False)
                    if not dfla:
                        continue
                    if entry.kind == b"enca":
                        sinf = _one(entry_boxes, b"sinf", False)
                        frma = _one(_children(data, sinf), b"frma", False) if sinf else None
                        if frma and _body(data, frma, 4) != b"fLaC":
                            continue
                    if selection is not None:
                        raise _bad()
                    selection = (stbl, index, entry, entry_boxes, dfla)
            if selection is None:
                raise AudioExportError("该音源不是可提取的 FLAC 无损原流。")
            stbl, index, entry, entry_boxes, dfla = selection
            metadata = _metadata(_body(data, dfla))
            sizes = _sample_sizes(data, _one(stbl, b"stsz"))
            positions = _sample_positions(data, stbl, sizes, index, mdats)
            senc = _one(stbl, b"senc", False) or _one(moov_children, b"senc", False)
            encrypted = entry.kind == b"enca" or senc is not None
            if encrypted and (key is None or senc is None):
                raise AudioExportError("缺少当前账号返回的音频解密信息，请重新获取歌曲。")
            crypt_info = _encryption(data, entry_boxes, senc, sizes) if encrypted else None
            with target.open("wb") as out:
                out.write(metadata)
                for i, (offset, size) in enumerate(zip(positions, sizes)):
                    _cancelled(cancel)
                    sample = data[offset : offset + size]
                    if crypt_info:
                        iv, subsamples = crypt_info[i]
                        cipher = AES.new(key, AES.MODE_CTR, nonce=b"", initial_value=int.from_bytes(iv, "big"))
                        pos = 0
                        for clear, protected in subsamples:
                            out.write(sample[pos : pos + clear])
                            pos += clear
                            out.write(cipher.decrypt(sample[pos : pos + protected]))
                            pos += protected
                    else:
                        out.write(sample)


def _positive_float(value: object) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) and result > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _duration_slack(codec: str, sample_rate: int, *, original: bool = False) -> float:
    if codec == "flac":
        return max(0.01 if original else 0.002, 2 / sample_rate)
    # Lossy codecs carry encoder priming/padding. Container duration may include
    # these samples while a decoder honors skip-samples or an edit list.
    samples = {"aac": 3072, "mp3": 3456, "opus": 3840}[codec]
    return max(0.08, samples / sample_rate)


def _validate(
    path: Path,
    ffmpeg: str,
    ffprobe: str,
    expected_duration: float | None,
    cancel: threading.Event | None,
    expected_codec: str | None = "flac",
) -> dict:
    info = _probe(path, ffprobe, cancel)
    streams = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
    if len(streams) != 1 or streams[0].get("codec_name") not in _FORMATS:
        raise AudioExportError("导出文件的音频编码不受支持。")
    stream = streams[0]
    codec = stream["codec_name"]
    if expected_codec is not None and codec != expected_codec:
        raise AudioExportError("导出文件的实际编码与所选音质不一致。")
    progress = _run(
        [
            ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
            "-xerror", "-err_detect", "explode", "-i", str(path),
            "-map", "0:a:0", "-progress", "pipe:1", "-nostats", "-f", "null", "-",
        ],
        cancel,
    )
    decoded_times = re.findall(r"^out_time_us=(\d+)$", progress, re.MULTILINE)
    decoded_duration = max((int(value) / 1_000_000 for value in decoded_times), default=0)
    if decoded_duration <= 0:
        raise AudioExportError("音频没有可完整解码的内容，未保存文件。")
    declared = _positive_float(stream.get("duration")) or _positive_float(info.get("format", {}).get("duration"))
    sample_rate = int(stream.get("sample_rate") or 0)
    bits = int(stream.get("bits_per_raw_sample") or stream.get("bits_per_sample") or 0)
    if sample_rate <= 0 or (codec == "flac" and bits <= 0):
        raise AudioExportError("无法确认音频采样率或位深，未保存文件。")
    # A truncated file may still carry its original duration in STREAMINFO.
    if declared and abs(decoded_duration - declared) > _duration_slack(codec, sample_rate):
        raise AudioExportError("音频内容不完整，解码时长与文件信息不一致。")
    if expected_duration is not None:
        expected = _positive_float(expected_duration)
        if expected is None:
            raise AudioExportError("歌曲时长信息无效，请重新获取歌曲。")
        tolerance = max(0.25, min(2.0, expected * 0.01), _duration_slack(codec, sample_rate))
        if abs(decoded_duration - expected) > tolerance:
            raise AudioExportError("返回的音源可能是试听片段，或歌曲时长不匹配；未保存文件。")
    return {
        "codec": codec,
        "codec_name": codec,
        "sample_rate": sample_rate,
        "bits_per_sample": bits,
        "bit_depth": bits,
        "channels": int(stream.get("channels") or 0),
        "duration": decoded_duration,
        "bit_rate": int(
            _positive_float(stream.get("bit_rate"))
            or _positive_float(info.get("format", {}).get("bit_rate"))
            or (path.stat().st_size * 8 / decoded_duration)
        ),
        "is_lossless": codec == "flac",
        "extension": _FORMATS[codec][0],
        "size_bytes": path.stat().st_size,
        "verified": True,
    }


def export_audio(
    source: Path,
    target: Path,
    spade_a: str,
    ffmpeg: str,
    ffprobe: str,
    expected_duration: float | None = None,
    cancel: threading.Event | None = None,
    expected_codec: str | None = None,
) -> dict:
    """Save a supported original stream with no transcoding.

    FLAC/AAC/MP3/Opus require .flac/.m4a/.mp3/.opus target suffixes respectively.
    expected_codec, when supplied, must match the actual audio stream. Returns
    verified media metadata, including is_lossless (true only for FLAC).
    Source and preexisting target files are never modified. Safe Chinese errors
    use AudioExportError; cancellation uses its AudioCancelledError subclass.
    """
    source, target = Path(source), Path(target)
    temporary: Path | None = None
    created_identity: tuple[int, int] | None = None
    succeeded = False
    try:
        _cancelled(cancel)
        if not source.is_file():
            raise AudioExportError("找不到待处理的音频文件。")
        if target.exists() or target.is_symlink():
            raise AudioExportError("目标文件已存在，请更换文件名。")
        if expected_codec is not None:
            expected_codec = expected_codec.strip().lower()
            if expected_codec not in _FORMATS:
                raise AudioExportError("所选音频编码暂不支持；目前支持 FLAC、AAC、MP3 和 Opus。")
            if target.suffix.lower() != _FORMATS[expected_codec][0]:
                raise AudioExportError(f"目标文件扩展名应为 {_FORMATS[expected_codec][0]}，请更换文件名。")
        if expected_duration is not None and _positive_float(expected_duration) is None:
            raise AudioExportError("歌曲时长信息无效，请重新获取歌曲。")
        key = _decode_key(spade_a)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".soda-audio-", suffix=".audio", dir=target.parent)
        os.close(fd)
        temporary = Path(name)
        source_duration = None
        actual_codec = expected_codec
        try:
            remux_info = _remux(source, temporary, key, ffmpeg, ffprobe, cancel, expected_codec)
            source_duration = remux_info["source_duration"]
            actual_codec = remux_info["codec"]
        except AudioCancelledError:
            raise
        except AudioExportError:
            _cancelled(cancel)
            if expected_codec not in (None, "flac"):
                raise AudioExportError("该音源的编码或加密封装暂不支持，未保存文件。") from None
            _extract_mp4_flac(source, temporary, key, cancel)
            actual_codec = "flac"
        result = _validate(temporary, ffmpeg, ffprobe, expected_duration, cancel, actual_codec)
        if target.suffix.lower() != result["extension"]:
            raise AudioExportError(f"目标文件扩展名应为 {result['extension']}，请更换文件名。")
        # A remuxer may rewrite STREAMINFO after dropping a missing final frame.
        # Preserve the input's declared duration as an independent completeness
        # check, in addition to the expected song duration from the service.
        if source_duration and abs(result["duration"] - source_duration) > _duration_slack(actual_codec, result["sample_rate"], original=True):
            raise AudioExportError("音频内容不完整，解码时长与原始音源不一致。")
        _cancelled(cancel)
        # Exclusive creation prevents a concurrent export from replacing a file.
        # Only commit after the complete audio has already passed validation.
        with target.open("xb") as final_file:
            stat = os.fstat(final_file.fileno())
            created_identity = (stat.st_dev, stat.st_ino)
            with temporary.open("rb") as verified_file:
                while chunk := verified_file.read(1024 * 1024):
                    _cancelled(cancel)
                    final_file.write(chunk)
            final_file.flush()
            os.fsync(final_file.fileno())
        _cancelled(cancel)
        result["path"] = str(target)
        succeeded = True
        return result
    except AudioExportError:
        raise
    except FileExistsError:
        raise AudioExportError("目标文件已存在，请更换文件名。") from None
    except (OSError, ValueError, IndexError, KeyError, TypeError, struct.error):
        raise AudioExportError("音频文件处理失败，请检查文件、保存位置或重新获取音源。") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        if not succeeded and created_identity is not None:
            try:
                stat = target.stat(follow_symlinks=False)
                if (stat.st_dev, stat.st_ino) == created_identity:
                    target.unlink()
            except OSError:
                pass


def export_flac(
    source: Path,
    target: Path,
    spade_a: str,
    ffmpeg: str,
    ffprobe: str,
    expected_duration: float | None = None,
    cancel: threading.Event | None = None,
) -> dict:
    """Compatibility entry point that accepts only actual FLAC source audio."""
    return export_audio(
        source, target, spade_a, ffmpeg, ffprobe,
        expected_duration=expected_duration, cancel=cancel, expected_codec="flac",
    )
