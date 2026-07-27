"""SeventhSlide — FastAPI live presentation server (lyrics, Bible, media, outputs).

Feature behaviour and operator docs live in the user guide / README. This module
is the HTTP/WebSocket entry point; shared helpers live under ``seventhslide/``.

Usage: ``python3 lyrics.py`` then open http://localhost:49777/admin
"""

import json
import os
import sys
import re
import copy
import asyncio
import shutil
import hashlib
import math
import uuid
import tempfile
import time
import types
import zipfile
import urllib.parse
from typing import List, Optional, Dict, Any
from functools import lru_cache
from contextlib import asynccontextmanager, contextmanager

from fastapi import FastAPI, WebSocket, UploadFile, File, Form, Body, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# --- Extracted subsystems (seventhslide/) ---------------------------------
from seventhslide.paths import (
    _path_is_within, get_base_dir, get_data_dir, get_resource_path, logger,
)
from seventhslide.models import (
    BG_THEME_KEYS, CLOCK_KEYS, DEFAULT_THEME_PRIORITY, OUTPUT_STYLE_KEYS,
    OutputConfig, TEXT_THEME_KEYS, THEME_CATEGORIES, THEME_PRIORITY_TIERS,
    normalize_theme_priority,
)
from seventhslide.parsing import (
    _VERSE_TYPE_MAP, _sanitize_inline_html, parse_bible_file, parse_bible_reference,
    parse_song_file,
)
from seventhslide.fonts import FontManager, _resolve_font_file_cached
from seventhslide.database import DatabaseManager
# Not in database.__all__; used to validate/normalize embedded title-slide payloads.
from seventhslide.database import _normalize_ann_boxes
from seventhslide.render_safe import (
    _clamp_size_pct,
    convert_size_tags as _convert_size_tags,
    escape_rich_text as _escape_rich_text,
    safe_css_color,
    safe_css_url,
    safe_font_family,
    safe_text_align,
)

# Try to use PIL for font measurement, fallback to approximation
try:
    from PIL import ImageFont
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# Optional: pure-Python QR generator for the admin-link QR code. If it's missing the
# admin URL is still shown; only the QR image is omitted.
try:
    import segno
except ImportError:
    segno = None

def _sanitize_lyrics(lyrics):
    """Whitelist lyric markup on save (<b>/<i>/<u>, <size=NN>); escape other tags."""
    if not isinstance(lyrics, str) or not lyrics:
        return lyrics
    return _sanitize_inline_html(lyrics)


def _validate_verse_order(verse_order: str, lyrics: str) -> Optional[str]:
    """Return an error message if verse_order references verse codes absent from lyrics, else None."""
    if not (verse_order and lyrics):
        return None
    keys = set()
    parts = re.split(r'---\[([^\]]+)\]---\n', lyrics)
    if len(parts) > 1:
        i = 1
        while i < len(parts):
            keys.add(_VerseParser._label_to_code(parts[i].strip()))
            i += 2
    req_tokens = [x.lower() for x in verse_order.split()]
    # 't1' is always valid: the title is virtual (a theme-driven title slide), so it
    # needs no backing lyric section.
    missing = [t for t in req_tokens
               if t != 't1' and not any(_VerseParser._matches_token(c, t) for c in keys)]
    if missing:
        return f"Invalid codes: {', '.join(missing)}\nval: {', '.join(sorted(keys))}"
    return None


def _validate_no_blank_lines_in_verse(lyrics: str) -> Optional[str]:
    """Validate that no verse block contains a blank line between non-blank lines.

    Blank lines *between* verses (between the ---[...]--- headers) are fine.
    Blank lines *within* a verse block (with non-blank content on both sides) are not.

    Returns an error message string if invalid, or None if valid.
    """
    # Only applies to the structured header format ---[Label]---
    raw_parts = re.split(r'---\[([^\]]+)\]---\n', lyrics)
    if len(raw_parts) <= 1:
        # Legacy plain-text format: blank lines are the verse separator, no constraint
        return None

    # raw_parts layout: [pre-header-text, label1, body1, label2, body2, ...]
    i = 1
    while i < len(raw_parts):
        label = raw_parts[i]
        body = raw_parts[i + 1] if i + 1 < len(raw_parts) else ''

        # Trailing blank lines at the end of a block are fine (they separate verses)
        lines = body.rstrip('\n').split('\n')

        found_content = False
        found_blank_after_content = False

        for line in lines:
            if line.strip():
                if found_blank_after_content:
                    return (
                        f'"{label}" contains a blank line in the middle of the verse. '
                        f'Blank lines are only allowed between verses, not within them.'
                    )
                found_content = True
            elif found_content:
                found_blank_after_content = True

        i += 2

    return None
_VERSE_CODE_MAP: dict[str, str] = {v.lower(): k for k, v in _VERSE_TYPE_MAP.items()}

def _load_pil_font(font_family, font_size, bold, italic):
    """Resolve a PIL ImageFont for the family/style, or None if PIL can't load any face.

    Tries, in order: (1) the exact bold/italic face — a bare-family load returns the
    regular face, which measures narrower than the browser paints, wrapping styled lines
    a slide too late; (2) a direct load by family name; (3) the shared cross-platform
    resolver (fontconfig where present, else the bundled index) — one disk-backed code
    path, cached across calls and restarts.
    """
    if bold or italic:
        face = _resolve_font_file_cached(
            font_family,
            weight='bold' if bold else 'regular',
            slant='italic' if italic else 'roman')
        if face:
            try:
                return ImageFont.truetype(face, font_size)
            except Exception:
                logger.debug("PIL font load failed (styled face %r)", face, exc_info=True)
    try:
        return ImageFont.truetype(font_family, font_size)
    except Exception:
        logger.debug("PIL font load failed (family %r)", font_family, exc_info=True)
    path = _resolve_font_file_cached(font_family)
    if path:
        try:
            return ImageFont.truetype(path, font_size)
        except Exception:
            logger.debug("PIL font load failed (resolved path %r)", path, exc_info=True)
    return None


def _pil_measure(font_obj, font_size):
    """Advance-width measurer for a loaded PIL font. getlength sums glyph advances —
    exactly the browser's wrap metric — and both this measurer and the output page load
    the same resolved font file, so it's an unbiased width predictor (no correction
    factor). Over-estimating would wrap near-boundary lines a slide too early; the box's
    overflow:hidden backstops any sub-pixel under-estimate.

    If both getlength and getbbox fail, fall back to the same 0.5*font_size char-width
    approximation used when no font loads at all — never return 0 (which would disable
    wrapping and collapse all lyrics onto one slide).
    """
    avg_char_width = font_size * 0.5
    def measure(text):
        try:
            if hasattr(font_obj, 'getlength'):
                return float(font_obj.getlength(text))
        except Exception:
            pass
        try:
            bbox = font_obj.getbbox(text)
            if bbox:
                return float(bbox[2] - bbox[0])
        except Exception:
            pass
        logger.warning("PIL font measure failed; using char-width approximation",
                       exc_info=True)
        return len(text) * avg_char_width
    return measure


@lru_cache(maxsize=256)
def _get_font_measurement(font_family, font_size, bold=False, italic=False):
    """Get font measurement function. Returns (measure_func, line_height).

    Cached because font resolution (fc-match subprocess + PIL load) is expensive
    and fonts don't change at runtime. Bounded eviction via lru_cache.
    """
    line_height = int(math.ceil(font_size * 1.2))   # match CSS line-height: 1.2
    if HAS_PIL:
        font_obj = _load_pil_font(font_family, font_size, bold, italic)
        if font_obj:
            return _pil_measure(font_obj, font_size), line_height

    # Fallback: simple approximation (assumes ~0.5 * font_size average char width).
    # 0.5 (vs 0.6) is deliberately less conservative to prevent unnecessary wrapping.
    avg_char_width = font_size * 0.5
    def measure(text):
        return len(text) * avg_char_width
    return measure, line_height


# ---------------------- Slide generation ----------------------

TAG_RE = re.compile(r'<[^>]+>')

# Inline relative-size formatting: editors store `<size=NN>text</size>` (NN = %
# of the surrounding font size), applied like <b>/<i>/<u> from the text toolbar.
# The canonical tag form flows through storage and measurement; it converts to a
# styled span only at the HTML assembly edge via _convert_size_tags (render_safe).
# Size clamping lives in render_safe._clamp_size_pct (imported above).


def _size_runs(text: str) -> list:
    """Split a lyric line into (plain_text, scale_pct) runs for measurement.

    Only one level matters for width/height estimation, so nested size tags
    resolve to the innermost scale. Other markup is stripped (as TAG_RE would)."""
    runs = []
    stack = [100]
    pos = 0
    for m in re.finditer(r'<size=(\d{1,3})>|</size>|<[^>]+>', text, re.IGNORECASE):
        if m.start() > pos:
            runs.append((text[pos:m.start()], stack[-1]))
        tok = m.group(0).lower()
        if tok.startswith('<size='):
            stack.append(_clamp_size_pct(m.group(1)))
        elif tok == '</size>':
            if len(stack) > 1:
                stack.pop()
        pos = m.end()
    if pos < len(text):
        runs.append((text[pos:], stack[-1]))
    return [(t, s) for t, s in runs if t]

def wrap_plain_text_to_width(plain_text, measure_func, max_width_px):
    """Greedy word-wrap for plain text. Returns list of visual lines."""
    words = plain_text.split()
    if not words:
        return ['']
    lines = []
    line_words = [words[0]]
    for w in words[1:]:
        cand = line_words + [w]
        if measure_func(' '.join(cand)) <= max_width_px:
            line_words = cand
        else:
            lines.append(' '.join(line_words))
            line_words = [w]
    lines.append(' '.join(line_words))
    return lines

def split_text_smart(text, delimiters):
    """Split text by delimiters while respecting quotes and parentheses."""
    chunks = []
    current = ""
    i = 0
    while i < len(text):
        char = text[i]
        current += char
        
        # Check for delimiters
        if char in delimiters:
            # Look ahead for closing quotes, parentheses, etc. to treat them as part of the chunk
            j = i + 1
            while j < len(text) and text[j] in '"\')]}':
                current += text[j]
                j += 1
            
            # Check if followed by space or end (heuristic to avoid splitting abbreviations like Mr. or numbering)
            # For commas/semicolons this is usually true too.
            if j >= len(text) or text[j].isspace():
                chunks.append(current.strip())
                current = ""
                i = j
                continue
        
        i += 1
    
    # Add remaining text
    if current.strip():
        chunks.append(current.strip())
    
    return chunks if chunks else [text]

def split_text_by_sentences(text):
    return split_text_smart(text, '.!?')

def split_text_by_clauses(text):
    return split_text_smart(text, ';,')

def _greedy_group(chunks, measure_func, max_width_px, max_visual_lines, split_oversize):
    """Greedily pack `chunks` into groups, each wrapping to at most `max_visual_lines`
    visual lines, joining grouped chunks with a single space.

    A chunk that doesn't fit even on its own is handed to `split_oversize(chunk)`,
    whose returned lines are emitted as-is (the next escalation tier). This is the
    shared grouping pass used by both the sentence and clause tiers below.
    """
    final_lines = []
    group = []
    group_text = ""
    for chunk in chunks:
        test_text = (group_text + " " + chunk).strip() if group_text else chunk
        if len(wrap_plain_text_to_width(test_text, measure_func, max_width_px)) <= max_visual_lines:
            # Fits in the current group.
            group_text = test_text
            group.append(chunk)
            continue
        # Doesn't fit — close the current group first.
        if group:
            final_lines.append(" ".join(group))
            group = []
            group_text = ""
        # Does the chunk fit on a fresh line by itself?
        if len(wrap_plain_text_to_width(chunk, measure_func, max_width_px)) <= max_visual_lines:
            group.append(chunk)
            group_text = chunk
        else:
            final_lines.extend(split_oversize(chunk))
    if group:
        final_lines.append(" ".join(group))
    return final_lines


def split_line_to_fit(text, measure_func, line_height, max_width_px, max_height_px, is_html=False):
    """
    Split a line into sub-lines that fit within the given dimensions.
    Strategy: Sentences -> Clauses (;, ) -> Words
    """
    # Strip HTML for measurement if needed
    if is_html:
        plain_text = TAG_RE.sub('', text)
    else:
        plain_text = text

    # First check if the line fits as-is
    wrapped = wrap_plain_text_to_width(plain_text, measure_func, max_width_px)
    total_height = len(wrapped) * line_height

    if total_height <= max_height_px:
        return [text]  # Fits fine, no split needed

    # Line is too tall, need to split
    max_visual_lines = max(1, int(max_height_px / line_height))

    def to_words(chunk):
        # --- TIER 3: Words ---
        return _split_by_words(chunk, measure_func, max_width_px, max_visual_lines)

    def split_sentence(sentence):
        # A sentence too big for a whole slide: --- TIER 2: Clauses --- then words.
        clauses = split_text_by_clauses(sentence)
        if len(clauses) <= 1:
            return to_words(sentence)
        return _greedy_group(clauses, measure_func, max_width_px, max_visual_lines, to_words)

    # --- TIER 1: Sentences ---
    sentences = split_text_by_sentences(plain_text)
    return _greedy_group(sentences, measure_func, max_width_px, max_visual_lines, split_sentence)

def _split_by_words(text, measure_func, max_width_px, max_visual_lines):
    """Split plain text into sub-lines each wrapping to at most max_visual_lines.

    Incremental greedy wrap matching wrap_plain_text_to_width — O(n) in word count
    (the previous version re-wrapped the whole candidate on every word).
    """
    words = text.split()
    if not words:
        return []
    sub_lines = []
    # Current group: list of visual lines, each a list of words.
    group_lines = []

    def flush():
        nonlocal group_lines
        if group_lines:
            sub_lines.append(' '.join(w for line in group_lines for w in line))
            group_lines = []

    for word in words:
        if not group_lines:
            group_lines = [[word]]
            continue
        last = group_lines[-1]
        cand = ' '.join(last + [word])
        if measure_func(cand) <= max_width_px:
            last.append(word)
        elif len(group_lines) < max_visual_lines:
            group_lines.append([word])
        else:
            flush()
            # A single word wider than max_width_px still becomes its own sub-line
            # (wrap_plain_text_to_width never splits mid-word).
            group_lines = [[word]]
    flush()
    return sub_lines


class _ChordProcessor:
    """Render inline ``[Chord]`` notation as chords stacked above their lyric syllables.

    The generated HTML is class-based — all styling lives once in ``output.html`` (the
    ``.cc`` / ``.ch`` / ``.ly`` / ``.fl`` rules). Each lyric syllable becomes an
    inline-flex *cell* (chord on top, lyric below).

    Layout mirrors OpenLP's web-remote chord view:

    * **Overlap (the default).** The chord renders with zero width and simply overflows to
      the right above its syllable (CSS ``width: 0; overflow: visible``). It still occupies
      the row *vertically*, so lines wrap cleanly, but it adds no horizontal width — the
      lyric keeps its natural spacing instead of being stretched to the chord's width. This
      is what keeps chord-only markers (a ``[Chord]`` before a space or line end) and chords
      over short syllables from blowing gaps into the line.
    * **Expand (only when a chord is wider than its syllable).** Here the chord reclaims its
      own width so it cannot collide with the next chord, and the surplus is bridged with a
      dashed connector (the ``.fl`` element, e.g. ``A — men``). Width is compared by glyph
      count, matching OpenLP's own heuristic — no font measurement needed.

    ``b``/``#`` accidentals become the typographic ``♭``/``♯`` symbols. Inline
    ``<b>/<i>/<u>`` formatting is tracked across syllable boundaries so a tag opened before
    a chord stays balanced in every cell.
    """

    _MARKER_RE = re.compile(r'\[[^\]]*\]')

    # Dashed connector for the expand case, bridging the gap a wide chord opens before the
    # next cell. Only emitted on expand cells (.cc-x), where the stretched lyric row gives
    # it room to grow into; CSS paints it as a single centered dash.
    _FILL = '<span class="fl"></span>'

    # ASCII accidentals → typographic symbols, matching OpenLP's chord display. Only a
    # lowercase ``b`` is a flat (an uppercase ``B`` is the note name), so the table is
    # case-sensitive.
    _ACCIDENTALS = str.maketrans({'b': '♭', '#': '♯'})

    # One token at a time: a [chord] marker, an inline <b>/<i>/<u> tag, a run of
    # whitespace, a run of ordinary text (allowing a stray '<'), or any leftover char.
    _TOKEN_RE = re.compile(
        r'\[(?P<chord>[^\]]*)\]'
        r'|(?P<tag></?[biu]>)'
        r'|(?P<space>\s+)'
        r'|(?P<word>(?:[^\[\s<]|<(?![/]?[biu]>))+)'
        r'|(?P<other>.)',
        re.IGNORECASE,
    )

    # Zero-width space: reserves a chord row's line-height while adding no width, so
    # plain syllables line up vertically with chorded ones and empty chords vanish.
    _ZWSP = '&#8203;'

    @staticmethod
    def strip_chords(text: str) -> str:
        """Remove inline ``[chord]`` markers, leaving the lyric text intact."""
        return _ChordProcessor._MARKER_RE.sub('', text)

    @staticmethod
    def has_chords(text: str) -> bool:
        """True if the line contains at least one ``[chord]`` marker."""
        return _ChordProcessor._MARKER_RE.search(text) is not None

    @staticmethod
    def _apply_tag(raw: str, open_tags: list) -> None:
        """Update the open-inline-tag stack for a <b>/<i>/<u> open or close token, so a tag
        opened before a chord stays balanced across the syllable cells that follow."""
        is_close = raw[1] == '/'
        name = (raw[2:-1] if is_close else raw[1:-1]).lower()
        if is_close:
            if name in open_tags:
                open_tags.remove(name)
        else:
            open_tags.append(name)

    @staticmethod
    def _cell(chord: Optional[str], syllable: str, open_tags: list) -> str:
        """One chord-over-lyric cell's HTML. ``syllable`` wrapped in the currently-open
        inline tags (or a zero-width space if empty); the chord sits above it, expanding
        to reclaim its width (with a dashed connector) only when it's wider than the
        syllable — otherwise it overflows free so the lyric is never stretched."""
        if syllable:
            body = ''.join(f'<{t}>' for t in open_tags) + syllable + \
                   ''.join(f'</{t}>' for t in reversed(open_tags))
        else:
            body = _ChordProcessor._ZWSP
        if not chord:
            return (f'<span class="cc"><span class="ch">{_ChordProcessor._ZWSP}</span>'
                    f'<span class="ly">{body}</span></span>')
        glyph = chord.translate(_ChordProcessor._ACCIDENTALS)
        # Expand (glyph count, like OpenLP) only when wider than the syllable; a chord with
        # no syllable always overlaps — nothing to push, nothing to bridge.
        if syllable and len(glyph) > len(syllable):
            return (f'<span class="cc cc-x"><span class="ch">{glyph}</span>'
                    f'<span class="ly">{body}{_ChordProcessor._FILL}</span></span>')
        return (f'<span class="cc"><span class="ch">{glyph}</span>'
                f'<span class="ly">{body}</span></span>')

    @staticmethod
    def render(line: str) -> str:
        """Convert a line containing ``[chord]`` markers into chord-over-lyric HTML."""
        cells: list[str] = []
        open_tags: list[str] = []          # inline tags currently in effect, e.g. ['b']
        pending_chord: Optional[str] = None  # chord awaiting its lyric syllable

        def emit(chord: Optional[str], syllable: str) -> None:
            cells.append(_ChordProcessor._cell(chord, syllable, open_tags))

        for m in _ChordProcessor._TOKEN_RE.finditer(line):
            kind = m.lastgroup
            if kind == 'chord':
                # Two chords with no syllable between: anchor the first on an empty cell.
                if pending_chord is not None:
                    emit(pending_chord, '')
                pending_chord = m.group('chord')
            elif kind == 'tag':
                _ChordProcessor._apply_tag(m.group(), open_tags)
            elif kind == 'space':
                # A chord directly followed by a space has no syllable of its own.
                if pending_chord is not None:
                    emit(pending_chord, '')
                    pending_chord = None
                cells.append(' ')
            else:  # 'word' or 'other' — a lyric syllable
                emit(pending_chord, m.group())
                pending_chord = None

        if pending_chord is not None:
            emit(pending_chord, '')

        return ''.join(cells).strip()


# A title section/token code ('t1', 't1a', …) as produced by _label_to_code and
# typed in verse orders. Titles are virtual since themes gained title templates:
# the code may appear in a verse order without any backing lyric section.
_TITLE_CODE_RE = re.compile(r'^t\d+[a-z]?$')


class _VerseParser:
    """Handles verse structure parsing and ordering."""

    @staticmethod
    def parse_verses(lyrics_text: str, verse_order: Optional[str] = None,
                     suppress_titles: bool = False) -> tuple[list[str], list[str]]:
        """
        Parse lyrics into ordered verses with codes.

        With suppress_titles, baked Title sections (---[Title:1]--- blocks, common in
        pre-1.2 imports) are dropped: the theme-driven title slide replaces them, so
        rendering both would show the title twice. Verse-order title tokens (t1) become
        virtual empty verses (no lyric body) so each can occupy its own slide position.

        Returns:
            (verses, verse_codes) - Lists of verse content and their codes
        """
        # Pattern: ---[Label]--- . re.split yields [pre, label1, body1, label2, body2, ...];
        # a single element means no headers were found (legacy/plain text).
        raw_parts = re.split(r'---\[([^\]]+)\]---\n', lyrics_text)
        if len(raw_parts) <= 1:
            return _VerseParser._verses_from_plain(lyrics_text, verse_order, suppress_titles)
        return _VerseParser._verses_from_blocks(raw_parts, verse_order, suppress_titles)

    @staticmethod
    def _verses_from_plain(lyrics_text, verse_order, suppress_titles):
        """No headers: split on blank lines. Verse-order lyric tokens can't match codes,
        so the body plays once in written order; title tokens insert virtual empty verses
        only when theme-driven titles are active (suppress_titles)."""
        body_verses = [v for v in lyrics_text.split('\n\n') if v != '']
        if not verse_order:
            return body_verses, ['' for _ in body_verses]
        req_order = [x.lower() for x in verse_order.split()]
        verses: list[str] = []
        verse_codes: list[str] = []
        emitted_body = False
        for token in req_order:
            if _TITLE_CODE_RE.match(token):
                if suppress_titles:
                    verses.append('')
                    verse_codes.append('t1')
            elif not emitted_body:
                verses.extend(body_verses)
                verse_codes.extend(['' for _ in body_verses])
                emitted_body = True
        return (verses, verse_codes) if verses else (body_verses, ['' for _ in body_verses])

    @staticmethod
    def _verses_from_blocks(raw_parts, verse_order, suppress_titles):
        """Blocks parsed from ---[Label]--- headers, ordered by verse_order when given (a
        bare token plays every lettered section of that verse), else in appearance order."""
        blocks = []
        i = 1
        while i < len(raw_parts):
            label = raw_parts[i].strip()
            content = raw_parts[i + 1].strip()
            code = _VerseParser._label_to_code(label)
            if not (suppress_titles and _TITLE_CODE_RE.match(code)):
                blocks.append({'code': code, 'content': content, 'label': label})
            i += 2

        if not verse_order:
            return [b['content'] for b in blocks], [b['code'] for b in blocks]

        req_order = [x.lower() for x in verse_order.split()]
        ordered_parts = []
        verse_codes = []
        for token in req_order:
            if suppress_titles and _TITLE_CODE_RE.match(token):
                # Virtual title slide: empty body, theme paints the overlay.
                ordered_parts.append('')
                verse_codes.append('t1')
                continue
            # A token can match several blocks: 'v1' plays every section of
            # verse 1 (v1a, v1b, ...) in written order.
            for b in blocks:
                if _VerseParser._matches_token(b['code'], token):
                    ordered_parts.append(b['content'])
                    verse_codes.append(b['code'])

        if ordered_parts:
            return ordered_parts, verse_codes
        # Fallback if no tokens match: appearance order.
        return [b['content'] for b in blocks], [b['code'] for b in blocks]

    @staticmethod
    def _label_to_code(label: str) -> str:
        """Convert a verse label like 'Verse:1' to a code like 'v1'.

        A trailing section letter is kept ('Verse:1a' -> 'v1a'): sections are the
        OpenLyrics verse-part convention, used to force a slide break inside one
        verse in paging mode (see _compute_line_groups)."""
        lud = label.lower()
        m = re.search(r'(\d+)([a-z])?\s*$', lud)
        if m:
            digits, part = m.group(1), m.group(2) or ''
        else:
            digits, part = "".join(filter(str.isdigit, lud)), ''
        label_type = lud.split(':')[0]
        prefix = _VERSE_CODE_MAP.get(label_type)
        code = (prefix + (digits or '1') + part) if prefix else "misc"
        return code

    @staticmethod
    def _matches_token(code: str, token: str) -> bool:
        """True when verse `code` is selected by verse-order `token`. A bare token
        ('v1') selects the verse and all its lettered sections ('v1a', 'v1b');
        a section token ('v1a') selects exactly that section."""
        return code == token or (
            code[:-1] == token and code[-1:].isalpha() and code[-2:-1].isdigit()
        )


class _SlideGrouper:
    """Handles slide grouping and pagination logic."""

    def __init__(self, output_config, max_visual_px: int):
        self.output_config = output_config
        self.max_visual_px = max_visual_px
        # (line_index, base_height_px, active_height_px, verse_index). active_height_px
        # is the line's height when rendered as a highlighted/active line — larger than
        # base in follow-lines mode with a highlight font size; equal to base otherwise.
        self.line_buffer: list[tuple[int, int, int, int]] = []
        self.groups: list[dict] = []

    def add_line(self, line_index: int, base_height_px: int, active_height_px: int, verse_index: int):
        """Add a line to the buffer."""
        self.line_buffer.append((line_index, base_height_px, active_height_px, verse_index))

    def flush_buffer(self):
        """Convert buffered lines into slide groups."""
        if not self.line_buffer:
            return

        step = self.output_config.follow_lines

        if step > 0:
            # "Follow Lines" mode: Sliding window
            self._flush_follow_mode(step)
        else:
            # Standard paging mode
            self._flush_paging_mode()

        self.line_buffer.clear()

    def _pack_from(self, start: int, active_count: int) -> tuple:
        """Greedily pack buffered lines from `start` into one slide that fits
        max_visual_px (always taking at least one line). The first `active_count`
        lines are measured at their active/highlight height (they render larger in
        follow-lines mode); the rest at their base height. Returns (indices, next_index)."""
        slide_grp = []
        used_h = 0
        k = start
        while k < len(self.line_buffer):
            idx, base_h, active_h, _ = self.line_buffer[k]
            h = active_h if (k - start) < active_count else base_h
            if used_h + h > self.max_visual_px and used_h > 0:
                break
            slide_grp.append(idx)
            used_h += h
            k += 1
        return slide_grp, k

    def _flush_follow_mode(self, step: int):
        """Flush buffer using sliding window logic for follow lines mode."""
        curr_start = 0
        while curr_start < len(self.line_buffer):
            # Determine active line count
            start_verse = self.line_buffer[curr_start][3]

            actual_active_count = 0
            for k in range(curr_start, min(curr_start + step, len(self.line_buffer))):
                if self.output_config.prevent_mixed_active and self.line_buffer[k][3] != start_verse:
                    break
                actual_active_count += 1

            if actual_active_count == 0:
                actual_active_count = 1

            # Build full slide content starting from curr_start. The window advances
            # by the active count (overlapping slides), not by how many lines fit. The
            # first actual_active_count lines render highlighted (and larger), so pack
            # them at their active height to keep the slide within the box.
            slide_grp, _ = self._pack_from(curr_start, actual_active_count)
            if slide_grp:
                self.groups.append({'indices': slide_grp, 'active_count': actual_active_count})

            curr_start += actual_active_count

    def _flush_paging_mode(self):
        """Flush buffer using standard paging logic."""
        curr_start = 0
        while curr_start < len(self.line_buffer):
            # Paging mode has no highlighted/enlarged lines, so every line packs at its
            # base height (active_count = 0).
            slide_grp, k = self._pack_from(curr_start, 0)
            if slide_grp:
                self.groups.append({'indices': slide_grp, 'active_count': len(slide_grp)})
            else:
                k += 1  # defensive: never stall if a line couldn't be placed
            curr_start = k

    def get_groups(self) -> list[dict]:
        """Get the generated slide groups."""
        return self.groups


# The author-forced slide break: a line consisting solely of this marker flushes the
# current slide so the next line begins a new one.
_FORCED_SPLIT = '[--}{--]'


def _prepend_title_line(all_lines, verse_indices, groups, line_labels, verse_codes,
                         show_upcoming_lines: bool = False):
    """Prepend the virtual title slide to one output's computed line structures.

    The title is one blank logical line ('t1') forming its own slide group, so it
    rides the shared line cursor like any lyric line: every output pages onto slide 0
    together, and outputs whose theme resolves a title template paint it via
    slide_overlays[0] (the blank .box underneath is hidden by overlay-mode).

    Outputs without a template have nothing to paint over that blank box. When
    show_upcoming_lines is set (a follow-lines/confidence-monitor output with no
    template of its own), slide 0 instead reuses the exact line window the first
    real slide already computed, with active_count forced to 0 so every line renders
    dim/base-sized — the box shows the song's opening lines as upcoming rather than
    sitting empty, and never overflows since it's the same window that was already
    measured to fit. Outputs with follow_lines off keep the blank slide (nothing to
    show non-active, since they have no active/dim distinction at all)."""
    all_lines = [''] + all_lines
    line_labels = ['t1'] + line_labels
    verse_codes = ['t1'] + verse_codes
    verse_indices = [0] + [v + 1 for v in verse_indices]
    shifted_groups = [{'indices': [i + 1 for i in g['indices']], 'active_count': g['active_count']}
                      for g in groups]
    if show_upcoming_lines and shifted_groups:
        title_group = {'indices': shifted_groups[0]['indices'], 'active_count': 0, 'is_title': True}
    else:
        title_group = {'indices': [0], 'active_count': 1, 'is_title': True}
    groups = [title_group] + shifted_groups
    return all_lines, verse_indices, groups, line_labels, verse_codes


def _apply_title_upcoming(groups: list[dict]) -> None:
    """For title groups without their own overlay, reuse the next lyric window dimmed.

    Mutates title groups in place: indices become the following non-title group's
    indices with active_count 0 (follow-lines upcoming look). Title groups with no
    following lyric slide keep their blank line."""
    for i, g in enumerate(groups):
        if not g.get('is_title'):
            continue
        nxt = next((groups[j] for j in range(i + 1, len(groups)) if not groups[j].get('is_title')), None)
        if nxt:
            g['indices'] = list(nxt['indices'])
            g['active_count'] = 0


# A section code: verse-type prefix + number + one section letter (v1a, c2b, ...).
_SECTION_CODE_RE = re.compile(r'^([a-z]+\d+)([a-z])$')


def _base_verse_code(code: str) -> str:
    """The parent-verse code of a section code ('v1a' -> 'v1'); other codes unchanged.

    Sections (v1a/v1b/…) exist only to force slide breaks within one verse. For the
    verse indicator and verse-order tokens they collapse back to the parent verse, so
    'v1' selects/labels the whole verse regardless of how it's split (see
    _VerseParser._matches_token, which already treats a bare token this way)."""
    m = _SECTION_CODE_RE.match(code or '')
    return m.group(1) if m else (code or '')


def _same_verse_sections(code_a: str, code_b: str) -> bool:
    """True when two verse codes are lettered sections of the same verse (v1a/v1b).

    Sections exist to force a slide break in paging mode; in fluid mode they read
    as one continuous verse, so the verse gap between them is suppressed."""
    ma = _SECTION_CODE_RE.match(code_a or '')
    mb = _SECTION_CODE_RE.match(code_b or '')
    return bool(ma and mb and ma.group(1) == mb.group(1))


# Inline formatting tags that may span a balanced-wrap fragment boundary. Chords are a
# separate, cell-based structure and are not balanced yet (they keep the browser wrap).
_INLINE_TAG_RE = re.compile(r'</?([biu])\b[^>]*>', re.IGNORECASE)


def _track_inline_tags(word: str, open_tags: list[str]) -> None:
    """Update ``open_tags`` (a stack) with the <b>/<i>/<u> opens and closes inside ``word``."""
    for m in _INLINE_TAG_RE.finditer(word):
        name = m.group(1).lower()
        if m.group(0)[1] == '/':
            if name in open_tags:
                open_tags.remove(name)
        else:
            open_tags.append(name)


def _greedy_wrap_runs(plains: list[str], measure_func, max_width_px: float) -> list[tuple[int, int]]:
    """Greedy word-wrap ``plains`` into ``(start, end)`` runs — the same fill logic (and so the
    same line count) as ``wrap_plain_text_to_width``, but returning the break positions."""
    if not plains:
        return []
    runs = []
    start = 0
    line = [plains[0]]
    for i in range(1, len(plains)):
        if measure_func(' '.join(line + [plains[i]])) <= max_width_px:
            line.append(plains[i])
        else:
            runs.append((start, i))
            start, line = i, [plains[i]]
    runs.append((start, len(plains)))
    return runs


def _wrap_html_to_width(text: str, measure_func, max_width_px: float) -> tuple[str, int]:
    """Greedy-wrap one lyric line to ``max_width_px``, returning ``(html, line_count)`` with
    the breaks made explicit as ``<br/>``. A line that fits is returned unchanged (count 1).

    Greedy keeps each line as full as fits, so the top line is the longest and the last the
    shortest — the top-heavy shape wanted for evened lines. ``text`` may carry inline
    ``<b>/<i>/<u>`` formatting (not chords); any tag still open at a break is closed and
    re-opened across the ``<br/>`` so every fragment is valid, balanced markup.
    """
    words = text.split()
    if not words:
        return text, 1
    plains = [TAG_RE.sub('', w) for w in words]
    runs = _greedy_wrap_runs(plains, measure_func, max_width_px)
    if len(runs) <= 1:
        return text, 1

    # Open-tag stack at each word boundary: boundary[i] = tags open before word i.
    boundary: list[list[str]] = [[]]
    stack: list[str] = []
    for w in words:
        _track_inline_tags(w, stack)
        boundary.append(list(stack))

    fragments = []
    for a, b in runs:
        opener = ''.join(f'<{t}>' for t in boundary[a])
        closer = ''.join(f'</{t}>' for t in reversed(boundary[b]))
        fragments.append(opener + ' '.join(words[a:b]) + closer)
    return '<br/>'.join(fragments), len(fragments)


def _min_even_width(plain: str, measure_func, box_w: float) -> float:
    """Smallest width that still wraps ``plain`` into its box-minimum number of lines — the
    most-even target for a slide's widest line. Returns ``box_w`` when the line already fits
    one line (so evening is a no-op)."""
    stripped = TAG_RE.sub('', plain)
    n = len(wrap_plain_text_to_width(stripped, measure_func, box_w))
    if n <= 1:
        return box_w
    words = stripped.split()
    lo = max((measure_func(w) for w in words), default=box_w)   # can't be narrower than the widest word
    hi = box_w
    for _ in range(32):                                          # binary search: smallest W keeping n lines
        if hi - lo <= 0.5:
            break
        mid = (lo + hi) / 2
        if len(wrap_plain_text_to_width(stripped, measure_func, mid)) <= n:
            hi = mid
        else:
            lo = mid
    return hi


def _topheavy_even_width(plain: str, measure_func, box_w: float) -> float:
    """Even target for a slide's widest line, biased so the wrap is *top-heavy*: each line at
    least as wide as the one below it. This is the minimax split subject to non-increasing
    widths — the most even split that still keeps the top line the longest.

    Returns ``box_w`` when the line fits one line. Falls back to the plain most-even width when
    no top-heavy split exists at the minimum line count (e.g. a long trailing word forces the
    last line wider). A candidate target is a top-line prefix width; the smallest one that
    keeps the box-minimum line count and comes out non-increasing is the answer.
    """
    words = TAG_RE.sub('', plain).split()
    if len(words) < 2:
        return box_w
    n = len(_greedy_wrap_runs(words, measure_func, box_w))
    if n <= 1:
        return box_w
    for i in range(1, len(words)):
        target = measure_func(' '.join(words[:i]))              # top line = first i words
        if target > box_w:
            break
        runs = _greedy_wrap_runs(words, measure_func, target)
        if len(runs) != n:
            continue
        widths = [measure_func(' '.join(words[a:b])) for a, b in runs]
        if all(widths[j] + 0.5 >= widths[j + 1] for j in range(len(widths) - 1)):
            return target
    return _min_even_width(plain, measure_func, box_w)


