"""Cross-platform font resolution (fontconfig / bundled index) and bundling."""
import os
import sys
import json
import shutil
import hashlib
import threading
import subprocess
from functools import lru_cache
from typing import Optional, List, Dict

try:
    from PIL import ImageFont
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

from .paths import logger, get_data_dir




# Persistent, cross-process cache for resolved font files. Resolution is costly —
# an fc-match subprocess (~0.3s on systems with many fonts) or building the font
# index — and font bundling resolves dozens of faces per export, so doing it fresh
# on every launch added several seconds of startup hang. System fonts almost never
# change between runs, so we persist the resolved (family/style/weight/slant) ->
# file-path map to disk and only re-resolve on a genuine miss. Stale entries (font
# moved/uninstalled) are detected by the os.path.exists check below and re-resolved
# transparently. The cache is backend-agnostic: the same key/value shape works
# whether the path came from fontconfig or the bundled cross-platform index.
_FC_DISK_CACHE: Optional[dict] = None
_FC_DISK_CACHE_PATH: Optional[str] = None


def _fc_disk_cache() -> dict:
    global _FC_DISK_CACHE, _FC_DISK_CACHE_PATH
    if _FC_DISK_CACHE is None:
        _FC_DISK_CACHE_PATH = os.path.join(get_data_dir(), '.font_match_cache.json')
        try:
            with open(_FC_DISK_CACHE_PATH, encoding='utf-8') as f:
                loaded = json.load(f)
            _FC_DISK_CACHE = loaded if isinstance(loaded, dict) else {}
        except Exception:
            _FC_DISK_CACHE = {}
    return _FC_DISK_CACHE


def _fc_disk_cache_save() -> None:
    if _FC_DISK_CACHE is None or not _FC_DISK_CACHE_PATH:
        return
    try:
        tmp = f"{_FC_DISK_CACHE_PATH}.tmp"
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(_FC_DISK_CACHE, f)
        os.replace(tmp, _FC_DISK_CACHE_PATH)
    except Exception:
        # Non-fatal: the in-memory cache still works this run; only persistence is
        # lost. Log it so a permanently-failing write (bad perms, full disk) doesn't
        # silently degrade every restart into a cold fc-match.
        logger.debug("Failed to persist font-match cache", exc_info=True)


def _fc_match_run(font_family: str, style: Optional[str], weight: Optional[str],
                  slant: Optional[str]) -> Optional[str]:
    """Run fc-match for one pattern. Returns a validated file path or None."""
    try:
        pat = font_family
        if style:
            pat = f"{font_family}:style={style}"
        else:
            if weight:
                pat += f":weight={weight}"
            if slant:
                pat += f":slant={slant}"

        res = subprocess.run(['fc-match', '-f', '%{file}', pat],
                           capture_output=True, text=True, timeout=2)
        if res.returncode == 0 and res.stdout.strip():
            p = res.stdout.strip()
            if os.path.exists(p) and os.path.isfile(p):
                return p
    except Exception:
        return None
    return None


@lru_cache(maxsize=1)
def _fontconfig_available() -> bool:
    """Whether the fontconfig `fc-match` tool is on PATH.

    Present by default on Linux; available on macOS/Windows only if the user
    installed fontconfig. When absent we fall back to the bundled font index.
    """
    return shutil.which('fc-match') is not None


