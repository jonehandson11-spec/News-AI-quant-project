"""云端启动脚本（Render 免费版）：下载价格库，然后启动网页（无需访问码）。

环境变量：
  NEWS_PRICE_DB_URL             价格库下载地址（.sqlite3.gz），默认仓库 Release prices-latest。
  PORT                          平台指定的端口。

启动时带 --yahoo-ondemand：价格库没有的标的会现场从 Yahoo 拉取。
若运行环境还没装 yfinance，会按 deploy/requirements.txt 自动安装。

以下变量若仍留在平台上，本脚本会忽略，不必删除：
  NEWS_PRICE_ACCESS_CODES_JSON、NEWS_PRICE_SESSION_SECRET、NEWS_PRICE_ACCESS_CODES。
本地启动（python -m web.server --access-codes）不受影响。
"""

import gzip
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


def server_command(argv=None):
    """Build web.server argv. Cloud mode never enables access codes."""
    os.environ.pop("NEWS_PRICE_ACCESS_CODES", None)
    db = RUNTIME / "prices.sqlite3"
    return [sys.executable, "-m", "web.server", "--provider", "local", "--db", str(db),
            "--host", "0.0.0.0", "--yahoo-ondemand",
            *(argv if argv is not None else sys.argv[1:])]


def ensure_deps():
    """Install yfinance if the host image only has tzdata (Render build command)."""
    try:
        import yfinance  # noqa: F401
        return
    except ImportError:
        pass
    req = ROOT / "deploy" / "requirements.txt"
    command = ([sys.executable, "-m", "pip", "install", "-r", str(req)]
               if req.exists() else [sys.executable, "-m", "pip", "install", "yfinance", "tzdata"])
    subprocess.check_call(command)


def main():
    ensure_deps()
    RUNTIME.mkdir(parents=True, exist_ok=True)
    db = RUNTIME / "prices.sqlite3"
    fetch_db(os.environ.get("NEWS_PRICE_DB_URL", DEFAULT_DB_URL), db)
    cmd = server_command()
    os.chdir(ROOT)
    os.execv(sys.executable, cmd)


if __name__ == "__main__":
    main()
