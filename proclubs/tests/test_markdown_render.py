"""Tests for markdown_render.py -- staff-authored content is still web
input, so sanitization needs to actually hold up against a hostile payload,
not just render nicely-behaved Markdown.

Run with: pytest proclubs/tests/test_markdown_render.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import markdown_render  # noqa: E402


def test_basic_formatting_survives():
    html = markdown_render.render("**bold** and *em* and a [link](https://example.com)")
    assert "<strong>bold</strong>" in html
    assert "<em>em</em>" in html
    assert '<a href="https://example.com"' in html


def test_script_tags_are_stripped():
    html = markdown_render.render("<script>alert(1)</script>\n\nSome text")
    assert "<script" not in html
    assert "alert(1)" not in html


def test_event_handlers_are_stripped():
    html = markdown_render.render('<img src="x.png" onerror="alert(1)">')
    assert "onerror" not in html


def test_javascript_url_scheme_is_stripped():
    html = markdown_render.render("[click me](javascript:alert(1))")
    assert "javascript:" not in html


def test_data_uri_image_is_allowed():
    html = markdown_render.render("![cover](data:image/png;base64,iVBORw0KGgo=)")
    assert "<img" in html


def test_headings_and_lists_render():
    html = markdown_render.render("# Title\n\n- one\n- two")
    assert "<h1>Title</h1>" in html
    assert "<li>one</li>" in html


def test_empty_body_does_not_crash():
    assert markdown_render.render("") == ""
    assert markdown_render.render(None) == ""
