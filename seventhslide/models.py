"""Core data structures: OutputConfig and the field-group key sets."""
import json
from dataclasses import dataclass, field, asdict



# ---------------------- Data structures ----------------------

@dataclass
class OutputConfig:
    """Configuration for a presentation output display."""
    # Core identification
    name: str

    # Canvas dimensions
    canvas_width: int = 1920
    canvas_height: int = 1080

    # Lyrics text box positioning
    box_x: int = 320
    box_y: int = 340
    width_px: int = 1280
    height_px: int = 400

    # Lyrics text styling
    font_family: str = 'Helvetica'
    font_size: int = 48
    area_padding: int = 20

    # Transition settings
    enable_fade: bool = False
    fade_duration: int = 500

    # Display options
    show_chords: bool = False
    align: str = 'center'
    valign: str = 'center'
    fluid_slides: bool = False
    follow_lines: int = 0
    highlight_color: str = '#ffffff'
    dim_color: str = '#888888'
    prevent_mixed_active: bool = False
    verse_gap: int = 0
    highlight_font_size: int = 0

    # Slide indicator
    show_indicator: bool = False
    indicator_x: int = 10
    indicator_y: int = 1000
    indicator_font_size: int = 30

    # Wall-clock overlay (intrinsic, per-output — never themed). Stays visible across
    # every content category so a foldback / stage monitor can always show the time.
    show_clock: bool = False
    clock_x: int = 10
    clock_y: int = 10
    clock_font_size: int = 48
    clock_color: str = '#ffffff'
    clock_seconds: bool = False
    clock_24h: bool = False

    # Bible reference box
    bible_ref_box_x: int = 100
    bible_ref_box_y: int = 900
    bible_ref_width: int = 800
    bible_ref_height: int = 100
    bible_ref_font_family: str = ''
    bible_ref_font_size: int = 30
    bible_ref_color: str = '#ffffff'
    bible_ref_align: str = 'left'
    bible_ref_valign: str = 'center'

    # Bible main text styling
    bible_main_font_family: str = ''
    bible_main_font_size: int = 0

    # Bible text box positioning
    bible_text_box_x: int = 320
    bible_text_box_y: int = 340
    bible_text_box_width: int = 1280
    bible_text_box_height: int = 400
    bible_text_padding: int = 20
    bible_text_color: str = '#ffffff'
    bible_text_align: str = 'center'
    bible_text_valign: str = 'center'

    # Bible display options
    show_bible_text: bool = True
    show_bible_ref: bool = True
    show_bible_verse_numbers: bool = False

    # Video settings
    video_enabled: bool = True
    video_area_x: int = 0
    video_area_y: int = 0
    video_area_width: int = 0
    video_area_height: int = 0

    # Video countdown timer overlay
    show_video_countdown: bool = False
    video_countdown_x: int = 10
    video_countdown_y: int = 50
    video_countdown_font_size: int = 30
    video_countdown_font_family: str = ''
    video_countdown_color: str = '#ffffff'
    video_countdown_align: str = 'left'

    # Image display area settings
    image_enabled: bool = True
    image_area_x: int = 0
    image_area_y: int = 0
    image_area_width: int = 0
    image_area_height: int = 0
    image_fit: str = 'contain'  # 'contain', 'cover', 'fill'

    # Background settings
    background_type: str = 'transparent'  # 'transparent', 'color', 'image'
    background_color: str = '#000000'
    background_image: str = ''

    # Bible-specific background settings
    bible_background_type: str = 'inherit'  # 'inherit', 'transparent', 'color', 'image'
    bible_background_color: str = '#000000'
    bible_background_image: str = ''

    # Blank settings
    exempt_from_global_blank: bool = False
    # Freeze settings — when global freeze is active, an exempt output keeps
    # updating live (mirrors the blank exemption, e.g. a stage monitor).
    exempt_from_global_freeze: bool = False
    show_announcements: bool = True

    # Copyright box
    copyright_box_x: int = 100
    copyright_box_y: int = 980
    copyright_box_width: int = 1720
    copyright_box_height: int = 80
    copyright_font_family: str = ''
    copyright_font_size: int = 20
    copyright_color: str = '#ffffff'
    copyright_align: str = 'left'
    copyright_valign: str = 'center'
    show_copyright: bool = True
    copyright_slide_mode: str = 'all'
    copyright_slide_count: int = 1

    # Text opacity settings
    text_opacity: float = 1.0
    bible_text_opacity: float = 1.0
    bible_ref_opacity: float = 1.0
    copyright_text_opacity: float = 1.0
    indicator_opacity: float = 1.0

    # Theme model v2: separate text + background theme libraries (per output)
    text_themes: list = field(default_factory=list)
    bg_themes: list = field(default_factory=list)
    # Per-category default theme assignments:
    # {'song': {'text': id, 'bg': id}, 'bible': {...}, 'announcement': {'bg': id}}
    category_defaults: dict = field(default_factory=dict)

    # Runtime state (not persisted in to_dict serialization)
    is_blank: bool = field(default=False, init=False, repr=False)
    is_frozen: bool = field(default=False, init=False, repr=False)
    is_ignored: bool = field(default=False, init=False, repr=False)
    slides: list = field(default_factory=list, init=False, repr=False)
    index: int = field(default=0, init=False, repr=False)
    line_to_slide: list = field(default_factory=list, init=False, repr=False)
    verse_codes: list = field(default_factory=list, init=False, repr=False)
    verse_indices: list = field(default_factory=list, init=False, repr=False)
    template_html: str = field(default='', init=False, repr=False)

    def __post_init__(self):
        """Handle computed defaults for font fallbacks."""
        # Fallback empty font families to main font_family
        if not self.bible_ref_font_family:
            self.bible_ref_font_family = self.font_family
        if not self.bible_main_font_family:
            self.bible_main_font_family = self.font_family
        if not self.copyright_font_family:
            self.copyright_font_family = self.font_family
        if not self.video_countdown_font_family:
            self.video_countdown_font_family = self.font_family

        # Fallback bible_main_font_size to main font_size if not set
        if not self.bible_main_font_size:
            self.bible_main_font_size = self.font_size

    def to_dict(self):
        """Serialize to dict with actual resolved values, excluding runtime-only fields."""
        d = asdict(self)
        for k in _OUTPUT_RUNTIME_KEYS:
            d.pop(k, None)
        return d

    def to_persist_dict(self):
        """Serialize for config file persistence (theme model v2).

        Only intrinsic output config and the theme libraries are persisted; all
        themeable style now lives inside text_themes / bg_themes. Loose style
        fields on the dataclass are runtime scratch space populated by the
        theme resolver and are intentionally not written out.
        """
        full = self.to_dict()
        keep = _OUTPUT_STRUCTURAL_KEYS | {'index'} | OUTPUT_INTRINSIC_KEYS
        return {k: full[k] for k in keep if k in full}

    def style_to_dict(self):
        """Export style properties only (excludes structural config and runtime state)."""
        d = self.to_dict()
        for k in _OUTPUT_STRUCTURAL_KEYS | {'index'}:
            d.pop(k, None)
        return d

    def apply_style_dict(self, d: dict):
        """Apply style properties from a dict, excluding structural config."""
        if not d:
            return
        for k, v in d.items():
            if k in _OUTPUT_STRUCTURAL_KEYS:
                continue
            if hasattr(self, k):
                setattr(self, k, v)

    @classmethod
    def from_dict(cls, d):
        """Deserialize from dict with defaults for missing fields."""
        # Filter to only include fields that are in __init__ (dataclass fields with init=True)
        init_fields = {f.name for f in cls.__dataclass_fields__.values() if f.init}
        kwargs = {k: v for k, v in d.items() if k in init_fields}

        # Create instance with filtered kwargs
        oc = cls(**kwargs)

        # Set runtime state fields that are persisted
        oc.index = d.get('index', 0)

        return oc

