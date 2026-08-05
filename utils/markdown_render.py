"""Renders a unit document's Markdown source to sanitized HTML.

Web-only (not imported by the bot), so its dependencies live in
web/requirements.txt rather than the bot's. Markdown is officer-authored, but
still web input -- rendered through nh3 (an HTML sanitizer) rather than
trusted outright, so a document can't carry a script tag or a javascript:
link into another reader's browser.
"""
from __future__ import annotations

import markdown as _markdown
import nh3

_EXTENSIONS = ["fenced_code", "tables", "sane_lists", "nl2br"]

_ALLOWED_TAGS = {
    "p", "br", "hr", "strong", "em", "b", "i", "u", "s", "del", "blockquote",
    "pre", "code", "span", "ul", "ol", "li", "a", "img",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "table", "thead", "tbody", "tr", "th", "td",
}
_ALLOWED_ATTRIBUTES = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title"},
    "code": {"class"},  # fenced-code-block language, e.g. "language-python"
}


def render(body: str) -> str:
    """Markdown source -> sanitized HTML fragment, safe to render unescaped."""
    html = _markdown.markdown(body or "", extensions=_EXTENSIONS)
    return nh3.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        url_schemes={"http", "https", "mailto"},
        link_rel="noopener noreferrer nofollow",
    )
