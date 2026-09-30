"""Check one authorized FT article locally; emit only fixed diagnostic codes."""
from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import logging
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default includes untrusted arguments, possibly credentials.
        raise ValueError("invalid_arguments")


def preflight(cookie_file, article_url, *, root=ROOT):
    """Read private credentials directly; never change FT_COOKIE or write files."""
    # Import under the CLI's suppression boundary, including dependency errors.
    import requests
    from crawler.ft_impl.diagnostics import ArticleParseError, safe_diagnostic
    from crawler.ft_impl.parsing import canonical_url, parse_article
    from crawler.ft_impl.session import FTSession, StopCollection

    stage = "input"
    try:
        try:
            url = canonical_url(article_url)
        except (ValueError, TypeError):
            return safe_diagnostic("invalid_article_url", stage)
        stage = "credential"
        path = Path(cookie_file).resolve()
        if path.is_relative_to(Path(root).resolve()):
            return safe_diagnostic("private_file_must_be_external", stage)
        try:
            raw_cookie = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return safe_diagnostic("credential_unavailable", stage)
        with FTSession(raw_cookie=raw_cookie) as session:
            if not session.has_login_cookie():
                return safe_diagnostic("auth_required", stage)
            stage = "article_fetch"
            html = session.fetch(url)
            stage = "article_parse"
            parse_article(html, url)
        return safe_diagnostic("ok", "complete")
    except (StopCollection, ArticleParseError) as error:
        return safe_diagnostic(error.reason, stage)
    except requests.RequestException:
        return safe_diagnostic("article_request_failed", stage)
    except Exception:
        return safe_diagnostic("diagnostic_failed", stage)


def main(argv=None):
    parser = SafeArgumentParser(description=__doc__)
    parser.add_argument("--cookie-file", type=Path, required=True)
    parser.add_argument("--article-url", required=True)
    result = {"reason": "diagnostic_failed", "stage": "input", "action": "check_local_setup"}
    previous_logging = logging.root.manager.disable
    try:
        try:
            args = parser.parse_args(argv)
        except ValueError:
            result = {"reason": "invalid_arguments", "stage": "input", "action": "check_arguments"}
        else:
            # Existing logging handlers can retain the original stderr object.
            logging.disable(sys.maxsize)
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                result = preflight(args.cookie_file, args.article_url)
                from crawler.ft_impl.diagnostics import safe_diagnostic
                result = safe_diagnostic(result.get("reason"), result.get("stage"))
    except Exception:
        result = {"reason": "diagnostic_failed", "stage": "input", "action": "check_local_setup"}
    finally:
        logging.disable(previous_logging)
    print(json.dumps({"status": "ok" if result["reason"] == "ok" else "failed", **result}))
    return 0 if result["reason"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
