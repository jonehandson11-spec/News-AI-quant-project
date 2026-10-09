"""Synthetic-only regressions for the supplied trafilatura extraction method."""
from datetime import datetime, timezone
from html import escape
import json
import unittest
from unittest.mock import patch

import trafilatura

from crawler.ft_impl.parsing import parse_article
from crawler.ft_impl.session import StopCollection


URL = "https://www.ft.com/content/11111111-2222-3333-4444-555555555555"
PUBLISHED = "2026-09-27T09:00:00Z"
TITLE = "A fictional town considers a new transport plan"
PARAGRAPHS = (
    "The fictional town of Alderbridge published a transport proposal on Monday. "
    "The plan describes a new route between the railway station and the western "
    "business district, where residents currently change buses twice. Officials "
    "said the proposal would be discussed at three public meetings before a vote.",
    "Under the proposed timetable, the first service would leave the station at "
    "six in the morning and the final service would return after the evening "
    "market closes. The operating budget includes driver training and additional "
    "maintenance. The council has not yet selected a company to run the route.",
    "A separate assessment will compare the projected journey times with the "
    "existing bus network. Researchers will also count passengers at several "
    "interchanges during the autumn trial. Their findings will be published "
    "before councillors decide whether to continue the service the following year.",
)
PROSE = "\n\n".join(PARAGRAPHS)


def page(*, body=None, known_body=False, extra="", metadata=None):
    if metadata is None:
        metadata = {"@type": "NewsArticle", "url": URL, "datePublished": PUBLISHED}
    body = body if body is not None else "".join(f"<p>{p}</p>" for p in PARAGRAPHS)
    body_class = "article__content-body" if known_body else "new-layout-prose"
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        f"<title>{TITLE}</title>"
        '<script type="application/ld+json">' + json.dumps(metadata) + "</script>"
        f"</head><body><h1>{TITLE}</h1><main><article>"
        f'<div class="{body_class}">{body}</div></article>{extra}</main></body></html>'
    )


