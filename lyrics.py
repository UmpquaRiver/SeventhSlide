"""
SeventhSlide

A FastAPI-based live presentation tool for song lyrics, Bible verses, and
announcements, designed for use in church services or similar settings.

Song & Lyrics Management:
- Import songs from OpenLP / OpenSong XML files with tolerant parsing
- Create and edit songs directly in the admin UI with verse tagging (V1, C, B, etc.)
  and custom verse ordering
- Supports inline formatting (<b>, <i>, <u>) and chord notation ([Chord] above lyrics)
- Automatic slide pagination: lyrics are split across slides based on the configured
  text box height, with smart line-wrapping that breaks at sentence/clause boundaries
- Optional fluid-slides mode that groups a configurable number of lines per slide
  with highlight/dim colors for line-by-line advancing
- Copyright and CCLI licence number display, configurable per-song

Bible Display:
- Import Bibles from Zefania XML files; browse by book/chapter/verse or search
- Display Bible text and reference in independently positioned and styled text boxes
- Add Bible passages as service items alongside songs

Services:
- Organize songs, Bible passages, and announcements into ordered service lists
- Select, reorder, and navigate between service items during a live presentation
- Per-service and per-song theme overrides for output styling

Announcements:
- Create and manage text-based announcements with their own theme maps
- Add announcements to services or display them standalone

Multiple Outputs:
- Define any number of named outputs (e.g. main screen, lower third, stage monitor),
  each with fully independent layout: canvas size, text box position/size, font,
  alignment (horizontal and vertical), padding, and text opacity
- Per-output backgrounds: transparent, solid color, or image, with separate settings
  for Bible mode
- Per-output themes: save and apply named style presets; override themes at the
  service or song level via theme maps
- Optional fade transitions with configurable duration
- Slide position indicator overlay with configurable position and size
- Copyright info box with configurable position, font, and display mode
  (all slides, first N, last N)
- Global and per-output blank/unblank controls; outputs can be exempt from global blank
- Each output is served as a standalone HTML page via WebSocket, suitable for
  OBS browser sources or any browser window

Live Presentation Controls:
- Unified line cursor across all outputs: advance forward/back or jump to any line
- Line labels (verse tags) shown in admin for easy navigation
- Real-time sync via WebSocket — all connected outputs and admin panels update instantly

Admin Interface:
- Single-page web UI at /admin for all management and live control
- Song library browser with search, inline editing, and XML upload
- Bible browser and search
- Service builder with drag-and-drop item management
- Output configuration panels with visual box positioning
- Live preview of all outputs

Technical:
- Persistent storage via SQLite (songs, announcements, bibles, services, app config)
- Output configuration and app settings stored in the database
- Font measurement via PIL (ImageFont) with cross-platform font resolution for accurate pagination
- Optional local font bundling for outputs (embeds @font-face CSS)
- Packagable as a PyInstaller executable

Usage:
- Run: python3 lyrics.py
- Open http://localhost:49777/admin in your browser
"""

import xml.etree.ElementTree as ET
import threading
import json
import os
import sys
import html
import re
import subprocess
import asyncio
import shutil
import hashlib
import math
import uuid
import tempfile
import time
import types
from contextlib import contextmanager
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, field, asdict
from functools import lru_cache

from fastapi import FastAPI, WebSocket, UploadFile, File, Form, Body, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import logging

# --- Extracted subsystems (see seventhslide/ package) ---------------------
# These were split out of this module; imported * to preserve the original
# flat namespace every downstream reference relies on.
from seventhslide.paths import *      # noqa: F401,F403
from seventhslide.models import *     # noqa: F401,F403
from seventhslide.parsing import *    # noqa: F401,F403
from seventhslide.fonts import *      # noqa: F401,F403
from seventhslide.database import *   # noqa: F401,F403

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
    missing = [t for t in req_tokens if t not in keys]
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

@lru_cache(maxsize=256)
def _get_font_measurement(font_family, font_size):
    """Get font measurement function. Returns (measure_func, line_height).

    Cached because font resolution (fc-match subprocess + PIL load) is expensive
    and fonts don't change at runtime. Bounded eviction via lru_cache.
    """
    if HAS_PIL:
        font_obj = None
        # 1. Try direct load
        try:
            font_obj = ImageFont.truetype(font_family, font_size)
        except Exception:
            pass

        # 2. Resolve via the cross-platform font resolver if the direct load failed.
        # Reuse the shared, disk-backed resolver (fontconfig where present, else the
        # bundled font index) rather than rolling our own — same resolved file, but
        # cached across calls and restarts, and one code path to maintain.
        if font_obj is None:
            path = _resolve_font_file_cached(font_family)
            if path:
                try:
                    font_obj = ImageFont.truetype(path, font_size)
                except Exception:
                    pass

        if font_obj:
            def measure(text):
                try:
                    # Prefer advance-width measurement to match browser word-wrapping.
                    if hasattr(font_obj, 'getlength'):
                        return float(font_obj.getlength(text)) * 1.01
                except Exception:
                    pass
                try:
                    bbox = font_obj.getbbox(text)
                    return (bbox[2] - bbox[0]) * 1.01 if bbox else 0
                except Exception:
                    return 0
            # Match CSS line-height: 1.2
            line_height = int(math.ceil(font_size * 1.2))
            return measure, line_height

    # Fallback: simple approximation (assumes ~0.5 * font_size average char width)
    # Lowered from 0.6 to 0.5 to be less conservative and prevent unnecessary wrapping
    avg_char_width = font_size * 0.5
    def measure(text):
        return len(text) * avg_char_width
    line_height = int(math.ceil(font_size * 1.2))
    return measure, line_height


# ---------------------- Slide generation ----------------------

TAG_RE = re.compile(r'<[^>]+>')

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
    words = text.split()
    sub_lines = []
    current_words = []
    
    for word in words:
        test_words = current_words + [word]
        test_text = " ".join(test_words)
        test_wrapped = wrap_plain_text_to_width(test_text, measure_func, max_width_px)
        
        if len(test_wrapped) <= max_visual_lines:
            current_words.append(word)
        else:
            if current_words:
                sub_lines.append(" ".join(current_words))
            current_words = [word]
            # A single word wider than max_visual_lines still ends up on its own
            # sub-line (on the next iteration, or at the final flush below). We
            # can't split a word more granularly than wrap_plain_text_to_width
            # already does, so there is nothing extra to handle here.

    if current_words:
        sub_lines.append(" ".join(current_words))
        
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
    def render(line: str) -> str:
        """Convert a line containing ``[chord]`` markers into chord-over-lyric HTML."""
        cells: list[str] = []
        open_tags: list[str] = []          # inline tags currently in effect, e.g. ['b']
        pending_chord: Optional[str] = None  # chord awaiting its lyric syllable

        def emit(chord: Optional[str], syllable: str) -> None:
            if syllable:
                body = ''.join(f'<{t}>' for t in open_tags) + syllable + \
                       ''.join(f'</{t}>' for t in reversed(open_tags))
            else:
                body = _ChordProcessor._ZWSP
            if chord:
                glyph = chord.translate(_ChordProcessor._ACCIDENTALS)
                # Expand only when the chord is wider than the syllable it sits on (glyph
                # count, like OpenLP). Otherwise overlap: the chord floats free with zero
                # width so the lyric is never stretched. Chords with no syllable always
                # overlap — there is nothing to push and nothing to bridge.
                if syllable and len(glyph) > len(syllable):
                    cells.append(f'<span class="cc cc-x"><span class="ch">{glyph}</span>'
                                 f'<span class="ly">{body}{_ChordProcessor._FILL}</span></span>')
                else:
                    cells.append(f'<span class="cc"><span class="ch">{glyph}</span>'
                                 f'<span class="ly">{body}</span></span>')
            else:
                cells.append(f'<span class="cc"><span class="ch">{_ChordProcessor._ZWSP}</span>'
                             f'<span class="ly">{body}</span></span>')

        for m in _ChordProcessor._TOKEN_RE.finditer(line):
            kind = m.lastgroup
            if kind == 'chord':
                # Two chords with no syllable between: anchor the first on an empty cell.
                if pending_chord is not None:
                    emit(pending_chord, '')
                pending_chord = m.group('chord')
            elif kind == 'tag':
                raw = m.group()
                is_close = raw[1] == '/'
                name = (raw[2:-1] if is_close else raw[1:-1]).lower()
                if is_close:
                    if name in open_tags:
                        open_tags.remove(name)
                else:
                    open_tags.append(name)
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


class _VerseParser:
    """Handles verse structure parsing and ordering."""

    @staticmethod
    def parse_verses(lyrics_text: str, verse_order: Optional[str] = None) -> tuple[list[str], list[str]]:
        """
        Parse lyrics into ordered verses with codes.

        Returns:
            (verses, verse_codes) - Lists of verse content and their codes
        """
        # Parse structure from lyrics_text if headers exist
        # Pattern: ---[Label]---
        raw_parts = re.split(r'---\[([^\]]+)\]---\n', lyrics_text)

        if len(raw_parts) <= 1:
            # Legacy/plain text - no headers
            verses = [v for v in lyrics_text.split('\n\n') if v != '']
            verse_codes = ['' for _ in verses]
            return verses, verse_codes

        # Parse blocks with headers
        blocks = []
        i = 1
        while i < len(raw_parts):
            label = raw_parts[i].strip()
            content = raw_parts[i + 1].strip()
            code = _VerseParser._label_to_code(label)
            blocks.append({'code': code, 'content': content, 'label': label})
            i += 2

        # Order blocks according to verse_order if provided
        if verse_order:
            req_order = [x.lower() for x in verse_order.split()]
            lookup = {b['code']: b['content'] for b in blocks}

            ordered_parts = []
            verse_codes = []
            for token in req_order:
                if token in lookup:
                    ordered_parts.append(lookup[token])
                    verse_codes.append(token)

            if ordered_parts:
                verses = ordered_parts
            else:
                # Fallback if no tokens match
                verses = [b['content'] for b in blocks]
                verse_codes = [b['code'] for b in blocks]
        else:
            # Maintain appearance order
            verses = [b['content'] for b in blocks]
            verse_codes = [b['code'] for b in blocks]

        return verses, verse_codes

    @staticmethod
    def _label_to_code(label: str) -> str:
        """Convert a verse label like 'Verse:1' to a code like 'v1'."""
        lud = label.lower()
        digits = "".join(filter(str.isdigit, lud))
        label_type = lud.split(':')[0]
        prefix = _VERSE_CODE_MAP.get(label_type)
        code = (prefix + (digits or '1')) if prefix else "misc"
        return code


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


