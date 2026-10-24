import hashlib
import hmac
import http.client
import json
from pathlib import Path
import tempfile
from threading import Thread
import unittest
from urllib.parse import urlencode

from web.access import AccessControl, generate_code
from web.server import create_server


def save_codes(path, entries):
    path.write_text(json.dumps({"version": 1, "codes": entries}), encoding="utf-8")


def entry(name, code):
    salt = b"0123456789abcdef"
    normalized = code.strip().lower().replace(" ", "").replace("-", "").encode()
    return {"name": name, "salt": salt.hex(),
            "hash": hashlib.scrypt(normalized, salt=salt, n=2**14, r=8, p=1, dklen=32).hex()}


class AccessCodeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.codes = self.directory / "codes.json"
        self.secret = self.directory / "secret"
        self.log = self.directory / "access.jsonl"
        self.code = "abcd-2345-efgh"
        save_codes(self.codes, [entry("Alice", self.code)])
        self.access = AccessControl(self.codes, self.secret, self.log)
        self.server = None
        self.thread = None
        self.connection = None

    def _service(self):
        class Service:
            def catalog(self):
                return []
            def source_info(self):
                return {}
        return Service()

    def tearDown(self):
        if self.connection:
            self.connection.close()
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=1)
        self.temp.cleanup()

    def start_server(self, service, access=None):
        try:
            server = create_server(service, "127.0.0.1", 0, access=access)
        except PermissionError:
            self.skipTest("当前沙箱禁止创建本地监听 socket")
        self.server = server
        self.thread = Thread(target=server.serve_forever, daemon=True)
        self.thread.start()
        self.connection = http.client.HTTPConnection("127.0.0.1", server.server_port)

    def request(self, method, path, body=None, headers=None):
        if self.server is None:
            self.start_server(self._service(), self.access)
        self.connection.request(method, path, body, headers or {})
        response = self.connection.getresponse()
        payload = response.read()
        return response.status, response.headers, payload

    def test_disabled_server_still_serves_page_and_api(self):
        from web.service import QueryService
        class Provider:
            def bars(self, *args):
                return []
        service = QueryService(Provider)
        self.start_server(service)
        try:
            self.connection.request("GET", "/")
            self.assertEqual(self.connection.getresponse().status, 200)
            self.connection.request("GET", "/api/assets")
            self.assertEqual(self.connection.getresponse().status, 200)
        finally:
            self.connection.close()
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=1)
            self.connection = None
            self.server = None
            self.thread = None

    def test_unauthenticated_requests_are_redirected_or_rejected(self):
        self.assertEqual(self.request("GET", "/")[0], 303)
        self.assertEqual(self.request("GET", "/api/assets")[0], 401)
        self.assertEqual(self.request("POST", "/api/query", "{}")[0], 401)
        status, _, body = self.request("GET", "/login")
        self.assertEqual(status, 200)
        self.assertIn("访问码".encode(), body)
        self.assertEqual(self.request("HEAD", "/server.py")[0], 405)
        self.assertEqual(self.request("HEAD", "/")[0], 405)

    def test_wrong_code_is_logged_without_recording_secret(self):
        status, headers, _ = self.request("POST", "/login", urlencode({"code": "wrong-code"}),
                                          {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 401)
        self.assertIsNone(headers.get("Set-Cookie"))
        logged = self.log.read_text(encoding="utf-8")
        self.assertIn('"event": "login_failed"', logged)
        self.assertNotIn("wrong-code", logged)

    def test_valid_code_issues_session_and_can_access_api(self):
        status, headers, _ = self.request("POST", "/login", urlencode({"code": " AB CD 2345-EFGH "}),
                                          {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 303)
        cookie = headers.get("Set-Cookie")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        self.assertIn("SameSite=Lax", cookie)
        value = cookie.split(";", 1)[0]
        status, _, body = self.request("GET", "/api/assets", headers={"Cookie": value})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["assets"], [])
        logged = self.log.read_text(encoding="utf-8")
        self.assertIn('"name": "Alice"', logged)
        self.assertNotIn(self.code, logged)

    def test_tampered_and_expired_cookies_are_rejected(self):
        session = self.access.issue("Alice")
        self.assertIsNone(self.access.check(session + "x"))
        clock = [1000]
        access = AccessControl(self.codes, self.secret, self.log, now=lambda: clock[0])
        expired = access.issue("Alice")
        clock[0] += 30 * 24 * 60 * 60 + 1
        self.assertIsNone(access.check(expired))

    def test_fifth_failure_blocks_even_a_valid_code(self):
        for _ in range(5):
            self.request("POST", "/login", urlencode({"code": "bad"}),
                         {"Content-Type": "application/x-www-form-urlencoded"})
        status, _, _ = self.request("POST", "/login", urlencode({"code": self.code}),
                                    {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 429)

    def test_hash_file_and_generated_code_format(self):
        generated = generate_code()
        alphabet = set("23456789abcdefghjkmnpqrstuvwxyz")
        self.assertRegex(generated, r"^[23456789abcdefghjkmnpqrstuvwxyz]{4}(-[23456789abcdefghjkmnpqrstuvwxyz]{4}){2}$")
        self.assertTrue(set(generated.replace("-", "")) <= alphabet)
        self.assertNotIn(self.code, self.codes.read_text(encoding="utf-8"))
        self.assertEqual(self.access.verify(" A B C D 2345 EFGH "), "Alice")


if __name__ == "__main__":
    unittest.main()