# Single source of truth for how OutputConfig fields are grouped. Every
# serializer below derives its key set from these rather than re-listing fields.
#   - runtime: transient state, never serialized
#   - structural: edited via Output Settings, never part of themeable style
#   - 'index': structural but persisted as a loose runtime value
_OUTPUT_RUNTIME_KEYS = frozenset({
    'is_blank', 'is_frozen', 'is_ignored', 'slides', 'line_to_slide',
    'verse_codes', 'verse_indices', 'template_html',
})
_OUTPUT_STRUCTURAL_KEYS = frozenset({
    'name', 'canvas_width', 'canvas_height',
    'text_themes', 'bg_themes', 'category_defaults',
})
_OUTPUT_STYLE_EXCLUDE = _OUTPUT_RUNTIME_KEYS | _OUTPUT_STRUCTURAL_KEYS | {'index'}
OUTPUT_STYLE_KEYS = frozenset(
    f for f in OutputConfig.__dataclass_fields__ if f not in _OUTPUT_STYLE_EXCLUDE
)

# --- Theme model v2: split themeable style into Text themes + Background themes ---
# Background-theme fields (the literal screen background behind everything).
BG_THEME_KEYS = frozenset({
    'background_type', 'background_color', 'background_image',
})
# Fields that stay intrinsic to the output (edited in Output Settings, never themed).
OUTPUT_INTRINSIC_KEYS = frozenset({
    'video_enabled', 'image_enabled', 'show_announcements',
    'exempt_from_global_blank', 'exempt_from_global_freeze',
    'show_clock', 'clock_x', 'clock_y', 'clock_font_size',
    'clock_color', 'clock_seconds', 'clock_24h',
})
# Legacy per-mode background fields, dropped in v2 (bible uses its category bg theme).
LEGACY_BIBLE_BG_KEYS = frozenset({
    'bible_background_type', 'bible_background_color', 'bible_background_image',
})
# Text-theme fields = everything else themeable (fonts, positioning, transitions,
# opacity, image/video positioning, copyright, fine display toggles, etc.).
TEXT_THEME_KEYS = frozenset(
    OUTPUT_STYLE_KEYS - BG_THEME_KEYS - OUTPUT_INTRINSIC_KEYS - LEGACY_BIBLE_BG_KEYS
)
# Categories that have themed defaults on each output.
THEME_CATEGORIES = ('song', 'bible', 'announcement')

