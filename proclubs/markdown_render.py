"""Renders an article's Markdown source to sanitized HTML.

A local copy rather than importing ValorLink's utils/markdown_render.py --
this app deliberately shares no code with the bot/web app (see
proclubs/README.md). Markdown is staff-authored but still web input, so it's
sanitized rather than trusted outright.
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
    "code": {"class"},
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
