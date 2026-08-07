"""Tests for ea_client.py's kit-color decoding -- EA reports colors as a
plain decimal RGB integer, not hex, and this is the one bit of "club
branding" the unofficial API actually exposes (no crest image, just IDs
EA's own game client renders internally).

Run with: pytest proclubs/tests/test_ea_client.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ea_client  # noqa: E402


def test_hex_color_converts_decimal_rgb():
    assert ea_client._hex_color("15921906") == "#F2F2F2"
    assert ea_client._hex_color("13179675") == "#C91B1B"


def test_hex_color_handles_bad_input():
    assert ea_client._hex_color(None) is None
    assert ea_client._hex_color("not-a-number") is None


def test_crest_colors_decodes_custom_kit(monkeypatch):
    monkeypatch.setattr(ea_client, "club_info", lambda platform, club_id: {
        "name": "Yeehaw FC",
        "customKit": {
            "crestColor": "13179675", "kitColor1": "15921906", "kitColor2": "14358546",
            "kitThrdColor2": "7122142", "kitThrdColor4": "15921906",
        },
    })
    colors = ea_client.crest_colors("common-gen5", "8481799")
    assert colors == {
        "crest": "#C91B1B", "kit1": "#F2F2F2", "kit2": "#DB1812",
        "accent": "#6CACDE", "accent_trim": "#F2F2F2",
    }


def test_crest_colors_falls_back_to_crest_and_home_kit_without_a_third_kit(monkeypatch):
    monkeypatch.setattr(ea_client, "club_info", lambda platform, club_id: {
        "name": "No Third Kit FC",
        "customKit": {"crestColor": "13179675", "kitColor1": "15921906", "kitColor2": "14358546"},
    })
    colors = ea_client.crest_colors("common-gen5", "1")
    assert colors["accent"] == "#C91B1B"       # falls back to crestColor
    assert colors["accent_trim"] == "#F2F2F2"  # falls back to kitColor1


def test_crest_colors_returns_none_without_custom_kit(monkeypatch):
    monkeypatch.setattr(ea_client, "club_info", lambda platform, club_id: {"name": "No Kit FC"})
    assert ea_client.crest_colors("common-gen5", "1") is None


def test_crest_colors_returns_none_when_club_not_found(monkeypatch):
    monkeypatch.setattr(ea_client, "club_info", lambda platform, club_id: None)
    assert ea_client.crest_colors("common-gen5", "1") is None