def _search_even_target(ideal, box_w, cap, slide_height):
    """Smallest target width in [ideal, box_w] whose slide still fits ``cap``. box_w is the
    baseline (always fits) and ideal the fullest evening; narrower is taller, so once ideal
    overflows, binary-search between them."""
    if slide_height(ideal) <= cap:
        return ideal
    lo, hi = ideal, box_w
    for _ in range(32):
        if hi - lo <= 0.5:
            break
        mid = (lo + hi) / 2
        if slide_height(mid) <= cap:
            hi = mid
        else:
            lo = mid
    return hi


def _even_one_slide(idxs, all_lines, line_meta, measure_func, box_w, cap, strength):
    """Even one slide's wrappable lines within the vertical slack it already has. No-op if
    the slide has no evenable line, nothing wraps, or there's no free slack to spend."""
    evenable = [i for i in idxs if line_meta[i][3]]
    if not evenable:
        return

    # The widest wrappable line sets the even target (top-heavy: top line stays the
    # longest). Other lines fill greedily to the same target.
    widest = max(evenable, key=lambda i: measure_func(TAG_RE.sub('', line_meta[i][0])))
    ideal = _topheavy_even_width(line_meta[widest][0], measure_func, box_w)
    if ideal >= box_w:
        return                                                  # nothing on this slide wraps

    def slide_height(target: float) -> float:
        total = 0.0
        for i in idxs:
            plain, line_h, extra_px, ev = line_meta[i]
            width = target if ev else box_w
            lines = len(wrap_plain_text_to_width(TAG_RE.sub('', plain), measure_func, width))
            total += lines * line_h + extra_px
        return total

    target = _search_even_target(ideal, box_w, cap, slide_height)

    # `target` is the fullest evening this slide's slack allows. Ease it back toward box_w
    # by the user's strength (1.0 = full, 0 = none): a wider target evens less. Widening is
    # always safe — a wider target wraps to fewer/shorter lines, so it still fits the cap.
    target = box_w - strength * (box_w - target)
    if target >= box_w - 0.5:
        return                                                  # no slack to spend — leave as-is

    for i in evenable:
        all_lines[i] = _wrap_html_to_width(line_meta[i][0], measure_func, target)[0]


def _even_slide_lines(groups, all_lines, line_meta, measure_func, box_w, cap, strength=1.0):
    """Post-grouping pass: even the widths of each slide's wrappable lines using only the
    vertical slack that slide already has. Lines never move between slides, so this cannot add
    a slide — it just spends free rows to make a wrapped stanza read evenly (top-heavy).

    ``line_meta[i]`` is ``(plain, line_height_px, extra_px, evenable)`` for ``all_lines[i]``.
    Chorded lines are not evenable (they keep the browser wrap) but still count toward height.

    ``strength`` (0.0-1.0) scales the aggressiveness: 1.0 evens as fully as the slide's free
    space allows, lower values ease the wrap target back toward the natural greedy wrap.
    """
    for grp in groups:
        _even_one_slide(grp['indices'], all_lines, line_meta, measure_func, box_w, cap, strength)


class _LineLayout:
    """Font/geometry context for measuring one output's logical lines while grouping them
    into slides. Holds the base and highlight (follow-lines) font measurers and the usable
    box dimensions, and provides the per-line metric helpers the grouping loop calls.
    Built once per _compute_line_groups run."""

    def __init__(self, output_config):
        self.oc = output_config
        self.measure_func, self.line_height = _get_font_measurement(
            output_config.font_family, output_config.font_size,
            output_config.font_bold, output_config.font_italic)
        self.avail_w = max(1, output_config.width_px - 2 * output_config.area_padding)
        self.avail_h = max(1, output_config.height_px - 2 * output_config.area_padding)

        # In follow-lines mode the active lines render at highlight_font_size when it's set.
        # When that's larger than the base font, those lines are taller (and may wrap wider)
        # than the base measurement — so measure them separately and pack the slide's active
        # lines at this height, otherwise enlarged active text overflows the box.
        self.hl_enabled = (output_config.follow_lines > 0
                           and output_config.highlight_font_size > 0
                           and output_config.highlight_font_size != output_config.font_size)
        if self.hl_enabled:
            self.measure_func_hl, self.line_height_hl = _get_font_measurement(
                output_config.font_family, output_config.highlight_font_size,
                output_config.font_bold, output_config.font_italic)
        else:
            self.measure_func_hl, self.line_height_hl = self.measure_func, self.line_height

    def visual_lines(self, lyric_text, mf=None):
        """How many visual lines a logical line wraps to at the given measurement (base font
        by default; pass the highlight measurer for active lines).

        Markup is stripped before measuring — the inline tags (`<b>`/`<i>`/`<u>`) and chord
        spans aren't painted text, so counting their characters would over-wrap. Chords also
        sit *above* the lyric and never widen it beyond the syllables they cover, so neither
        affects the wrap count.
        """
        plain = TAG_RE.sub('', lyric_text).replace('\n', '')
        wrapped = wrap_plain_text_to_width(plain, mf or self.measure_func, self.avail_w)
        return max(1, len(wrapped))

    def sized_metrics(self, lyric_text, base_size):
        """(visual_line_count, height_factor) for a line containing <size=NN> runs.

        Greedy word-wrap where each word is measured at its run's scaled font; words
        spanning a size boundary sum their fragments' widths. The line-box height follows
        the tallest run (browser behavior), so a line whose runs are all smaller than 100%
        packs tighter and an enlarged run grows it."""
        runs = _size_runs(lyric_text.replace('\n', ''))
        if not runs:
            return 1, 1.0
        max_scale = max(s for _, s in runs)
        pieces = []          # (word_width_px,) — words merged across run boundaries
        carry = 0.0
        carrying = False
        for text, scale in runs:
            mf = _get_font_measurement(self.oc.font_family,
                                       max(1, round(base_size * scale / 100)),
                                       self.oc.font_bold, self.oc.font_italic)[0]
            parts = text.split(' ')
            for j, part in enumerate(parts):
                if j > 0 and carrying:
                    pieces.append(carry)
                    carry, carrying = 0.0, False
                if part:
                    carry += mf(part)
                    carrying = True
        if carrying:
            pieces.append(carry)
        if not pieces:
            return 1, max_scale / 100.0
        space_w = self.measure_func(' ') or self.measure_func('n n') - self.measure_func('nn')
        count, cur = 1, 0.0
        for wd in pieces:
            cand = wd if cur == 0 else cur + space_w + wd
            if cur > 0 and cand > self.avail_w:
                count += 1
                cur = wd
            else:
                cur = cand
        return count, max_scale / 100.0

    def active_px(self, lyric_text, has_chords, extra_px, base_px):
        """Rendered height of a line when it's an active (highlighted) line. In follow mode
        with a highlight font size this is larger than the base height (bigger line height,
        and possibly more wrap lines); otherwise it matches the base height."""
        if not self.hl_enabled:
            return base_px
        if not has_chords and '<size=' in lyric_text.lower():
            v, f = self.sized_metrics(lyric_text, self.oc.highlight_font_size)
            return int(math.ceil(v * self.line_height_hl * f)) + extra_px
        eff_lh = self.line_height_hl * 1.8 if has_chords else self.line_height_hl
        return self.visual_lines(lyric_text, self.measure_func_hl) * eff_lh + extra_px


class _GroupAccumulator:
    """Collects the parallel per-line arrays a slide rebuild produces (the flat HTML lines,
    their labels, verse indices, and render metadata) and feeds each finished line to the
    grouper. Centralizes the append so the line's grouper index stays in lockstep with the
    arrays without a separate running counter.

    line_meta[i] is (plain, line_height_px, extra_px, evenable) for all_lines[i]; the
    post-grouping evening pass reads it to recompute a line's height at a candidate width
    and re-wrap it. evenable excludes chorded lines."""

    def __init__(self, grouper):
        self.grouper = grouper
        self.all_lines: list[str] = []
        self.line_labels: list[str] = []
        self.verse_indices: list[int] = []
        self.line_meta: list[tuple] = []

    def emit(self, html_line, meta, v_idx, label, base_px, active_px):
        """Append one paged line and register it with the grouper for slide packing."""
        self.grouper.add_line(len(self.all_lines), base_px, active_px, v_idx)
        self._append(html_line, meta, v_idx, label)

    def emit_title(self, meta, v_idx):
        """Append the virtual title line as its own finished, single-line slide group."""
        self.grouper.groups.append({
            'indices': [len(self.all_lines)], 'active_count': 1, 'is_title': True
        })
        self._append('', meta, v_idx, 't1')

    def _append(self, html_line, meta, v_idx, label):
        self.all_lines.append(html_line)
        self.line_meta.append(meta)
        self.verse_indices.append(v_idx)
        self.line_labels.append(label)


def _emit_logical_line(raw_line, li_idx, v_idx, verse_label, verse_codes, output_config, lay, acc):
    """Render, measure, and emit one logical lyric line into the accumulator, splitting an
    over-tall line across sub-slides. No-op for a blank lyric line (a chord-only line still
    renders). Geometry/measurement come from ``lay`` (see _LineLayout)."""
    measure_func = lay.measure_func
    line_height = lay.line_height
    avail_w = lay.avail_w
    max_visual_px = lay.avail_h

    # The lyric text alone (no chord markers) — used for measurement and as the plain
    # fallback. Chords are rendered above these syllables, not inline.
    plain_lyric = re.sub(r' +', ' ', _ChordProcessor.strip_chords(raw_line)).strip()

    # Render chords above the lyric, or fall back to plain lyric text (which the browser
    # wraps; the post-grouping pass may later even those wraps).
    has_chords = output_config.show_chords and _ChordProcessor.has_chords(raw_line)
    html_line = _ChordProcessor.render(raw_line) if has_chords else plain_lyric

    # Inline <size=NN> runs change both the wrap point and the line-box height, so sized
    # lines get run-aware metrics (chorded lines keep the base approximation — chords
    # already dominate their height).
    has_sizes = not has_chords and '<size=' in plain_lyric.lower()
    if has_sizes:
        vis_lines, size_factor = lay.sized_metrics(plain_lyric, output_config.font_size)
    else:
        vis_lines = lay.visual_lines(plain_lyric)
        size_factor = 1.0

    # Skip blank lines (a chord-only line still renders; a blank lyric does not).
    if not html_line:
        return

    # Effective line height (chords need more vertical space; a sized line follows its runs).
    effective_line_height = line_height * 1.8 if has_chords else line_height
    if size_factor != 1.0:
        effective_line_height = int(math.ceil(line_height * size_factor))

    # Verse gap on the first line of a verse (fluid mode). No gap between lettered sections
    # of the same verse — they read as one verse.
    extra_px = 0
    if (output_config.fluid_slides and li_idx == 0 and v_idx > 0
            and output_config.verse_gap > 0
            and not _same_verse_sections(verse_codes[v_idx - 1], verse_codes[v_idx])):
        extra_px = output_config.verse_gap

    total_px = vis_lines * effective_line_height + extra_px

    if total_px <= max_visual_px:
        # Line fits normally. Sized lines are not evenable — the evening pass re-wraps at
        # the base font, which their runs don't use.
        active_px = lay.active_px(plain_lyric, has_chords, extra_px, total_px)
        acc.emit(html_line,
                 (plain_lyric, effective_line_height, extra_px, not has_chords and not has_sizes),
                 v_idx, verse_label, total_px, active_px)
        return

    # Too tall: split on the plain lyric text (chords are dropped on this rare overflow
    # path); the resulting sub-lines are plain lyric and may be evened like any other line.
    sub_lines = split_line_to_fit(plain_lyric, measure_func, effective_line_height,
                                  avail_w, max_visual_px - extra_px, is_html=False)
    for part_idx, sub_line in enumerate(sub_lines):
        sub_extra_px = extra_px if part_idx == 0 else 0
        total_px_sub = lay.visual_lines(sub_line) * effective_line_height + sub_extra_px
        active_px_sub = lay.active_px(sub_line, has_chords, sub_extra_px, total_px_sub)
        if len(sub_lines) > 1:
            split_label = f"{verse_label} ({part_idx + 1}/{len(sub_lines)})" if verse_label else f"Part {part_idx + 1}/{len(sub_lines)}"
        else:
            split_label = verse_label
        acc.emit(sub_line, (sub_line, effective_line_height, sub_extra_px, True),
                 v_idx, split_label, total_px_sub, active_px_sub)


def _compute_line_groups(lyrics_text, output_config, verse_order=None, suppress_titles=False):
    """
    Compute logical line list and how they are grouped into slides for an output.

    suppress_titles drops baked Title sections (see _VerseParser.parse_verses) —
    set when a theme-driven title slide will be injected for this rebuild.

    Returns:
        (all_lines, verse_indices, groups, line_labels, verse_codes) where:
        - all_lines: flat list of HTML strings (one per logical line)
        - verse_indices: verse index for each line
        - groups: list of dicts with {'indices': [...], 'active_count': int}
        - line_labels: label for each line (e.g., 'v1', 'c1')
        - verse_codes: list of verse codes in order
    """
    # Font/geometry context + per-line metric helpers for this output (built once).
    lay = _LineLayout(output_config)
    # Thin aliases so the grouping loop, grouper, and evening pass below read unchanged.
    measure_func = lay.measure_func
    line_height = lay.line_height
    avail_w = lay.avail_w
    max_visual_px = lay.avail_h

    # Parse verses and verse codes
    verses, verse_codes = _VerseParser.parse_verses(lyrics_text, verse_order, suppress_titles)

    # Slide grouper + the accumulator that collects the parallel per-line arrays and hands
    # each finished line to the grouper.
    grouper = _SlideGrouper(output_config, max_visual_px)
    acc = _GroupAccumulator(grouper)

    # Process each verse
    for v_idx, verse in enumerate(verses):
        verse_label = verse_codes[v_idx] if v_idx < len(verse_codes) else ''
        # Virtual title slide: empty body, one blank line, its own slide group.
        # Theme overlays paint over this position; without a template it stays blank
        # (or shows upcoming lines after groups are finalized in _rebuild_slides_song).
        if _TITLE_CODE_RE.match(verse_label or ''):
            grouper.flush_buffer()
            acc.emit_title(('', line_height, 0, False), v_idx)
            continue

        logical_lines = verse.split('\n')

        # Flush buffer at verse boundary if not in fluid mode
        if not output_config.fluid_slides:
            grouper.flush_buffer()

        for li_idx, raw_line in enumerate(logical_lines):
            # Forced split marker: flush the current slide group and skip (it affects
            # grouping, not a line).
            if raw_line.strip() == _FORCED_SPLIT:
                grouper.flush_buffer()
                continue
            _emit_logical_line(raw_line, li_idx, v_idx, verse_label, verse_codes,
                               output_config, lay, acc)

    # Final flush and get groups
    grouper.flush_buffer()
    groups = grouper.get_groups()

    # Even out wrapped-line widths within each slide's existing free space (never adds a
    # slide). Skipped in follow-lines mode, where the active line renders at a size these
    # base-font breaks weren't measured for (deferred, like chord-aware evening).
    # A blank UI field can arrive as None/non-numeric; fall back to the default rather than
    # crashing live output. 0 disables evening as surely as the toggle being off.
    raw_strength = output_config.balance_wrapped_strength
    if not isinstance(raw_strength, (int, float)):
        raw_strength = 100
    balance_strength = max(0.0, min(1.0, raw_strength / 100.0))
    if output_config.balance_wrapped_lines and balance_strength > 0 and output_config.follow_lines <= 0:
        _even_slide_lines(groups, acc.all_lines, acc.line_meta, measure_func, avail_w, max_visual_px,
                          balance_strength)

    return acc.all_lines, acc.verse_indices, groups, acc.line_labels, verse_codes


# ---------------------- Application state ----------------------

class PlayerController:
    """Handles cursor navigation and slide position tracking."""

    def __init__(self, app_state):
        self.app_state = app_state
        self._line_cursor = 0
        self._total_lines = 0
        self._all_lines: list[str] = []  # logical lines for UI display
        self._all_line_labels: list[str] = []

    @staticmethod
    def _slide_for_line(oc, line: int) -> int:
        """The slide an output shows at a given logical line, or 0 when it has no map
        for that line. A rebuild gives every output a line_to_slide of length
        _total_lines; this stays bounds-safe if one ever isn't, so a single output's
        malformed map can't crash next/prev for all of them (the cursor is shared)."""
        l2s = oc.line_to_slide
        return l2s[line] if l2s and 0 <= line < len(l2s) else 0

    def _find_next_change_line(self, start: int, direction: int) -> Optional[int]:
        """Next line where any output changes slides (direction: 1 or -1), or None."""
        if not self.app_state.outputs or self._total_lines <= 0:
            return None

        cur_slides = [self._slide_for_line(oc, start) for oc in self.app_state.outputs]
        candidate = start + direction

        while 0 <= candidate < self._total_lines:
            cand_slides = [self._slide_for_line(oc, candidate) for oc in self.app_state.outputs]
            if any(cand_slides[i] != cur_slides[i] for i in range(len(self.app_state.outputs))):
                return candidate
            candidate += direction

        return None

    def _set_line_cursor(self, line_index: int) -> None:
        """Update the line cursor and all output indices."""
        self._line_cursor = line_index
        for oc in self.app_state.outputs:
            if oc.line_to_slide and 0 <= line_index < len(oc.line_to_slide):
                oc.index = oc.line_to_slide[line_index]

    def next_slide(self):
        """Advance line-by-line, skipping lines that don't change any output."""
        if not self.app_state.outputs or self._total_lines <= 0:
            return False
        if self._line_cursor >= self._total_lines - 1:
            return False

        target_line = self._find_next_change_line(self._line_cursor, 1)
        if target_line is None:
            return False

        self._set_line_cursor(target_line)
        return True

    def prev_slide(self):
        """Move backwards line-by-line."""
        if not self.app_state.outputs or self._total_lines <= 0:
            return False
        if self._line_cursor <= 0:
            return False

        target_line = self._find_next_change_line(self._line_cursor, -1)
        if target_line is None:
            target_line = 0

        self._set_line_cursor(target_line)
        return True

    def jump_to_line(self, line_index: int):
        """Jump to a specific logical line index."""
        if not self.app_state.outputs or line_index < 0 or line_index >= self._total_lines:
            return False
        self._set_line_cursor(line_index)
        return True


class ConfigurationManager:
    """Handles configuration persistence in the database (app_settings + outputs tables)."""

    def __init__(self, app_state):
        self.app_state = app_state

    @property
    def db(self):
        return self.app_state.db

    def _apply_config(self, data: dict):
        """Populate AppState from a loaded config dict."""
        self.app_state.outputs = [OutputConfig.from_dict(d) for d in data.get('outputs', [])]
        self._migrate_clock_to_bg_themes()

        # export_dir: resolve the stored value to a location that is valid for *this*
        # OS. A relative value (the normal case) is taken relative to the per-user data
        # dir. A stored absolute path is honoured only when it is a real absolute path
        # on this platform that points outside the read-only install dir; otherwise it
        # is treated as stale and re-resolved to the default, so relocating the data dir
        # (or carrying the config to another OS) never strands the exports.
        #
        # The case the naive "if not isabs(): join(data_dir, value)" logic got wrong: a
        # value saved on another platform, e.g. a Linux "/home/<user>/.../web_export".
        # On Windows os.path.isabs() is False for it (no drive letter), and
        # os.path.join(data_dir, "/home/...") silently re-roots it onto the current
        # drive ("C:\\home\\..."), pointing both the static mount and the exporter at a
        # bogus directory instead of %APPDATA%.
        data_dir = get_data_dir()
        default_export_dir = os.path.join(data_dir, 'web_export')
        loaded_export_dir = data.get('export_dir') or 'web_export'
        candidate = os.path.normpath(os.path.join(data_dir, loaded_export_dir))
        if _path_is_within(candidate, data_dir):
            # Relative value (or any path that stays inside the data dir).
            self.app_state.export_dir = candidate
        elif os.path.isabs(loaded_export_dir) and not _path_is_within(loaded_export_dir, get_base_dir()):
            # A genuine custom absolute path on this OS, outside the install dir.
            self.app_state.export_dir = loaded_export_dir
        else:
            # Stale install-relative path, or a foreign-OS path this platform does not
            # recognise as absolute (e.g. a Linux "/home/..." value on Windows).
            self.app_state.export_dir = default_export_dir

        self.app_state.bundle_local_fonts = bool(data.get('bundle_local_fonts', False))
        self.app_state.ccli_licence_number = data.get('ccli_licence_number', '')
        self.app_state.preview_video_mode = data.get('preview_video_mode', 'still')
        self.app_state.theme_priority = normalize_theme_priority(data.get('theme_priority'))
        # Active style profile (validated/repaired against the profiles table by
        # StyleProfileManager.ensure_seeded after load).
        self.app_state.active_profile_id = data.get('active_profile_id')

    def _migrate_clock_to_bg_themes(self):
        """Carry legacy per-output clock settings into background themes.

        The wall clock used to be an intrinsic output field; it is now part of the
        background theme. For configs saved before that move, from_dict() still loads
        the old top-level clock values onto the dataclass, but the existing background
        themes don't carry them — so seed each bg theme's style from the output's
        clock values when a clock key is missing. Idempotent and self-healing: once a
        theme has the keys (here or via a save) this is a no-op. Outputs with no bg
        themes yet are skipped — _seed_default_themes() builds their first theme from
        the same field values, so the clock is preserved there too.
        """
        for oc in self.app_state.outputs:
            bg_themes = getattr(oc, 'bg_themes', None)
            if not isinstance(bg_themes, list):
                continue
            for theme in bg_themes:
                style = theme.get('style') if isinstance(theme, dict) else None
                if not isinstance(style, dict):
                    continue
                for key in CLOCK_KEYS:
                    style.setdefault(key, getattr(oc, key))

    def load_config(self):
        """Load persistent configuration from the database."""
        try:
            data = self.db.load_app_settings()
            data['outputs'] = self.db.load_output_configs()
            self._apply_config(data)
        except Exception as e:
            logger.warning("Failed to load config: %s", e, exc_info=True)

    def save_config(self):
        """Persist configuration to the database."""
        try:
            # Store export_dir relative to the data dir when it lives under it, so the
            # value stays portable across machines and data-dir relocations.
            data_dir = get_data_dir()
            ed = self.app_state.export_dir
            export_dir_value = os.path.relpath(ed, data_dir) if _path_is_within(ed, data_dir) else ed
            self.db.save_app_settings({
                'export_dir': export_dir_value,
                'bundle_local_fonts': bool(self.app_state.bundle_local_fonts),
                'ccli_licence_number': self.app_state.ccli_licence_number,
                'preview_video_mode': self.app_state.preview_video_mode,
                'theme_priority': normalize_theme_priority(self.app_state.theme_priority),
                'active_profile_id': self.app_state.active_profile_id,
            })
            self.db.save_output_configs([oc.to_persist_dict() for oc in self.app_state.outputs])
        except Exception as e:
            logger.warning("Failed to save config: %s", e, exc_info=True)

    def set_output_order(self, names: list) -> bool:
        """
        Reorder outputs to match the given list of output names (used by
        drag-and-drop in the previews grid). Names not present are ignored;
        any current outputs missing from `names` keep their relative order at
        the end, so a stale/partial list can never drop an output. Returns
        True only if the resulting order actually differs.
        """
        outs = self.app_state.outputs
        by_name = {o.name: o for o in outs}
        seen = set()
        ordered = []
        for nm in (names or []):
            o = by_name.get(nm)
            if o is not None and nm not in seen:
                seen.add(nm)
                ordered.append(o)
        # Append anything the caller didn't mention, preserving current order.
        for o in outs:
            if o.name not in seen:
                ordered.append(o)
        if [o.name for o in ordered] == [o.name for o in outs]:
            return False
        outs[:] = ordered  # mutate in place so existing references stay valid
        self.save_config()
        return True


class StyleProfileManager:
    """Owns switching between named *style profiles* — saved snapshots of theme
    assignments the user can flip between (see the style_profiles table comment in
    database.py).

    A profile captures three assignment stores that together form the resolvable look:
      - each output's per-category default themes (OutputConfig.category_defaults — the
        'global' cascade tier),
      - each library song's theme_map (songs table), and
      - each library announcement item's theme_map (ann_items table).

    The live tables always mirror the *active* profile, so the ThemeResolver and every
    existing read path are untouched. Switching = capture the current live state into the
    active profile, then apply the target profile's blob onto the live tables. Service
    theme_maps and per-service-item snapshot theme_maps are intentionally NOT part of a
    profile, keeping services insulated from a profile switch while the 'global' tier a
    profile owns still cascades into them.

    All methods here are synchronous (DB + in-memory only); the async endpoints run the
    DB-heavy ones off the event loop and own the rebuild/broadcast tail.
    """

    def __init__(self, app_state):
        self.app_state = app_state

    @property
    def db(self):
        return self.app_state.db

    def snapshot_live(self) -> dict:
        """Capture the current live assignment stores into a profile `data` blob.
        Only non-empty item theme_maps are stored (empty means 'follow the output
        default' either way, so omitting them keeps snapshots compact)."""
        outputs = {oc.name: copy.deepcopy(oc.category_defaults or {})
                   for oc in self.app_state.outputs}
        songs = {str(s['id']): s['theme_map']
                 for s in self.db.get_all_songs_summary() if s.get('theme_map')}
        ann_items = {str(a['id']): a['theme_map']
                     for a in self.db.get_ann_items() if a.get('theme_map')}
        return {'outputs': outputs, 'songs': songs, 'ann_items': ann_items}

    def apply_snapshot(self, data: dict):
        """Apply a profile `data` blob onto the live tables.

        Outputs use *keep-current-if-absent* (an output the profile never knew about
        inherits the current defaults — its defaults are never blanked). Library items
        use *set-or-empty* (an item the profile never knew about is reset to no override,
        so it cleanly falls back to the output category default the profile carries).
        Only rows whose value actually changes are written."""
        data = data or {}

        outs = data.get('outputs') or {}
        for oc in self.app_state.outputs:
            ent = outs.get(oc.name)
            if isinstance(ent, dict):
                oc.category_defaults = copy.deepcopy(ent)

        songs_snap = data.get('songs') or {}
        song_deltas = {}
        for s in self.db.get_all_songs_summary():
            desired = songs_snap.get(str(s['id'])) or {}
            if (s.get('theme_map') or {}) != desired:
                song_deltas[s['id']] = desired
        self.db.set_songs_theme_maps(song_deltas)

        items_snap = data.get('ann_items') or {}
        item_deltas = {}
        for a in self.db.get_ann_items():
            desired = items_snap.get(str(a['id'])) or {}
            if (a.get('theme_map') or {}) != desired:
                item_deltas[a['id']] = desired
        self.db.set_ann_items_theme_maps(item_deltas)

    def capture_active(self):
        """Persist the current live state into the currently-active profile. The live
        tables are the source of truth while a profile is active, so the active profile's
        stored blob is refreshed lazily here (before switching away, on delete, etc.)."""
        aid = self.app_state.active_profile_id
        if aid:
            self.db.save_style_profile_data(aid, self.snapshot_live())

    def activate(self, target_id) -> bool:
        """Switch the active profile: capture the current live state into the old active
        profile, then apply the target onto the live tables. Returns False if the target
        does not exist. The caller persists config and rebuilds/broadcasts."""
        prof = self.db.get_style_profile(target_id)
        if not prof:
            return False
        if self.app_state.active_profile_id != target_id:
            self.capture_active()
        self.apply_snapshot(prof.get('data') or {})
        self.app_state.active_profile_id = target_id
        self.app_state.config_manager.save_config()
        return True

    def create_blank(self, name) -> int:
        """Create a profile that carries the current per-output defaults (so outputs keep
        rendering) but has no per-library-item overrides — every song/announcement falls
        back to its output's category default until the user sets it."""
        live = self.snapshot_live()
        data = {'outputs': live['outputs'], 'songs': {}, 'ann_items': {}}
        return self.db.create_style_profile(name, data)

    def duplicate(self, source_id, name) -> Optional[int]:
        """Create a full copy of an existing profile's assignments (the 'clone the look'
        path). Duplicating the active profile captures the live state first so the copy
        reflects unsaved edits."""
        if source_id == self.app_state.active_profile_id:
            self.capture_active()
        src = self.db.get_style_profile(source_id)
        if not src:
            return None
        return self.db.create_style_profile(name, copy.deepcopy(src.get('data') or {}))

    def ensure_seeded(self):
        """Guarantee exactly one valid active profile exists. First run (no profiles):
        snapshot the current live look into a 'Default' profile and activate it, so
        existing installs are unchanged. Otherwise repair a missing/stale
        active_profile_id to the first profile."""
        profs = self.db.get_style_profiles()
        if not profs:
            pid = self.db.create_style_profile('Default', self.snapshot_live())
            self.app_state.active_profile_id = pid
            self.app_state.config_manager.save_config()
            return
        if not any(p['id'] == self.app_state.active_profile_id for p in profs):
            self.app_state.active_profile_id = profs[0]['id']
            self.app_state.config_manager.save_config()


class ThemeResolver:
    """Resolves an output's effective themes across the three tiers (content, service,
    global) in the user-configured priority order — see _resolve_effective_theme_ids."""

    def __init__(self, app_state):
        self.app_state = app_state
        # Memo for DB theme-map lookups inside cached_lookups() only.
        self._lookup_cache = None

    @contextmanager
    def cached_lookups(self):
        """Memoize the service/song theme-map lookups for the duration of one
        slide-rebuild pass.

        Both lookups hit the DB but are loop-invariant across a rebuild (they
        depend only on the live selection, which a rebuild never changes), yet
        they used to run once per output for style resolution and again per
        output for title overlays — 2-4×N identical queries on the hottest path
        in the app. The cache must not outlive the pass: the maps change
        whenever the operator selects different content."""
        self._lookup_cache = {}
        try:
            yield
        finally:
            self._lookup_cache = None

    def _cached(self, key, compute):
        """Return compute() memoized under `key` while a cached_lookups() block
        is active; a plain passthrough outside one."""
        cache = self._lookup_cache
        if cache is None:
            return compute()
        if key not in cache:
            cache[key] = compute()
        return cache[key]

    def _get_current_service_theme_map(self) -> dict:
        return self._cached('service_map', self._compute_service_theme_map)

    def _compute_service_theme_map(self) -> dict:
        if self.app_state.current_mode != 'service':
            return {}
        sid = self.app_state.current_service_id
        if not sid or sid == -1:
            return {}
        try:
            svc = self.app_state.db.get_service(sid)
        except Exception:
            svc = None
        if not svc:
            return {}
        return svc.get('theme_map') or {}

    def _get_current_song_theme_map(self) -> dict:
        return self._cached('song_map', self._compute_song_theme_map)

    def _compute_song_theme_map(self) -> dict:
        """Content-tier theme_map for the cascade.

        Standalone library play reads the live library row. In-service song items use
        their snapshot's theme_map only — never a live get_song() — so library theme
        edits cannot change a planned service. song_id is provenance for Reset only.
        """
        mode = self.app_state.current_mode
        if mode == 'song':
            sid = self.app_state.current_song_id
            if not sid:
                return {}
            try:
                song = self.app_state.db.get_song(sid)
            except Exception:
                song = None
            return (song.get('theme_map') or {}) if song else {}

        if mode == 'service':
            itm = self.app_state.current_service_item()
            if itm is None or itm.get('item_type') != 'song':
                return {}
            # Snapshot already applied onto the item as theme_map.
            return itm.get('theme_map') or {}

        return {}

    @staticmethod
    def _find_theme_in(theme_list, theme_id: str) -> Optional[dict]:
        """Find a theme by ID within a given theme list (text_themes or bg_themes)."""
        if not theme_id:
            return None
        return next((t for t in (theme_list or [])
                     if isinstance(t, dict) and t.get('id') == theme_id), None)

    def _get_active_category(self) -> str:
        """Determine the rendering category for theme resolution."""
        mode = self.app_state.current_mode
        if mode in ('song', 'bible'):
            return mode
        # Standalone announcement sent live from the library (current_mode ==
        # 'announcement') resolves its themes against the announcement category
        # defaults, same as an in-service announcement item.
        if mode == 'announcement':
            return 'announcement'
        if mode == 'service':
            itm = self.app_state.current_service_item()
            if itm is not None:
                it = itm.get('item_type')
                if it == 'bible':
                    return 'bible'
                if it == 'announcement':
                    return 'announcement'
        # Songs, images, videos and fallbacks all use the song-category defaults
        return 'song'

    def _theme_priority(self) -> list:
        """The active theme-cascade tier order (highest→lowest priority), a validated
        permutation of THEME_PRIORITY_TIERS. Defends against a malformed live setting
        by falling back to the default order."""
        return normalize_theme_priority(getattr(self.app_state, 'theme_priority', None))

    def _resolve_effective_theme_ids(self, oc: 'OutputConfig') -> tuple:
        """Select an output's effective (text_id, bg_id) theme ids by walking the three
        theme tiers — content (song / service-item / standalone announcement),
        service, and the output's per-category default ('global') — in the user's
        configured priority order (AppState.theme_priority). Each field (text, bg) is
        resolved independently: the highest-priority tier that sets it wins, and a
        tier that leaves it unset is transparent to it. The default order
        content > service > global reproduces the original most-specific-wins cascade."""
        category = self._get_active_category()
        cat_def = (oc.category_defaults or {}).get(category) or {}

        # Gather each tier's per-output {text, bg} entry.
        svc_map = self._get_current_service_theme_map()

        skip_song_theme = False
        si_map = {}
        if self.app_state.current_mode == 'service':
            itm = self.app_state.current_service_item()
            if itm is not None:
                si_map = itm.get('theme_map') or {}
                # Song items carry their full theme_map in the snapshot (content tier
                # via _compute_song_theme_map). Other item types (bible, announcement)
                # use si_map as the content-tier override; skip the song tier for them.
                if itm.get('item_type') == 'song':
                    # Avoid double-applying the same map at both song and service-item
                    # layers: song tier holds the snapshot; si_map stays empty here.
                    si_map = {}
                    skip_song_theme = False
                else:
                    skip_song_theme = True
        elif self.app_state.current_mode == 'announcement':
            # Standalone announcement: its own per-output theme_map is the content
            # tier of the cascade (mirrors the in-service item above).
            si_map = (self.app_state.current_ann_data or {}).get('theme_map') or {}
            skip_song_theme = True

        song_map = {} if skip_song_theme else self._get_current_song_theme_map()

        svc_entry = svc_map.get(oc.name) if isinstance(svc_map, dict) else None
        si_entry = si_map.get(oc.name) if isinstance(si_map, dict) else None
        song_entry = song_map.get(oc.name) if isinstance(song_map, dict) else None

        order = self._theme_priority()

        def resolve(field: str):
            for tier in order:
                v = self._theme_tier_value(tier, field, cat_def, svc_entry, si_entry, song_entry)
                if v:
                    return v
            return None

        return resolve('text'), resolve('bg')

    @staticmethod
    def _theme_tier_value(tier, field, cat_def, svc_entry, si_entry, song_entry):
        """One tier's value for a theme field (text/bg), or None if that tier leaves it
        unset. Within 'content', a service-item override still wins over the song's own
        theme (unchanged from the historical behaviour)."""
        if tier == 'global':
            return cat_def.get(field)
        if tier == 'service':
            return svc_entry.get(field) if isinstance(svc_entry, dict) else None
        if isinstance(si_entry, dict) and si_entry.get(field):
            return si_entry.get(field)
        if isinstance(song_entry, dict) and song_entry.get(field):
            return song_entry.get(field)
        return None

    def resolve_text_theme(self, oc: 'OutputConfig') -> Optional[dict]:
        """The full text-theme dict this output is currently rendering with (same
        cascade as style resolution), or None. Lets the slide builder read non-style
        theme content such as the title-slide template reference."""
        return self._find_theme_in(oc.text_themes, self._resolve_effective_theme_ids(oc)[0])

    def _resolve_effective_style_for_output(self, oc: 'OutputConfig') -> dict:
        """Resolve the effective style for an output by selecting one text theme and
        one background theme through the cascade
        (output category default < service < song < service item), then combining
        their complete style dicts on top of the output's intrinsic fields."""
        text_id, bg_id = self._resolve_effective_theme_ids(oc)

        # Start from dataclass defaults for completeness, then layer themes.
        eff = oc.style_to_dict() or {}
        text_theme = self._find_theme_in(oc.text_themes, text_id)
        if text_theme and isinstance(text_theme.get('style'), dict):
            eff.update(text_theme['style'])
        bg_theme = self._find_theme_in(oc.bg_themes, bg_id)
        if bg_theme and isinstance(bg_theme.get('style'), dict):
            eff.update(bg_theme['style'])

        return {k: v for k, v in eff.items() if k in OUTPUT_STYLE_KEYS}

    def default_style_for_output(self, oc: 'OutputConfig', category: str = 'song') -> dict:
        """Resolve an output's static default style (its category default text + bg
        themes, ignoring live service/song/item overrides). Used to bake the initial
        output.html so it reflects the user's configured look, not dataclass defaults."""
        cat = (oc.category_defaults or {}).get(category) or {}
        eff = oc.style_to_dict() or {}
        tt = self._find_theme_in(oc.text_themes, cat.get('text')) or (oc.text_themes or [None])[0]
        bt = self._find_theme_in(oc.bg_themes, cat.get('bg')) or (oc.bg_themes or [None])[0]
        if isinstance(tt, dict) and isinstance(tt.get('style'), dict):
            eff.update(tt['style'])
        if isinstance(bt, dict) and isinstance(bt.get('style'), dict):
            eff.update(bt['style'])
        return {k: v for k, v in eff.items() if k in OUTPUT_STYLE_KEYS}

    @staticmethod
    def _extract_background_images_from_style(style: dict) -> List[str]:
        """Extract background image URLs from a style dictionary (both the normal
        background and any title-slide background override)."""
        images = []
        if style.get('background_type') == 'image' and style.get('background_image'):
            images.append(style['background_image'])
        if style.get('title_background_type') == 'image' and style.get('title_background_image'):
            images.append(style['title_background_image'])
        return images

    def _collect_service_background_images(self) -> List[str]:
        """
        Collect every background image URL that any background theme could apply
        during the current service, for preloading to prevent flicker. Background
        themes are few per output, so we simply gather all image backgrounds across
        every output's background-theme library.
        """
        if self.app_state.current_mode != 'service':
            return []
        images = set()
        for oc in self.app_state.outputs:
            for bt in (oc.bg_themes or []):
                if isinstance(bt, dict):
                    images.update(self._extract_background_images_from_style(bt.get('style') or {}))
        return list(images)