def _system_font_dirs() -> List[str]:
    """The standard per-OS directories that hold installed font files."""
    home = os.path.expanduser('~')
    if sys.platform == 'win32':
        dirs = [os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Fonts')]
        local = os.environ.get('LOCALAPPDATA')
        if local:  # per-user fonts (installed without admin rights)
            dirs.append(os.path.join(local, 'Microsoft', 'Windows', 'Fonts'))
        return dirs
    if sys.platform == 'darwin':
        return ['/System/Library/Fonts', '/System/Library/Fonts/Supplemental',
                '/Library/Fonts', os.path.join(home, 'Library', 'Fonts')]
    # Linux / *BSD: XDG plus the conventional locations.
    dirs = ['/usr/share/fonts', '/usr/local/share/fonts',
            os.path.join(home, '.fonts'), os.path.join(home, '.local', 'share', 'fonts')]
    xdg = os.environ.get('XDG_DATA_HOME')
    if xdg:
        dirs.append(os.path.join(xdg, 'fonts'))
    return dirs


# Cross-platform font index: family (lowercased) -> list of (subfamily, file path).
# Built once, lazily, by scanning the system font directories and reading each
# face's name table via PIL. This is the platform-agnostic fallback to fontconfig,
# so font measurement and "Bundle Local Fonts" work identically on Windows/macOS/Linux.
_FONT_INDEX: Optional[Dict[str, List[tuple]]] = None
_FONT_INDEX_LOCK = threading.Lock()
_FONT_FILE_EXTS = ('.ttf', '.otf', '.ttc', '.otc')


def _faces_in_file(path: str) -> List[tuple]:
    """Read (family, subfamily) for every face in a font file via PIL.

    Handles TrueType/OpenType collections (.ttc/.otc) by walking face indices
    until PIL reports no more. Returns [] on any read error.
    """
    faces: List[tuple] = []
    is_collection = path.lower().endswith(('.ttc', '.otc'))
    index = 0
    while True:
        try:
            font = ImageFont.truetype(path, size=10, index=index)
            family, sub = font.getname()
        except Exception:
            break
        if family:
            faces.append((family, sub or ''))
        if not is_collection:
            break
        index += 1
        if index > 64:  # defensive bound against a malformed collection header
            break
    return faces


def _font_index() -> Dict[str, List[tuple]]:
    """Lazily build (and cache) the system font index. Thread-safe."""
    global _FONT_INDEX
    if _FONT_INDEX is not None:
        return _FONT_INDEX
    with _FONT_INDEX_LOCK:
        if _FONT_INDEX is not None:  # built while we waited on the lock
            return _FONT_INDEX
        index: Dict[str, List[tuple]] = {}
        if HAS_PIL:
            seen_paths = set()
            for directory in _system_font_dirs():
                if not os.path.isdir(directory):
                    continue
                for root, _dirs, files in os.walk(directory):
                    for name in files:
                        if os.path.splitext(name)[1].lower() not in _FONT_FILE_EXTS:
                            continue
                        path = os.path.join(root, name)
                        if path in seen_paths:
                            continue
                        seen_paths.add(path)
                        for family, sub in _faces_in_file(path):
                            index.setdefault(family.lower(), []).append((sub, path))
        _FONT_INDEX = index
        logger.debug("Built font index: %d families", len(index))
        return _FONT_INDEX


def _desired_face(style: Optional[str], weight: Optional[str],
                  slant: Optional[str]) -> tuple:
    """Normalise a fontconfig-style request to (want_bold, want_italic) booleans."""
    s = (style or '').lower()
    w = str(weight or '').lower()
    want_bold = ('bold' in s) or (w == 'bold') or (w.isdigit() and int(w) >= 600)
    want_italic = ('italic' in s) or ('oblique' in s) or ((slant or '').lower() == 'italic')
    return want_bold, want_italic


def _classify_subfamily(sub: str) -> tuple:
    """Classify a face's subfamily string into (is_bold, is_italic)."""
    s = sub.lower()
    is_bold = any(tok in s for tok in ('bold', 'black', 'heavy', 'semibold', 'extrabold'))
    is_italic = ('italic' in s) or ('oblique' in s)
    return is_bold, is_italic


def _index_match(font_family: str, want_bold: bool, want_italic: bool) -> Optional[str]:
    """Resolve a family + desired face to a file path using the scanned index.

    Returns the best-scoring face for the family, or None if the family is not
    installed (so callers fall back to a regular face or browser synthesis,
    rather than silently substituting an unrelated font as fontconfig would).
    """
    faces = _font_index().get(font_family.lower())
    if not faces:
        return None
    best_path, best_score = None, -1
    for sub, path in faces:
        is_bold, is_italic = _classify_subfamily(sub)
        score = (2 if is_bold == want_bold else 0) + (2 if is_italic == want_italic else 0)
        # Tie-break toward the *canonical* face for the requested weight/slant so
        # e.g. a bold request prefers "Bold" over a same-classified "Black"/"SemiBold",
        # and a regular request prefers the plain face over "Light"/"Medium".
        s = sub.lower().replace(' ', '')
        canonical = {
            (False, False): s in ('regular', 'book', 'roman', 'normal', ''),
            (True, False): s == 'bold',
            (False, True): s in ('italic', 'oblique'),
            (True, True): s in ('bolditalic', 'boldoblique'),
        }[(want_bold, want_italic)]
        if canonical:
            score += 1
        if score > best_score:
            best_path, best_score = path, score
    return best_path


def _resolve_font_run(font_family: str, style: Optional[str], weight: Optional[str],
                      slant: Optional[str]) -> Optional[str]:
    """Resolve a font family + face to a file path, cross-platform.

    Uses fontconfig when available (fast and authoritative on Linux), otherwise
    falls back to the bundled system-font index — so the result is consistent on
    Windows, macOS and Linux alike.
    """
    if _fontconfig_available():
        path = _fc_match_run(font_family, style, weight, slant)
        if path:
            return path
    want_bold, want_italic = _desired_face(style, weight, slant)
    return _index_match(font_family, want_bold, want_italic)


@lru_cache(maxsize=128)
def _resolve_font_file_cached(font_family: str, style: Optional[str] = None,
                                weight: Optional[str] = None, slant: Optional[str] = None) -> Optional[str]:
    """Resolve a font family + face to a file path, backed by a persistent cache.

    The lru_cache handles the hot in-process path; the disk cache (see notes
    above) survives restarts so the expensive resolution (an fc-match subprocess,
    or building the font index) runs only on a genuine miss or when a
    previously-resolved file no longer exists.
    """
    if not font_family:
        return None

    cache = _fc_disk_cache()
    key = f"{font_family}\x1f{style}\x1f{weight}\x1f{slant}"
    if key in cache:
        cached = cache[key]
        if cached and os.path.exists(cached) and os.path.isfile(cached):
            return cached
        # Stale (font moved/removed) — fall through and re-resolve.

    result = _resolve_font_run(font_family, style, weight, slant)
    if result and cache.get(key) != result:
        cache[key] = result
        _fc_disk_cache_save()
    return result


class FontManager:
    """Handles cross-platform font discovery and CSS bundling for exports."""

    # Output style attributes that name a font family.
    _FONT_FAMILY_ATTRS = ('font_family', 'bible_main_font_family', 'bible_ref_font_family',
                          'copyright_font_family', 'video_countdown_font_family')

    # Faces to attempt per family: (css_weight, css_style, fc_weight, fc_slant).
    _FONT_FACES = (
        (400, 'normal', 'regular', 'roman'),
        (700, 'normal', 'bold', 'roman'),
        (400, 'italic', 'regular', 'italic'),
        (700, 'italic', 'bold', 'italic'),
    )

    def __init__(self, app_state):
        """Initialize FontManager with reference to AppState.

        Args:
            app_state: Reference to parent AppState instance
        """
        self.app_state = app_state

    def _resolve_font_file(self, font_family: str, style: Optional[str] = None,
                            weight: Optional[str] = None, slant: Optional[str] = None) -> Optional[str]:
        """Resolve a font file cross-platform. Delegates to the cached module function."""
        return _resolve_font_file_cached(font_family, style, weight, slant)

    def _font_format_for_ext(self, ext: str) -> str:
        e = (ext or '').lower()
        if e == '.otf':
            return 'opentype'
        if e in ('.ttf', '.ttc'):
            return 'truetype'
        if e == '.woff2':
            return 'woff2'
        if e == '.woff':
            return 'woff'
        return 'truetype'

    def _bundle_fonts_for_output(self, oc: 'OutputConfig') -> str:
        if not self.app_state.bundle_local_fonts:
            return ''

        fonts_dir = os.path.join(self.app_state.export_dir, 'fonts')
        os.makedirs(fonts_dir, exist_ok=True)

        css_parts = []
        for fam in sorted(self._collect_output_font_families(oc)):
            css_parts.extend(self._build_font_faces_css(fam, fonts_dir))
        return "\n".join(css_parts) + ("\n" if css_parts else '')

    @classmethod
    def _collect_output_font_families(cls, oc) -> set:
        """Gather every font family referenced by the output's current style and its text themes,
        so switching themes never leaves a face unbundled. (Background themes carry no fonts.)"""
        families = set()
        for attr in cls._FONT_FAMILY_ATTRS:
            fam = getattr(oc, attr, '')
            if fam:
                families.add(str(fam))
        for t in (getattr(oc, 'text_themes', None) or []):
            if not isinstance(t, dict):
                continue
            st = t.get('style')
            if not isinstance(st, dict):
                continue
            for attr in cls._FONT_FAMILY_ATTRS:
                v = st.get(attr)
                if v:
                    families.add(str(v))
        return families

    def _build_font_faces_css(self, fam: str, fonts_dir: str) -> list:
        """Build the @font-face CSS blocks for one family, copying matched files into fonts_dir.

        Prefers property matching so real bold faces are used when present. Critical behavior:
        if a bold/italic face resolves to the same file as regular (common when a font ships no
        bold file), it is intentionally NOT registered, so the browser can synthesize the style.
        """
        regular_path = (
            self._resolve_font_file(fam, weight='regular', slant='roman')
            or self._resolve_font_file(fam, style='Regular')
            or self._resolve_font_file(fam)
        )

        css_parts = []
        seen_src = set()
        for weight, fstyle, fc_weight, fc_slant in self._FONT_FACES:
            p = self._resolve_face_path(fam, weight, fstyle, fc_weight, fc_slant, regular_path)
            if not p:
                continue
            # Don't map bold/italic to the same file as regular.
            if regular_path and p == regular_path and (weight != 400 or fstyle != 'normal'):
                continue

            out_name = self._copy_font_file(p, fonts_dir)
            if not out_name:
                continue
            src = f"/static/fonts/{out_name}"
            key = (fam, src, weight, fstyle)
            if key in seen_src:
                continue
            seen_src.add(key)

            fmt = self._font_format_for_ext(os.path.splitext(p)[1])
            css_parts.append(
                "@font-face {\n"
                f"  font-family: \"{fam}\";\n"
                f"  src: url('{src}') format('{fmt}');\n"
                f"  font-weight: {weight};\n"
                f"  font-style: {fstyle};\n"
                "  font-display: swap;\n"
                "}"
            )
        return css_parts

    def _resolve_face_path(self, fam, weight, fstyle, fc_weight, fc_slant, regular_path):
        """Resolve the font file for one face: property match, then style-name match, then regular."""
        p = self._resolve_font_file(fam, weight=fc_weight, slant=fc_slant)
        if p:
            return p
        if weight == 700 and fstyle == 'normal':
            return self._resolve_font_file(fam, style='Bold')
        if weight == 400 and fstyle == 'italic':
            return self._resolve_font_file(fam, style='Italic')
        if weight == 700 and fstyle == 'italic':
            return self._resolve_font_file(fam, style='Bold Italic')
        return regular_path

    @staticmethod
    def _copy_font_file(p: str, fonts_dir: str) -> Optional[str]:
        """Copy a font file into fonts_dir under a content-hashed name; return the filename or None."""
        ext = os.path.splitext(p)[1]
        h = hashlib.sha1(p.encode('utf-8', errors='ignore')).hexdigest()[:12]
        out_name = f"{h}{ext.lower() or '.ttf'}"
        dest = os.path.join(fonts_dir, out_name)
        try:
            if not os.path.exists(dest):
                shutil.copy2(p, dest)
        except Exception:
            return None
        return out_name


__all__ = [
    'FontManager',
    '_FC_DISK_CACHE',
    '_FC_DISK_CACHE_PATH',
    '_FONT_FILE_EXTS',
    '_FONT_INDEX',
    '_FONT_INDEX_LOCK',
    '_classify_subfamily',
    '_desired_face',
    '_faces_in_file',
    '_fc_disk_cache',
    '_fc_disk_cache_save',
    '_fc_match_run',
    '_font_index',
    '_fontconfig_available',
    '_index_match',
    '_resolve_font_file_cached',
    '_resolve_font_run',
    '_system_font_dirs',
]
