# FT local collection: diagnosis and recovery

## September 30 finding

The inspected main revision was `9012a1cce50b0ec64929232c9456aecfeba8ec48`.
Its `data/source_reports/ft.json` records a local attempt at
2026-09-30 20:01:47 +08:00, 208 discovered candidates, 0 successful articles,
1 failure and 207 deferred candidates. Last successful collection was
2026-09-29 00:39:25 +08:00. These are historical observations, not a live
verification of the owner's current session.

The call path is:

1. `scripts/ft_sync.py:prepare` / `_collect_batch` invokes
   `scripts/ft_local.py:collect_batch`, which reads the external private Cookie
   file and calls `crawler.ft.collect`.
2. `crawler/ft.py:collect` discovers RSS URLs, then calls
   `crawler/ft_impl/session.py:FTSession.fetch` for an article.
3. `crawler/ft_impl/parsing.py:parse_article` raises the historical
   `login_or_subscription_required` at three sites (lines 112, 120 and 129 in
   that revision): no body plus a page-text phrase; an explicit subscription
   barrier; or article-body phrases.
4. `collect` catches `StopCollection`, stops the source and defers the remaining
   candidates. `ft_local._report` removes URLs and unapproved fields before the
   batch is merged; `scripts/dataset.py:refresh` copies the reason into health.

Thus the recorded reason originated in the **article parser's HTML barrier
checks**. It was not the session layer's missing-cookie, HTTP 401, HTTP 403 or
login-redirect code. RSS discovery and the presence of a session cookie do not
prove authenticated article access. A subscription/login response caused by
an expired session is plausible, as is a phrase-based false positive. The old
report has no branch evidence or response capture to choose among them. Do not
claim that the account's subscription has expired from this report alone.

## Credential-safe preflight

Run from the updated repository on the owner's local computer, using the same
private Cookie file as the scheduled collector and an article normally readable
in the owner's signed-in browser:

```sh
python scripts/ft_preflight.py --cookie-file "/path/outside/repository/ft-cookie.txt" --article-url "https://www.ft.com/content/ARTICLE-UUID"
```

Replace the path and `ARTICLE-UUID`. On Windows, use the private file's Windows
path. Pass only its path, never the Cookie itself. Do not paste credentials,
browser state, raw HTML, HTTP headers or verbose HTTP logs into chat or GitHub.

The command requests robots.txt and at most one article redirect chain using
the existing restricted FT session, pacing and robots rules. It performs no RSS
discovery, batch upload or database write; it does not persist response HTML or
renewed cookies. It outputs a single JSON object of fixed string codes, such as:

```json
{"status":"failed","reason":"subscription_barrier_detected","stage":"article_parse","action":"check_subscription_access"}
```

Exit code is 0 only when full visible article prose, title and publication
metadata pass parsing; otherwise 1. Success validates this article now, not all
articles, future session lifetime or eligibility for the collection time window.
`--help` prints usage. Invalid arguments and dependency exceptions cannot echo
their contents. The CLI suppresses dependency stdout/stderr and Python logging;
it does not enable HTTP tracing or print cookies, cookie hashes, headers, paths,
URLs, titles, body excerpts or exception messages. Only share the code summary.

## What to do with each result

| Reason | Evidence | Recovery action |
| --- | --- | --- |
| `credential_unavailable` | Private file could not be read | Check the local file path, permissions and encoding. |
| `auth_required` | No usable session cookie, or login destination | Sign in normally, confirm article access, and update the external private file. |
| `auth_expired` | HTTP 401, or login cookie no longer usable during collection | Refresh the local session through normal login. Cookie presence alone does not prove validity. |
| `login_prompt_detected` | Explicit barrier asks for sign-in | Check normal browser login; refresh the private file if stale. |
| `subscription_required` | HTTP 402 or subscription destination | Check access to this exact article in the browser and the account's subscription coverage. This does not establish why access is unavailable. |
| `subscription_barrier_detected` | Explicit HTML barrier mentions subscription | Check the same article in the browser. If readable there, update the stale local session and retry once. If unreadable, resolve entitlement with FT; stop collection meanwhile. |
| `paywall_page_text_detected` | No recognized body and a page-level paywall phrase | Verify browser access first. If the current session has access, investigate body selectors or phrase detection locally. |
| `paywall_body_text_detected` | Paywall phrase rule matched extracted body | Same browser/session check; then investigate a possible quoted phrase or parser mismatch. It remains blocked until understood. |
| `visible_article_body_missing`, `incomplete_article_body`, `article_title_missing`, publication-time codes | Article structure or required metadata did not validate | Check parsing logic after verifying normal browser access. Do not weaken full-text or publication-time checks to accept teasers. |
| `access_denied` | HTTP 403 | Check access restrictions; this is not proof of an expired cookie. Do not change identity, use proxies or bypass the denial. |
| `rate_limited` | HTTP 429 | Stop and wait before another manual attempt. |
| `robots_disallowed` | robots.txt denies the request | Stop; do not override the restriction. |
| `robots_unavailable`, `article_request_failed` | Robots/network check failed | Check connectivity/service availability. Do not repeatedly refresh credentials. |

For mixed subscribe/sign-in barriers, subscription-response evidence takes
precedence; it is not a verified subscription verdict. Bare phrase matches stay
explicitly ambiguous. Normal navigation Subscribe/Sign in links alongside a
valid article body are not explicit barriers. All barrier detections still stop
collection; no access controls have been bypassed or relaxed.

After preflight succeeds, resume the existing local collection/upload workflow
and verify the merged batch receipt and `data/source_reports/ft.json`. The
preflight intentionally does not rewrite health or claim a successful collection.
New collection failures retain the fixed reason plus `diagnostic.stage` and
`diagnostic.action` through batch filtering and merging. Historical
`login_or_subscription_required` batches remain accepted, but new parser
failures use the more specific codes. No historical report or article data is
rewritten by this change.

## Offline regression tests

```sh
python -m unittest discover -s tests -p 'test_ft*.py'
```

Tests use synthetic cookies and response fixtures. They cover redirect/status
separation, HTML evidence, parser failures, normal navigation, credential/URL
validation, no writes/environment changes, output redaction, and preservation
through local batch merging. No real FT requests or credentials are needed.