_VALIGN_FLEX = {'top': 'flex-start', 'bottom': 'flex-end'}
_VALIGN_CSS  = {'top': 'start',      'bottom': 'end'}
_HALIGN_FLEX = {'left': 'flex-start', 'right': 'flex-end'}

def _valign_to_css(valign: str, flex_mode: bool = False) -> str:
    if flex_mode:
        return _VALIGN_FLEX.get(valign, 'center')
    return _VALIGN_CSS.get(valign, 'center')

def _align_to_css(align: str, flex_mode: bool = False) -> str:
    if flex_mode:
        return _HALIGN_FLEX.get(align, 'center')
    return align  # 'left', 'right', 'center' map directly to text-align


def _esc_html_text(s: str) -> str:
    """Escape a string for HTML text content (the exported page's <title>)."""
    return str(s or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def _bg_params(bg_type: str, bg_color: str, bg_image: str) -> tuple:
    """Return (bg, initial_bg_a, initial_bg_key) for the given background settings."""
    if bg_type == 'image' and bg_image:
        safe_url = safe_css_url(bg_image)
        if safe_url:
            # Double-quoted CSS url() so a stray apostrophe in a legacy path cannot
            # break out; safe_css_url already rejects quotes and unsafe schemes.
            return 'transparent', f'background-image:url("{safe_url}");', f"i:{safe_url}"
        return 'transparent', '', 't'
    if bg_type == 'color':
        color = safe_css_color(bg_color, default='#000000')
        return color, f"background-color:{color};", f"c:{color}"
    return 'transparent', '', 't'


def _title_bg_override(style: dict) -> Optional[dict]:
    """The background_* overlay a background theme applies to a song's title slide,
    or None when the theme leaves the title slide on its normal background.

    Reads the theme's title_background_* fields (see BG_THEME_KEYS); 'inherit' — the
    default — means no override. Returns a dict of exactly the three background_*
    render keys so it can be merged onto a resolved style for one slide."""
    t = (style or {}).get('title_background_type') or 'inherit'
    if t == 'inherit':
        return None
    return {
        'background_type': t,
        'background_color': (style or {}).get('title_background_color') or '#000000',
        'background_image': (style or {}).get('title_background_image', '') if t == 'image' else '',
    }


class HtmlExporter:
    """Handles HTML generation for web display outputs."""

    def __init__(self, app_state):
        self.app_state = app_state

    def export_outputs(self):
        """Generate HTML wrapper files for all outputs."""
        if not os.path.exists(self.app_state.export_dir):
            os.makedirs(self.app_state.export_dir, exist_ok=True)

        self.app_state.bundled_font_css_map = {}

        for oc in self.app_state.outputs:
            initial = oc.slides[oc.index] if oc.slides and 0 <= oc.index < len(oc.slides) else ''

            bundled_fonts_css = self.app_state.font_manager._bundle_fonts_for_output(oc)
            self.app_state.bundled_font_css_map[oc.name] = bundled_fonts_css

            # Bake the output's default-theme style into the static template so the
            # initial render matches the user's configured look (not dataclass
            # defaults). Restored in the finally below — restoring only on the happy
            # path would leave the output's live runtime style corrupted (baked with
            # the default theme) if the template build ever raised.
            _export_orig_style = oc.style_to_dict()
            try:
                oc.apply_style_dict(self.app_state.theme_resolver.default_style_for_output(oc))

                # Compute initial background for double-buffered bg layers
                bg, initial_bg_a, initial_bg_key = _bg_params(
                    oc.background_type, oc.background_color, oc.background_image)

                # Animated-background config baked for first paint, so the bar is mounted
                # (and shown/hidden to match initial content) before the WebSocket connects
                # — no flash of un-barred lyrics. Substituted as a single JSON literal, so
                # its braces don't disturb str.format's placeholder scan.
                initial_anim_json = json.dumps({
                    'background_type': oc.background_type,
                    'background_anim_preset': oc.background_anim_preset,
                    'background_anim_color': oc.background_anim_color,
                    'background_anim_accent': oc.background_anim_accent,
                    'background_anim_opacity': oc.background_anim_opacity,
                    'background_anim_height': oc.background_anim_height,
                    'background_anim_duration': oc.background_anim_duration,
                    'background_anim_gap': oc.background_anim_gap,
                    'background_anim_inset': oc.background_anim_inset,
                    'background_anim_radius': oc.background_anim_radius,
                })

                htmlpage = HTML_TEMPLATE.format(
                    # <title> is HTML text: the name sanitizer strips <>, but & (and any
                    # legacy pre-sanitizer name) must still be escaped for markup.
                    title=f"Lyrics - {_esc_html_text(oc.name)}",
                    bg=bg, fg=safe_css_color(oc.highlight_color),
                    initial_bg_a=initial_bg_a, initial_bg_key=initial_bg_key,
                    canvas_w=oc.canvas_width, canvas_h=oc.canvas_height,
                    box_x=oc.box_x, box_y=oc.box_y, box_w=oc.width_px, box_h=oc.height_px,
                    pad=oc.area_padding, font_family=safe_font_family(oc.font_family), font_size=oc.font_size,
                    text_weight='bold' if oc.font_bold else 'normal',
                    text_style='italic' if oc.font_italic else 'normal',
                    initial=initial,
                    # Baked into a single-quoted JS string used as a query-string value
                    # (see the WebSocket URL in output.html). Percent-encoding makes it
                    # safe in both contexts at once: an apostrophe would otherwise end
                    # the JS string (SyntaxError — dead output page) and an ampersand
                    # would truncate the output_name query param, orphaning the client
                    # from its broadcast packets. The server decodes it transparently.
                    output_name=urllib.parse.quote(oc.name, safe=''),
                    initial_anim_json=initial_anim_json,
                    enable_fade='true' if oc.enable_fade else 'false',
                    fade_duration=oc.fade_duration,
                    align=safe_text_align(oc.align),
                    bundled_fonts_css=bundled_fonts_css,

                    ind_x=oc.indicator_x,
                    ind_y=oc.indicator_y,
                    ind_fs=oc.indicator_font_size,
                    ind_color='#ffffff',
                    ind_opacity=str(oc.indicator_opacity) if oc.show_indicator else '0',

                    # Wall-clock overlay (from the default bg theme applied above; the
                    # format flags seed the JS ticker, then applyStyle keeps it live)
                    clock_x=oc.clock_x,
                    clock_y=oc.clock_y,
                    clock_fs=oc.clock_font_size,
                    clock_font_family=safe_font_family(oc.clock_font_family or oc.font_family),
                    clock_color=safe_css_color(oc.clock_color),
                    clock_opacity='1' if oc.show_clock else '0',
                    clock_on='true' if oc.show_clock else 'false',
                    clock_seconds='true' if oc.clock_seconds else 'false',
                    clock_24h='true' if oc.clock_24h else 'false',

                    text_opacity=oc.text_opacity,
                    valign_css=_valign_to_css(oc.valign),

                    # Bible Text Box
                    bible_text_box_x=oc.bible_text_box_x,
                    bible_text_box_y=oc.bible_text_box_y,
                    bible_text_box_w=oc.bible_text_box_width,
                    bible_text_box_h=oc.bible_text_box_height,
                    bible_text_pad=oc.bible_text_padding,
                    bible_text_color=safe_css_color(oc.bible_text_color),
                    bible_text_align=safe_text_align(oc.bible_text_align),
                    bible_text_valign_css=_valign_to_css(oc.bible_text_valign),
                    bible_text_opacity=str(oc.bible_text_opacity) if oc.show_bible_text else '0',
                    bible_main_font_family=safe_font_family(oc.bible_main_font_family),
                    bible_main_font_size=oc.bible_main_font_size,
                    bible_text_weight='bold' if oc.bible_main_font_bold else 'normal',
                    bible_text_style='italic' if oc.bible_main_font_italic else 'normal',

                    # Bible Reference Box
                    bible_ref_x=oc.bible_ref_box_x,
                    bible_ref_y=oc.bible_ref_box_y,
                    bible_ref_w=oc.bible_ref_width,
                    bible_ref_h=oc.bible_ref_height,
                    bible_ref_font_family=safe_font_family(oc.bible_ref_font_family),
                    bible_ref_font_size=oc.bible_ref_font_size,
                    bible_ref_weight='bold' if oc.bible_ref_font_bold else 'normal',
                    bible_ref_style='italic' if oc.bible_ref_font_italic else 'normal',
                    bible_ref_color=safe_css_color(oc.bible_ref_color),
                    bible_ref_opacity=str(oc.bible_ref_opacity) if oc.show_bible_ref else '0',
                    bible_ref_justify=_align_to_css(oc.bible_ref_align, flex_mode=True),
                    bible_ref_valign_css=_valign_to_css(oc.bible_ref_valign, flex_mode=True),

                    # Copyright Info Box
                    copyright_x=oc.copyright_box_x,
                    copyright_y=oc.copyright_box_y,
                    copyright_w=oc.copyright_box_width,
                    copyright_h=oc.copyright_box_height,
                    copyright_font_family=safe_font_family(oc.copyright_font_family),
                    copyright_font_size=oc.copyright_font_size,
                    copyright_weight='bold' if oc.copyright_font_bold else 'normal',
                    copyright_style='italic' if oc.copyright_font_italic else 'normal',
                    copyright_color=safe_css_color(oc.copyright_color),
                    copyright_opacity=str(oc.copyright_text_opacity) if oc.show_copyright else '0',
                    copyright_align=safe_text_align(oc.copyright_align),
                    copyright_valign_css=_valign_to_css(oc.copyright_valign),

                    # Video settings
                    video_enabled='true' if oc.video_enabled else 'false',
                    countdown_x=oc.video_countdown_x,
                    countdown_y=oc.video_countdown_y,
                    countdown_fs=oc.video_countdown_font_size,
                    countdown_font_family=safe_font_family(oc.video_countdown_font_family),
                    countdown_weight='bold' if oc.video_countdown_font_bold else 'normal',
                    countdown_style='italic' if oc.video_countdown_font_italic else 'normal',
                    countdown_color=safe_css_color(oc.video_countdown_color),
                    countdown_align=safe_text_align(oc.video_countdown_align),
                    countdown_opacity='1' if oc.show_video_countdown else '0',
                    video_x=oc.video_area_x,
                    video_y=oc.video_area_y,
                    video_w=oc.video_area_width if oc.video_area_width > 0 else oc.canvas_width,
                    video_h=oc.video_area_height if oc.video_area_height > 0 else oc.canvas_height,
                    # Image settings
                    image_enabled='true' if oc.image_enabled else 'false',
                    image_x=oc.image_area_x,
                    image_y=oc.image_area_y,
                    image_w=oc.image_area_width if oc.image_area_width > 0 else oc.canvas_width,
                    image_h=oc.image_area_height if oc.image_area_height > 0 else oc.canvas_height,
                    image_fit=oc.image_fit)
            finally:
                # Restore the output's runtime style whether or not the bake succeeded.
                oc.apply_style_dict(_export_orig_style)

            fname = f"{oc.name}.html"
            dest = os.path.join(self.app_state.export_dir, fname)
            # Defence in depth: names are sanitized on save (_sanitize_output_name),
            # but contain the write anyway so a legacy/unsanitized name can never place
            # the file outside the export dir.
            if not _path_is_within(dest, self.app_state.export_dir):
                logger.error("Refusing to export output %r: name escapes export dir", oc.name)
                continue
            try:
                with open(dest, 'w', encoding='utf-8') as f:
                    f.write(htmlpage)
            except Exception as e:
                logger.error("Error exporting output %s: %s", oc.name, e, exc_info=True)


# Built-in variables usable in template box lines and field text as {name}
# tokens, filled from the song on title slides (empty for non-song items). A
# template's own field names join the same token namespace (fields win on
# collision). Names are user-facing syntax: keep this tuple, the chip list in
# admin.js (_TMPL_VARIABLES) and the manual in sync.
_TEMPLATE_VARIABLES = ('song-title', 'songbook', 'songbook-number',
                       'authors', 'key', 'copyright', 'ccli-number')
# A token is any brace-delimited run without braces/newlines; names are matched
# trimmed and case-insensitively so {Title} and {title} are the same token.
_VARIABLE_TOKEN_RE = re.compile(r'\{([^{}\n]+)\}')


def _token_key(name: str) -> str:
    return str(name or '').strip().casefold()


def _substitute_variables(text: str, values: dict) -> str:
    """Fill {token} placeholders in template text. `values` is keyed by
    _token_key; an entry may exist with an empty value (known but absent).

    The line is the unit of composition: a line that uses tokens renders only
    when every token on it has a value, so decoration never dangles (a
    "{songbook} - #{songbook-number}" line vanishes as a whole for a song without
    a number, rather than rendering "Hymnal - #"). Literal-only lines always
    render. Unknown token names are left as typed — a typo shows on screen where
    it can be seen and fixed, instead of silently disappearing.
    """
    out = []
    for line in str(text or '').split('\n'):
        keys = {_token_key(m.group(1)) for m in _VARIABLE_TOKEN_RE.finditer(line)}
        known = {k for k in keys if k in values}
        if known:
            if any(not str(values.get(k) or '').strip() for k in known):
                continue
            line = _VARIABLE_TOKEN_RE.sub(
                lambda m: str(values[_token_key(m.group(1))]).strip()
                if _token_key(m.group(1)) in values else m.group(0),
                line)
        out.append(line)
    return '\n'.join(out)


def _ann_base_context(song_vars: dict) -> dict:
    """Base token context: every built-in variable present (empty unless the song
    supplies it). Field/slot values are layered on top by the callers below."""
    ctx = {_token_key(name): '' for name in _TEMPLATE_VARIABLES}
    for name, val in (song_vars or {}).items():
        ctx[_token_key(name)] = str(val or '')
    return ctx


def _ann_order_context(song_vars: dict, slot_names: list, values: list) -> dict:
    """Token context for a v2 announcement: built-ins overlaid by the resolved
    layout's slots, filled by ORDER — the item's field value i fills slot i. Values
    may contain built-in tokens, substituted against the base first."""
    ctx = _ann_base_context(song_vars)
    base = dict(ctx)
    for i, name in enumerate(slot_names or []):
        key = _token_key(name)
        if key:
            raw = values[i] if i < len(values) else ''
            ctx[key] = _substitute_variables(raw, base)
    return ctx


def _resolve_ann_layout_id(oc, item_theme_map):
    """The layout an announcement item uses on one output: the layout assigned to
    that output in the item's per-output map (theme_map[output]['layout']), or None
    when unassigned. There is no default layout — an unassigned announcement renders
    blank on that output. `oc` supplies the output name the assignment is keyed by."""
    return ((item_theme_map or {}).get(oc.name) or {}).get('layout')


_ANN_VALIGN_FLEX = {'top': 'flex-start', 'middle': 'center', 'bottom': 'flex-end'}


def _cssnum(v) -> str:
    """Format a numeric CSS value compactly (10 not 10.0, 33.333 not 33.33333…)."""
    return f'{round(float(v), 3):g}'


def _ann_box_html(box: dict, token_values: dict) -> str:
    """Render one layout box: a positioned flow container whose lines stack in
    order, each at a size relative to the box's base font. A line whose tokens
    resolve empty drops out of the flow (see _substitute_variables), and the
    remaining lines anchor per the box's vertical alignment — so a title line
    stays visually centered when the subtitle line beneath it has nothing to
    show. A box with no visible lines renders nothing."""
    lines_html = []
    for ln in box.get('lines', []):
        text = _substitute_variables(ln.get('text', ''), token_values)
        if not text.strip():
            continue
        style = f"font-size:{int(ln.get('scale') or 100)}%;"
        if ln.get('bold'):
            style += 'font-weight:bold;'
        if ln.get('italic'):
            style += 'font-style:italic;'
        if ln.get('color'):
            style += f"color:{safe_css_color(ln['color'])};"
        style += 'white-space:pre-wrap;overflow-wrap:break-word;word-break:break-word;'
        lines_html.append(f'<div style="{style}">{_escape_rich_text(text)}</div>')
    if not lines_html:
        return ''
    justify = _ANN_VALIGN_FLEX.get(box.get('vertical_align', 'middle'), 'center')
    ff = safe_font_family(box.get('font_family', 'Helvetica'))
    # line-height is a unitless multiplier (leading within a wrapped line, and the
    # baseline spacing between lines); line_gap is extra post-line space as a % of
    # the base font, rendered as flex `gap` so it collapses when a line drops out.
    # Both arrive pre-clamped from _normalize_ann_boxes; guard defensively anyway.
    try:
        line_height = max(0.8, min(3.0, float(box.get('line_height') or 1.15)))
    except (TypeError, ValueError):
        line_height = 1.15
    try:
        line_gap = max(0, min(300, int(box.get('line_gap') or 0)))
    except (TypeError, ValueError):
        line_gap = 0
    gap_css = f"gap:{_cssnum(line_gap / 100)}em;" if line_gap else ''
    # Geometry is canvas pixels (unified with text-theme boxes) — the overlay
    # layer spans the fixed-size canvas, which output.html scales as a whole.
    font_color = safe_css_color(box.get('font_color', '#ffffff'))
    text_align = safe_text_align(box.get('text_align', 'center'))
    container_style = (
        f"position:absolute;left:{_cssnum(box.get('x', 0))}px;top:{_cssnum(box.get('y', 0))}px;"
        f"width:{_cssnum(box.get('w', 100))}px;height:{_cssnum(box.get('h', 100))}px;"
        f"display:flex;flex-direction:column;justify-content:{justify};{gap_css}"
        f"font-family:'{ff}';font-size:{int(box.get('font_size') or 48)}px;"
        f"line-height:{_cssnum(line_height)};"
        f"color:{font_color};text-align:{text_align};"
        f"overflow:hidden;box-sizing:border-box;"
    )
    return f'<div style="{container_style}">{"".join(lines_html)}</div>'


def _build_template_ann_html(layout: dict, token_values: dict) -> str:
    """Build the full-canvas text-box overlay for a template-based announcement
    or title slide.

    The slide background is supplied by the output's background layer (driven by
    the resolved background theme), so this overlay is intentionally transparent
    and only renders the template's boxes on top. `token_values` carries the
    resolved token context (built-ins + field values — see `_ann_order_context`
    / `_ann_base_context`)."""
    parts = [_ann_box_html(box, token_values) for box in layout.get('text_boxes', [])]
    return ('<div style="position:absolute;inset:0;background-color:transparent;overflow:hidden;">'
            f'{"".join(parts)}</div>')


def _item_data(item) -> dict:
    """Parse a service item's JSON ``data`` blob into a dict, returning {} on a missing
    item, missing/empty data, or malformed JSON. Consolidates the try/json.loads/{}
    dance the no-slide rebuilds and content resolvers each repeated."""
    if not item:
        return {}
    try:
        parsed = json.loads(item.get('data') or '{}')
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


class SlideBuilder:
    """Handles core slide generation logic for songs, Bible verses, and announcements."""

    def __init__(self, app_state):
        self.app_state = app_state

    def get_current_song(self):
        if self.app_state.current_song_lyrics:
            return types.SimpleNamespace(lyrics=self.app_state.current_song_lyrics)
        return None

    def _restore_styles(self, originals: dict):
        """Re-apply each output's pre-rebuild ('original') style, returning outputs to
        their base defaults after a per-item rebuild temporarily applied effective styles."""
        for oc in self.app_state.outputs:
            try:
                oc.apply_style_dict(originals.get(oc.name) or {})
            except Exception:
                # A failed restore leaves this output with the transient effective
                # style baked into its runtime config — log it so the corruption is
                # diagnosable rather than silent.
                logger.exception("Failed to restore base style for output %r", oc.name)

    def _rebuild_slides_bible(self, data: dict, originals: dict):
        """
        Build slides for Bible verses.

        Args:
            data: Bible data dict containing bible_id, book, chapter, verse_start, verse_end
            originals: Original output styles to restore after processing
        """
        chapter = data.get('chapter')
        verses_list = self._fetch_bible_verses(data)
        bible_outputs = [oc for oc in self.app_state.outputs if oc.show_bible_text and not oc.is_ignored]

        # Split each verse into chunks sized for the most restrictive output, so every
        # output shares identical chunks (keeps text synchronized across outputs).
        verse_chunks = [
            {'verse_num': v['verse_num'],
             'chunks': self._chunk_bible_verse(chapter, v['verse_num'], v['text'], bible_outputs)}
            for v in verses_list
        ]

        # Build plain_lines and line_labels based on unified chunks
        plain_lines = []
        line_labels = []
        for vc in verse_chunks:
            for chunk_text in vc['chunks']:
                plain_lines.append(chunk_text)
                line_labels.append(f"{chapter}:{vc['verse_num']}")

        for oc in bible_outputs:
            self._build_bible_slides_for_output(oc, verse_chunks, chapter)

        # Every output that isn't rendering this passage's text still needs a
        # line_to_slide of length total_lines. bible_outputs excludes two kinds of
        # output: ignored ones, and ones whose effective theme resolved
        # show_bible_text=False. Neither gets its slides rebuilt above, so without this
        # they carry a stale mapping from the previous item. The shared line cursor
        # indexes *every* output's line_to_slide (see PlayerController._find_next_change_line),
        # so a stale map shorter than total_lines makes next/prev raise IndexError —
        # which surfaces as Bible arrows silently doing nothing. Reset them to empty
        # slides (clearing stale verse indicators too), exactly like the song path.
        total_lines = len(plain_lines)
        bible_output_ids = {id(oc) for oc in bible_outputs}
        for oc in self.app_state.outputs:
            if id(oc) not in bible_output_ids:
                self._set_empty_slides(oc, total_lines)

        # Update app state
        self.app_state.player._total_lines = total_lines
        self.app_state.player._all_lines = plain_lines
        self.app_state.player._all_line_labels = line_labels
        self.app_state.player._line_cursor = 0

        # Restore global output styles
        self._restore_styles(originals)

    def _fetch_bible_verses(self, data: dict):
        """Resolve the verse list from bible data: a range query, or legacy single-verse text."""
        bible_id = data.get('bible_id')
        book = data.get('book')
        chapter = data.get('chapter')
        v_start = data.get('verse_start')
        v_end = data.get('verse_end')
        if bible_id and book and chapter and v_start is not None and v_end is not None:
            all_verses = self.app_state.db.get_bible_verses(bible_id, book, chapter)
            return [v for v in all_verses if v_start <= v['verse_num'] <= v_end]
        if data.get('text'):
            # Legacy single verse support
            return [{'verse_num': data.get('verse_num'), 'text': data.get('text')}]
        return []

    @staticmethod
    def _chunk_bible_verse(chapter, v_num, v_text, bible_outputs):
        """Split one verse into text chunks sized to fit the most restrictive output."""
        full_measure_text = f"{chapter}:{v_num} " + v_text

        # Find the most restrictive output (smallest available vertical space)
        min_space = float('inf')
        most_restrictive_oc = None
        for oc in bible_outputs:
            _, line_height = _get_font_measurement(oc.bible_main_font_family, oc.bible_main_font_size)
            avail_h = max(1, oc.bible_text_box_height - 2 * oc.bible_text_padding)
            max_lines = int(avail_h / line_height)
            if max_lines < min_space:
                min_space = max_lines
                most_restrictive_oc = oc

        if not most_restrictive_oc:
            return [full_measure_text]

        measure_func, line_height = _get_font_measurement(
            most_restrictive_oc.bible_main_font_family, most_restrictive_oc.bible_main_font_size,
            most_restrictive_oc.bible_main_font_bold, most_restrictive_oc.bible_main_font_italic)
        avail_w = max(1, most_restrictive_oc.bible_text_box_width - 2 * most_restrictive_oc.bible_text_padding)
        avail_h = max(1, most_restrictive_oc.bible_text_box_height - 2 * most_restrictive_oc.bible_text_padding)

        wrapped = wrap_plain_text_to_width(full_measure_text, measure_func, avail_w)
        if len(wrapped) * line_height > avail_h:
            return split_line_to_fit(full_measure_text, measure_func, line_height, avail_w, avail_h, is_html=False)
        return [full_measure_text]

    @staticmethod
    def _build_bible_slides_for_output(oc, verse_chunks, chapter):
        """Build one output's slides by greedily combining shared verse chunks that fit together."""
        measure_func, line_height = _get_font_measurement(
            oc.bible_main_font_family, oc.bible_main_font_size,
            oc.bible_main_font_bold, oc.bible_main_font_italic)
        avail_w = max(1, oc.bible_text_box_width - 2 * oc.bible_text_padding)
        avail_h = max(1, oc.bible_text_box_height - 2 * oc.bible_text_padding)

        oc_slides = []
        line_to_slide = []

        for vc in verse_chunks:
            v_num = vc['verse_num']
            verse_chunk_list = vc['chunks']
            prefix = f"{chapter}:{v_num} "

            local_chunk_idx = 0
            while local_chunk_idx < len(verse_chunk_list):
                slide_text = verse_chunk_list[local_chunk_idx]
                consumed_count = 1

                # Try to combine with subsequent chunks
                for next_idx in range(local_chunk_idx + 1, len(verse_chunk_list)):
                    test_text = slide_text + " " + verse_chunk_list[next_idx]
                    wrapped_test = wrap_plain_text_to_width(test_text, measure_func, avail_w)
                    if len(wrapped_test) * line_height <= avail_h:
                        slide_text = test_text
                        consumed_count += 1
                    else:
                        break

                # Format the slide text
                final_text = slide_text
                if oc.show_bible_verse_numbers:
                    # Replace plain prefix with styled span
                    if local_chunk_idx == 0 and final_text.startswith(prefix):
                        final_text = final_text.replace(prefix, f'<span class="verse-num">{chapter}:{v_num}</span>', 1)
                else:
                    # Strip the prefix if verse numbers are disabled
                    if local_chunk_idx == 0 and final_text.startswith(prefix):
                        final_text = final_text[len(prefix):]

                oc_slides.append(final_text)

                # Map all consumed chunks to this slide
                slide_idx = len(oc_slides) - 1
                for _ in range(consumed_count):
                    line_to_slide.append(slide_idx)

                local_chunk_idx += consumed_count

        oc.slides = oc_slides if oc_slides else ['']
        oc.line_to_slide = line_to_slide
        oc.index = 0

    def _rebuild_slides_template_ann(self, item_data: dict, originals: dict):
        """Announcement service item → one full-canvas overlay per output.

        Items carry `fields` and resolve a per-output layout from the ann_layouts
        library (order-based — field i fills the layout's slot i). It is the
        single-slide case of the per-slide overlay list; the background is supplied
        by the resolved background theme via each output's background layer."""
        overlays = self._ann_overlays_v2(item_data)
        for oc in self.app_state.outputs:
            html = overlays.get(oc.name)
            oc.slide_overlays = [html] if html else []
            oc.slides = ['']
            oc.line_to_slide = [0]
            oc.index = 0
            oc.verse_codes = []
            oc.verse_indices = []

        self.app_state.player._total_lines = 1
        self.app_state.player._all_lines = ['']
        self.app_state.player._all_line_labels = ['AN']
        self.app_state.player._line_cursor = 0

        self._restore_styles(originals)

    def _ann_overlays_v2(self, item_data: dict) -> dict:
        """{output_name: overlay_html} for an announcement. Each output uses the
        layout assigned to it in the item's per-output map; the item's field values
        fill that layout's slots by order. Outputs with no assigned layout are omitted
        (the announcement shows blank there)."""
        fields = item_data.get('fields') or []
        values = [f.get('value', '') for f in fields]
        theme_map = item_data.get('theme_map') or {}
        overlays = {}
        for oc in self.app_state.outputs:
            if oc.is_ignored:
                continue
            lid = _resolve_ann_layout_id(oc, theme_map)
            layout = self.app_state.db.get_ann_layout(lid) if lid else None
            if layout:
                ctx = _ann_order_context({}, layout.get('slot_names', []), values)
                overlays[oc.name] = _build_template_ann_html(layout, ctx)
        return overlays

    def _current_song_variables(self) -> dict:
        """The {variable} values of the song being rebuilt (keys from
        _TEMPLATE_VARIABLES). Prefers the service-item snapshot in service mode;
        falls back to the library row for standalone play, then in-memory title.
        """
        if self.app_state.current_mode == 'service':
            itm = self.app_state.current_service_item()
            if itm is not None and itm.get('item_type') == 'song':
                return {
                    'song-title': itm.get('title') or '',
                    'songbook': itm.get('songbook_name') or '',
                    'songbook-number': str(itm.get('songbook_entry') or ''),
                    'authors': ', '.join(a for a in (itm.get('authors') or []) if a),
                    'key': itm.get('key') or '',
                    'copyright': itm.get('copyright') or '',
                    'ccli-number': str(itm.get('ccli_song_number') or ''),
                }

        sid = self.app_state.current_song_id
        row = self.app_state.db.get_song(sid) if sid else None
        if not row:
            return {'song-title': self.app_state.current_song_title or ''}
        return {
            'song-title': row.get('title') or '',
            'songbook': row.get('songbook_name') or '',
            'songbook-number': str(row.get('songbook_entry') or ''),
            'authors': ', '.join(a for a in (row.get('authors') or []) if a),
            'key': row.get('key') or '',
            'copyright': row.get('copyright') or '',
            'ccli-number': str(row.get('ccli_song_number') or ''),
        }

    def _resolve_title_overlays(self, active_outputs, verse_order) -> Optional[dict]:
        """Decide whether this song rebuild gets a title slide, and build its overlay
        HTML per output. Returns {output_name: overlay_html_or_''} when active, else
        None.

        The song opts in implicitly when it has no verse_order (its written order is
        the de facto order, title first); an explicit verse_order participates only
        if it contains a title token (t1). The decision is then global: if any active
        output's resolved text theme carries a title layout, every output gets the
        slide position — those without one show it blank, keeping the shared line
        cursor in step."""
        tokens = (verse_order or '').lower().split()
        if verse_order and not any(_TITLE_CODE_RE.match(t) for t in tokens):
            return None

        overlays = {}
        song_vars = None
        for oc in active_outputs:
            theme = self.app_state.theme_resolver.resolve_text_theme(oc)
            # The title-slide layout lives INSIDE the text theme (overhaul phase F4):
            # {'text_boxes': [...]} in canvas px, self-contained — no ann_layout
            # lookup. Legacy title_layout_id pointers are embedded at startup by
            # _migrate_title_slides_embedded and on save by _apply_title_slide_ref.
            boxes = _theme_title_boxes(theme)
            if not boxes:
                continue
            if song_vars is None:
                song_vars = self._current_song_variables()
            # A title slide has no item filling it — its boxes reference song
            # variables directly; unknown tokens resolve empty (their lines drop).
            ctx = _ann_order_context(song_vars, [], [])
            overlays[oc.name] = _build_template_ann_html({'text_boxes': boxes}, ctx)
        return overlays or None

    def _rebuild_slides_song(self, song, verse_order, is_announcement, originals: dict):
        """
        Build slides for songs and announcements.

        Args:
            song: Song object with lyrics attribute
            verse_order: Optional verse ordering string
            is_announcement: Whether this is an announcement
            originals: Original output styles to restore after processing
        """
        # Filter to only enabled outputs for announcements; ignored outputs are excluded from pagination
        active_outputs = [oc for oc in self.app_state.outputs if (not is_announcement or oc.show_announcements) and not oc.is_ignored]

        # Theme-driven title slides: decided once per rebuild, before pagination, so
        # every output agrees on the slide positions. When active, baked Title
        # sections are suppressed (overlays replace them). An empty verse_order
        # still prepends one virtual title; an explicit order with t1 tokens already
        # carries virtual title verses from parse_verses / _compute_line_groups.
        title_overlays = self._resolve_title_overlays(active_outputs, verse_order) \
            if not is_announcement else None
        # Explicit order already embeds title verses; only prepend for implicit title.
        prepend_title = bool(title_overlays is not None and not (verse_order or '').strip())

        # First pass: compute line groups for all outputs to find max line count
        output_data = []
        all_lines_master = None
        all_line_labels_master = None
        total_lines = 0

        for oc in active_outputs:
            data = self._compute_output_line_data(oc, song, verse_order, title_overlays, prepend_title)
            output_data.append(data)
            all_lines = data['all_lines']
            if all_lines_master is None or len(all_lines) > len(all_lines_master):
                all_lines_master = all_lines
                all_line_labels_master = data['line_labels']
            total_lines = max(total_lines, len(all_lines))

        # Second pass: build slides and line_to_slide mappings
        for data in output_data:
            self._build_song_slides_for_output(data, total_lines, is_announcement)
            if title_overlays is not None:
                self._apply_title_slide_overlays(data, title_overlays)

        # Give disabled announcement outputs empty slides
        if is_announcement:
            for oc in self.app_state.outputs:
                if not oc.show_announcements:
                    self._set_empty_slides(oc, total_lines)

        # Give ignored outputs empty slides (they are excluded from pagination)
        for oc in self.app_state.outputs:
            if oc.is_ignored:
                self._set_empty_slides(oc, total_lines)

        self._update_player_line_controller(all_lines_master, all_line_labels_master,
                                            total_lines, title_overlays, is_announcement)

        # Restore global output styles
        self._restore_styles(originals)

    def _compute_output_line_data(self, oc, song, verse_order, title_overlays, prepend_title):
        """First pass for one output: compute its line groups, then apply the title-slide
        treatment. When a title slide is prepended, outputs with their own resolved template
        overlay don't need real content behind it (the overlay covers it); outputs without
        one show the song's opening lines dimmed instead of sitting blank, but only in
        follow-lines mode and when not opted out (show_upcoming). Returns the output_data dict."""
        all_lines, verse_indices, groups, line_labels, verse_codes = _compute_line_groups(
            song.lyrics, oc, verse_order, suppress_titles=title_overlays is not None)
        show_upcoming = (title_overlays is not None
                         and oc.follow_lines > 0 and oc.title_slide_show_lines
                         and not title_overlays.get(oc.name))
        if prepend_title:
            all_lines, verse_indices, groups, line_labels, verse_codes = _prepend_title_line(
                all_lines, verse_indices, groups, line_labels, verse_codes, show_upcoming)
        elif title_overlays is not None and show_upcoming:
            _apply_title_upcoming(groups)
        return {
            'oc': oc,
            'all_lines': all_lines,
            'verse_indices': verse_indices,
            'groups': groups,
            'line_labels': line_labels,
            'verse_codes': verse_codes,
        }

    def _apply_title_slide_overlays(self, data, title_overlays):
        """Second-pass, one output: give its title-slide positions the theme overlay (or ''
        when this output has no title layout) and an optional title background; non-title
        slides get '' / None."""
        oc = data['oc']
        overlay = title_overlays.get(oc.name, '')
        bg = _title_bg_override(oc.style_to_dict())
        n = len(oc.slides)
        groups = data['groups']
        oc.slide_overlays = [
            (overlay if (i < len(groups) and groups[i].get('is_title')) else '')
            for i in range(n)
        ]
        if bg:
            oc.slide_bg_overrides = [
                (bg if (i < len(groups) and groups[i].get('is_title')) else None)
                for i in range(n)
            ]
        else:
            oc.slide_bg_overrides = []

    def _update_player_line_controller(self, all_lines_master, all_line_labels_master,
                                       total_lines, title_overlays, is_announcement):
        """Update the admin line controller after a rebuild. The controller shows one row per
        logical line, so strip the per-output balance-wrap <br/>s (each replaced a space) and
        convert canonical <size=NN> tags to relative spans (same conversion the slides get);
        label virtual title rows with the song title, and mark announcement rows 'AN'."""
        self.app_state.player._total_lines = total_lines
        self.app_state.player._all_lines = [
            _convert_size_tags(ln.replace('<br/>', ' ')) for ln in all_lines_master] if all_lines_master else []
        if title_overlays is not None and self.app_state.player._all_lines and all_line_labels_master:
            # Virtual title lines are blank on the outputs; show the song title in the admin
            # line controller so the operator sees what each title slide is.
            t = (self.app_state.current_song_title or 'Title slide').replace('&', '&amp;').replace('<', '&lt;')
            for i, lab in enumerate(all_line_labels_master):
                if i < len(self.app_state.player._all_lines) and _TITLE_CODE_RE.match((lab or '').lower()):
                    self.app_state.player._all_lines[i] = t
        if is_announcement and all_line_labels_master:
            all_line_labels_master = ['AN'] * len(all_line_labels_master)
        self.app_state.player._all_line_labels = all_line_labels_master or []
        self.app_state.player._line_cursor = 0

    @staticmethod
    def _set_empty_slides(oc, total_lines):
        """Reset an output to a single empty slide (used for ignored/disabled outputs)."""
        oc.slides = ['']
        oc.line_to_slide = [0] * total_lines
        oc.index = 0
        oc.verse_codes = []
        oc.verse_indices = []

    def _build_song_slides_for_output(self, data, total_lines, is_announcement):
        """Pass 2 for one output: build its HTML slides and line_to_slide map from line groups."""
        oc = data['oc']
        all_lines = data['all_lines']
        verse_indices = data['verse_indices']
        groups = data['groups']
        verse_codes = data['verse_codes']

        # Override verse codes for announcements
        if is_announcement:
            verse_codes = ['an' for _ in verse_codes]
        oc.verse_codes = verse_codes
        oc.verse_indices = verse_indices

        slides = []
        line_to_slide = [0] * total_lines

        for slide_idx, g_info in enumerate(groups):
            slides.append(self._render_song_slide_html(oc, g_info, all_lines, verse_indices))
            for li in g_info['indices']:
                if 0 <= li < len(line_to_slide):
                    line_to_slide[li] = slide_idx

        # Pad remaining line_to_slide entries
        last_slide_idx = len(slides) - 1 if slides else 0
        for li in range(len(all_lines), total_lines):
            line_to_slide[li] = last_slide_idx

        oc.slides = slides or ['']
        oc.line_to_slide = line_to_slide
        oc.index = 0

    @staticmethod
    def _render_song_slide_html(oc, g_info, all_lines, verse_indices):
        """Render one slide's HTML from a group of line indices, applying verse-gap and follow-line styling."""
        grp = g_info['indices']
        count = g_info['active_count']
        html_lines = []

        for i, idx in enumerate(grp):
            line = all_lines[idx]
            v_idx = verse_indices[idx]

            # Apply verse gap when verse boundary occurs within a slide (but not
            # between lettered sections of the same verse — they read as one verse;
            # must mirror the extra_px packing rule in _compute_line_groups)
            if i > 0 and oc.fluid_slides and oc.verse_gap > 0:
                prev_v_idx = verse_indices[grp[i-1]]
                if v_idx != prev_v_idx and not _same_verse_sections(
                        oc.verse_codes[prev_v_idx], oc.verse_codes[v_idx]):
                    line = f'<span style="display:inline-block; margin-top:{oc.verse_gap}px; width:100%;">{line}</span>'

            # Apply follow lines highlighting
            if oc.follow_lines > 0:
                is_active = i < count
                color = safe_css_color(
                    oc.highlight_color if is_active else oc.dim_color,
                    default='#ffffff' if is_active else '#888888',
                )

                style = f'color:{color};'
                if is_active and oc.highlight_font_size > 0:
                    style += f' font-size:{oc.highlight_font_size}px;'

                line = f'<span style="{style}">{line}</span>'

            html_lines.append(line)

        # Canonical <size=NN> markup converts to relative spans only here, at the
        # HTML edge — measurement and the evening pass see the tag form.
        return _convert_size_tags('<br/>'.join(html_lines))

    def _rebuild_slides_and_mappings(self):
        """Rebuild slides and line-to-slide mappings for all outputs.

        Runs under the theme resolver's lookup cache: the service/song theme
        maps are resolved once per pass instead of once per output (the live
        selection can't change mid-pass — rebuilds are serialized by
        _render_lock and this runs synchronously within it)."""
        with self.app_state.theme_resolver.cached_lookups():
            self._rebuild_slides_and_mappings_inner()

    def _rebuild_slides_and_mappings_inner(self):
        # Pre-compute effective style per output (Global < Service < Song)
        effective_styles = {}
        originals = {}

        for oc in self.app_state.outputs:
            # Single reset point for per-slide overlays and background overrides:
            # builders that produce them (template announcements, song title slides)
            # repopulate below, so none can survive a rebuild into unrelated content.
            oc.slide_overlays = []
            oc.slide_bg_overrides = []
            originals[oc.name] = oc.style_to_dict()
            eff = self.app_state.theme_resolver._resolve_effective_style_for_output(oc)
            effective_styles[oc.name] = eff
            oc.apply_style_dict(eff)

        self.app_state.effective_output_styles = effective_styles

        # Standalone announcement sent live directly from the library (no service). It
        # renders through the same builder as an in-service announcement item — only the
        # source of the snapshot differs (current_ann_data vs. the service item).
        if self.app_state.current_mode == 'announcement':
            self._rebuild_slides_template_ann(self.app_state.current_ann_data, originals)
            return

        # Bible verses (from bible mode or a bible service item).
        bible_data = self._current_bible_data()
        if bible_data:
            self._rebuild_slides_bible(bible_data, originals)
            return

        # No-slide content types: each just loads its data and clears the outputs.
        no_slide_handler = {
            'video': self._rebuild_slides_video,
            'image': self._rebuild_slides_single_image,
            'image_folder': self._rebuild_slides_image_folder,
            'divider': self._rebuild_slides_divider,
        }.get(self.app_state.current_item_type())
        if no_slide_handler is not None:
            no_slide_handler(originals)
            return

        # Template-based announcement in a service.
        if self.app_state.current_item_type() == 'announcement':
            self._rebuild_slides_template_ann(self.app_state.current_service_item(), originals)
            return

        # Process songs
        song = self.get_current_song()
        if not song or not self.app_state.outputs:
            # No song to page: restore the base styles like every other no-content
            # branch above. Skipping this leaves each output baked with the effective
            # style the loop applied, which then becomes the "base" the next rebuild
            # resolves on top of — harmless for style fields every theme re-specifies,
            # but a stale leak for optional ones (e.g. title_background_*).
            self._restore_styles(originals)
            return

        # Determine verse order
        verse_order = None
        item = self.app_state.current_service_item()
        if item is not None:
            verse_order = item.get('verse_order')
        elif self.app_state.current_song_verse_order:
            verse_order = self.app_state.current_song_verse_order

        self._rebuild_slides_song(song, verse_order, False, originals)

    def _current_bible_data(self):
        """Bible payload for the current mode/item, or None when this isn't a bible context.
        (An item whose data won't parse yields {} — falsy — so the caller skips it.)"""
        if self.app_state.current_mode == 'bible':
            return self.app_state.current_bible_data
        if self.app_state.current_item_type() == 'bible':
            return _item_data(self.app_state.current_service_item())
        return None

    def _rebuild_slides_video(self, originals):
        """Video service item: store its data + reset timing, no slides."""
        self.app_state.current_video_data = _item_data(self.app_state.current_service_item())
        self.app_state._reset_video_timing(
            autoplay=bool(self.app_state.current_video_data.get('autoplay', True)))
        self.app_state._clear_outputs_and_player()
        self._restore_styles(originals)

    def _rebuild_slides_single_image(self, originals):
        """Single-image service item: load it as a one-image set, no slides."""
        item_data = _item_data(self.app_state.current_service_item())
        filename = item_data.get('filename', '')
        self.app_state.current_image_data = {
            'folder_id': None,
            'folder_name': filename,
            'images': [filename] if filename else [],
            'index': 0,
        }
        self.app_state._clear_outputs_and_player()
        self._restore_styles(originals)

    def _rebuild_slides_image_folder(self, originals):
        """Image-folder service item: load the folder's image list, no slides."""
        item_data = _item_data(self.app_state.current_service_item())
        self.app_state.current_image_data = {
            'folder_id': item_data.get('folder_id'),
            'folder_name': item_data.get('folder_name', ''),
            'images': item_data.get('images', []),
            'index': 0,
        }
        self.app_state._clear_outputs_and_player()
        self._restore_styles(originals)

    def _rebuild_slides_divider(self, originals):
        """Divider service item: output stays blank, no slides."""
        self.app_state._clear_outputs_and_player()
        self._restore_styles(originals)






class AppState:
    def __init__(self):
        self.db = DatabaseManager()
        self.outputs: list[OutputConfig] = []

        # Service state
        self.current_service_id = -1
        self.current_service_items = []

        # Player state
        self.current_item_index = -1  # Index in current_service_items
        self.current_song_title = ""
        self.current_song_lyrics = ""
        self.current_song_verse_order = None # Track current song's verse order independently of service item

        self.is_blank = False
        # Global freeze. Unlike blank, freeze deliberately persists across content
        # loads: its purpose is to hold the live screen while the operator stages the
        # next item behind it, so it is only cleared by an explicit unfreeze.
        self.is_frozen = False

        self.current_mode = 'song' # 'song', 'bible', 'service', 'video', 'image', 'announcement'
        self.current_bible_data = {} # {'text': '', 'ref': '', 'version': ''}
        self.current_song_id = None
        self.current_video_data = {} # {'filename': str, 'title': str, 'loop': bool, 'autoplay': bool}
        self.current_image_data = {} # {'folder_id': int, 'folder_name': str, 'images': [str...], 'index': int}
        # Standalone announcement sent live straight from the library (no service): the
        # snapshot of the library item {name, fields, theme_map} the slide builder renders.
        self.current_ann_data = {}

        # Video playback timing — used to calculate current position for newly-connecting clients
        self.video_is_playing = False
        self.video_start_time = 0.0    # time.time() when the current playback segment began
        self.video_start_position = 0.0  # video position (seconds) when segment began
        self.video_pause_position = 0.0  # video position when paused
        self.video_pending = False  # True while waiting for clients to buffer before switching to video mode

        # Configurable settings (populated by config_manager.load_config)
        self.export_dir = os.path.join(get_data_dir(), 'web_export')
        self.bundle_local_fonts = False
        self.ccli_licence_number = ""
        self.preview_video_mode = "still"
        # Global theme-cascade priority (see normalize_theme_priority / ThemeResolver).
        self.theme_priority = list(DEFAULT_THEME_PRIORITY)
        # Active style profile id (theme-assignment snapshot). Loaded from config and
        # validated/repaired by StyleProfileManager.ensure_seeded below.
        self.active_profile_id = None

        # Runtime caches
        self.effective_output_styles: dict = {}
        self.bundled_font_css_map: dict = {}

        # Pending video task — set when waiting for clients to buffer before switching
        self.pending_video_task: asyncio.Task = None

        # Initialize components
        self.config_manager = ConfigurationManager(self)
        self.theme_resolver = ThemeResolver(self)
        self.style_profile_manager = StyleProfileManager(self)
        self.font_manager = FontManager(self)
        self.slide_builder = SlideBuilder(self)
        self.exporter = HtmlExporter(self)
        self.player = PlayerController(self)

        self.config_manager.load_config()
        # Ensure a valid active style profile exists (seeds a 'Default' from the loaded
        # look on first run, so existing installs are unchanged).
        self.style_profile_manager.ensure_seeded()

    def get_output(self, name: str):
        return next((o for o in self.outputs if o.name == name), None)

    def current_service_item(self):
        """The active service item dict (by current_item_index), or None if the
        index is out of range. Bounds check only — callers gate on mode as needed."""
        if 0 <= self.current_item_index < len(self.current_service_items):
            return self.current_service_items[self.current_item_index]
        return None

    def active_item_id(self):
        """item_id of the live service item, or None when nothing is live. Captured
        before a mutation so the live selection can be tracked by identity rather than
        by list position (see reconcile_active_item)."""
        item = self.current_service_item()
        return item.get('item_id') if item else None

    def reconcile_active_item(self, prev_item_id):
        """Re-point current_item_index at the item with id `prev_item_id` after
        current_service_items has been refreshed.

        The live selection is stored as a list index, so adding, removing, or
        reordering *other* items would otherwise silently leave the index pointing at a
        different item. Matching on the stable item_id keeps the same item live.

        Returns True if the previously-live item is gone (it was removed), in which case
        the index is reset to -1 and the caller should clear the live display."""
        if prev_item_id is None:
            return False
        for i, item in enumerate(self.current_service_items):
            if item.get('item_id') == prev_item_id:
                self.current_item_index = i
                return False
        self.current_item_index = -1
        return True

    def clear_live_item(self):
        """Drop the live song/selection fields. Used when the active service item is
        removed; the current service stays selected (only the item goes away)."""
        self.current_song_id = None
        self.current_song_title = ""
        self.current_song_lyrics = ""
        self.current_song_verse_order = None

    def current_item_type(self):
        """item_type of the active service item when current_mode == 'service', else
        None. Encapsulates the common `mode=='service' and in-bounds and type==X` guard."""
        if self.current_mode != 'service':
            return None
        item = self.current_service_item()
        return item.get('item_type') if item else None

    def effective_content_type(self):
        """The active content type, collapsing standalone mode and in-service item
        type into one value so callers stop repeating `mode==X or item_type==X`.

        An item renders the same whether selected standalone or as a service item, so
        a standalone mode maps to itself and a service item maps to its item_type, with
        image_folder folded onto 'image' (both render as image mode). Returns 'service'
        only for a service with no resolvable active item."""
        if self.current_mode != 'service':
            return self.current_mode
        it = self.current_item_type()
        if it == 'image_folder':
            return 'image'
        return it or 'service'

    def _clear_outputs_and_player(self):
        for oc in self.outputs:
            oc.slides = []
            oc.slide_overlays = []
            oc.slide_bg_overrides = []
            oc.line_to_slide = []
            oc.index = 0
        self.player._total_lines = 0
        self.player._all_lines = []
        self.player._all_line_labels = []
        self.player._line_cursor = 0

    def _collect_service_video_urls(self) -> List[str]:
        """Return /static/videos/ URLs for every video item in the current service."""
        urls = []
        for item in self.current_service_items:
            if item.get('item_type') == 'video':
                url = _static_media_url('videos', _item_data(item).get('filename', ''))
                if url:
                    urls.append(url)
        return urls

    def _reset_video_timing(self, autoplay: bool = True):
        """Reset video position tracking when a new video starts."""
        self.video_is_playing = autoplay
        self.video_start_position = 0.0
        self.video_pause_position = 0.0
        self.video_start_time = time.time() if autoplay else 0.0

    def _get_video_position(self) -> float:
        """Estimate current playback position in seconds."""
        if self.video_is_playing:
            return self.video_start_position + (time.time() - self.video_start_time)
        return self.video_pause_position



# Global application state. Constructed by init_app() — NOT at import — so that
# importing this module never opens the database, creates directories, or runs
# migrations (tests and tooling can import lyrics without mutating user data).
# Every handler dereferences the global at call time, after init_app has run.
APP_STATE = None

def init_app():
    """Construct global state and run the startup passes. Idempotent.

    Called from build_server() (state is needed before uvicorn starts) and from the
    FastAPI lifespan (covers TestClient and any other direct ASGI use of `app`)."""
    global APP_STATE
    if APP_STATE is not None:
        return
    APP_STATE = AppState()

    # Ensure export directory and subdirectories exist for static file mounting
    os.makedirs(APP_STATE.export_dir, exist_ok=True)
    os.makedirs(os.path.join(APP_STATE.export_dir, 'images'), exist_ok=True)
    # Background-theme image store, served at /static/backgrounds/ (see the
    # /api/backgrounds/* endpoints). Pre-existing loose files dropped here by hand are
    # adopted automatically — the directory listing is the source of truth.
    os.makedirs(os.path.join(APP_STATE.export_dir, 'backgrounds'), exist_ok=True)

    # Mounted here (not at import) because export_dir is config-overridable, so the
    # path is only known once the config is loaded. Appending after the route table
    # is safe: no other route matches a multi-segment /static/... path (the
    # /{filename} catch-all is single-segment only).
    app.mount("/static", StaticFiles(directory=APP_STATE.export_dir), name="static")

    _migrate_theme_bg_ownership()
    _migrate_title_slides_embedded()


# ---------------------- Web server ----------------------

def _read_template(filename: str) -> str:
    """Read a bundled template/asset from the templates dir (PyInstaller-aware)."""
    with open(get_resource_path(os.path.join('templates', filename)), encoding='utf-8') as f:
        return f.read()

def _read_asset_bytes(relpath: str):
    """Read a bundled binary asset (PyInstaller-aware), or None if unavailable — so a
    missing non-critical asset (e.g. the favicon) never breaks startup."""
    try:
        with open(get_resource_path(relpath), 'rb') as f:
            return f.read()
    except OSError:
        return None

HTML_TEMPLATE = _read_template('output.html')

# Admin UI is split across three bundled files — admin.html links /admin.css and
# /admin.js (served by the routes below). All read once at startup, served from memory.
ADMIN_HTML = _read_template('admin.html')
ADMIN_CSS = _read_template('admin.css')
ADMIN_JS = _read_template('admin.js')

# Web-remote browser icons, generated from the logo by icons/make_icons.py and read
# once at startup. favicon.png/.ico cover browser tabs (incl. the legacy /favicon.ico
# probe); apple-touch-icon is iOS's "Add to Home Screen" icon. See admin.html.
FAVICON_BYTES = _read_asset_bytes(os.path.join('icons', 'favicon.png'))
FAVICON_ICO_BYTES = _read_asset_bytes(os.path.join('icons', 'seventhslide.ico'))
APPLE_TOUCH_ICON_BYTES = _read_asset_bytes(os.path.join('icons', 'apple-touch-icon.png'))

# Texture fill for the 'floating_bar' animated-background preset (a cropped, optimized
# strip of the sheet-music photo). Served at /assets/song-bar-texture.jpg and referenced
# by the preset CSS in output.html. Read once at startup; served from memory.
SONG_BAR_TEXTURE_BYTES = _read_asset_bytes(os.path.join('assets', 'song-bar-texture.jpg'))


# ---------------------- WebSockets ----------------------

def _get_copyright_song_payload(state=None):
    """Song-like dict used for copyright display, preferring the service snapshot."""
    st = state or APP_STATE
    mode = st.current_mode
    if mode == 'song':
        sid = st.current_song_id
        return st.db.get_song(sid) if sid else None
    if mode == 'service':
        itm = st.current_service_item()
        if itm is not None and itm.get('item_type') == 'song':
            return itm
    return None


def _build_copyright_base(song, state=None) -> str:
    """Build the raw copyright text from a song dict (no slide-position filtering)."""
    st = state or APP_STATE
    if not song or not song.get('show_copyright', 0):
        return ''
    lines = []
    authors = song.get('authors', [])
    if authors:
        lines.append(', '.join(authors))
    copyright_text = song.get('copyright', '')
    if copyright_text:
        lines.append(copyright_text)
    ccli = st.ccli_licence_number
    if ccli:
        lines.append(f'CCLI License #{ccli}')
    return '\n'.join(lines)


def _build_copyright_info(oc, copyright_base: str = '', state=None):
    """Build copyright info string for the given output, applying slide-position filtering."""
    st = state or APP_STATE
    if not copyright_base:
        copyright_base = _build_copyright_base(_get_copyright_song_payload(st), st)

    copyright_info = copyright_base
    if copyright_info:
        eff_style = (st.effective_output_styles or {}).get(oc.name) or oc.style_to_dict()
        slide_mode = eff_style.get('copyright_slide_mode', 'all')
        slide_count = eff_style.get('copyright_slide_count', 1)
        total_slides = len(oc.slides) if oc.slides else 0
        if slide_mode == 'first':
            if oc.index >= slide_count:
                copyright_info = ''
        elif slide_mode == 'last':
            if total_slides > 0 and oc.index < total_slides - slide_count:
                copyright_info = ''
    return copyright_info


# --- Per-mode broadcast content resolvers --------------------------------
# Each resolver inspects the (loop-invariant) broadcast content and the active
# service item, and fills in the media fields it owns, promoting content['mode']
# to its own mode when a service item selects it. They are applied in order by
# _resolve_broadcast_content; the guards are mutually exclusive in practice.

def _static_media_url(subdir: str, filename: str) -> Optional[str]:
    """Build a `/static/{subdir}/{filename}` URL with the path segment percent-encoded.

    On-disk names are unchanged; only the URL form is encoded so spaces and other
    special characters survive in ``src`` / preload lists. ``basename`` rejects any
    accidental directory component.
    """
    name = os.path.basename(filename or '')
    if not name:
        return None
    return f'/static/{subdir}/{urllib.parse.quote(name, safe="")}'


def _resolve_bible_content(content, item, state=None):
    st = state or APP_STATE
    mode = content['mode']
    b_data = None
    if mode == 'bible':
        b_data = st.current_bible_data
    elif item and item.get('item_type') == 'bible':
        content['mode'] = 'bible'
        b_data = _item_data(item)
    if b_data:
        ref_text = b_data.get('ref', '')
        version = b_data.get('version', '')
        if ref_text:
            content['bible_ref'] = f"{ref_text}\n{version}" if version else ref_text


def _resolve_video_content(content, item, state=None):
    st = state or APP_STATE
    if content['mode'] != 'video' and not (item and item.get('item_type') == 'video'):
        return
    content['mode'] = 'video'
    vd = st.current_video_data
    content['video_url'] = _static_media_url('videos', vd.get('filename', ''))
    content['video_loop'] = bool(vd.get('loop', False))
    content['video_autoplay'] = st.video_is_playing


def _resolve_image_content(content, item, state=None):
    st = state or APP_STATE
    if content['mode'] != 'image' and not (item and item.get('item_type') in ('image_folder', 'image')):
        return
    content['mode'] = 'image'
    img_data = st.current_image_data
    images = img_data.get('images', [])
    idx = img_data.get('index', 0)
    if images and 0 <= idx < len(images):
        content['image_url'] = _static_media_url('images', images[idx])


_CONTENT_RESOLVERS = (_resolve_bible_content, _resolve_video_content, _resolve_image_content)


class ConnectionManager:
    def __init__(self):
        self.active_connections: List[Dict[str, Any]] = []
        self._lib_dirty = True
        self._lib_cache: dict = {}
        # Serializes the off-loop library snapshot refresh so two concurrent
        # broadcasts can't both fire the heavy DB read or race on _lib_cache.
        self._lib_lock = asyncio.Lock()
        # Cached copyright base string — valid until the copyright source changes.
        # Avoids a DB query per output on every nav broadcast.
        self._copyright_base: Optional[str] = None
        self._copyright_cache_key = None
        # Per-output frozen-frame snapshots, keyed by output name. Populated when an
        # output becomes frozen and served (instead of live content) for as long as it
        # stays frozen, so the held frame survives reconnects and is unaffected by
        # whatever the operator stages behind it. See _refresh_freeze_snapshots.
        self._frozen_snapshots: Dict[str, dict] = {}

    async def connect(self, websocket: WebSocket, client_type: str, output_name: Optional[str] = None):
        await websocket.accept()
        self.active_connections.append({
            "ws": websocket,
            "type": client_type,
            "output_name": output_name
        })

    def disconnect(self, websocket: WebSocket):
        self.active_connections = [c for c in self.active_connections if c["ws"] != websocket]

    def invalidate_library_cache(self):
        """Mark library cache dirty so the next broadcast re-queries the DB."""
        self._lib_dirty = True

    def invalidate_copyright_cache(self):
        """Clear cached copyright base so the next broadcast re-fetches it."""
        self._copyright_base = None
        self._copyright_cache_key = None

    @staticmethod
    async def _safe_send(ws, payload):
        """Send one JSON payload to one client. A failed send must never break the
        broadcast fan-out, so all errors are caught — but a serialization error (a
        bug in what we built) is logged, whereas a transport error (the client went
        away, which it recovers from by reconnecting) is left silent."""
        try:
            await ws.send_json(payload)
        except (TypeError, ValueError):
            # Non-serializable / malformed payload — our bug, not a dead socket.
            # Silently dropping this would lose the update for every client with
            # no trace; surface it so it's diagnosable.
            logger.exception("Failed to serialize WebSocket payload")
        except Exception:
            # Transport-level failure (disconnect, broken pipe). Expected; the
            # client reconnects on its own.
            logger.debug("WebSocket send failed (client likely disconnected)", exc_info=True)

    async def broadcast_video_command(self, action: str, position=None):
        """Send a video playback command to all output clients."""
        msg = {'type': 'video_command', 'video_command': action}
        if position is not None:
            msg['video_position'] = position
        sends = [self._safe_send(c['ws'], msg)
                 for c in self.active_connections if c['type'] == 'output']
        if sends:
            await asyncio.gather(*sends)

    @staticmethod
    def _build_indicator_html(oc, eff_style) -> str:
        """Build the verse-indicator HTML (e.g. V1 C1 B …) for an output, marking the
        currently-displayed verse active. Lettered sections of one verse (v1a, v1b, v1c)
        collapse into a single item (V1). Returns '' when the indicator is disabled."""
        if not (eff_style.get('show_indicator') and getattr(oc, 'verse_codes', None)):
            return ''
        curr_v_idx = -1
        if hasattr(oc, 'verse_indices') and oc.line_to_slide and oc.verse_indices:
            try:
                first_line = oc.line_to_slide.index(oc.index)
                if 0 <= first_line < len(oc.verse_indices):
                    curr_v_idx = oc.verse_indices[first_line]
            except ValueError:
                pass
        parts = []
        codes = oc.verse_codes
        i, n = 0, len(codes)
        while i < n:
            # Collapse a run of consecutive sections of the same verse into one item;
            # it renders active if the current verse is any section within that run.
            base = _base_verse_code(codes[i])
            active = False
            while i < n and _base_verse_code(codes[i]) == base:
                active = active or (i == curr_v_idx)
                i += 1
            c = base.upper() if base else '?'
            parts.append(f'<span class="ind-item{" active" if active else ""}">{c}</span>')
        return "".join(parts)

    def _refresh_copyright_base(self, state=None):
        """Refresh the cached copyright base if the copyright source changed."""
        st = state or APP_STATE
        payload = _get_copyright_song_payload(st)
        if payload is None:
            key = None
        elif st.current_mode == 'song':
            key = ('song', st.current_song_id)
        else:
            key = (
                'svc',
                payload.get('item_id'),
                payload.get('copyright'),
                bool(payload.get('show_copyright')),
                tuple(payload.get('authors') or []),
            )
        if key != self._copyright_cache_key:
            self._copyright_base = _build_copyright_base(payload, st)
            self._copyright_cache_key = key

    @staticmethod
    def _resolve_broadcast_content(state=None) -> dict:
        """Resolve the content shared by every output this broadcast cycle."""
        st = state or APP_STATE
        mode = st.current_mode
        item = st.current_service_item() if mode == 'service' else None
        content = {
            'mode': mode,
            'service_item': item,
            'bible_ref': '',
            'video_url': None,
            'video_loop': False,
            'video_autoplay': True,
            'image_url': None,
        }
        for resolve in _CONTENT_RESOLVERS:
            resolve(content, item, st)

        content['transition_key'] = item.get('item_type', content['mode']) if item else content['mode']
        return content

    def _build_output_packets(self) -> dict:
        """Build full per-output state_update packets. Returns dict keyed by output name."""
        preload_images = APP_STATE.theme_resolver._collect_service_background_images()
        preload_videos = APP_STATE._collect_service_video_urls()

        self._refresh_copyright_base()
        copyright_base = self._copyright_base or ''
        content = self._resolve_broadcast_content()

        # Frozen outputs keep their snapshot; others get a live full packet.
        packets = {}
        for oc in APP_STATE.outputs:
            snap = self._frozen_snapshots.get(oc.name) if self._output_is_frozen(oc) else None
            packets[oc.name] = snap if snap is not None else self._build_full_packet(
                oc, content, copyright_base, preload_images, preload_videos)
        return packets

    @staticmethod
    def _output_is_blank(oc, state=None) -> bool:
        st = state or APP_STATE
        return oc.is_blank or (st.is_blank and not oc.exempt_from_global_blank)

    @staticmethod
    def _output_is_frozen(oc, state=None) -> bool:
        st = state or APP_STATE
        return oc.is_frozen or (st.is_frozen and not oc.exempt_from_global_freeze)

    def _resolve_visible_fields(self, oc, content, copyright_base, state=None):
        """Resolve one output's visible fields. Returns
        (html, eff_style, indicator_html, copyright_info, output_is_blank, overlay_html)."""
        st = state or APP_STATE
        h = oc.slides[oc.index] if oc.slides and 0 <= oc.index < len(oc.slides) else ''
        overlay_html = (oc.slide_overlays[oc.index]
                        if oc.slide_overlays and 0 <= oc.index < len(oc.slide_overlays) else '') or ''
        eff_style = (st.effective_output_styles or {}).get(oc.name) or oc.style_to_dict()
        bg_override = (oc.slide_bg_overrides[oc.index]
                       if oc.slide_bg_overrides and 0 <= oc.index < len(oc.slide_bg_overrides) else None)
        if bg_override:
            eff_style = {**eff_style, **bg_override}
        indicator_html = self._build_indicator_html(oc, eff_style)
        copyright_info = _build_copyright_info(oc, copyright_base, st)
        output_is_blank = self._output_is_blank(oc, st)

        if output_is_blank:
            h = ''
            overlay_html = ''
            indicator_html = ''
            copyright_info = ''
        return h, eff_style, indicator_html, copyright_info, output_is_blank, overlay_html

    def _build_full_packet(self, oc, content, copyright_base, preload_images, preload_videos,
                           state=None) -> dict:
        """Build one output's full state_update packet from the shared broadcast content."""
        st = state or APP_STATE
        h, eff_style, indicator_html, copyright_info, output_is_blank, overlay_html = \
            self._resolve_visible_fields(oc, content, copyright_base, st)

        pkt = {
            'type': 'state_update',
            'html': h,
            'overlay_html': overlay_html,
            'indicator': indicator_html,
            'index': oc.index,
            'mode': content['mode'],
            'transition_key': content['transition_key'],
            'bible_ref': '' if output_is_blank else content['bible_ref'],
            'copyright_info': copyright_info,
            'style': eff_style,
            'font_css': (st.bundled_font_css_map or {}).get(oc.name, '') if st.bundle_local_fonts else '',
            'preload_images': preload_images,
            'preload_videos': preload_videos,
            'is_blank': output_is_blank,
            'frozen': self._output_is_frozen(oc, st),
            'preview_video_mode': st.preview_video_mode,
            'hold_frame': st.video_pending,
        }
        self._add_media_fields(pkt, oc, content, output_is_blank, st)
        return pkt

    @staticmethod
    def _add_media_fields(pkt, oc, content, output_is_blank, state=None):
        """Attach the mode-specific media fields to an output packet."""
        st = state or APP_STATE
        mode = content['mode']
        if mode == 'video':
            pkt['video_url'] = None if output_is_blank else content['video_url']
            pkt['video_loop'] = content['video_loop']
            pkt['video_autoplay'] = content['video_autoplay'] and not output_is_blank
            pkt['video_position'] = st._get_video_position()
        elif mode == 'image':
            pkt['image_url'] = None if output_is_blank else content['image_url']
            img_data = st.current_image_data
            pkt['image_index'] = img_data.get('index', 0)
            pkt['image_count'] = len(img_data.get('images', []))

    def _build_nav_output_packets(self) -> dict:
        """Build minimal per-output packets for next/prev navigation.

        Skips style, font_css, preload_images, preload_videos, mode, bible_ref,
        preview_video_mode, and hold_frame — none of these change during slide navigation.
        Reuses the copyright base cached by the last _build_output_packets call.
        """
        copyright_base = self._copyright_base or ''
        content = self._resolve_broadcast_content()
        is_image_mode = content['mode'] == 'image'

        output_data_map = {}
        for oc in APP_STATE.outputs:
            # Frozen outputs hold their snapshot — navigation behind a freeze must not
            # disturb the held frame (same rule as the full-packet path).
            if self._output_is_frozen(oc):
                snap = self._frozen_snapshots.get(oc.name)
                if snap is not None:
                    output_data_map[oc.name] = snap
                    continue

            h, eff_style, indicator_html, copyright_info, output_is_blank, overlay_html = \
                self._resolve_visible_fields(oc, content, copyright_base)

            pkt = {
                'type': 'state_update',
                'html': h,
                'overlay_html': overlay_html,
                'indicator': indicator_html,
                'index': oc.index,
                'copyright_info': copyright_info,
                'is_blank': output_is_blank,
                'frozen': False,
            }

            # Nav packets normally omit style — nothing themeable changes between
            # slides of one item. The exception is an item with per-slide background
            # overrides (a song title slide with a title background): the background
            # differs between the title slide and the rest, so carry the (already
            # per-slide-folded) style on nav for that item so the display switches
            # backgrounds when the operator steps on or off the title slide.
            if oc.slide_bg_overrides:
                pkt['style'] = eff_style

            if is_image_mode:
                pkt['image_url'] = None if output_is_blank else content['image_url']

            output_data_map[oc.name] = pkt

        return output_data_map

    @staticmethod
    def _freeze_video_still(pkt: dict):
        """Turn a captured video packet into a held still: clear autoplay and any
        real-time command so the frozen frame doesn't keep playing while held."""
        if pkt.get('video_url'):
            pkt['video_autoplay'] = False
            pkt.pop('video_command', None)

    def _refresh_freeze_snapshots(self):
        """Reconcile the stored frozen-frame snapshots with the current freeze state.

        Captures a fresh snapshot for every output that is now frozen but doesn't yet
        have one, and drops snapshots for outputs that are no longer frozen. The
        snapshot is the live full packet built at the moment of freezing, so it holds
        whatever was on screen then — independent of what the operator stages behind it.
        Call this after any change to global or per-output freeze state, before
        broadcasting.

        Idempotent: an already-frozen output keeps its existing snapshot (so toggling
        global freeze, or another output, never disturbs a frame already held)."""
        frozen_now = {oc.name for oc in APP_STATE.outputs if self._output_is_frozen(oc)}

        # Drop snapshots for outputs that are no longer frozen (or were removed).
        for name in [n for n in self._frozen_snapshots if n not in frozen_now]:
            del self._frozen_snapshots[name]

        # Capture for newly-frozen outputs only. Build the shared content once.
        new_names = [oc.name for oc in APP_STATE.outputs
                     if oc.name in frozen_now and oc.name not in self._frozen_snapshots]
        if not new_names:
            return

        preload_images = APP_STATE.theme_resolver._collect_service_background_images()
        preload_videos = APP_STATE._collect_service_video_urls()
        self._refresh_copyright_base()
        copyright_base = self._copyright_base or ''
        content = self._resolve_broadcast_content()
        for oc in APP_STATE.outputs:
            if oc.name in new_names:
                pkt = self._build_full_packet(
                    oc, content, copyright_base, preload_images, preload_videos)
                self._freeze_video_still(pkt)
                self._frozen_snapshots[oc.name] = pkt

    def _fetch_library_snapshot(self) -> dict:
        """Pull the full library snapshot from the DB. Blocking — must run off the
        event loop (via asyncio.to_thread) so the per-call SQLite reads (notably
        get_all_songs_summary, a full sorted table scan) can't stall every connected
        display's WebSocket."""
        return {
            'songs': APP_STATE.db.get_all_songs_summary(),
            'ann_items': APP_STATE.db.get_ann_items(),
            'ann_folders': APP_STATE.db.get_ann_folders(),
            # Per-output layout summaries ({output_name: [{id,name,slot_count}]}) for the
            # announcement editors' per-output layout picker.
            'ann_layouts': {oc.name: APP_STATE.db.get_ann_layouts_summary(oc.name)
                            for oc in APP_STATE.outputs},
            'bibles': APP_STATE.db.get_bibles(),
            'services': APP_STATE.db.get_all_services(),
            'service_groups': APP_STATE.db.get_service_groups(),
            'image_display_names': APP_STATE.db.get_image_display_names(),
            'style_profiles': APP_STATE.db.get_style_profiles(),
        }

    async def _ensure_library_cache(self, force: bool = False) -> bool:
        """Ensure the library snapshot is populated, refreshing it off the event loop
        when dirty. Returns whether the heavy song list should be (re)sent to clients:
        True if a refresh happened this cycle (library changed) or `force` is set.

        The lock + double-check means two near-simultaneous broadcasts share one DB
        read instead of both hammering it and racing on _lib_cache.
        """
        if self._lib_dirty or not self._lib_cache:
            async with self._lib_lock:
                if self._lib_dirty or not self._lib_cache:
                    self._lib_cache = await asyncio.to_thread(self._fetch_library_snapshot)
                    self._lib_dirty = False
            return True
        return force

    @staticmethod
    def _admin_service_items_payload(items):
        """Copy service items for the admin WS payload with lyrics stripped.

        The service list never reads lyrics; the editor fetches a full item on open.
        Leaving lyrics out of every state_full cut keeps large-service pushes small.
        """
        out = []
        for it in items or []:
            if not isinstance(it, dict):
                out.append(it)
                continue
            slim = dict(it)
            slim.pop('lyrics', None)
            out.append(slim)
        return out

    def _build_admin_state(self, include_songs: bool = False) -> dict:
        """Build the full admin state payload from the already-populated library cache.

        Pure in-memory assembly — the DB snapshot is refreshed separately by
        _ensure_library_cache (which callers await first). The song summary (the
        heaviest part — thousands of rows) is only included when `include_songs` is set
        (library changed, or a brand-new client / explicit full fetch). The client keeps
        its cached `allSongs` and skips the full re-render when the key is absent.
        """
        state = {
                'ann_items': self._lib_cache['ann_items'],
                'ann_folders': self._lib_cache['ann_folders'],
                'ann_layouts': self._lib_cache['ann_layouts'],
                'bibles': self._lib_cache['bibles'],
                'image_display_names': self._lib_cache['image_display_names'],
                'current_mode': APP_STATE.current_mode,
                'current_bible_data': APP_STATE.current_bible_data,
                'services': self._lib_cache['services'],
                'service_groups': self._lib_cache['service_groups'],
                'current_service_id': APP_STATE.current_service_id,
                # Lyrics omitted from the admin wire payload (list UI never reads them;
                # the song editor fetches a full item on open). In-memory
                # APP_STATE.current_service_items keeps full snapshots for rebuild.
                'current_service_items': self._admin_service_items_payload(
                    APP_STATE.current_service_items),
                'current_item_index': APP_STATE.current_item_index,
                'bundle_local_fonts': bool(APP_STATE.bundle_local_fonts),
                'ccli_licence_number': APP_STATE.ccli_licence_number,
                'preview_video_mode': APP_STATE.preview_video_mode,
                'theme_priority': normalize_theme_priority(APP_STATE.theme_priority),
                'style_profiles': self._lib_cache['style_profiles'],
                'active_profile_id': APP_STATE.active_profile_id,
                'current_image_data': APP_STATE.current_image_data,
                'outputs': [{
                    **oc.to_dict(),
                    # Slide HTML is served to output clients / preview iframes via their
                    # own WS or page — admin never reads `slides` from this payload, so
                    # omit it to keep state_full small (outputs × slides of HTML).
                    'index': oc.index,
                    'line_to_slide': oc.line_to_slide,
                    'is_blank': oc.is_blank,
                    'is_frozen': oc.is_frozen,
                    'is_ignored': oc.is_ignored,
                } for oc in APP_STATE.outputs],
                'all_lines': APP_STATE.player._all_lines,
                'all_line_labels': APP_STATE.player._all_line_labels,
                'line_cursor': APP_STATE.player._line_cursor,
                'total_lines': APP_STATE.player._total_lines,
                'is_blank': APP_STATE.is_blank,
                'is_frozen': APP_STATE.is_frozen,
        }
        if include_songs:
            state['songs'] = self._lib_cache['songs']
        return {'type': 'state_full', 'state': state}

    async def send_full_state_to(self, ws: WebSocket):
        """Send full admin state to a single WebSocket connection (includes full library)."""
        include_songs = await self._ensure_library_cache(force=True)
        async with _render_lock:
            msg = self._build_admin_state(include_songs)
        try:
            await ws.send_json(msg)
        except Exception:
            logger.debug("send_full_state_to failed (client likely disconnected)", exc_info=True)

    async def _fan_out(self, admin_msg, output_data_map):
        """Send admin_msg to every admin client and the matching per-output packet to
        each output client, concurrently. Per-socket errors are swallowed (clients
        reconnect) so one slow/backpressured socket can't delay the others.
        Shared by broadcast_state / broadcast_nav_state / broadcast_blank_state."""
        sends = []
        for connection in self.active_connections:
            ctype = connection["type"]
            if ctype == 'admin':
                sends.append(self._safe_send(connection["ws"], admin_msg))
            elif ctype == 'output':
                name = connection["output_name"]
                if name and name in output_data_map:
                    sends.append(self._safe_send(connection["ws"], output_data_map[name]))
        if sends:
            await asyncio.gather(*sends)

    def _admin_nav_payload(self) -> dict:
        """Admin-side nav/blank cursor snapshot. Call under `_render_lock`."""
        return {
            'type': 'state_nav',
            'line_cursor': APP_STATE.player._line_cursor,
            'total_lines': APP_STATE.player._total_lines,
            'is_blank': APP_STATE.is_blank,
            'is_frozen': APP_STATE.is_frozen,
            'outputs': [{
                'name': oc.name,
                'index': oc.index,
                'is_blank': oc.is_blank,
                'is_frozen': oc.is_frozen,
                'is_ignored': oc.is_ignored,
                'line_to_slide': oc.line_to_slide,
                'exempt_from_global_blank': oc.exempt_from_global_blank,
                'exempt_from_global_freeze': oc.exempt_from_global_freeze,
            } for oc in APP_STATE.outputs],
        }

    async def broadcast_state(self, prepare=None):
        """Broadcast full state to all clients.

        Packet assembly runs under `_render_lock` so it cannot interleave with a
        rebuild/export worker mutating slides/index/style. Optional `prepare` runs
        under the same lock immediately before the snapshot (e.g. image-index step)
        so mutation and read stay atomic. If `prepare` returns False, skip the
        broadcast (no-op). Fan-out is outside the lock.
        """
        include_songs = await self._ensure_library_cache()
        async with _render_lock:
            if prepare is not None and prepare() is False:
                return False
            admin_msg = self._build_admin_state(include_songs)
            packets = self._build_output_packets()
        await self._fan_out(admin_msg, packets)
        return True

    async def broadcast_library_state(self):
        """Admin-only broadcast for library/service metadata changes that don't alter
        what's rendered on the output displays (service rename, reorder, regrouping).

        Skips the per-output packet rebuild and the output fan-out entirely — those
        re-collect backgrounds/videos and re-resolve every output's HTML for content
        that didn't change. Only admin clients receive the refreshed state.
        """
        include_songs = await self._ensure_library_cache()
        async with _render_lock:
            msg = self._build_admin_state(include_songs)
        sends = [self._safe_send(c["ws"], msg)
                 for c in self.active_connections if c["type"] == 'admin']
        if sends:
            await asyncio.gather(*sends)

    async def broadcast_nav_state(self, prepare=None):
        """Lightweight broadcast for next/prev/jump — no DB queries, no full re-render.

        Optional `prepare` (e.g. player.next_slide) runs under `_render_lock` with
        the packet build so cursor mutation cannot race a rebuild. Returns False if
        `prepare` was given and returned a falsy value (no-op nav).
        """
        async with _render_lock:
            if prepare is not None and not prepare():
                return False
            admin_nav = self._admin_nav_payload()
            packets = self._build_nav_output_packets()
        await self._fan_out(admin_nav, packets)
        return True

    async def broadcast_blank_state(self, prepare=None):
        """Broadcast for blank/unblank — no DB queries, no library data.

        Sends the blank flags to admin clients and full resolved packets to output
        clients. Full packets (not the lighter nav packets) are used because blanking
        toggles the blank mask on *every* blank-affected field — html, overlay, indicator,
        copyright, the Bible reference, and the media URLs. The nav packet builder omits
        bible_ref and the media fields on purpose (they don't change during pure slide
        navigation), so reusing it here left the Bible reference and video/image visible
        through a blank. Mirrors broadcast_freeze_state, which uses full packets for the
        same reason: a state change that flips masking must restate the complete live
        (masked) frame in one message.

        Optional `prepare` flips blank flags under the same lock as the packet build.
        """
        async with _render_lock:
            if prepare is not None:
                prepare()
            admin_blank = {
                'type': 'state_blank',
                'is_blank': APP_STATE.is_blank,
                'outputs': [{
                    'name': oc.name,
                    'is_blank': oc.is_blank,
                    'exempt_from_global_blank': oc.exempt_from_global_blank,
                } for oc in APP_STATE.outputs],
            }
            packets = self._build_output_packets()
        await self._fan_out(admin_blank, packets)

    async def broadcast_freeze_state(self, prepare=None):
        """Broadcast for freeze/unfreeze — no DB queries, no library data.

        Sends the freeze flags to admin clients and the resolved output packets to
        output clients. Full packets (not the lighter nav packets) are used so an
        output that is *unfreezing* repaints its complete live state — mode, style and
        media — in a single message, while an output that is *freezing* receives its
        held snapshot. Call _refresh_freeze_snapshots() before this so snapshots are
        in sync with the new freeze state.

        Optional `prepare` runs under the render lock with the packet build. Prefer
        flipping freeze flags before `_reconcile_freeze_snapshots` (which also takes
        the lock); use `prepare` only when mutation and broadcast must be one critical
        section without an intervening reconcile.
        """
        async with _render_lock:
            if prepare is not None:
                prepare()
            admin_freeze = {
                'type': 'state_freeze',
                'is_frozen': APP_STATE.is_frozen,
                'outputs': [{
                    'name': oc.name,
                    'is_frozen': oc.is_frozen,
                    'exempt_from_global_freeze': oc.exempt_from_global_freeze,
                } for oc in APP_STATE.outputs],
            }
            packets = self._build_output_packets()
        await self._fan_out(admin_freeze, packets)

# Serialize slide rebuilds, HTML exports, freeze snapshots, and live packet
# assembly. Fan-out stays outside the lock.
_render_lock = asyncio.Lock()


async def _db_run(fn, /, *args, **kwargs):
    """Run a blocking DB (or other) callable off the event loop."""
    return await asyncio.to_thread(fn, *args, **kwargs)


manager = ConnectionManager()


async def _rebuild_slides_unlocked():
    await asyncio.to_thread(APP_STATE.slide_builder._rebuild_slides_and_mappings)


async def _export_outputs_unlocked():
    await asyncio.to_thread(APP_STATE.exporter.export_outputs)


async def _reconcile_freeze_unlocked():
    await asyncio.to_thread(manager._refresh_freeze_snapshots)


async def _rebuild_slides():
    """Rebuild all output slides under the render lock (off the event loop)."""
    async with _render_lock:
        await _rebuild_slides_unlocked()


async def _export_outputs():
    """Re-export output HTML under the render lock (off the event loop)."""
    async with _render_lock:
        await _export_outputs_unlocked()


async def _reconcile_freeze_snapshots():
    """Capture/drop freeze snapshots under the render lock (off the event loop)."""
    async with _render_lock:
        await _reconcile_freeze_unlocked()


async def _live_commit(*, mutate=None, rebuild=False, export=False, reconcile_freeze=False,
                       after_rebuild=None, broadcast='state', prepare=None):
    """Hold ``_render_lock`` across mutate → rebuild/export → packet snapshot.

    Library cache refresh runs outside the lock. Fan-out runs after release.
    ``broadcast``: ``state`` | ``nav`` | ``blank`` | ``freeze`` | ``library`` | None.
    ``prepare`` runs under the lock (after mutate). For ``nav`` / ``state``, a
    falsy / ``False`` return skips the broadcast (existing next/jump semantics).
    ``after_rebuild`` runs under the lock after rebuild/export (e.g. video pending).
    """
    include_songs = False
    if broadcast in ('state', 'library'):
        include_songs = await manager._ensure_library_cache()

    admin_msg = None
    packets = None
    library_only = False

    async with _render_lock:
        if mutate is not None:
            mutate()
        if prepare is not None:
            prep = prepare()
            if broadcast == 'nav' and not prep:
                return False
            if broadcast == 'state' and prep is False:
                return False
        if rebuild:
            await _rebuild_slides_unlocked()
        if export:
            await _export_outputs_unlocked()
        if after_rebuild is not None:
            after_rebuild()
        if reconcile_freeze:
            await _reconcile_freeze_unlocked()

        if broadcast is None:
            return True
        if broadcast == 'library':
            admin_msg = manager._build_admin_state(include_songs)
            library_only = True
        elif broadcast == 'nav':
            admin_msg = manager._admin_nav_payload()
            packets = manager._build_nav_output_packets()
        elif broadcast == 'blank':
            admin_msg = {
                'type': 'state_blank',
                'is_blank': APP_STATE.is_blank,
                'outputs': [{
                    'name': oc.name,
                    'is_blank': oc.is_blank,
                    'exempt_from_global_blank': oc.exempt_from_global_blank,
                } for oc in APP_STATE.outputs],
            }
            packets = manager._build_output_packets()
        elif broadcast == 'freeze':
            admin_msg = {
                'type': 'state_freeze',
                'is_frozen': APP_STATE.is_frozen,
                'outputs': [{
                    'name': oc.name,
                    'is_frozen': oc.is_frozen,
                    'exempt_from_global_freeze': oc.exempt_from_global_freeze,
                } for oc in APP_STATE.outputs],
            }
            packets = manager._build_output_packets()
        else:
            admin_msg = manager._build_admin_state(include_songs)
            packets = manager._build_output_packets()

    if library_only:
        sends = [manager._safe_send(c['ws'], admin_msg)
                 for c in manager.active_connections if c['type'] == 'admin']
        if sends:
            await asyncio.gather(*sends)
    else:
        await manager._fan_out(admin_msg, packets)
    return True


# ---------------------- FastAPI App ----------------------

@asynccontextmanager
async def _app_lifespan(app):
    init_app()
    try:
        yield
    finally:
        try:
            APP_STATE.db.close()
        except Exception:
            pass

app = FastAPI(title="SeventhSlide", lifespan=_app_lifespan)

# Admin mutation APIs return HTTP 200 with {"success": bool, "message"?: str}.
# templates/admin.js checks `res.success` after API.get/post. Export/capture
# endpoints that non-UI clients call use real HTTP 4xx and {"ok": false, "error": ...}.


# Allow CORS (credentials=False required when origins="*"; this app uses no cookies)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _emit_item_frames(oc, item_index, it_type, copyright_base, preload_images, frames, state):
    """Append one export frame per distinct slide for the current item on ``state``."""
    def emit(content):
        pkt = manager._build_full_packet(
            oc, content, copyright_base, preload_images, [], state)
        for k in ('preload_videos', 'video_url', 'video_loop', 'video_autoplay',
                  'video_position', 'video_command', 'hold_frame', 'preview_video_mode',
                  'frozen', 'image_index', 'image_count'):
            pkt.pop(k, None)
        pkt['export_item_index'] = item_index
        pkt['export_item_type'] = it_type
        frames.append(pkt)

    if it_type in ('image', 'image_folder'):
        images = (state.current_image_data or {}).get('images', [])
        for k in range(len(images)):
            state.current_image_data['index'] = k
            emit(manager._resolve_broadcast_content(state))
        return

    content = manager._resolve_broadcast_content(state)
    n = len(oc.slides) if oc.slides else 0
    for s in range(n):
        oc.index = s
        html = oc.slides[s] if 0 <= s < len(oc.slides) else ''
        overlay = (oc.slide_overlays[s]
                   if oc.slide_overlays and 0 <= s < len(oc.slide_overlays) else '') or ''
        if not (html or overlay):
            continue
        emit(content)


def _snapshot_export_bundle(output_name: str):
    """Brief under `_render_lock`: copy everything export needs so the walk never
    touches live APP_STATE. Returns None if the output name is unknown."""
    if APP_STATE.get_output(output_name) is None:
        return None
    return {
        'output_name': output_name,
        'current_service_id': APP_STATE.current_service_id,
        'current_service_items': copy.deepcopy(APP_STATE.current_service_items),
        'theme_priority': list(APP_STATE.theme_priority or DEFAULT_THEME_PRIORITY),
        'bundle_local_fonts': bool(APP_STATE.bundle_local_fonts),
        'ccli_licence_number': APP_STATE.ccli_licence_number or '',
        'preview_video_mode': APP_STATE.preview_video_mode,
        'bundled_font_css_map': dict(APP_STATE.bundled_font_css_map or {}),
        'export_dir': APP_STATE.export_dir,
        'active_profile_id': APP_STATE.active_profile_id,
        'outputs': [
            OutputConfig.from_dict(copy.deepcopy(oc.to_dict()))
            for oc in APP_STATE.outputs
        ],
    }


def _make_export_app_state(bundle):
    """Private AppState-like object for export walks (never the live APP_STATE)."""
    st = types.SimpleNamespace()
    st.db = APP_STATE.db
    st.outputs = bundle['outputs']
    st.current_service_id = bundle['current_service_id']
    st.current_service_items = bundle['current_service_items']
    st.theme_priority = bundle['theme_priority']
    st.bundle_local_fonts = bundle['bundle_local_fonts']
    st.ccli_licence_number = bundle['ccli_licence_number']
    st.preview_video_mode = bundle['preview_video_mode']
    st.bundled_font_css_map = bundle['bundled_font_css_map']
    st.export_dir = bundle['export_dir']
    st.active_profile_id = bundle['active_profile_id']
    st.effective_output_styles = {}
    st.current_mode = 'service'
    st.current_item_index = -1
    st.current_song_id = None
    st.current_song_title = ''
    st.current_song_lyrics = ''
    st.current_song_verse_order = None
    st.current_bible_data = {}
    st.current_ann_data = {}
    st.current_image_data = {}
    st.current_video_data = {}
    st.is_blank = False
    st.is_frozen = False
    st.video_pending = False
    st.video_is_playing = False
    st.video_start_time = 0.0
    st.video_start_position = 0.0
    st.video_pause_position = 0.0
    st.theme_resolver = ThemeResolver(st)
    st.slide_builder = SlideBuilder(st)
    st.player = PlayerController(st)
    st.get_output = types.MethodType(AppState.get_output, st)
    st.current_service_item = types.MethodType(AppState.current_service_item, st)
    st.current_item_type = types.MethodType(AppState.current_item_type, st)
    st._clear_outputs_and_player = types.MethodType(AppState._clear_outputs_and_player, st)
    st._reset_video_timing = types.MethodType(AppState._reset_video_timing, st)
    st._get_video_position = types.MethodType(AppState._get_video_position, st)
    return st


def _build_service_export_frames(bundle: dict) -> dict:
    """Walk a cloned AppState and emit per-slide packets for one output.

    Live APP_STATE is never mutated. Videos/dividers are skipped; blank/freeze
    are forced off so frames show real content. Non-target outputs are ignored
    for cheaper pagination during the walk.
    """
    st = _make_export_app_state(bundle)
    oc = st.get_output(bundle['output_name'])
    if oc is None:
        return {"ok": False, "error": "unknown_output"}

    items = list(st.current_service_items)
    builder = st.slide_builder
    frames = []
    skipped_videos = 0

    st.is_blank = False
    oc.is_blank = False
    for o in st.outputs:
        o.is_ignored = (o.name != oc.name)

    preload_images = st.theme_resolver._collect_service_background_images()

    for i, item in enumerate(items):
        it_type = item.get('item_type')
        if it_type == 'video':
            skipped_videos += 1
            continue
        if it_type == 'divider':
            continue

        st.current_mode = 'service'
        st.current_item_index = i
        st.current_song_id = item.get('song_id')
        st.current_song_title = item.get('title') or ''
        st.current_song_lyrics = item.get('lyrics') or ''
        st.current_song_verse_order = item.get('verse_order')
        builder._rebuild_slides_and_mappings()

        # Compute copyright locally — do not touch manager's live cache.
        copyright_base = _build_copyright_base(_get_copyright_song_payload(st), st) or ''
        _emit_item_frames(oc, i, it_type, copyright_base, preload_images, frames, st)

    svc = st.db.get_service(st.current_service_id) if st.current_service_id != -1 else None
    return {
        "ok": True,
        "service_name": (svc or {}).get('name', 'Service'),
        "output": oc.name,
        "canvas": {"width": oc.canvas_width, "height": oc.canvas_height},
        "count": len(frames),
        "skipped": {"videos": skipped_videos},
        "frames": frames,
    }


async def _refresh_current_service_items(prev_item_id):
    """Reload service items and re-point the live selection by item id.

    Returns True if the previously-live item was removed (display already cleared).
    DB fetch stays off the render lock; mutations that touch live slides/player
    run under ``_render_lock`` so they cannot interleave with rebuild/export.
    """
    if APP_STATE.current_service_id == -1:
        return False
    items = await _db_run(
        APP_STATE.db.get_service_items, APP_STATE.current_service_id)
    async with _render_lock:
        APP_STATE.current_service_items = items
        active_lost = APP_STATE.reconcile_active_item(prev_item_id)
        if active_lost:
            APP_STATE.clear_live_item()
            APP_STATE._clear_outputs_and_player()
        return active_lost

@app.get("/admin", response_class=HTMLResponse)
async def get_admin():
    return HTMLResponse(content=ADMIN_HTML)

# Externalized admin assets. Registered as literal paths before the /{filename}
# catch-all so they resolve here rather than being looked up in the export dir.
@app.get("/admin.css")
async def get_admin_css():
    # Starlette appends "; charset=utf-8" to text/* media types automatically.
    return Response(content=ADMIN_CSS, media_type="text/css")

@app.get("/admin.js")
async def get_admin_js():
    return Response(content=ADMIN_JS, media_type="text/javascript")

def _icon_response(data: Optional[bytes], media_type: str) -> Response:
    """Serve a preloaded icon, or 404 when the asset wasn't bundled."""
    if data is None:
        return Response(status_code=404)
    return Response(content=data, media_type=media_type)

@app.get("/favicon.png")
async def get_favicon():
    return _icon_response(FAVICON_BYTES, "image/png")

# Browsers auto-probe /favicon.ico even with a PNG <link>; serve the multi-res ICO.
@app.get("/favicon.ico")
async def get_favicon_ico():
    return _icon_response(FAVICON_ICO_BYTES, "image/x-icon")

# iOS requests both names when adding the web remote to the home screen.
@app.get("/assets/song-bar-texture.jpg")
async def get_song_bar_texture():
    if SONG_BAR_TEXTURE_BYTES is None:
        return Response(status_code=404)
    # Long-lived: the texture is an immutable bundled asset.
    return Response(content=SONG_BAR_TEXTURE_BYTES, media_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=31536000, immutable"})

@app.get("/apple-touch-icon.png")
@app.get("/apple-touch-icon-precomposed.png")
async def get_apple_touch_icon():
    return _icon_response(APPLE_TOUCH_ICON_BYTES, "image/png")

def _get_lan_ip() -> str:
    """Best-effort LAN IP of this machine — the address other devices use to reach it.

    Opens a UDP socket toward a public address and reads back the local endpoint the OS
    selected; this picks the primary outbound interface without sending any packets.
    Falls back to loopback when there's no usable network.
    """
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        return s.getsockname()[0]
    except Exception:
        return '127.0.0.1'
    finally:
        s.close()

@app.get("/api/admin-qr")
async def api_admin_qr(request: Request):
    """The admin page's URL on this machine's LAN address, plus a scannable QR code.

    The browser may have opened /admin via localhost, so we swap in the detected LAN IP
    while keeping the scheme and port the client actually connected on. The QR is an
    inline SVG data URI; if segno isn't installed only the URL is returned.
    """
    ip = _get_lan_ip()
    port = request.url.port or DEFAULT_PORT
    url = f"{request.url.scheme}://{ip}:{port}/admin"
    qr_uri = None
    if segno is not None:
        try:
            qr_uri = segno.make(url, error='m').svg_data_uri(scale=5, border=2, dark='#111111')
        except Exception:
            qr_uri = None
    return {"url": url, "qr": qr_uri}

@app.get("/{filename}")
async def get_root_file(filename: str):
    # Serve generated output pages only (e.g. /Main.html). Electron and OBS load
    # these at the site root; keep that URL shape, but refuse non-.html names so
    # this catch-all cannot shadow future routes or serve arbitrary export files.
    #
    # Starlette's {filename} converter won't match '/', so path traversal isn't
    # reachable today — but resolve and contain the path anyway so this can't
    # regress into serving arbitrary files if the route or framework changes.
    # _path_is_within (not raw commonpath): on Windows a name like "D:foo" makes
    # os.path.join reset to another drive and commonpath then *raises* ValueError
    # across drives — the containment check itself would become the 500.
    if (not filename
            or os.path.basename(filename) != filename
            or not filename.lower().endswith('.html')):
        raise HTTPException(status_code=404, detail="File not found")
    export_root = os.path.realpath(APP_STATE.export_dir)
    fpath = os.path.realpath(os.path.join(export_root, filename))
    if not _path_is_within(fpath, export_root):
        raise HTTPException(status_code=404, detail="File not found")
    if os.path.isfile(fpath):
        return FileResponse(fpath)
    raise HTTPException(status_code=404, detail="File not found")

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, client_type: str = 'admin', output_name: Optional[str] = None):
    await manager.connect(websocket, client_type, output_name)
    try:
        # Send initial state
        if client_type == 'admin':
            await manager.send_full_state_to(websocket)
        elif client_type == 'output':
             # Send initial state including mode and preload list so
             # the client starts with the correct background and caches
             # all service backgrounds immediately on (re)connect.
             oc = APP_STATE.get_output(output_name)
             if oc:
                 # Reuse the exact packet the broadcast path builds for this output,
                 # so a (re)connecting client renders identically to an already-connected
                 # one — same mode/media/blank/indicator/copyright resolution, with no
                 # separately-maintained reconnect variant to drift out of sync.
                 #
                 # Take the render lock first: a rebuild running in a worker thread
                 # mutates each OutputConfig's slides/index/style in place and is only
                 # consistent once it completes. Reading mid-rebuild could hand a fresh
                 # client a half-updated packet (new slides against a stale index, or a
                 # transient effective style). The lock makes this read wait for the
                 # in-flight rebuild to finish.
                 async with _render_lock:
                     pkt = manager._build_output_packets().get(output_name)
                 if pkt is not None:
                     await websocket.send_json(pkt)
        
        while True:
            # We don't expect much upstream data from clients via WS, mainly API calls.
            # But we must keep loop alive.
            await websocket.receive_text()
    except Exception:
        manager.disconnect(websocket)


# --- Video cold-start sync ---

VIDEO_SYNC_DELAY = 3.0            # cold start: hold current content for 3s while video buffers
VIDEO_SYNC_DELAY_PRELOADED = 1.0  # warm start: video already preloaded by client, shorter hold

def _cancel_pending_video_task():
    if APP_STATE.pending_video_task and not APP_STATE.pending_video_task.done():
        APP_STATE.pending_video_task.cancel()
    APP_STATE.pending_video_task = None
    APP_STATE.video_pending = False

async def _delayed_video_play(expected_filename: str, is_preloaded: bool = False):
    """Hold current display while video buffers, then atomically switch all clients to video."""
    delay = VIDEO_SYNC_DELAY_PRELOADED if is_preloaded else VIDEO_SYNC_DELAY
    await asyncio.sleep(delay)

    def _go():
        vd = APP_STATE.current_video_data
        if not (APP_STATE.video_pending and vd.get('filename') == expected_filename):
            return False
        APP_STATE.video_pending = False
        APP_STATE.video_is_playing = True
        APP_STATE.video_start_position = 0.0
        APP_STATE.video_start_time = time.time()
        return True

    await _live_commit(prepare=_go, broadcast='state')

# --- API Endpoints ---

@app.post("/api/app-settings")
async def api_app_settings(data: dict = Body(...)):
    bundle = bool(data.get('bundle_local_fonts', False))
    new_ccli = data.get('ccli_licence_number', APP_STATE.ccli_licence_number)
    pvm = data.get('preview_video_mode', 'still')
    raw_priority = data.get('theme_priority') if 'theme_priority' in data else None

    def _apply():
        APP_STATE.bundle_local_fonts = bundle
        if new_ccli != APP_STATE.ccli_licence_number:
            manager.invalidate_copyright_cache()
        APP_STATE.ccli_licence_number = new_ccli
        if pvm in ('disabled', 'still', 'live'):
            APP_STATE.preview_video_mode = pvm
        if isinstance(raw_priority, list) and sorted(raw_priority) == sorted(THEME_PRIORITY_TIERS):
            APP_STATE.theme_priority = list(raw_priority)
        APP_STATE.config_manager.save_config()

    await _live_commit(mutate=_apply, export=True, rebuild=True, broadcast='state')
    return {"success": True}

@app.get("/api/state")
async def api_get_state():
    include_songs = await manager._ensure_library_cache(force=True)
    async with _render_lock:
        return manager._build_admin_state(include_songs)['state']

def _current_is_image_mode() -> bool:
    return APP_STATE.effective_content_type() == 'image'

async def _nav_step(step: int) -> dict:
    """Shared body of /api/next and /api/prev (step is +1 or -1)."""
    if _current_is_image_mode():
        def _step_image():
            img_data = APP_STATE.current_image_data
            images = img_data.get('images', [])
            new_index = img_data.get('index', 0) + step
            if not (images and 0 <= new_index < len(images)):
                return False
            img_data['index'] = new_index
            return True
        await _live_commit(prepare=_step_image, broadcast='state')
        return {"success": True}
    def _step_lyrics():
        return APP_STATE.player.next_slide() if step > 0 else APP_STATE.player.prev_slide()
    await _live_commit(prepare=_step_lyrics, broadcast='nav')
    return {"success": True}

@app.post("/api/next")
async def api_next():
    return await _nav_step(1)

@app.post("/api/toggle-blank")
async def api_toggle_blank():
    def _flip():
        APP_STATE.is_blank = not APP_STATE.is_blank
    await _live_commit(prepare=_flip, broadcast='blank')
    return {"success": True}

@app.post("/api/toggle-output-blank")
async def api_toggle_output_blank(data: dict = Body(...)):
    """Toggle blank state for a specific output."""
    output_name = data.get('name')
    if not output_name:
        return {"success": False, "message": "Output name required"}

    oc = APP_STATE.get_output(output_name)
    if not oc:
        return {"success": False, "message": "Output not found"}

    def _flip():
        oc.is_blank = not oc.is_blank
    await _live_commit(prepare=_flip, broadcast='blank')
    return {"success": True, "is_blank": oc.is_blank}

@app.post("/api/toggle-freeze")
async def api_toggle_freeze():
    """Toggle global freeze. Holds every non-exempt output on its current frame."""
    def _flip():
        APP_STATE.is_frozen = not APP_STATE.is_frozen
    await _live_commit(mutate=_flip, reconcile_freeze=True, broadcast='freeze')
    return {"success": True, "is_frozen": APP_STATE.is_frozen}

@app.post("/api/toggle-output-freeze")
async def api_toggle_output_freeze(data: dict = Body(...)):
    """Toggle freeze state for a specific output."""
    output_name = data.get('name')
    if not output_name:
        return {"success": False, "message": "Output name required"}

    oc = APP_STATE.get_output(output_name)
    if not oc:
        return {"success": False, "message": "Output not found"}

    def _flip():
        oc.is_frozen = not oc.is_frozen
    await _live_commit(mutate=_flip, reconcile_freeze=True, broadcast='freeze')
    return {"success": True, "is_frozen": oc.is_frozen}

@app.post("/api/toggle-output-ignore")
async def api_toggle_output_ignore(data: dict = Body(...)):
    """Toggle ignore state for a specific output."""
    output_name = data.get('name')
    if not output_name:
        return {"success": False, "message": "Output name required"}

    oc = APP_STATE.get_output(output_name)
    if not oc:
        return {"success": False, "message": "Output not found"}

    def _flip():
        oc.is_ignored = not oc.is_ignored
    await _live_commit(mutate=_flip, rebuild=True, broadcast='state')
    return {"success": True, "is_ignored": oc.is_ignored}

@app.post("/api/prev")
async def api_prev():
    return await _nav_step(-1)

@app.post("/api/jump-to-line")
async def api_jump(data: dict = Body(...)):
    line_index = _coerce_int(data.get('line_index'), -1)
    success = await _live_commit(
        prepare=lambda: APP_STATE.player.jump_to_line(line_index),
        broadcast='nav')
    return {"success": success}

@app.post("/api/services/select-item")
async def api_select_item(data: dict = Body(...)):
    idx = _coerce_int(data.get('index'), -1)
    if not (0 <= idx < len(APP_STATE.current_service_items)):
        return {"success": False, "message": "Invalid item index"}
    item = APP_STATE.current_service_items[idx]
    image_index = data.get('image_index')
    is_video = item.get('item_type') == 'video'
    is_image_folder = item.get('item_type') == 'image_folder'

    def _apply():
        APP_STATE.current_mode = 'service'
        APP_STATE.current_item_index = idx
        APP_STATE.current_song_id = item.get('song_id')
        APP_STATE.current_song_title = item.get('title') or ''
        APP_STATE.current_song_lyrics = item.get('lyrics') or ''
        if item.get('item_type') == 'song':
            APP_STATE.current_song_verse_order = item.get('verse_order')
        else:
            APP_STATE.current_song_verse_order = None

    def _after():
        if is_image_folder and image_index is not None:
            images = APP_STATE.current_image_data.get('images', [])
            if images:
                APP_STATE.current_image_data['index'] = max(
                    0, min(_coerce_int(image_index, 0), len(images) - 1))
        _cancel_pending_video_task()
        if is_video:
            filename = APP_STATE.current_video_data.get('filename', '')
            wants_autoplay = APP_STATE.current_video_data.get('autoplay', True)
            APP_STATE.video_is_playing = False
            if wants_autoplay and filename:
                APP_STATE.video_pending = True
                APP_STATE.pending_video_task = asyncio.create_task(
                    _delayed_video_play(filename, is_preloaded=True))

    await _live_commit(mutate=_apply, rebuild=True, after_rebuild=_after, broadcast='state')
    return {"success": True}

@app.post("/api/select-video")
async def api_select_video(data: dict = Body(...)):
    filename = _normalize_video_filename(data.get('filename'))
    if not filename:
        return {"success": False, "message": "Valid video filename required"}
    autoplay = bool(data.get('autoplay', True))
    title = data.get('title') or filename
    loop = bool(data.get('loop', False))

    def _apply():
        APP_STATE.current_mode = 'video'
        APP_STATE.current_video_data = {
            'filename': filename,
            'title': title,
            'loop': loop,
            'autoplay': autoplay,
        }
        _cancel_pending_video_task()
        APP_STATE._reset_video_timing(autoplay=False)
        APP_STATE.current_item_index = -1
        APP_STATE.current_song_id = None
        APP_STATE.is_blank = False
        APP_STATE._clear_outputs_and_player()
        if autoplay:
            APP_STATE.video_pending = True
            APP_STATE.pending_video_task = asyncio.create_task(_delayed_video_play(filename))

    await _live_commit(mutate=_apply, broadcast='state')
    return {"success": True}


@app.post("/api/services/theme-map")
async def api_service_theme_map(data: dict = Body(...)):
    sid = data.get('id')
    theme_map = data.get('theme_map') or {}
    if not isinstance(theme_map, dict):
        return {"success": False, "message": "Invalid theme_map"}
    if not sid:
        return {"success": False, "message": "Missing service id"}

    try:
        sid_int = int(sid)
    except Exception:
        return {"success": False, "message": "Invalid service id"}

    await _db_run(APP_STATE.db.update_service_theme_map, sid_int, theme_map)
    await _live_commit(rebuild=True, broadcast='state')
    return {"success": True}

@app.post("/api/export/service-frames")
async def api_export_service_frames(data: dict = Body(...)):
    """Return ordered per-slide render packets for one output across the current service.

    Snapshot under `_render_lock`; the walk runs on a cloned AppState off the lock
    so live next/prev/blank stay responsive during export."""
    output_name = (data.get('output_name') or '').strip()
    if not output_name:
        return {"ok": False, "error": "missing_output_name"}
    if APP_STATE.current_service_id == -1:
        return {"ok": False, "error": "no_service"}
    async with _render_lock:
        bundle = _snapshot_export_bundle(output_name)
    if bundle is None:
        return {"ok": False, "error": "unknown_output"}
    return await asyncio.to_thread(_build_service_export_frames, bundle)


# --- Service image export: headless Chromium (Playwright) rasterization ---
# Frames come from the cloned walk above; this section only paints PNGs to a ZIP.

EXPORT_HARNESS_TIMEOUT = 20000   # ms to wait for the export page's harness to be ready
EXPORT_NAV_TIMEOUT = 30000       # ms to wait for the export page to load


async def _broadcast_export_progress(payload: dict):
    """Push an export-progress tick to admin clients (the requester filters by job_id)."""
    sends = [manager._safe_send(c["ws"], payload)
             for c in manager.active_connections if c["type"] == 'admin']
    if sends:
        await asyncio.gather(*sends)


def _safe_unlink(path: str):
    """Best-effort delete of a temp file (missing/permission errors ignored)."""
    try:
        os.remove(path)
    except OSError:
        pass


def _export_tmp_dir() -> str:
    """Temp ZIP dir under the data dir (not tmpfs). Sweeps ZIPs older than an hour."""
    d = os.path.join(get_data_dir(), 'export_tmp')
    os.makedirs(d, exist_ok=True)
    cutoff = time.time() - 3600
    try:
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if name.endswith('.zip') and os.path.getmtime(p) < cutoff:
                _safe_unlink(p)
    except OSError:
        pass
    return d


async def _render_export_zip(output_name: str, canvas: dict, frames: list, job_id: str) -> Optional[str]:
    """Rasterize frames with headless Chromium into an on-disk ZIP; return its path.

    One screenshot in memory at a time. Returns None if Playwright/Chromium is missing.
    """
    try:
        # Frozen builds ship Chromium with PLAYWRIGHT_BROWSERS_PATH=0.
        if getattr(sys, 'frozen', False):
            os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', '0')
        from playwright.async_api import async_playwright
    except ImportError:
        logger.warning("Image export unavailable: playwright is not installed")
        return None

    port = _SERVER_PORT or DEFAULT_PORT
    url = f"http://127.0.0.1:{port}/{urllib.parse.quote(output_name)}.html?export=1"
    w = int((canvas or {}).get('width') or 1920)
    h = int((canvas or {}).get('height') or 1080)
    total = len(frames)
    # Local content only; sandbox plumbing is unreliable in packaged apps.
    launch_args = ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu',
                   '--hide-scrollbars', '--force-color-profile=srgb']

    fd, zip_path = tempfile.mkstemp(suffix='.zip', prefix='service-', dir=_export_tmp_dir())
    os.close(fd)
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=launch_args)
            try:
                page = await browser.new_page(
                    viewport={'width': w, 'height': h}, device_scale_factor=1)
                page.set_default_timeout(EXPORT_NAV_TIMEOUT)
                await page.goto(url, wait_until='load')
                await page.wait_for_function("window.__ssExportReady === true",
                                             timeout=EXPORT_HARNESS_TIMEOUT)
                # PNGs are already compressed — store without deflate.
                with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_STORED) as z:
                    for i, frame in enumerate(frames):
                        await page.evaluate("(pkt) => window.__ssApplyExportFrame(pkt)", frame)
                        # Keep page alpha (transparent/OBS outputs stay real PNGs).
                        png = await page.screenshot(type='png', omit_background=True)
                        z.writestr(f"slide-{i + 1:03d}.png", png)
                        del png  # release before the next capture
                        await _broadcast_export_progress({
                            "type": "export_progress", "job_id": job_id,
                            "done": i + 1, "total": total})
            finally:
                await browser.close()
    except Exception:
        logger.exception("Headless render failed for output %r", output_name)
        _safe_unlink(zip_path)
        raise

    return zip_path


