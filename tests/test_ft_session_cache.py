"""Synthetic cookies only; no requests or real credential files."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import requests

from crawler.ft_impl import session_cache as cache_module
from crawler.ft_impl.session_cache import SessionCache


FINGERPRINT = "a" * 64
NOW = 1_800_000_000
TOKEN = "synthetic-rotated-token"


def mock_protect(value):
    # Deliberately a test double, never used as production encryption.
    return b"test-cipher:" + bytes(byte ^ 0xA7 for byte in value)


def mock_unprotect(value):
    if not value.startswith(b"test-cipher:"):
        raise ValueError("synthetic decryption failure")
    return bytes(byte ^ 0xA7 for byte in value[len(b"test-cipher:"):])


def cookie(value=TOKEN, **kwargs):
    options = dict(name="FTSession_s", domain=".ft.com", path="/", secure=True,
                   expires=NOW + 3600, discard=False, rest={"HttpOnly": None, "SameSite": "Lax"})
    options.update(kwargs)
    return requests.cookies.create_cookie(value=value, **options)


class SessionCacheTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "ft_session.dpapi"
        self.cache = SessionCache(self.path, FINGERPRINT)
        for target, value in (("_is_windows", lambda: True), ("_protect", mock_protect),
                              ("_unprotect", mock_unprotect)):
            mock = patch.object(cache_module, target, side_effect=value)
            mock.start()
            self.addCleanup(mock.stop)
        clock = patch.object(cache_module.time, "time", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)

    def session(self, *cookies):
        session = requests.Session()
        self.addCleanup(session.close)
        for item in cookies:
            session.cookies.set_cookie(item)
        return session

    def payload(self):
        contents = self.path.read_bytes()
        return json.loads(mock_unprotect(contents[len(cache_module.MAGIC):]))

    def write_payload(self, value):
        self.path.write_bytes(cache_module.MAGIC + mock_protect(json.dumps(value).encode("utf-8")))

    def test_roundtrip_ciphertext_and_cookie_metadata(self):
        original = cookie(domain="www.ft.com", path="/content/")
        original.domain_specified = False
        original.path_specified = False
        self.assertTrue(self.cache.save(self.session(original)))
        contents = self.path.read_bytes()
        for secret in (TOKEN, FINGERPRINT, "FTSession_s", '"cookies"'):
            self.assertNotIn(secret.encode(), contents)
        target = self.session()
        self.assertTrue(self.cache.restore(target))
        restored = list(target.cookies)[0]
        for attribute in ("name", "value", "domain", "path", "secure", "expires", "discard",
                          "domain_specified", "domain_initial_dot", "path_specified"):
            self.assertEqual(getattr(restored, attribute), getattr(original, attribute))
        self.assertEqual(restored._rest, original._rest)

    def test_restore_replaces_old_allowed_cookies_across_domains(self):
        self.assertTrue(self.cache.save(self.session(cookie(domain="www.ft.com"))))
        target = self.session(cookie("original-file-token"), cookie("old-host-token", domain="ft.com"),
                              cookie("tracking", name="tracking"),
                              cookie("external", name="tracking", domain="example.org"))
        self.assertTrue(self.cache.restore(target))
        login = [item for item in target.cookies if item.name == "FTSession_s"]
        self.assertEqual([(item.domain, item.value) for item in login], [("www.ft.com", TOKEN)])
        self.assertEqual(len(target.cookies), 3)

    def test_save_filters_foreign_unknown_insecure_and_expired_cookies(self):
        session = self.session(cookie(), cookie("foreign", domain="ft.com.evil.example"),
                               cookie("unknown", name="tracking"),
                               cookie("insecure", name="FTConsent", secure=False),
                               cookie("expired", name="consentDate", expires=NOW - 1))
        self.assertTrue(self.cache.save(session))
        rows = self.payload()["cookies"]
        self.assertEqual([(row["name"], row["value"]) for row in rows], [("FTSession_s", TOKEN)])

    def test_expired_login_cache_does_not_replace_original_jar(self):
        self.assertTrue(self.cache.save(self.session(cookie(expires=NOW + 5))))
        target = self.session(cookie("original-file-token"))
        original_jar = target.cookies
        with patch.object(cache_module.time, "time", return_value=NOW + 10):
            self.assertFalse(self.cache.restore(target))
        self.assertIs(target.cookies, original_jar)

    def test_restore_discards_expired_cookie_and_preserves_session_cookie(self):
        self.assertTrue(self.cache.save(self.session(cookie(expires=None, discard=True),
                                                     cookie("consent", name="FTConsent", expires=NOW + 5))))
        target = self.session()
        with patch.object(cache_module.time, "time", return_value=NOW + 10):
            self.assertTrue(self.cache.restore(target))
        self.assertEqual([(item.name, item.expires, item.discard) for item in target.cookies],
                         [("FTSession_s", None, True)])

    def test_changed_source_fingerprint_does_not_restore(self):
        self.assertTrue(self.cache.save(self.session(cookie())))
        target = self.session(cookie("manual-update"))
        original_jar = target.cookies
        self.assertFalse(SessionCache(self.path, "b" * 64).restore(target))
        self.assertIs(target.cookies, original_jar)

    def test_missing_corrupt_and_undecryptable_cache_preserves_jar(self):
        target = self.session(cookie("original"))
        original_jar = target.cookies
        self.assertFalse(self.cache.restore(target))
        for contents in (b"plaintext-cookie", cache_module.MAGIC + b"corrupt",
                         cache_module.MAGIC + mock_protect(b"not json")):
            with self.subTest(contents=contents[:5]):
                self.path.write_bytes(contents)
                self.assertFalse(self.cache.restore(target))
                self.assertIs(target.cookies, original_jar)

    def test_invalid_cookie_fields_reject_entire_cache(self):
        self.assertTrue(self.cache.save(self.session(cookie())))
        valid = self.payload()
        changes = (("domain", "evil.example"), ("domain", ".www.ft.com"), ("domain", ""),
                   ("name", "tracking"), ("secure", False), ("expires", True), ("expires", "later"),
                   ("path", "relative"), ("path", "/\r\n"), ("value", "secret\r\nheader"),
                   ("discard", 1), ("domain_initial_dot", False), ("same_site", {}))
        for key, value in changes:
            with self.subTest(key=key, value=value):
                payload = json.loads(json.dumps(valid))
                payload["cookies"][0][key] = value
                self.write_payload(payload)
                target = self.session(cookie("original"))
                original_jar = target.cookies
                self.assertFalse(self.cache.restore(target))
                self.assertIs(target.cookies, original_jar)

    def test_invalid_payload_schema_and_duplicate_cookies_rejected(self):
        self.assertTrue(self.cache.save(self.session(cookie())))
        valid = self.payload()
        changes = (("version", True), ("version", 2), ("source_fingerprint", []), ("cookies", {}),
                   ("cookies", []), ("cookies", valid["cookies"] * 2), ("extra", "field"))
        for key, value in changes:
            with self.subTest(key=key):
                payload = dict(valid)
                payload[key] = value
                self.write_payload(payload)
                self.assertFalse(self.cache.restore(self.session()))

    def test_duplicate_json_fields_rejected(self):
        self.assertTrue(self.cache.save(self.session(cookie())))
        raw = json.dumps(self.payload()).replace('"version": 1', '"version": 2, "version": 1')
        self.path.write_bytes(cache_module.MAGIC + mock_protect(raw.encode()))
        self.assertFalse(self.cache.restore(self.session()))

    def test_no_valid_login_cookie_never_saves(self):
        for item in (cookie("consent", name="FTConsent"), cookie(expires=NOW),
                     cookie(domain="example.org"), cookie(secure=False)):
            self.assertFalse(self.cache.save(self.session(item)))
            self.assertFalse(self.path.exists())

    def test_size_and_cookie_count_limits(self):
        self.path.write_bytes(cache_module.MAGIC + b"x" * cache_module.MAX_FILE_BYTES)
        with patch.object(cache_module, "_unprotect") as unprotect:
            self.assertFalse(self.cache.restore(self.session()))
            unprotect.assert_not_called()
        self.path.unlink()
        self.assertFalse(self.cache.save(self.session(*(
            cookie(path=f"/path-{index}/") for index in range(cache_module.MAX_COOKIES + 1)))))
        with patch.object(cache_module, "MAX_PAYLOAD_BYTES", 50):
            self.assertFalse(self.cache.save(self.session(cookie())))
        self.assertFalse(self.path.exists())

    def test_non_windows_never_encrypts_or_writes(self):
        with patch.object(cache_module, "_is_windows", return_value=False), \
                patch.object(cache_module, "_protect") as protect:
            self.assertFalse(self.cache.save(self.session(cookie())))
            self.assertFalse(self.cache.restore(self.session()))
            protect.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_encryption_failure_preserves_old_file_without_plaintext_fallback(self):
        for result in (OSError(TOKEN), lambda value: value):
            with self.subTest(result=type(result).__name__):
                self.path.write_bytes(b"original-ciphertext")
                with patch.object(cache_module, "_protect", side_effect=result):
                    self.assertFalse(self.cache.save(self.session(cookie())))
                self.assertEqual(self.path.read_bytes(), b"original-ciphertext")
                self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_replace_failure_preserves_previous_cache_and_cleans_temporary_file(self):
        self.path.write_bytes(b"original-ciphertext")
        with patch.object(cache_module.os, "replace", side_effect=OSError("synthetic failure")):
            self.assertFalse(self.cache.save(self.session(cookie())))
        self.assertEqual(self.path.read_bytes(), b"original-ciphertext")
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_clear_removes_cache_and_is_idempotent(self):
        self.assertTrue(self.cache.save(self.session(cookie())))
        self.assertTrue(self.cache.clear())
        self.assertFalse(self.path.exists())
        self.assertTrue(self.cache.clear())


@unittest.skipUnless(os.name == "nt", "Windows DPAPI only")
class WindowsDPAPITests(unittest.TestCase):
    def test_current_user_dpapi_roundtrip_with_synthetic_session(self):
        with tempfile.TemporaryDirectory() as directory, requests.Session() as source, requests.Session() as target:
            path = Path(directory) / "synthetic.dpapi"
            cache = SessionCache(path, FINGERPRINT)
            source.cookies.set_cookie(cookie(expires=None))
            self.assertTrue(cache.save(source))
            self.assertNotIn(TOKEN.encode(), path.read_bytes())
            self.assertTrue(cache.restore(target))
            self.assertEqual(list(target.cookies)[0].value, TOKEN)


if __name__ == "__main__":
    unittest.main()
