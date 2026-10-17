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
from web.service import QueryService


WEB_ROOT = Path(__file__).resolve().parent


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
                self._json(200, {"assets": service.catalog(), "price_source": "富途 OpenD"})
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="监听地址；团队内网可用 0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--futu-host", default="127.0.0.1")
    parser.add_argument("--futu-port", type=int, default=11111)
    parser.add_argument("--open-browser", action="store_true", help="启动后打开本地查询网页")
    args = parser.parse_args()
    service = QueryService(partial(FutuPriceProvider, host=args.futu_host, port=args.futu_port))
    server = create_server(service, args.host, args.port)
    print(f"内部查询网页：http://{args.host}:{server.server_port}/")
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
