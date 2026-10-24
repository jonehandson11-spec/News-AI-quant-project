"""访问码和会话管理。"""

import argparse
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import time
from datetime import datetime
from threading import Lock
from zoneinfo import ZoneInfo


ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"
HK = ZoneInfo("Asia/Hong_Kong")


def normalize_code(code):
    return code.strip().lower().replace(" ", "").replace("-", "").encode("utf-8")


def generate_code():
    return "-".join("".join(secrets.choice(ALPHABET) for _ in range(4)) for _ in range(3))


def _read_codes(path):
    with open(path, encoding="utf-8") as file:
        data = json.load(file)
    if data.get("version") != 1 or not isinstance(data.get("codes"), list):
        raise ValueError("访问码文件格式无效")
    return data["codes"]


class AccessControl:
    def __init__(self, codes_file, secret_file, log_file, *, now=time.time,
                 max_failures=5, window_seconds=900, global_max_failures=50):
        self.codes_file = Path(codes_file)
        self.secret_file = Path(secret_file)
        self.log_file = Path(log_file)
        self.now = now
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self.global_max_failures = global_max_failures
        self._failures = {}
        self._lock = Lock()
        self._load_secret()

    def _load_secret(self):
        try:
            self.secret = bytes.fromhex(self.secret_file.read_text(encoding="ascii").strip())
        except FileNotFoundError:
            self.secret_file.parent.mkdir(parents=True, exist_ok=True)
            self.secret = secrets.token_bytes(32)
            fd = os.open(self.secret_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="ascii") as file:
                file.write(self.secret.hex())
        if len(self.secret) != 32:
            raise ValueError("会话密钥必须为 32 字节")
        try:
            os.chmod(self.secret_file, 0o600)
        except OSError:
            pass

    def verify(self, code):
        candidate = normalize_code(code)
        matched = None
        for record in _read_codes(self.codes_file):
            digest = hashlib.scrypt(candidate, salt=bytes.fromhex(record["salt"]),
                                    n=2**14, r=8, p=1, dklen=32).hex()
            if hmac.compare_digest(digest, record["hash"]):
                matched = record["name"]
        return matched

    def issue(self, name):
        expiry = int(self.now()) + 30 * 24 * 60 * 60
        encoded = base64.urlsafe_b64encode(name.encode("utf-8")).rstrip(b"=").decode("ascii")
        payload = f"{encoded}.{expiry}"
        signature = hmac.new(self.secret, payload.encode("ascii"), hashlib.sha256).hexdigest()
        return f"{payload}.{signature}"

    def check(self, cookie_value):
        try:
            encoded, expiry_text, signature = cookie_value.split(".")
            payload = f"{encoded}.{expiry_text}"
            expected = hmac.new(self.secret, payload.encode("ascii"), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, signature) or int(expiry_text) <= self.now():
                return None
            padded = encoded + "=" * (-len(encoded) % 4)
            name = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
            if any(record.get("name") == name for record in _read_codes(self.codes_file)):
                return name
        except (ValueError, UnicodeError, KeyError, TypeError):
            return None
        return None

    def _recent_failures(self, client):
        cutoff = self.now() - self.window_seconds
        self._failures = {key: [stamp for stamp in stamps if stamp > cutoff]
                          for key, stamps in self._failures.items()}
        self._failures = {key: stamps for key, stamps in self._failures.items() if stamps}
        return self._failures.get(client, [])

    def allowed(self, client):
        with self._lock:
            current = self._recent_failures(client)
            total = sum(len(stamps) for stamps in self._failures.values())
            return len(current) < self.max_failures and total < self.global_max_failures

    def record_failure(self, client):
        with self._lock:
            self._recent_failures(client)
            self._failures.setdefault(client, []).append(self.now())

    def clear_failures(self, client):
        with self._lock:
            self._failures.pop(client, None)

    def log(self, event, name, client):
        if event not in {"login", "login_failed", "rate_limited"}:
            raise ValueError("日志事件无效")
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        record = {"time": datetime.fromtimestamp(self.now(), HK).isoformat(),
                  "event": event, "name": name, "client": client}
        with self._lock:
            with self.log_file.open("a", encoding="utf-8") as file:
                file.write(json.dumps(record, ensure_ascii=False) + "\n")


def _hash_code(code):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(normalize_code(code), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return {"salt": salt.hex(), "hash": digest.hex()}


def main(argv=None):
    parser = argparse.ArgumentParser(description="管理网页访问码")
    subparsers = parser.add_subparsers(dest="command", required=True)
    generate = subparsers.add_parser("generate", help="生成个人访问码")
    generate.add_argument("--names", required=True)
    generate.add_argument("--hash-file", required=True)
    generate.add_argument("--plain-file", required=True)
    listing = subparsers.add_parser("list", help="列出访问码标签")
    listing.add_argument("--hash-file", required=True)
    args = parser.parse_args(argv)

    if args.command == "list":
        for record in _read_codes(args.hash_file):
            print(record["name"])
        return

    names = list(dict.fromkeys(name.strip() for name in args.names.split(",") if name.strip()))
    if not names:
        parser.error("至少需要一个姓名标签")
    path = Path(args.hash_file)
    records = _read_codes(path) if path.exists() else []
    generated = []
    for name in names:
        code = generate_code()
        records = [record for record in records if record.get("name") != name]
        records.append({"name": name, **_hash_code(code)})
        generated.append((name, code))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "codes": records}, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    os.chmod(path, 0o600)
    plain = Path(args.plain_file)
    plain.parent.mkdir(parents=True, exist_ok=True)
    with plain.open("a", encoding="utf-8") as file:
        for name, code in generated:
            file.write(f"{name}\t{code}\n")
    os.chmod(plain, 0o600)


if __name__ == "__main__":
    main()
