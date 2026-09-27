"""Conservative extraction of visible BBC article prose (standard library only).

Only known article containers are accepted. This is intentionally fail-closed:
unsupported pages are reported for retry, never returned as a full article.
No attempt is made to recover content hidden behind login or subscription gates.
"""
import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urlsplit


class ArticleExtractionError(ValueError):
    """The response does not contain a confidently identified complete article."""


@dataclass
class _Node:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)
    closed: bool = False


_VOID = frozenset('area base br col embed hr img input link meta param source track wbr'.split())
_SKIP_TAGS = frozenset('aside nav footer header figure figcaption script style noscript template button form svg'.split())
_FURNITURE = re.compile(
    r'(?:related|recommend|promo|advert|social|share|newsletter|byline|contributor|timestamp|metadata|'
    r'caption|topic-list|topstories|mostread|elsewhere|features|carousel|links-block)', re.I)
_GATE = re.compile(r'(?:paywall|subscription-wall|registration-wall|sign-in-wall|consent-wall|content-gate)', re.I)
_GATE_TEXT = re.compile(r'(?:subscribe|sign in|register) to (?:continue reading|read (?:the |this )?(?:full |rest of the )?article)', re.I)


class _Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node('document', closed=True)
        self.stack = [self.root]
        self.nodes = 0

    def handle_starttag(self, tag, attrs):
        if self.nodes > 200_000 or len(self.stack) > 250:
            raise ArticleExtractionError('HTML structure exceeds safe parsing limits')
        # Common omitted </p> markup is legal HTML, but HTMLParser does not repair it.
        if tag == 'p' and self.stack[-1].tag == 'p':
            self.stack.pop().closed = True
        node = _Node(tag, dict(attrs), closed=tag in _VOID)
        self.stack[-1].children.append(node)
        self.nodes += 1
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                self.stack[i].closed = True
                del self.stack[i:]
                return

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def _walk(node):
    yield node
    for child in node.children:
        if isinstance(child, _Node):
            yield from _walk(child)


def _identity(node):
    return ' '.join(str(node.attrs.get(name, '') or '')
                    for name in ('class', 'id', 'data-component', 'data-testid', 'data-block'))


def _hidden(node):
    style = re.sub(r'\s+', '', node.attrs.get('style', '') or '').lower()
    return ('hidden' in node.attrs or node.attrs.get('aria-hidden') == 'true'
            or 'display:none' in style or 'visibility:hidden' in style)


def _skip(node):
    return node.tag in _SKIP_TAGS or _hidden(node) or bool(_FURNITURE.search(_identity(node)))


def _text(node):
    parts = []
    for child in node.children:
        if isinstance(child, str):
            parts.append(child)
        elif child.tag == 'br':
            parts.append(' ')
        elif not _skip(child):
            parts.append(_text(child))
    return ''.join(parts)


def _legacy_body(node):
    names = _identity(node).lower()
    return (node.attrs.get('itemprop') == 'articleBody'
            or 'story-body__inner' in names or '-storybody' in names
            or node.attrs.get('data-component') == 'article-body')


def _body_block(node):
    return (node.attrs.get('data-component') in ('text-block', 'subheadline-block', 'list-block')
            or node.attrs.get('data-testid') in ('rich-text', 'subheadline')
            or '-RichTextContainer' in (node.attrs.get('class') or ''))


def _structured_articles(value):
    if isinstance(value, list):
        for item in value:
            yield from _structured_articles(item)
    elif isinstance(value, dict):
        kinds = value.get('@type', [])
        if isinstance(kinds, str):
            kinds = [kinds]
        if not isinstance(kinds, list):
            kinds = []
        if any(kind in ('NewsArticle', 'Article', 'ReportageNewsArticle') for kind in kinds):
            yield value
        # Only inspect top-level graphs, not recommendations or embedded media.
        if '@graph' in value:
            yield from _structured_articles(value['@graph'])


def _declared_gated(item):
    if isinstance(item, list):
        return any(_declared_gated(part) for part in item)
    if isinstance(item, dict):
        return (item.get('isAccessibleForFree') in (False, 'false', 'False')
                or _declared_gated(item.get('hasPart')))
    return False


def _validate_page(nodes):
    for node in nodes:
        if node.tag == 'link' and 'canonical' in (node.attrs.get('rel') or '').split():
            path = urlsplit(node.attrs.get('href') or '').path.lower()
            if re.search(r'/(?:live|videos?|av)/', path):
                raise ArticleExtractionError('live/video page is not a full text article')
        if node.tag == 'script' and node.attrs.get('type') == 'application/ld+json':
            try:
                metadata = json.loads(''.join(x for x in node.children if isinstance(x, str)))
            except (ValueError, TypeError):
                continue
            for item in _structured_articles(metadata):
                if _declared_gated(item):
                    raise ArticleExtractionError('gated article: subscription or login required')
        if not _hidden(node) and _GATE.search(_identity(node)):
            raise ArticleExtractionError('gated article: subscription, consent or login wall')


def _collect(node, active=False):
    if _skip(node):
        return []
    active = active or _legacy_body(node) or _body_block(node)
    if active and node.tag in ('p', 'h2', 'h3', 'h4', 'li'):
        text = re.sub(r'\s+', ' ', _text(node)).strip()
        return [(node.tag, text)] if text else []
    parts = []
    for child in node.children:
        if isinstance(child, _Node):
            parts.extend(_collect(child, active))
    return parts


def parse_article(html: str) -> str:
    """Return visible article paragraphs separated by blank lines, or raise.

    Successful parsing is structural validation, not a guarantee that a publisher
    did not silently truncate a page. At least two prose blocks and 300 characters
    are required. JSON-LD articleBody is deliberately not used: it can contain
    hidden content and cannot establish what the HTTP response makes readable.
    """
    if not isinstance(html, str) or not html.strip() or len(html) > 20_000_000:
        raise ArticleExtractionError('missing or oversized HTML document')
    tree = _Tree()
    try:
        tree.feed(html)
        tree.close()
    except (ValueError, RecursionError) as exc:
        raise ArticleExtractionError('malformed article HTML') from exc
    nodes = list(_walk(tree.root))
    _validate_page(nodes)
    roots = [n for n in nodes if n.tag == 'article']
    if not roots:
        roots = [n for n in nodes if _legacy_body(n)]
    if len(roots) != 1:
        raise ArticleExtractionError('expected one unambiguous article body')
    root = roots[0]
    if not root.closed:
        raise ArticleExtractionError('truncated article HTML: missing closing body tag')
    if _GATE_TEXT.search(re.sub(r'\s+', ' ', _text(root))):
        raise ArticleExtractionError('gated article: only a preview is available')
    blocks = _collect(root)
    body = '\n\n'.join(text for _, text in blocks)
    prose = [text for tag, text in blocks if tag in ('p', 'li')]
    if len(prose) < 2 or sum(map(len, prose)) < 300:
        raise ArticleExtractionError('article body missing, unsupported, or only a short preview')
    return body