def _compute_line_groups(lyrics_text, output_config, verse_order=None):
    """
    Compute logical line list and how they are grouped into slides for an output.

    Returns:
        (all_lines, verse_indices, groups, line_labels, verse_codes) where:
        - all_lines: flat list of HTML strings (one per logical line)
        - verse_indices: verse index for each line
        - groups: list of dicts with {'indices': [...], 'active_count': int}
        - line_labels: label for each line (e.g., 'v1', 'c1')
        - verse_codes: list of verse codes in order
    """
    # Setup measurement and dimensions
    measure_func, line_height = _get_font_measurement(output_config.font_family, output_config.font_size)
    avail_w = max(1, output_config.width_px - 2 * output_config.area_padding)
    avail_h = max(1, output_config.height_px - 2 * output_config.area_padding)
    max_visual_px = avail_h

    # In follow-lines mode the active lines render at highlight_font_size when it's set.
    # When that's larger than the base font, those lines are taller (and may wrap wider)
    # than the base measurement — so measure them separately and pack the slide's active
    # lines at this height, otherwise enlarged active text overflows the box.
    hl_enabled = (output_config.follow_lines > 0
                  and output_config.highlight_font_size > 0
                  and output_config.highlight_font_size != output_config.font_size)
    if hl_enabled:
        measure_func_hl, line_height_hl = _get_font_measurement(
            output_config.font_family, output_config.highlight_font_size)
    else:
        measure_func_hl, line_height_hl = measure_func, line_height

    # Helper function to estimate visual line count
    def visual_lines_for_logical_line(lyric_text: str) -> int:
        """Calculate how many visual lines a logical line will take.

        Measures the lyric text only — chords sit *above* the lyric and never widen
        the line beyond the syllables they cover, so they don't affect wrap count.
        """
        plain = TAG_RE.sub('', lyric_text).replace('\n', '')
        wrapped = wrap_plain_text_to_width(plain, measure_func, avail_w)
        return max(1, len(wrapped))

    def active_line_px(plain_text: str, has_chords: bool, extra_px: int, base_px: int) -> int:
        """Rendered height of a line when it's an active (highlighted) line. In follow
        mode with a highlight font size this is larger than the base height (bigger line
        height, and possibly more wrap lines); otherwise it matches the base height."""
        if not hl_enabled:
            return base_px
        eff_lh = line_height_hl * 1.8 if has_chords else line_height_hl
        n = max(1, len(wrap_plain_text_to_width(plain_text, measure_func_hl, avail_w)))
        return n * eff_lh + extra_px

    # Parse verses and verse codes
    verses, verse_codes = _VerseParser.parse_verses(lyrics_text, verse_order)

    # Initialize tracking structures
    all_lines: list[str] = []
    line_labels: list[str] = []
    verse_indices: list[int] = []
    next_idx = 0

    # Initialize slide grouper
    grouper = _SlideGrouper(output_config, max_visual_px)

    # Process each verse
    for v_idx, verse in enumerate(verses):
        logical_lines = verse.split('\n')

        # Flush buffer at verse boundary if not in fluid mode
        if not output_config.fluid_slides:
            grouper.flush_buffer()

        for li_idx, raw_line in enumerate(logical_lines):
            # Forced split marker: flush current slide group and skip this line
            if raw_line.strip() == '[--}{--]':
                grouper.flush_buffer()
                continue

            # The lyric text alone (no chord markers) — used for measurement and as the
            # plain fallback. Chords are rendered above these syllables, not inline.
            plain_lyric = re.sub(r' +', ' ', _ChordProcessor.strip_chords(raw_line)).strip()

            # Render chords above the lyric, or fall back to plain lyric text.
            has_chords = output_config.show_chords and _ChordProcessor.has_chords(raw_line)
            if has_chords:
                html_line = _ChordProcessor.render(raw_line)
            else:
                html_line = plain_lyric

            # Skip blank lines (a chord-only line still renders; a blank lyric does not)
            if not html_line:
                continue

            # Calculate effective line height (chords need more vertical space)
            effective_line_height = line_height * 1.8 if has_chords else line_height

            # Get verse label
            verse_label = verse_codes[v_idx] if v_idx < len(verse_codes) else ''

            # Calculate visual lines needed (from lyric width — chords don't widen the line)
            vis_lines = visual_lines_for_logical_line(plain_lyric)

            # Account for verse gap on first line of verse (if fluid mode)
            extra_px = 0
            if output_config.fluid_slides and li_idx == 0 and v_idx > 0 and output_config.verse_gap > 0:
                extra_px = output_config.verse_gap

            total_px = vis_lines * effective_line_height + extra_px

            # Split line if it's too tall. A single logical line tall enough to need
            # splitting is split on its plain lyric text (chords are dropped on the
            # rare overflow path); normal-height chord lines keep their chords.
            if total_px > max_visual_px:
                sub_lines = split_line_to_fit(
                    plain_lyric,
                    measure_func,
                    effective_line_height,
                    avail_w,
                    max_visual_px - extra_px,
                    is_html=False
                )

                # Add each sub-line
                for part_idx, sub_line in enumerate(sub_lines):
                    sub_extra_px = extra_px if part_idx == 0 else 0
                    vis_lines_sub = visual_lines_for_logical_line(sub_line)
                    total_px_sub = vis_lines_sub * effective_line_height + sub_extra_px
                    active_px_sub = active_line_px(sub_line, has_chords, sub_extra_px, total_px_sub)

                    all_lines.append(sub_line)
                    verse_indices.append(v_idx)

                    # Label split lines
                    if len(sub_lines) > 1:
                        split_label = f"{verse_label} ({part_idx + 1}/{len(sub_lines)})" if verse_label else f"Part {part_idx + 1}/{len(sub_lines)}"
                    else:
                        split_label = verse_label
                    line_labels.append(split_label)

                    grouper.add_line(next_idx, total_px_sub, active_px_sub, v_idx)
                    next_idx += 1
            else:
                # Line fits normally
                active_px = active_line_px(plain_lyric, has_chords, extra_px, total_px)
                all_lines.append(html_line)
                verse_indices.append(v_idx)
                line_labels.append(verse_label)

                grouper.add_line(next_idx, total_px, active_px, v_idx)
                next_idx += 1

    # Final flush and get groups
    grouper.flush_buffer()
    groups = grouper.get_groups()

    return all_lines, verse_indices, groups, line_labels, verse_codes


# ---------------------- Application state ----------------------

class PlayerController:
    """Handles cursor navigation and slide position tracking."""

    def __init__(self, app_state):
        """Initialize PlayerController with reference to AppState.

        Args:
            app_state: Reference to parent AppState instance
        """
        self.app_state = app_state
        self._line_cursor = 0
        self._total_lines = 0
        self._all_lines: list[str] = []  # Store logical lines for UI display
        self._all_line_labels: list[str] = []  # Store labels

    def _find_next_change_line(self, start: int, direction: int) -> Optional[int]:
        """Find the next line where at least one output changes slides.

        Args:
            start: Starting line number
            direction: 1 for forward, -1 for backward

        Returns:
            Line number where a change occurs, or None if no change found
        """
        if not self.app_state.outputs or self._total_lines <= 0:
            return None

        cur_slides = [oc.line_to_slide[start] if oc.line_to_slide else 0 for oc in self.app_state.outputs]
        candidate = start + direction

        while 0 <= candidate < self._total_lines:
            cand_slides = [oc.line_to_slide[candidate] if oc.line_to_slide else 0 for oc in self.app_state.outputs]
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
            })
            self.db.save_output_configs([oc.to_persist_dict() for oc in self.app_state.outputs])
        except Exception as e:
            logger.warning("Failed to save config: %s", e, exc_info=True)

    def reorder_output(self, index: int, direction: str) -> bool:
        """
        Reorder output at `index` by moving it `direction` ('up' or 'down').
        Returns True if changed, False otherwise.
        """
        if direction not in ('up', 'down'):
            return False

        n = len(self.app_state.outputs)
        if not (0 <= index < n):
            return False

        if direction == 'up':
            if index > 0:
                self.app_state.outputs[index], self.app_state.outputs[index-1] = self.app_state.outputs[index-1], self.app_state.outputs[index]
                self.save_config()
                return True
        elif direction == 'down':
            if index < n - 1:
                self.app_state.outputs[index], self.app_state.outputs[index+1] = self.app_state.outputs[index+1], self.app_state.outputs[index]
                self.save_config()
                return True
        return False


class ThemeResolver:
    """Handles theme hierarchy resolution (service < song < service item)."""

    def __init__(self, app_state):
        """Initialize ThemeResolver with reference to AppState.

        Args:
            app_state: Reference to parent AppState instance
        """
        self.app_state = app_state

    def _get_current_service_theme_map(self) -> dict:
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
        mode = self.app_state.current_mode
        sid = None
        if mode == 'song':
            sid = self.app_state.current_song_id
        elif mode == 'announcement':
            aid = self.app_state.current_song_id
            if aid:
                try:
                    ann = self.app_state.db.get_announcement(aid)
                except Exception:
                    ann = None
                if ann:
                    return ann.get('theme_map') or {}
            return {}
        elif mode == 'service':
            itm = self.app_state.current_service_item()
            if itm is not None:
                if itm.get('item_type') == 'announcement':
                    # For announcement service items, get announcement's theme_map
                    try:
                        adata = json.loads(itm.get('data', '{}'))
                        aid = adata.get('announcement_id')
                        if aid:
                            ann = self.app_state.db.get_announcement(aid)
                            if ann:
                                return ann.get('theme_map') or {}
                    except Exception:
                        pass
                    return {}
                sid = itm.get('song_id')

        if not sid:
            return {}
        try:
            song = self.app_state.db.get_song(sid)
        except Exception:
            song = None
        if not song:
            return {}
        return song.get('theme_map') or {}

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
        if mode in ('song', 'bible', 'announcement'):
            return mode
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

    def _resolve_effective_style_for_output(self, oc: 'OutputConfig') -> dict:
        """Resolve the effective style for an output by selecting one text theme and
        one background theme through the cascade
        (output category default < service < song < service item), then combining
        their complete style dicts on top of the output's intrinsic fields."""
        category = self._get_active_category()
        cat_def = (oc.category_defaults or {}).get(category) or {}
        text_id = cat_def.get('text')
        bg_id = cat_def.get('bg')

        # Gather per-level {output: {text, bg}} maps in ascending priority order.
        svc_map = self._get_current_service_theme_map()

        skip_song_theme = False
        si_map = {}
        if self.app_state.current_mode == 'service':
            itm = self.app_state.current_service_item()
            if itm is not None:
                si_map = itm.get('theme_map') or {}
                skip_song_theme = itm.get('has_overrides', False)

        song_map = {} if skip_song_theme else self._get_current_song_theme_map()

        for level_map in (svc_map, song_map, si_map):
            entry = level_map.get(oc.name) if isinstance(level_map, dict) else None
            if isinstance(entry, dict):
                if entry.get('text'):
                    text_id = entry['text']
                if entry.get('bg'):
                    bg_id = entry['bg']

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
        """Extract background image URLs from a style dictionary."""
        images = []
        if style.get('background_type') == 'image' and style.get('background_image'):
            images.append(style['background_image'])
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


def _bg_params(bg_type: str, bg_color: str, bg_image: str) -> tuple:
    """Return (bg, initial_bg_a, initial_bg_key) for the given background settings."""
    if bg_type == 'image' and bg_image:
        return 'transparent', f"background-image:url('{bg_image}');", f"i:{bg_image}"
    if bg_type == 'color':
        return bg_color, f"background-color:{bg_color};", f"c:{bg_color}"
    return 'transparent', '', 't'


