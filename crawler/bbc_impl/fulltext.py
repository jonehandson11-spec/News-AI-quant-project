"""Public BBC article fetching, rate limiting and resumable on-disk cache."""
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .article import parse_article, ArticleExtractionError

USER_AGENT = 'BBCWorldRSSCollector/2.0'
BEIJING = timezone(timedelta(hours=8))
MAX_BYTES = 8_000_000


class AccessDenied(ValueError):
    """Stop the batch if access is denied or the server asks us to wait."""


def check_url(url):
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or '').lower()
    if parsed.scheme != 'https' or host not in ('bbc.co.uk', 'www.bbc.co.uk', 'bbc.com', 'www.bbc.com'):
        raise ValueError('Only public HTTPS BBC article URLs are supported')
    if parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError('Invalid BBC URL')


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


class ArticleClient:
    def __init__(self, root, timeout=20, delay=2, cache_hours=6):
        self.root = Path(root)
        self.timeout = timeout
        self.delay = max(1.0, delay)
        self.cache_seconds = cache_hours*3600
        self.last_request = None
        self.robots = {}

    def _pace(self):
        if self.last_request is not None:
            remaining = self.delay - (time.monotonic()-self.last_request)
            if remaining > 0:
                time.sleep(remaining)
        self.last_request = time.monotonic()

    def _open(self, url):
        self._pace()
        owner = self
        class Redirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                check_url(newurl)
                if not urllib.parse.urlsplit(newurl).path.endswith('/robots.txt'):
                    owner.check_robots(newurl)
                owner._pace()
                return super().redirect_request(req, fp, code, msg, headers, newurl)
        opener = urllib.request.build_opener(Redirect())
        return opener.open(urllib.request.Request(url, headers={
            'User-Agent': USER_AGENT, 'Accept': 'text/html,text/plain;q=0.8',
            'Accept-Encoding': 'identity'}), timeout=self.timeout)

    def check_robots(self, url):
        parts = urllib.parse.urlsplit(url)
        origin = f'{parts.scheme}://{parts.netloc}'
        if origin not in self.robots:
            parser = urllib.robotparser.RobotFileParser(origin+'/robots.txt')
            try:
                with self._open(origin+'/robots.txt') as response:
                    data = response.read(512_001)
                    content_type = response.headers.get('Content-Type', '').lower()
                if len(data) > 512_000:
                    raise AccessDenied('robots.txt too large to validate')
                if 'html' in content_type or data.lstrip().lower().startswith((b'<!doctype html', b'<html')):
                    raise AccessDenied('robots.txt returned an HTML page; cannot validate access rules')
                parser.parse(data.decode('utf-8', errors='replace').splitlines())
            except urllib.error.HTTPError as error:
                if error.code in (404, 410):
                    parser.parse(['User-agent: *', 'Allow: /'])
                else:
                    raise AccessDenied(f'Cannot validate robots.txt: HTTP {error.code}') from error
            except OSError as error:
                raise AccessDenied(f'Cannot validate robots.txt: {error}') from error
            self.robots[origin] = parser
        if not self.robots[origin].can_fetch(USER_AGENT, url):
            raise AccessDenied('robots.txt disallows this article')
        crawl_delay = self.robots[origin].crawl_delay(USER_AGENT)
        if crawl_delay:
            self.delay = max(self.delay, crawl_delay)

    def fetch(self, url):
        check_url(url)
        if any(segment in urllib.parse.urlsplit(url).path.split('/') for segment in ('live', 'videos', 'video', 'av')):
            raise ArticleExtractionError('live/video page is not a full text article')
        key = hashlib.sha256(url.encode()).hexdigest()
        directory = self.root/'article_pages'
        meta_path = directory/(key+'.json')
        html_path = directory/(key+'.html')
        if meta_path.exists() and html_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding='utf-8'))
                if meta['url'] == url and 0 <= time.time()-meta['timestamp'] < self.cache_seconds:
                    html = html_path.read_text(encoding='utf-8')
                    if hashlib.sha256(html.encode()).hexdigest() == meta['html_sha256']:
                        body = parse_article(html)
                        digest = hashlib.sha256(body.encode()).hexdigest()
                        if digest != meta.get('content_sha256'):
                            meta.update(content_sha256=digest, characters=len(body))
                            atomic_json(meta_path, meta)
                        return body, 'cached', meta['crawled_time']
            except (ValueError, KeyError, OSError):
                pass
        self.check_robots(url)
        for attempt in range(2):
            try:
                with self._open(url) as response:
                    final_url = response.geturl()
                    check_url(final_url)
                    content_type = response.headers.get('Content-Type', '').lower()
                    if 'text/html' not in content_type:
                        raise ValueError('Article response is not HTML')
                    data = response.read(MAX_BYTES+1)
                if len(data) > MAX_BYTES:
                    raise ValueError('Article page exceeds 8 MB')
                break
            except urllib.error.HTTPError as error:
                if error.code in (401, 403, 429):
                    retry = error.headers.get('Retry-After', '') if error.headers else ''
                    raise AccessDenied(f'HTTP {error.code}; Retry-After={retry}; stop this run') from error
                if error.code < 500 or attempt:
                    raise
                time.sleep(self.delay*2)
            except OSError:
                if attempt:
                    raise
                time.sleep(self.delay*2)
        html = data.decode('utf-8', errors='replace')
        body = parse_article(html)
        captured = datetime.now(BEIJING).strftime('%Y-%m-%d %H:%M:%S')
        directory.mkdir(parents=True, exist_ok=True)
        temporary = html_path.with_suffix('.html.tmp')
        temporary.write_text(html, encoding='utf-8')
        os.replace(temporary, html_path)
        atomic_json(meta_path, dict(url=url, final_url=final_url, timestamp=time.time(),
                                  crawled_time=captured, characters=len(body),
                                  html_sha256=hashlib.sha256(html.encode()).hexdigest(),
                                  content_sha256=hashlib.sha256(body.encode()).hexdigest()))
        return body, 'downloaded', captured

