"""Read complete audio already cached by the official Windows SodaMusic client.

LMDB-JS uses an unreleased LMDB v2 format, incompatible with python-lmdb.
The bundled read-only Node bridge uses the matching LMDB-JS/Msgpackr codecs.
No cookies are accessed and no cached URL is requested.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

CODEC_EXTENSIONS = {"flac": ".flac", "aac": ".m4a", "mp3": ".mp3", "opus": ".opus"}
QUALITIES = {"best", "standard", "high", "lossless"}
MAX_MEDIA_BYTES = 512 * 1024 * 1024
BRIDGE_TIMEOUT = 20


class CacheError(Exception):
    """Safe user-facing cache failure; retryable allows normal playback to fill it."""

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


def _cancelled(cancel):
    if cancel is not None and cancel.is_set():
        raise CacheError("已取消下载。")


def _asset_roots() -> list[Path]:
    roots = []
    if getattr(sys, "frozen", False):
        bundle = getattr(sys, "_MEIPASS", None)
        if bundle:
            roots.append(Path(bundle))
        roots.append(Path(sys.executable).parent)
    roots.append(Path(__file__).resolve().parent)
    return list(dict.fromkeys(roots))


def _runtime_paths() -> tuple[Path, Path, Path]:
    roots = _asset_roots()
    bridge = next((root / "soda_cache_reader.cjs" for root in roots
                   if (root / "soda_cache_reader.cjs").is_file()), None)
    runtimes = [root / "tools" / folder for root in roots
                for folder in ("cache-runtime", "cache-reader")]
    runtime = next((folder for folder in runtimes
                    if (folder / "node_modules" / "lmdb" / "package.json").is_file()), None)
    node = next((folder / "node.exe" for folder in runtimes
                 if (folder / "node.exe").is_file()), None)
    if node is None:
        installed = shutil.which("node")
        node = Path(installed) if installed else None
    if bridge is None or runtime is None or node is None:
        raise CacheError("缺少本地缓存读取组件，请使用包含 tools 文件夹的完整工具包。")
    return node.resolve(), bridge.resolve(), runtime.resolve()


def _cache_directories(cache_dir=None) -> list[Path]:
    if cache_dir is not None:
        directories = [Path(cache_dir).expanduser().resolve()]
    else:
        directories = []
        for variable in ("APPDATA", "LOCALAPPDATA"):
            value = os.environ.get(variable)
            if value:
                candidate = (Path(value) / "SodaMusic" / "LunaCacheV2").resolve()
                if candidate not in directories:
                    directories.append(candidate)
    directories = [directory for directory in directories if (directory / "entries.db").is_file()]
    if not directories:
        raise CacheError("还没有找到汽水播放缓存。请先在汽水音乐中完整播放这首歌，再重试。", retryable=True)
    return directories


def _read_bridge(directory: Path, track_id: str | None, cancel=None) -> dict:
    _cancelled(cancel)
    node, bridge, runtime = _runtime_paths()
    request = json.dumps({
        "action": "find" if track_id is not None else "inspect",
        "cache_dir": str(directory),
        "track_id": track_id,
    }).encode("utf-8")
    environment = os.environ.copy()
    # Do not inherit Node preload hooks into the isolated cache reader.
    environment.pop("NODE_OPTIONS", None)
    environment.pop("NODE_PATH", None)
    environment["SODA_CACHE_RUNTIME"] = str(runtime)
    process = None
    try:
        process = subprocess.Popen(
            [str(node), "--max-old-space-size=256", str(bridge)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=environment, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        deadline = time.monotonic() + BRIDGE_TIMEOUT
        while True:
            _cancelled(cancel)
            if time.monotonic() >= deadline:
                raise CacheError("读取汽水缓存超时。请退出汽水后重试。", retryable=True)
            try:
                stdout, _stderr = process.communicate(input=request, timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                request = None
        if len(stdout) > 4 * 1024 * 1024:
            raise CacheError("缓存记录过大，当前版本无法安全读取。")
        try:
            result = json.loads(stdout)
        except (ValueError, UnicodeError):
            raise CacheError("缓存读取组件没有返回有效结果，请使用完整工具包。") from None
        if not isinstance(result, dict) or not result.get("ok"):
            code = result.get("error") if isinstance(result, dict) else None
            if code == "cache_missing":
                raise CacheError("播放缓存尚未生成，请在汽水中播放这首歌后重试。", retryable=True)
            if code == "runtime_missing":
                raise CacheError("缓存读取组件不完整，请使用包含 tools 文件夹的完整工具包。")
            if code in {"database_too_large", "too_many_matches", "output_too_large"}:
                raise CacheError("汽水缓存记录超出当前读取范围，已停止处理。")
            raise CacheError("无法读取当前汽水缓存格式，请更新工具后重试。")
        if process.returncode != 0 or not isinstance(result.get("matches"), list):
            raise CacheError("缓存读取组件运行异常，请使用完整工具包。")
        return result
    except OSError:
        raise CacheError("无法启动缓存读取组件，请检查工具文件是否完整。") from None
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.communicate()


def inspect_cache(cache_dir=None, cancel=None) -> dict:
    """Return counts only, without song names, account identifiers or URLs."""
    result = {"entries": 0, "with_metadata": 0, "full_marked": 0, "preview_marked": 0}
    for directory in _cache_directories(cache_dir):
        data = _read_bridge(directory, None, cancel)
        for key in result:
            value = data.get("counts", {}).get(key)
            if isinstance(value, int) and value >= 0:
                result[key] += value
    return result


def _positive_number(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def _preview_flag(value) -> bool:
    return value in (True, 1, "1", "true")


def _is_preview(part: dict) -> bool:
    return any(_preview_flag(part.get(key))
               for key in ("is_preview", "is_trial", "is_trial_play", "preview_only"))


def _validate_entry(entry: dict, track_id: str, directory: Path) -> dict:
    """Validate one metadata snapshot and its immutable UUID media file."""
    if not isinstance(entry, dict):
        raise CacheError("缓存记录格式异常。")
    info, detail = entry.get("info"), entry.get("detail")
    if not isinstance(info, dict) or not isinstance(detail, dict):
        raise CacheError("缓存记录格式异常。")
    if str(info.get("track_id")) != track_id:
        raise CacheError("缓存歌曲与请求不匹配，已停止。")
    if not detail.get("present"):
        raise CacheError("这首歌的缓存缺少音源信息，请在汽水中重新播放。", retryable=True)
    if info.get("is_preview") is not False or detail.get("video_model_type") in (2, "2") \
            or detail.get("player_model_type") in (2, "2"):
        raise CacheError("当前只有试听缓存，无法保存完整歌曲。请播放账号可完整收听的版本。", retryable=True)
    if any(_preview_flag(detail.get("model_" + field))
           for field in ("is_preview", "is_trial", "is_trial_play", "preview_only")):
        raise CacheError("当前缓存标记为试听，无法保存完整歌曲。", retryable=True)
    status = detail.get("status_code")
    if status is not None and str(status) not in ("0", "200"):
        raise CacheError("缓存音源记录未获服务端授权，已停止。")
    playable = detail.get("playable")
    if not isinstance(playable, dict) or str(playable.get("id")) != track_id \
            or playable.get("type") != "track":
        raise CacheError("缓存缺少可核对的单曲信息，已停止。")
    response_track = detail.get("response_track") or {}
    if not isinstance(response_track, dict):
        raise CacheError("缓存歌曲信息格式异常。")
    if response_track.get("id") is not None and str(response_track["id"]) != track_id:
        raise CacheError("缓存歌曲与音源信息不一致，已停止。")
    vid, cached_quality = playable.get("vid"), info.get("quality")
    if not isinstance(vid, str) or not vid or not isinstance(cached_quality, str) or not cached_quality:
        raise CacheError("缓存音源缺少完整资源标识，已停止。")
    resource_id = f"{vid}_F_{cached_quality}"
    if entry.get("key") != resource_id or entry.get("resource_id") != resource_id:
        raise CacheError("缓存未标记为该歌曲的完整音源。请在汽水中完整播放后重试。", retryable=True)
    candidates = entry.get("source_candidates")
    if not isinstance(candidates, list):
        raise CacheError("缓存音源格式异常。")
    verified = []
    for source in candidates:
        if not isinstance(source, dict):
            continue
        meta = source.get("video_meta")
        if not isinstance(meta, dict) or meta.get("quality") != cached_quality:
            continue
        if _is_preview(source) or _is_preview(meta):
            continue
        codec = str(meta.get("codec_type", source.get("codec_type", ""))).lower()
        if codec not in CODEC_EXTENSIONS:
            continue
        spade = info.get("spade")
        if not isinstance(spade, str) or spade != source.get("spade", ""):
            continue
        rate = _positive_number(meta.get("bitrate")) or _positive_number(info.get("bitrate")) or 0
        verified.append((codec, int(rate), spade))
    if not verified:
        raise CacheError("缓存音源信息无法核对，当前版本无法导出。")
    # A resource ID identifies one quality/source; conflicting codec data is unsafe.
    if len({(codec, spade) for codec, _, spade in verified}) != 1:
        raise CacheError("缓存中存在不一致的音频格式信息，已停止。")
    codec, rate, spade = max(verified, key=lambda item: item[1])
    duration = _positive_number(playable.get("duration_ms")) or _positive_number(playable.get("duration"))
    if duration is None:
        duration = _positive_number(response_track.get("duration_ms")) or _positive_number(response_track.get("duration"))
    if duration is None:
        raise CacheError("缓存未提供完整歌曲时长，无法排除试听片段。")
    size = entry.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_MEDIA_BYTES:
        raise CacheError("缓存音频大小无效，已停止。")
    chunk = entry.get("chunk_id")
    if not isinstance(chunk, str) or not re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", chunk
    ):
        raise CacheError("缓存文件标识格式异常，已停止。")
    path = directory / (chunk + ".bin")
    try:
        resolved = path.resolve()
        if resolved.parent != directory.resolve():
            raise CacheError("缓存文件位置异常，已停止。")
        with resolved.open("rb") as source_file:
            stat = os.fstat(source_file.fileno())
        if stat.st_size != size:
            raise CacheError("缓存音频尚不完整，请等汽水缓冲完成后重试。", retryable=True)
    except (FileNotFoundError, PermissionError):
        raise CacheError("缓存音频尚未完成或已被清理，请在汽水中重新播放。", retryable=True) from None
    except OSError:
        raise CacheError("缓存音频无法读取，请检查磁盘状态后重试。") from None
    artists = playable.get("artists") or []
    artist = "、".join(str(item.get("name", "")) for item in artists if isinstance(item, dict)) \
        if isinstance(artists, list) else ""
    album = playable.get("album") or {}
    return {
        "path": str(resolved), "codec": codec, "extension": CODEC_EXTENSIONS[codec],
        "duration": duration / 1000, "title": str(playable.get("name") or track_id),
        "artist": artist, "album": str(album.get("name", "")) if isinstance(album, dict) else "",
        "spade": spade, "bit_rate": rate, "is_lossless": codec == "flac",
        "cache_quality": cached_quality, "cache_size": size,
        "cache_mtime_ns": stat.st_mtime_ns, "resource_id": resource_id,
    }


def find_cached_track(track_id: str, quality="best", cache_dir=None, cancel=None) -> dict:
    """Select only a complete locally cached version of one requested track.

    Cache presence reflects an earlier official retrieval; it does not establish
    current membership status. URLs and credentials are never returned or used.
    """
    _cancelled(cancel)
    track_id = str(track_id)
    if not re.fullmatch(r"\d{10,25}", track_id):
        raise CacheError("歌曲 ID 格式不正确。")
    if quality not in QUALITIES:
        raise CacheError("请选择自动、标准、高音质或无损。")
    eligible, errors, matched = [], [], False
    for directory in _cache_directories(cache_dir):
        snapshot = _read_bridge(directory, track_id, cancel)
        for entry in snapshot["matches"]:
            _cancelled(cancel)
            matched = True
            try:
                eligible.append(_validate_entry(entry, track_id, directory))
            except CacheError as error:
                errors.append(error)
    if not eligible:
        if errors:
            raise next((error for error in errors if error.retryable), errors[0])
        message = "还没有这首歌的完整播放缓存。请先在汽水中播放，等待缓冲完成后重试。"
        raise CacheError(message, retryable=True)
    if quality == "lossless":
        eligible = [item for item in eligible if item["is_lossless"]]
    elif quality in ("standard", "high"):
        eligible = [item for item in eligible if not item["is_lossless"]]
    if not eligible:
        message = "还没有 FLAC 无损缓存。请在汽水中选择账号可用的无损音质并播放。" \
            if quality == "lossless" else "还没有普通音质缓存。请在汽水中选择标准或高音质并播放。"
        raise CacheError(message, retryable=True)
    if quality == "best":
        chosen = max(eligible, key=lambda item: (item["is_lossless"], item["bit_rate"]))
    elif quality == "standard":
        chosen = min(eligible, key=lambda item: item["bit_rate"] or float("inf"))
    else:
        chosen = max(eligible, key=lambda item: item["bit_rate"])
    return {**chosen, "quality": quality}