class HtmlExporter:
    """Handles HTML generation for web display outputs."""

    def __init__(self, app_state):
        """Initialize HtmlExporter with reference to AppState.

        Args:
            app_state: Reference to parent AppState instance
        """
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
            # defaults). Restored after the template is built.
            _export_orig_style = oc.style_to_dict()
            oc.apply_style_dict(self.app_state.theme_resolver.default_style_for_output(oc))

            # Compute initial background for double-buffered bg layers
            bg, initial_bg_a, initial_bg_key = _bg_params(
                oc.background_type, oc.background_color, oc.background_image)

            htmlpage = HTML_TEMPLATE.format(
                title=f"Lyrics - {oc.name}", bg=bg, fg=oc.highlight_color,
                initial_bg_a=initial_bg_a, initial_bg_key=initial_bg_key,
                canvas_w=oc.canvas_width, canvas_h=oc.canvas_height,
                box_x=oc.box_x, box_y=oc.box_y, box_w=oc.width_px, box_h=oc.height_px,
                pad=oc.area_padding, font_family=oc.font_family, font_size=oc.font_size,
                initial=initial, output_name=oc.name,
                enable_fade='true' if oc.enable_fade else 'false',
                fade_duration=oc.fade_duration,
                align=oc.align,
                bundled_fonts_css=bundled_fonts_css,

                ind_x=oc.indicator_x,
                ind_y=oc.indicator_y,
                ind_fs=oc.indicator_font_size,
                ind_color='#ffffff',
                ind_opacity=str(oc.indicator_opacity) if oc.show_indicator else '0',

                # Wall-clock overlay (intrinsic; the format flags seed the JS ticker)
                clock_x=oc.clock_x,
                clock_y=oc.clock_y,
                clock_fs=oc.clock_font_size,
                clock_color=oc.clock_color,
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
                bible_text_color=oc.bible_text_color,
                bible_text_align=oc.bible_text_align,
                bible_text_valign_css=_valign_to_css(oc.bible_text_valign),
                bible_text_opacity=str(oc.bible_text_opacity) if oc.show_bible_text else '0',
                bible_main_font_family=oc.bible_main_font_family,
                bible_main_font_size=oc.bible_main_font_size,

                # Bible Reference Box
                bible_ref_x=oc.bible_ref_box_x,
                bible_ref_y=oc.bible_ref_box_y,
                bible_ref_w=oc.bible_ref_width,
                bible_ref_h=oc.bible_ref_height,
                bible_ref_font_family=oc.bible_ref_font_family,
                bible_ref_font_size=oc.bible_ref_font_size,
                bible_ref_color=oc.bible_ref_color,
                bible_ref_opacity=str(oc.bible_ref_opacity) if oc.show_bible_ref else '0',
                bible_ref_justify=_align_to_css(oc.bible_ref_align, flex_mode=True),
                bible_ref_valign_css=_valign_to_css(oc.bible_ref_valign, flex_mode=True),

                # Copyright Info Box
                copyright_x=oc.copyright_box_x,
                copyright_y=oc.copyright_box_y,
                copyright_w=oc.copyright_box_width,
                copyright_h=oc.copyright_box_height,
                copyright_font_family=oc.copyright_font_family,
                copyright_font_size=oc.copyright_font_size,
                copyright_color=oc.copyright_color,
                copyright_opacity=str(oc.copyright_text_opacity) if oc.show_copyright else '0',
                copyright_align=oc.copyright_align,
                copyright_valign_css=_valign_to_css(oc.copyright_valign),

                # Video settings
                video_enabled='true' if oc.video_enabled else 'false',
                countdown_x=oc.video_countdown_x,
                countdown_y=oc.video_countdown_y,
                countdown_fs=oc.video_countdown_font_size,
                countdown_font_family=oc.video_countdown_font_family,
                countdown_color=oc.video_countdown_color,
                countdown_align=oc.video_countdown_align,
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

            # Restore the output's runtime style now that the template is baked.
            oc.apply_style_dict(_export_orig_style)

            fname = f"{oc.name}.html"
            try:
                with open(os.path.join(self.app_state.export_dir, fname), 'w', encoding='utf-8') as f:
                    f.write(htmlpage)
            except Exception as e:
                logger.error("Error exporting output %s: %s", oc.name, e, exc_info=True)


def _build_template_ann_html(layout: dict, field_names: list, field_values: list) -> str:
    """Build the full-canvas text-box overlay for a template-based announcement.

    The slide background is supplied by the output's background layer (driven by the
    resolved background theme), so this overlay is intentionally transparent and only
    renders the template's text boxes on top.
    """
    bg_css = "background-color:transparent;"

    text_boxes = layout.get('text_boxes', [])
    valign_map = {'top': 'flex-start', 'middle': 'center', 'bottom': 'flex-end'}
    boxes_html = []
    for i, box in enumerate(text_boxes):
        content = field_values[i] if i < len(field_values) else ''
        # Escape & first, allow <b>/<i>/<u> markup, convert \n → <br>
        content_esc = content.replace('&', '&amp;')
        content_esc = re.sub(r'<(?!\/?(?:b|i|u)\b)[^>]*>', '', content_esc)
        content_esc = content_esc.replace('\n', '<br>')
        justify = valign_map.get(box.get('vertical_align', 'middle'), 'center')
        ff = box.get('font_family', 'Helvetica').replace("'", "\\'")
        outer_style = (
            f"position:absolute;left:{box.get('x', 0)}%;top:{box.get('y', 0)}%;"
            f"width:{box.get('w', 100)}%;height:{box.get('h', 100)}%;"
            f"display:flex;flex-direction:column;justify-content:{justify};"
            f"overflow:hidden;box-sizing:border-box;"
        )
        inner_style = (
            f"font-family:'{ff}';font-size:{box.get('font_size', 48)}px;"
            f"color:{box.get('font_color', '#ffffff')};text-align:{box.get('text_align', 'center')};"
            f"{'font-weight:bold;' if box.get('bold') else ''}"
            f"{'font-style:italic;' if box.get('italic') else ''}"
            f"white-space:pre-wrap;overflow-wrap:break-word;word-break:break-word;"
        )
        boxes_html.append(f'<div style="{outer_style}"><div style="{inner_style}">{content_esc}</div></div>')

    return f'<div style="position:absolute;inset:0;{bg_css}overflow:hidden;">{"".join(boxes_html)}</div>'


class SlideBuilder:
    """Handles core slide generation logic for songs, Bible verses, and announcements."""

    def __init__(self, app_state):
        """Initialize SlideBuilder with reference to AppState.

        Args:
            app_state: Reference to parent AppState instance
        """
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

        # Give ignored outputs empty slides
        total_lines = len(plain_lines)
        for oc in self.app_state.outputs:
            if oc.is_ignored:
                oc.slides = ['']
                oc.line_to_slide = [0] * total_lines
                oc.index = 0

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
            most_restrictive_oc.bible_main_font_family, most_restrictive_oc.bible_main_font_size)
        avail_w = max(1, most_restrictive_oc.bible_text_box_width - 2 * most_restrictive_oc.bible_text_padding)
        avail_h = max(1, most_restrictive_oc.bible_text_box_height - 2 * most_restrictive_oc.bible_text_padding)

        wrapped = wrap_plain_text_to_width(full_measure_text, measure_func, avail_w)
        if len(wrapped) * line_height > avail_h:
            return split_line_to_fit(full_measure_text, measure_func, line_height, avail_w, avail_h, is_html=False)
        return [full_measure_text]

    @staticmethod
    def _build_bible_slides_for_output(oc, verse_chunks, chapter):
        """Build one output's slides by greedily combining shared verse chunks that fit together."""
        measure_func, line_height = _get_font_measurement(oc.bible_main_font_family, oc.bible_main_font_size)
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
        """Build one full-canvas slide per output from an announcement template layout."""
        template_id = item_data.get('template_id')
        field_values = item_data.get('field_values', [])
        tmpl = self.app_state.db.get_ann_template(template_id) if template_id else None
        field_names = tmpl['field_names'] if tmpl else []
        layouts = self.app_state.db.get_ann_template_layouts(template_id) if template_id else {}

        for oc in self.app_state.outputs:
            layout = layouts.get(oc.name)
            if layout and not oc.is_ignored:
                # Background comes from the resolved background theme via the output's
                # background layer; this overlay only carries the text boxes.
                oc.template_html = _build_template_ann_html(layout, field_names, field_values)
            else:
                oc.template_html = ''
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

        # First pass: compute line groups for all outputs to find max line count
        output_data = []
        all_lines_master = None
        all_line_labels_master = None
        total_lines = 0

        for oc in active_outputs:
            all_lines, verse_indices, groups, line_labels, verse_codes = _compute_line_groups(song.lyrics, oc, verse_order)
            output_data.append({
                'oc': oc,
                'all_lines': all_lines,
                'verse_indices': verse_indices,
                'groups': groups,
                'line_labels': line_labels,
                'verse_codes': verse_codes
            })

            if all_lines_master is None or len(all_lines) > len(all_lines_master):
                all_lines_master = all_lines
                all_line_labels_master = line_labels

            total_lines = max(total_lines, len(all_lines))

        # Second pass: build slides and line_to_slide mappings
        for data in output_data:
            self._build_song_slides_for_output(data, total_lines, is_announcement)

        # Give disabled announcement outputs empty slides
        if is_announcement:
            for oc in self.app_state.outputs:
                if not oc.show_announcements:
                    self._set_empty_slides(oc, total_lines)

        # Give ignored outputs empty slides (they are excluded from pagination)
        for oc in self.app_state.outputs:
            if oc.is_ignored:
                self._set_empty_slides(oc, total_lines)

        # Update app state
        self.app_state.player._total_lines = total_lines
        self.app_state.player._all_lines = all_lines_master or []
        if is_announcement and all_line_labels_master:
            all_line_labels_master = ['AN'] * len(all_line_labels_master)
        self.app_state.player._all_line_labels = all_line_labels_master or []
        self.app_state.player._line_cursor = 0

        # Restore global output styles
        self._restore_styles(originals)

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

            # Apply verse gap when verse boundary occurs within a slide
            if i > 0 and oc.fluid_slides and oc.verse_gap > 0:
                prev_v_idx = verse_indices[grp[i-1]]
                if v_idx != prev_v_idx:
                    line = f'<span style="display:inline-block; margin-top:{oc.verse_gap}px; width:100%;">{line}</span>'

            # Apply follow lines highlighting
            if oc.follow_lines > 0:
                is_active = i < count
                color = oc.highlight_color if is_active else oc.dim_color

                style = f'color:{color};'
                if is_active and oc.highlight_font_size > 0:
                    style += f' font-size:{oc.highlight_font_size}px;'

                line = f'<span style="{style}">{line}</span>'

            html_lines.append(line)

        return '<br/>'.join(html_lines)

    def _rebuild_slides_and_mappings(self):
        """Rebuild slides and line-to-slide mappings for all outputs."""
        # Pre-compute effective style per output (Global < Service < Song)
        effective_styles = {}
        originals = {}

        for oc in self.app_state.outputs:
            originals[oc.name] = oc.style_to_dict()
            eff = self.app_state.theme_resolver._resolve_effective_style_for_output(oc)
            effective_styles[oc.name] = eff
            oc.apply_style_dict(eff)

        self.app_state.effective_output_styles = effective_styles

        # Determine if we're processing Bible verses
        bible_data = None
        if self.app_state.current_mode == 'bible':
            bible_data = self.app_state.current_bible_data
        elif self.app_state.current_item_type() == 'bible':
            try:
                bible_data = json.loads(self.app_state.current_service_item()['data'])
            except Exception:
                bible_data = {}

        # Process Bible verses
        if bible_data:
            self._rebuild_slides_bible(bible_data, originals)
            return

        # Process video items — no slides needed, just store video data
        if self.app_state.current_item_type() == 'video':
            try:
                self.app_state.current_video_data = json.loads(
                    self.app_state.current_service_item().get('data', '{}'))
            except Exception:
                self.app_state.current_video_data = {}
            self.app_state._reset_video_timing(
                autoplay=bool(self.app_state.current_video_data.get('autoplay', True)))
            self.app_state._clear_outputs_and_player()
            self._restore_styles(originals)
            return

        # Process single image service items — no slides needed
        if self.app_state.current_item_type() == 'image':
            try:
                item_data = json.loads(
                    self.app_state.current_service_item().get('data', '{}'))
            except Exception:
                item_data = {}
            filename = item_data.get('filename', '')
            self.app_state.current_image_data = {
                'folder_id': None,
                'folder_name': filename,
                'images': [filename] if filename else [],
                'index': 0,
            }
            self.app_state._clear_outputs_and_player()
            self._restore_styles(originals)
            return

        # Process image folder items — no slides needed, just load folder data
        if self.app_state.current_item_type() == 'image_folder':
            try:
                item_data = json.loads(
                    self.app_state.current_service_item().get('data', '{}'))
            except Exception:
                item_data = {}
            folder_id = item_data.get('folder_id')
            images = item_data.get('images', [])
            self.app_state.current_image_data = {
                'folder_id': folder_id,
                'folder_name': item_data.get('folder_name', ''),
                'images': images,
                'index': 0,
            }
            self.app_state._clear_outputs_and_player()
            self._restore_styles(originals)
            return

        # Process divider items — no slides needed, output stays blank
        if self.app_state.current_item_type() == 'divider':
            self.app_state._clear_outputs_and_player()
            self._restore_styles(originals)
            return

        # Process announcement library items (direct play mode)
        if self.app_state.current_mode == 'announcement':
            self._rebuild_slides_template_ann(self.app_state.current_announcement_data, originals)
            return

        # Process template-based announcements in service
        if self.app_state.current_item_type() == 'announcement':
            self._rebuild_slides_template_ann(self.app_state.current_service_item(), originals)
            return

        # Process songs
        song = self.get_current_song()
        if not song or not self.app_state.outputs:
            return

        # Determine verse order
        verse_order = None
        item = self.app_state.current_service_item()
        if item is not None:
            verse_order = item.get('verse_order')
        elif self.app_state.current_song_verse_order:
            verse_order = self.app_state.current_song_verse_order

        self._rebuild_slides_song(song, verse_order, False, originals)






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
        self.current_announcement_data = {} # {'id': int, 'template_id': int, 'field_values': list, 'title': str}

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

        # Runtime caches
        self.effective_output_styles: dict = {}
        self.bundled_font_css_map: dict = {}

        # Pending video task — set when waiting for clients to buffer before switching
        self.pending_video_task: asyncio.Task = None

        # Initialize components
        self.config_manager = ConfigurationManager(self)
        self.theme_resolver = ThemeResolver(self)
        self.font_manager = FontManager(self)
        self.slide_builder = SlideBuilder(self)
        self.exporter = HtmlExporter(self)
        self.player = PlayerController(self)

        self.config_manager.load_config()

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
                try:
                    fn = json.loads(item.get('data', '{}')).get('filename', '')
                    if fn:
                        urls.append(f'/static/videos/{fn}')
                except Exception:
                    pass
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



# Global application state
APP_STATE = AppState()

# Ensure export directory and subdirectories exist for static file mounting
os.makedirs(APP_STATE.export_dir, exist_ok=True)
os.makedirs(os.path.join(APP_STATE.export_dir, 'images'), exist_ok=True)


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


# ---------------------- WebSockets ----------------------

def _get_copyright_song_id():
    """Return the song_id relevant for copyright display given current APP_STATE."""
    mode = APP_STATE.current_mode
    if mode == 'song':
        return APP_STATE.current_song_id
    if mode == 'service':
        itm = APP_STATE.current_service_item()
        if itm is not None and itm.get('item_type') == 'song':
            return itm.get('song_id')
    return None


def _build_copyright_base(song) -> str:
    """Build the raw copyright text from a song dict (no slide-position filtering)."""
    if not song or not song.get('show_copyright', 0):
        return ''
    lines = []
    authors = song.get('authors', [])
    if authors:
        lines.append(', '.join(authors))
    copyright_text = song.get('copyright', '')
    if copyright_text:
        lines.append(copyright_text)
    ccli = APP_STATE.ccli_licence_number
    if ccli:
        lines.append(f'CCLI License #{ccli}')
    return '\n'.join(lines)


def _build_copyright_info(oc, copyright_base: str = ''):
    """Build copyright info string for the given output, applying slide-position filtering.

    Pass pre-computed copyright_base to avoid redundant DB lookups across outputs.
    """
    if not copyright_base:
        song_id = _get_copyright_song_id()
        if song_id:
            song = APP_STATE.db.get_song(song_id)
            copyright_base = _build_copyright_base(song)

    copyright_info = copyright_base
    if copyright_info:
        eff_style = (APP_STATE.effective_output_styles or {}).get(oc.name) or oc.style_to_dict()
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