class TestFTLTCParser(unittest.TestCase):
    def test_unfamiliar_visible_layout_uses_real_trafilatura(self):
        with patch("trafilatura.extract", wraps=trafilatura.extract) as extract:
            result = parse_article(page(), URL)
        self.assertEqual(result["title"], TITLE)
        self.assertEqual(result["published"], datetime(2026, 9, 27, 9, tzinfo=timezone.utc))
        self.assertGreaterEqual(len(result["content"]), 600)
        for paragraph in PARAGRAPHS:
            self.assertIn(paragraph, result["content"])
        extract.assert_called_once()
        self.assertEqual(extract.call_args.kwargs["url"], URL)
        self.assertIs(extract.call_args.kwargs["include_comments"], False)
        self.assertIs(extract.call_args.kwargs["include_tables"], False)
        self.assertIs(extract.call_args.kwargs["favor_precision"], True)

    def test_one_long_visible_paragraph_is_accepted(self):
        result = parse_article(page(body="<p>" + " ".join(PARAGRAPHS) + "</p>"), URL)
        self.assertGreaterEqual(len(result["content"]), 600)
        self.assertIn(PARAGRAPHS[-1], result["content"])

    def test_known_body_excludes_other_article_recommendations(self):
        unrelated = "UNRELATED RECOMMENDATION: " + PROSE.replace("Alderbridge", "Briarport")
        extra = '<section><h2>Recommended story</h2><p>' + escape(unrelated) + "</p></section>"
        result = parse_article(page(known_body=True, extra=extra), URL)
        self.assertIn("Alderbridge", result["content"])
        self.assertNotIn("UNRELATED RECOMMENDATION", result["content"])
        self.assertNotIn("Briarport", result["content"])

    def test_hidden_payload_and_navigation_are_not_prose(self):
        hidden = (
            '<div hidden><p>HIDDEN ATTRIBUTE TEXT ' + PROSE + "</p></div>"
            '<div aria-hidden="true"><p>ARIA HIDDEN TEXT ' + PROSE + "</p></div>"
            '<div style="display: none"><p>DISPLAY HIDDEN TEXT ' + PROSE + "</p></div>"
            '<div style="visibility: hidden"><p>VISIBILITY HIDDEN TEXT ' + PROSE + "</p></div>"
            '<div style="content-visibility: hidden"><p>CONTENT HIDDEN TEXT ' + PROSE + "</p></div>"
            '<template><p>TEMPLATE PAYLOAD TEXT ' + PROSE + "</p></template>"
            '<noscript><p>NOSCRIPT PAYLOAD TEXT ' + PROSE + "</p></noscript>"
            '<script type="application/json">'
            + json.dumps({"articleBody": "APPLICATION PAYLOAD TEXT " + PROSE}) + "</script>"
            '<nav><p>NAVIGATION TEXT ' + PROSE + "</p></nav>"
        )
        metadata = {"@type": "NewsArticle", "url": URL, "datePublished": PUBLISHED,
                    "articleBody": "JSON LD PAYLOAD TEXT " + PROSE}
        result = parse_article(page(body=hidden + "".join(f"<p>{p}</p>" for p in PARAGRAPHS),
                                    metadata=metadata), URL)
        for marker in ("HIDDEN ATTRIBUTE TEXT", "ARIA HIDDEN TEXT", "DISPLAY HIDDEN TEXT",
                       "VISIBILITY HIDDEN TEXT", "CONTENT HIDDEN TEXT", "TEMPLATE PAYLOAD TEXT",
                       "NOSCRIPT PAYLOAD TEXT", "APPLICATION PAYLOAD TEXT", "NAVIGATION TEXT",
                       "JSON LD PAYLOAD TEXT"):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, result["content"])
        self.assertIn(PARAGRAPHS[0], result["content"])

    def test_hidden_or_json_text_cannot_supply_missing_visible_body(self):
        metadata = {"@type": "NewsArticle", "url": URL, "datePublished": PUBLISHED,
                    "articleBody": PROSE}
        for body in ("", '<div hidden><p>' + PROSE + "</p></div>",
                     '<div style="display: none"><p>' + PROSE + "</p></div>"):
            with self.subTest(body_kind=body[:30]), self.assertRaises(ValueError):
                parse_article(page(body=body, metadata=metadata), URL)

    def test_visible_barrier_rejects_even_a_long_teaser(self):
        for known_body in (False, True):
            with self.subTest(known_body=known_body), patch("trafilatura.extract") as extract:
                with self.assertRaises(StopCollection) as raised:
                    parse_article(page(known_body=known_body,
                                       extra='<div class="barrier">Subscribe for full access</div>'), URL)
                self.assertEqual(raised.exception.reason, "login_or_subscription_required")
                extract.assert_not_called()

    def test_ordinary_subscribe_navigation_does_not_block_article(self):
        extra = '<nav><a href="/subscribe">Subscribe</a><a href="/signin">Sign in</a></nav>'
        result = parse_article(page(extra=extra), URL)
        self.assertIn(PARAGRAPHS[0], result["content"])
        self.assertNotIn("Subscribe", result["content"])

    def test_unrelated_weak_paywall_phrase_in_aside_does_not_block_article(self):
        extra = '<aside><p>A newsletter brief with no waffle</p></aside>'
        result = parse_article(page(extra=extra), URL)
        self.assertIn(PARAGRAPHS[0], result["content"])
        self.assertNotIn("brief with no waffle", result["content"])

    def test_generic_subscription_page_is_auth_failure_without_metadata(self):
        html = '<html><body><h1>Subscribe to unlock</h1><p>Choose a subscription.</p></body></html>'
        with patch("trafilatura.extract") as extract:
            with self.assertRaises(StopCollection) as raised:
                parse_article(html, URL)
        self.assertEqual(raised.exception.reason, "login_or_subscription_required")
        extract.assert_not_called()

    def test_missing_or_modified_only_publication_metadata_is_rejected(self):
        for metadata in ({"@type": "NewsArticle", "url": URL},
                         {"@type": "NewsArticle", "url": URL, "dateModified": PUBLISHED}):
            with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                parse_article(page(metadata=metadata), URL)

    def test_bad_extraction_does_not_fall_back_to_available_paragraphs(self):
        for extracted in (None, "", "Only a short excerpt.", PROSE + "\ufffd"):
            with self.subTest(extracted_kind=type(extracted).__name__, length=len(extracted or "")), \
                    patch("trafilatura.extract", return_value=extracted), self.assertRaises(ValueError):
                parse_article(page(known_body=True), URL)

    def test_strong_paywall_phrases_in_extracted_text_are_rejected(self):
        for phrase in ("Subscribe to unlock", "Sign in to continue"):
            with self.subTest(phrase=phrase), patch("trafilatura.extract", return_value=PROSE + phrase):
                with self.assertRaises(StopCollection) as raised:
                    parse_article(page(), URL)
                self.assertEqual(raised.exception.reason, "login_or_subscription_required")


if __name__ == "__main__":
    unittest.main()
