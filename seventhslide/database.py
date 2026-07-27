"""SQLite persistence layer (DatabaseManager) and the Song transfer object's store."""
import os
import re
import json
import sqlite3
import threading
from contextlib import contextmanager, suppress

from .paths import logger, get_data_dir
from .models import SIMPLE_SERVICE_ITEM_PARSERS


def _normalize_ann_item_fields(raw):
    """Normalize an announcement library item's fields to
    [{'label': str, 'value': str}, ...].

    The item owns its own labels and values (this is what replaced templates).
    Order is significant: at render time field i fills the resolved layout's
    slot i. Values may contain rich text and {variable} tokens — plain text at
    this layer.
    """
    out = []
    for f in raw or []:
        if isinstance(f, dict):
            out.append({'label': str(f.get('label') or ''), 'value': str(f.get('value') or '')})
        else:
            out.append({'label': '', 'value': str(f or '')})
    return out


# A ready-to-use "Song Title" title layout (song-variable tokens, no slots): a big
# title line over the authors and songbook reference, which drop out when absent.
# Geometry is stored as FRACTIONS of the canvas and materialized to whole canvas
# pixels per output at insert time (_seed_boxes_px).
_SEED_TITLE_BOXES_REL = [
    {'x': .08, 'y': .30, 'w': .84, 'h': .30, 'font_family': 'Helvetica', 'font_size': 96,
     'font_color': '#ffffff', 'text_align': 'center', 'vertical_align': 'middle',
     'line_height': 1.1, 'line_gap': 0,
     'lines': [{'text': '{song-title}', 'scale': 100, 'bold': True, 'italic': False, 'color': ''}]},
    {'x': .08, 'y': .62, 'w': .84, 'h': .14, 'font_family': 'Helvetica', 'font_size': 44,
     'font_color': '#ffffff', 'text_align': 'center', 'vertical_align': 'top',
     'line_height': 1.15, 'line_gap': 0,
     'lines': [{'text': '{authors}', 'scale': 100, 'bold': False, 'italic': True, 'color': ''},
               {'text': '{songbook} - #{songbook-number}', 'scale': 85, 'bold': False, 'italic': False, 'color': ''}]},
]


def _seed_boxes_px(canvas_w, canvas_h) -> list:
    """Materialize the relative seed boxes to canvas-pixel geometry."""
    out = []
    for b in _SEED_TITLE_BOXES_REL:
        px = dict(b)
        px['x'] = round(b['x'] * canvas_w)
        px['y'] = round(b['y'] * canvas_h)
        px['w'] = round(b['w'] * canvas_w)
        px['h'] = round(b['h'] * canvas_h)
        px['lines'] = [dict(ln) for ln in b['lines']]
        out.append(px)
    return out


def _clamp_line_height(raw):
    """Box line-height: a unitless CSS multiplier (leading within a wrapped line
    and the baseline spacing between lines). Default 1.15 ≈ browser `normal`, so
    boxes stored before this control shipped render unchanged."""
    try:
        lh = float(raw)
    except (TypeError, ValueError):
        return 1.15
    return round(max(0.8, min(3.0, lh)), 3)


def _clamp_line_gap(raw):
    """Extra space *after* each line (paragraph spacing), as a whole-number % of
    the box's base font — rendered as flex `gap`, so it collapses automatically
    when a line drops out. Default 0 preserves the pre-control appearance."""
    try:
        gap = int(round(float(raw)))
    except (TypeError, ValueError):
        return 0
    return max(0, min(300, gap))


def _normalize_ann_line(raw):
    """Normalize one content line of a layout box to
    {'text': str, 'scale': int %, 'bold': bool, 'italic': bool, 'color': str}."""
    if not isinstance(raw, dict):
        raw = {'text': str(raw or '')}
    try:
        scale = int(raw.get('scale') or 100)
    except (TypeError, ValueError):
        scale = 100
    return {'text': str(raw.get('text') or ''),
            'scale': max(10, min(400, scale)),
            'bold': bool(raw.get('bold')),
            'italic': bool(raw.get('italic')),
            'color': str(raw.get('color') or '')}


def _normalize_ann_boxes(raw, fields):
    """Normalize a layout's text_boxes to the box/lines shape:

        {x, y, w, h, font_family, font_size, font_color, text_align,
         vertical_align, line_height, line_gap,
         lines: [{text, scale, bold, italic, color}]}

    x/y/w/h are canvas pixels. A box is a positioned flow container; its lines
    carry the content as text with {tokens} (song variables and field names),
    each at a size relative to the box's base font. Two retired shapes convert
    on read (no data rewrite):

      - pre-lines boxes were index-parallel to the template's fields and carried
        bold/italic on the box → one line referencing that field by name;
      - stacked runs (stack_with_prev) → ONE box over the union of the members'
        rects, whose lines carry each member's field token at a scale relative
        to the first member's font size. The flow model subsumes stacking.
    """
    def field_name(i):
        return fields[i]['name'] if 0 <= i < len(fields or []) else ''
    boxes = [b for b in (raw or []) if isinstance(b, dict)]

    def geom_style(b, **overrides):
        out = {'x': b.get('x', 0), 'y': b.get('y', 0), 'w': b.get('w', 100), 'h': b.get('h', 100),
               'font_family': b.get('font_family', 'Helvetica'),
               'font_size': b.get('font_size', 48),
               'font_color': b.get('font_color', '#ffffff'),
               'text_align': b.get('text_align', 'center'),
               'vertical_align': b.get('vertical_align', 'middle'),
               'line_height': _clamp_line_height(b.get('line_height')),
               'line_gap': _clamp_line_gap(b.get('line_gap'))}
        out.update(overrides)
        return out

    # Partition legacy boxes into stack runs; modern boxes are their own run.
    runs = []
    for i, b in enumerate(boxes):
        if 'lines' not in b and i > 0 and b.get('stack_with_prev') and runs \
                and 'lines' not in boxes[runs[-1][0]]:
            runs[-1].append(i)
        else:
            runs.append([i])

    out = []
    for run in runs:
        first = boxes[run[0]]
        if 'lines' in first:
            box = geom_style(first)
            box['lines'] = [_normalize_ann_line(ln) for ln in (first.get('lines') or [])]
            out.append(box)
            continue
        if len(run) == 1:
            box = geom_style(first)
            box['lines'] = [_normalize_ann_line({'text': '{%s}' % field_name(run[0]),
                                                 'bold': first.get('bold'),
                                                 'italic': first.get('italic')})]
            out.append(box)
            continue
        # Legacy stacked run → merged flow box over the union rect.
        members = sorted(run, key=lambda k: float(boxes[k].get('y', 0)))
        ux = min(float(boxes[k].get('x', 0)) for k in members)
        uy = float(boxes[members[0]].get('y', 0))
        ur = max(float(boxes[k].get('x', 0)) + float(boxes[k].get('w', 100)) for k in members)
        ub = max(float(boxes[k].get('y', 0)) + float(boxes[k].get('h', 100)) for k in members)
        base = float(first.get('font_size', 48)) or 48
        box = geom_style(first, x=ux, y=uy, w=ur - ux, h=ub - uy,
                         vertical_align=first.get('stack_align', 'middle'))
        box['lines'] = [_normalize_ann_line({
            'text': '{%s}' % field_name(k),
            'scale': round(float(boxes[k].get('font_size', 48)) / base * 100),
            'bold': boxes[k].get('bold'),
            'italic': boxes[k].get('italic'),
            'color': boxes[k].get('font_color') if boxes[k].get('font_color') != box['font_color'] else '',
        }) for k in members]
        out.append(box)
    return out


