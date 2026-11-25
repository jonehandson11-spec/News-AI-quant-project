"""云端启动脚本（Render 免费版）：下载价格库、从环境变量读访问码哈希，然后启动网页。

环境变量：
  NEWS_PRICE_ACCESS_CODES_JSON  访问码哈希文件的完整内容（只含 salt/hash，不含明文访问码）。必填。
  NEWS_PRICE_SESSION_SECRET     64 位十六进制会话密钥，休眠重启后登录不失效。可选。
  NEWS_PRICE_DB_URL             价格库下载地址（.sqlite3.gz），默认仓库 Release prices-latest。
  PORT                          平台指定的端口。
"""

import gzip
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_URL = ("https://github.com/jonehandson11-spec/News-AI-quant-project/"
                  "releases/download/prices-latest/prices.sqlite3.gz")
RUNTIME = Path(os.environ.get("NEWS_PRICE_RUNTIME_DIR", "/tmp/news_price_query"))


def fetch_db(url, target):
    if target.exists():
        return
    tmp = target.with_suffix(".download")
    with urllib.request.urlopen(url, timeout=120) as resp, open(tmp, "wb") as out:
        if url.endswith(".gz"):
            with gzip.GzipFile(fileobj=resp) as src:
                shutil.copyfileobj(src, out)
        else:
            shutil.copyfileobj(resp, out)
    tmp.replace(target)


def write_access_files():
    raw = os.environ.get("NEWS_PRICE_ACCESS_CODES_JSON", "").strip()
    if not raw:
        sys.exit("缺少 NEWS_PRICE_ACCESS_CODES_JSON，拒绝无访问码公开启动")
    data = json.loads(raw)
    if data.get("version") != 1 or not data.get("codes"):
        sys.exit("NEWS_PRICE_ACCESS_CODES_JSON 格式无效")
    if any("code" in record or "plain" in record for record in data["codes"]):
        sys.exit("NEWS_PRICE_ACCESS_CODES_JSON 只能放哈希，不能放明文访问码")
    codes = RUNTIME / "access_codes.json"
    codes.write_text(json.dumps(data), encoding="utf-8")
    secret = os.environ.get("NEWS_PRICE_SESSION_SECRET", "").strip()
    if secret:
        if len(bytes.fromhex(secret)) != 32:
            sys.exit("NEWS_PRICE_SESSION_SECRET 必须是 64 位十六进制")
        (RUNTIME / "session_secret").write_text(secret, encoding="ascii")
    return codes


def main():
    RUNTIME.mkdir(parents=True, exist_ok=True)
    codes = write_access_files()
    db = RUNTIME / "prices.sqlite3"
    fetch_db(os.environ.get("NEWS_PRICE_DB_URL", DEFAULT_DB_URL), db)
    cmd = [sys.executable, "-m", "web.server", "--provider", "local", "--db", str(db),
           "--access-codes", str(codes), "--host", "0.0.0.0", *sys.argv[1:]]
    os.chdir(ROOT)
    os.execv(sys.executable, cmd)


if __name__ == "__main__":
    main()
