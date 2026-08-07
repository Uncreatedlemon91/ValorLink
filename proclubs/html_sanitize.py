"""Sanitizes an article's rich-text HTML before it's stored/rendered.

Articles are authored with a WYSIWYG editor (Quill, see news_form.html)
that produces HTML directly -- there's no Markdown compile step anymore,
just sanitization. Staff-authored, but still web input reaching every
visitor's browser unescaped, so it's never trusted outright (a compromised
staff account, or a bug in the editor's own JS, shouldn't turn into
stored XSS).

A local copy rather than importing ValorLink's utils/markdown_render.py --
this app deliberately shares no code with the bot/web app (see README.md).
"""
from __future__ import annotations

import nh3

_ALLOWED_TAGS = {
    "p", "br", "hr", "strong", "em", "b", "i", "u", "s", "del", "blockquote",
    "pre", "code", "span", "ul", "ol", "li", "a", "img",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "table", "thead", "tbody", "tr", "th", "td",
    "clip-embed",
}
_ALLOWED_ATTRIBUTES = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title"},
    "code": {"class"},
    # A placeholder the article editor inserts for a Discord clip (see the
    # "Insert Clip" button in article-editor.js) -- deliberately just an id,
    # not the clip's video URL: that URL is Discord's signed CDN link, which
    # expires (~24h), so baking it into stored body_html would go stale.
    # services.render_clip_embeds() resolves this into a live <video> on
    # every render instead, using whatever's currently in the Clip table.
    "clip-embed": {"data-clip-id"},
}

# "data" is needed so inline images the editor embeds as data URIs (see the
# image button in news_form.html) survive sanitization -- there's no file
# upload endpoint for body images, they're embedded directly like cover
# images/avatars elsewhere in this app. nh3 applies url_schemes to every
# URL-bearing attribute uniformly, so this also permits a data: <a href>,
# which is a milder version of the same shortcoming: modern browsers already
# refuse top-level navigation to a cross-origin data: URL, so the realistic
# risk is low, but it's a real tradeoff, not an oversight.
_URL_SCHEMES = {"http", "https", "mailto", "data"}

# Keeps a single runaway article (mostly: someone pasting in a lot of
# inline images) from becoming an unbounded row -- generous enough for a
# long article with several photos, not a hard technical limit.
MAX_BODY_LENGTH = 4_000_000


def sanitize(html: str) -> str:
    """Raw editor HTML -> sanitized HTML fragment, safe to render unescaped."""
    return nh3.clean(
        html or "",
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        url_schemes=_URL_SCHEMES,
        link_rel="noopener noreferrer nofollow",
    )
