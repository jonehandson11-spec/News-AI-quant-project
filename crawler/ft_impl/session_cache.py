"""Current-user Windows DPAPI cache for a verified FT session's rotated cookies.

Call save only after a complete authenticated article has been validated. The
original credential file is never read or changed here. Cache failures are
optional: restore/save return False without logging credentials or exceptions.
"""
import ctypes
from ctypes import wintypes
from http.cookiejar import Cookie
import hmac
import json
import os
from pathlib import Path
import tempfile
import time

from .session import COOKIE_KEYS


MAGIC = b"FTSC\x01"
MAX_PAYLOAD_BYTES = 128 * 1024
MAX_FILE_BYTES = 256 * 1024
MAX_COOKIES = 32
ALLOWED_DOMAINS = frozenset((".ft.com", "www.ft.com", "ft.com"))
LOGIN_KEYS = frozenset(("FTSession", "FTSession_s"))
COOKIE_FIELDS = frozenset((
    "name", "value", "domain", "path", "secure", "expires", "discard",
    "domain_specified", "domain_initial_dot", "path_specified", "http_only", "same_site",
))


def _is_windows():
    return os.name == "nt"


class _DataBlob(ctypes.Structure):
    _fields_ = (("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte)))


def _dpapi(data, *, decrypt):
    if not _is_windows() or not isinstance(data, bytes) or not 0 < len(data) <= MAX_FILE_BYTES:
        raise ValueError("session_cache_unavailable")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    function.argtypes = (ctypes.POINTER(_DataBlob), ctypes.c_void_p,
                         ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.c_void_p,
                         wintypes.DWORD, ctypes.POINTER(_DataBlob))
    function.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel32.LocalFree.restype = ctypes.c_void_p
    buffer = ctypes.create_string_buffer(data)
    incoming = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = _DataBlob()
    try:
        # UI_FORBIDDEN only: omit LOCAL_MACHINE so protection binds to this user.
        if not function(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
            raise OSError("session_cache_crypto_failed")
        if not outgoing.pbData or not 0 < outgoing.cbData <= MAX_FILE_BYTES:
            raise ValueError("session_cache_invalid_size")
        return ctypes.string_at(outgoing.pbData, outgoing.cbData)
    finally:
        if outgoing.pbData:
            kernel32.LocalFree(ctypes.cast(outgoing.pbData, ctypes.c_void_p))


def _protect(data):
    return _dpapi(data, decrypt=False)


def _unprotect(data):
    return _dpapi(data, decrypt=True)


def _text(value, maximum):
    return (isinstance(value, str) and 0 < len(value) <= maximum
            and all(32 <= ord(character) <= 126 for character in value))


def _valid_cookie(row):
    if not isinstance(row, dict) or set(row) != COOKIE_FIELDS:
        return False
    if not isinstance(row["name"], str) or row["name"] not in COOKIE_KEYS:
        return False
    if not _text(row["value"], 16_384) or ";" in row["value"]:
        return False
    if not isinstance(row["domain"], str) or row["domain"] not in ALLOWED_DOMAINS:
        return False
    if not _text(row["path"], 1024) or not row["path"].startswith("/") or ";" in row["path"]:
        return False
    flags = ("secure", "discard", "domain_specified", "domain_initial_dot", "path_specified", "http_only")
    if any(type(row[key]) is not bool for key in flags) or not row["secure"]:
        return False
    if (row["domain_initial_dot"] != row["domain"].startswith(".")
            or row["domain_initial_dot"] and not row["domain_specified"]):
        return False
    expires = row["expires"]
    if expires is not None and (type(expires) is not int or not 0 <= expires <= 253_402_300_799):
        return False
    same_site = row["same_site"]
    return same_site is None or isinstance(same_site, str) and same_site.lower() in ("strict", "lax", "none")


def _cookie_row(cookie):
    if cookie.version != 0 or cookie.port is not None:
        return None
    row = {name: getattr(cookie, name) for name in COOKIE_FIELDS - {"http_only", "same_site"}}
    row["http_only"] = cookie.has_nonstandard_attr("HttpOnly")
    row["same_site"] = cookie.get_nonstandard_attr("SameSite")
    return row if _valid_cookie(row) else None


def _cookie(row):
    rest = {"HttpOnly": None} if row["http_only"] else {}
    if row["same_site"] is not None:
        rest["SameSite"] = row["same_site"]
    return Cookie(version=0, name=row["name"], value=row["value"], port=None, port_specified=False,
                  domain=row["domain"], domain_specified=row["domain_specified"],
                  domain_initial_dot=row["domain_initial_dot"], path=row["path"],
                  path_specified=row["path_specified"], secure=True, expires=row["expires"],
                  discard=row["discard"], comment=None, comment_url=None, rest=rest, rfc2109=False)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("session_cache_duplicate_field")
        result[key] = value
    return result


class SessionCache:
    def __init__(self, path: Path, credential_fingerprint: str):
        self.path = Path(path)
        self.credential_fingerprint = credential_fingerprint

    def restore(self, session):
        """Replace allowed FT cookies only after fully validating a matching cache."""
        try:
            if not _is_windows() or not _text(self.credential_fingerprint, 256):
                return False
            with self.path.open("rb") as stream:
                encrypted = stream.read(MAX_FILE_BYTES + 1)
            if len(encrypted) > MAX_FILE_BYTES or not encrypted.startswith(MAGIC):
                return False
            raw = _unprotect(encrypted[len(MAGIC):])
            if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_PAYLOAD_BYTES:
                return False
            payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
            if not isinstance(payload, dict) or set(payload) != {"version", "source_fingerprint", "cookies"}:
                return False
            if type(payload["version"]) is not int or payload["version"] != 1:
                return False
            fingerprint = payload["source_fingerprint"]
            if not _text(fingerprint, 256) or not hmac.compare_digest(fingerprint, self.credential_fingerprint):
                return False
            rows = payload["cookies"]
            if not isinstance(rows, list) or not 0 < len(rows) <= MAX_COOKIES:
                return False
            if not all(_valid_cookie(row) for row in rows):
                return False
            identities = {(row["name"], row["domain"], row["path"]) for row in rows}
            if len(identities) != len(rows):
                return False
            now = time.time()
            live = [row for row in rows if row["expires"] is None or row["expires"] > now]
            if not any(row["name"] in LOGIN_KEYS for row in live):
                return False
            jar = session.cookies.copy()
            for cookie in list(jar):
                if cookie.name in COOKIE_KEYS and cookie.domain in ALLOWED_DOMAINS:
                    jar.clear(cookie.domain, cookie.path, cookie.name)
            for row in live:
                jar.set_cookie(_cookie(row))
            session.cookies = jar
            return True
        except Exception:
            return False

    def save(self, session):
        """Persist a verified jar as ciphertext; never fall back to plaintext."""
        temporary = None
        try:
            if not _is_windows() or not _text(self.credential_fingerprint, 256):
                return False
            now = time.time()
            rows = []
            for cookie in session.cookies:
                row = _cookie_row(cookie)
                if row is not None and (row["expires"] is None or row["expires"] > now):
                    rows.append(row)
            if not 0 < len(rows) <= MAX_COOKIES or not any(row["name"] in LOGIN_KEYS for row in rows):
                return False
            raw = json.dumps({"version": 1, "source_fingerprint": self.credential_fingerprint,
                              "cookies": rows}, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
            if len(raw) > MAX_PAYLOAD_BYTES:
                return False
            encrypted = _protect(raw)
            if not isinstance(encrypted, bytes) or not encrypted or encrypted == raw:
                return False
            contents = MAGIC + encrypted
            if len(contents) > MAX_FILE_BYTES:
                return False
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, filename = tempfile.mkstemp(prefix="." + self.path.name + ".", suffix=".tmp", dir=self.path.parent)
            temporary = Path(filename)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(contents)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            return True
        except Exception:
            return False
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def clear(self):
        """Remove this cache, for example when replacing source credentials."""
        try:
            self.path.unlink(missing_ok=True)
            return True
        except OSError:
            return False
