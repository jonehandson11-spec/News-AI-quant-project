import importlib.util
import json
import os
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "cloud_start", Path(__file__).resolve().parents[1] / "deploy" / "cloud_start.py")
cloud_start = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cloud_start)


def test_starts_without_access_codes_env(monkeypatch, tmp_path):
    monkeypatch.setattr(cloud_start, "RUNTIME", tmp_path)
    monkeypatch.delenv("NEWS_PRICE_ACCESS_CODES_JSON", raising=False)
    monkeypatch.delenv("NEWS_PRICE_ACCESS_CODES", raising=False)
    cmd = cloud_start.server_command([])
    assert "--access-codes" not in cmd
    assert str(tmp_path / "prices.sqlite3") in cmd


def test_ignores_access_codes_env_even_if_set(monkeypatch, tmp_path):
    monkeypatch.setattr(cloud_start, "RUNTIME", tmp_path)
    monkeypatch.setenv("NEWS_PRICE_ACCESS_CODES_JSON",
                       json.dumps({"version": 1, "codes": [{"name": "x", "salt": "00", "hash": "11"}]}))
    monkeypatch.setenv("NEWS_PRICE_ACCESS_CODES", str(tmp_path / "leftover.json"))
    monkeypatch.setenv("NEWS_PRICE_SESSION_SECRET", "ab" * 32)
    cmd = cloud_start.server_command([])
    assert "--access-codes" not in cmd
    assert "NEWS_PRICE_ACCESS_CODES" not in os.environ
    assert not (tmp_path / "access_codes.json").exists()
    assert not (tmp_path / "session_secret").exists()


def test_cloud_command_enables_yahoo_ondemand(monkeypatch, tmp_path):
    monkeypatch.setattr(cloud_start, "RUNTIME", tmp_path)
    cmd = cloud_start.server_command([])
    assert "--yahoo-ondemand" in cmd
