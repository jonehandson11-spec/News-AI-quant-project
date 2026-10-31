import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "cloud_start", Path(__file__).resolve().parents[1] / "deploy" / "cloud_start.py")
cloud_start = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cloud_start)


def test_refuses_without_access_codes(monkeypatch, tmp_path):
    monkeypatch.setattr(cloud_start, "RUNTIME", tmp_path)
    monkeypatch.delenv("NEWS_PRICE_ACCESS_CODES_JSON", raising=False)
    with pytest.raises(SystemExit):
        cloud_start.write_access_files()


def test_refuses_plain_code(monkeypatch, tmp_path):
    monkeypatch.setattr(cloud_start, "RUNTIME", tmp_path)
    monkeypatch.setenv("NEWS_PRICE_ACCESS_CODES_JSON",
                       json.dumps({"version": 1, "codes": [{"name": "x", "code": "abcd"}]}))
    with pytest.raises(SystemExit):
        cloud_start.write_access_files()


def test_writes_hashes_and_secret(monkeypatch, tmp_path):
    monkeypatch.setattr(cloud_start, "RUNTIME", tmp_path)
    data = {"version": 1, "codes": [{"name": "team", "salt": "00", "hash": "11"}]}
    monkeypatch.setenv("NEWS_PRICE_ACCESS_CODES_JSON", json.dumps(data))
    monkeypatch.setenv("NEWS_PRICE_SESSION_SECRET", "ab" * 32)
    path = cloud_start.write_access_files()
    assert json.loads(path.read_text()) == data
    assert (tmp_path / "session_secret").read_text() == "ab" * 32
