"""Serve the internal query page and its same-origin data API."""

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs, urlsplit
import webbrowser

from news_price_query.prices import FutuPriceProvider
from web.service import LocalNewsStore, QueryService


WEB_ROOT = Path(__file__).resolve().parent
REPO_NEWS = WEB_ROOT.parent / "data" / "news.csv"


def create_server(service: QueryService, host="127.0.0.1", port=8765):
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

        def _json(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _error(self, exc):
            self._json(400 if isinstance(exc, (ValueError, KeyError, TypeError)) else 503,
                       {"error": str(exc)})

        def do_GET(self):
            parsed = urlsplit(self.path)
            if parsed.path == "/api/assets":
                info = service.source_info() if hasattr(service, "source_info") else {"price_source": "富途 OpenD"}
                self._json(200, {"assets": service.catalog(), **info})
                return
            if parsed.path == "/api/news":
                try:
                    params = parse_qs(parsed.query)
                    rows = service.news(params["asset"][0],
                                        params.get("start", [None])[0], params.get("end", [None])[0])
                    self._json(200, {"matches": rows,
                                     "stale": bool(getattr(service.news_source, "stale", False))})
                except (KeyError, IndexError, TypeError, ValueError, OSError, RuntimeError) as exc:
                    self._error(exc)
                return
            if parsed.path.startswith("/api/"):
                self._json(404, {"error": "未知 API 路径"})
                return
            if parsed.path not in {"/", "/index.html", "/styles.css", "/app.mjs", "/core.mjs"}:
                self.send_error(404)
                return
            super().do_GET()

        def do_POST(self):
            if urlsplit(self.path).path != "/api/query":
                self._json(404, {"error": "未知 API 路径"})
                return
            try:
                if self.headers.get_content_type() != "application/json":
                    raise ValueError("请求必须使用 JSON")
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536:
                    raise ValueError("请求大小无效")
                request = json.loads(self.rfile.read(length))
                if not isinstance(request, dict):
                    raise ValueError("请求内容必须是对象")
                result = service.query(request["asset"], request["published_at"],
                                       benchmarks=request.get("benchmarks", []))
                self._json(200, result)
            except (KeyError, TypeError, ValueError, OSError, RuntimeError) as exc:
                self._error(exc)

    return ThreadingHTTPServer((host, port), Handler)


def build_service(args) -> QueryService:
    if args.news_csv:
        news_source = LocalNewsStore(args.news_csv)
    elif REPO_NEWS.exists():
        news_source = LocalNewsStore(REPO_NEWS)
    else:
        news_source = None  # public team feed on GitHub
    if args.provider == "futu":
        return QueryService(partial(FutuPriceProvider, host=args.futu_host, port=args.futu_port),
                            news_source=news_source, mode=args.mode, price_source="富途 OpenD")
    from news_price_query.price_store import LocalDbPriceProvider, PriceStore

    store = PriceStore(args.db, create=False)
    provider = LocalDbPriceProvider(store=store)

    def version():
        return provider.data_version()

    run = version()
    label = "本地价格库 · yfinance 日线" + (f" · 更新于 {run['finished_at'][:16].replace('T', ' ')} HKT" if run else "")
    return QueryService(lambda: provider, news_source=news_source, mode=args.mode,
                        instruments=store.instruments, price_source=label, data_version=version)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="监听地址；默认只在本机。公开部署须先获 Ek 批准并加访问控制")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--provider", choices=("local", "futu"), default="local",
                        help="local：本地价格库（默认，免 OpenD）；futu：富途 OpenD")
    parser.add_argument("--db", default=None, help="本地价格库路径，默认 ~/.news_price_query/prices.sqlite3 或 NEWS_PRICE_DB")
    parser.add_argument("--mode", choices=("daily", "minute"), default="daily",
                        help="daily：交易日窗口（默认）；minute：旧版九个自然时间窗口，需要分钟线")
    parser.add_argument("--news-csv", default=None, help="新闻 CSV；默认仓库 data/news.csv，不存在时读团队公开源")
    parser.add_argument("--futu-host", default="127.0.0.1")
    parser.add_argument("--futu-port", type=int, default=11111)
    parser.add_argument("--open-browser", action="store_true", help="启动后打开本地查询网页")
    args = parser.parse_args()
    if args.provider == "local" and args.mode == "minute":
        parser.error("本地价格库只有日线，请用 --mode daily 或 --provider futu")
    try:
        service = build_service(args)
    except FileNotFoundError as exc:
        print(f"{exc}\n先运行：python scripts/update_prices.py --universe news_price_query/data/universe_example_hk.csv")
        raise SystemExit(2)
    server = create_server(service, args.host, args.port)
    print(f"内部查询网页：http://{args.host}:{server.server_port}/  （{service.price_source}，{args.mode}）")
    if args.open_browser:
        Thread(target=webbrowser.open,
               args=(f"http://127.0.0.1:{server.server_port}/",), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
