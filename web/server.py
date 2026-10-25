"""Serve the internal query page and its same-origin data API."""

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import time
from threading import Thread
from urllib.parse import parse_qs, urlsplit
from http.cookies import SimpleCookie
import webbrowser

from news_price_query.prices import FutuPriceProvider
from web.service import LocalNewsStore, QueryService


WEB_ROOT = Path(__file__).resolve().parent
REPO_NEWS = WEB_ROOT.parent / "data" / "news.csv"


def create_server(service: QueryService, host="127.0.0.1", port=8765, access=None):
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

        def end_headers(self):
            if access is not None:
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def _client(self):
            forwarded = self.headers.get("CF-Connecting-IP")
            return forwarded if forwarded is not None else self.client_address[0]

        def _session_name(self):
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
                morsel = cookie.get("pq_session")
                return access.check(morsel.value) if morsel else None
            except (ValueError, TypeError):
                return None

        def _login_page(self, status=200, error=None):
            message = (f'<p class="error">{error}</p>' if error else "")
            body = ("<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\">"
                    "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
                    "<title>新闻价格查询 · 登录</title><style>"
                    "body{margin:0;background:#f4f7f2;color:#26352a;font:16px sans-serif;"
                    "display:grid;min-height:100vh;place-items:center}.card{box-sizing:border-box;"
                    "width:min(92vw,380px);padding:32px;background:white;border:1px solid #dce7dc;"
                    "border-radius:14px;box-shadow:0 8px 28px #193d2512}h1{font-size:22px;margin:0 0 24px}"
                    "label{display:block;margin-bottom:8px}input{box-sizing:border-box;width:100%;"
                    "padding:12px;border:1px solid #b9cabb;border-radius:8px;font:inherit}"
                    "button{width:100%;margin-top:16px;padding:12px;background:#23834a;color:white;"
                    "border:0;border-radius:8px;font:inherit;cursor:pointer}.note{color:#68776b;"
                    "font-size:13px;margin:18px 0 0}.error{color:#b42318;margin:0 0 16px}"
                    "</style><main class=\"card\"><h1>新闻价格查询 · 登录</h1>" + message +
                    "<form method=\"post\" action=\"/login\"><label for=\"code\">访问码</label>"
                    "<input id=\"code\" name=\"code\" autocomplete=\"off\" required>"
                    "<button type=\"submit\">进入</button></form>"
                    "<p class=\"note\">访问码每人一个，请勿转发。</p></main></html>").encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _redirect(self, location, cookie=None):
            self.send_response(303)
            self.send_header("Location", location)
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _authorized_or_reject(self, path):
            if access is None or self._session_name() is not None:
                return True
            if path.startswith("/api/"):
                self._json(401, {"error": "未登录"})
            else:
                self._redirect("/login")
            return False

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
            if access is not None:
                if parsed.path == "/login":
                    self._login_page()
                    return
                if parsed.path == "/logout":
                    self._redirect("/login", "pq_session=; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=0")
                    return
                if not self._authorized_or_reject(parsed.path):
                    return
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

        def do_HEAD(self):
            if access is not None:
                self.send_response(405)
                self.send_header("Allow", "GET, POST")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            super().do_HEAD()

        def do_POST(self):
            path = urlsplit(self.path).path
            if access is not None and path == "/login":
                client = self._client()
                if not access.allowed(client):
                    access.log("rate_limited", None, client)
                    self._login_page(429, "尝试次数过多，请 15 分钟后再试")
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = 0
                if length <= 0 or length > 1024:
                    code = ""
                    if length > 1024:
                        self.close_connection = True
                else:
                    raw = self.rfile.read(length)
                    try:
                        code = parse_qs(raw.decode("utf-8"), keep_blank_values=True).get("code", [""])[0]
                    except UnicodeDecodeError:
                        code = ""
                name = access.verify(code)
                if name is not None:
                    access.clear_failures(client)
                    access.log("login", name, client)
                    self._redirect("/", f"pq_session={access.issue(name)}; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=2592000")
                    return
                access.record_failure(client)
                access.log("login_failed", None, client)
                time.sleep(0.3)
                self._login_page(401, "访问码不正确")
                return
            if access is not None and not self._authorized_or_reject(path):
                return
            if path != "/api/query":
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
    parser.add_argument("--mode", choices=("hourly", "daily", "minute"), default="hourly",
                        help="hourly：交易小时窗口（默认）；daily：交易日窗口；minute：旧版自然时间窗口")
    parser.add_argument("--news-csv", default=None, help="新闻 CSV；默认仓库 data/news.csv，不存在时读团队公开源")
    parser.add_argument("--futu-host", default="127.0.0.1")
    parser.add_argument("--futu-port", type=int, default=11111)
    parser.add_argument("--open-browser", action="store_true", help="启动后打开本地查询网页")
    parser.add_argument("--access-codes", default=os.environ.get("NEWS_PRICE_ACCESS_CODES"),
                        help="访问码哈希文件；也可设置 NEWS_PRICE_ACCESS_CODES")
    parser.add_argument("--session-secret", default=None, help="会话密钥文件；默认与访问码文件同目录")
    parser.add_argument("--access-log", default=None, help="访问日志文件；默认与访问码文件同目录")
    args = parser.parse_args()
    if args.provider == "local" and args.mode == "minute":
        parser.error("本地价格库没有分钟线，请用 --mode hourly/daily 或 --provider futu")
    try:
        service = build_service(args)
    except FileNotFoundError as exc:
        print(f"{exc}\n先运行：python scripts/update_prices.py --universe news_price_query/data/universe_example_hk.csv")
        raise SystemExit(2)
    access = None
    if args.access_codes:
        from web.access import AccessControl
        codes_path = Path(args.access_codes)
        secret_path = Path(args.session_secret) if args.session_secret else codes_path.with_name("session_secret")
        log_path = Path(args.access_log) if args.access_log else codes_path.with_name("access_log.jsonl")
        access = AccessControl(codes_path, secret_path, log_path)
    server = create_server(service, args.host, args.port, access=access)
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
