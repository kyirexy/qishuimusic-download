"""Synthetic Windows-only credential fixture; never reads an actual account."""
import base64
import ctypes
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from ctypes import wintypes
from pathlib import Path

from Crypto.Cipher import AES
from soda_client import DownloadError, read_local_session


def protect(data):
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    function = ctypes.windll.crypt32.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_wchar_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise RuntimeError("Synthetic fixture encryption failed")
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        free = ctypes.windll.kernel32.LocalFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = ctypes.c_void_p
        free(result.data)


@unittest.skipUnless(sys.platform == "win32", "Windows DPAPI")
class LocalAuthTests(unittest.TestCase):
    def test_encrypted_fixture_and_database_handle_release(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = Path(temp)
            (profile / "Network").mkdir()
            key, nonce, host, token = os.urandom(32), os.urandom(12), ".qishui.com", "a" * 32
            cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
            data, tag = cipher.encrypt_and_digest(hashlib.sha256(host.encode()).digest() + token.encode())
            (profile / "Local State").write_text(json.dumps({
                "os_crypt": {"encrypted_key": base64.b64encode(b"DPAPI" + protect(key)).decode()},
            }))
            database = profile / "Network" / "Cookies"
            with closing(sqlite3.connect(database)) as db:
                db.execute("CREATE TABLE cookies(host_key TEXT,name TEXT,value TEXT,encrypted_value BLOB,expires_utc INTEGER,last_access_utc INTEGER)")
                db.execute("CREATE TABLE meta(key TEXT,value TEXT)")
                db.execute("INSERT INTO meta VALUES (?,?)", ("version", "24"))
                db.execute("INSERT INTO cookies VALUES (?,?,?,?,?,?)",
                           (host, "sessionid", "", b"v10" + nonce + data + tag, 0, 1))
                db.commit()
            self.assertEqual(read_local_session(profile), token)
            database.unlink()  # Fails on Windows if the reader leaks its handle.

    def test_missing_profile_has_clear_error(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(DownloadError):
                read_local_session(Path(temp))
