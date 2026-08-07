"""Tests for html_sanitize.py -- articles are authored with a rich-text
editor now (Quill), which produces HTML directly, so this sanitizes that
HTML rather than compiling Markdown. Staff-authored, but still web input
that reaches every visitor's browser unescaped, so a hostile payload still
needs to actually be neutralized, not just nicely-behaved input rendered.

Run with: pytest proclubs/tests/test_html_sanitize.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import html_sanitize  # noqa: E402


def test_basic_formatting_survives():
    html = html_sanitize.sanitize('<p><strong>bold</strong> and <em>em</em> and a <a href="https://example.com">link</a></p>')
    assert "<strong>bold</strong>" in html
    assert "<em>em</em>" in html
    assert '<a href="https://example.com"' in html


def test_script_tags_are_stripped():
    html = html_sanitize.sanitize("<script>alert(1)</script><p>Some text</p>")
    assert "<script" not in html
    assert "alert(1)" not in html


def test_event_handlers_are_stripped():
    html = html_sanitize.sanitize('<img src="x.png" onerror="alert(1)">')
    assert "onerror" not in html


def test_javascript_url_scheme_is_stripped():
    html = html_sanitize.sanitize('<a href="javascript:alert(1)">click me</a>')
    assert "javascript:" not in html


def test_data_uri_image_is_allowed():
    html = html_sanitize.sanitize('<img src="data:image/png;base64,iVBORw0KGgo=">')
    assert "<img" in html
    assert 'src="data:image/png;base64,iVBORw0KGgo="' in html


def test_headings_and_lists_render():
    html = html_sanitize.sanitize("<h1>Title</h1><ul><li>one</li><li>two</li></ul>")
    assert "<h1>Title</h1>" in html
    assert "<li>one</li>" in html


def test_quill_code_block_and_underline_survive():
    html = html_sanitize.sanitize('<pre class="ql-syntax">code here</pre><p><u>underlined</u></p>')
    assert "code here" in html
    assert "<u>underlined</u>" in html


def test_style_and_class_are_not_smuggled_through_arbitrary_tags():
    html = html_sanitize.sanitize('<p style="background:url(javascript:alert(1))" class="evil">text</p>')
    assert "style=" not in html
    assert "class=" not in html


def test_clip_embed_placeholder_survives_with_only_its_id():
    html = html_sanitize.sanitize('<clip-embed data-clip-id="42" contenteditable="false">🎥 label</clip-embed>')
    assert '<clip-embed data-clip-id="42">' in html
    assert "contenteditable" not in html


def test_clip_embed_with_non_numeric_id_is_still_just_an_attribute():
    # Sanitization doesn't validate the id is numeric -- that's on the
    # render side (services.render_clip_embeds); this only checks nothing
    # unexpected (extra attributes, script content) sneaks through.
    html = html_sanitize.sanitize('<clip-embed data-clip-id="42" onclick="alert(1)"></clip-embed>')
    assert "onclick" not in html


def test_empty_body_does_not_crash():
    assert html_sanitize.sanitize("") == ""
    assert html_sanitize.sanitize(None) == ""