@app.post("/api/export/service-images")
async def api_export_service_images(data: dict = Body(...)):
    """Render the whole current service to one PNG per slide of `output_name` and
    stream back a ZIP. Frame walk uses a cloned AppState (brief lock for snapshot
    only); Chromium rasterization stays outside the lock."""
    output_name = (data.get('output_name') or '').strip()
    job_id = (data.get('job_id') or uuid.uuid4().hex).strip()
    if not output_name:
        return JSONResponse({"ok": False, "error": "missing_output_name"}, status_code=400)
    if APP_STATE.current_service_id == -1:
        return JSONResponse({"ok": False, "error": "no_service"}, status_code=400)

    async with _render_lock:
        bundle = _snapshot_export_bundle(output_name)
    if bundle is None:
        return JSONResponse({"ok": False, "error": "unknown_output"}, status_code=400)
    built = await asyncio.to_thread(_build_service_export_frames, bundle)
    if not built.get("ok"):
        return JSONResponse(built, status_code=400)
    if not built.get("count"):
        # Nothing to render (empty service, or the output shows no slides).
        return JSONResponse({"ok": True, "count": 0, "skipped": built.get("skipped", {})})

    try:
        zip_path = await _render_export_zip(
            output_name, built["canvas"], built["frames"], job_id)
    except Exception:
        return JSONResponse({"ok": False, "error": "render_failed"}, status_code=500)
    if zip_path is None:
        return JSONResponse({"ok": False, "error": "renderer_unavailable"}, status_code=503)

    safe = re.sub(r'[\\/:*?"<>|]+', '_', f"{built.get('service_name', 'Service')} - {output_name}").strip() or 'export'
    headers = {
        "X-Export-Count": str(built.get("count", 0)),
        "X-Export-Skipped-Videos": str((built.get("skipped") or {}).get("videos", 0)),
    }
    # Stream ZIP from disk, then delete the temp file once the response is sent.
    return FileResponse(
        zip_path, media_type="application/zip", filename=f"{safe}.zip", headers=headers,
        background=BackgroundTask(_safe_unlink, zip_path))