class DatabaseManager:
    def __init__(self, db_path=None):
        if db_path is None:
            db_path = os.path.join(get_data_dir(), 'songs.db')
        self.db_path = db_path
        self._local = threading.local()
        self._init_db()
        self.backfill_song_snapshot_copyright()
        self.backfill_legacy_song_overrides()

    def _get_conn(self):
        # Return the cached per-thread connection without a proactive `SELECT 1`
        # liveness probe — an in-process threading.local connection effectively
        # never dies silently, and the probe taxed every DB call. Recovery from a
        # genuinely dead connection happens lazily in _db_transaction.
        conn = getattr(self._local, 'conn', None)
        if conn is None:
            # Default check_same_thread=True: each thread already owns its own
            # connection via threading.local; leaving the check enabled surfaces
            # accidental cross-thread reuse instead of hiding it.
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            # Retry briefly when another thread's writer holds the lock instead of
            # failing immediately with "database is locked".
            conn.execute("PRAGMA busy_timeout=5000")
            # Keep the WAL from growing unbounded between checkpoints (the long-lived
            # per-thread read connections can otherwise hold it open for a long time).
            conn.execute("PRAGMA wal_autocheckpoint=400")
            self._local.conn = conn
        return conn

    def close(self):
        """Close this thread's cached SQLite connection, if any."""
        conn = getattr(self._local, 'conn', None)
        if conn is None:
            return
        try:
            conn.close()
        finally:
            self._local.conn = None

    @contextmanager
    def _db_transaction(self, commit=True):
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            yield cur
            if commit:
                conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                # The connection itself is unusable — drop it so the next call
                # transparently reconnects (replaces the old SELECT 1 recovery).
                self._local.conn = None
            raise

    def checkpoint(self):
        """Force a full WAL checkpoint, truncating the -wal file. Called at startup so
        a large WAL left behind by a prior run (or a crash) doesn't linger."""
        try:
            self._get_conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            logger.debug("WAL checkpoint failed", exc_info=True)

    @staticmethod
    def _parse_json_field(value, default=None):
        """Safely parse a JSON string. Returns default (not {}) on missing or invalid input."""
        if not value:
            return default
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return default

    def _parse_entity_data(self, row_dict):
        """Parse common JSON fields from a database row (authors, theme_map)."""
        # Parse authors
        if 'authors' in row_dict:
            row_dict['authors'] = self._parse_json_field(row_dict.get('authors'), [])

        # Parse theme_map
        if 'theme_map' in row_dict:
            row_dict['theme_map'] = self._parse_json_field(row_dict.get('theme_map'), {})

        return row_dict

    def _init_db(self):
        # Schema is the final shape — no ALTER/_add_column migrations. Existing
        # installs already have these columns; ancient DBs missing a column are
        # unsupported (see BUILD_INSTRUCTIONS.md / README "Database upgrades").
        conn = self._get_conn()
        cur = conn.cursor()

        cur.execute('''CREATE TABLE IF NOT EXISTS songs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            lyrics TEXT NOT NULL,
            verse_order TEXT,
            authors TEXT,
            songbook_name TEXT,
            songbook_entry TEXT,
            theme_map TEXT,
            copyright TEXT,
            ccli_song_number TEXT,
            show_copyright INTEGER DEFAULT 0,
            key TEXT
        )''')
        cur.execute("CREATE INDEX IF NOT EXISTS idx_songs_title ON songs(title)")

        # Maps the random on-disk filename for an uploaded image to the original
        # human-readable name shown in the UI. library_visible=0 means the user
        # "deleted" the image from the library but a service still references it.
        cur.execute('''CREATE TABLE IF NOT EXISTS image_files (
            filename TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            library_visible INTEGER DEFAULT 1
        )''')

        # Per-output announcement layouts: reusable named looks (announcement
        # analogue of a text theme). Field values fill slots by position.
        cur.execute('''CREATE TABLE IF NOT EXISTS ann_layouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            output_name TEXT NOT NULL,
            name TEXT NOT NULL,
            slot_names TEXT NOT NULL DEFAULT '[]',
            text_boxes TEXT NOT NULL DEFAULT '[]',
            background_type TEXT DEFAULT 'color',
            background_value TEXT DEFAULT '#000000',
            sort_order INTEGER DEFAULT 0,
            tags TEXT NOT NULL DEFAULT '[]'
        )''')
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ann_layouts_output ON ann_layouts(output_name)")

        # Announcement library: self-contained items with ordered fields and a
        # per-output theme_map. Nestable folders re-home items on delete.
        cur.execute('''CREATE TABLE IF NOT EXISTS ann_folders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            sort_order INTEGER DEFAULT 0,
            parent_id INTEGER
        )''')
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ann_folders_parent ON ann_folders(parent_id)")
        cur.execute('''CREATE TABLE IF NOT EXISTS ann_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            folder_id INTEGER REFERENCES ann_folders(id) ON DELETE SET NULL,
            sort_order INTEGER DEFAULT 0,
            fields TEXT NOT NULL DEFAULT '[]',
            theme_map TEXT NOT NULL DEFAULT '{}',
            tags TEXT NOT NULL DEFAULT '[]'
        )''')
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ann_items_folder ON ann_items(folder_id)")

        cur.execute('''CREATE TABLE IF NOT EXISTS services (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            theme_map TEXT,
            group_id INTEGER,
            sort_order INTEGER DEFAULT 0
        )''')
        cur.execute('''CREATE TABLE IF NOT EXISTS service_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            sort_order INTEGER DEFAULT 0
        )''')
        cur.execute("CREATE INDEX IF NOT EXISTS idx_services_group ON services(group_id)")
        cur.execute('''CREATE TABLE IF NOT EXISTS service_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            service_id INTEGER NOT NULL,
            song_id INTEGER,
            order_num INTEGER NOT NULL,
            item_type TEXT DEFAULT 'song',
            data TEXT,
            FOREIGN KEY(service_id) REFERENCES services(id) ON DELETE CASCADE,
            FOREIGN KEY(song_id) REFERENCES songs(id) ON DELETE SET NULL
        )''')
        # Hot path: get_service_items / reorder / insert all filter by service_id.
        # (Existing DBs already have the table; CREATE INDEX IF NOT EXISTS still applies.)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_service_items_service ON service_items(service_id)")

        cur.execute('''CREATE TABLE IF NOT EXISTS bibles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            copyright TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        cur.execute('''CREATE TABLE IF NOT EXISTS verses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bible_id INTEGER NOT NULL,
            book TEXT NOT NULL,
            chapter INTEGER NOT NULL,
            verse_num INTEGER NOT NULL,
            text TEXT NOT NULL,
            FOREIGN KEY (bible_id) REFERENCES bibles (id) ON DELETE CASCADE
        )''')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_verses_lookup ON verses(bible_id, book, chapter, verse_num)')

        cur.execute('''CREATE TABLE IF NOT EXISTS image_folders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            sort_order INTEGER DEFAULT 0,
            parent_id INTEGER
        )''')
        cur.execute('''CREATE TABLE IF NOT EXISTS image_folder_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            folder_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            sort_order INTEGER DEFAULT 0,
            FOREIGN KEY (folder_id) REFERENCES image_folders(id) ON DELETE CASCADE
        )''')
        cur.execute("CREATE INDEX IF NOT EXISTS idx_image_folders_parent ON image_folders(parent_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_image_folder_items_folder ON image_folder_items(folder_id)")

        cur.execute('''CREATE TABLE IF NOT EXISTS video_folders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            sort_order INTEGER DEFAULT 0,
            parent_id INTEGER
        )''')
        cur.execute("CREATE INDEX IF NOT EXISTS idx_video_folders_parent ON video_folders(parent_id)")
        cur.execute('''CREATE TABLE IF NOT EXISTS video_folder_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            folder_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            sort_order INTEGER DEFAULT 0,
            FOREIGN KEY (folder_id) REFERENCES video_folders(id) ON DELETE CASCADE
        )''')
        cur.execute("CREATE INDEX IF NOT EXISTS idx_video_folder_items_folder ON video_folder_items(folder_id)")

        cur.execute('''CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )''')
        cur.execute('''CREATE TABLE IF NOT EXISTS outputs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_num INTEGER NOT NULL DEFAULT 0,
            data TEXT NOT NULL
        )''')
        cur.execute('''CREATE TABLE IF NOT EXISTS style_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            sort_order INTEGER DEFAULT 0,
            data TEXT NOT NULL DEFAULT '{}'
        )''')

        # One-shot: rewrite any pre-v2 announcement service items, then drop the
        # retired tables they depended on (and other orphan leftovers).
        self._convert_legacy_ann_service_items(cur)
        self._drop_retired_tables(cur)
        self._ensure_song_title_layouts_for_outputs(cur)

        conn.commit()

    @staticmethod
    def _table_exists(cur, name):
        cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,))
        return cur.fetchone() is not None

    def _convert_legacy_ann_service_items(self, cur):
        """Rewrite pre-v2 announcement service_items (template_id + field_values)
        into self-contained v2 snapshots (name + fields + theme_map). Must run
        before the template tables are dropped."""
        if not self._table_exists(cur, 'service_items'):
            return

        templates = {}
        if self._table_exists(cur, 'ann_templates'):
            for r in cur.execute("SELECT id, name, field_names FROM ann_templates"):
                raw = self._parse_json_field(r['field_names'], []) or []
                fields = []
                for f in raw:
                    if isinstance(f, dict):
                        fields.append({'name': str(f.get('name') or ''),
                                       'default': str(f.get('default') or '')})
                    else:
                        fields.append({'name': str(f), 'default': ''})
                templates[r['id']] = {'name': r['name'], 'field_names': fields}

        converted = 0
        cur.execute("SELECT id, data FROM service_items WHERE item_type = 'announcement' AND data IS NOT NULL")
        rows = cur.fetchall()
        for row in rows:
            try:
                adata = json.loads(row['data'])
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(adata, dict):
                continue
            if adata.get('fields') is not None:
                continue
            if 'template_id' not in adata and 'field_values' not in adata:
                continue

            tmpl = templates.get(adata.get('template_id')) if adata.get('template_id') else None
            tfields = (tmpl['field_names'] if tmpl else []) or []
            values = adata.get('field_values')
            if isinstance(values, list):
                values = {f['name']: v for f, v in zip(tfields, values)}
            values = values if isinstance(values, dict) else {}

            fields = [{'label': f['name'], 'value': str(values.get(f['name'], '') or '')}
                      for f in tfields]
            name = adata.get('title') or adata.get('name') or (fields[0]['value'] if fields else '')
            theme_map = dict(adata.get('theme_map') or {})
            if not isinstance(theme_map, dict):
                theme_map = {}

            # Best-effort: map outputs without a layout id to the ann_layouts row
            # seeded from this template's name (original migrate naming).
            tmpl_name = tmpl['name'] if tmpl else None
            if tmpl_name and self._table_exists(cur, 'ann_layouts'):
                cur.execute("SELECT id, output_name FROM ann_layouts WHERE name = ?", (tmpl_name,))
                for lr in cur.fetchall():
                    out_name = lr['output_name']
                    entry = theme_map.get(out_name)
                    if not isinstance(entry, dict):
                        entry = {}
                        theme_map[out_name] = entry
                    if not entry.get('layout'):
                        entry['layout'] = lr['id']

            new_data = {
                'name': name,
                'fields': fields,
                'theme_map': theme_map,
            }
            cur.execute("UPDATE service_items SET data = ? WHERE id = ?",
                        (json.dumps(new_data), row['id']))
            converted += 1

        if converted:
            logger.info("Converted %d legacy announcement service item(s) to v2", converted)

    def _drop_retired_tables(self, cur):
        """Drop tables that no code path reads or writes after the v2 cutover."""
        # Child before parent for the old template layout FK.
        for table in ('announcements', 'names', 'themes',
                      'ann_template_layouts', 'ann_templates'):
            cur.execute(f"DROP TABLE IF EXISTS {table}")
        cur.execute(
            "DELETE FROM app_settings WHERE key IN "
            "('ann_layouts_migrated', 'title_layouts_migrated', "
            "'ann_layout_units_px', 'title_layouts_seeded')"
        )

    def ensure_song_title_layout(self, output_name, canvas_w=1920, canvas_h=1080):
        """Insert a ready-to-use "Song Title" layout for an output if missing."""
        with self._db_transaction() as cur:
            self._ensure_song_title_layout(cur, output_name, canvas_w, canvas_h)

    def _ensure_song_title_layout(self, cur, output_name, canvas_w=1920, canvas_h=1080):
        cur.execute("SELECT 1 FROM ann_layouts WHERE output_name = ? AND name = 'Song Title'",
                    (output_name,))
        if cur.fetchone():
            return
        cur.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM ann_layouts WHERE output_name = ?",
                    (output_name,))
        so = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO ann_layouts (output_name, name, slot_names, text_boxes, "
            "background_type, background_value, sort_order) "
            "VALUES (?, 'Song Title', '[]', ?, 'transparent', '', ?)",
            (output_name, json.dumps(_seed_boxes_px(canvas_w, canvas_h)), so))

    def _ensure_song_title_layouts_for_outputs(self, cur):
        """Give every existing output a Song Title layout when absent."""
        if not self._table_exists(cur, 'outputs') or not self._table_exists(cur, 'ann_layouts'):
            return
        cur.execute("SELECT data FROM outputs")
        for r in cur.fetchall():
            try:
                d = json.loads(r['data'])
            except (json.JSONDecodeError, TypeError):
                continue
            name = d.get('name')
            if not name:
                continue
            cw = float(d.get('canvas_width') or 1920)
            ch = float(d.get('canvas_height') or 1080)
            self._ensure_song_title_layout(cur, name, cw, ch)

    # Ordered song columns shared by INSERT (add_song) and UPDATE (update_song).
    _SONG_COLUMNS = ('title', 'lyrics', 'verse_order', 'authors', 'songbook_name',
                     'songbook_entry', 'theme_map', 'copyright', 'ccli_song_number',
                     'show_copyright', 'key')

    @staticmethod
    def _song_values(title, lyrics, verse_order, authors, songbook_name, songbook_entry,
                     theme_map, copyright, ccli_song_number, show_copyright, key):
        """Build the ordered value list matching _SONG_COLUMNS (with JSON/bool encoding)."""
        return [title, lyrics, verse_order, json.dumps(authors if authors is not None else []),
                songbook_name, songbook_entry, json.dumps(theme_map or {}), copyright,
                ccli_song_number, 1 if show_copyright else 0, key]

    def add_song(self, title, lyrics, verse_order=None, authors=None, songbook_name="", songbook_entry="", theme_map=None, copyright="", ccli_song_number="", show_copyright=False, key=""):
        values = self._song_values(title, lyrics, verse_order, authors, songbook_name,
                                    songbook_entry, theme_map, copyright, ccli_song_number, show_copyright, key)
        cols = ", ".join(self._SONG_COLUMNS)
        placeholders = ", ".join("?" * len(self._SONG_COLUMNS))
        with self._db_transaction() as cur:
            cur.execute(f"INSERT INTO songs ({cols}) VALUES ({placeholders})", values)
            return cur.lastrowid

    def update_song(self, song_id, title, lyrics, verse_order=None, authors=None, songbook_name="", songbook_entry="", theme_map=None, copyright="", ccli_song_number="", show_copyright=False, key=""):
        values = self._song_values(title, lyrics, verse_order, authors, songbook_name,
                                   songbook_entry, theme_map, copyright, ccli_song_number, show_copyright, key)
        set_clause = ", ".join(f"{c} = ?" for c in self._SONG_COLUMNS)
        with self._db_transaction() as cur:
            cur.execute(f"UPDATE songs SET {set_clause} WHERE id = ?", values + [song_id])

    def get_all_songs_summary(self):
        """Returns all songs without the lyrics column for lightweight library listing."""
        with self._db_transaction(commit=False) as cur:
            cur.execute(
                "SELECT id, title, verse_order, authors, songbook_name, songbook_entry, "
                "theme_map, copyright, ccli_song_number, show_copyright, key "
                "FROM songs ORDER BY title ASC"
            )
            rows = cur.fetchall()
            return [self._parse_entity_data(dict(r)) for r in rows]

    # ---- Per-output announcement layouts --------------------------------------
    # A layout is the announcement analogue of a text theme: it belongs to one
    # output, names an ordered set of slots, and positions them via the shared
    # box/lines model. An announcement item's field values fill the slots by order
    # (item field i → slot i), so layouts are reusable across items.

    def _normalize_ann_layout(self, row):
        """Row → layout dict: slot_names (list of str), slot_count, normalized
        box/lines. Legacy-shaped boxes convert on read, keyed by the slot names."""
        d = dict(row)
        slot_names = [str(s) for s in (self._parse_json_field(d.get('slot_names'), []) or [])]
        d['slot_names'] = slot_names
        d['slot_count'] = len(slot_names)
        fields = [{'name': n, 'default': ''} for n in slot_names]
        d['text_boxes'] = _normalize_ann_boxes(self._parse_json_field(d.get('text_boxes'), []), fields)
        d['background_type'] = d.get('background_type') or 'color'
        if d.get('background_value') is None:
            d['background_value'] = '#000000'
        d['tags'] = [str(t) for t in (self._parse_json_field(d.get('tags'), []) or [])]
        return d

    def get_ann_layouts(self, output_name):
        """Every layout belonging to one output, ordered for display."""
        with self._db_transaction(commit=False) as cur:
            cur.execute("SELECT * FROM ann_layouts WHERE output_name = ? "
                        "ORDER BY sort_order, name COLLATE NOCASE", (output_name,))
            return [self._normalize_ann_layout(r) for r in cur.fetchall()]

    def get_ann_layout(self, layout_id):
        with self._db_transaction(commit=False) as cur:
            cur.execute("SELECT * FROM ann_layouts WHERE id = ?", (layout_id,))
            r = cur.fetchone()
            return self._normalize_ann_layout(r) if r else None

    def get_ann_layouts_summary(self, output_name):
        """Lightweight per-output layout list for the pickers: id, name, slot_count
        (no boxes). Sent in the broadcast state so item/service editors can offer a
        per-output layout choice without a separate fetch."""
        with self._db_transaction(commit=False) as cur:
            cur.execute("SELECT id, name, slot_names, tags FROM ann_layouts WHERE output_name = ? "
                        "ORDER BY sort_order, name COLLATE NOCASE", (output_name,))
            return [{'id': r['id'], 'name': r['name'],
                     'slot_count': len(self._parse_json_field(r['slot_names'], []) or []),
                     'tags': [str(t) for t in (self._parse_json_field(r['tags'], []) or [])]}
                    for r in cur.fetchall()]

    def create_ann_layout(self, output_name, name, slot_names, text_boxes,
                          background_type='color', background_value='#000000', tags=None):
        with self._db_transaction() as cur:
            cur.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM ann_layouts "
                        "WHERE output_name = ?", (output_name,))
            sort_order = cur.fetchone()[0]
            cur.execute('''INSERT INTO ann_layouts
                    (output_name, name, slot_names, text_boxes,
                     background_type, background_value, sort_order, tags)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                (output_name, name, json.dumps(list(slot_names or [])),
                 json.dumps(text_boxes or []), background_type or 'color',
                 background_value if background_value is not None else '#000000', sort_order,
                 json.dumps(list(tags or []))))
            return cur.lastrowid

    def update_ann_layout(self, layout_id, name, slot_names, text_boxes,
                          background_type='color', background_value='#000000', tags=None):
        """tags=None leaves the stored tags unchanged; a list — including [] —
        replaces them (mirrors update_ann_item)."""
        with self._db_transaction() as cur:
            cur.execute('''UPDATE ann_layouts
                    SET name = ?, slot_names = ?, text_boxes = ?,
                        background_type = ?, background_value = ?
                    WHERE id = ?''',
                (name, json.dumps(list(slot_names or [])), json.dumps(text_boxes or []),
                 background_type or 'color',
                 background_value if background_value is not None else '#000000', layout_id))
            if tags is not None:
                cur.execute("UPDATE ann_layouts SET tags = ? WHERE id = ?",
                            (json.dumps(list(tags)), layout_id))

    def delete_ann_layout(self, layout_id):
        with self._db_transaction() as cur:
            cur.execute("DELETE FROM ann_layouts WHERE id = ?", (layout_id,))

    # ---- Announcement library: folders ----------------------------------------
    # Nestable folders mirroring the image library. Items reference a folder via
    # ann_items.folder_id (ON DELETE SET NULL), so deleting a folder re-homes its
    # items to the top level instead of destroying them.

    def get_ann_folders(self):
        """Flat list of every library folder; the client builds the tree from parent_id."""
        with self._db_transaction(commit=False) as cur:
            cur.execute("SELECT id, name, sort_order, parent_id FROM ann_folders "
                        "ORDER BY sort_order ASC, id ASC")
            return [dict(r) for r in cur.fetchall()]

    # Nestable library folder tables (announcements / images / videos). Methods below
    # share create/rename/move against this whitelist; delete stays specialized because
    # each kind has different cascade side effects (re-home items vs return filenames).
    _NESTABLE_FOLDER_TABLES = {
        'ann': 'ann_folders',
        'image': 'image_folders',
        'video': 'video_folders',
    }
    _ALLOWED_FOLDER_TABLES = frozenset(_NESTABLE_FOLDER_TABLES.values())

    def _folder_table(self, kind):
        table = self._NESTABLE_FOLDER_TABLES.get(kind)
        if table is None:
            raise ValueError(f"Unknown folder kind: {kind!r}")
        return table

    def _create_nestable_folder(self, kind, name, parent_id=None):
        """Insert a nestable folder; sort_order is scoped per parent."""
        table = self._folder_table(kind)
        with self._db_transaction() as cur:
            cur.execute(
                f"SELECT COALESCE(MAX(sort_order), -1) + 1 FROM {table} WHERE parent_id IS ?",
                (parent_id,))
            next_order = cur.fetchone()[0]
            cur.execute(
                f"INSERT INTO {table} (name, sort_order, parent_id) VALUES (?, ?, ?)",
                (name, next_order, parent_id))
            return cur.lastrowid

    def _rename_nestable_folder(self, kind, folder_id, name):
        table = self._folder_table(kind)
        with self._db_transaction() as cur:
            cur.execute(f"UPDATE {table} SET name = ? WHERE id = ?", (name, folder_id))

    def _move_nestable_folder(self, kind, folder_id, new_parent_id, ordered_ids=None):
        """Re-parent a folder (None = top level) and optionally reorder the destination
        parent's children. Rejects moves that would create a cycle. Returns True/False."""
        table = self._folder_table(kind)
        with self._db_transaction() as cur:
            if new_parent_id is not None:
                if new_parent_id in self._descendant_folder_ids(cur, folder_id, table):
                    return False
            cur.execute(f"UPDATE {table} SET parent_id = ? WHERE id = ?", (new_parent_id, folder_id))
            if ordered_ids:
                cur.executemany(
                    f"UPDATE {table} SET sort_order = ? WHERE id = ?",
                    list(enumerate(ordered_ids)))
            return True

    def create_ann_folder(self, name, parent_id=None):
        return self._create_nestable_folder('ann', name, parent_id)

    def rename_ann_folder(self, folder_id, name):
        self._rename_nestable_folder('ann', folder_id, name)

    def move_ann_folder(self, folder_id, new_parent_id, ordered_ids=None):
        """Re-parent a folder (None = top level) and optionally reorder the destination
        parent's children. Rejects moves that would create a cycle (into self or a
        descendant). Returns True on success, False if rejected."""
        return self._move_nestable_folder('ann', folder_id, new_parent_id, ordered_ids)

    def delete_ann_folder(self, folder_id):
        """Delete a folder and every nested subfolder. Items anywhere in the deleted
        subtree are re-homed to the top level (folder_id -> NULL via ON DELETE SET
        NULL), never destroyed."""
        self.delete_ann_folders([folder_id])

    def delete_ann_folders(self, folder_ids):
        """Batch form of delete_ann_folder: delete several folders (with their nested
        subfolders) in ONE transaction, deduplicating overlapping subtrees (deleting a
        folder together with one of its descendants must not double-delete)."""
        if not folder_ids:
            return
        with self._db_transaction() as cur:
            seen = set()
            ids = []
            for fid in folder_ids:
                for did in self._descendant_folder_ids(cur, fid, 'ann_folders'):
                    if did not in seen:
                        seen.add(did)
                        ids.append(did)
            placeholders = ','.join('?' * len(ids))
            cur.execute(f"DELETE FROM ann_folders WHERE id IN ({placeholders})", ids)

    # ---- Announcement library: items (v2 model) --------------------------------
    # A first-class, self-contained announcement: its own ordered fields
    # [{label, value}] plus a theme_map ({output_name: {'layout': id, 'bg': id}})
    # that picks a layout + background per output, mirroring a song's theme_map.

    def _normalize_ann_item(self, row):
        d = dict(row)
        d['fields'] = _normalize_ann_item_fields(self._parse_json_field(d.get('fields'), []))
        d['theme_map'] = self._parse_json_field(d.get('theme_map'), {}) or {}
        d['tags'] = [str(t) for t in (self._parse_json_field(d.get('tags'), []) or [])]
        return d

    def get_ann_items(self):
        """Every library item, normalized. Ordered by sort_order then id; sort_order
        is folder-scoped, so grouping by folder_id client-side preserves each
        bucket's order."""
        with self._db_transaction(commit=False) as cur:
            cur.execute("SELECT id, name, folder_id, sort_order, fields, theme_map, tags "
                        "FROM ann_items ORDER BY sort_order ASC, id ASC")
            return [self._normalize_ann_item(r) for r in cur.fetchall()]

    def get_ann_item(self, item_id):
        with self._db_transaction(commit=False) as cur:
            cur.execute("SELECT id, name, folder_id, sort_order, fields, theme_map, tags "
                        "FROM ann_items WHERE id = ?", (item_id,))
            r = cur.fetchone()
            return self._normalize_ann_item(r) if r else None

    def create_ann_item(self, name, folder_id=None, fields=None, theme_map=None, tags=None):
        with self._db_transaction() as cur:
            cur.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM ann_items WHERE folder_id IS ?",
                        (folder_id,))
            next_order = cur.fetchone()[0]
            cur.execute("INSERT INTO ann_items (name, folder_id, sort_order, fields, theme_map, tags) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (name, folder_id, next_order,
                         json.dumps(_normalize_ann_item_fields(fields)),
                         json.dumps(theme_map or {}), json.dumps(list(tags or []))))
            return cur.lastrowid

    def update_ann_item(self, item_id, name, fields, theme_map, tags=None):
        """tags=None leaves the stored tags unchanged (a request that doesn't carry
        the key must not clear them); a list — including [] — replaces them."""
        with self._db_transaction() as cur:
            if tags is None:
                cur.execute("UPDATE ann_items SET name = ?, fields = ?, theme_map = ? WHERE id = ?",
                            (name, json.dumps(_normalize_ann_item_fields(fields)),
                             json.dumps(theme_map or {}), item_id))
            else:
                cur.execute("UPDATE ann_items SET name = ?, fields = ?, theme_map = ?, tags = ? "
                            "WHERE id = ?",
                            (name, json.dumps(_normalize_ann_item_fields(fields)),
                             json.dumps(theme_map or {}), json.dumps(list(tags)), item_id))

    def duplicate_ann_item(self, item_id):
        """Copy an item into the same folder, placed immediately after the source so
        the copy lands next to the original. Name gets a ' Copy' suffix; fields and
        theme_map are carried over verbatim (already-valid JSON is reused as-is).
        Returns the new item id, or None if the source no longer exists."""
        with self._db_transaction() as cur:
            cur.execute("SELECT name, folder_id, sort_order, fields, theme_map, tags "
                        "FROM ann_items WHERE id = ?", (item_id,))
            row = cur.fetchone()
            if not row:
                return None
            src = dict(row)
            folder_id, src_order = src['folder_id'], src['sort_order']
            # Open a slot right after the source within its folder bucket.
            cur.execute("UPDATE ann_items SET sort_order = sort_order + 1 "
                        "WHERE folder_id IS ? AND sort_order > ?", (folder_id, src_order))
            cur.execute("INSERT INTO ann_items (name, folder_id, sort_order, fields, theme_map, tags) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (src['name'] + ' Copy', folder_id, src_order + 1,
                         src['fields'], src['theme_map'], src['tags'] or '[]'))
            return cur.lastrowid

    def delete_ann_item(self, item_id):
        with self._db_transaction() as cur:
            cur.execute("DELETE FROM ann_items WHERE id = ?", (item_id,))

    def delete_ann_items(self, item_ids):
        """Batch delete library announcement items in one transaction. Returns the
        number deleted."""
        if not item_ids:
            return 0
        with self._db_transaction() as cur:
            placeholders = ','.join('?' * len(item_ids))
            cur.execute(f"DELETE FROM ann_items WHERE id IN ({placeholders})", list(item_ids))
            return cur.rowcount

    def move_ann_item(self, item_id, folder_id, ordered_ids=None):
        """Move an item into a folder (None = top level) and optionally reorder the
        destination bucket's items."""
        with self._db_transaction() as cur:
            cur.execute("UPDATE ann_items SET folder_id = ? WHERE id = ?", (folder_id, item_id))
            if ordered_ids:
                cur.executemany("UPDATE ann_items SET sort_order = ? WHERE id = ?",
                                list(enumerate(ordered_ids)))

    def add_announcement_to_service(self, service_id, name, fields, theme_map=None, at_index=None):
        """Insert a v2 announcement snapshot: self-contained name + ordered fields
        ([{label, value}]) + per-output layout/background theme_map. The snapshot is
        independent of any library item, so later library edits never touch it."""
        data = {'name': name, 'fields': _normalize_ann_item_fields(fields)}
        if theme_map:
            data['theme_map'] = theme_map
        with self._db_transaction() as cur:
            order = self._open_insert_orders(cur, service_id, at_index, 1)[0]
            self._insert_service_item(cur, service_id, 'announcement', data, order_num=order)

    def add_announcements_to_service(self, service_id, item_ids, at_index=None):
        """Batch add: snapshot several library announcements as service items in one
        transaction (mirrors add_songs_to_service). Each snapshot is identical to what
        the single add produces from a bare item_id. Ids that no longer exist are
        skipped; the rest land consecutively (at at_index, or appended) in list order.
        Returns the number added."""
        if not item_ids:
            return 0
        with self._db_transaction() as cur:
            placeholders = ','.join('?' * len(item_ids))
            cur.execute(f"SELECT id, name, fields, theme_map FROM ann_items WHERE id IN ({placeholders})",
                        list(item_ids))
            rows_by_id = {r['id']: r for r in cur.fetchall()}
            valid_ids = [iid for iid in item_ids if iid in rows_by_id]
            orders = self._open_insert_orders(cur, service_id, at_index, len(valid_ids))
            for order, iid in zip(orders, valid_ids, strict=True):
                row = rows_by_id[iid]
                data = {'name': row['name'] or 'Announcement',
                        'fields': _normalize_ann_item_fields(self._parse_json_field(row['fields'], []))}
                theme_map = self._parse_json_field(row['theme_map'], {}) or {}
                if theme_map:
                    data['theme_map'] = theme_map
                self._insert_service_item(cur, service_id, 'announcement', data, order_num=order)
            return len(valid_ids)

    def get_service(self, service_id):
        with self._db_transaction(commit=False) as cur:
            cur.execute("SELECT * FROM services WHERE id = ?", (service_id,))
            row = cur.fetchone()
            if not row:
                return None
            return self._parse_entity_data(dict(row))

    def get_song(self, song_id):
        with self._db_transaction(commit=False) as cur:
            cur.execute("SELECT * FROM songs WHERE id = ?", (song_id,))
            row = cur.fetchone()
            if not row:
                return None
            return self._parse_entity_data(dict(row))

    def delete_song(self, song_id):
        """Delete a library song while preserving any service items that reference it.

        Each referencing service row is given a full lyric snapshot and its song_id is
        cleared before the songs row is removed, so planned services keep their order
        and content even after the library entry is gone (replaces ON DELETE CASCADE
        wipe-out). Existing DBs may still declare CASCADE; detaching first makes both
        FK policies safe.
        """
        with self._db_transaction() as cur:
            self._detach_songs_from_services(cur, [song_id])
            cur.execute("DELETE FROM songs WHERE id = ?", (song_id,))

    def create_service(self, name, group_id=None):
        with self._db_transaction() as cur:
            # Append to the end of its bucket (group, or the ungrouped list).
            cur.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM services WHERE group_id IS ?",
                (group_id,)
            )
            next_order = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO services (name, theme_map, group_id, sort_order) VALUES (?, ?, ?, ?)",
                (name, json.dumps({}), group_id, next_order)
            )
            return cur.lastrowid

    def delete_service(self, service_id):
        with self._db_transaction() as cur:
            cur.execute("DELETE FROM services WHERE id = ?", (service_id,))

    def rename_service(self, service_id, new_name):
        with self._db_transaction() as cur:
            cur.execute("UPDATE services SET name = ? WHERE id = ?", (new_name, service_id))

    def get_all_services(self):
        with self._db_transaction(commit=False) as cur:
            # sort_order ASC orders manually-arranged buckets; id DESC keeps the default
            # (no explicit order) newest-first, preserving prior behavior.
            cur.execute("SELECT * FROM services ORDER BY sort_order ASC, id DESC")
            rows = cur.fetchall()
            return [self._parse_entity_data(dict(r)) for r in rows]

    # ---- Service groups (one-level folders for organizing services) ----
    def get_service_groups(self):
        with self._db_transaction(commit=False) as cur:
            cur.execute("SELECT id, name, sort_order FROM service_groups ORDER BY sort_order ASC, id ASC")
            return [dict(r) for r in cur.fetchall()]

    def create_service_group(self, name):
        with self._db_transaction() as cur:
            cur.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM service_groups")
            next_order = cur.fetchone()[0]
            cur.execute("INSERT INTO service_groups (name, sort_order) VALUES (?, ?)", (name, next_order))
            return cur.lastrowid

    def rename_service_group(self, group_id, name):
        with self._db_transaction() as cur:
            cur.execute("UPDATE service_groups SET name = ? WHERE id = ?", (name, group_id))

    def delete_service_group(self, group_id):
        """Delete a group but keep its services, moving them back to the ungrouped list."""
        with self._db_transaction() as cur:
            cur.execute("UPDATE services SET group_id = NULL WHERE group_id = ?", (group_id,))
            cur.execute("DELETE FROM service_groups WHERE id = ?", (group_id,))

    def move_service_to_group(self, service_id, group_id, ordered_ids=None):
        """Re-bucket a service (group_id None = ungrouped) and optionally apply an explicit
        order to that destination bucket's services."""
        with self._db_transaction() as cur:
            if not ordered_ids:
                # Append to the end of the destination bucket.
                cur.execute(
                    "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM services WHERE group_id IS ?",
                    (group_id,)
                )
                next_order = cur.fetchone()[0]
                cur.execute("UPDATE services SET group_id = ?, sort_order = ? WHERE id = ?",
                            (group_id, next_order, service_id))
            else:
                cur.execute("UPDATE services SET group_id = ? WHERE id = ?", (group_id, service_id))
                cur.executemany(
                    "UPDATE services SET sort_order = ? WHERE id = ?",
                    list(enumerate(ordered_ids))
                )

    def update_service_theme_map(self, service_id, theme_map):
        with self._db_transaction() as cur:
            cur.execute("UPDATE services SET theme_map = ? WHERE id = ?", (json.dumps(theme_map or {}), service_id))

    def _get_next_service_item_order(self, cur, service_id):
        """Helper to get the next order number for a service item."""
        cur.execute("SELECT MAX(order_num) FROM service_items WHERE service_id = ?", (service_id,))
        val = cur.fetchone()[0]
        return (val + 1) if val is not None else 0

    def _open_insert_orders(self, cur, service_id, at_index, count):
        """Return `count` consecutive order_num values for inserting new items so the
        first lands at position `at_index` (0-based over the service's current items,
        as ordered by order_num). Existing items at or after that position are shifted
        up by `count` to make room. `at_index` of None (or >= the item count) appends
        at the end without shifting anything.

        Because every item at/after the target order is bumped by `count`, the returned
        contiguous block [base, base+count) is guaranteed collision-free."""
        if count <= 0:
            return []
        cur.execute(
            "SELECT order_num FROM service_items WHERE service_id = ? ORDER BY order_num ASC",
            (service_id,)
        )
        orders = [r['order_num'] for r in cur.fetchall()]
        if at_index is None or at_index >= len(orders):
            start = (orders[-1] + 1) if orders else 0
            return list(range(start, start + count))
        if at_index < 0:
            at_index = 0
        base = orders[at_index]
        cur.execute(
            "UPDATE service_items SET order_num = order_num + ? WHERE service_id = ? AND order_num >= ?",
            (count, service_id, base)
        )
        return list(range(base, base + count))

    def _insert_service_item(self, cur, service_id, item_type, data, song_id=None, order_num=None):
        """Insert a service item of the given type, JSON-encoding its data payload.
        `order_num` None appends at the end; pass an explicit value (e.g. from
        _open_insert_orders) to place the item at a chosen position."""
        if order_num is None:
            order_num = self._get_next_service_item_order(cur, service_id)
        cur.execute(
            "INSERT INTO service_items (service_id, song_id, order_num, item_type, data) VALUES (?, ?, ?, ?, ?)",
            (service_id, song_id, order_num, item_type, json.dumps(data))
        )
        return cur.lastrowid

    def _song_row_to_service_snapshot(self, row, user_modified=False):
        """Build a self-contained service-item `data` dict from a songs table row.

        Service items are independent copies: lyrics, themes, and copyright metadata
        live in this snapshot so library edits/deletes cannot change a planned service.
        `song_id` on the service_items row is provenance for Reset only.
        """
        return {
            'user_modified': bool(user_modified),
            'title': (row['title'] if row['title'] is not None else '') if row else '',
            'lyrics': (row['lyrics'] if row['lyrics'] is not None else '') if row else '',
            'verse_order': (row['verse_order'] if row and row['verse_order'] is not None else '') or '',
            'theme_map': self._parse_json_field(row['theme_map'], {}) if row else {},
            'authors': self._parse_json_field(row['authors'], []) if row else [],
            'songbook_name': (row['songbook_name'] or '') if row else '',
            'songbook_entry': (row['songbook_entry'] or '') if row else '',
            'copyright': (row['copyright'] or '') if row else '',
            'ccli_song_number': (row['ccli_song_number'] or '') if row else '',
            'show_copyright': bool(row['show_copyright']) if row else False,
            'key': (row['key'] or '') if row else '',
        }

    def _fill_song_snapshot_gaps(self, snap, song_row):
        """Fill missing snapshot keys from a library row without overwriting present ones."""
        lib = self._song_row_to_service_snapshot(song_row, user_modified=False)
        out = dict(snap or {})
        for k, v in lib.items():
            if k == 'user_modified':
                continue
            if k not in out:
                out[k] = v
        return out

    def _detach_songs_from_services(self, cur, song_ids):
        """Snapshot + null song_id for every service_items row pointing at song_ids.

        After this, deleting the songs row cannot cascade-remove (or orphan without
        content) those service entries — `_apply_song_item` reads the snapshot.
        """
        if not song_ids:
            return
        placeholders = ','.join('?' * len(song_ids))
        cur.execute(
            f"SELECT id, title, lyrics, verse_order, theme_map, authors, songbook_name, "
            f"songbook_entry, copyright, ccli_song_number, show_copyright, key "
            f"FROM songs WHERE id IN ({placeholders})",
            list(song_ids))
        songs_by_id = {r['id']: r for r in cur.fetchall()}
        if not songs_by_id:
            return
        cur.execute(
            f"SELECT id, song_id, data FROM service_items "
            f"WHERE item_type = 'song' AND song_id IN ({placeholders})",
            list(song_ids))
        for row in cur.fetchall():
            song = songs_by_id.get(row['song_id'])
            if not song:
                continue
            snap = self._parse_json_field(row['data'], {}) or {}
            # Prefer existing snapshot fields (per-service edits); fill gaps from the
            # library row so a never-overridden item still has full content after detach.
            new_snap = self._fill_song_snapshot_gaps(snap, song)
            new_snap['user_modified'] = True
            cur.execute(
                "UPDATE service_items SET song_id = NULL, data = ? WHERE id = ?",
                (json.dumps(new_snap), row['id']))

    def add_song_to_service(self, service_id, song_id, at_index=None):
        with self._db_transaction() as cur:
            cur.execute(
                "SELECT title, lyrics, verse_order, theme_map, authors, songbook_name, "
                "songbook_entry, copyright, ccli_song_number, show_copyright, key "
                "FROM songs WHERE id = ?", (song_id,))
            row = cur.fetchone()
            snapshot = self._song_row_to_service_snapshot(row, user_modified=False)
            order = self._open_insert_orders(cur, service_id, at_index, 1)[0]
            self._insert_service_item(cur, service_id, 'song', snapshot, song_id=song_id, order_num=order)

    def add_songs_to_service(self, service_id, song_ids, at_index=None):
        """Batch add: snapshot each song fully into service item data, all in one
        transaction. Skips ids that don't exist. `at_index` None appends; otherwise
        the songs land consecutively starting at that position."""
        if not song_ids:
            return 0
        with self._db_transaction() as cur:
            placeholders = ','.join('?' * len(song_ids))
            cur.execute(
                f"SELECT id, title, lyrics, verse_order, theme_map, authors, songbook_name, "
                f"songbook_entry, copyright, ccli_song_number, show_copyright, key "
                f"FROM songs WHERE id IN ({placeholders})",
                list(song_ids))
            rows_by_id = {r['id']: r for r in cur.fetchall()}
            valid_ids = [sid for sid in song_ids if sid in rows_by_id]
            orders = self._open_insert_orders(cur, service_id, at_index, len(valid_ids))
            for order, sid in zip(orders, valid_ids, strict=True):
                snapshot = self._song_row_to_service_snapshot(rows_by_id[sid], user_modified=False)
                self._insert_service_item(cur, service_id, 'song', snapshot, song_id=sid, order_num=order)
            return len(valid_ids)

    def add_bible_to_service(self, service_id, bible_data, at_index=None):
        with self._db_transaction() as cur:
            order = self._open_insert_orders(cur, service_id, at_index, 1)[0]
            self._insert_service_item(cur, service_id, 'bible', bible_data, order_num=order)

    def add_video_to_service(self, service_id, video_data, at_index=None):
        with self._db_transaction() as cur:
            order = self._open_insert_orders(cur, service_id, at_index, 1)[0]
            self._insert_service_item(cur, service_id, 'video', video_data, order_num=order)

    def remove_items_from_service(self, item_ids):
        """Batch delete service items in one transaction. Returns the number deleted."""
        if not item_ids:
            return 0
        with self._db_transaction() as cur:
            placeholders = ','.join('?' * len(item_ids))
            cur.execute(f"DELETE FROM service_items WHERE id IN ({placeholders})", list(item_ids))
            return cur.rowcount

    def delete_songs(self, song_ids):
        """Batch delete library songs in one transaction. Returns the number deleted.
        Service items that referenced these songs are snapshotted and kept (see
        delete_song)."""
        if not song_ids:
            return 0
        with self._db_transaction() as cur:
            self._detach_songs_from_services(cur, list(song_ids))
            placeholders = ','.join('?' * len(song_ids))
            cur.execute(f"DELETE FROM songs WHERE id IN ({placeholders})", list(song_ids))
            return cur.rowcount

    def remove_item_from_service(self, item_id):
        with self._db_transaction() as cur:
            cur.execute("DELETE FROM service_items WHERE id = ?", (item_id,))

    def reorder_service_items(self, service_id, ordered_item_ids):
        """Update order_num for service items based on a new ordered list of item IDs."""
        with self._db_transaction() as cur:
            cur.executemany(
                "UPDATE service_items SET order_num = ? WHERE id = ? AND service_id = ?",
                [(new_order, item_id, service_id) for new_order, item_id in enumerate(ordered_item_ids)]
            )

    def update_service_item(self, item_id, overrides):
        """Store per-service-item overrides in the data column.
        overrides is a dict like {title, lyrics, verse_order}.
        Pass None or empty dict to clear overrides."""
        with self._db_transaction() as cur:
            if overrides:
                cur.execute("UPDATE service_items SET data = ? WHERE id = ?",
                           (json.dumps(overrides), item_id))
            else:
                cur.execute("UPDATE service_items SET data = NULL WHERE id = ?", (item_id,))

    def _reset_service_item_data(self, item_type, existing_data, song_id):
        """Re-snapshot a service item from its library source (reset path).

        Returns (new_data_dict_or_None, error_message_or_None). error is set when
        a song reset is requested but the library song is gone (song_id null/missing).
        """
        if item_type == 'bible':
            # Keep ref/bible_id fields but remove overrides
            existing_data.pop('theme_map', None)
            return (existing_data if existing_data else None), None
        if item_type == 'song':
            if not song_id:
                return None, "This service song is no longer linked to the library (the library song was deleted). Reset is unavailable."
            with self._db_transaction(commit=False) as cur:
                cur.execute(
                    "SELECT title, lyrics, verse_order, theme_map, authors, songbook_name, "
                    "songbook_entry, copyright, ccli_song_number, show_copyright, key "
                    "FROM songs WHERE id = ?", (song_id,))
                srow = cur.fetchone()
            if not srow:
                return None, "The linked library song no longer exists. Reset is unavailable."
            return self._song_row_to_service_snapshot(srow, user_modified=False), None
        if item_type == 'announcement':
            # Announcement items are self-contained snapshots (name / fields /
            # theme_map), so a reset just drops the per-item layout+background
            # override (theme_map) and keeps the content, so it falls back to each
            # output's defaults — mirroring the bible reset above.
            existing_data.pop('theme_map', None)
            return (existing_data if existing_data else None), None
        # Video / image / divider / etc.: strip theme_map only; keep media payload
        # (filename, autoplay, loop, …). Never NULL out data on reset.
        existing_data.pop('theme_map', None)
        return (existing_data if existing_data else None), None

    def compute_updated_service_item_data(self, update, item_type, existing_data, song_id):
        """Build the new `data` payload for a service-item update.

        On reset, re-snapshots from the library (songs) or strips overrides
        (bible/announcement/video/etc.); otherwise merges the provided
        title/lyrics/verse_order/theme_map onto the existing payload.

        Returns (new_data_or_None, error_message_or_None).
        """
        if update.get('reset'):
            return self._reset_service_item_data(item_type, existing_data, song_id)

        if item_type == 'bible':
            new_data = dict(existing_data)
        elif item_type in ('song', 'announcement'):
            # Preserve all snapshot fields; apply updates on top
            new_data = dict(existing_data)
            new_data['user_modified'] = True
        else:
            # Preserve media payload (filename, autoplay, …) for video/image/etc.
            new_data = dict(existing_data)
        for key in ('title', 'lyrics', 'verse_order', 'theme_map',
                    'authors', 'songbook_name', 'songbook_entry',
                    'copyright', 'ccli_song_number', 'show_copyright', 'key'):
            if key in update:
                new_data[key] = update[key]
        return new_data, None

    def backfill_song_snapshot_copyright(self):
        """One-shot: fill missing copyright/metadata keys on song service-item snapshots
        from the linked library row, then flag app_settings so get_service_items stays
        read-only. Idempotent — skips when song_snapshot_copyright_backfilled is set."""
        settings = self.load_app_settings()
        if settings.get('song_snapshot_copyright_backfilled'):
            return
        with self._db_transaction() as cur:
            cur.execute('''
                SELECT si.id, si.data,
                       s.title, s.lyrics, s.verse_order, s.theme_map,
                       s.authors, s.songbook_name, s.songbook_entry,
                       s.copyright, s.ccli_song_number, s.show_copyright, s.key
                FROM service_items si
                LEFT JOIN songs s ON si.song_id = s.id
                WHERE si.item_type = 'song' AND si.data IS NOT NULL AND si.song_id IS NOT NULL
            ''')
            updates = []
            for row in cur.fetchall():
                snap = self._parse_json_field(row['data'], {}) or {}
                if 'title' not in snap or 'copyright' in snap:
                    continue
                if row['title'] is None:
                    continue
                lib_row = {
                    'title': row['title'],
                    'lyrics': row['lyrics'],
                    'verse_order': row['verse_order'],
                    'theme_map': row['theme_map'],
                    'authors': row['authors'],
                    'songbook_name': row['songbook_name'],
                    'songbook_entry': row['songbook_entry'],
                    'copyright': row['copyright'],
                    'ccli_song_number': row['ccli_song_number'],
                    'show_copyright': row['show_copyright'],
                    'key': row['key'],
                }
                filled = self._fill_song_snapshot_gaps(snap, lib_row)
                if filled != snap:
                    updates.append((json.dumps(filled), row['id']))
            if updates:
                cur.executemany("UPDATE service_items SET data = ? WHERE id = ?", updates)
            cur.execute(
                "INSERT INTO app_settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ('song_snapshot_copyright_backfilled', json.dumps(True)))

    def backfill_legacy_song_overrides(self):
        """One-shot: convert legacy override-shaped song items (no ``title`` in
        ``data``) into full snapshots so get_service_items never needs a songs JOIN.

        Idempotent via ``song_snapshot_full_backfilled``. Overlay keys in the old
        payload win over the library row.
        """
        settings = self.load_app_settings()
        if settings.get('song_snapshot_full_backfilled'):
            return
        with self._db_transaction() as cur:
            cur.execute('''
                SELECT si.id, si.song_id, si.data,
                       s.title, s.lyrics, s.verse_order, s.theme_map,
                       s.authors, s.songbook_name, s.songbook_entry,
                       s.copyright, s.ccli_song_number, s.show_copyright, s.key
                FROM service_items si
                LEFT JOIN songs s ON si.song_id = s.id
                WHERE si.item_type = 'song' AND si.data IS NOT NULL
            ''')
            updates = []
            for row in cur.fetchall():
                snap = self._parse_json_field(row['data'], {}) or {}
                if 'title' in snap:
                    continue
                if row['song_id'] is None or row['title'] is None:
                    # Detached or orphan legacy row: promote overlay-only fields if any.
                    if not snap:
                        continue
                    full = {
                        'user_modified': True,
                        'title': snap.get('title') or '',
                        'lyrics': snap.get('lyrics') or '',
                        'verse_order': snap.get('verse_order') or '',
                        'theme_map': snap.get('theme_map') or {},
                        'authors': snap.get('authors') if isinstance(snap.get('authors'), list) else [],
                        'songbook_name': snap.get('songbook_name') or '',
                        'songbook_entry': snap.get('songbook_entry') or '',
                        'copyright': snap.get('copyright') or '',
                        'ccli_song_number': snap.get('ccli_song_number') or '',
                        'show_copyright': bool(snap.get('show_copyright')),
                        'key': snap.get('key') or '',
                    }
                    updates.append((json.dumps(full), row['id']))
                    continue
                lib_row = {
                    'title': row['title'],
                    'lyrics': row['lyrics'],
                    'verse_order': row['verse_order'],
                    'theme_map': row['theme_map'],
                    'authors': row['authors'],
                    'songbook_name': row['songbook_name'],
                    'songbook_entry': row['songbook_entry'],
                    'copyright': row['copyright'],
                    'ccli_song_number': row['ccli_song_number'],
                    'show_copyright': row['show_copyright'],
                    'key': row['key'],
                }
                full = self._song_row_to_service_snapshot(lib_row, user_modified=True)
                for key in ('title', 'lyrics', 'verse_order', 'theme_map',
                            'authors', 'songbook_name', 'songbook_entry',
                            'copyright', 'ccli_song_number', 'show_copyright', 'key'):
                    if key in snap:
                        full[key] = snap[key]
                updates.append((json.dumps(full), row['id']))
            if updates:
                cur.executemany("UPDATE service_items SET data = ? WHERE id = ?", updates)
            cur.execute(
                "INSERT INTO app_settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ('song_snapshot_full_backfilled', json.dumps(True)))

    def _songs_by_ids(self, song_ids):
        """Batch-load library songs keyed by id. Empty input → {}."""
        ids = [i for i in song_ids if i is not None]
        if not ids:
            return {}
        with self._db_transaction(commit=False) as cur:
            placeholders = ','.join('?' * len(ids))
            cur.execute(
                f"SELECT * FROM songs WHERE id IN ({placeholders})", list(ids))
            return {r['id']: self._parse_entity_data(dict(r)) for r in cur.fetchall()}

    def get_service_items(self, service_id):
        """Return resolved service items. Song content comes from ``data`` snapshots
        (no songs.lyrics JOIN). Legacy override rows are rare after
        backfill_legacy_song_overrides; those still hydrate from a batched library fetch.
        """
        with self._db_transaction(commit=False) as cur:
            cur.execute('''
                SELECT si.id as item_id, si.order_num, si.item_type, si.data, si.song_id
                FROM service_items si
                WHERE si.service_id = ?
                ORDER BY si.order_num ASC
            ''', (service_id,))
            rows = [dict(r) for r in cur.fetchall()]

        legacy_ids = []
        for d in rows:
            if d.get('item_type') != 'song' or not d.get('data') or not d.get('song_id'):
                continue
            snap = self._parse_json_field(d['data'], {}) or {}
            if 'title' not in snap:
                legacy_ids.append(d['song_id'])
        lib_by_id = self._songs_by_ids(legacy_ids) if legacy_ids else {}
        return [self._resolve_service_item(d, lib_by_id) for d in rows]

    def get_service_item(self, item_id):
        """Return one fully resolved service item by id, or None if missing."""
        with self._db_transaction(commit=False) as cur:
            cur.execute('''
                SELECT si.id as item_id, si.order_num, si.item_type, si.data, si.song_id
                FROM service_items si
                WHERE si.id = ?
            ''', (item_id,))
            row = cur.fetchone()
            if not row:
                return None
            d = dict(row)

        lib_by_id = {}
        if d.get('item_type') == 'song' and d.get('data') and d.get('song_id'):
            snap = self._parse_json_field(d['data'], {}) or {}
            if 'title' not in snap:
                lib_by_id = self._songs_by_ids([d['song_id']])
        return self._resolve_service_item(d, lib_by_id)

    def _resolve_service_item(self, d, lib_by_id=None):
        """Resolve one service-item row dict into its display form, dispatching on item_type."""
        d['theme_map'] = {}
        d['has_overrides'] = False

        item_type = d['item_type']
        if not d['data']:
            return d

        if item_type == 'bible':
            self._apply_bible_item(d)
        elif item_type == 'announcement':
            self._apply_announcement_item(d)
        elif item_type in SIMPLE_SERVICE_ITEM_PARSERS:
            parsed = self._parse_json_field(d['data'], {})
            d.update(SIMPLE_SERVICE_ITEM_PARSERS[item_type](parsed))
            d['theme_map'] = parsed.get('theme_map') or {}
            d['has_overrides'] = bool(d['theme_map'])
            d['lyrics'] = ''
        elif item_type == 'song':
            self._apply_song_item(d, (lib_by_id or {}).get(d.get('song_id')))
        return d

    @staticmethod
    def _apply_bible_item(d):
        try:
            bdata = json.loads(d['data'])
            d['title'] = bdata.get('ref', 'Bible Verse')
            d['theme_map'] = bdata.get('theme_map') or {}
            d['has_overrides'] = bool(d['theme_map'])
        except json.JSONDecodeError:
            pass

    @staticmethod
    def _apply_announcement_item(d):
        """Resolve a service announcement item for display.

        Items are self-contained snapshots (``fields`` + ``name`` + ``theme_map``).
        """
        d['lyrics'] = ''
        d['fields'] = []
        d['theme_map'] = {}
        try:
            adata = json.loads(d['data'])
        except json.JSONDecodeError:
            d['title'] = 'Announcement'
            d['name'] = 'Announcement'
            return

        d['theme_map'] = adata.get('theme_map') or {}
        d['has_overrides'] = bool(d['theme_map'])
        fields = _normalize_ann_item_fields(adata.get('fields'))
        d['fields'] = fields
        name = adata.get('name') or (fields[0]['value'] if fields else '')
        d['name'] = name
        d['title'] = re.sub(r'<[^>]+>', '', name).strip() or 'Announcement'

    @staticmethod
    def _apply_song_item(d, library_song=None):
        """Resolve a service song from its snapshot in `data` (source of truth).

        Modern items carry a full snapshot (``title`` present). Legacy override
        payloads lack ``title`` and overlay onto ``library_song`` when provided.
        """
        try:
            snap = json.loads(d['data'])
        except (json.JSONDecodeError, TypeError):
            return
        if 'title' in snap:
            d['title'] = snap['title']
            d['lyrics'] = snap.get('lyrics', '')
            d['verse_order'] = snap.get('verse_order', '')
            d['theme_map'] = snap.get('theme_map') or {}
            d['authors'] = snap.get('authors') if isinstance(snap.get('authors'), list) else []
            d['songbook_name'] = snap.get('songbook_name', '') or ''
            d['songbook_entry'] = snap.get('songbook_entry', '') or ''
            d['copyright'] = snap.get('copyright', '') or ''
            d['ccli_song_number'] = snap.get('ccli_song_number', '') or ''
            d['show_copyright'] = bool(snap.get('show_copyright'))
            d['key'] = snap.get('key', '') or ''
            d['has_overrides'] = snap.get('user_modified', True)
            return

        # Legacy override format: base from library row, then overlay.
        if library_song:
            d['title'] = library_song.get('title') or ''
            d['lyrics'] = library_song.get('lyrics') or ''
            d['verse_order'] = library_song.get('verse_order') or ''
        if snap.get('title'):
            d['title'] = snap['title']
        if snap.get('lyrics'):
            d['lyrics'] = snap['lyrics']
        if 'verse_order' in snap:
            d['verse_order'] = snap['verse_order']
        d['theme_map'] = snap.get('theme_map') or {}
        d['has_overrides'] = True

    # --- Bibles ---

    def import_bible(self, name, copyright, verses):
        """
        verses: list of dict {'book': str, 'chapter': int, 'verse': int, 'text': str}
        """
        with self._db_transaction() as cur:
            cur.execute('INSERT INTO bibles (name, copyright) VALUES (?, ?)', (name, copyright))
            bible_id = cur.lastrowid

            # Batch insert verses
            data = [(bible_id, v['book'], v['chapter'], v['verse'], v['text']) for v in verses]
            cur.executemany('INSERT INTO verses (bible_id, book, chapter, verse_num, text) VALUES (?, ?, ?, ?, ?)', data)

            return bible_id

    def get_bibles(self):
        with self._db_transaction(commit=False) as cur:
            cur.execute('SELECT * FROM bibles ORDER BY name ASC')
            rows = cur.fetchall()
            return [dict(r) for r in rows]

    def get_bible_books(self, bible_id):
        with self._db_transaction(commit=False) as cur:
            cur.execute('SELECT book FROM verses WHERE bible_id=? GROUP BY book ORDER BY MIN(id) ASC', (bible_id,))
            rows = cur.fetchall()
            return [r[0] for r in rows]

    def get_bible_chapters(self, bible_id, book):
        with self._db_transaction(commit=False) as cur:
            cur.execute('SELECT DISTINCT chapter FROM verses WHERE bible_id=? AND book=? ORDER BY chapter ASC', (bible_id, book))
            rows = cur.fetchall()
            return [r[0] for r in rows]

    def get_bible_verses(self, bible_id, book, chapter):
        with self._db_transaction(commit=False) as cur:
            cur.execute('SELECT verse_num, text FROM verses WHERE bible_id=? AND book=? AND chapter=? ORDER BY verse_num ASC', (bible_id, book, chapter))
            rows = cur.fetchall()
            return [dict(r) for r in rows]

    def delete_bible(self, bible_id):
        with self._db_transaction() as cur:
            cur.execute('DELETE FROM bibles WHERE id=?', (bible_id,))

    def rename_bible(self, bible_id, new_name):
        with self._db_transaction() as cur:
            cur.execute('UPDATE bibles SET name=? WHERE id=?', (new_name, bible_id))

    # --- Image Folders ---

    def create_image_folder(self, name, parent_id=None):
        return self._create_nestable_folder('image', name, parent_id)

    def rename_image_folder(self, folder_id, name):
        self._rename_nestable_folder('image', folder_id, name)

    @staticmethod
    def _descendant_folder_ids(cur, folder_id, table='image_folders'):
        """Return [folder_id] plus every nested descendant folder id (depth-first),
        for a self-nesting folder table (image_folders, ann_folders or video_folders)."""
        if table not in DatabaseManager._ALLOWED_FOLDER_TABLES:
            raise ValueError(f"Unknown folder table: {table!r}")
        result = [folder_id]
        stack = [folder_id]
        while stack:
            pid = stack.pop()
            cur.execute(f"SELECT id FROM {table} WHERE parent_id = ?", (pid,))
            for r in cur.fetchall():
                result.append(r['id'])
                stack.append(r['id'])
        return result

    def delete_image_folder(self, folder_id):
        """Delete a folder and every nested subfolder, returning the de-duplicated list
        of image filenames linked anywhere in the deleted subtree (for orphan cleanup).
        Deleting each image_folders row cascades its image_folder_items."""
        with self._db_transaction() as cur:
            ids = self._descendant_folder_ids(cur, folder_id)
            placeholders = ','.join('?' * len(ids))
            cur.execute(
                f"SELECT DISTINCT filename FROM image_folder_items WHERE folder_id IN ({placeholders})",
                ids
            )
            filenames = [r['filename'] for r in cur.fetchall()]
            cur.execute(f"DELETE FROM image_folders WHERE id IN ({placeholders})", ids)
            return filenames

    def move_image_folder(self, folder_id, new_parent_id, ordered_ids=None):
        """Re-parent a folder (new_parent_id None = top level) and optionally reorder the
        destination parent's children. Rejects moves that would create a cycle (into self
        or a descendant). Returns True on success, False if rejected."""
        return self._move_nestable_folder('image', folder_id, new_parent_id, ordered_ids)

    def get_image_folders(self):
        with self._db_transaction(commit=False) as cur:
            cur.execute("SELECT id, name, sort_order, parent_id FROM image_folders ORDER BY sort_order ASC, id ASC")
            folders = [dict(r) for r in cur.fetchall()]
            by_id = {f['id']: f for f in folders}
            for f in folders:
                f['images'] = []
            if folders:
                # Single grouped query instead of one per folder. Iterating a globally
                # (sort_order, id)-ordered result preserves each folder's item ordering.
                cur.execute(
                    "SELECT id, filename, sort_order, folder_id FROM image_folder_items "
                    "ORDER BY sort_order ASC, id ASC"
                )
                for r in cur.fetchall():
                    folder = by_id.get(r['folder_id'])
                    if folder is not None:
                        folder['images'].append(
                            {'id': r['id'], 'filename': r['filename'], 'sort_order': r['sort_order']})
            cur.execute("SELECT filename, display_name FROM image_files")
            dn = {r['filename']: r['display_name'] for r in cur.fetchall()}
            for f in folders:
                for img in f['images']:
                    img['display_name'] = dn.get(img['filename'], img['filename'])
            return folders

    def get_image_folder(self, folder_id):
        with self._db_transaction(commit=False) as cur:
            cur.execute("SELECT id, name, sort_order FROM image_folders WHERE id = ?", (folder_id,))
            row = cur.fetchone()
            if not row:
                return None
            folder = dict(row)
            cur.execute(
                "SELECT id, filename, sort_order FROM image_folder_items "
                "WHERE folder_id = ? ORDER BY sort_order ASC, id ASC",
                (folder_id,)
            )
            folder['images'] = [dict(r) for r in cur.fetchall()]
            return folder

    def add_image_to_folder(self, folder_id, filename):
        with self._db_transaction() as cur:
            cur.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM image_folder_items WHERE folder_id = ?",
                (folder_id,)
            )
            next_order = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO image_folder_items (folder_id, filename, sort_order) VALUES (?, ?, ?)",
                (folder_id, filename, next_order)
            )
            return cur.lastrowid

    def remove_image_from_folder(self, item_id):
        with self._db_transaction() as cur:
            cur.execute("DELETE FROM image_folder_items WHERE id = ?", (item_id,))

    def reorder_image_folder_items(self, folder_id, ordered_item_ids):
        with self._db_transaction() as cur:
            cur.executemany(
                "UPDATE image_folder_items SET sort_order = ? WHERE id = ? AND folder_id = ?",
                [(new_order, item_id, folder_id) for new_order, item_id in enumerate(ordered_item_ids)]
            )

    def register_image_file(self, filename, display_name):
        """Record the original (display) name for an uploaded image saved under a random on-disk filename."""
        with self._db_transaction() as cur:
            cur.execute("INSERT OR REPLACE INTO image_files (filename, display_name) VALUES (?, ?)",
                        (filename, display_name))

    def get_image_display_names(self):
        """Return {on_disk_filename: display_name}. Images uploaded before this feature won't appear here;
        callers should fall back to the on-disk filename when looking up a name. Hidden images are
        included so service items can still resolve their original names."""
        with self._db_transaction(commit=False) as cur:
            cur.execute("SELECT filename, display_name FROM image_files")
            return {r['filename']: r['display_name'] for r in cur.fetchall()}

    def count_service_references(self, filenames):
        """For each filename, count how many service_items snapshots reference it
        (single 'image' items + 'image_folder' items' images list). Returns a dict
        keyed by filename. Used by the lazy-delete path so a file the library is
        trying to delete is kept on disk while any service still uses it."""
        if not filenames:
            return {}
        fnset = set(filenames)
        counts = {fn: 0 for fn in fnset}
        with self._db_transaction(commit=False) as cur:
            cur.execute("SELECT item_type, data FROM service_items "
                        "WHERE item_type IN ('image', 'image_folder') AND data IS NOT NULL")
            for row in cur.fetchall():
                try:
                    data = json.loads(row['data'])
                except (json.JSONDecodeError, TypeError):
                    continue
                if row['item_type'] == 'image':
                    fn = data.get('filename')
                    if fn in fnset:
                        counts[fn] += 1
                else:  # image_folder
                    for fn in data.get('images', []) or []:
                        if fn in fnset:
                            counts[fn] += 1
        return counts

    def video_reference_count(self, filename):
        """How many service items reference this video filename (item_type='video').
        Used to refuse deleting a video a service still depends on."""
        if not filename:
            return 0
        return self.video_reference_counts([filename]).get(filename, 0)

    def video_reference_counts(self, filenames):
        """For each filename, how many service items reference it (item_type='video'),
        gathered in ONE table scan. Returns {filename: count}. The bulk delete path
        uses this so deleting N videos doesn't scan (and JSON-parse) every video
        service item N separate times."""
        fnset = {fn for fn in filenames if fn}
        counts = {fn: 0 for fn in fnset}
        if not fnset:
            return counts
        with self._db_transaction(commit=False) as cur:
            cur.execute("SELECT data FROM service_items "
                        "WHERE item_type = 'video' AND data IS NOT NULL")
            rows = cur.fetchall()
        for row in rows:
            try:
                data = json.loads(row['data'])
            except (json.JSONDecodeError, TypeError):
                continue
            fn = data.get('filename')
            if fn in fnset:
                counts[fn] += 1
        return counts

    def delete_library_images(self, filenames, images_dir):
        """Remove each filename from every library folder (image_folder_items) and from the
        library listing. If a filename is still referenced by any service_items snapshot, the
        file stays on disk (and its image_files row is just flagged library_visible=0). If
        nothing references it, the file is unlinked and its image_files row removed.
        Returns (unlinked_count, hidden_count)."""
        if not filenames:
            return 0, 0
        filenames = [os.path.basename(n) for n in filenames if n]
        refs = self.count_service_references(filenames)
        to_unlink = [fn for fn in filenames if refs.get(fn, 0) == 0]
        to_hide = [fn for fn in filenames if refs.get(fn, 0) > 0]
        with self._db_transaction() as cur:
            placeholders = ','.join('?' * len(filenames))
            cur.execute(f"DELETE FROM image_folder_items WHERE filename IN ({placeholders})", filenames)
            if to_unlink:
                ph = ','.join('?' * len(to_unlink))
                cur.execute(f"DELETE FROM image_files WHERE filename IN ({ph})", to_unlink)
            if to_hide:
                ph = ','.join('?' * len(to_hide))
                # Make sure a row exists for legacy uploads, then flag hidden.
                cur.executemany("INSERT OR IGNORE INTO image_files (filename, display_name) VALUES (?, ?)",
                                [(fn, fn) for fn in to_hide])
                cur.execute(f"UPDATE image_files SET library_visible = 0 WHERE filename IN ({ph})", to_hide)
        for fn in to_unlink:
            p = os.path.join(images_dir, fn)
            with suppress(OSError):
                os.unlink(p)
        return len(to_unlink), len(to_hide)

    def cleanup_orphan_hidden_images(self, images_dir):
        """Sweep all library-hidden images: any whose service references are now gone
        get their file unlinked and image_files row removed. Cheap (small table) — safe
        to call after any service mutation that might have dropped the last reference."""
        with self._db_transaction(commit=False) as cur:
            cur.execute("SELECT filename FROM image_files WHERE library_visible = 0")
            candidates = [r['filename'] for r in cur.fetchall()]
        if not candidates:
            return 0
        refs = self.count_service_references(candidates)
        to_unlink = [fn for fn in candidates if refs.get(fn, 0) == 0]
        if not to_unlink:
            return 0
        with self._db_transaction() as cur:
            ph = ','.join('?' * len(to_unlink))
            cur.execute(f"DELETE FROM image_files WHERE filename IN ({ph})", to_unlink)
        for fn in to_unlink:
            p = os.path.join(images_dir, fn)
            with suppress(OSError):
                os.unlink(p)
        return len(to_unlink)

    def add_image_to_service(self, service_id, filename, at_index=None):
        with self._db_transaction() as cur:
            order = self._open_insert_orders(cur, service_id, at_index, 1)[0]
            self._insert_service_item(cur, service_id, 'image', {'filename': filename}, order_num=order)

    def add_images_to_service(self, service_id, filenames, at_index=None):
        """Batch add: insert each filename as a standalone single-image service item, one
        transaction. `at_index` None appends; otherwise they land consecutively there."""
        if not filenames:
            return 0
        with self._db_transaction() as cur:
            orders = self._open_insert_orders(cur, service_id, at_index, len(filenames))
            for order, fn in zip(orders, filenames, strict=True):
                self._insert_service_item(cur, service_id, 'image', {'filename': fn}, order_num=order)
        return len(filenames)

    def add_image_folder_to_service(self, service_id, folder_id, folder_name, at_index=None):
        with self._db_transaction() as cur:
            cur.execute(
                "SELECT filename FROM image_folder_items WHERE folder_id = ? ORDER BY sort_order ASC",
                (folder_id,)
            )
            images = [row['filename'] for row in cur.fetchall()]
            order = self._open_insert_orders(cur, service_id, at_index, 1)[0]
            self._insert_service_item(cur, service_id, 'image_folder',
                                      {'folder_id': folder_id, 'folder_name': folder_name, 'images': images},
                                      order_num=order)

    # ---- Video library: nestable folders ------------------------------------
    # Videos are files on disk; video_folders/video_folder_items overlay an
    # organizational tree by referencing filenames. A video not linked to any folder
    # renders as "loose". Deleting a folder only un-nests its videos (files kept) —
    # never destructive — so there is no orphan/hidden bookkeeping like images have.

    def get_video_folders(self):
        """Every video folder (flat; client builds the tree from parent_id), each with
        its direct videos in sort order under a 'videos' key."""
        with self._db_transaction(commit=False) as cur:
            cur.execute("SELECT id, name, sort_order, parent_id FROM video_folders ORDER BY sort_order ASC, id ASC")
            folders = [dict(r) for r in cur.fetchall()]
            by_id = {f['id']: f for f in folders}
            for f in folders:
                f['videos'] = []
            if folders:
                cur.execute("SELECT id, filename, sort_order, folder_id FROM video_folder_items "
                            "ORDER BY sort_order ASC, id ASC")
                for r in cur.fetchall():
                    folder = by_id.get(r['folder_id'])
                    if folder is not None:
                        folder['videos'].append(
                            {'id': r['id'], 'filename': r['filename'], 'sort_order': r['sort_order']})
            return folders

    def create_video_folder(self, name, parent_id=None):
        return self._create_nestable_folder('video', name, parent_id)

    def rename_video_folder(self, folder_id, name):
        self._rename_nestable_folder('video', folder_id, name)

    def move_video_folder(self, folder_id, new_parent_id, ordered_ids=None):
        """Re-parent a folder (None = top level) and optionally reorder the destination
        parent's children. Rejects moves that would create a cycle. Returns True/False."""
        return self._move_nestable_folder('video', folder_id, new_parent_id, ordered_ids)

    def delete_video_folder(self, folder_id):
        """Delete a folder and every nested subfolder. The video FILES are never touched
        — deleting the folder rows just drops the folder links (video_folder_items cascade),
        so those videos reappear as loose in the library."""
        with self._db_transaction() as cur:
            ids = self._descendant_folder_ids(cur, folder_id, 'video_folders')
            placeholders = ','.join('?' * len(ids))
            cur.execute(f"DELETE FROM video_folders WHERE id IN ({placeholders})", ids)

    def reorder_video_folders(self, ordered_ids):
        with self._db_transaction() as cur:
            cur.executemany("UPDATE video_folders SET sort_order = ? WHERE id = ?",
                            list(enumerate(ordered_ids)))

    def add_video_to_folder(self, folder_id, filename):
        with self._db_transaction() as cur:
            cur.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM video_folder_items WHERE folder_id = ?",
                        (folder_id,))
            next_order = cur.fetchone()[0]
            cur.execute("INSERT INTO video_folder_items (folder_id, filename, sort_order) VALUES (?, ?, ?)",
                        (folder_id, filename, next_order))
            return cur.lastrowid

    def remove_video_from_folder(self, item_id):
        with self._db_transaction() as cur:
            cur.execute("DELETE FROM video_folder_items WHERE id = ?", (item_id,))

    def reorder_video_folder_items(self, folder_id, ordered_item_ids):
        with self._db_transaction() as cur:
            cur.executemany("UPDATE video_folder_items SET sort_order = ? WHERE id = ? AND folder_id = ?",
                            [(new_order, item_id, folder_id) for new_order, item_id in enumerate(ordered_item_ids)])

    # The two library trees (images, videos) share one row shape, so the multi-select
    # move below is written once against whitelisted (items, folders) table names.
    _FOLDER_ITEM_TABLES = {'image': ('image_folder_items', 'image_folders'),
                           'video': ('video_folder_items', 'video_folders')}

    def move_folder_items(self, kind, selections, to_folder_id, to_index=None):
        """Relocate several library-tree entries in one transaction (the multi-select
        drag — replaces the old client-side per-item remove/add/refetch/reorder chain).

        `selections` is an ordered list of {'id': row_id} (an entry already in some
        folder) or {'filename': str} (a loose file, which gains a row). All-or-nothing:
        a stale row id (concurrent edit from another admin tab) refuses the whole move.

        to_folder_id None deletes the selected rows — the entries become loose again
        (files are never touched). Otherwise the entries land in to_folder_id, appended
        in selection order when to_index is None, else inserted at to_index counted
        against the target's CURRENT list (adjusted for moved rows the target loses
        above that position — same contract as move_service_folder_images). Rows moved
        between folders are re-inserted, so their ids change; callers reload the tree.
        Returns True on success."""
        pair = self._FOLDER_ITEM_TABLES.get(kind)
        if pair is None:
            raise ValueError(f"Unknown folder kind: {kind!r}")
        table, folders_table = pair
        if not selections:
            return False
        with self._db_transaction() as cur:
            if to_folder_id is not None:
                cur.execute(f"SELECT 1 FROM {folders_table} WHERE id = ?", (to_folder_id,))
                if cur.fetchone() is None:
                    return False
            row_ids = [s.get('id') for s in selections if s.get('id') is not None]
            rows_by_id = {}
            if row_ids:
                placeholders = ','.join('?' * len(row_ids))
                cur.execute(f"SELECT id, folder_id, filename FROM {table} WHERE id IN ({placeholders})",
                            row_ids)
                rows_by_id = {r['id']: dict(r) for r in cur.fetchall()}
            moved = []   # (row_or_None, filename) in selection order
            for s in selections:
                if s.get('id') is not None:
                    row = rows_by_id.get(s['id'])
                    if row is None:
                        return False
                    moved.append((row, row['filename']))
                else:
                    if not s.get('filename'):
                        return False
                    moved.append((None, s['filename']))

            if to_folder_id is None:
                ids = [row['id'] for row, _fn in moved if row is not None]
                if ids:
                    placeholders = ','.join('?' * len(ids))
                    cur.execute(f"DELETE FROM {table} WHERE id IN ({placeholders})", ids)
                return True

            # Append mode leaves entries already in the target where they are;
            # positional mode repositions them like everything else.
            if to_index is None:
                moved = [(row, fn) for row, fn in moved
                         if row is None or row['folder_id'] != to_folder_id]
                if not moved:
                    return True

            cur.execute(f"SELECT id FROM {table} WHERE folder_id = ? "
                        "ORDER BY sort_order ASC, id ASC", (to_folder_id,))
            target_ids = [r['id'] for r in cur.fetchall()]
            moved_row_ids = {row['id'] for row, _fn in moved if row is not None}

            insert_at = len(target_ids) - len(moved_row_ids & set(target_ids))
            if to_index is not None:
                pos_by_id = {rid: i for i, rid in enumerate(target_ids)}
                shifted = sum(1 for rid in moved_row_ids
                              if rid in pos_by_id and pos_by_id[rid] < to_index)
                insert_at = max(0, min(to_index - shifted,
                                       len(target_ids) - len(moved_row_ids & set(target_ids))))

            if moved_row_ids:
                placeholders = ','.join('?' * len(moved_row_ids))
                cur.execute(f"DELETE FROM {table} WHERE id IN ({placeholders})", list(moved_row_ids))

            remaining = [rid for rid in target_ids if rid not in moved_row_ids]
            cur.executemany(f"UPDATE {table} SET sort_order = ? WHERE id = ?",
                            [(i if i < insert_at else i + len(moved), rid)
                             for i, rid in enumerate(remaining)])
            cur.executemany(f"INSERT INTO {table} (folder_id, filename, sort_order) VALUES (?, ?, ?)",
                            [(to_folder_id, fn, insert_at + offset)
                             for offset, (_row, fn) in enumerate(moved)])
            return True

    def prune_missing_video_folder_items(self, existing_filenames):
        """Drop folder links for videos whose file no longer exists on disk (e.g. deleted
        outside the app), so stale rows can't haunt the tree. Cheap; safe to call on load."""
        with self._db_transaction() as cur:
            cur.execute("SELECT id, filename FROM video_folder_items")
            stale = [r['id'] for r in cur.fetchall() if r['filename'] not in existing_filenames]
            if stale:
                ph = ','.join('?' * len(stale))
                cur.execute(f"DELETE FROM video_folder_items WHERE id IN ({ph})", stale)
        return len(stale)

    def add_video_folder_to_service(self, service_id, folder_id, at_index=None):
        """Add every video in a library folder to the service as individual video items,
        in the folder's order, in one transaction (mirrors add_image_folder_to_service,
        but videos have no grouped-playback item type so each becomes its own item)."""
        with self._db_transaction() as cur:
            cur.execute("SELECT filename FROM video_folder_items WHERE folder_id = ? ORDER BY sort_order ASC",
                        (folder_id,))
            filenames = [r['filename'] for r in cur.fetchall()]
            if not filenames:
                return 0
            orders = self._open_insert_orders(cur, service_id, at_index, len(filenames))
            for order, fn in zip(orders, filenames, strict=True):
                self._insert_service_item(cur, service_id, 'video',
                                          {'filename': fn, 'title': fn, 'autoplay': True, 'loop': False},
                                          order_num=order)
            return len(filenames)

    def add_videos_to_service(self, service_id, filenames, at_index=None):
        """Add several videos to the service as individual video items, in list order,
        in one transaction (the multi-select drag/add — same per-item defaults as
        add_video_folder_to_service). Returns the number added."""
        if not filenames:
            return 0
        with self._db_transaction() as cur:
            orders = self._open_insert_orders(cur, service_id, at_index, len(filenames))
            for order, fn in zip(orders, filenames, strict=True):
                self._insert_service_item(cur, service_id, 'video',
                                          {'filename': fn, 'title': fn, 'autoplay': True, 'loop': False},
                                          order_num=order)
            return len(filenames)

    def create_service_image_folder(self, service_id, folder_name):
        """Create an empty image_folder service item not linked to any library folder."""
        with self._db_transaction() as cur:
            return self._insert_service_item(cur, service_id, 'image_folder',
                                              {'folder_id': None, 'folder_name': folder_name or 'New Folder', 'images': []})

    def merge_image_into_service_folder(self, from_item_id, to_item_id, to_index=None):
        """Move a standalone single-image service item's filename into a service
        image_folder item, then delete the standalone item. Atomic, service-scoped.
        Returns True on success."""
        with self._db_transaction() as cur:
            cur.execute("SELECT id, item_type, data FROM service_items WHERE id IN (?, ?)",
                        (from_item_id, to_item_id))
            rows = {r['id']: dict(r) for r in cur.fetchall()}
            src = rows.get(from_item_id)
            dst = rows.get(to_item_id)
            if not src or not dst:
                return False
            if src['item_type'] != 'image' or dst['item_type'] != 'image_folder':
                return False
            src_data = self._parse_json_field(src['data'], {})
            filename = src_data.get('filename')
            if not filename:
                return False
            dst_data = self._parse_json_field(dst['data'], {})
            imgs = list(dst_data.get('images', []))
            if to_index is None:
                imgs.append(filename)
            else:
                insert_at = max(0, min(int(to_index), len(imgs)))
                imgs.insert(insert_at, filename)
            dst_data['images'] = imgs
            cur.execute("UPDATE service_items SET data = ? WHERE id = ?", (json.dumps(dst_data), to_item_id))
            cur.execute("DELETE FROM service_items WHERE id = ?", (from_item_id,))
            return True

    def remove_filenames_from_service_folders(self, removals):
        """Bulk remove. `removals` is a list of {'item_id': int, 'index': int}.
        Indexes are interpreted against each item's current snapshot; multiple
        indexes per item are popped descending so earlier indexes stay valid."""
        if not removals:
            return 0
        by_item = {}
        for r in removals:
            iid = r.get('item_id')
            idx = r.get('index')
            if iid is None or idx is None:
                continue
            by_item.setdefault(iid, []).append(int(idx))
        if not by_item:
            return 0
        total = 0
        with self._db_transaction() as cur:
            placeholders = ','.join('?' * len(by_item))
            cur.execute(f"SELECT id, item_type, data FROM service_items WHERE id IN ({placeholders})", list(by_item))
            rows = {r['id']: dict(r) for r in cur.fetchall()}
            for iid, idxs in by_item.items():
                row = rows.get(iid)
                if not row or row['item_type'] != 'image_folder':
                    continue
                data = self._parse_json_field(row['data'], {})
                imgs = list(data.get('images', []))
                # Sort descending and deduplicate to keep remaining indexes valid.
                for idx in sorted(set(idxs), reverse=True):
                    if 0 <= idx < len(imgs):
                        imgs.pop(idx)
                        total += 1
                data['images'] = imgs
                cur.execute("UPDATE service_items SET data = ? WHERE id = ?", (json.dumps(data), iid))
        return total

    def remove_filename_from_service_folder(self, item_id, index):
        """Remove the image at `index` from a service image_folder item's snapshot.
        Service-scoped: the library is untouched. Returns True on success."""
        with self._db_transaction() as cur:
            cur.execute("SELECT item_type, data FROM service_items WHERE id = ?", (item_id,))
            row = cur.fetchone()
            if not row or row['item_type'] != 'image_folder':
                return False
            data = self._parse_json_field(row['data'], {})
            imgs = list(data.get('images', []))
            if index < 0 or index >= len(imgs):
                return False
            imgs.pop(index)
            data['images'] = imgs
            cur.execute("UPDATE service_items SET data = ? WHERE id = ?", (json.dumps(data), item_id))
            return True

    def add_filenames_to_service_folder(self, item_id, filenames, to_index=None):
        """Insert filenames into a service image_folder item's snapshot (service-scoped).
        Appends when to_index is None; otherwise inserts at to_index. Returns True on success."""
        if not filenames:
            return False
        with self._db_transaction() as cur:
            cur.execute("SELECT item_type, data FROM service_items WHERE id = ?", (item_id,))
            row = cur.fetchone()
            if not row or row['item_type'] != 'image_folder':
                return False
            data = self._parse_json_field(row['data'], {})
            imgs = list(data.get('images', []))
            if to_index is None:
                imgs.extend(filenames)
            else:
                insert_at = max(0, min(int(to_index), len(imgs)))
                for offset, fn in enumerate(filenames):
                    imgs.insert(insert_at + offset, fn)
            data['images'] = imgs
            cur.execute("UPDATE service_items SET data = ? WHERE id = ?", (json.dumps(data), item_id))
            return True

    def move_service_folder_image(self, from_item_id, from_index, to_item_id, to_index=None):
        """Move an image filename (by index) from one service image_folder item's snapshot
        into another's, or reorder within one (when from/to are the same item). Service-scoped:
        only service_items rows are touched, never the library image_folders. `to_index` is the
        insertion position in the target's original image list (None = append). Returns True on success."""
        with self._db_transaction() as cur:
            cur.execute("SELECT id, item_type, data FROM service_items WHERE id IN (?, ?)",
                        (from_item_id, to_item_id))
            rows = {r['id']: dict(r) for r in cur.fetchall()}
            src = rows.get(from_item_id)
            dst = rows.get(to_item_id)
            if not src or not dst or src['item_type'] != 'image_folder' or dst['item_type'] != 'image_folder':
                return False
            src_data = self._parse_json_field(src['data'], {})
            src_imgs = list(src_data.get('images', []))
            if from_index < 0 or from_index >= len(src_imgs):
                return False

            if from_item_id == to_item_id:
                fn = src_imgs.pop(from_index)
                # Removing the item before its target shifts the target left by one.
                insert_at = len(src_imgs) if to_index is None else (to_index - 1 if from_index < to_index else to_index)
                insert_at = max(0, min(insert_at, len(src_imgs)))
                src_imgs.insert(insert_at, fn)
                src_data['images'] = src_imgs
                cur.execute("UPDATE service_items SET data = ? WHERE id = ?", (json.dumps(src_data), from_item_id))
                return True

            dst_data = self._parse_json_field(dst['data'], {})
            dst_imgs = list(dst_data.get('images', []))
            fn = src_imgs.pop(from_index)
            insert_at = len(dst_imgs) if to_index is None else max(0, min(to_index, len(dst_imgs)))
            dst_imgs.insert(insert_at, fn)
            src_data['images'] = src_imgs
            dst_data['images'] = dst_imgs
            cur.execute("UPDATE service_items SET data = ? WHERE id = ?", (json.dumps(src_data), from_item_id))
            cur.execute("UPDATE service_items SET data = ? WHERE id = ?", (json.dumps(dst_data), to_item_id))
            return True

    def _load_folder_item_data(self, cur, item_ids, to_item_id):
        """Fetch the given service_items and parse each one's JSON data. Returns {id: data}
        only if every row exists, is an image_folder, and includes the move target to_item_id;
        otherwise None (the caller aborts the move)."""
        placeholders = ','.join('?' * len(item_ids))
        cur.execute(f"SELECT id, item_type, data FROM service_items WHERE id IN ({placeholders})", list(item_ids))
        rows = {r['id']: dict(r) for r in cur.fetchall()}
        if to_item_id not in rows:
            return None
        data_by_id = {}
        for iid, row in rows.items():
            if row['item_type'] != 'image_folder':
                return None
            data_by_id[iid] = self._parse_json_field(row['data'], {})
        return data_by_id

    @staticmethod
    def _collect_selected_images(selections, data_by_id):
        """Resolve each {item_id, index} selection to (item_id, index, filename) in selection
        order, validating every reference against the parsed data. Returns None if any
        selection is missing or out of range."""
        ordered = []
        for s in selections:
            iid = s.get('item_id')
            idx = s.get('index')
            if iid not in data_by_id or idx is None:
                return None
            imgs = data_by_id[iid].get('images', [])
            if idx < 0 or idx >= len(imgs):
                return None
            ordered.append((iid, idx, imgs[idx]))
        return ordered

    def move_service_folder_images(self, selections, to_item_id, to_index=None):
        """Move several images at once into to_item_id, from one or more source image_folder
        items, in selection order. `selections` is a list of {'item_id': int, 'index': int}
        referencing positions in each source's current snapshot. If `to_index` is None the
        images are appended; otherwise they are inserted at that position in the target's
        original image list (with the index automatically adjusted for any of the moved
        items that came from positions before to_index in the target itself).
        Service-scoped: only service_items rows are touched. Returns True on success."""
        if not selections:
            return False
        with self._db_transaction() as cur:
            item_ids = {s.get('item_id') for s in selections} | {to_item_id}
            data_by_id = self._load_folder_item_data(cur, item_ids, to_item_id)
            if data_by_id is None:
                return False
            # Capture the selected filenames (in selection order) before any removal.
            ordered = self._collect_selected_images(selections, data_by_id)
            if ordered is None:
                return False
            # Adjust to_index for removals from the target at positions < to_index.
            adjusted_to_index = to_index
            if to_index is not None:
                shifted = sum(1 for (iid, idx, _fn) in ordered if iid == to_item_id and idx < to_index)
                adjusted_to_index = to_index - shifted
            self._reshuffle_images(data_by_id, ordered, to_item_id, to_index, adjusted_to_index)
            for iid, d in data_by_id.items():
                cur.execute("UPDATE service_items SET data = ? WHERE id = ?", (json.dumps(d), iid))
            return True

    @staticmethod
    def _reshuffle_images(data_by_id, ordered, to_item_id, to_index, adjusted_to_index):
        """Remove the moved images from their source folders and insert them into the target
        (appended when to_index is None, else at adjusted_to_index, preserving selection
        order). Mutates the images list of each affected entry in data_by_id."""
        # Remove from each source (descending index keeps remaining indices valid).
        by_src = {}
        for iid, idx, _fn in ordered:
            by_src.setdefault(iid, []).append(idx)
        for iid, idxs in by_src.items():
            imgs = list(data_by_id[iid].get('images', []))
            for idx in sorted(idxs, reverse=True):
                imgs.pop(idx)
            data_by_id[iid]['images'] = imgs
        # Insert the moved filenames at the chosen position (or append if None).
        dst_imgs = list(data_by_id[to_item_id].get('images', []))
        if to_index is None:
            dst_imgs.extend(fn for (_iid, _idx, fn) in ordered)
        else:
            insert_at = max(0, min(adjusted_to_index, len(dst_imgs)))
            for offset, (_iid, _idx, fn) in enumerate(ordered):
                dst_imgs.insert(insert_at + offset, fn)
        data_by_id[to_item_id]['images'] = dst_imgs

    def add_divider_to_service(self, service_id, title, at_index=None):
        with self._db_transaction() as cur:
            order = self._open_insert_orders(cur, service_id, at_index, 1)[0]
            self._insert_service_item(cur, service_id, 'divider', {'title': title}, order_num=order)

    # --- Application configuration (app_settings + outputs tables) ---

    def load_app_settings(self) -> dict:
        """Return all scalar app settings as a dict, JSON-decoding each stored value."""
        with self._db_transaction(commit=False) as cur:
            cur.execute("SELECT key, value FROM app_settings")
            return {row['key']: self._parse_json_field(row['value']) for row in cur.fetchall()}

    def save_app_settings(self, settings: dict):
        """Upsert scalar app settings (each value JSON-encoded) in one transaction."""
        with self._db_transaction() as cur:
            cur.executemany(
                "INSERT INTO app_settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                [(k, json.dumps(v)) for k, v in settings.items()])

    def load_output_configs(self) -> list:
        """Return the stored output persist-dicts in display order."""
        with self._db_transaction(commit=False) as cur:
            cur.execute("SELECT data FROM outputs ORDER BY order_num, id")
            return [self._parse_json_field(row['data'], {}) for row in cur.fetchall()]

    def save_output_configs(self, configs: list):
        """Replace the stored outputs with `configs` (list of persist-dicts), keeping
        list order via order_num. A full replace mirrors the prior whole-file rewrite
        and is trivially cheap at this scale."""
        with self._db_transaction() as cur:
            cur.execute("DELETE FROM outputs")
            cur.executemany(
                "INSERT INTO outputs(order_num, data) VALUES (?, ?)",
                [(i, json.dumps(cfg, ensure_ascii=False)) for i, cfg in enumerate(configs)])

    # ---- Style profiles ---------------------------------------------------------
    # Named snapshots of theme assignments (see the style_profiles table comment in
    # _init_db). The picker reads the lightweight summary; a switch reads the full blob.

    def get_style_profiles(self) -> list:
        """Lightweight profile list for the picker (id/name/sort_order, no data blob)."""
        with self._db_transaction(commit=False) as cur:
            cur.execute("SELECT id, name, sort_order FROM style_profiles "
                        "ORDER BY sort_order ASC, id ASC")
            return [dict(r) for r in cur.fetchall()]

    def get_style_profile(self, profile_id):
        """One profile with its `data` blob parsed to a dict, or None."""
        with self._db_transaction(commit=False) as cur:
            cur.execute("SELECT id, name, sort_order, data FROM style_profiles WHERE id = ?",
                        (profile_id,))
            row = cur.fetchone()
            if not row:
                return None
            d = dict(row)
            d['data'] = self._parse_json_field(d.get('data'), {}) or {}
            return d

    def create_style_profile(self, name, data=None) -> int:
        """Insert a profile, appended to the end of the display order. `data` is the
        snapshot dict (JSON-encoded here)."""
        with self._db_transaction() as cur:
            cur.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM style_profiles")
            sort_order = cur.fetchone()[0]
            cur.execute("INSERT INTO style_profiles (name, sort_order, data) VALUES (?, ?, ?)",
                        (name, sort_order, json.dumps(data or {})))
            return cur.lastrowid

    def rename_style_profile(self, profile_id, name):
        with self._db_transaction() as cur:
            cur.execute("UPDATE style_profiles SET name = ? WHERE id = ?", (name, profile_id))

    def save_style_profile_data(self, profile_id, data):
        """Overwrite a profile's snapshot blob (used to capture the live state into the
        active profile before switching away)."""
        with self._db_transaction() as cur:
            cur.execute("UPDATE style_profiles SET data = ? WHERE id = ?",
                        (json.dumps(data or {}), profile_id))

    def delete_style_profile(self, profile_id):
        with self._db_transaction() as cur:
            cur.execute("DELETE FROM style_profiles WHERE id = ?", (profile_id,))

    def set_songs_theme_maps(self, mapping):
        """Bulk-update songs.theme_map from {song_id: theme_map_dict} in one transaction
        (used when applying a style profile to the live library)."""
        if not mapping:
            return
        with self._db_transaction() as cur:
            cur.executemany("UPDATE songs SET theme_map = ? WHERE id = ?",
                            [(json.dumps(tm or {}), sid) for sid, tm in mapping.items()])

    def set_ann_items_theme_maps(self, mapping):
        """Bulk-update ann_items.theme_map from {item_id: theme_map_dict} in one transaction."""
        if not mapping:
            return
        with self._db_transaction() as cur:
            cur.executemany("UPDATE ann_items SET theme_map = ? WHERE id = ?",
                            [(json.dumps(tm or {}), iid) for iid, tm in mapping.items()])


__all__ = [
    'DatabaseManager',
    'sqlite3',
]
