"""Uploaded-image processing: downscale on the way in, two sizes on the
way out.

Uploads used to be stored verbatim as base64 data URIs and pasted straight
into the HTML of every page that showed them. That made the home page
~10MB of markup and the news index ~23MB, none of it cacheable: a base64
blob lives inside the document, so the browser re-downloads every cover
image on every navigation and can't paint until the whole document has
arrived.

So uploads are now re-encoded to something sane, and each one is stored at
two sizes -- a display-size image for heroes and article headers, and a
thumbnail for the card/rail grids that show a dozen covers at once. Both
are still data URIs in the database (no object storage on this droplet, and
SQLite is where backups already point), but they're *served* from
/media/... routes so browsers can cache them and fetch them in parallel
instead of inline (see app.py).
"""
from __future__ import annotations

import base64
import io
import re

from PIL import Image, ImageOps

# Longest edge, in pixels. The display size is generous enough for the home
# hero on a 2x laptop; the thumbnail is sized for the rail/grid cards, which
# never render wider than ~420 CSS px.
DISPLAY_MAX_EDGE = 1600
THUMB_MAX_EDGE = 480

# WebP at these qualities is visually indistinguishable from the source for
# photographic covers at roughly a fifth of the JPEG bytes, and every
# browser that can run this site supports it.
DISPLAY_QUALITY = 80
THUMB_QUALITY = 72

_DATA_URI_RE = re.compile(r"^data:([^;,]+);base64,(.+)$", re.S)


class ImageError(Exception):
    """An upload that isn't a usable image."""


def _to_data_uri(content_type: str, raw: bytes) -> str:
    return f"data:{content_type};base64," + base64.b64encode(raw).decode("ascii")


def decode_data_uri(data_uri: str | None) -> tuple[str, bytes] | tuple[None, None]:
    """Splits a `data:<mime>;base64,<...>` URI into its content type and
    decoded bytes. Returns (None, None) for anything that isn't one --
    including a plain http(s) URL, which is what Event.image holds."""
    match = _DATA_URI_RE.match(data_uri or "")
    if not match:
        return None, None
    try:
        return match.group(1), base64.b64decode(match.group(2), validate=True)
    except (ValueError, TypeError):
        return None, None


def _encode(image: Image.Image, max_edge: int, quality: int) -> bytes:
    resized = ImageOps.contain(image, (max_edge, max_edge), Image.LANCZOS)
    buffer = io.BytesIO()
    resized.save(buffer, format="WEBP", quality=quality, method=4)
    return buffer.getvalue()


def render_variants(raw: bytes) -> tuple[str, str]:
    """(display, thumbnail) as data URIs, both WebP.

    EXIF orientation is applied and then dropped along with the rest of the
    metadata -- re-encoding wouldn't otherwise rotate a phone photo, and
    cover images have no business carrying GPS tags into a public page."""
    try:
        with Image.open(io.BytesIO(raw)) as image:
            image = ImageOps.exif_transpose(image)
            # WebP has no palette mode and doesn't want CMYK; RGBA is fine
            # and keeps transparency for the odd PNG crest or logo.
            if image.mode not in ("RGB", "RGBA"):
                image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
            display = _encode(image, DISPLAY_MAX_EDGE, DISPLAY_QUALITY)
            thumb = _encode(image, THUMB_MAX_EDGE, THUMB_QUALITY)
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ImageError("That image couldn't be read.") from exc
    return _to_data_uri("image/webp", display), _to_data_uri("image/webp", thumb)