@app.post("/api/select-song")
async def api_select_song(data: dict = Body(...)):
    song_id = data.get('id')
    song = await _db_run(APP_STATE.db.get_song, song_id)
    if not song:
        return {"success": True}

    def _apply():
        _cancel_pending_video_task()
        APP_STATE.current_mode = 'song'
        APP_STATE.current_song_id = song_id
        APP_STATE.current_song_title = song['title']
        APP_STATE.current_song_lyrics = song['lyrics']
        APP_STATE.current_song_verse_order = song.get('verse_order')
        APP_STATE.current_item_index = -1

    await _live_commit(mutate=_apply, rebuild=True, broadcast='state')
    return {"success": True}

@app.post("/api/select-announcement")
async def api_select_announcement(data: dict = Body(...)):
    """Send a library announcement live directly, without adding it to a service."""
    item = await _db_run(APP_STATE.db.get_ann_item, data.get('id'))
    if not item:
        return {"success": False, "message": "Announcement not found"}
    ann_data = {
        'name': item.get('name', ''),
        'fields': item.get('fields', []),
        'theme_map': item.get('theme_map') or {},
    }

    def _apply():
        _cancel_pending_video_task()
        APP_STATE.current_mode = 'announcement'
        APP_STATE.current_ann_data = ann_data
        APP_STATE.current_item_index = -1
        APP_STATE.current_song_id = None
        APP_STATE.is_blank = False

    await _live_commit(mutate=_apply, rebuild=True, broadcast='state')
    return {"success": True}

