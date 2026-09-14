"""Event times: entered in the author's zone, stored as UTC, shown in each
reader's own zone.

The conversion is the part that quietly puts a fixture an hour out, so it
is tested at the boundary rather than through the UI.
"""
import os
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="proclubs-localtime-")
os.environ["SITE_DB_PATH"] = os.path.join(_TMP, "site.db")
os.environ["SESSION_SECRET"] = "test-secret"

import app as appmod  # noqa: E402


# --- entering a time -------------------------------------------------------- #
def test_offset_west_of_utc_shifts_forward():
    """New York in summer is UTC-4, so getTimezoneOffset() reports 240 and
    an 8pm kick-off is midnight UTC."""
    assert appmod._parse_scheduled_at("2026-09-20T20:00", "240") == \
        datetime(2026, 9, 21, 0, 0)


def test_offset_east_of_utc_shifts_back():
    """Berlin in summer is UTC+2: getTimezoneOffset() reports -120, so a
    9pm kick-off is 7pm UTC."""
    assert appmod._parse_scheduled_at("2026-09-20T21:00", "-120") == \
        datetime(2026, 9, 20, 19, 0)


def test_utc_author_is_unchanged():
    assert appmod._parse_scheduled_at("2026-09-20T20:00", "0") == \
        datetime(2026, 9, 20, 20, 0)


def test_no_offset_falls_back_to_utc():
    """JavaScript off, or an old bookmarked form. The field then means what
    it always meant rather than the save being rejected -- the label and a
    <noscript> hint both say so."""
    assert appmod._parse_scheduled_at("2026-09-20T20:00", "") == \
        datetime(2026, 9, 20, 20, 0)
    assert appmod._parse_scheduled_at("2026-09-20T20:00") == \
        datetime(2026, 9, 20, 20, 0)


def test_a_nonsense_offset_is_ignored_rather_than_applied():
    """Real offsets run UTC-12..UTC+14. Shifting a fixture by a junk amount
    is worse than treating the field as UTC."""
    for junk in ("99999", "1441", "-900", "not-a-number"):
        assert appmod._parse_scheduled_at("2026-09-20T20:00", junk) == \
            datetime(2026, 9, 20, 20, 0)


def test_extreme_but_real_offsets_are_honoured():
    # Kiritimati is UTC+14 (-840), Baker Island UTC-12 (720).
    assert appmod._parse_scheduled_at("2026-09-20T12:00", "-840") == \
        datetime(2026, 9, 19, 22, 0)
    assert appmod._parse_scheduled_at("2026-09-20T12:00", "720") == \
        datetime(2026, 9, 21, 0, 0)


def test_an_unparseable_datetime_is_still_rejected():
    assert appmod._parse_scheduled_at("not a date", "240") is None
    assert appmod._parse_scheduled_at("", "240") is None


def test_the_offset_used_is_the_one_for_the_selected_instant():
    """The browser sends getTimezoneOffset() for the picked date, so a
    fixture on either side of a daylight-saving change converts with the
    offset in force that day -- this is what stops a November match being
    an hour out because it was booked in August."""
    summer = appmod._parse_scheduled_at("2026-08-20T20:00", "240")   # EDT
    winter = appmod._parse_scheduled_at("2026-12-20T20:00", "300")   # EST
    assert summer == datetime(2026, 8, 21, 0, 0)
    assert winter == datetime(2026, 12, 21, 1, 0)


# --- showing a time --------------------------------------------------------- #
def test_localtime_emits_a_machine_readable_instant():
    html = str(appmod._localtime(datetime(2026, 9, 20, 19, 30), "full"))
    assert 'datetime="2026-09-20T19:30:00+00:00"' in html
    assert 'data-localtime="full"' in html


def test_localtime_fallback_text_is_labelled_utc():
    """What stands when JavaScript never runs. It must not read as local."""
    html = str(appmod._localtime(datetime(2026, 9, 20, 19, 30), "full"))
    assert "19:30 UTC" in html


def test_localtime_formats_without_a_clock_carry_no_utc_label():
    """A bare month or day has no time in it to be in the wrong zone."""
    assert "UTC" not in str(appmod._localtime(datetime(2026, 9, 20, 19, 30), "month"))
    assert "UTC" not in str(appmod._localtime(datetime(2026, 9, 20, 19, 30), "day"))


def test_localtime_falls_back_to_full_for_an_unknown_format():
    html = str(appmod._localtime(datetime(2026, 9, 20, 19, 30), "nonsense"))
    assert "19:30 UTC" in html


def test_localtime_escapes_its_attributes():
    html = str(appmod._localtime(datetime(2026, 9, 20, 19, 30), '"><script>'))
    assert "<script>" not in html