def _resolve_bible_content(content, item):
    mode = content['mode']
    b_data = None
    if mode == 'bible':
        b_data = APP_STATE.current_bible_data
    elif item and item.get('item_type') == 'bible':
        content['mode'] = 'bible'
        try:
            b_data = json.loads(item.get('data', '{}'))
        except json.JSONDecodeError:
            b_data = {}
    if b_data:
        ref_text = b_data.get('ref', '')
        version = b_data.get('version', '')
        if ref_text:
            content['bible_ref'] = f"{ref_text}\n{version}" if version else ref_text


def _resolve_video_content(content, item):
    if content['mode'] != 'video' and not (item and item.get('item_type') == 'video'):
        return
    content['mode'] = 'video'
    vd = APP_STATE.current_video_data
    filename = vd.get('filename', '')
    if filename:
        content['video_url'] = f'/static/videos/{filename}'
    content['video_loop'] = bool(vd.get('loop', False))
    content['video_autoplay'] = APP_STATE.video_is_playing


def _resolve_image_content(content, item):
    if content['mode'] != 'image' and not (item and item.get('item_type') in ('image_folder', 'image')):
        return
    content['mode'] = 'image'
    img_data = APP_STATE.current_image_data
    images = img_data.get('images', [])
    idx = img_data.get('index', 0)
    if images and 0 <= idx < len(images):
        content['image_url'] = f'/static/images/{images[idx]}'


_CONTENT_RESOLVERS = (_resolve_bible_content, _resolve_video_content, _resolve_image_content)