# --- Service Helper Wrappers ---

@app.post("/api/services/create")
async def api_service_create(data: dict = Body(...)):
    new_id = await _db_run(
        APP_STATE.db.create_service, data.get('name', 'New Service'), data.get('group_id'))
    items = await _db_run(APP_STATE.db.get_service_items, new_id)

    def _apply():
        APP_STATE.current_service_id = new_id
        APP_STATE.current_service_items = items
        APP_STATE.current_mode = 'service'
        APP_STATE.current_item_index = -1
        APP_STATE.clear_live_item()
        manager.invalidate_library_cache()

    await _live_commit(mutate=_apply, broadcast='state')
    return {"success": True}

@app.post("/api/services/delete")
async def api_service_delete(data: dict = Body(...)):
    sid = data.get('id')
    await _db_run(APP_STATE.db.delete_service, sid)

    def _apply():
        if APP_STATE.current_service_id == sid:
            APP_STATE.current_service_id = -1
            APP_STATE.current_service_items = []
        manager.invalidate_library_cache()

    await _live_commit(mutate=_apply, broadcast='state')
    return {"success": True}

@app.post("/api/services/rename")
async def api_service_rename(data: dict = Body(...)):
    sid = data.get('id')
    new_name = data.get('name', '').strip()
    if not new_name:
        return {"success": False, "message": "Name cannot be empty"}
    await _db_run(APP_STATE.db.rename_service, sid, new_name)
    manager.invalidate_library_cache()
    await _live_commit(broadcast='library')
    return {"success": True}

@app.post("/api/services/select")
async def api_service_select(data: dict = Body(...)):
    sid = data.get('id')
    items = await _db_run(APP_STATE.db.get_service_items, sid)

    def _apply():
        APP_STATE.current_service_id = sid
        APP_STATE.current_service_items = items
        APP_STATE.current_mode = 'service'
        if items:
            APP_STATE.current_item_index = 0
            APP_STATE.current_song_id = items[0].get('song_id')
            APP_STATE.current_song_title = items[0]['title']
            APP_STATE.current_song_lyrics = items[0]['lyrics']
            APP_STATE.current_song_verse_order = items[0].get('verse_order')
        else:
            APP_STATE.current_item_index = -1
            APP_STATE.clear_live_item()

    await _live_commit(mutate=_apply, rebuild=True, broadcast='state')
    return {"success": True}

@app.post("/api/services/deselect")
async def api_service_deselect():
    def _apply():
        APP_STATE.current_service_id = -1
        APP_STATE.current_service_items = []
        APP_STATE.current_item_index = -1
        APP_STATE.current_mode = 'song'
        APP_STATE.clear_live_item()

    await _live_commit(mutate=_apply, broadcast='state')
    return {"success": True}

# --- Service groups (one-level organization of services) ---
@app.post("/api/service-groups/create")
async def api_service_group_create(data: dict = Body(...)):
    name = (data.get('name') or '').strip()
    if not name:
        return {"success": False, "message": "Name required"}
    gid = await _db_run(APP_STATE.db.create_service_group, name)
    manager.invalidate_library_cache()
    await _live_commit(broadcast='library')
    return {"success": True, "id": gid}

@app.post("/api/service-groups/rename")
async def api_service_group_rename(data: dict = Body(...)):
    gid = data.get('id')
    name = (data.get('name') or '').strip()
    if not gid or not name:
        return {"success": False, "message": "id and name required"}
    await _db_run(APP_STATE.db.rename_service_group, gid, name)
    manager.invalidate_library_cache()
    await _live_commit(broadcast='library')
    return {"success": True}

@app.post("/api/service-groups/delete")
async def api_service_group_delete(data: dict = Body(...)):
    """Delete a group; its services are kept and returned to the ungrouped list."""
    gid = data.get('id')
    if not gid:
        return {"success": False, "message": "id required"}
    await _db_run(APP_STATE.db.delete_service_group, gid)
    manager.invalidate_library_cache()
    await _live_commit(broadcast='library')
    return {"success": True}

@app.post("/api/services/move")
async def api_service_move(data: dict = Body(...)):
    """Move a service into a group (group_id None = ungrouped) and optionally reorder the
    destination bucket via ordered_ids."""
    sid = data.get('id')
    if not sid:
        return {"success": False, "message": "id required"}
    await _db_run(APP_STATE.db.move_service_to_group, sid, data.get('group_id'), data.get('ordered_ids'))
    manager.invalidate_library_cache()
    await _live_commit(broadcast='library')
    return {"success": True}

def _coerce_int(val, default=0):
    """Coerce a request-body value to int, returning `default` for None or
    non-numeric input. Endpoint handlers must never cast client JSON directly —
    a malformed payload from any LAN client would raise an unhandled 500 (and
    can leave state half-updated between a rebuild and its broadcast); funneling
    through here degrades it to the caller's safe default instead."""
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _coerce_at_index(data):
    """Read an optional service-item insertion position from a request body. Returns a
    non-negative int (drag-drop placement) or None (append). Non-numeric input → None
    so a malformed value degrades to the safe append behaviour rather than erroring."""
    val = data.get('at_index')
    if val is None:
        return None
    try:
        idx = int(val)
    except (TypeError, ValueError):
        return None
    return idx if idx >= 0 else 0


@app.post("/api/services/add-song")
async def api_service_add_song(data: dict = Body(...)):
    sid = APP_STATE.current_service_id
    if sid != -1:
        prev_item_id = APP_STATE.active_item_id()
        await _db_run(APP_STATE.db.add_song_to_service, sid, data.get('song_id'), _coerce_at_index(data))
        await _refresh_current_service_items(prev_item_id)
        manager.invalidate_library_cache()
        # Appending a song changes neither the live item nor any output's preload media,
        # so only the admin panel needs the refreshed item list.
        await manager.broadcast_library_state()
    return {"success": True}

@app.post("/api/services/reorder-items")
async def api_service_reorder_items(data: dict = Body(...)):
    sid = APP_STATE.current_service_id
    ordered_ids = data.get('ordered_ids', [])
    if sid == -1 or not ordered_ids:
        return {"success": False, "message": "No active service or empty order"}
    prev_item_id = APP_STATE.active_item_id()
    await _db_run(APP_STATE.db.reorder_service_items, sid, ordered_ids)
    # Reorder never removes items, so the live item simply moves to a new index.
    await _refresh_current_service_items(prev_item_id)
    manager.invalidate_library_cache()
    # Reorder only changes list positions; live slides and preload lists are unchanged.
    await manager.broadcast_library_state()
    return {"success": True}

@app.post("/api/services/remove-item")
async def api_service_remove_item(data: dict = Body(...)):
    prev_item_id = APP_STATE.active_item_id()
    await _db_run(APP_STATE.db.remove_item_from_service, data.get('item_id'))
    # Off the loop: the sweep scans every image/image_folder service item (JSON
    # parse per row) and unlinks files — inline it stalls all connected WebSockets.
    await asyncio.to_thread(
        APP_STATE.db.cleanup_orphan_hidden_images, os.path.join(APP_STATE.export_dir, 'images'))
    if APP_STATE.current_service_id != -1:
        # Reconcile by id: the live item keeps its slides if it survived (only its
        # position shifted), or the display is cleared if it was the one removed.
        active_lost = await _refresh_current_service_items(prev_item_id)
        manager.invalidate_library_cache()
        if active_lost:
            await manager.broadcast_state()
        else:
            await manager.broadcast_library_state()
    return {"success": True}

@app.post("/api/services/remove-items")
async def api_service_remove_items(data: dict = Body(...)):
    """Bulk delete service items (one DB transaction + one broadcast)."""
    ids = data.get('item_ids') or []
    if not ids:
        return {"success": False, "message": "item_ids required"}
    prev_item_id = APP_STATE.active_item_id()
    deleted = await _db_run(APP_STATE.db.remove_items_from_service, ids)
    await asyncio.to_thread(
        APP_STATE.db.cleanup_orphan_hidden_images, os.path.join(APP_STATE.export_dir, 'images'))
    if APP_STATE.current_service_id != -1:
        active_lost = await _refresh_current_service_items(prev_item_id)
        manager.invalidate_library_cache()
        if active_lost:
            await manager.broadcast_state()
        else:
            await manager.broadcast_library_state()
    return {"success": True, "deleted": deleted}

@app.post("/api/services/add-songs")
async def api_service_add_songs(data: dict = Body(...)):
    """Bulk add songs to the current service (one DB transaction + one broadcast)."""
    sid = APP_STATE.current_service_id
    if sid == -1:
        return {"success": False, "message": "No service selected"}
    ids = data.get('song_ids') or []
    if not ids:
        return {"success": False, "message": "song_ids required"}
    prev_item_id = APP_STATE.active_item_id()
    added = await _db_run(APP_STATE.db.add_songs_to_service, sid, ids, _coerce_at_index(data))
    await _refresh_current_service_items(prev_item_id)
    manager.invalidate_library_cache()
    # Appending songs changes neither the live item nor any output's preload media.
    await manager.broadcast_library_state()
    return {"success": True, "added": added}

@app.post("/api/services/update-item")
async def api_service_update_item(data: dict = Body(...)):
    item_id = data.get('item_id')
    if not item_id:
        return {"success": False, "message": "Missing item_id"}

    if isinstance(data.get('lyrics'), str):
        data['lyrics'] = _sanitize_lyrics(data['lyrics'])

    def _compute():
        with APP_STATE.db._db_transaction(commit=False) as cur:
            cur.execute(
                "SELECT item_type, data, song_id FROM service_items WHERE id = ?",
                (item_id,))
            row = cur.fetchone()
        if not row:
            return None, "Item not found"
        existing_data = APP_STATE.db._parse_json_field(row['data'], {})
        return APP_STATE.db.compute_updated_service_item_data(
            data, row['item_type'], existing_data, row['song_id'])

    new_data, err = await _db_run(_compute)
    if err:
        return {"success": False, "message": err}

    prev_item_id = APP_STATE.active_item_id()
    await _db_run(APP_STATE.db.update_service_item, item_id, new_data)

    if APP_STATE.current_service_id != -1:
        await _refresh_current_service_items(prev_item_id)

    def _apply_live():
        item = APP_STATE.current_service_item()
        if item is not None and item.get('item_id') == item_id:
            APP_STATE.current_song_title = item.get('title', '')
            APP_STATE.current_song_lyrics = item.get('lyrics', '')
        manager.invalidate_library_cache()

    await _live_commit(mutate=_apply_live, rebuild=True, broadcast='state')
    return {"success": True}

# --- Song Editing ---

def _song_fields_from_request(data: dict) -> dict:
    """The song-record fields shared by /api/songs/create and /api/songs/update,
    in add_song/update_song positional order."""
    theme_map = data.get('theme_map') or {}
    if not isinstance(theme_map, dict):
        theme_map = {}
    return {
        'title': data.get('title'),
        'lyrics': _sanitize_lyrics(data.get('lyrics')),
        'verse_order': data.get('verse_order'),
        'authors': data.get('authors', []),
        'songbook_name': data.get('songbook_name', ''),
        'songbook_entry': data.get('songbook_entry', ''),
        'theme_map': theme_map,
        'copyright_text': data.get('copyright', ''),
        'ccli_song_number': data.get('ccli_song_number', ''),
        'show_copyright': data.get('show_copyright', False),
        'key': data.get('key', ''),
    }

@app.post("/api/songs/create")
async def api_song_create(data: dict = Body(...)):
    f = _song_fields_from_request(data)

    if not f['title'] or not f['lyrics']:
        return {'success': False, 'message': 'Title and lyrics required'}

    lyrics_error = _validate_no_blank_lines_in_verse(f['lyrics'])
    if lyrics_error:
        return {'success': False, 'message': lyrics_error}

    order_error = _validate_verse_order(f['verse_order'], f['lyrics'])
    if order_error:
        return {'success': False, 'message': order_error}

    await _db_run(
        APP_STATE.db.add_song, f['title'], f['lyrics'], f['verse_order'], f['authors'],
        f['songbook_name'], f['songbook_entry'], f['theme_map'],
        f['copyright_text'], f['ccli_song_number'], f['show_copyright'], f['key'])
    manager.invalidate_library_cache()
    await _live_commit(broadcast='library')
    return {"success": True}

@app.post("/api/songs/update")
async def api_song_update(data: dict = Body(...)):
    song_id = data.get('id')
    f = _song_fields_from_request(data)
    title, lyrics, verse_order = f['title'], f['lyrics'], f['verse_order']

    if not song_id or not title:
        return {'success': False, 'message': 'Song id and title required'}

    if lyrics:
        lyrics_error = _validate_no_blank_lines_in_verse(lyrics)
        if lyrics_error:
            return {'success': False, 'message': lyrics_error}

    order_error = _validate_verse_order(verse_order, lyrics)
    if order_error:
        return {'success': False, 'message': order_error}

    await _db_run(
        APP_STATE.db.update_song, song_id, title, lyrics, verse_order, f['authors'],
        f['songbook_name'], f['songbook_entry'], f['theme_map'],
        f['copyright_text'], f['ccli_song_number'], f['show_copyright'], f['key'])

    if APP_STATE.current_mode == 'song' and APP_STATE.current_song_id == song_id:
        def _apply():
            APP_STATE.current_song_title = title
            APP_STATE.current_song_lyrics = lyrics
            APP_STATE.current_song_verse_order = verse_order
            manager.invalidate_copyright_cache()
            manager.invalidate_library_cache()
        await _live_commit(mutate=_apply, rebuild=True, broadcast='state')
    else:
        manager.invalidate_library_cache()
        await _live_commit(broadcast='library')

    return {'success': True}

@app.post("/api/songs/delete")
async def api_song_delete(data: dict = Body(...)):
    song_id = data.get('id')
    prev_item_id = APP_STATE.active_item_id()
    was_live_standalone = (
        APP_STATE.current_mode == 'song' and APP_STATE.current_song_id == song_id)
    await _db_run(APP_STATE.db.delete_song, song_id)
    # Service items that referenced this song were snapshotted + detached — refresh
    # so the admin list shows the kept rows (song_id null) instead of ghosts.
    outputs_changed = False
    if APP_STATE.current_service_id != -1:
        if await _refresh_current_service_items(prev_item_id):
            outputs_changed = True
    if was_live_standalone:
        APP_STATE.clear_live_item()
        APP_STATE._clear_outputs_and_player()
        outputs_changed = True
    manager.invalidate_library_cache()
    if outputs_changed:
        await manager.broadcast_state()
    else:
        await manager.broadcast_library_state()
    return {"success": True}

@app.post("/api/songs/delete-many")
async def api_songs_delete_many(data: dict = Body(...)):
    ids = data.get('ids') or []
    if not ids:
        return {"success": False, "message": "ids required"}
    id_set = set(ids)
    prev_item_id = APP_STATE.active_item_id()
    was_live_standalone = (
        APP_STATE.current_mode == 'song' and APP_STATE.current_song_id in id_set)
    deleted = await _db_run(APP_STATE.db.delete_songs, ids)
    outputs_changed = False
    if APP_STATE.current_service_id != -1:
        if await _refresh_current_service_items(prev_item_id):
            outputs_changed = True
    if was_live_standalone:
        APP_STATE.clear_live_item()
        APP_STATE._clear_outputs_and_player()
        outputs_changed = True
    manager.invalidate_library_cache()
    if outputs_changed:
        await manager.broadcast_state()
    else:
        await manager.broadcast_library_state()
    return {"success": True, "deleted": deleted}

@app.get("/api/songs/{song_id}")
async def api_song_get(song_id: int):
    song = await _db_run(APP_STATE.db.get_song, song_id)
    if not song:
        raise HTTPException(status_code=404, detail="Song not found")
    return song

