"""Safe CSS/HTML helpers for slide and overlay rendering.

Values that end up inside ``style="..."`` attributes or CSS ``url(...)`` must be
validated before interpolation. Rich text for announcement/title overlays goes
through the same inline-tag whitelist used for song lyrics (attributes stripped).
"""
from __future__ import annotations

import re
from typing import Optional

from .parsing import _sanitize_inline_html

_CSS_HEX_RE = re.compile(r'^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$')
# Font family names: letters, digits, spaces, hyphen, underscore, comma (fallback lists).
_FONT_FAMILY_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9 _\-,]*$')
_TEXT_ALIGNS = frozenset({'left', 'center', 'right', 'justify'})
# App-served static assets only (theme backgrounds, images, videos, fonts).
_STATIC_URL_RE = re.compile(r'^/static/[A-Za-z0-9._~/-]+$')
_SIZE_OPEN_RE = re.compile(r'<size=(\d{1,3})>', re.IGNORECASE)
_SIZE_CLOSE_RE = re.compile(r'</size>', re.IGNORECASE)


def _clamp_size_pct(raw) -> int:
    try:
        return max(10, min(400, int(raw)))
    except (TypeError, ValueError):
        return 100


def convert_size_tags(text: str) -> str:
    """Convert canonical ``<size=NN>`` tags to relative font-size spans for display."""
    text = _SIZE_OPEN_RE.sub(
        lambda m: f'<span style="font-size:{_clamp_size_pct(m.group(1))}%">', text)
    return _SIZE_CLOSE_RE.sub('</span>', text)


def safe_css_color(value, default: str = '#ffffff') -> str:
    """Return ``value`` if it is a hex color (#rgb / #rrggbb / #rrggbbaa), else ``default``."""
    s = str(value or '').strip()
    if _CSS_HEX_RE.match(s):
        return s
    return default


def safe_font_family(value, default: str = 'Helvetica') -> str:
    """Return a CSS-safe font-family name, or ``default`` if the value is unsafe."""
    s = str(value or '').strip()
    # Reject quotes, semicolons, backslashes, and control characters early.
    if not s or any(c in s for c in ('"', "'", ';', '\\', '<', '>', '{', '}')):
        return default
    if any(ord(c) < 32 for c in s):
        return default
    if not _FONT_FAMILY_RE.match(s):
        return default
    return s


def safe_text_align(value, default: str = 'center') -> str:
    """Return an allowlisted text-align keyword, or ``default``."""
    s = str(value or '').strip().lower()
    return s if s in _TEXT_ALIGNS else default


def safe_css_url(url) -> Optional[str]:
    """Return a safe ``/static/...`` path for use in CSS ``url(...)``, or None."""
    if not isinstance(url, str):
        return None
    s = url.strip()
    if not s or any(c in s for c in ('"', "'", ')', '(', '\\', ' ', '\t', '\n', '\r', '<', '>')):
        return None
    if not _STATIC_URL_RE.match(s):
        return None
    # Normalize: reject path traversal segments.
    if '/../' in s or s.endswith('/..'):
        return None
    return s


def escape_rich_text(content: str) -> str:
    """Escape user text for slide HTML with the song-safe inline tag subset.

    Allows ``<b>/<i>/<u>`` (attributes stripped) and ``<size=NN>`` (converted to a
    relative span); newlines become ``<br>``.
    """
    esc = _sanitize_inline_html(content or '')
    esc = convert_size_tags(esc)
    return esc.replace('\n', '<br>')


__all__ = [
    'convert_size_tags',
    'escape_rich_text',
    'safe_css_color',
    'safe_css_url',
    'safe_font_family',
    'safe_text_align',
    '_clamp_size_pct',
]