class ConnectionManager:
    def __init__(self):
        self.active_connections: List[Dict[str, Any]] = []
        self._lib_dirty = True
        self._lib_cache: dict = {}
        # Serializes the off-loop library snapshot refresh so two concurrent
        # broadcasts can't both fire the heavy DB read or race on _lib_cache.
        self._lib_lock = asyncio.Lock()
        # Cached copyright base string — valid until the current song changes.
        # Avoids a DB query per output on every nav broadcast.
        self._copyright_base: Optional[str] = None
        self._copyright_song_id: Optional[int] = None
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
        self._copyright_song_id = None

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
        currently-displayed verse active. Returns '' when the indicator is disabled."""
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
        for i, code in enumerate(oc.verse_codes):
            c = code.upper() if code else '?'
            if i == curr_v_idx:
                parts.append(f'<span class="ind-item active">{c}</span>')
            else:
                parts.append(f'<span class="ind-item">{c}</span>')
        return "".join(parts)

    def _refresh_copyright_base(self):
        """Refresh the cached copyright base if the copyright song changed.

        Fetched once per full broadcast (not once per output); nav broadcasts
        reuse the cached value via self._copyright_base.
        """
        cr_song_id = _get_copyright_song_id()
        if cr_song_id != self._copyright_song_id:
            cr_song = APP_STATE.db.get_song(cr_song_id) if cr_song_id else None
            self._copyright_base = _build_copyright_base(cr_song)
            self._copyright_song_id = cr_song_id

    @staticmethod
    def _resolve_broadcast_content() -> dict:
        """Resolve the content shared by every output this broadcast cycle.

        Mode, the active service item, and the mode-specific media fields do not
        depend on the individual output, so they're resolved once here (via the
        per-mode resolver chain) rather than re-derived inside the per-output loop.
        """
        mode = APP_STATE.current_mode
        item = APP_STATE.current_service_item() if mode == 'service' else None
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
            resolve(content, item)

        content['is_template_ann'] = (
            content['mode'] == 'announcement' or
            (content['mode'] == 'service' and item and item.get('item_type') == 'announcement')
        )
        # transition_key distinguishes content types for hard-cut logic in the output.
        # Service songs and announcements both have mode='service', so we use item_type
        # directly to tell the output which kind of content is being shown.
        content['transition_key'] = item.get('item_type', content['mode']) if item else content['mode']
        return content

    def _build_output_packets(self) -> dict:
        """Build full per-output state_update packets. Returns dict keyed by output name."""
        preload_images = APP_STATE.theme_resolver._collect_service_background_images()
        preload_videos = APP_STATE._collect_service_video_urls()

        self._refresh_copyright_base()
        copyright_base = self._copyright_base or ''
        content = self._resolve_broadcast_content()

        # A frozen output is served its stored snapshot rather than live content, so
        # the held frame is unaffected by anything staged behind it. _refresh_freeze_-
        # snapshots guarantees a frozen output always has a snapshot; the fallback keeps
        # us safe (serve live) if one is somehow missing.
        packets = {}
        for oc in APP_STATE.outputs:
            snap = self._frozen_snapshots.get(oc.name) if self._output_is_frozen(oc) else None
            packets[oc.name] = snap if snap is not None else self._build_full_packet(
                oc, content, copyright_base, preload_images, preload_videos)
        return packets

    @staticmethod
    def _output_is_blank(oc) -> bool:
        """Whether this output should render blank: its own blank toggle, or the global
        blank unless it's exempt. Single source of truth — both the full and nav packet
        builders use it so blank semantics can't drift between the two paths."""
        return oc.is_blank or (APP_STATE.is_blank and not oc.exempt_from_global_blank)

    @staticmethod
    def _output_is_frozen(oc) -> bool:
        """Whether this output should hold its frozen frame: its own freeze toggle, or
        the global freeze unless it's exempt. Mirrors _output_is_blank so freeze and
        blank resolve their global/per-output/exempt precedence identically."""
        return oc.is_frozen or (APP_STATE.is_frozen and not oc.exempt_from_global_freeze)

    def _resolve_visible_fields(self, oc, content, copyright_base):
        """Resolve one output's html, effective style, indicator, copyright and blank
        flag — applying blank masking and template-announcement copyright suppression.
        Shared by the full and nav packet builders so the two can't drift.

        Returns (html, eff_style, indicator_html, copyright_info, output_is_blank).
        """
        h = oc.slides[oc.index] if oc.slides and 0 <= oc.index < len(oc.slides) else ''
        # show_indicator is a themed value; read it from the resolved effective
        # style rather than oc (oc has been restored to base defaults by now).
        eff_style = (APP_STATE.effective_output_styles or {}).get(oc.name) or oc.style_to_dict()
        indicator_html = self._build_indicator_html(oc, eff_style)
        copyright_info = _build_copyright_info(oc, copyright_base)
        output_is_blank = self._output_is_blank(oc)

        if output_is_blank:
            h = ''
            indicator_html = ''
            copyright_info = ''
        if content['is_template_ann']:
            copyright_info = ''
        return h, eff_style, indicator_html, copyright_info, output_is_blank

    def _build_full_packet(self, oc, content, copyright_base, preload_images, preload_videos) -> dict:
        """Build one output's full state_update packet from the shared broadcast content."""
        h, eff_style, indicator_html, copyright_info, output_is_blank = \
            self._resolve_visible_fields(oc, content, copyright_base)

        pkt = {
            'type': 'state_update',
            'html': h,
            'indicator': indicator_html,
            'index': oc.index,
            'mode': content['mode'],
            'transition_key': content['transition_key'],
            # bible_ref is shared content, blanked per-output below.
            'bible_ref': '' if output_is_blank else content['bible_ref'],
            'copyright_info': copyright_info,
            'style': eff_style,
            'font_css': (APP_STATE.bundled_font_css_map or {}).get(oc.name, '') if APP_STATE.bundle_local_fonts else '',
            'preload_images': preload_images,
            'preload_videos': preload_videos,
            'is_blank': output_is_blank,
            'frozen': self._output_is_frozen(oc),
            'preview_video_mode': APP_STATE.preview_video_mode,
            'hold_frame': APP_STATE.video_pending,
        }
        self._add_media_fields(pkt, oc, content, output_is_blank)
        return pkt

    @staticmethod
    def _add_media_fields(pkt, oc, content, output_is_blank):
        """Attach the mode-specific media/template fields to an output packet."""
        mode = content['mode']
        if mode == 'video':
            pkt['video_url'] = None if output_is_blank else content['video_url']
            pkt['video_loop'] = content['video_loop']
            pkt['video_autoplay'] = content['video_autoplay'] and not output_is_blank
            pkt['video_position'] = APP_STATE._get_video_position()
        elif mode == 'image':
            pkt['image_url'] = None if output_is_blank else content['image_url']
            img_data = APP_STATE.current_image_data
            pkt['image_index'] = img_data.get('index', 0)
            pkt['image_count'] = len(img_data.get('images', []))

        if content['is_template_ann']:
            pkt['template_html'] = ('' if output_is_blank else getattr(oc, 'template_html', '')) or ''
        else:
            pkt['template_html'] = ''

    def _build_nav_output_packets(self) -> dict:
        """Build minimal per-output packets for next/prev navigation.

        Skips style, font_css, preload_images, preload_videos, mode, bible_ref,
        preview_video_mode, and hold_frame — none of these change during slide navigation.
        Reuses the copyright base cached by the last _build_output_packets call.
        """
        copyright_base = self._copyright_base or ''
        content = self._resolve_broadcast_content()
        is_template_ann = content['is_template_ann']
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

            h, _eff_style, indicator_html, copyright_info, output_is_blank = \
                self._resolve_visible_fields(oc, content, copyright_base)

            pkt = {
                'type': 'state_update',
                'html': h,
                'indicator': indicator_html,
                'index': oc.index,
                'copyright_info': copyright_info,
                'is_blank': output_is_blank,
                'frozen': False,
                'template_html': ('' if output_is_blank else getattr(oc, 'template_html', '')) if is_template_ann else '',
            }

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
            'ann_templates': APP_STATE.db.get_ann_templates(),
            'announcements': APP_STATE.db.get_all_announcements(),
            'bibles': APP_STATE.db.get_bibles(),
            'services': APP_STATE.db.get_all_services(),
            'service_groups': APP_STATE.db.get_service_groups(),
            'image_display_names': APP_STATE.db.get_image_display_names(),
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

    def _build_admin_state(self, include_songs: bool = False) -> dict:
        """Build the full admin state payload from the already-populated library cache.

        Pure in-memory assembly — the DB snapshot is refreshed separately by
        _ensure_library_cache (which callers await first). The song summary (the
        heaviest part — thousands of rows) is only included when `include_songs` is set
        (library changed, or a brand-new client / explicit full fetch). The client keeps
        its cached `allSongs` and skips the full re-render when the key is absent.
        """
        state = {
                'ann_templates': self._lib_cache['ann_templates'],
                'announcements': self._lib_cache['announcements'],
                'bibles': self._lib_cache['bibles'],
                'image_display_names': self._lib_cache['image_display_names'],
                'current_mode': APP_STATE.current_mode,
                'current_bible_data': APP_STATE.current_bible_data,
                'current_announcement_data': APP_STATE.current_announcement_data,
                'services': self._lib_cache['services'],
                'service_groups': self._lib_cache['service_groups'],
                'current_service_id': APP_STATE.current_service_id,
                'current_service_items': APP_STATE.current_service_items,
                'current_item_index': APP_STATE.current_item_index,
                'bundle_local_fonts': bool(APP_STATE.bundle_local_fonts),
                'ccli_licence_number': APP_STATE.ccli_licence_number,
                'preview_video_mode': APP_STATE.preview_video_mode,
                'current_image_data': APP_STATE.current_image_data,
                'outputs': [{
                    **oc.to_dict(),
                    'slides': oc.slides,
                    'index': oc.index,
                    'line_to_slide': oc.line_to_slide,
                    'is_blank': oc.is_blank,
                    'is_frozen': oc.is_frozen,
                    'is_ignored': oc.is_ignored,
                    'template_html': getattr(oc, 'template_html', None),
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
        try:
            await ws.send_json(self._build_admin_state(include_songs))
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

    async def broadcast_state(self):
        """Broadcast full state to all clients."""
        include_songs = await self._ensure_library_cache()
        await self._fan_out(self._build_admin_state(include_songs), self._build_output_packets())

    async def broadcast_library_state(self):
        """Admin-only broadcast for library/service metadata changes that don't alter
        what's rendered on the output displays (service rename, reorder, regrouping).

        Skips the per-output packet rebuild and the output fan-out entirely — those
        re-collect backgrounds/videos and re-resolve every output's HTML for content
        that didn't change. Only admin clients receive the refreshed state.
        """
        include_songs = await self._ensure_library_cache()
        msg = self._build_admin_state(include_songs)
        sends = [self._safe_send(c["ws"], msg)
                 for c in self.active_connections if c["type"] == 'admin']
        if sends:
            await asyncio.gather(*sends)

    async def broadcast_nav_state(self):
        """Lightweight broadcast for next/prev/jump — no DB queries, no full re-render."""
        admin_nav = {
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
        await self._fan_out(admin_nav, self._build_nav_output_packets())

    async def broadcast_blank_state(self):
        """Lightweight broadcast for blank/unblank — no DB queries, no library data.

        Sends only the blank flags to the admin and the updated output HTML to
        output clients (via the existing nav output packets which already apply
        blank masking).
        """
        admin_blank = {
            'type': 'state_blank',
            'is_blank': APP_STATE.is_blank,
            'outputs': [{
                'name': oc.name,
                'is_blank': oc.is_blank,
                'exempt_from_global_blank': oc.exempt_from_global_blank,
            } for oc in APP_STATE.outputs],
        }
        await self._fan_out(admin_blank, self._build_nav_output_packets())

    async def broadcast_freeze_state(self):
        """Broadcast for freeze/unfreeze — no DB queries, no library data.

        Sends the freeze flags to admin clients and the resolved output packets to
        output clients. Full packets (not the lighter nav packets) are used so an
        output that is *unfreezing* repaints its complete live state — mode, style and
        media — in a single message, while an output that is *freezing* receives its
        held snapshot. Call _refresh_freeze_snapshots() before this so snapshots are
        in sync with the new freeze state.
        """
        admin_freeze = {
            'type': 'state_freeze',
            'is_frozen': APP_STATE.is_frozen,
            'outputs': [{
                'name': oc.name,
                'is_frozen': oc.is_frozen,
                'exempt_from_global_freeze': oc.exempt_from_global_freeze,
            } for oc in APP_STATE.outputs],
        }
        await self._fan_out(admin_freeze, self._build_output_packets())

manager = ConnectionManager()


# ---------------------- FastAPI App ----------------------

app = FastAPI(title="SeventhSlide")

# Mount static files for exported outputs
app.mount("/static", StaticFiles(directory=APP_STATE.export_dir), name="static")

# Allow CORS (credentials=False required when origins="*"; this app uses no cookies)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serializes slide rebuilds and HTML exports. Both run in a worker thread (via
# asyncio.to_thread) and mutate shared, process-global state: _rebuild_slides_and_mappings
# temporarily applies and then restores each OutputConfig's style in place, and the
# exporter reads those same styles. Without a lock, two near-simultaneous requests
# (a double-click, two admin tabs) start two worker threads that interleave the
# apply→work→restore dance on the same objects and corrupt the rendered output. It
# also guards the process-wide PIL font cache, whose font objects are not thread-safe.
_render_lock = asyncio.Lock()

async def _rebuild_slides():
    """Rebuild all output slides under the render lock (off the event loop)."""
    async with _render_lock:
        await asyncio.to_thread(APP_STATE.slide_builder._rebuild_slides_and_mappings)

async def _export_outputs():
    """Re-export output HTML under the render lock (off the event loop)."""
    async with _render_lock:
        await asyncio.to_thread(APP_STATE.exporter.export_outputs)

def _refresh_current_service_items(prev_item_id):
    """Reload the active service's items and reconcile the live selection by identity.

    `prev_item_id` is the live item's id captured *before* the mutation (via
    APP_STATE.active_item_id()). Tracking the selection by id rather than list position
    means adding / removing / reordering other items can't silently change which item is
    live. If the live item was itself removed, the live display is cleared.

    Returns True if the previously-live item is gone — callers that re-render on content
    change should skip their rebuild in that case (the display has already been cleared).
    No-op when no service is selected.
    """
    if APP_STATE.current_service_id == -1:
        return False
    APP_STATE.current_service_items = APP_STATE.db.get_service_items(APP_STATE.current_service_id)
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
    # Serve generated HTML files for outputs from the export root
    # e.g. /Main.html
    #
    # Starlette's {filename} converter won't match '/', so path traversal isn't
    # reachable today — but resolve and contain the path anyway so this can't
    # regress into serving arbitrary files if the route or framework changes.
    export_root = os.path.realpath(APP_STATE.export_dir)
    fpath = os.path.realpath(os.path.join(export_root, filename))
    if os.path.commonpath([export_root, fpath]) != export_root:
        raise HTTPException(status_code=404, detail="File not found")
    if os.path.isfile(fpath):
        return FileResponse(fpath)
    # Also valid for main index.html if we had one
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
    vd = APP_STATE.current_video_data
    if (APP_STATE.video_pending
            and vd.get('filename') == expected_filename):
        APP_STATE.video_pending = False
        APP_STATE.video_is_playing = True
        APP_STATE.video_start_position = 0.0
        APP_STATE.video_start_time = time.time()
        await manager.broadcast_state()

# --- API Endpoints ---

@app.post("/api/app-settings")
async def api_app_settings(data: dict = Body(...)):
    bundle = bool(data.get('bundle_local_fonts', False))
    APP_STATE.bundle_local_fonts = bundle
    new_ccli = data.get('ccli_licence_number', APP_STATE.ccli_licence_number)
    if new_ccli != APP_STATE.ccli_licence_number:
        # The CCLI number is part of the copyright base string; clear the cache so
        # the live output picks up the new value instead of a stale rebuild.
        manager.invalidate_copyright_cache()
    APP_STATE.ccli_licence_number = new_ccli
    pvm = data.get('preview_video_mode', 'still')
    if pvm in ('disabled', 'still', 'live'):
        APP_STATE.preview_video_mode = pvm
    APP_STATE.config_manager.save_config()
    # (Re)export to ensure fonts are copied and bundled CSS is built.
    await _export_outputs()
    await _rebuild_slides()
    await manager.broadcast_state()
    return {"success": True}

@app.get("/api/state")
async def api_get_state():
    include_songs = await manager._ensure_library_cache(force=True)
    return manager._build_admin_state(include_songs)['state']

def _current_is_image_mode() -> bool:
    return APP_STATE.effective_content_type() == 'image'

@app.post("/api/next")
async def api_next():
    if _current_is_image_mode():
        img_data = APP_STATE.current_image_data
        images = img_data.get('images', [])
        if images and img_data.get('index', 0) < len(images) - 1:
            APP_STATE.current_image_data['index'] = img_data['index'] + 1
            await manager.broadcast_state()
        return {"success": True}
    changed = APP_STATE.player.next_slide()
    if changed:
        await manager.broadcast_nav_state()
    return {"success": True}

@app.post("/api/toggle-blank")
async def api_toggle_blank():
    APP_STATE.is_blank = not APP_STATE.is_blank
    await manager.broadcast_blank_state()
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
    
    oc.is_blank = not oc.is_blank
    await manager.broadcast_blank_state()
    return {"success": True, "is_blank": oc.is_blank}

@app.post("/api/toggle-freeze")
async def api_toggle_freeze():
    """Toggle global freeze. Holds every non-exempt output on its current frame; the
    operator can stage the next item behind it, and unfreezing reveals the live state."""
    APP_STATE.is_frozen = not APP_STATE.is_frozen
    manager._refresh_freeze_snapshots()
    await manager.broadcast_freeze_state()
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

    oc.is_frozen = not oc.is_frozen
    manager._refresh_freeze_snapshots()
    await manager.broadcast_freeze_state()
    return {"success": True, "is_frozen": oc.is_frozen}

@app.post("/api/toggle-output-ignore")
async def api_toggle_output_ignore(data: dict = Body(...)):
    """Toggle ignore state for a specific output.

    When ignored, an output is excluded from slide pagination on the next item
    load and its content is blanked. Unignoring takes effect on the next item load.
    """
    output_name = data.get('name')
    if not output_name:
        return {"success": False, "message": "Output name required"}

    oc = APP_STATE.get_output(output_name)
    if not oc:
        return {"success": False, "message": "Output not found"}

    oc.is_ignored = not oc.is_ignored
    # Rebuild slides so the change takes effect immediately for the current item
    await _rebuild_slides()
    await manager.broadcast_state()
    return {"success": True, "is_ignored": oc.is_ignored}

@app.post("/api/prev")
async def api_prev():
    if _current_is_image_mode():
        img_data = APP_STATE.current_image_data
        images = img_data.get('images', [])
        if images and img_data.get('index', 0) > 0:
            APP_STATE.current_image_data['index'] = img_data['index'] - 1
            await manager.broadcast_state()
        return {"success": True}
    changed = APP_STATE.player.prev_slide()
    if changed:
        await manager.broadcast_nav_state()
    return {"success": True}

@app.post("/api/jump-to-line")
async def api_jump(data: dict = Body(...)):
    success = APP_STATE.player.jump_to_line(data.get('line_index', 0))
    if success:
        await manager.broadcast_nav_state()
    return {"success": success}

@app.post("/api/services/select-item")
async def api_select_item(data: dict = Body(...)):
    idx = data.get('index')
    if 0 <= idx < len(APP_STATE.current_service_items):
        APP_STATE.current_mode = 'service'
        APP_STATE.current_item_index = idx
        item = APP_STATE.current_service_items[idx]
        APP_STATE.current_song_id = item.get('song_id')
        APP_STATE.current_song_title = item.get('title') or ''
        APP_STATE.current_song_lyrics = item.get('lyrics') or ''
        await _rebuild_slides()
        is_video = item.get('item_type') == 'video'
        is_image_folder = item.get('item_type') == 'image_folder'
        is_image = item.get('item_type') == 'image'
        # Apply requested image index for folder items
        image_index = data.get('image_index')
        if is_image_folder and image_index is not None:
            images = APP_STATE.current_image_data.get('images', [])
            if images:
                APP_STATE.current_image_data['index'] = max(0, min(int(image_index), len(images) - 1))
        _cancel_pending_video_task()
        if is_video:
            filename = APP_STATE.current_video_data.get('filename', '')
            wants_autoplay = APP_STATE.current_video_data.get('autoplay', True)
            APP_STATE.video_is_playing = False
            if wants_autoplay and filename:
                APP_STATE.video_pending = True
                APP_STATE.pending_video_task = asyncio.create_task(_delayed_video_play(filename, is_preloaded=True))
        await manager.broadcast_state()
    return {"success": True}

@app.post("/api/generate-slides")
async def api_gen_slides():
    await _rebuild_slides()
    await manager.broadcast_state()
    return {"success": True}

@app.post("/api/select-video")
async def api_select_video(data: dict = Body(...)):
    filename = data.get('filename')
    if not filename:
        return {"success": False, "message": "Filename required"}
    autoplay = bool(data.get('autoplay', True))
    APP_STATE.current_mode = 'video'
    APP_STATE.current_video_data = {
        'filename': filename,
        'title': data.get('title') or filename,
        'loop': bool(data.get('loop', False)),
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
    await manager.broadcast_state()
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

    APP_STATE.db.update_service_theme_map(sid_int, theme_map)
    await _rebuild_slides()
    await manager.broadcast_state()
    return {"success": True}

@app.post("/api/select-song")
async def api_select_song(data: dict = Body(...)):
    _cancel_pending_video_task()
    song_id = data.get('id')
    song = APP_STATE.db.get_song(song_id)
    if song:
        APP_STATE.current_mode = 'song'
        APP_STATE.current_song_id = song_id
        APP_STATE.current_song_title = song['title']
        APP_STATE.current_song_lyrics = song['lyrics']
        APP_STATE.current_song_verse_order = song.get('verse_order')
        APP_STATE.current_item_index = -1
        await _rebuild_slides()
        await manager.broadcast_state()
    return {"success": True}

# --- Service Helper Wrappers ---

@app.post("/api/services/create")
async def api_service_create(data: dict = Body(...)):
    new_id = APP_STATE.db.create_service(data.get('name', 'New Service'), data.get('group_id'))
    # Auto-select the newly created service
    items = APP_STATE.db.get_service_items(new_id)
    APP_STATE.current_service_id = new_id
    APP_STATE.current_service_items = items
    APP_STATE.current_mode = 'service'
    APP_STATE.current_item_index = -1
    APP_STATE.clear_live_item()
    manager.invalidate_library_cache()
    await manager.broadcast_state()
    return {"success": True}

@app.post("/api/services/delete")
async def api_service_delete(data: dict = Body(...)):
    sid = data.get('id')
    APP_STATE.db.delete_service(sid)
    if APP_STATE.current_service_id == sid:
        APP_STATE.current_service_id = -1
        APP_STATE.current_service_items = []
    manager.invalidate_library_cache()
    await manager.broadcast_state()
    return {"success": True}

@app.post("/api/services/rename")
async def api_service_rename(data: dict = Body(...)):
    sid = data.get('id')
    new_name = data.get('name', '').strip()
    if not new_name:
        return {"success": False, "message": "Name cannot be empty"}
    APP_STATE.db.rename_service(sid, new_name)
    manager.invalidate_library_cache()
    await manager.broadcast_library_state()
    return {"success": True}

@app.post("/api/services/select")
async def api_service_select(data: dict = Body(...)):
    sid = data.get('id')
    items = APP_STATE.db.get_service_items(sid)
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
    await _rebuild_slides()
    await manager.broadcast_state()
    return {"success": True}

@app.post("/api/services/deselect")
async def api_service_deselect():
    APP_STATE.current_service_id = -1
    APP_STATE.current_service_items = []
    APP_STATE.current_item_index = -1
    APP_STATE.current_mode = 'song'
    APP_STATE.clear_live_item()
    await manager.broadcast_state()
    return {"success": True}

# --- Service groups (one-level organization of services) ---
@app.post("/api/service-groups/create")
async def api_service_group_create(data: dict = Body(...)):
    name = (data.get('name') or '').strip()
    if not name:
        return {"success": False, "message": "Name required"}
    gid = APP_STATE.db.create_service_group(name)
    manager.invalidate_library_cache()
    await manager.broadcast_library_state()
    return {"success": True, "id": gid}

@app.post("/api/service-groups/rename")
async def api_service_group_rename(data: dict = Body(...)):
    gid = data.get('id')
    name = (data.get('name') or '').strip()
    if not gid or not name:
        return {"success": False, "message": "id and name required"}
    APP_STATE.db.rename_service_group(gid, name)
    manager.invalidate_library_cache()
    await manager.broadcast_library_state()
    return {"success": True}

@app.post("/api/service-groups/delete")
async def api_service_group_delete(data: dict = Body(...)):
    """Delete a group; its services are kept and returned to the ungrouped list."""
    gid = data.get('id')
    if not gid:
        return {"success": False, "message": "id required"}
    APP_STATE.db.delete_service_group(gid)
    manager.invalidate_library_cache()
    await manager.broadcast_library_state()
    return {"success": True}

@app.post("/api/services/move")
async def api_service_move(data: dict = Body(...)):
    """Move a service into a group (group_id None = ungrouped) and optionally reorder the
    destination bucket via ordered_ids."""
    sid = data.get('id')
    if not sid:
        return {"success": False, "message": "id required"}
    APP_STATE.db.move_service_to_group(sid, data.get('group_id'), data.get('ordered_ids'))
    manager.invalidate_library_cache()
    await manager.broadcast_library_state()
    return {"success": True}

@app.post("/api/services/reorder")
async def api_service_reorder(data: dict = Body(...)):
    APP_STATE.db.reorder_services(data.get('ordered_ids') or [])
    manager.invalidate_library_cache()
    await manager.broadcast_library_state()
    return {"success": True}

@app.post("/api/services/add-song")
async def api_service_add_song(data: dict = Body(...)):
    sid = APP_STATE.current_service_id
    if sid != -1:
        prev_item_id = APP_STATE.active_item_id()
        APP_STATE.db.add_song_to_service(sid, data.get('song_id'))
        _refresh_current_service_items(prev_item_id)
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
    APP_STATE.db.reorder_service_items(sid, ordered_ids)
    # Reorder never removes items, so the live item simply moves to a new index.
    _refresh_current_service_items(prev_item_id)
    manager.invalidate_library_cache()
    await manager.broadcast_state()
    return {"success": True}

@app.post("/api/services/remove-item")
async def api_service_remove_item(data: dict = Body(...)):
    prev_item_id = APP_STATE.active_item_id()
    APP_STATE.db.remove_item_from_service(data.get('item_id'))
    APP_STATE.db.cleanup_orphan_hidden_images(os.path.join(APP_STATE.export_dir, 'images'))
    if APP_STATE.current_service_id != -1:
        # Reconcile by id: the live item keeps its slides if it survived (only its
        # position shifted), or the display is cleared if it was the one removed.
        _refresh_current_service_items(prev_item_id)
        manager.invalidate_library_cache()
        await manager.broadcast_state()
    return {"success": True}

@app.post("/api/services/remove-items")
async def api_service_remove_items(data: dict = Body(...)):
    """Bulk delete service items (one DB transaction + one broadcast)."""
    ids = data.get('item_ids') or []
    if not ids:
        return {"success": False, "message": "item_ids required"}
    prev_item_id = APP_STATE.active_item_id()
    deleted = APP_STATE.db.remove_items_from_service(ids)
    APP_STATE.db.cleanup_orphan_hidden_images(os.path.join(APP_STATE.export_dir, 'images'))
    if APP_STATE.current_service_id != -1:
        _refresh_current_service_items(prev_item_id)
        manager.invalidate_library_cache()
        await manager.broadcast_state()
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
    added = APP_STATE.db.add_songs_to_service(sid, ids)
    _refresh_current_service_items(prev_item_id)
    manager.invalidate_library_cache()
    # Appending songs changes neither the live item nor any output's preload media.
    await manager.broadcast_library_state()
    return {"success": True, "added": added}

@app.post("/api/services/update-item")
async def api_service_update_item(data: dict = Body(...)):
    item_id = data.get('item_id')
    if not item_id:
        return {"success": False, "message": "Missing item_id"}

    # Read current item to know its type, existing data, and song_id
    with APP_STATE.db._db_transaction(commit=False) as cur:
        cur.execute("SELECT item_type, data, song_id FROM service_items WHERE id = ?", (item_id,))
        row = cur.fetchone()
    if not row:
        return {"success": False, "message": "Item not found"}

    existing_data = APP_STATE.db._parse_json_field(row['data'], {})
    new_data = APP_STATE.db.compute_updated_service_item_data(
        data, row['item_type'], existing_data, row['song_id'])

    prev_item_id = APP_STATE.active_item_id()
    APP_STATE.db.update_service_item(item_id, new_data)

    # Refresh service items and rebuild slides (themes affect output rendering)
    if APP_STATE.current_service_id != -1:
        _refresh_current_service_items(prev_item_id)
        # Update live song data if the edited item is currently active
        item = APP_STATE.current_service_item()
        if item is not None and item.get('item_id') == item_id:
            APP_STATE.current_song_title = item.get('title', '')
            APP_STATE.current_song_lyrics = item.get('lyrics', '')
    await _rebuild_slides()
    manager.invalidate_library_cache()
    await manager.broadcast_state()
    return {"success": True}

# --- Song Editing ---

@app.post("/api/songs/create")
async def api_song_create(data: dict = Body(...)):
    title = data.get('title')
    lyrics = data.get('lyrics')
    verse_order = data.get('verse_order')
    authors = data.get('authors', [])
    songbook_name = data.get('songbook_name', '')
    songbook_entry = data.get('songbook_entry', '')
    copyright_text = data.get('copyright', '')
    ccli_song_number = data.get('ccli_song_number', '')
    key = data.get('key', '')
    show_copyright = data.get('show_copyright', False)
    theme_map = data.get('theme_map') or {}
    if not isinstance(theme_map, dict):
        theme_map = {}

    if not title or not lyrics:
        return JSONResponse({'success': False, 'message': 'Title and lyrics required'})

    lyrics_error = _validate_no_blank_lines_in_verse(lyrics)
    if lyrics_error:
        return JSONResponse({'success': False, 'message': lyrics_error})

    APP_STATE.db.add_song(title, lyrics, verse_order, authors, songbook_name, songbook_entry, theme_map, copyright_text, ccli_song_number, show_copyright, key)
    manager.invalidate_library_cache()
    await manager.broadcast_state()
    return {"success": True}

@app.post("/api/songs/update")
async def api_song_update(data: dict = Body(...)):
    song_id = data.get('id')
    title = data.get('title')
    lyrics = data.get('lyrics')
    verse_order = data.get('verse_order')
    authors = data.get('authors', [])
    songbook_name = data.get('songbook_name', '')
    songbook_entry = data.get('songbook_entry', '')
    copyright_text = data.get('copyright', '')
    ccli_song_number = data.get('ccli_song_number', '')
    key = data.get('key', '')
    show_copyright = data.get('show_copyright', False)
    theme_map = data.get('theme_map') or {}
    if not isinstance(theme_map, dict):
        theme_map = {}

    # Validation
    if lyrics:
        lyrics_error = _validate_no_blank_lines_in_verse(lyrics)
        if lyrics_error:
            return JSONResponse({'success': False, 'message': lyrics_error})

    order_error = _validate_verse_order(verse_order, lyrics)
    if order_error:
        return JSONResponse({'success': False, 'message': order_error})

    if song_id and title:
        APP_STATE.db.update_song(song_id, title, lyrics, verse_order, authors, songbook_name, songbook_entry, theme_map, copyright_text, ccli_song_number, show_copyright, key)
        
        is_live = False
        if APP_STATE.current_mode == 'song' and APP_STATE.current_song_id == song_id:
            is_live = True
        elif APP_STATE.current_mode == 'service':
            curr = APP_STATE.current_service_item()
            if curr is not None and curr.get('song_id') == song_id:
                curr['title'] = title
                curr['lyrics'] = lyrics
                curr['verse_order'] = verse_order
                is_live = True

        if is_live:
            APP_STATE.current_song_title = title
            APP_STATE.current_song_lyrics = lyrics
            APP_STATE.current_song_verse_order = verse_order
            await _rebuild_slides()

        manager.invalidate_library_cache()
        # The copyright base (authors / copyright text / show_copyright) is cached
        # keyed only on song id, so an edit to the live song wouldn't otherwise show.
        manager.invalidate_copyright_cache()
        await manager.broadcast_state()

    return {'success': True}

@app.post("/api/songs/delete")
async def api_song_delete(data: dict = Body(...)):
    APP_STATE.db.delete_song(data.get('id'))
    manager.invalidate_library_cache()
    await manager.broadcast_state()
    return {"success": True}

@app.post("/api/songs/delete-many")
async def api_songs_delete_many(data: dict = Body(...)):
    ids = data.get('ids') or []
    if not ids:
        return {"success": False, "message": "ids required"}
    deleted = APP_STATE.db.delete_songs(ids)
    manager.invalidate_library_cache()
    await manager.broadcast_state()
    return {"success": True, "deleted": deleted}

@app.get("/api/songs/{song_id}")
async def api_song_get(song_id: int):
    song = APP_STATE.db.get_song(song_id)
    if not song:
        raise HTTPException(status_code=404, detail="Song not found")
    return song

# --- Announcement Library API ---

@app.get("/api/announcements")
async def api_get_announcements():
    return {"announcements": APP_STATE.db.get_all_announcements()}

@app.post("/api/announcements/create")
async def api_create_announcement(data: dict = Body(...)):
    template_id = data.get('template_id')
    if not template_id:
        return {"success": False, "message": "template_id required"}
    field_values = data.get('field_values', [])
    title = data.get('title', '').strip() or (field_values[0] if field_values else 'Announcement')
    theme_map = data.get('theme_map') or {}
    aid = APP_STATE.db.create_announcement(template_id, title, field_values, theme_map)
    manager.invalidate_library_cache()
    await manager.broadcast_state()
    return {"success": True, "id": aid}

@app.post("/api/announcements/update")
async def api_update_announcement(data: dict = Body(...)):
    aid = data.get('id')
    if not aid:
        return {"success": False, "message": "id required"}
    field_values = data.get('field_values', [])
    title = data.get('title', '').strip() or (field_values[0] if field_values else 'Announcement')
    theme_map = data.get('theme_map')
    APP_STATE.db.update_announcement(aid, title, field_values, theme_map)
    manager.invalidate_library_cache()
    await _rebuild_slides()
    await manager.broadcast_state()
    return {"success": True}

@app.post("/api/announcements/delete")
async def api_delete_announcement(data: dict = Body(...)):
    aid = data.get('id')
    if aid:
        APP_STATE.db.delete_announcement(aid)
        manager.invalidate_library_cache()
        await manager.broadcast_state()
    return {"success": True}

@app.post("/api/announcements/select")
async def api_select_announcement(data: dict = Body(...)):
    aid = data.get('id')
    ann = APP_STATE.db.get_announcement(aid) if aid else None
    if not ann:
        raise HTTPException(status_code=404, detail="Announcement not found")
    APP_STATE.current_mode = 'announcement'
    # ThemeResolver._get_current_song_theme_map() reads the announcement's theme_map via
    # current_song_id in announcement mode, so set it here — otherwise it stays None (or
    # a stale song id) and the per-output background theme override is silently dropped,
    # falling back to the output default.
    APP_STATE.current_song_id = ann['id']
    APP_STATE.current_announcement_data = {
        'id': ann['id'],
        'template_id': ann['template_id'],
        'field_values': ann['field_values'],
        'title': ann['title'],
        'template_name': ann.get('template_name', ''),
        'field_names': ann.get('field_names', []),
    }
    await _rebuild_slides()
    await manager.broadcast_state()
    return {"success": True}

# --- Announcement Templates API ---

@app.get("/api/ann-templates")
async def api_get_ann_templates():
    return {"templates": APP_STATE.db.get_ann_templates()}

@app.post("/api/ann-templates")
async def api_create_ann_template(data: dict = Body(...)):
    name = data.get('name', '').strip()
    field_names = data.get('field_names', [])
    if not name or not field_names:
        return {"success": False, "message": "Name and at least one field required"}
    tid = APP_STATE.db.create_ann_template(name, field_names)
    manager.invalidate_library_cache()
    await manager.broadcast_state()
    return {"success": True, "id": tid}

@app.post("/api/ann-templates/rename")
async def api_rename_ann_template(data: dict = Body(...)):
    tid = data.get('id')
    name = data.get('name', '').strip()
    if not tid or not name:
        return {"success": False, "message": "id and name required"}
    APP_STATE.db.update_ann_template(tid, name)
    manager.invalidate_library_cache()
    await manager.broadcast_state()
    return {"success": True}

@app.post("/api/ann-templates/delete")
async def api_delete_ann_template(data: dict = Body(...)):
    tid = data.get('id')
    if tid:
        APP_STATE.db.delete_ann_template(tid)
        manager.invalidate_library_cache()
        await manager.broadcast_state()
    return {"success": True}

@app.post("/api/ann-template-layouts/save")
async def api_save_ann_template_layout(data: dict = Body(...)):
    template_id = data.get('template_id')
    output_name = data.get('output_name', '')
    if not template_id or not output_name:
        return {"success": False, "message": "template_id and output_name required"}
    APP_STATE.db.upsert_ann_template_layout(
        template_id, output_name,
        data.get('background_type', 'color'),
        data.get('background_value', '#000000'),
        data.get('text_boxes', []),
    )
    manager.invalidate_library_cache()
    await manager.broadcast_state()
    return {"success": True}

@app.post("/api/ann-template-layouts/delete")
async def api_delete_ann_template_layout(data: dict = Body(...)):
    template_id = data.get('template_id')
    output_name = data.get('output_name', '')
    if template_id and output_name:
        APP_STATE.db.delete_ann_template_layout(template_id, output_name)
        manager.invalidate_library_cache()
        await manager.broadcast_state()
    return {"success": True}

@app.get("/api/ann-template-layouts/{template_id}")
async def api_get_ann_template_layouts(template_id: int):
    layouts = APP_STATE.db.get_ann_template_layouts(template_id)
    return {"layouts": layouts}

@app.post("/api/services/add-announcement")
async def api_add_announcement_to_service(data: dict = Body(...)):
    service_id = APP_STATE.current_service_id
    if service_id == -1:
        raise HTTPException(status_code=400, detail="No service selected")
    announcement_id = data.get('announcement_id')
    if announcement_id:
        ann = APP_STATE.db.get_announcement(announcement_id)
        if not ann:
            raise HTTPException(status_code=404, detail="Announcement not found")
        template_id = ann['template_id']
        field_values = ann['field_values']
        title = ann.get('title', '')
    else:
        template_id = data.get('template_id')
        field_values = data.get('field_values', [])
        title = data.get('title', '')
        if not template_id:
            return {"success": False, "message": "announcement_id or template_id required"}
    prev_item_id = APP_STATE.active_item_id()
    APP_STATE.db.add_announcement_to_service(service_id, template_id, field_values, title)
    _refresh_current_service_items(prev_item_id)
    manager.invalidate_library_cache()
    await manager.broadcast_state()
    return {"success": True}

@app.post("/api/services/update-announcement")
async def api_update_announcement_in_service(data: dict = Body(...)):
    item_id = data.get('item_id')
    field_values = data.get('field_values', [])
    if not item_id:
        return {"success": False, "message": "item_id required"}
    # Load existing data to preserve template_id
    service_id = APP_STATE.current_service_id
    items = APP_STATE.current_service_items
    item = next((i for i in items if i.get('item_id') == item_id), None)
    if not item:
        return {"success": False, "message": "Item not found"}
    title = data.get('title', item.get('title', ''))
    theme_map = data.get('theme_map')
    if theme_map is None:
        theme_map = item.get('theme_map') or {}
    new_data = {'template_id': item.get('template_id'), 'field_values': field_values,
                'title': title, 'theme_map': theme_map}
    prev_item_id = APP_STATE.active_item_id()
    APP_STATE.db.update_service_item(item_id, new_data)
    _refresh_current_service_items(prev_item_id)
    await _rebuild_slides()
    await manager.broadcast_state()
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
    ok = APP_STATE.db.move_service_folder_image(from_item_id, from_index, to_item_id, to_index)
    if not ok:
        return {"success": False, "message": "Move failed"}
    active_lost = _refresh_current_service_items(prev_item_id)
    # Rebuild so the active folder's live image list reflects the change (unless the
    # active item itself went away, in which case the display was already cleared).
    if not active_lost:
        await _rebuild_slides()
    await manager.broadcast_state()
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
    ok = APP_STATE.db.move_service_folder_images(selections, to_item_id, to_index)
    if not ok:
        return {"success": False, "message": "Move failed"}
    active_lost = _refresh_current_service_items(prev_item_id)
    if not active_lost:
        await _rebuild_slides()
    await manager.broadcast_state()
    return {"success": True}

@app.post("/api/services/add-video")
async def api_add_video_to_service(data: dict = Body(...)):
    service_id = APP_STATE.current_service_id
    if service_id == -1:
        raise HTTPException(status_code=400, detail="No service selected")
    filename = data.get('filename')
    if not filename:
        return {"success": False, "message": "Filename required"}
    video_data = {
        'filename': filename,
        'title': data.get('title') or filename,
        'loop': bool(data.get('loop', False)),
        'autoplay': bool(data.get('autoplay', True)),
    }
    prev_item_id = APP_STATE.active_item_id()
    APP_STATE.db.add_video_to_service(service_id, video_data)
    _refresh_current_service_items(prev_item_id)
    manager.invalidate_library_cache()
    # broadcast_state (not library_state): a new video item adds to every output's
    # preload_videos list, which output clients need to pre-buffer.
    await manager.broadcast_state()
    return {"success": True}

# Allowed video container extensions, and a hard upload-size cap. Uploads are
# served as static files and stored on the presentation machine's disk, so an
# unbounded or arbitrary-type upload is both a disk-fill risk and a way to drop
# unexpected files into the served directory. 4 GiB covers a long service video.
VIDEO_EXTS = {'.mp4', '.webm', '.ogg', '.mov', '.avi', '.mkv'}
MAX_VIDEO_BYTES = 4 * 1024 * 1024 * 1024


class _UploadTooLarge(Exception):
    """Raised when a streamed upload exceeds its size cap."""


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

@app.get("/api/videos/list")
async def api_video_list():
    videos_dir = os.path.join(APP_STATE.export_dir, 'videos')
    os.makedirs(videos_dir, exist_ok=True)
    files = [n for n in sorted(os.listdir(videos_dir))
             if os.path.splitext(n)[1].lower() in VIDEO_EXTS]
    return {"videos": files}

@app.post("/api/videos/delete")
async def api_video_delete(data: dict = Body(...)):
    filename = os.path.basename(data.get('filename', ''))
    if not filename:
        return {"success": False, "message": "Filename required"}
    # Refuse to delete a video any service still references — otherwise the file
    # vanishes from disk and that service item silently plays nothing.
    refs = APP_STATE.db.video_reference_count(filename)
    if refs > 0:
        return {"success": False,
                "message": f"Video is used by {refs} service item(s); remove it from those services first."}
    path = os.path.join(APP_STATE.export_dir, 'videos', filename)
    if os.path.exists(path):
        os.unlink(path)
    return {"success": True}

@app.post("/api/live/video-control")
async def api_video_control(data: dict = Body(...)):
    action = data.get('action')  # 'play' | 'pause' | 'restart' | 'seek'
    position = data.get('position')
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
        pos = float(position)
        if APP_STATE.video_is_playing:
            APP_STATE.video_start_position = pos
            APP_STATE.video_start_time = now
        else:
            APP_STATE.video_pause_position = pos

    await manager.broadcast_video_command(action, position)
    return {"success": True}

# --- Images ---

def _save_uploaded_image(file: UploadFile) -> tuple:
    """Save an uploaded image under a random on-disk filename and record the
    original (display) name in the image_files table. Returns (filename, display_name).
    Random naming avoids name collisions entirely, so duplicate display names
    (e.g. two 'slide.jpg' files) coexist on disk."""
    images_dir = os.path.join(APP_STATE.export_dir, 'images')
    os.makedirs(images_dir, exist_ok=True)
    display_name = os.path.basename(file.filename or 'image')
    ext = os.path.splitext(display_name)[1].lower()
    filename = uuid.uuid4().hex + ext
    dest = os.path.join(images_dir, filename)
    with open(dest, 'wb') as f:
        shutil.copyfileobj(file.file, f)
    APP_STATE.db.register_image_file(filename, display_name)
    return filename, display_name

@app.post("/api/images/upload")
async def api_image_upload(file: UploadFile = File(...)):
    filename, display_name = await asyncio.to_thread(_save_uploaded_image, file)
    manager.invalidate_library_cache()
    return {"success": True, "filename": filename, "display_name": display_name}

@app.post("/api/images/upload-to-folder")
async def api_image_upload_to_folder(folder_id: int = Form(...), file: UploadFile = File(...)):
    filename, display_name = await asyncio.to_thread(_save_uploaded_image, file)
    APP_STATE.db.add_image_to_folder(folder_id, filename)
    manager.invalidate_library_cache()
    return {"success": True, "filename": filename, "display_name": display_name}

@app.get("/api/images/list")
async def api_image_list():
    images_dir = os.path.join(APP_STATE.export_dir, 'images')
    os.makedirs(images_dir, exist_ok=True)
    exts = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.svg'}
    on_disk = [n for n in sorted(os.listdir(images_dir))
               if os.path.splitext(n)[1].lower() in exts]
    # Skip files the user "deleted" from the library but a service still uses.
    with APP_STATE.db._db_transaction(commit=False) as cur:
        cur.execute("SELECT filename FROM image_files WHERE library_visible = 0")
        hidden = {r['filename'] for r in cur.fetchall()}
    files = [n for n in on_disk if n not in hidden]
    dn = APP_STATE.db.get_image_display_names()
    return {"images": [{"filename": n, "display_name": dn.get(n, n)} for n in files]}

@app.post("/api/images/delete")
async def api_image_delete(data: dict = Body(...)):
    """Delete an image from the LIBRARY. The file stays on disk if any service still
    references it (so the service keeps working); only orphan files are unlinked."""
    filename = os.path.basename(data.get('filename', ''))
    if not filename:
        return {"success": False, "message": "Filename required"}
    images_dir = os.path.join(APP_STATE.export_dir, 'images')
    unlinked, hidden = APP_STATE.db.delete_library_images([filename], images_dir)
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
    unlinked, hidden = APP_STATE.db.delete_library_images(filenames, images_dir)
    manager.invalidate_library_cache()
    await manager.broadcast_state()
    return {"success": True, "unlinked": unlinked, "kept_for_services": hidden}

@app.get("/api/image-folders/list")
async def api_image_folders_list():
    return {"folders": APP_STATE.db.get_image_folders()}

@app.post("/api/image-folders/create")
async def api_image_folder_create(data: dict = Body(...)):
    name = (data.get('name') or 'New Folder').strip()
    if not name:
        return {"success": False, "message": "Name required"}
    parent_id = data.get('parent_id')  # None = top-level folder
    folder_id = APP_STATE.db.create_image_folder(name, parent_id)
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
    ok = APP_STATE.db.move_image_folder(folder_id, parent_id, ordered_ids)
    return {"success": ok}

@app.post("/api/image-folders/rename")
async def api_image_folder_rename(data: dict = Body(...)):
    folder_id = data.get('id')
    name = (data.get('name') or '').strip()
    if not folder_id or not name:
        return {"success": False, "message": "ID and name required"}
    APP_STATE.db.rename_image_folder(folder_id, name)
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
    filenames = APP_STATE.db.delete_image_folder(folder_id)
    unlinked, hidden = 0, 0
    if filenames:
        images_dir = os.path.join(APP_STATE.export_dir, 'images')
        unlinked, hidden = APP_STATE.db.delete_library_images(filenames, images_dir)
    manager.invalidate_library_cache()
    return {"success": True, "unlinked": unlinked, "kept_for_services": hidden}

@app.post("/api/image-folders/reorder")
async def api_image_folder_reorder(data: dict = Body(...)):
    ordered_ids = data.get('ordered_ids', [])
    APP_STATE.db.reorder_image_folders(ordered_ids)
    return {"success": True}

@app.post("/api/image-folders/add-image")
async def api_image_folder_add_image(data: dict = Body(...)):
    folder_id = data.get('folder_id')
    filename = data.get('filename', '')
    if not folder_id or not filename:
        return {"success": False, "message": "folder_id and filename required"}
    item_id = APP_STATE.db.add_image_to_folder(folder_id, filename)
    return {"success": True, "id": item_id}

@app.post("/api/image-folders/remove-image")
async def api_image_folder_remove_image(data: dict = Body(...)):
    item_id = data.get('id')
    if not item_id:
        return {"success": False, "message": "ID required"}
    APP_STATE.db.remove_image_from_folder(item_id)
    return {"success": True}

@app.post("/api/image-folders/reorder-images")
async def api_image_folder_reorder_images(data: dict = Body(...)):
    folder_id = data.get('folder_id')
    ordered_ids = data.get('ordered_ids', [])
    if not folder_id:
        return {"success": False, "message": "folder_id required"}
    APP_STATE.db.reorder_image_folder_items(folder_id, ordered_ids)
    return {"success": True}

@app.post("/api/select-single-image")
async def api_select_single_image(data: dict = Body(...)):
    filename = data.get('filename')
    if not filename:
        return {"success": False, "message": "filename required"}
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
    await manager.broadcast_state()
    return {"success": True}

@app.post("/api/select-image-folder")
async def api_select_image_folder(data: dict = Body(...)):
    folder_id = data.get('folder_id')
    if not folder_id:
        return {"success": False, "message": "folder_id required"}
    folder = APP_STATE.db.get_image_folder(folder_id)
    if not folder:
        return {"success": False, "message": "Folder not found"}
    images = [fi['filename'] for fi in folder.get('images', [])]
    start_index = int(data.get('index', 0))
    APP_STATE.current_mode = 'image'
    APP_STATE.current_image_data = {
        'folder_id': folder_id,
        'folder_name': folder['name'],
        'images': images,
        'index': max(0, min(start_index, len(images) - 1)) if images else 0,
    }
    APP_STATE.current_item_index = -1
    APP_STATE.current_song_id = None
    APP_STATE.is_blank = False
    APP_STATE._clear_outputs_and_player()
    _cancel_pending_video_task()
    await manager.broadcast_state()
    return {"success": True}

@app.post("/api/live/image-next")
async def api_image_next():
    img_data = APP_STATE.current_image_data
    images = img_data.get('images', [])
    if not images:
        return {"success": False}
    idx = min(img_data.get('index', 0) + 1, len(images) - 1)
    APP_STATE.current_image_data['index'] = idx
    await manager.broadcast_state()
    return {"success": True, "index": idx}

@app.post("/api/live/image-prev")
async def api_image_prev():
    img_data = APP_STATE.current_image_data
    images = img_data.get('images', [])
    if not images:
        return {"success": False}
    idx = max(img_data.get('index', 0) - 1, 0)
    APP_STATE.current_image_data['index'] = idx
    await manager.broadcast_state()
    return {"success": True, "index": idx}

@app.post("/api/live/image-goto")
async def api_image_goto(data: dict = Body(...)):
    img_data = APP_STATE.current_image_data
    images = img_data.get('images', [])
    if not images:
        return {"success": False}
    idx = max(0, min(int(data.get('index', 0)), len(images) - 1))
    APP_STATE.current_image_data['index'] = idx
    await manager.broadcast_state()
    return {"success": True, "index": idx}

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
    APP_STATE.db.add_image_folder_to_service(service_id, folder_id, folder_name)
    _refresh_current_service_items(prev_item_id)
    manager.invalidate_library_cache()
    await manager.broadcast_state()
    return {"success": True}

@app.post("/api/services/create-image-folder")
async def api_create_image_folder_in_service(data: dict = Body(...)):
    """Create an empty image folder inside the current service (not linked to the library)."""
    service_id = APP_STATE.current_service_id
    if service_id == -1:
        raise HTTPException(status_code=400, detail="No service selected")
    folder_name = (data.get('folder_name') or '').strip() or 'New Folder'
    prev_item_id = APP_STATE.active_item_id()
    item_id = APP_STATE.db.create_service_image_folder(service_id, folder_name)
    _refresh_current_service_items(prev_item_id)
    await _rebuild_slides()
    await manager.broadcast_state()
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
    ok = APP_STATE.db.merge_image_into_service_folder(from_item_id, to_item_id, to_index)
    if not ok:
        return {"success": False, "message": "Merge failed"}
    # Merging deletes the standalone source image item; if it was live the display is
    # cleared, otherwise the (surviving) active item is rebuilt to reflect the change.
    active_lost = _refresh_current_service_items(prev_item_id)
    if not active_lost:
        await _rebuild_slides()
    await manager.broadcast_state()
    return {"success": True}

@app.post("/api/services/folder-remove-image")
async def api_folder_remove_image(data: dict = Body(...)):
    """Remove a single image from a service image_folder item's snapshot (service-scoped)."""
    item_id = data.get('item_id')
    index = data.get('index')
    if not item_id or index is None:
        return {"success": False, "message": "item_id and index required"}
    prev_item_id = APP_STATE.active_item_id()
    ok = APP_STATE.db.remove_filename_from_service_folder(item_id, int(index))
    if not ok:
        return {"success": False, "message": "Remove failed"}
    APP_STATE.db.cleanup_orphan_hidden_images(os.path.join(APP_STATE.export_dir, 'images'))
    active_lost = _refresh_current_service_items(prev_item_id)
    if not active_lost:
        await _rebuild_slides()
    await manager.broadcast_state()
    return {"success": True}

@app.post("/api/services/folder-remove-images")
async def api_folder_remove_images(data: dict = Body(...)):
    """Bulk remove from service folder snapshots. `removals` = [{item_id, index}, ...]."""
    removals = data.get('removals') or []
    if not removals:
        return {"success": False, "message": "removals required"}
    prev_item_id = APP_STATE.active_item_id()
    deleted = APP_STATE.db.remove_filenames_from_service_folders(removals)
    APP_STATE.db.cleanup_orphan_hidden_images(os.path.join(APP_STATE.export_dir, 'images'))
    active_lost = _refresh_current_service_items(prev_item_id)
    if not active_lost:
        await _rebuild_slides()
    await manager.broadcast_state()
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
    added = APP_STATE.db.add_images_to_service(sid, filenames)
    _refresh_current_service_items(prev_item_id)
    manager.invalidate_library_cache()
    await manager.broadcast_state()
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
    ok = APP_STATE.db.add_filenames_to_service_folder(item_id, filenames, to_index)
    if not ok:
        return {"success": False, "message": "Add failed"}
    active_lost = _refresh_current_service_items(prev_item_id)
    if not active_lost:
        await _rebuild_slides()
    await manager.broadcast_state()
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
    APP_STATE.db.add_image_to_service(service_id, filename)
    _refresh_current_service_items(prev_item_id)
    manager.invalidate_library_cache()
    await manager.broadcast_state()
    return {"success": True}

@app.post("/api/services/add-divider")
async def api_add_divider_to_service(data: dict = Body(...)):
    service_id = APP_STATE.current_service_id
    if service_id == -1:
        raise HTTPException(status_code=400, detail="No service selected")
    title = data.get('title', 'Section')
    prev_item_id = APP_STATE.active_item_id()
    APP_STATE.db.add_divider_to_service(service_id, title)
    _refresh_current_service_items(prev_item_id)
    manager.invalidate_library_cache()
    await manager.broadcast_state()
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
        if temp_path and os.path.exists(temp_path):
            try: os.unlink(temp_path)
            except Exception: pass

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
    await manager.broadcast_state()

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
    await _rebuild_slides()
    await manager.broadcast_state()


@app.post("/api/output/add")
async def api_output_add(data: dict = Body(...)):
    oc = OutputConfig.from_dict(data)
    _seed_default_themes(oc)
    APP_STATE.outputs.append(oc)
    APP_STATE.config_manager.save_config()
    await _export_outputs()
    await manager.broadcast_state()
    return {"success": True}

@app.post("/api/output/edit")
async def api_output_edit(data: dict = Body(...)):
    idx = data.get('index')
    if 0 <= idx < len(APP_STATE.outputs):
        old = APP_STATE.outputs[idx]
        updated = OutputConfig.from_dict(data)
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
        manager._refresh_freeze_snapshots()
        await manager.broadcast_state()
    return {"success": True}

@app.post("/api/output/delete")
async def api_output_delete(data: dict = Body(...)):
    idx = data.get('index')
    if 0 <= idx < len(APP_STATE.outputs):
        APP_STATE.outputs.pop(idx)
        APP_STATE.config_manager.save_config()
        await _export_outputs()
        await manager.broadcast_state()
    return {"success": True}

@app.post("/api/output/reorder")
async def api_output_reorder(data: dict = Body(...)):
    idx = data.get('index')
    direction = data.get('direction') # 'up' or 'down'
    if idx is None or not direction:
        return {"success": False, "message": "Missing index or direction"}
        
    changed = APP_STATE.config_manager.reorder_output(idx, direction)
    if changed:
        await _export_outputs()
        await manager.broadcast_state()
        return {"success": True}
    return {"success": False, "message": "No change"}


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
            updated = t
            break
    if not updated:
        return {"success": False, "message": "Theme not found"}

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

    # Repoint any category defaults that referenced the deleted theme to the first remaining.
    field_key = 'text' if kind == 'text' else 'bg'
    fallback_id = remaining[0]['id']
    for cat, ent in (oc.category_defaults or {}).items():
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
    await _rebuild_slides()
    await manager.broadcast_state()
    return {"success": True, "category_defaults": clean}


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
        if temp_path and os.path.exists(temp_path):
            try: os.unlink(temp_path)
            except Exception: pass

@app.post("/api/bibles/import")
async def api_bible_import(file: UploadFile = File(...)):
    try:
        content = await file.read()
        result = await asyncio.to_thread(_import_bible_bytes, content)
        if result is None:
            return JSONResponse({'success': False, 'message': 'No verses found in XML'})
        bid, count = result
        manager.invalidate_library_cache()
        await manager.broadcast_state()
        return {'success': True, 'id': bid, 'count': count}
    except Exception as e:
        return JSONResponse({'success': False, 'message': str(e)})

@app.get("/api/bibles")
async def api_get_bibles():
    return APP_STATE.db.get_bibles()

@app.post("/api/bibles/delete")
async def api_bibles_delete(data: dict = Body(...)):
    bid = data.get('id')
    if bid:
        APP_STATE.db.delete_bible(bid)
        manager.invalidate_library_cache()
        await manager.broadcast_state()
    return {"success": True}

@app.post("/api/bibles/rename")
async def api_bibles_rename(data: dict = Body(...)):
    bid = data.get('id')
    new_name = data.get('name')
    if bid and new_name:
        APP_STATE.db.rename_bible(bid, new_name)
        manager.invalidate_library_cache()
        await manager.broadcast_state()
    return {"success": True}

@app.get("/api/bibles/{id}/books")
async def api_get_bible_books(id: int):
    return APP_STATE.db.get_bible_books(id)

@app.get("/api/bibles/{id}/{book}/chapters")
async def api_get_bible_chapters(id: int, book: str):
    return APP_STATE.db.get_bible_chapters(id, book)

@app.get("/api/bibles/{id}/{book}/{chapter}")
async def api_get_bible_verses(id: int, book: str, chapter: int):
    return APP_STATE.db.get_bible_verses(id, book, chapter)

@app.post("/api/bibles/search")
async def api_search_bible(data: dict = Body(...)):
    return APP_STATE.db.search_bible(data.get('id'), data.get('query'))

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

    books = APP_STATE.db.get_bible_books(bid)
    parsed = parse_bible_reference(reference, books)
    if not parsed:
        return {"success": False, "message": f'Couldn\'t find "{reference}" in this bible.'}

    book, chapter = parsed['book'], parsed['chapter']
    v_start, v_end = parsed['verse_start'], parsed['verse_end']

    chapters = APP_STATE.db.get_bible_chapters(bid, book)
    # Single-chapter books (Jude, Philemon, Obadiah, 2/3 John): a bare trailing number
    # is conventionally the verse, not the chapter — reinterpret it against chapter 1.
    if v_start is None and len(chapters) == 1 and chapter not in chapters:
        v_start = v_end = chapter
        chapter = chapters[0]
    if chapter not in chapters:
        return {"success": False, "message": f"{book} has no chapter {chapter}."}

    chapter_verses = APP_STATE.db.get_bible_verses(bid, book, chapter)
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
    # Range selection sends bible_id/book/verse_start/verse_end; the legacy single-verse
    # form (text/verse_num) is still accepted by _fetch_bible_verses.
    APP_STATE.current_mode = 'bible'
    APP_STATE.current_bible_data = data  # Store all sent data
    APP_STATE.is_blank = False
    await _rebuild_slides()
    await manager.broadcast_state()
    return {"success": True}

@app.post("/api/services/add-bible")
async def api_add_bible_to_service(data: dict = Body(...)):
    service_id = APP_STATE.current_service_id
    if (service_id == -1):
        raise HTTPException(status_code=400, detail="No service selected")
    
    prev_item_id = APP_STATE.active_item_id()
    APP_STATE.db.add_bible_to_service(service_id, data)
    _refresh_current_service_items(prev_item_id)
    manager.invalidate_library_cache()
    await manager.broadcast_state()
    return {"success": True}


# Fixed default port for the web UI. A stable, predictable port (rather than an
# OS-assigned free one) is what lets people on other devices reach the UI at an
# address that never changes between launches: http://<this-machine-ip>:49777/.
# 49777 sits in the private/dynamic range, so it rarely clashes with other services.
DEFAULT_PORT = 49777


def build_server(port=DEFAULT_PORT, host="0.0.0.0", log_level="info"):
    """Prepare runtime state and return a configured, not-yet-running uvicorn Server.

    Split out from start_server so the setup can be reused: returning the Server
    object (rather than running it) gives the caller a clean shutdown hook
    (``server.should_exit = True``).
    """
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