@app.get("/api/services/items/{item_id}")
async def api_service_item_get(item_id: int):
    """Full resolved service item (including lyrics). Admin WS state omits lyrics
    from the list payload; the song editor fetches here on open.

    Prefer the in-memory current-service copy when present (matches live edits
    not yet re-read from DB); otherwise load from the database.
    """
    for it in APP_STATE.current_service_items:
        if it.get('item_id') == item_id:
            return it
    item = await _db_run(APP_STATE.db.get_service_item, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Service item not found")
    return item

# --- Per-output announcement layouts (v2 model) ---
# A layout is the announcement analogue of a text theme: it belongs to one output,
# names an ordered set of slots, and positions them with the box/lines model. An
# announcement item's field values fill the slots by order at render time.

def _output_name_at(idx):
    """Resolve a UI output index to its stable output_name (the layout storage
    key), or None if the index is out of range."""
    if isinstance(idx, int) and 0 <= idx < len(APP_STATE.outputs):
        return APP_STATE.outputs[idx].name
    return None

@app.get("/api/ann-layouts/{output_index}")
async def api_get_ann_layouts(output_index: int):
    name = _output_name_at(output_index)
    if name is None:
        return {"success": False, "message": "Invalid output index", "layouts": []}
    return {"success": True, "layouts": await _db_run(APP_STATE.db.get_ann_layouts, name)}

@app.post("/api/ann-layouts")
async def api_create_ann_layout(data: dict = Body(...)):
    name = _output_name_at(data.get('output_index'))
    if name is None:
        return {"success": False, "message": "Invalid output index"}
    lid = await _db_run(
        APP_STATE.db.create_ann_layout,
        name, (data.get('name') or '').strip() or 'Layout',
        data.get('slot_names') or [], data.get('text_boxes') or [],
        data.get('background_type') or 'color',
        data.get('background_value') if data.get('background_value') is not None else '#000000',
        _normalize_theme_tags(data.get('tags')))
    manager.invalidate_library_cache()
    await _live_commit(broadcast='library')
    return {"success": True, "id": lid}

@app.post("/api/ann-layouts/update")
async def api_update_ann_layout(data: dict = Body(...)):
    lid = data.get('id')
    if not isinstance(lid, int):
        return {"success": False, "message": "Invalid layout id"}
    await _db_run(
        APP_STATE.db.update_ann_layout,
        lid, (data.get('name') or '').strip() or 'Layout',
        data.get('slot_names') or [], data.get('text_boxes') or [],
        data.get('background_type') or 'color',
        data.get('background_value') if data.get('background_value') is not None else '#000000',
        _normalize_theme_tags(data['tags']) if 'tags' in data else None)
    manager.invalidate_library_cache()
    await _live_commit(rebuild=True, broadcast='state')
    return {"success": True}

@app.post("/api/ann-layouts/delete")
async def api_delete_ann_layout(data: dict = Body(...)):
    lid = data.get('id')
    if not isinstance(lid, int):
        return {"success": False, "message": "Invalid layout id"}
    # A deleted layout id lingering in an announcement's per-output assignment simply
    # resolves to nothing at build time (get_ann_layout → None), so no scrub is needed.
    await _db_run(APP_STATE.db.delete_ann_layout, lid)
    manager.invalidate_library_cache()
    await _live_commit(rebuild=True, broadcast='state')
    return {"success": True}

# --- Announcement library: items + folders ---
# First-class, reusable announcement items organized in nestable folders (mirrors
# the image library). The library ships in the broadcast admin state (like songs),
# so each mutation invalidates the library cache and broadcasts so every connected
# admin re-renders.

async def _ann_library_changed():
    manager.invalidate_library_cache()
    await _live_commit(broadcast='library')

@app.post("/api/ann-items")
async def api_create_ann_item(data: dict = Body(...)):
    name = (data.get('name') or '').strip()
    if not name:
        return {"success": False, "message": "Name required"}
    item_id = await _db_run(
        APP_STATE.db.create_ann_item,
        name, data.get('folder_id'), data.get('fields') or [], data.get('theme_map') or {},
        _normalize_theme_tags(data.get('tags')))
    await _ann_library_changed()
    return {"success": True, "id": item_id}

@app.post("/api/ann-items/update")
async def api_update_ann_item(data: dict = Body(...)):
    item_id = data.get('id')
    if not isinstance(item_id, int):
        return {"success": False, "message": "Invalid item id"}
    if not await _db_run(APP_STATE.db.get_ann_item, item_id):
        return {"success": False, "message": "Item not found"}
    name = (data.get('name') or '').strip() or 'Announcement'
    await _db_run(
        APP_STATE.db.update_ann_item, item_id, name, data.get('fields') or [],
        data.get('theme_map') or {},
        _normalize_theme_tags(data['tags']) if 'tags' in data else None)
    await _ann_library_changed()
    return {"success": True}

@app.post("/api/ann-items/delete")
async def api_delete_ann_item(data: dict = Body(...)):
    item_id = data.get('id')
    if not isinstance(item_id, int):
        return {"success": False, "message": "Invalid item id"}
    await _db_run(APP_STATE.db.delete_ann_item, item_id)
    await _ann_library_changed()
    return {"success": True}

@app.post("/api/ann-items/delete-many")
async def api_delete_ann_items(data: dict = Body(...)):
    """Bulk-delete library announcements and/or folders (one broadcast)."""
    ids = data.get('ids') or []
    folder_ids = data.get('folder_ids') or []
    if not all(isinstance(i, int) for i in list(ids) + list(folder_ids)):
        return {"success": False, "message": "Invalid item id"}
    if not ids and not folder_ids:
        return {"success": False, "message": "ids or folder_ids required"}
    await _db_run(APP_STATE.db.delete_ann_items, ids)
    await _db_run(APP_STATE.db.delete_ann_folders, folder_ids)
    await _ann_library_changed()
    return {"success": True}

@app.post("/api/ann-items/duplicate")
async def api_duplicate_ann_item(data: dict = Body(...)):
    item_id = data.get('id')
    if not isinstance(item_id, int):
        return {"success": False, "message": "Invalid item id"}
    new_id = await _db_run(APP_STATE.db.duplicate_ann_item, item_id)
    if new_id is None:
        return {"success": False, "message": "Item not found"}
    await _ann_library_changed()
    return {"success": True, "id": new_id}

@app.post("/api/ann-items/duplicate-many")
async def api_duplicate_ann_items(data: dict = Body(...)):
    """Bulk-duplicate library announcements (one broadcast); missing ids are skipped."""
    ids = data.get('ids') or []
    if not ids or not all(isinstance(i, int) for i in ids):
        return {"success": False, "message": "ids required"}
    new_ids = await _db_run(
        lambda: [nid for nid in (APP_STATE.db.duplicate_ann_item(i) for i in ids) if nid is not None])
    await _ann_library_changed()
    return {"success": True, "ids": new_ids}

@app.post("/api/ann-items/move")
async def api_move_ann_item(data: dict = Body(...)):
    item_id = data.get('id')
    if not isinstance(item_id, int):
        return {"success": False, "message": "Invalid item id"}
    await _db_run(APP_STATE.db.move_ann_item, item_id, data.get('folder_id'), data.get('ordered_ids') or [])
    await _ann_library_changed()
    return {"success": True}

@app.post("/api/ann-folders/create")
async def api_create_ann_folder(data: dict = Body(...)):
    name = (data.get('name') or 'New Folder').strip()
    if not name:
        return {"success": False, "message": "Name required"}
    folder_id = await _db_run(APP_STATE.db.create_ann_folder, name, data.get('parent_id'))
    await _ann_library_changed()
    return {"success": True, "id": folder_id}

@app.post("/api/ann-folders/rename")
async def api_rename_ann_folder(data: dict = Body(...)):
    folder_id = data.get('id')
    name = (data.get('name') or '').strip()
    if not folder_id or not name:
        return {"success": False, "message": "ID and name required"}
    await _db_run(APP_STATE.db.rename_ann_folder, folder_id, name)
    await _ann_library_changed()
    return {"success": True}

@app.post("/api/ann-folders/move")
async def api_move_ann_folder(data: dict = Body(...)):
    """Re-parent and/or reorder a library folder. parent_id None = top level;
    ordered_ids is the destination parent's child folders in their desired order."""
    folder_id = data.get('id')
    if not folder_id:
        return {"success": False, "message": "ID required"}
    ok = await _db_run(APP_STATE.db.move_ann_folder, folder_id, data.get('parent_id'), data.get('ordered_ids') or [])
    await _ann_library_changed()
    return {"success": ok}

@app.post("/api/ann-folders/delete")
async def api_delete_ann_folder(data: dict = Body(...)):
    """Delete a library folder and its subfolders; items in the subtree re-home to
    the top level (never destroyed)."""
    folder_id = data.get('id')
    if not folder_id:
        return {"success": False, "message": "ID required"}
    await _db_run(APP_STATE.db.delete_ann_folder, folder_id)
    await _ann_library_changed()
    return {"success": True}

@app.post("/api/services/add-announcement")
async def api_add_announcement_to_service(data: dict = Body(...)):
    """Add a v2 announcement to the current service as a self-contained snapshot.

    A plain add sends `item_id` and the server snapshots that library item; the
    quick-edit-on-add path sends the (edited) `name`/`fields`/`theme_map` directly.
    Either may be combined — provided fields override the library base.
    """
    service_id = APP_STATE.current_service_id
    if service_id == -1:
        raise HTTPException(status_code=400, detail="No service selected")
    base = await _db_run(APP_STATE.db.get_ann_item, data['item_id']) if data.get('item_id') is not None else {}
    base = base or {}
    name = data['name'] if data.get('name') is not None else base.get('name', '')
    fields = data['fields'] if 'fields' in data else base.get('fields', [])
    theme_map = data['theme_map'] if 'theme_map' in data else (base.get('theme_map') or {})
    if not (name or fields):
        return {"success": False, "message": "Nothing to add"}
    prev_item_id = APP_STATE.active_item_id()
    await _db_run(
        APP_STATE.db.add_announcement_to_service, service_id, name or 'Announcement',
        fields, theme_map, _coerce_at_index(data))
    await _refresh_current_service_items(prev_item_id)
    manager.invalidate_library_cache()
    await _live_commit(broadcast='library')
    return {"success": True}

@app.post("/api/services/add-announcements")
async def api_add_announcements_to_service(data: dict = Body(...)):
    """Bulk add library announcements to the current service (one DB transaction +
    one broadcast — replaces the client-side per-item add loop, mirroring the
    add-songs / add-videos batch endpoints). Each item is snapshotted exactly as
    the single add with a bare item_id would; ids that no longer exist are skipped."""
    service_id = APP_STATE.current_service_id
    if service_id == -1:
        raise HTTPException(status_code=400, detail="No service selected")
    ids = data.get('item_ids') or []
    if not ids or not all(isinstance(i, int) for i in ids):
        return {"success": False, "message": "item_ids required"}
    prev_item_id = APP_STATE.active_item_id()
    added = await _db_run(APP_STATE.db.add_announcements_to_service, service_id, ids, _coerce_at_index(data))
    await _refresh_current_service_items(prev_item_id)
    manager.invalidate_library_cache()
    await _live_commit(broadcast='library')
    return {"success": True, "added": added}

@app.post("/api/services/update-announcement")
async def api_update_announcement_in_service(data: dict = Body(...)):
    """Update a service announcement in place, writing the v2 snapshot. A legacy
    (template-based) item migrates to v2 here — the editor sends the uniform
    name/fields it was shown, so nothing is lost."""
    item_id = data.get('item_id')
    if not item_id:
        return {"success": False, "message": "item_id required"}
    items = APP_STATE.current_service_items
    item = next((i for i in items if i.get('item_id') == item_id), None)
    if not item:
        return {"success": False, "message": "Item not found"}
    name = data['name'] if data.get('name') is not None else item.get('name', '')
    fields = data['fields'] if 'fields' in data else (item.get('fields') or [])
    theme_map = data['theme_map'] if data.get('theme_map') is not None else (item.get('theme_map') or {})
    new_data = {'name': name or 'Announcement', 'fields': fields, 'theme_map': theme_map,
                'user_modified': True}
    prev_item_id = APP_STATE.active_item_id()
    await _db_run(APP_STATE.db.update_service_item, item_id, new_data)
    await _refresh_current_service_items(prev_item_id)
    await _live_commit(rebuild=True, broadcast='state')
    return {"success": True}

@app.post("/api/services/move-folder-image")
async def api_move_folder_image(data: dict = Body(...)):
    """Move an image between image_folder items within the current service (service-scoped)."""
    from_item_id = data.get('from_item_id')
    to_item_id = data.get('to_item_id')
    from_index = data.get('from_index')
    to_index = data.get('to_index')
    if from_item_id is None or to_item_id is None or from_index is None:
        return {"success": False, "message": "from_item_id, to_item_id, from_index required"}
    prev_item_id = APP_STATE.active_item_id()
    ok = await _db_run(APP_STATE.db.move_service_folder_image, from_item_id, from_index, to_item_id, to_index)
    if not ok:
        return {"success": False, "message": "Move failed"}
    active_lost = await _refresh_current_service_items(prev_item_id)
    # Rebuild so the active folder's live image list reflects the change (unless the
    # active item itself went away, in which case the display was already cleared).
    if not active_lost:
        await _live_commit(rebuild=True, broadcast='state')
    return {"success": True}

@app.post("/api/services/move-folder-images")
async def api_move_folder_images(data: dict = Body(...)):
    """Move several selected images into one image_folder item within the service (service-scoped)."""
    selections = data.get('selections') or []
    to_item_id = data.get('to_item_id')
    to_index = data.get('to_index')
    if not selections or to_item_id is None:
        return {"success": False, "message": "selections and to_item_id required"}
    prev_item_id = APP_STATE.active_item_id()
    ok = await _db_run(APP_STATE.db.move_service_folder_images, selections, to_item_id, to_index)
    if not ok:
        return {"success": False, "message": "Move failed"}
    active_lost = await _refresh_current_service_items(prev_item_id)
    if not active_lost:
        await _live_commit(rebuild=True, broadcast='state')
    return {"success": True}

@app.post("/api/services/add-video")
async def api_add_video_to_service(data: dict = Body(...)):
    service_id = APP_STATE.current_service_id
    if service_id == -1:
        raise HTTPException(status_code=400, detail="No service selected")
    filename = _normalize_video_filename(data.get('filename'))
    if not filename:
        return {"success": False, "message": "Valid video filename required"}
    video_data = {
        'filename': filename,
        'title': data.get('title') or filename,
        'loop': bool(data.get('loop', False)),
        'autoplay': bool(data.get('autoplay', True)),
    }
    prev_item_id = APP_STATE.active_item_id()
    await _db_run(APP_STATE.db.add_video_to_service, service_id, video_data, _coerce_at_index(data))
    await _refresh_current_service_items(prev_item_id)
    manager.invalidate_library_cache()
    # broadcast_state (not library_state): a new video item adds to every output's
    # preload_videos list, which output clients need to pre-buffer.
    await manager.broadcast_state()
    return {"success": True}

@app.post("/api/services/add-videos")
async def api_add_videos_to_service(data: dict = Body(...)):
    """Add several videos to the current service in one transaction and one
    broadcast (the multi-select add — replaces the client-side per-video loop).
    Same per-item defaults as the single add: title = filename, autoplay, no loop."""
    service_id = APP_STATE.current_service_id
    if service_id == -1:
        raise HTTPException(status_code=400, detail="No service selected")
    filenames = []
    for f in (data.get('filenames') or []):
        name = _normalize_video_filename(f)
        if name:
            filenames.append(name)
    if not filenames:
        return {"success": False, "message": "filenames required"}
    prev_item_id = APP_STATE.active_item_id()
    added = await _db_run(APP_STATE.db.add_videos_to_service, service_id, filenames, _coerce_at_index(data))
    await _refresh_current_service_items(prev_item_id)
    manager.invalidate_library_cache()
    # broadcast_state (not library_state): new video items add to every output's
    # preload_videos list, which output clients need to pre-buffer.
    await manager.broadcast_state()
    return {"success": True, "added": added}

@app.post("/api/services/add-video-folder")
async def api_add_video_folder_to_service(data: dict = Body(...)):
    """Add every video in a library folder to the current service, in folder order,
    each as its own video item (the drag-a-folder-to-service equivalent)."""
    service_id = APP_STATE.current_service_id
    if service_id == -1:
        raise HTTPException(status_code=400, detail="No service selected")
    folder_id = data.get('folder_id')
    if not folder_id:
        return {"success": False, "message": "folder_id required"}
    prev_item_id = APP_STATE.active_item_id()
    added = await _db_run(APP_STATE.db.add_video_folder_to_service, service_id, folder_id, _coerce_at_index(data))
    await _refresh_current_service_items(prev_item_id)
    manager.invalidate_library_cache()
    await manager.broadcast_state()
    return {"success": True, "added": added}

# Allowed video container extensions, and a hard upload-size cap. Uploads are
# served as static files and stored on the presentation machine's disk, so an
# unbounded or arbitrary-type upload is both a disk-fill risk and a way to drop
# unexpected files into the served directory. 4 GiB covers a long service video.
VIDEO_EXTS = {'.mp4', '.webm', '.ogg', '.mov', '.avi', '.mkv'}
MAX_VIDEO_BYTES = 4 * 1024 * 1024 * 1024

# Raster/vector image types accepted for the content-image library and the
# background-theme image store. 64 MiB is generous for a high-resolution backdrop
# while still capping a runaway/malicious upload.
IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.svg'}
MAX_BACKGROUND_BYTES = 64 * 1024 * 1024
# Content-library images share the same per-file cap as theme backgrounds.
MAX_IMAGE_BYTES = MAX_BACKGROUND_BYTES


def _normalize_video_filename(raw) -> Optional[str]:
    """Return a safe on-disk video basename under export_dir/videos, or None.

    Strips any directory component, requires a known extension, and verifies the
    file exists inside the videos directory (path-traversal safe).
    """
    name = os.path.basename(str(raw or ''))
    if not name or os.path.splitext(name)[1].lower() not in VIDEO_EXTS:
        return None
    videos_dir = os.path.realpath(os.path.join(APP_STATE.export_dir, 'videos'))
    path = os.path.realpath(os.path.join(videos_dir, name))
    if not _path_is_within(path, videos_dir) or not os.path.isfile(path):
        return None
    return name


class _UploadTooLarge(Exception):
    """Raised when a streamed upload exceeds its size cap."""


class _UploadBadType(Exception):
    """Raised when an upload's extension is not in the allowed set."""


def _stream_upload_capped(file: UploadFile, dest: str, max_bytes: int):
    """Stream an UploadFile to `dest` in chunks, aborting (and removing the partial
    file) if it exceeds max_bytes. Blocking — call via asyncio.to_thread."""
    written = 0
    try:
        with open(dest, 'wb') as f:
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise _UploadTooLarge()
                f.write(chunk)
    except BaseException:
        if os.path.exists(dest):
            try:
                os.unlink(dest)
            except OSError:
                pass
        raise


def _unique_dest_path(directory: str, filename: str) -> tuple:
    """Pick a non-colliding destination path in `directory` for `filename`.
    If the name is already in use, suffix the basename with ' (N)' until free.
    Returns (full_path, final_filename)."""
    base, ext = os.path.splitext(filename)
    candidate = filename
    n = 1
    while os.path.exists(os.path.join(directory, candidate)):
        candidate = f"{base} ({n}){ext}"
        n += 1
    return os.path.join(directory, candidate), candidate

@app.post("/api/videos/upload")
async def api_video_upload(file: UploadFile = File(...)):
    videos_dir = os.path.join(APP_STATE.export_dir, 'videos')
    os.makedirs(videos_dir, exist_ok=True)
    raw_name = os.path.basename(file.filename or 'video')
    if os.path.splitext(raw_name)[1].lower() not in VIDEO_EXTS:
        return {"success": False,
                "message": f"Unsupported video type. Allowed: {', '.join(sorted(VIDEO_EXTS))}"}
    dest, filename = _unique_dest_path(videos_dir, raw_name)
    # Stream the (potentially multi-GB) upload to disk off the event loop so a
    # large video upload can't stall every live output's WebSocket, and cap the
    # size so a runaway/malicious upload can't fill the disk.
    try:
        await asyncio.to_thread(_stream_upload_capped, file, dest, MAX_VIDEO_BYTES)
    except _UploadTooLarge:
        return {"success": False,
                "message": f"Video exceeds the {MAX_VIDEO_BYTES // (1024*1024*1024)} GB limit"}
    return {"success": True, "filename": filename}

def _existing_video_filenames() -> set:
    """{filename} set of every video on disk. Blocking (directory scan on a possibly
    slow disk) — call via asyncio.to_thread from endpoint handlers."""
    videos_dir = os.path.join(APP_STATE.export_dir, 'videos')
    os.makedirs(videos_dir, exist_ok=True)
    return {n for n in os.listdir(videos_dir)
            if os.path.splitext(n)[1].lower() in VIDEO_EXTS}

@app.get("/api/videos/list")
async def api_video_list():
    return {"videos": sorted(await asyncio.to_thread(_existing_video_filenames))}

@app.post("/api/videos/delete")
async def api_video_delete(data: dict = Body(...)):
    filename = os.path.basename(data.get('filename', ''))
    if not filename:
        return {"success": False, "message": "Filename required"}
    # Refuse to delete a video any service still references — otherwise the file
    # vanishes from disk and that service item silently plays nothing. Off the
    # loop: the count scans (and JSON-parses) every video service item.
    refs = await asyncio.to_thread(APP_STATE.db.video_reference_count, filename)
    if refs > 0:
        return {"success": False,
                "message": f"Video is used by {refs} service item(s); remove it from those services first."}
    path = os.path.join(APP_STATE.export_dir, 'videos', filename)

    def _unlink_and_rescan():
        if os.path.exists(path):
            os.unlink(path)
        return _existing_video_filenames()

    existing = await asyncio.to_thread(_unlink_and_rescan)
    # Drop any library-folder links to the now-gone file so the tree can't show a dead row.
    await _db_run(APP_STATE.db.prune_missing_video_folder_items, existing)
    return {"success": True}

@app.post("/api/videos/delete-many")
async def api_video_delete_many(data: dict = Body(...)):
    """Delete several videos in one request (one disk rescan + one prune), replacing
    the client-side per-video delete loop. Same rule as the single delete: a video
    any service still references is refused (reported back, others still deleted)."""
    filenames = [os.path.basename(f or '') for f in (data.get('filenames') or [])]
    filenames = [f for f in filenames if f]
    if not filenames:
        return {"success": False, "message": "filenames required"}
    # One scan for every filename (not one scan each), off the event loop so a
    # bulk delete against a large service list can't stall the WebSockets.
    refs = await asyncio.to_thread(APP_STATE.db.video_reference_counts, filenames)
    refused = [fn for fn in filenames if refs.get(fn, 0) > 0]
    refused_set = set(refused)
    deletable = [fn for fn in filenames if fn not in refused_set]

    def _unlink_and_rescan():
        for fn in deletable:
            path = os.path.join(APP_STATE.export_dir, 'videos', fn)
            if os.path.exists(path):
                os.unlink(path)
        return _existing_video_filenames()

    existing = await asyncio.to_thread(_unlink_and_rescan)
    await _db_run(APP_STATE.db.prune_missing_video_folder_items, existing)
    return {"success": True, "deleted": len(deletable), "refused": refused}

# --- Video library folders (organize/reorder videos by drag, like the image library) ---

@app.get("/api/video-folders/list")
async def api_video_folders_list():
    # Prune links to videos deleted outside the app so the tree stays consistent.
    await _db_run(APP_STATE.db.prune_missing_video_folder_items, await asyncio.to_thread(_existing_video_filenames))
    return {"folders": await _db_run(APP_STATE.db.get_video_folders)}

@app.post("/api/video-folders/create")
async def api_video_folder_create(data: dict = Body(...)):
    name = (data.get('name') or 'New Folder').strip()
    if not name:
        return {"success": False, "message": "Name required"}
    folder_id = await _db_run(APP_STATE.db.create_video_folder, name, data.get('parent_id'))
    return {"success": True, "id": folder_id}

@app.post("/api/video-folders/rename")
async def api_video_folder_rename(data: dict = Body(...)):
    folder_id = data.get('id')
    name = (data.get('name') or '').strip()
    if not folder_id or not name:
        return {"success": False, "message": "ID and name required"}
    await _db_run(APP_STATE.db.rename_video_folder, folder_id, name)
    return {"success": True}

@app.post("/api/video-folders/move")
async def api_video_folder_move(data: dict = Body(...)):
    """Re-parent and/or reorder a video folder. parent_id None = top level;
    ordered_ids is the destination parent's child folders in desired order."""
    folder_id = data.get('id')
    if not folder_id:
        return {"success": False, "message": "ID required"}
    ok = await _db_run(APP_STATE.db.move_video_folder, folder_id, data.get('parent_id'), data.get('ordered_ids') or [])
    return {"success": ok}

@app.post("/api/video-folders/delete")
async def api_video_folder_delete(data: dict = Body(...)):
    """Delete a video folder (and nested subfolders). Video files are kept on disk —
    the folder's videos simply become loose again."""
    folder_id = data.get('id')
    if not folder_id:
        return {"success": False, "message": "ID required"}
    await _db_run(APP_STATE.db.delete_video_folder, folder_id)
    return {"success": True}

@app.post("/api/video-folders/reorder")
async def api_video_folder_reorder(data: dict = Body(...)):
    await _db_run(APP_STATE.db.reorder_video_folders, data.get('ordered_ids', []))
    return {"success": True}

@app.post("/api/video-folders/add-video")
async def api_video_folder_add_video(data: dict = Body(...)):
    folder_id = data.get('folder_id')
    filename = data.get('filename', '')
    if not folder_id or not filename:
        return {"success": False, "message": "folder_id and filename required"}
    item_id = await _db_run(APP_STATE.db.add_video_to_folder, folder_id, filename)
    return {"success": True, "id": item_id}

@app.post("/api/video-folders/remove-video")
async def api_video_folder_remove_video(data: dict = Body(...)):
    item_id = data.get('id')
    if not item_id:
        return {"success": False, "message": "ID required"}
    await _db_run(APP_STATE.db.remove_video_from_folder, item_id)
    return {"success": True}

@app.post("/api/video-folders/reorder-videos")
async def api_video_folder_reorder_videos(data: dict = Body(...)):
    folder_id = data.get('folder_id')
    if not folder_id:
        return {"success": False, "message": "folder_id required"}
    await _db_run(APP_STATE.db.reorder_video_folder_items, folder_id, data.get('ordered_ids', []))
    return {"success": True}

def _move_folder_items_request(kind: str, data: dict):
    """Shared body of the image/video library multi-select move endpoints. `items`
    is an ordered list of {'id': row_id} or {'filename': name}; to_folder_id None
    makes the entries loose; to_index None appends. One transaction server-side —
    replaces the client's per-item remove/add/refetch/reorder request chain."""
    selections = data.get('items') or []
    if not isinstance(selections, list) or not all(isinstance(s, dict) for s in selections):
        return {"success": False, "message": "items must be a list of objects"}
    if not selections:
        return {"success": False, "message": "items required"}
    ok = APP_STATE.db.move_folder_items(kind, selections, data.get('to_folder_id'),
                                        _coerce_int(data.get('to_index'), None))
    return {"success": ok}

@app.post("/api/video-folders/move-items")
async def api_video_folder_move_items(data: dict = Body(...)):
    return _move_folder_items_request('video', data)

@app.post("/api/live/video-control")
async def api_video_control(data: dict = Body(...)):
    action = data.get('action')  # 'play' | 'pause' | 'restart' | 'seek'
    # Coerce once, up front: a malformed position becomes None so the seek below
    # is skipped and the broadcast omits it (clients ignore a positionless seek).
    # Non-finite values are rejected too — float('nan') parses, and a NaN stored
    # in the video timing would make json.dumps emit invalid JSON (a bare NaN
    # token) in every later state packet, breaking JSON.parse on every client.
    try:
        position = float(data.get('position'))
        if not math.isfinite(position):
            position = None
    except (TypeError, ValueError):
        position = None
    now = time.time()

    if action == 'play' and not APP_STATE.video_is_playing:
        APP_STATE.video_start_position = APP_STATE.video_pause_position
        APP_STATE.video_start_time = now
        APP_STATE.video_is_playing = True
    elif action == 'pause' and APP_STATE.video_is_playing:
        APP_STATE.video_pause_position = APP_STATE._get_video_position()
        APP_STATE.video_is_playing = False
    elif action == 'restart':
        APP_STATE._reset_video_timing(autoplay=True)
    elif action == 'seek' and position is not None:
        if APP_STATE.video_is_playing:
            APP_STATE.video_start_position = position
            APP_STATE.video_start_time = now
        else:
            APP_STATE.video_pause_position = position

    # Broadcast the coerced value, so clients receive the same (validated)
    # position the server just recorded.
    await manager.broadcast_video_command(action, position)
    return {"success": True}

# --- Images ---

def _save_uploaded_image(file: UploadFile) -> tuple:
    """Save an uploaded image under a random on-disk filename and record the
    original (display) name in the image_files table. Returns (filename, display_name).
    Random naming avoids name collisions entirely, so duplicate display names
    (e.g. two 'slide.jpg' files) coexist on disk.

    Raises _UploadBadType / _UploadTooLarge before any DB registration so a
    rejected upload never appears in the library."""
    images_dir = os.path.join(APP_STATE.export_dir, 'images')
    os.makedirs(images_dir, exist_ok=True)
    display_name = os.path.basename(file.filename or 'image')
    ext = os.path.splitext(display_name)[1].lower()
    if ext not in IMAGE_EXTS:
        raise _UploadBadType()
    filename = uuid.uuid4().hex + ext
    dest = os.path.join(images_dir, filename)
    _stream_upload_capped(file, dest, MAX_IMAGE_BYTES)
    APP_STATE.db.register_image_file(filename, display_name)
    return filename, display_name

def _image_upload_error_response(exc: BaseException):
    """Map upload exceptions to the standard {success:false} JSON body."""
    if isinstance(exc, _UploadBadType):
        return {"success": False,
                "message": f"Unsupported image type. Allowed: {', '.join(sorted(IMAGE_EXTS))}"}
    if isinstance(exc, _UploadTooLarge):
        return {"success": False,
                "message": f"Image exceeds the {MAX_IMAGE_BYTES // (1024*1024)} MB limit"}
    raise exc

@app.post("/api/images/upload")
async def api_image_upload(file: UploadFile = File(...)):
    try:
        filename, display_name = await asyncio.to_thread(_save_uploaded_image, file)
    except (_UploadBadType, _UploadTooLarge) as e:
        return _image_upload_error_response(e)
    manager.invalidate_library_cache()
    return {"success": True, "filename": filename, "display_name": display_name}

@app.post("/api/images/upload-to-folder")
async def api_image_upload_to_folder(folder_id: int = Form(...), file: UploadFile = File(...)):
    try:
        filename, display_name = await asyncio.to_thread(_save_uploaded_image, file)
    except (_UploadBadType, _UploadTooLarge) as e:
        return _image_upload_error_response(e)
    await _db_run(APP_STATE.db.add_image_to_folder, folder_id, filename)
    manager.invalidate_library_cache()
    return {"success": True, "filename": filename, "display_name": display_name}

def _existing_image_filenames() -> list:
    """Sorted image filenames on disk. Blocking (directory scan on a possibly slow
    disk) — call via asyncio.to_thread from endpoint handlers."""
    images_dir = os.path.join(APP_STATE.export_dir, 'images')
    os.makedirs(images_dir, exist_ok=True)
    return [n for n in sorted(os.listdir(images_dir))
            if os.path.splitext(n)[1].lower() in IMAGE_EXTS]

@app.get("/api/images/list")
async def api_image_list():
    on_disk = await asyncio.to_thread(_existing_image_filenames)

    def _list():
        with APP_STATE.db._db_transaction(commit=False) as cur:
            cur.execute("SELECT filename FROM image_files WHERE library_visible = 0")
            hidden = {r['filename'] for r in cur.fetchall()}
        files = [n for n in on_disk if n not in hidden]
        dn = APP_STATE.db.get_image_display_names()
        return [{"filename": n, "display_name": dn.get(n, n)} for n in files]

    return {"images": await _db_run(_list)}

@app.post("/api/images/delete")
async def api_image_delete(data: dict = Body(...)):
    """Delete an image from the LIBRARY. The file stays on disk if any service still
    references it (so the service keeps working); only orphan files are unlinked."""
    filename = os.path.basename(data.get('filename', ''))
    if not filename:
        return {"success": False, "message": "Filename required"}
    images_dir = os.path.join(APP_STATE.export_dir, 'images')
    unlinked, hidden = await _db_run(APP_STATE.db.delete_library_images, [filename], images_dir)
    manager.invalidate_library_cache()
    await manager.broadcast_state()
    return {"success": True, "unlinked": unlinked, "kept_for_services": hidden}

@app.post("/api/images/delete-many")
async def api_images_delete_many(data: dict = Body(...)):
    """Bulk library delete with the same reference-preserving behavior as the single delete."""
    filenames = [os.path.basename(n) for n in (data.get('filenames') or []) if n]
    if not filenames:
        return {"success": False, "message": "filenames required"}
    images_dir = os.path.join(APP_STATE.export_dir, 'images')
    unlinked, hidden = await _db_run(APP_STATE.db.delete_library_images, filenames, images_dir)
    manager.invalidate_library_cache()
    await manager.broadcast_state()
    return {"success": True, "unlinked": unlinked, "kept_for_services": hidden}

# --- Legacy shared background pool ---
# Background-theme model v2 kept a shared image pool in web_export/backgrounds/
# (served at /static/backgrounds/). Model v3 made every theme own its images
# outright (see the section below), so the pool's list/upload/delete API is gone.
# The prefix stays: _sync_theme_slot still recognizes a legacy /static/backgrounds/
# URL on a theme and adopts the file into the theme's own storage, and files left
# in the pool remain served so an un-migrated theme keeps rendering.
BACKGROUND_URL_PREFIX = "/static/backgrounds/"


# ---------------------- Theme-owned background images ----------------------
# Background-theme model v3: each background theme OWNS its image files outright,
# 1:1 — uploaded directly onto the theme, copied when the theme is duplicated, and
# deleted with the image slot / theme / output. Files live in
# web_export/theme_backgrounds/ (served at /static/theme_backgrounds/ via the same
# static mount) named "<theme_id>.<slot>.<token><ext>", so ownership is derivable
# from the filename alone and a fresh token per upload naturally cache-busts
# clients that had the previous image preloaded.
#
# The legacy shared pool (web_export/backgrounds/, above) is retained read-only for
# rollback: any theme style still referencing a pool URL is ADOPTED — the file is
# copied into the theme's owned slot and the style rewritten — by
# _sync_theme_owned_images, which every theme mutation path and the startup
# migration run. The pool file itself is never touched by adoption.

THEME_BG_URL_PREFIX = "/static/theme_backgrounds/"
# slot -> (style key holding the image URL, style key holding the background type)
_THEME_BG_SLOTS = {
    'bg':    ('background_image', 'background_type'),
    'title': ('title_background_image', 'title_background_type'),
}

def _theme_bg_dir() -> str:
    d = os.path.join(APP_STATE.export_dir, 'theme_backgrounds')
    os.makedirs(d, exist_ok=True)
    return d

def _theme_bg_key(theme_id) -> str:
    """Filename-safe key for a theme id. Theme ids are uuid4 hex in practice; any
    id that isn't already safe (hand-edited config) falls back to a stable hash so
    ownership stays deterministic without ever writing a hostile filename."""
    tid = str(theme_id or '')
    if tid and re.fullmatch(r'[A-Za-z0-9_-]+', tid):
        return tid
    return hashlib.sha1(tid.encode('utf-8')).hexdigest()

def _theme_bg_url(filename: str) -> str:
    return THEME_BG_URL_PREFIX + filename

def _theme_owned_files(theme_id, slot=None) -> list:
    """Filenames in the owned store belonging to a theme (optionally one slot)."""
    key = _theme_bg_key(theme_id)
    prefixes = ([f"{key}.{slot}."] if slot
                else [f"{key}.{s}." for s in _THEME_BG_SLOTS])
    d = _theme_bg_dir()
    return [fn for fn in os.listdir(d)
            if any(fn.startswith(p) for p in prefixes)
            and os.path.isfile(os.path.join(d, fn))]

def _delete_theme_owned_files(theme_id, slot=None):
    """Remove a theme's owned image files (all slots, or one). Called when the
    image slot is cleared, the theme is deleted, or its output is deleted."""
    d = _theme_bg_dir()
    for fn in _theme_owned_files(theme_id, slot):
        try:
            os.unlink(os.path.join(d, fn))
        except OSError:
            logger.warning("Could not remove owned background %s", fn, exc_info=True)

def _static_url_export_path(url: str):
    """Resolve a /static/... URL to its file path under export_dir, or None for
    non-static URLs or anything escaping the export dir."""
    if not (isinstance(url, str) and url.startswith('/static/')):
        return None
    p = os.path.normpath(os.path.join(APP_STATE.export_dir, url[len('/static/'):].lstrip('/')))
    return p if _path_is_within(p, APP_STATE.export_dir) else None

def _adopt_theme_bg_image(theme_id, slot: str, src_path: str):
    """Copy an existing image file into a theme's owned slot and return the new
    owned URL (None if the source is unreadable). The source file is left in place
    — pool files stay for rollback, and a source owned by another theme still
    belongs to that theme."""
    ext = os.path.splitext(src_path)[1].lower()
    filename = f"{_theme_bg_key(theme_id)}.{slot}.{uuid.uuid4().hex[:8]}{ext}"
    try:
        shutil.copy2(src_path, os.path.join(_theme_bg_dir(), filename))
    except OSError:
        logger.warning("Could not adopt background %s for theme %s", src_path, theme_id,
                       exc_info=True)
        return None
    return _theme_bg_url(filename)

def _sync_theme_owned_images(theme: dict) -> bool:
    """Enforce the ownership invariant on one background theme: each image slot
    either points at this theme's own file under /static/theme_backgrounds/ or at
    an external URL the app doesn't manage. Returns True when the style was
    rewritten (caller decides whether to persist).

    Per slot:
      - a legacy pool URL, or an owned URL belonging to a DIFFERENT theme/slot
        (the duplicate-theme path), is adopted — copied into this theme's slot and
        the style rewritten; a missing source file leaves the URL untouched
        (already broken; don't destroy the evidence);
      - owned files the slot no longer references are deleted, so replacing or
        clearing an image never strands a file.
    """
    if not isinstance(theme, dict) or not theme.get('id'):
        return False
    tid = theme.get('id')
    style = theme.get('style')
    if not isinstance(style, dict):
        return False
    changed = False
    for slot, (img_key, _type_key) in _THEME_BG_SLOTS.items():
        # Call for every slot (each does its own file cleanup) — don't short-circuit.
        if _sync_theme_slot(tid, style, slot, img_key):
            changed = True
    return changed

def _sync_theme_slot(tid, style, slot, img_key) -> bool:
    """Enforce the ownership invariant on one background-theme image slot (see
    _sync_theme_owned_images): adopt a legacy pool URL or a foreign-owned URL into this
    theme's own slot, then delete owned files the slot no longer references. Returns True
    when the style was rewritten."""
    url = style.get(img_key) or ''
    if not isinstance(url, str):   # corrupted/hand-edited config must not crash boot
        url = ''
    own_prefix = THEME_BG_URL_PREFIX + f"{_theme_bg_key(tid)}.{slot}."
    needs_adopt = (
        url.startswith(BACKGROUND_URL_PREFIX)
        or (url.startswith(THEME_BG_URL_PREFIX) and not url.startswith(own_prefix))
    )
    changed = False
    if needs_adopt:
        src = _static_url_export_path(url)
        if src and os.path.isfile(src):
            new_url = _adopt_theme_bg_image(tid, slot, src)
            if new_url:
                style[img_key] = url = new_url
                changed = True
    keep = os.path.basename(url) if url.startswith(own_prefix) else None
    d = _theme_bg_dir()
    for fn in _theme_owned_files(tid, slot):
        if fn != keep:
            try:
                os.unlink(os.path.join(d, fn))
            except OSError:
                logger.warning("Could not remove stale owned background %s", fn,
                               exc_info=True)
    return changed

def _find_bg_theme(oc, theme_id):
    """A background theme dict on an output by id, or None."""
    for t in (getattr(oc, 'bg_themes', None) or []):
        if isinstance(t, dict) and t.get('id') == theme_id:
            return t
    return None

@app.post("/api/output/theme/background/upload")
async def api_theme_background_upload(output_index: int = Form(...),
                                      theme_id: str = Form(...),
                                      slot: str = Form('bg'),
                                      file: UploadFile = File(...)):
    """Upload an image directly onto a background theme's slot ('bg' or 'title').
    The file becomes owned by the theme (replacing any previous owned file for the
    slot) and the slot's background type is switched to 'image' so the upload is
    immediately visible."""
    if not (0 <= output_index < len(APP_STATE.outputs)):
        return {"success": False, "message": "Invalid output index"}
    if slot not in _THEME_BG_SLOTS:
        return {"success": False, "message": "Invalid slot (use 'bg' or 'title')"}
    theme = _find_bg_theme(APP_STATE.outputs[output_index], theme_id)
    if not theme:
        return {"success": False, "message": "Background theme not found"}
    ext = os.path.splitext(os.path.basename(file.filename or ''))[1].lower()
    if ext not in IMAGE_EXTS:
        return {"success": False,
                "message": f"Unsupported image type. Allowed: {', '.join(sorted(IMAGE_EXTS))}"}
    filename = f"{_theme_bg_key(theme_id)}.{slot}.{uuid.uuid4().hex[:8]}{ext}"
    dest = os.path.join(_theme_bg_dir(), filename)
    try:
        await asyncio.to_thread(_stream_upload_capped, file, dest, MAX_BACKGROUND_BYTES)
    except _UploadTooLarge:
        return {"success": False,
                "message": f"Image exceeds the {MAX_BACKGROUND_BYTES // (1024*1024)} MB limit"}
    img_key, type_key = _THEME_BG_SLOTS[slot]
    style = theme.setdefault('style', {})
    style[img_key] = _theme_bg_url(filename)
    style[type_key] = 'image'
    _sync_theme_owned_images(theme)   # drops the replaced owned file, if any
    await _persist_and_broadcast()
    return {"success": True, "url": style[img_key], "theme_id": theme_id, "slot": slot}

@app.post("/api/output/theme/background/delete")
async def api_theme_background_delete(data: dict = Body(...)):
    """Delete the image owned by a background theme's slot. Clears the slot's URL,
    removes the file, and steps the background type back to its no-image default
    ('transparent' for the main background, 'inherit' for the title override)."""
    out_idx = data.get('output_index')
    slot = data.get('slot', 'bg')
    if not (isinstance(out_idx, int) and 0 <= out_idx < len(APP_STATE.outputs)):
        return {"success": False, "message": "Invalid output index"}
    if slot not in _THEME_BG_SLOTS:
        return {"success": False, "message": "Invalid slot (use 'bg' or 'title')"}
    theme = _find_bg_theme(APP_STATE.outputs[out_idx], data.get('theme_id'))
    if not theme:
        return {"success": False, "message": "Background theme not found"}
    img_key, type_key = _THEME_BG_SLOTS[slot]
    style = theme.setdefault('style', {})
    style[img_key] = ''
    if style.get(type_key) == 'image':
        style[type_key] = 'transparent' if slot == 'bg' else 'inherit'
    _sync_theme_owned_images(theme)   # unlinks the now-unreferenced owned file
    await _persist_and_broadcast()
    return {"success": True}

def _migrate_theme_bg_ownership():
    """Startup pass: enforce the owned-image invariant on the loaded config, then
    sweep orphans.

    Adoption converts legacy shared-pool references (and any cross-theme
    references) into per-theme copies — the pool files are kept for rollback. The
    sweep then removes owned files no theme references (crash leftovers, config
    restored from backup). Skipped entirely when no outputs loaded, so a failed
    config load can never wipe every owned file. Naturally idempotent: adopted
    styles no longer match the pool prefix on the next run."""
    if not APP_STATE.outputs:
        return
    changed, referenced = _collect_theme_bg_references()
    if changed:
        APP_STATE.config_manager.save_config()
        logger.info("Adopted legacy background references into theme-owned storage")
    _sweep_orphan_theme_bgs(referenced)

def _collect_theme_bg_references():
    """Sync every loaded background theme's owned images and gather the set of owned
    filenames still referenced by a slot. Returns (changed, referenced_filenames)."""
    changed = False
    referenced = set()
    for oc in APP_STATE.outputs:
        for theme in (getattr(oc, 'bg_themes', None) or []):
            if not isinstance(theme, dict):
                continue
            changed |= _sync_theme_owned_images(theme)
            style = theme.get('style') or {}
            for _slot, (img_key, _tk) in _THEME_BG_SLOTS.items():
                url = style.get(img_key) or ''
                if isinstance(url, str) and url.startswith(THEME_BG_URL_PREFIX):
                    referenced.add(os.path.basename(url))
    return changed, referenced

def _sweep_orphan_theme_bgs(referenced):
    """Delete owned theme-background files no theme references (crash leftovers, or a config
    restored from backup)."""
    d = _theme_bg_dir()
    for fn in os.listdir(d):
        if fn not in referenced and os.path.isfile(os.path.join(d, fn)):
            try:
                os.unlink(os.path.join(d, fn))
                logger.info("Swept orphaned theme background %s", fn)
            except OSError:
                logger.warning("Could not sweep orphaned theme background %s", fn,
                               exc_info=True)

# Called from init_app(), right after AppState construction.


@app.get("/api/image-folders/list")
async def api_image_folders_list():
    return {"folders": await _db_run(APP_STATE.db.get_image_folders)}

@app.post("/api/image-folders/create")
async def api_image_folder_create(data: dict = Body(...)):
    name = (data.get('name') or 'New Folder').strip()
    if not name:
        return {"success": False, "message": "Name required"}
    parent_id = data.get('parent_id')  # None = top-level folder
    folder_id = await _db_run(APP_STATE.db.create_image_folder, name, parent_id)
    return {"success": True, "id": folder_id}

@app.post("/api/image-folders/move")
async def api_image_folder_move(data: dict = Body(...)):
    """Re-parent and/or reorder a library folder. parent_id None = top level.
    ordered_ids is the destination parent's child folders in their desired order."""
    folder_id = data.get('id')
    if not folder_id:
        return {"success": False, "message": "ID required"}
    parent_id = data.get('parent_id')
    ordered_ids = data.get('ordered_ids') or []
    ok = await _db_run(APP_STATE.db.move_image_folder, folder_id, parent_id, ordered_ids)
    return {"success": ok}

@app.post("/api/image-folders/rename")
async def api_image_folder_rename(data: dict = Body(...)):
    folder_id = data.get('id')
    name = (data.get('name') or '').strip()
    if not folder_id or not name:
        return {"success": False, "message": "ID and name required"}
    await _db_run(APP_STATE.db.rename_image_folder, folder_id, name)
    return {"success": True}

@app.post("/api/image-folders/delete")
async def api_image_folder_delete(data: dict = Body(...)):
    """Delete a library image folder. Files referenced by any service are kept on disk
    (the service keeps working); orphaned files are unlinked."""
    folder_id = data.get('id')
    if not folder_id:
        return {"success": False, "message": "ID required"}
    # delete_image_folder cascades nested subfolders and returns every filename linked
    # across the deleted subtree, so orphan cleanup covers images in subfolders too.
    filenames = await _db_run(APP_STATE.db.delete_image_folder, folder_id)
    unlinked, hidden = 0, 0
    if filenames:
        images_dir = os.path.join(APP_STATE.export_dir, 'images')
        unlinked, hidden = await _db_run(APP_STATE.db.delete_library_images, filenames, images_dir)
    manager.invalidate_library_cache()
    return {"success": True, "unlinked": unlinked, "kept_for_services": hidden}

@app.post("/api/image-folders/add-image")
async def api_image_folder_add_image(data: dict = Body(...)):
    folder_id = data.get('folder_id')
    filename = data.get('filename', '')
    if not folder_id or not filename:
        return {"success": False, "message": "folder_id and filename required"}
    item_id = await _db_run(APP_STATE.db.add_image_to_folder, folder_id, filename)
    return {"success": True, "id": item_id}

@app.post("/api/image-folders/remove-image")
async def api_image_folder_remove_image(data: dict = Body(...)):
    item_id = data.get('id')
    if not item_id:
        return {"success": False, "message": "ID required"}
    await _db_run(APP_STATE.db.remove_image_from_folder, item_id)
    return {"success": True}

@app.post("/api/image-folders/reorder-images")
async def api_image_folder_reorder_images(data: dict = Body(...)):
    folder_id = data.get('folder_id')
    ordered_ids = data.get('ordered_ids', [])
    if not folder_id:
        return {"success": False, "message": "folder_id required"}
    await _db_run(APP_STATE.db.reorder_image_folder_items, folder_id, ordered_ids)
    return {"success": True}

@app.post("/api/image-folders/move-items")
async def api_image_folder_move_items(data: dict = Body(...)):
    return _move_folder_items_request('image', data)

@app.post("/api/select-single-image")
async def api_select_single_image(data: dict = Body(...)):
    filename = data.get('filename')
    if not filename:
        return {"success": False, "message": "filename required"}

    def _apply():
        APP_STATE.current_mode = 'image'
        APP_STATE.current_image_data = {
            'folder_id': None,
            'folder_name': filename,
            'images': [filename],
            'index': 0,
        }
        APP_STATE.current_item_index = -1
        APP_STATE.current_song_id = None
        APP_STATE.is_blank = False
        APP_STATE._clear_outputs_and_player()
        _cancel_pending_video_task()

    await _live_commit(mutate=_apply, broadcast='state')
    return {"success": True}

@app.post("/api/select-image-folder")
async def api_select_image_folder(data: dict = Body(...)):
    folder_id = data.get('folder_id')
    if not folder_id:
        return {"success": False, "message": "folder_id required"}
    folder = await _db_run(APP_STATE.db.get_image_folder, folder_id)
    if not folder:
        return {"success": False, "message": "Folder not found"}
    images = [fi['filename'] for fi in folder.get('images', [])]
    start_index = _coerce_int(data.get('index'), 0)
    folder_name = folder['name']
    index = max(0, min(start_index, len(images) - 1)) if images else 0

    def _apply():
        APP_STATE.current_mode = 'image'
        APP_STATE.current_image_data = {
            'folder_id': folder_id,
            'folder_name': folder_name,
            'images': images,
            'index': index,
        }
        APP_STATE.current_item_index = -1
        APP_STATE.current_song_id = None
        APP_STATE.is_blank = False
        APP_STATE._clear_outputs_and_player()
        _cancel_pending_video_task()

    await _live_commit(mutate=_apply, broadcast='state')
    return {"success": True}

@app.post("/api/live/image-goto")
async def api_image_goto(data: dict = Body(...)):
    def _step():
        img_data = APP_STATE.current_image_data
        images = img_data.get('images', [])
        if not images:
            return False
        idx = max(0, min(_coerce_int(data.get('index'), 0), len(images) - 1))
        APP_STATE.current_image_data['index'] = idx
        return True

    ok = await _live_commit(prepare=_step, broadcast='state')
    if not ok:
        return {"success": False}
    return {"success": True, "index": APP_STATE.current_image_data.get('index', 0)}

@app.post("/api/services/add-image-folder")
async def api_add_image_folder_to_service(data: dict = Body(...)):
    service_id = APP_STATE.current_service_id
    if service_id == -1:
        raise HTTPException(status_code=400, detail="No service selected")
    folder_id = data.get('folder_id')
    folder_name = data.get('folder_name', '')
    if not folder_id:
        return {"success": False, "message": "folder_id required"}
    prev_item_id = APP_STATE.active_item_id()
    await _db_run(APP_STATE.db.add_image_folder_to_service, service_id, folder_id, folder_name, _coerce_at_index(data))
    await _refresh_current_service_items(prev_item_id)
    manager.invalidate_library_cache()
    await _live_commit(broadcast='library')
    return {"success": True}

@app.post("/api/services/create-image-folder")
async def api_create_image_folder_in_service(data: dict = Body(...)):
    """Create an empty image folder inside the current service (not linked to the library)."""
    service_id = APP_STATE.current_service_id
    if service_id == -1:
        raise HTTPException(status_code=400, detail="No service selected")
    folder_name = (data.get('folder_name') or '').strip() or 'New Folder'
    prev_item_id = APP_STATE.active_item_id()
    item_id = await _db_run(APP_STATE.db.create_service_image_folder, service_id, folder_name)
    await _refresh_current_service_items(prev_item_id)
    await _live_commit(rebuild=True, broadcast='state')
    return {"success": True, "item_id": item_id}

@app.post("/api/services/merge-image-into-folder")
async def api_merge_image_into_folder(data: dict = Body(...)):
    """Merge a standalone single-image service item into a service image_folder item."""
    from_item_id = data.get('from_item_id')
    to_item_id = data.get('to_item_id')
    to_index = data.get('to_index')
    if not from_item_id or not to_item_id:
        return {"success": False, "message": "from_item_id and to_item_id required"}
    prev_item_id = APP_STATE.active_item_id()
    ok = await _db_run(APP_STATE.db.merge_image_into_service_folder, from_item_id, to_item_id, to_index)
    if not ok:
        return {"success": False, "message": "Merge failed"}
    # Merging deletes the standalone source image item; if it was live the display is
    # cleared, otherwise the (surviving) active item is rebuilt to reflect the change.
    active_lost = await _refresh_current_service_items(prev_item_id)
    if not active_lost:
        await _live_commit(rebuild=True, broadcast='state')
    return {"success": True}

@app.post("/api/services/folder-remove-image")
async def api_folder_remove_image(data: dict = Body(...)):
    """Remove a single image from a service image_folder item's snapshot (service-scoped)."""
    item_id = data.get('item_id')
    index = data.get('index')
    if not item_id or index is None:
        return {"success": False, "message": "item_id and index required"}
    prev_item_id = APP_STATE.active_item_id()
    # Coerce with -1 as the fallback: the DB method bounds-rejects it, so malformed
    # input is a refused no-op instead of an unhandled 500.
    ok = await _db_run(APP_STATE.db.remove_filename_from_service_folder, item_id, _coerce_int(index, -1))
    if not ok:
        return {"success": False, "message": "Remove failed"}
    await asyncio.to_thread(
        APP_STATE.db.cleanup_orphan_hidden_images, os.path.join(APP_STATE.export_dir, 'images'))
    active_lost = await _refresh_current_service_items(prev_item_id)
    if not active_lost:
        await _live_commit(rebuild=True, broadcast='state')
    return {"success": True}

@app.post("/api/services/folder-remove-images")
async def api_folder_remove_images(data: dict = Body(...)):
    """Bulk remove from service folder snapshots. `removals` = [{item_id, index}, ...]."""
    removals = data.get('removals') or []
    if not removals:
        return {"success": False, "message": "removals required"}
    prev_item_id = APP_STATE.active_item_id()
    deleted = await _db_run(APP_STATE.db.remove_filenames_from_service_folders, removals)
    await asyncio.to_thread(
        APP_STATE.db.cleanup_orphan_hidden_images, os.path.join(APP_STATE.export_dir, 'images'))
    active_lost = await _refresh_current_service_items(prev_item_id)
    if not active_lost:
        await _live_commit(rebuild=True, broadcast='state')
    return {"success": True, "deleted": deleted}

@app.post("/api/services/add-image-files")
async def api_service_add_image_files(data: dict = Body(...)):
    """Bulk add library image filenames as standalone single-image service items."""
    sid = APP_STATE.current_service_id
    if sid == -1:
        return {"success": False, "message": "No service selected"}
    filenames = [n for n in (data.get('filenames') or []) if n]
    if not filenames:
        return {"success": False, "message": "filenames required"}
    prev_item_id = APP_STATE.active_item_id()
    added = await _db_run(APP_STATE.db.add_images_to_service, sid, filenames, _coerce_at_index(data))
    await _refresh_current_service_items(prev_item_id)
    manager.invalidate_library_cache()
    await _live_commit(broadcast='library')
    return {"success": True, "added": added}

@app.post("/api/services/folder-add-images")
async def api_folder_add_images(data: dict = Body(...)):
    """Add image filenames into a service image_folder item (service-scoped)."""
    item_id = data.get('item_id')
    filenames = data.get('filenames') or []
    to_index = data.get('to_index')
    if not item_id or not filenames:
        return {"success": False, "message": "item_id and filenames required"}
    prev_item_id = APP_STATE.active_item_id()
    ok = await _db_run(APP_STATE.db.add_filenames_to_service_folder, item_id, filenames, to_index)
    if not ok:
        return {"success": False, "message": "Add failed"}
    active_lost = await _refresh_current_service_items(prev_item_id)
    if not active_lost:
        await _live_commit(rebuild=True, broadcast='state')
    return {"success": True}

@app.post("/api/services/add-image")
async def api_add_image_to_service(data: dict = Body(...)):
    service_id = APP_STATE.current_service_id
    if service_id == -1:
        raise HTTPException(status_code=400, detail="No service selected")
    filename = data.get('filename', '')
    if not filename:
        return {"success": False, "message": "filename required"}
    prev_item_id = APP_STATE.active_item_id()
    await _db_run(APP_STATE.db.add_image_to_service, service_id, filename, _coerce_at_index(data))
    await _refresh_current_service_items(prev_item_id)
    manager.invalidate_library_cache()
    await _live_commit(broadcast='library')
    return {"success": True}

@app.post("/api/services/add-divider")
async def api_add_divider_to_service(data: dict = Body(...)):
    service_id = APP_STATE.current_service_id
    if service_id == -1:
        raise HTTPException(status_code=400, detail="No service selected")
    title = data.get('title', 'Section')
    prev_item_id = APP_STATE.active_item_id()
    await _db_run(APP_STATE.db.add_divider_to_service, service_id, title, _coerce_at_index(data))
    await _refresh_current_service_items(prev_item_id)
    manager.invalidate_library_cache()
    await _live_commit(broadcast='library')
    return {"success": True}

def _import_song_bytes(content: bytes) -> int:
    """Write uploaded XML to a temp file, parse it, and insert the songs. Runs the
    blocking parse + DB inserts in a worker thread (called via asyncio.to_thread)
    so a large song file can't stall the event loop. Returns the number added."""
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode='wb', suffix='.xml', delete=False) as f:
            f.write(content)
            temp_path = f.name
        songs = parse_song_file(temp_path)
        for s in songs:
            APP_STATE.db.add_song(s.title, s.lyrics, s.verse_order, s.authors,
                                  s.songbook_name, s.songbook_entry, key=s.key)
        return len(songs)
    finally:
        if temp_path:
            _safe_unlink(temp_path)

