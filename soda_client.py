"""Single-track, account-scoped audio downloader. No credentials are logged or saved."""
from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from contextlib import closing
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import requests

DEFAULT_AID = "386088"
API = "https://api.qishui.com/luna/pc/track_v2"
SHARE_HOSTS = {"qishui.douyin.com", "music.douyin.com", "www.qishui.com", "qishui.com", "luna-web.douyin.com"}
MAX_BYTES = 512 * 1024 * 1024
CODEC_EXTENSIONS = {"flac": ".flac", "aac": ".m4a", "mp3": ".mp3", "opus": ".opus"}
QUALITIES = {"best", "standard", "high", "lossless"}


class DownloadError(Exception):
    """Safe, user-visible message without credentials, URLs or response dumps."""


def _cancelled(cancel):
    if cancel is not None and cancel.is_set():
        raise DownloadError("已取消下载。")


def _dpapi(data: bytes) -> bytes:
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    crypt = ctypes.windll.crypt32.CryptUnprotectData
    crypt.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                      ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    crypt.restype = wintypes.BOOL
    free = ctypes.windll.kernel32.LocalFree
    free.argtypes = [ctypes.c_void_p]
    free.restype = ctypes.c_void_p
    if not crypt(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise DownloadError("Windows 无法读取这个账号的汽水登录，请使用本人的手动登录信息。")
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        free(result.data)


def read_local_session(profile: Path | None = None) -> str:
    """Read only SodaMusic's own session cookie under the current Windows user."""
    if sys.platform != "win32":
        raise DownloadError("自动读取本机登录仅支持 Windows。")
    profile = profile or Path(os.environ.get("APPDATA", "")) / "SodaMusic"
    database = profile / "Network" / "Cookies"
    if not database.is_file():
        raise DownloadError("没有找到本机汽水登录。请先打开汽水音乐并登录自己的账号。")
    try:
        # Use a read-only connection and release the handle before any network work.
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
            rows = db.execute(
                "SELECT host_key,value,encrypted_value,expires_utc FROM cookies "
                "WHERE name='sessionid' AND host_key IN ('.qishui.com','api.qishui.com','qishui.com') "
                "ORDER BY last_access_utc DESC"
            ).fetchall()
            version = int(db.execute("SELECT value FROM meta WHERE key='version'").fetchone()[0])
    except (sqlite3.Error, OSError):
        raise DownloadError("登录文件仍被汽水占用。请先复制歌曲链接，再右键右下角托盘中的汽水图标并选择“退出”，然后重试。仅关闭汽水窗口通常不会退出。也可在登录设置填写本人 sessionid。") from None
    if not rows:
        raise DownloadError("本机没有可读取的汽水登录。请登录后退出汽水重试，或使用手动登录信息。")
    key = None
    for host, clear, encrypted, expires in rows:
        try:
            if expires and int(expires) <= int((time.time() + 11644473600) * 1_000_000):
                continue
            if clear:
                return _validate_session(clear)
            encrypted = bytes(encrypted)
            if encrypted[:3] == b"v20":
                continue
            if encrypted[:3] in (b"v10", b"v11"):
                if key is None:
                    state = json.loads((profile / "Local State").read_text(encoding="utf-8"))
                    wrapped = base64.b64decode(state["os_crypt"]["encrypted_key"])
                    if not wrapped.startswith(b"DPAPI"):
                        continue
                    key = _dpapi(wrapped[5:])
                from Crypto.Cipher import AES
                cipher = AES.new(key, AES.MODE_GCM, nonce=encrypted[3:15])
                plain = cipher.decrypt_and_verify(encrypted[15:-16], encrypted[-16:])
            else:
                plain = _dpapi(encrypted)
            if version >= 24:
                digest = hashlib.sha256(host.encode()).digest()
                if not plain.startswith(digest):
                    continue
                plain = plain[32:]
            return _validate_session(plain.decode("utf-8"))
        except (ValueError, KeyError, UnicodeError, OSError, DownloadError):
            continue
    raise DownloadError("当前登录采用无法读取的保护方式。请在登录设置中填写本人的 sessionid；工具不会修改客户端。")


def _validate_session(session: str) -> str:
    session = session.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", session):
        raise DownloadError("sessionid 格式不正确，请填写本人账号的 sessionid 值，不要填写整段 Cookie。")
    return session


def _request(method, url, *, cancel=None, **kwargs):
    _cancelled(cancel)
    try:
        response = requests.request(method, url, timeout=(10, 30), allow_redirects=False, **kwargs)
    except requests.RequestException:
        raise DownloadError("网络连接失败或超时，请检查网络后重试。") from None
    if response.status_code in (401, 403):
        response.close()
        raise DownloadError("服务端拒绝访问：登录可能失效，或账号没有当前内容的权限。")
    if response.status_code == 429:
        response.close()
        raise DownloadError("请求过于频繁，请稍后再试。")
    if response.status_code >= 400:
        status = response.status_code
        response.close()
        raise DownloadError(f"服务暂时不可用（HTTP {status}），请稍后重试。")
    return response


def _share_url(url):
    p = urlparse(url)
    if p.scheme != "https" or p.hostname not in SHARE_HOSTS or p.port not in (None, 443) or p.username:
        raise DownloadError("请粘贴汽水音乐的 HTTPS 单曲分享链接，或歌曲 ID。")
    return url


def _limited_content(response, maximum=5 * 1024 * 1024):
    chunks, total = [], 0
    try:
        for chunk in response.iter_content(65536):
            total += len(chunk)
            if total > maximum:
                raise DownloadError("分享页面异常过大，请换用单曲分享链接。")
            chunks.append(chunk)
    except requests.RequestException:
        raise DownloadError("读取分享页面时网络中断，请重试。") from None
    return b"".join(chunks).decode("utf-8", errors="replace")


def parse_share(text: str, cancel=None) -> dict:
    text = text.strip()
    if re.fullmatch(r"\d{10,25}", text):
        return {"track_id": text}
    match = re.search(r"https://[^\s<>\"'，。]+", text)
    if not match:
        raise DownloadError("请从汽水音乐复制单曲分享链接后粘贴，也可以填写歌曲 ID。")
    url = match.group().rstrip("）)]】,;")
    for _ in range(6):
        _share_url(url)
        query = parse_qs(urlparse(url).query)
        for key in ("track_id", "trackId"):
            value = query.get(key, [""])[0]
            if re.fullmatch(r"\d{10,25}", value):
                return {"track_id": value}
        response = _request("GET", url, cancel=cancel, stream=True,
                            headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"})
        try:
            if response.status_code in (301, 302, 303, 307, 308):
                url = urljoin(url, response.headers.get("Location", ""))
                continue
            html = _limited_content(response)
        finally:
            response.close()
        decoder = json.JSONDecoder()
        for marker in re.finditer(r"(?:window\.)?_ROUTER_DATA\s*=", html):
            start = html.find("{", marker.end())
            if start < 0:
                continue
            try:
                data, _ = decoder.raw_decode(html[start:])
                page = data["loaderData"]["track_page"]
                tid = str(page["track_id"])
                if not re.fullmatch(r"\d{10,25}", tid):
                    continue
                info = page.get("audioWithLyricsOption", {})
                return {"track_id": tid, "title": info.get("trackName", ""),
                        "artist": info.get("artistName", "")}
            except (ValueError, KeyError, TypeError):
                continue
        raise DownloadError("无法识别此分享页。请使用单曲链接；歌单、专辑和短视频链接暂不支持。")
    raise DownloadError("分享链接跳转过多，请重新复制单曲链接。")


def _duration(track):
    for key in ("duration_ms", "duration"):
        value = track.get(key)
        try:
            n = float(value)
        except (ValueError, TypeError):
            continue
        if math.isfinite(n) and n > 0:
            # The PC track API uses milliseconds for track.duration.
            return n / 1000
    return None


def select_source(payload: dict, shared=None, quality="best") -> dict:
    """Choose only complete sources already returned for the caller's account."""
    if quality not in QUALITIES:
        raise DownloadError("请选择自动、标准、高音质或无损。")
    if not isinstance(payload, dict):
        raise DownloadError("音乐接口响应格式已变化。")
    code = payload.get("status_code", payload.get("code", 0))
    if str(code) not in ("0", "200", "None"):
        raise DownloadError("音乐接口未授权此请求。请检查登录是否有效及账号权限。")
    track, player = payload.get("track") or {}, payload.get("track_player") or {}
    if not isinstance(track, dict) or not isinstance(player, dict):
        raise DownloadError("音乐接口响应格式已变化。")
    def is_trial(part):
        if not isinstance(part, dict):
            return False
        # The installed PC client uses this enum for preview media models.
        if str(part.get("video_model_type")) == "2":
            return True
        for field in ("is_preview", "is_trial", "is_trial_play", "preview_only", "is试听"):
            if part.get(field) in (True, 1, "1", "true"):
                return True
        return False
    def reject_trial(part):
        if is_trial(part):
            raise DownloadError("账号当前只获准试听，无法下载完整歌曲。请选择账号可完整播放的歌曲，或登录有相应权限的账号。")
    for part in (payload, track, player):
        reject_trial(part)
    try:
        model = player.get("video_model") or {}
        model = json.loads(model) if isinstance(model, str) else model
        reject_trial(model)
        sources = model.get("video_list") or []
        sources = list(sources.values()) if isinstance(sources, dict) else sources
    except (ValueError, AttributeError):
        raise DownloadError("音源信息无法解析，可能需要适配新版接口。") from None
    choices, trial_sources = [], False
    for source in sources:
        if not isinstance(source, dict):
            continue
        meta = source.get("video_meta") or {}
        if not isinstance(meta, dict):
            continue
        codec = str(meta.get("codec_type", source.get("codec_type", ""))).lower()
        if codec not in CODEC_EXTENSIONS:
            continue
        if not source.get("main_url"):
            continue
        if is_trial(source) or is_trial(meta):
            trial_sources = True
            continue
        choices.append((source, codec))
    if quality == "lossless":
        choices = [item for item in choices if item[1] == "flac"]
    elif quality in ("standard", "high"):
        choices = [item for item in choices if item[1] != "flac"]
    if not choices:
        if trial_sources:
            raise DownloadError("当前返回的是试听音源，无法保存为完整歌曲。请选择账号可完整播放的歌曲。")
        if quality == "lossless":
            raise DownloadError("账号未返回 FLAC 无损音源。可改选“自动”或“标准”下载账号获准的其他音质；无损通常需要 SVIP。")
        if quality in ("standard", "high"):
            raise DownloadError("当前没有可用的普通音质音源，请改选“自动”尝试账号已获准的其他音源。")
        raise DownloadError("账号没有返回支持的完整音源。请确认歌曲可以完整播放；普通账号也需要该歌曲的播放权限。")
    def rate(item):
        try:
            return max(0, int((item[0].get("video_meta") or {}).get("bitrate", 0)))
        except (TypeError, ValueError):
            return 0
    if quality == "best":
        source, codec = max(choices, key=lambda item: (item[1] == "flac", rate(item)))
    elif quality == "standard":
        source, codec = min(choices, key=lambda item: rate(item) if rate(item) else float("inf"))
    else:
        source, codec = max(choices, key=rate)
    duration = _duration(track)
    if duration is None:
        raise DownloadError("接口未提供完整歌曲时长，无法排除试听片段，已停止下载。")
    shared = shared or {}
    artists = track.get("artists") or []
    artist = "、".join(str(a.get("name", "")) for a in artists if isinstance(a, dict))
    return {"source": source, "duration": duration, "codec": codec,
            "extension": CODEC_EXTENSIONS[codec], "quality": quality,
            "bit_rate": rate((source, codec)), "is_lossless": codec == "flac",
            "title": str(track.get("name") or shared.get("title") or shared.get("track_id") or "歌曲"),
            "artist": artist or shared.get("artist", ""),
            "album": str((track.get("album") or {}).get("name", ""))}


def select_lossless(payload: dict, shared=None) -> dict:
    """Compatibility wrapper for callers explicitly requesting FLAC."""
    return select_source(payload, shared, quality="lossless")


def get_track(shared, sessionid, aid=DEFAULT_AID, cancel=None, quality="best"):
    if not re.fullmatch(r"\d{1,10}", str(aid)):
        raise DownloadError("aid 必须是数字。电脑版默认 386088。")
    response = _request("POST", API, cancel=cancel, params={"aid": str(aid)},
                        headers={"Cookie": "sessionid=" + _validate_session(sessionid)},
                        json={"track_id": shared["track_id"], "media_type": "track",
                              "queue_type": "search_one_track", "scene_name": "search"})
    try:
        if response.status_code != 200:
            raise DownloadError("登录请求出现跳转，已停止。请更新本人的登录信息。")
        if hasattr(response, "content") and not response.content:
            raise DownloadError("服务端没有返回音源信息，可能需要重新登录或接口已变化；这不代表必须开通 VIP。")
        try:
            data = response.json()
        except ValueError:
            raise DownloadError("服务端未返回音乐信息，可能需要重新登录或适配接口。") from None
        return select_source(data, shared, quality)
    finally:
        response.close()


def safe_filename(title: str) -> str:
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip(" .")[:100]
    if not clean:
        clean = "歌曲"
    if clean.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        clean = "_" + clean
    return clean


def find_media_tool(name: str) -> str:
    home = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
    for path in (home / "tools" / f"{name}.exe", home / f"{name}.exe"):
        if path.is_file():
            return str(path)
    found = shutil.which(name)
    if found:
        return found
    raise DownloadError(f"未找到 {name}，请安装 FFmpeg 并将其加入 PATH，或放在工具旁的 tools 文件夹。")


def _download_media(url, target, progress, cancel):
    # Account cookies are deliberately never included in CDN requests.
    for _ in range(6):
        p = urlparse(url)
        if p.scheme != "https" or not p.hostname or p.username or p.port not in (None, 443):
            raise DownloadError("服务端音源地址格式异常，已停止。")
        response = _request("GET", url, cancel=cancel, stream=True)
        try:
            if response.status_code in (301, 302, 303, 307, 308):
                url = urljoin(url, response.headers.get("Location", ""))
                continue
            if response.status_code != 200:
                raise DownloadError("服务器未返回完整音频，已停止。")
            try:
                total = int(response.headers.get("Content-Length") or 0)
            except ValueError:
                total = 0
            if total > MAX_BYTES:
                raise DownloadError("单曲文件超过 512 MB，已停止。")
            count = 0
            with target.open("xb") as out:
                for chunk in response.iter_content(128 * 1024):
                    _cancelled(cancel)
                    count += len(chunk)
                    if count > MAX_BYTES:
                        raise DownloadError("单曲文件超过 512 MB，已停止。")
                    out.write(chunk)
                    progress(f"下载中 · {count / 1048576:.1f} MB", 10 + count / total * 65 if total else None)
            if not count or (total and count != total):
                raise DownloadError("音频传输不完整，请重试。")
            return
        except requests.RequestException:
            raise DownloadError("音频下载中断，请检查网络后重试。") from None
        finally:
            response.close()
    raise DownloadError("音源地址跳转过多，已停止。")


def download_track(text, output_dir, sessionid=None, aid=DEFAULT_AID, progress=None, cancel=None, quality="best"):
    progress = progress or (lambda message, percent=None: None)
    cancel = cancel or threading.Event()
    try:
        if quality not in QUALITIES:
            raise DownloadError("请选择自动、标准、高音质或无损。")
        ffmpeg, ffprobe = find_media_tool("ffmpeg"), find_media_tool("ffprobe")
        output = Path(output_dir).expanduser().resolve()
        output.mkdir(parents=True, exist_ok=True)
        progress("正在读取本人登录信息…" if sessionid is None else "正在检查登录信息…", 1)
        token = read_local_session() if sessionid is None else _validate_session(sessionid)
        progress("正在解析单曲链接…", 3)
        shared = parse_share(text, cancel)
        progress("正在检查账号可用音质…", 5)
        info = get_track(shared, token, aid, cancel, quality=quality)
        del token
        source = info["source"]
        codec = info["codec"]
        extension = CODEC_EXTENSIONS[codec]
        stem = safe_filename(info["title"] + (" - " + info["artist"] if info["artist"] else ""))
        with tempfile.TemporaryDirectory(prefix=".soda-", dir=output) as temp:
            raw, checked = Path(temp) / "source.bin", Path(temp) / ("checked" + extension)
            _download_media(source["main_url"], raw, progress, cancel)
            _cancelled(cancel)
            progress(f"正在保存 {codec.upper()} 原始音源并检查完整性…", 78)
            from soda_audio import export_audio
            result = export_audio(raw, checked, (source.get("encrypt_info") or {}).get("spade_a", ""),
                                  ffmpeg, ffprobe, expected_duration=info["duration"], cancel=cancel,
                                  expected_codec=codec)
            _cancelled(cancel)
            # On Windows rename fails rather than overwriting an existing destination.
            for number in range(10000):
                dest = output / (stem + (f" ({number})" if number else "") + extension)
                try:
                    if os.name == "nt":
                        checked.rename(dest)
                    else:
                        os.link(checked, dest)
                        checked.unlink()
                    break
                except FileExistsError:
                    continue
            else:
                raise DownloadError("同名文件过多，请更换保存位置。")
        progress(f"下载完成 · {codec.upper()} 完整性检查通过", 100)
        return {**result, "path": str(dest), "title": info["title"], "quality": quality,
                "bit_rate": result.get("bit_rate") or info["bit_rate"], "is_lossless": codec == "flac"}
    except DownloadError:
        raise
    except OSError:
        raise DownloadError("文件无法读写，请检查保存位置、磁盘空间和目录权限。") from None
    except Exception as error:
        # Only our audio module's safe error class is allowed through to the UI.
        if error.__class__.__module__ == "soda_audio":
            raise DownloadError(str(error)) from None
        raise DownloadError("处理未完成，接口或音频格式可能已变化。没有保存未经验证的文件。") from None


def open_track_in_player(track_id: str) -> None:
    """Use the official registered single-track link, with no account arguments."""
    if not re.fullmatch(r"\d{10,25}", str(track_id)):
        raise DownloadError("歌曲 ID 格式不正确。")
    if sys.platform != "win32":
        raise DownloadError("自动打开汽水目前仅支持 Windows。")
    try:
        os.startfile(f"luna://luna.com/playing?track_id={track_id}&media_type=track")
    except OSError:
        raise DownloadError("无法打开汽水音乐。请在汽水电脑版中播放这首歌，再回来下载。") from None


def _commit_verified(checked: Path, output: Path, stem: str, extension: str) -> Path:
    for number in range(10000):
        dest = output / (stem + (f" ({number})" if number else "") + extension)
        try:
            if os.name == "nt":
                checked.rename(dest)
            else:
                os.link(checked, dest)
                checked.unlink()
            return dest
        except FileExistsError:
            continue
    raise DownloadError("同名文件过多，请更换保存位置。")


def download_cached_track(
    text, output_dir, quality="best", progress=None, cancel=None,
    open_in_player=True, *, cache_dir=None, wait_seconds=120,
):
    """Export a complete original stream cached by the official Windows player.

    No credentials, private API requests, or cached signed URLs are used. On a
    cache miss, the official client may obtain the track through its normal
    playback route; the selected cached quality is always checked independently.
    """
    progress = progress or (lambda message, percent=None: None)
    cancel = cancel or threading.Event()
    try:
        from soda_cache import CacheError, find_cached_track
        from soda_audio import export_audio

        if quality not in QUALITIES:
            raise DownloadError("请选择自动、标准、高音质或无损。")
        _cancelled(cancel)
        ffmpeg, ffprobe = find_media_tool("ffmpeg"), find_media_tool("ffprobe")
        output = Path(output_dir).expanduser().resolve()
        output.mkdir(parents=True, exist_ok=True)
        progress("正在解析单曲链接…", 2)
        shared = parse_share(text, cancel)
        progress("正在检查本机完整音源…", 5)
        deadline = time.monotonic() + max(0, wait_seconds)
        opened = False
        while True:
            _cancelled(cancel)
            try:
                info = find_cached_track(shared["track_id"], quality=quality, cache_dir=cache_dir, cancel=cancel)
                break
            except CacheError as error:
                if not getattr(error, "retryable", False) or not open_in_player:
                    raise DownloadError(str(error)) from None
                if not opened:
                    progress("正在汽水中打开歌曲，请保持登录并允许歌曲加载…", None)
                    open_track_in_player(shared["track_id"])
                    opened = True
                if time.monotonic() >= deadline:
                    raise DownloadError(str(error)) from None
                hint = "请在汽水中选择无损音质" if quality == "lossless" else "请保持这首歌在汽水中正常播放"
                progress(f"等待完整音源 · {hint}。加载完成后会自动保存。", None)
                cancel.wait(min(3.0, max(0, deadline - time.monotonic())))

        _cancelled(cancel)
        codec = info["codec"]
        if codec not in CODEC_EXTENSIONS:
            raise DownloadError("这份缓存的音频编码暂不支持。")
        if quality == "lossless" and codec != "flac":
            raise DownloadError("本机尚未缓存这首歌的 FLAC 无损音源。请先在汽水中选择无损音质。")
        if quality in ("standard", "high") and codec == "flac":
            raise DownloadError("本机尚未缓存这首歌的有损音源，请选择自动或在汽水中切换音质。")
        extension = CODEC_EXTENSIONS[codec]
        stem = safe_filename(info["title"] + (" - " + info["artist"] if info["artist"] else ""))
        progress(f"已找到完整 {codec.upper()} 音源，正在读取…", 55)
        with tempfile.TemporaryDirectory(prefix=".soda-", dir=output) as temp:
            raw, checked = Path(temp) / "source.bin", Path(temp) / ("checked" + extension)
            # Hold a file handle and copy a bounded snapshot: the player's cache
            # cleaner may remove old files, but it must never yield a partial export.
            expected_size = int(info["cache_size"])
            if expected_size <= 0 or expected_size > MAX_BYTES:
                raise DownloadError("缓存文件大小异常，已停止保存。")
            count = 0
            with Path(info["path"]).open("rb") as source, raw.open("xb") as target:
                if os.fstat(source.fileno()).st_size != expected_size:
                    raise DownloadError("歌曲缓存正在更新，请稍后重试。")
                while chunk := source.read(1024 * 1024):
                    _cancelled(cancel)
                    count += len(chunk)
                    if count > expected_size:
                        raise DownloadError("歌曲缓存正在更新，请稍后重试。")
                    target.write(chunk)
            if count != expected_size:
                raise DownloadError("歌曲缓存不完整，请在汽水中重新播放后重试。")
            progress(f"正在保存 {codec.upper()} 原始音频并检查完整时长…", 78)
            result = export_audio(
                raw, checked, info.get("spade", ""), ffmpeg, ffprobe,
                expected_duration=info["duration"], cancel=cancel, expected_codec=codec,
            )
            _cancelled(cancel)
            dest = _commit_verified(checked, output, stem, extension)
        progress(f"保存完成 · {codec.upper()} 完整性检查通过", 100)
        return {**result, "path": str(dest), "title": info["title"], "quality": quality,
                "source": "official_cache", "is_lossless": codec == "flac"}
    except DownloadError:
        raise
    except OSError:
        raise DownloadError("文件无法读写，请检查保存位置，或让汽水重新加载歌曲后重试。") from None
    except Exception as error:
        if error.__class__.__module__ in ("soda_audio", "soda_cache"):
            raise DownloadError(str(error)) from None
        raise DownloadError("本机音源格式暂不支持，未保存未经验证的文件。") from None