# Service-item types whose `data` payload is essentially a title (+ optional
# extras). Each maps parsed-data -> fields to merge onto the item dict. For these,
# an empty/invalid payload yields the same defaults as a parse failure.
SIMPLE_SERVICE_ITEM_PARSERS = {
    'video':        lambda x: {'title': x.get('title') or x.get('filename', 'Video')},
    'image':        lambda x: {'title': x.get('filename', 'Image')},
    'image_folder': lambda x: {'title': x.get('folder_name', 'Image Folder'),
                               'folder_images': x.get('images', [])},
    'divider':      lambda x: {'title': x.get('title', 'Section')},
}


class Song:
    # Transfer object produced by parse_song_file (OpenLyrics/XML import).
    # Persisted songs flow as sqlite3.Row/dicts everywhere else.
    def __init__(self, title, lyrics, verse_order=None, id=None, authors=None, songbook_name="", songbook_entry="", copyright="", ccli_song_number="", key=""):
        self.id = id
        self.title = title
        self.lyrics = lyrics
        self.verse_order = verse_order
        # Handle authors migration from string or list
        if isinstance(authors, str):
            try:
                self.authors = json.loads(authors)
            except json.JSONDecodeError:
                self.authors = [authors] if authors else []
        else:
            self.authors = authors or []

        self.songbook_name = songbook_name
        self.songbook_entry = songbook_entry
        self.copyright = copyright or ""
        self.ccli_song_number = ccli_song_number or ""
        self.key = key or ""


__all__ = [
    'BG_THEME_KEYS',
    'LEGACY_BIBLE_BG_KEYS',
    'OUTPUT_INTRINSIC_KEYS',
    'OUTPUT_STYLE_KEYS',
    'OutputConfig',
    'SIMPLE_SERVICE_ITEM_PARSERS',
    'Song',
    'TEXT_THEME_KEYS',
    'THEME_CATEGORIES',
    '_OUTPUT_RUNTIME_KEYS',
    '_OUTPUT_STRUCTURAL_KEYS',
    '_OUTPUT_STYLE_EXCLUDE',
]