@app.post("/api/upload")
async def api_upload(files: List[UploadFile] = File(...)):
    total_added = 0
    errors = []

    for file in files:
        try:
            content = await file.read()
            total_added += await asyncio.to_thread(_import_song_bytes, content)
        except Exception as e:
            errors.append(f"{file.filename}: {str(e)}")
        # Temp-file cleanup happens inside _import_song_bytes (it owns the temp path);
        # there is nothing to clean up here.

    APP_STATE.config_manager.save_config()
    manager.invalidate_library_cache()
    await _live_commit(broadcast='library')

    if errors:
        return {'success': True, 'count': total_added, 'errors': errors}
    else:
        return {'success': True, 'count': total_added}


# --- Outputs ---

def _seed_default_themes(oc: 'OutputConfig'):
    """Ensure an output has at least one text theme + one bg theme and per-category
    defaults, derived from its dataclass field values."""
    if not getattr(oc, 'text_themes', None):
        tid = uuid.uuid4().hex
        oc.text_themes = [{'id': tid, 'name': 'Base',
                           'style': {k: getattr(oc, k) for k in TEXT_THEME_KEYS}}]
    else:
        tid = oc.text_themes[0]['id']
    if not getattr(oc, 'bg_themes', None):
        bid = uuid.uuid4().hex
        oc.bg_themes = [{'id': bid, 'name': 'Base',
                         'style': {k: getattr(oc, k) for k in BG_THEME_KEYS}}]
    else:
        bid = oc.bg_themes[0]['id']
    if not getattr(oc, 'category_defaults', None):
        oc.category_defaults = {
            'song': {'text': tid, 'bg': bid},
            'bible': {'text': tid, 'bg': bid},
            'announcement': {'bg': bid},
        }


def _output_theme_list(oc: 'OutputConfig', kind: str) -> list:
    """Return the live text_themes or bg_themes list for an output, creating it if absent."""
    attr = 'text_themes' if kind == 'text' else 'bg_themes'
    lst = getattr(oc, attr, None)
    if not isinstance(lst, list):
        lst = []
        setattr(oc, attr, lst)
    return lst


def _resolve_output_and_kind(data: dict):
    """Validate the output_index and theme kind shared by the theme endpoints.
    Returns (oc, kind, None) on success or (None, None, error_dict) on failure."""
    out_idx = data.get('output_index')
    kind = data.get('kind', 'text')
    if not (isinstance(out_idx, int) and 0 <= out_idx < len(APP_STATE.outputs)):
        return None, None, {"success": False, "message": "Invalid output index"}
    if kind not in ('text', 'bg'):
        return None, None, {"success": False, "message": "Invalid theme kind"}
    return APP_STATE.outputs[out_idx], kind, None


async def _persist_and_broadcast():
    """Common tail of the theme-mutation endpoints: save config, re-export when font
    bundling is on, rebuild slides off the event loop, then broadcast."""
    APP_STATE.config_manager.save_config()
    if APP_STATE.bundle_local_fonts:
        await _export_outputs()
    await _live_commit(rebuild=True, broadcast='state')


def _normalize_theme_tags(raw) -> list:
    """Coerce a request's theme tags to a clean list: strings, trimmed, empties
    dropped, deduped case-insensitively (first casing wins), capped in length and
    count. Accepts a list or a comma-separated string. Tags live on the theme dict
    (like the title-layout ref) — organizational metadata, not style."""
    if isinstance(raw, str):
        raw = raw.split(',')
    if not isinstance(raw, list):
        return []
    out, seen = [], set()
    for t in raw:
        t = str(t).strip()[:40]
        key = t.casefold()
        if not t or key in seen:
            continue
        seen.add(key)
        out.append(t)
        if len(out) >= 24:
            break
    return out


def _apply_theme_tags(theme: dict, data: dict):
    """Apply request tags when present; empty list clears the key."""
    if 'tags' not in data:
        return
    tags = _normalize_theme_tags(data.get('tags'))
    if tags:
        theme['tags'] = tags
    else:
        theme.pop('tags', None)


def _theme_title_boxes(theme) -> list:
    """The embedded title-slide boxes of a (resolved) text theme, or [] when the
    theme doesn't drive a title slide."""
    ts = (theme or {}).get('title_slide')
    boxes = ts.get('text_boxes') if isinstance(ts, dict) else None
    return boxes if isinstance(boxes, list) and boxes else []


def _embed_title_slide_from_layout(theme: dict, layout_id, output_name: str) -> bool:
    """Copy an ann_layout's boxes into theme['title_slide'] — a geometry IMPORT;
    the embedded copy is independent of the source layout afterwards. Only layouts
    belonging to the same output are accepted. Returns True if embedded."""
    try:
        lid = int(layout_id)
    except (TypeError, ValueError):
        return False
    layout = APP_STATE.db.get_ann_layout(lid)
    if not layout or layout.get('output_name') != output_name:
        return False
    boxes = layout.get('text_boxes') or []
    if not boxes:
        return False
    theme['title_slide'] = {'text_boxes': copy.deepcopy(boxes)}
    return True


def _apply_title_slide_ref(theme: dict, kind: str, data: dict, output_name: str):
    """Set, keep, or clear a text theme's EMBEDDED title slide from request data.

    Theme model F4: the title-slide layout lives INSIDE the text theme as
    {'text_boxes': [...]} (canvas px), so a song's title slide and its lyrics are
    one self-contained theme. Held on the theme dict (not in `style`) — designed
    composition, not a loose style value.

    Two request forms, acted on only when the key is present (unrelated theme
    edits leave the title slide alone):
      - 'title_slide': the boxes themselves ({'text_boxes': [...]}, or null to
        clear) — the direct form the unified editor and theme duplication use;
      - 'title_layout_id': import form — a layout id on the same output embeds a
        COPY of that layout's boxes; the '__current__' sentinel keeps the embedded
        slide as-is (the interim theme-editor dropdown round-trips this);
        null/'' clears. An id that no longer resolves leaves the theme unchanged.
    Superseded pointer keys (title_layout_id / title_template_id) are dropped."""
    if kind != 'text' or ('title_slide' not in data and 'title_layout_id' not in data):
        return
    if 'title_slide' in data:
        ts = data.get('title_slide')
        boxes = ts.get('text_boxes') if isinstance(ts, dict) else None
        if isinstance(boxes, list) and boxes:
            theme['title_slide'] = {'text_boxes': _normalize_ann_boxes(boxes, [])}
        else:
            theme.pop('title_slide', None)
    else:
        lid = data.get('title_layout_id')
        if lid == '__current__':
            pass                          # keep the embedded slide untouched
        elif not lid:
            theme.pop('title_slide', None)
        else:
            _embed_title_slide_from_layout(theme, lid, output_name)
    theme.pop('title_layout_id', None)
    theme.pop('title_template_id', None)


def _migrate_title_slides_embedded():
    """Startup pass (theme model F4): embed legacy title-slide references into
    their text themes. A theme carrying title_layout_id (or the even older
    title_template_id, normally already converted by the DB-side
    _migrate_title_bindings) gets a deep copy of the referenced layout's boxes as
    theme['title_slide']; the pointer keys are dropped. The source ann_layouts row
    is untouched — it remains an ordinary announcement layout. Naturally
    idempotent: converted themes carry no pointer. A pointer to a deleted layout
    (which already resolved to nothing at build time) converts to no title slide."""
    changed = False
    for oc in APP_STATE.outputs:
        for theme in (getattr(oc, 'text_themes', None) or []):
            if not isinstance(theme, dict):
                continue
            if 'title_layout_id' not in theme and 'title_template_id' not in theme:
                continue
            lid = theme.get('title_layout_id')
            if lid and 'title_slide' not in theme:
                _embed_title_slide_from_layout(theme, lid, oc.name)
            theme.pop('title_layout_id', None)
            theme.pop('title_template_id', None)
            changed = True
    if changed:
        APP_STATE.config_manager.save_config()
        logger.info("Embedded legacy title-slide layout references into text themes")


# Called from init_app(): AppState construction runs the DB-side migrations
# (units, title bindings) first, so everything this pass reads is converted.


# Output names double as the exported HTML filename ({name}.html), a URL path
# segment, a WebSocket output_name, and the dict key for every per-output packet.
# Strip characters that would let a name escape the export dir (path separators),
# break a filename on Windows (: * ? " < > | and control chars), or inject markup
# into the admin page; then trim and cap the length. Applied on create/edit so only
# safe names are ever stored; falls back to 'Output' when nothing usable remains.
_UNSAFE_OUTPUT_NAME_CHARS = re.compile(r'[\x00-\x1f/\\:*?"<>|]')

def _sanitize_output_name(raw) -> str:
    name = _UNSAFE_OUTPUT_NAME_CHARS.sub('', str(raw or '')).replace('..', '')
    name = name.strip().strip('.').strip()[:64].strip()
    return name or 'Output'


def _unique_output_name(name: str, exclude_index=None) -> str:
    """Disambiguate `name` against the other outputs so every output name stays unique —
    names key the per-output packets, the theme maps and the export filename, so a
    duplicate would collide there. A clash gets the first free ' N' suffix. Compared
    case-insensitively so two names can't map to the same export file on a
    case-insensitive filesystem (Windows/macOS). Pass the index of the output being
    edited as exclude_index so it keeps its own name."""
    taken = {oc.name.lower() for i, oc in enumerate(APP_STATE.outputs) if i != exclude_index}
    if name.lower() not in taken:
        return name
    base = name[:60].strip() or 'Output'   # leave room for the ' N' suffix under the cap
    n = 2
    while f"{base} {n}".lower() in taken:
        n += 1
    return f"{base} {n}"


@app.post("/api/output/add")
async def api_output_add(data: dict = Body(...)):
    oc = OutputConfig.from_dict(data)
    oc.name = _unique_output_name(_sanitize_output_name(oc.name))
    _seed_default_themes(oc)

    def _apply():
        APP_STATE.outputs.append(oc)
        APP_STATE.db.ensure_song_title_layout(
            oc.name,
            float(getattr(oc, 'canvas_width', None) or 1920),
            float(getattr(oc, 'canvas_height', None) or 1080))
        APP_STATE.config_manager.save_config()

    await _live_commit(mutate=_apply, export=True, broadcast='state')
    return {"success": True}

@app.post("/api/output/edit")
async def api_output_edit(data: dict = Body(...)):
    idx = data.get('index')
    if isinstance(idx, int) and 0 <= idx < len(APP_STATE.outputs):
        old = APP_STATE.outputs[idx]
        updated = OutputConfig.from_dict(data)
        updated.name = _unique_output_name(_sanitize_output_name(updated.name), exclude_index=idx)
        # Theme libraries + defaults are managed via /api/output/theme/* only
        updated.text_themes = old.text_themes
        updated.bg_themes = old.bg_themes
        updated.category_defaults = old.category_defaults
        _seed_default_themes(updated)
        updated.slides = []
        updated.line_to_slide = []
        updated.index = 0
        APP_STATE.outputs[idx] = updated
        APP_STATE.config_manager.save_config()
        await _export_outputs()
        # The replacement OutputConfig resets runtime freeze state and may change the
        # global-freeze exemption, so reconcile snapshots before broadcasting.
        await _reconcile_freeze_snapshots()
        await manager.broadcast_state()
    return {"success": True}

@app.post("/api/output/delete")
async def api_output_delete(data: dict = Body(...)):
    idx = data.get('index')
    if isinstance(idx, int) and 0 <= idx < len(APP_STATE.outputs):
        removed = APP_STATE.outputs.pop(idx)
        # Owned background images die with their themes' output.
        for theme in (getattr(removed, 'bg_themes', None) or []):
            if isinstance(theme, dict) and theme.get('id'):
                _delete_theme_owned_files(theme['id'])
        APP_STATE.config_manager.save_config()
        await _live_commit(export=True, broadcast='state')
    return {"success": True}

@app.post("/api/output/order")
async def api_output_order(data: dict = Body(...)):
    """Set the absolute output order from a list of names (drag-and-drop)."""
    names = data.get('names')
    if not isinstance(names, list):
        return {"success": False, "message": "Missing names"}
    changed = APP_STATE.config_manager.set_output_order(names)
    if changed:
        await _live_commit(export=True, broadcast='state')
    return {"success": changed}


@app.post("/api/output/theme/create")
async def api_output_theme_create(data: dict = Body(...)):
    oc, kind, err = _resolve_output_and_kind(data)
    if err:
        return err
    name = (data.get('name') or '').strip() or 'Untitled'
    style_in = data.get('style') or {}
    if not isinstance(style_in, dict):
        style_in = {}

    keys = TEXT_THEME_KEYS if kind == 'text' else BG_THEME_KEYS
    style = {k: v for k, v in style_in.items() if k in keys}
    theme = {'id': uuid.uuid4().hex, 'name': name, 'style': style}
    _apply_title_slide_ref(theme, kind, data, oc.name)
    _apply_theme_tags(theme, data)
    if kind == 'bg':
        # Duplicating a theme arrives here with the source's style: adopt any image
        # it references (another theme's owned file, or a legacy pool URL) as this
        # theme's own copy — images are 1:1 with their theme.
        _sync_theme_owned_images(theme)
    _output_theme_list(oc, kind).append(theme)

    await _persist_and_broadcast()
    return {"success": True, "theme": theme, "kind": kind}


@app.post("/api/output/theme/update")
async def api_output_theme_update(data: dict = Body(...)):
    oc, kind, err = _resolve_output_and_kind(data)
    if err:
        return err
    theme_id = data.get('theme_id')
    name = (data.get('name') or '').strip() or 'Untitled'
    style_in = data.get('style') or {}
    if not isinstance(style_in, dict):
        style_in = {}
    if not theme_id:
        return {"success": False, "message": "Missing theme id"}

    themes = _output_theme_list(oc, kind)
    keys = TEXT_THEME_KEYS if kind == 'text' else BG_THEME_KEYS
    style = {k: v for k, v in style_in.items() if k in keys}
    updated = None
    for t in themes:
        if isinstance(t, dict) and t.get('id') == theme_id:
            t['name'] = name
            t['style'] = style
            _apply_title_slide_ref(t, kind, data, oc.name)
            _apply_theme_tags(t, data)
            updated = t
            break
    if not updated:
        return {"success": False, "message": "Theme not found"}
    if kind == 'bg':
        # Re-enforce image ownership: adopt any foreign/pool URL the new style
        # carries and drop owned files the style no longer references.
        _sync_theme_owned_images(updated)

    await _persist_and_broadcast()
    return {"success": True, "theme": updated, "kind": kind}


@app.post("/api/output/theme/delete")
async def api_output_theme_delete(data: dict = Body(...)):
    oc, kind, err = _resolve_output_and_kind(data)
    if err:
        return err
    theme_id = data.get('theme_id')
    if not theme_id:
        return {"success": False, "message": "Missing theme id"}

    themes = _output_theme_list(oc, kind)
    remaining = [t for t in themes if not (isinstance(t, dict) and t.get('id') == theme_id)]
    if len(remaining) == len(themes):
        return {"success": False, "message": "Theme not found"}
    if not remaining:
        return {"success": False, "message": "Cannot delete the last theme of this type"}
    setattr(oc, 'text_themes' if kind == 'text' else 'bg_themes', remaining)
    if kind == 'bg':
        # The theme's images live and die with it.
        _delete_theme_owned_files(theme_id)

    # Repoint any category defaults that referenced the deleted theme to the first remaining.
    field_key = 'text' if kind == 'text' else 'bg'
    fallback_id = remaining[0]['id']
    for _cat, ent in (oc.category_defaults or {}).items():
        if isinstance(ent, dict) and ent.get(field_key) == theme_id:
            ent[field_key] = fallback_id

    await _persist_and_broadcast()
    return {"success": True}


@app.post("/api/output/theme/defaults")
async def api_output_theme_defaults(data: dict = Body(...)):
    out_idx = data.get('output_index')
    defaults = data.get('category_defaults')
    if not (isinstance(out_idx, int) and 0 <= out_idx < len(APP_STATE.outputs)):
        return {"success": False, "message": "Invalid output index"}
    if not isinstance(defaults, dict):
        return {"success": False, "message": "Invalid category_defaults"}

    oc = APP_STATE.outputs[out_idx]
    clean = {}
    for cat in THEME_CATEGORIES:
        ent = defaults.get(cat) or {}
        if not isinstance(ent, dict):
            ent = {}
        c = {}
        if cat != 'announcement' and ent.get('text'):
            c['text'] = ent['text']
        if ent.get('bg'):
            c['bg'] = ent['bg']
        clean[cat] = c
    oc.category_defaults = clean

    APP_STATE.config_manager.save_config()
    await _live_commit(rebuild=True, broadcast='state')
    return {"success": True, "category_defaults": clean}


# --- Style profiles API ---
# Named snapshots of theme assignments the operator switches between. The heavy state
# mutations (capture the live look, apply a target) run off the event loop; the tail
# refreshes the profile-bearing library cache, rebuilds slides when the live look
# changed, and broadcasts. See StyleProfileManager.

async def _finish_profile_change(rebuild: bool):
    manager.invalidate_library_cache()   # the library snapshot carries the profile list
    if rebuild:
        await _live_commit(rebuild=True, broadcast='state')


@app.post("/api/style-profiles/create")
async def api_style_profile_create(data: dict = Body(...)):
    """Create a new (blank) profile and switch to it. Blank = keep the current per-output
    defaults, clear every per-library-item theme (they fall back to the output default)."""
    name = (data.get('name') or '').strip() or 'Untitled Profile'

    def _work():
        pid = APP_STATE.style_profile_manager.create_blank(name)
        APP_STATE.style_profile_manager.activate(pid)
        return pid

    pid = await asyncio.to_thread(_work)
    await _finish_profile_change(rebuild=True)
    return {"success": True, "id": pid}


@app.post("/api/style-profiles/activate")
async def api_style_profile_activate(data: dict = Body(...)):
    pid = data.get('id')
    if not isinstance(pid, int):
        return {"success": False, "message": "Missing profile id"}
    ok = await asyncio.to_thread(APP_STATE.style_profile_manager.activate, pid)
    if not ok:
        return {"success": False, "message": "Profile not found"}
    await _finish_profile_change(rebuild=True)
    return {"success": True}


@app.post("/api/style-profiles/rename")
async def api_style_profile_rename(data: dict = Body(...)):
    pid = data.get('id')
    name = (data.get('name') or '').strip()
    if not isinstance(pid, int) or not name:
        return {"success": False, "message": "Missing id or name"}
    await _db_run(APP_STATE.db.rename_style_profile, pid, name)
    await _finish_profile_change(rebuild=False)   # rename doesn't change the live look
    return {"success": True}


@app.post("/api/style-profiles/duplicate")
async def api_style_profile_duplicate(data: dict = Body(...)):
    """Copy an existing profile's full assignments into a new one (the 'clone the look'
    path). Defaults to duplicating the active profile when no source id is given."""
    src = data.get('id')
    if not isinstance(src, int):
        src = APP_STATE.active_profile_id
    name = (data.get('name') or '').strip() or 'Profile Copy'
    pid = await asyncio.to_thread(APP_STATE.style_profile_manager.duplicate, src, name)
    if pid is None:
        return {"success": False, "message": "Source profile not found"}
    await _finish_profile_change(rebuild=False)   # a copy doesn't change the active look
    return {"success": True, "id": pid}


@app.post("/api/style-profiles/delete")
async def api_style_profile_delete(data: dict = Body(...)):
    pid = data.get('id')
    if not isinstance(pid, int):
        return {"success": False, "message": "Missing profile id"}
    profiles = await _db_run(APP_STATE.db.get_style_profiles)
    if not any(p['id'] == pid for p in profiles):
        return {"success": False, "message": "Profile not found"}
    if len(profiles) <= 1:
        return {"success": False, "message": "Cannot delete the last style profile"}

    deleting_active = APP_STATE.active_profile_id == pid

    def _work():
        if deleting_active:
            # Land the live tables on a surviving profile before removing this one.
            other = next(p['id'] for p in profiles if p['id'] != pid)
            APP_STATE.style_profile_manager.activate(other)
        APP_STATE.db.delete_style_profile(pid)

    await asyncio.to_thread(_work)
    await _finish_profile_change(rebuild=deleting_active)
    return {"success": True, "active_profile_id": APP_STATE.active_profile_id}


# --- Bibles API ---

def _import_bible_bytes(content: bytes):
    """Write uploaded Bible XML to a temp file, parse it, and import it. Runs the
    blocking parse + (potentially thousands of) verse inserts in a worker thread
    (called via asyncio.to_thread) so a full-Bible import can't stall the event
    loop. Returns (bible_id, verse_count), or None if no verses were found."""
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode='wb', suffix='.xml', delete=False) as f:
            f.write(content)
            temp_path = f.name
        name, copyright, verses = parse_bible_file(temp_path)
        if not verses:
            return None
        bid = APP_STATE.db.import_bible(name, copyright, verses)
        return bid, len(verses)
    finally:
        if temp_path:
            _safe_unlink(temp_path)

@app.post("/api/bibles/import")
async def api_bible_import(file: UploadFile = File(...)):
    try:
        content = await file.read()
        result = await asyncio.to_thread(_import_bible_bytes, content)
        if result is None:
            return {'success': False, 'message': 'No verses found in XML'}
        bid, count = result
        manager.invalidate_library_cache()
        await _live_commit(broadcast='library')
        return {'success': True, 'id': bid, 'count': count}
    except Exception as e:
        return {'success': False, 'message': str(e)}

@app.get("/api/bibles")
async def api_get_bibles():
    return await _db_run(APP_STATE.db.get_bibles)

@app.post("/api/bibles/delete")
async def api_bibles_delete(data: dict = Body(...)):
    bid = data.get('id')
    if bid:
        await _db_run(APP_STATE.db.delete_bible, bid)
        manager.invalidate_library_cache()
        await _live_commit(broadcast='library')
    return {"success": True}

@app.post("/api/bibles/rename")
async def api_bibles_rename(data: dict = Body(...)):
    bid = data.get('id')
    new_name = data.get('name')
    if bid and new_name:
        await _db_run(APP_STATE.db.rename_bible, bid, new_name)
        manager.invalidate_library_cache()
        await _live_commit(broadcast='library')
    return {"success": True}

@app.get("/api/bibles/{id}/books")
async def api_get_bible_books(id: int):
    return await _db_run(APP_STATE.db.get_bible_books, id)

@app.get("/api/bibles/{id}/{book}/chapters")
async def api_get_bible_chapters(id: int, book: str):
    return await _db_run(APP_STATE.db.get_bible_chapters, id, book)

@app.get("/api/bibles/{id}/{book}/{chapter}")
async def api_get_bible_verses(id: int, book: str, chapter: int):
    return await _db_run(APP_STATE.db.get_bible_verses, id, book, chapter)

@app.post("/api/bibles/resolve-ref")
async def api_bibles_resolve_ref(data: dict = Body(...)):
    """Resolve a free-text scripture reference (e.g. "John 3:16", "Rom 8:28-30",
    "Ps 23") against a bible into the payload the live / add-to-service endpoints
    expect. The lookup stays server-side so abbreviations resolve against the
    selected bible's own (possibly non-English) book names, and so existence of the
    chapter/verses is validated before anything goes live."""
    try:
        bid = int(data.get('id'))
    except (TypeError, ValueError):
        return {"success": False, "message": "Select a bible first."}
    reference = (data.get('reference') or '').strip()
    if not reference:
        return {"success": False, "message": "Enter a reference, e.g. John 3:16."}

    books = await _db_run(APP_STATE.db.get_bible_books, bid)
    parsed = parse_bible_reference(reference, books)
    if not parsed:
        return {"success": False, "message": f'Couldn\'t find "{reference}" in this bible.'}

    book, chapter = parsed['book'], parsed['chapter']
    v_start, v_end = parsed['verse_start'], parsed['verse_end']

    chapters = await _db_run(APP_STATE.db.get_bible_chapters, bid, book)
    # Single-chapter books (Jude, Philemon, Obadiah, 2/3 John): a bare trailing number
    # is conventionally the verse, not the chapter — reinterpret it against chapter 1.
    if v_start is None and len(chapters) == 1 and chapter not in chapters:
        v_start = v_end = chapter
        chapter = chapters[0]
    if chapter not in chapters:
        return {"success": False, "message": f"{book} has no chapter {chapter}."}

    chapter_verses = await _db_run(APP_STATE.db.get_bible_verses, bid, book, chapter)
    verse_nums = [v['verse_num'] for v in chapter_verses]
    if not verse_nums:
        return {"success": False, "message": f"No verses found for {book} {chapter}."}

    if v_start is None:  # whole-chapter reference
        v_start, v_end = min(verse_nums), max(verse_nums)
        ref = f"{book} {chapter}"
    else:
        if v_start not in verse_nums:
            return {"success": False, "message": f"{book} {chapter} has no verse {v_start}."}
        v_end = min(v_end, max(verse_nums))  # clamp an over-long range to what exists
        ref = f"{book} {chapter}:{v_start}" + (f"-{v_end}" if v_end > v_start else "")

    # Include the matched verses so the search UI can show the result without a
    # second round trip.
    selected = [v for v in chapter_verses if v_start <= v['verse_num'] <= v_end]
    return {"success": True, "bible_id": bid, "book": book, "chapter": chapter,
            "verse_start": v_start, "verse_end": v_end, "ref": ref, "verses": selected}

@app.post("/api/live/bible-verse")
async def api_live_bible_verse(data: dict = Body(...)):
    def _apply():
        APP_STATE.current_mode = 'bible'
        APP_STATE.current_bible_data = data
        APP_STATE.is_blank = False

    await _live_commit(mutate=_apply, rebuild=True, broadcast='state')
    return {"success": True}

@app.post("/api/services/add-bible")
async def api_add_bible_to_service(data: dict = Body(...)):
    service_id = APP_STATE.current_service_id
    if (service_id == -1):
        raise HTTPException(status_code=400, detail="No service selected")
    
    prev_item_id = APP_STATE.active_item_id()
    await _db_run(APP_STATE.db.add_bible_to_service, service_id, data, _coerce_at_index(data))
    await _refresh_current_service_items(prev_item_id)
    manager.invalidate_library_cache()
    await _live_commit(broadcast='library')
    return {"success": True}


# Fixed default port for the web UI. A stable, predictable port (rather than an
# OS-assigned free one) is what lets people on other devices reach the UI at an
# address that never changes between launches: http://<this-machine-ip>:49777/.
# 49777 sits in the private/dynamic range, so it rarely clashes with other services.
DEFAULT_PORT = 49777

# The port the server is actually listening on, so the headless image-export renderer
# can navigate back to our own output pages (see _render_export_zip).
_SERVER_PORT = DEFAULT_PORT


def build_server(port=DEFAULT_PORT, host="0.0.0.0", log_level="info"):
    """Prepare runtime state and return a configured, not-yet-running uvicorn Server.

    Split out from start_server so the setup can be reused: returning the Server
    object (rather than running it) gives the caller a clean shutdown hook
    (``server.should_exit = True``).
    """
    global _SERVER_PORT
    _SERVER_PORT = port
    init_app()
    os.makedirs(APP_STATE.export_dir, exist_ok=True)
    APP_STATE.db.checkpoint()
    APP_STATE.exporter.export_outputs()
    config = uvicorn.Config(app, host=host, port=port, log_level=log_level)
    return uvicorn.Server(config)


def start_server(port=DEFAULT_PORT):
    server = build_server(port)
    print(f"Server started at http://localhost:{port}/")
    print(f"Admin UI: http://localhost:{port}/admin")
    print(f"Data directory: {get_data_dir()}")
    server.run()


if __name__ == '__main__':
    _port_args = [a for a in sys.argv[1:] if a.isdigit()]
    port = int(_port_args[0]) if _port_args else DEFAULT_PORT
    start_server(port)
