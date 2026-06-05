"""SQLite persistence layer (DatabaseManager) and the Song transfer object's store."""
import os
import re
import json
import sqlite3
import threading
from contextlib import contextmanager

from .paths import logger, get_data_dir
from .models import SIMPLE_SERVICE_ITEM_PARSERS



import sqlite3


class DatabaseManager:
    def __init__(self, db_path=None):
        if db_path is None:
            db_path = os.path.join(get_data_dir(), 'songs.db')
        self.db_path = db_path
        self._local = threading.local()
        self._init_db()

    def _get_conn(self):
        # Return the cached per-thread connection without a proactive `SELECT 1`
        # liveness probe — an in-process threading.local connection effectively
        # never dies silently, and the probe taxed every DB call. Recovery from a
        # genuinely dead connection happens lazily in _db_transaction.
        conn = getattr(self._local, 'conn', None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            # Keep the WAL from growing unbounded between checkpoints (the long-lived
            # per-thread read connections can otherwise hold it open for a long time).
            conn.execute("PRAGMA wal_autocheckpoint=400")
            self._local.conn = conn
        return conn

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
        conn = self._get_conn()
        conn.execute('PRAGMA journal_mode=WAL')
        cur = conn.cursor()
        # Songs table
        cur.execute('''CREATE TABLE IF NOT EXISTS songs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            lyrics TEXT NOT NULL,
            verse_order TEXT
        )''')
        # Migration: Add columns if not exists
        def _add_column(table, column, col_type):
            try:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
            except sqlite3.OperationalError:
                pass

        # Add all song metadata columns
        song_columns = [
            ('verse_order', 'TEXT'),
            ('authors', 'TEXT'),
            ('songbook_name', 'TEXT'),
            ('songbook_entry', 'TEXT'),
            ('theme_map', 'TEXT'),
            ('copyright', 'TEXT'),
            ('ccli_song_number', 'TEXT'),
            ('show_copyright', 'INTEGER DEFAULT 0'),
            ('key', 'TEXT'),
        ]
        for col_name, col_type in song_columns:
            _add_column('songs', col_name, col_type)
        # The library list and every admin snapshot read songs ORDER BY title.
        cur.execute("CREATE INDEX IF NOT EXISTS idx_songs_title ON songs(title)")

        # Announcement templates (global, define field names/count)
        cur.execute('''CREATE TABLE IF NOT EXISTS ann_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            field_names TEXT NOT NULL
        )''')

        # Announcement library records
        cur.execute('''CREATE TABLE IF NOT EXISTS announcements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            template_id INTEGER NOT NULL REFERENCES ann_templates(id) ON DELETE CASCADE,
            title TEXT NOT NULL DEFAULT '',
            field_values TEXT NOT NULL DEFAULT '[]'
        )''')
        _add_column('announcements', 'theme_map', 'TEXT')

        # Maps the random on-disk filename for an uploaded image to the original
        # human-readable name shown in the UI. Lets duplicate display names coexist
        # (e.g. two uploads called "slide.jpg" each get a unique on-disk filename).
        cur.execute('''CREATE TABLE IF NOT EXISTS image_files (
            filename TEXT PRIMARY KEY,
            display_name TEXT NOT NULL
        )''')
        # library_visible=0 means the user "deleted" the image from the library but a
        # service still references it; the file stays on disk until no service does.
        _add_column('image_files', 'library_visible', 'INTEGER DEFAULT 1')

        # Per-output visual layouts for each template
        cur.execute('''CREATE TABLE IF NOT EXISTS ann_template_layouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            template_id INTEGER NOT NULL REFERENCES ann_templates(id) ON DELETE CASCADE,
            output_name TEXT NOT NULL,
            background_type TEXT DEFAULT 'color',
            background_value TEXT DEFAULT '#000000',
            text_boxes TEXT NOT NULL,
            UNIQUE(template_id, output_name)
        )''')
            
        # Services table
        cur.execute('''CREATE TABLE IF NOT EXISTS services (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL
        )''')

        try:
            cur.execute("ALTER TABLE services ADD COLUMN theme_map TEXT")
        except sqlite3.OperationalError:
            pass
        # Service grouping: optional one-level folders (e.g. an "Evangelistic Series"
        # holding nightly services). group_id NULL = ungrouped; sort_order drives manual
        # ordering within a bucket (a group, or the ungrouped list).
        cur.execute('''CREATE TABLE IF NOT EXISTS service_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            sort_order INTEGER DEFAULT 0
        )''')
        _add_column('services', 'group_id', 'INTEGER')
        _add_column('services', 'sort_order', 'INTEGER DEFAULT 0')
        cur.execute("CREATE INDEX IF NOT EXISTS idx_services_group ON services(group_id)")
        # Service Items table (polymorphic: songs, bibles, announcements, …)
        cur.execute('''CREATE TABLE IF NOT EXISTS service_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            service_id INTEGER NOT NULL,
            song_id INTEGER,
            order_num INTEGER NOT NULL,
            item_type TEXT DEFAULT 'song',
            data TEXT,
            FOREIGN KEY(service_id) REFERENCES services(id) ON DELETE CASCADE,
            FOREIGN KEY(song_id) REFERENCES songs(id) ON DELETE CASCADE
        )''')

        # Bibles tables
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

        # Image folders tables
        cur.execute('''CREATE TABLE IF NOT EXISTS image_folders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            sort_order INTEGER DEFAULT 0
        )''')
        cur.execute('''CREATE TABLE IF NOT EXISTS image_folder_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            folder_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            sort_order INTEGER DEFAULT 0,
            FOREIGN KEY (folder_id) REFERENCES image_folders(id) ON DELETE CASCADE
        )''')
        # Nesting: parent_id (NULL = top level) lets folders nest to any depth. Added via
        # migration without an inline FK so it applies cleanly to existing tables; the
        # cascade for nested subfolders is handled explicitly in delete_image_folder.
        _add_column('image_folders', 'parent_id', 'INTEGER')
        cur.execute("CREATE INDEX IF NOT EXISTS idx_image_folders_parent ON image_folders(parent_id)")

        # Application configuration (formerly config.json). Scalar app settings live
        # as JSON-encoded key/value rows; each output is one row holding its
        # OutputConfig.to_persist_dict() blob, ordered by order_num.
        cur.execute('''CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )''')
        cur.execute('''CREATE TABLE IF NOT EXISTS outputs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_num INTEGER NOT NULL DEFAULT 0,
            data TEXT NOT NULL
        )''')

        conn.commit()

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

    # --- Announcement Templates ---

    def get_ann_templates(self):
        with self._db_transaction(commit=False) as cur:
            cur.execute("SELECT id, name, field_names FROM ann_templates ORDER BY name ASC")
            rows = cur.fetchall()
            result = []
            for r in rows:
                d = dict(r)
                d['field_names'] = self._parse_json_field(d['field_names'], [])
                result.append(d)
            return result

    def get_ann_template(self, template_id):
        with self._db_transaction(commit=False) as cur:
            cur.execute("SELECT id, name, field_names FROM ann_templates WHERE id = ?", (template_id,))
            row = cur.fetchone()
            if not row:
                return None
            d = dict(row)
            d['field_names'] = self._parse_json_field(d['field_names'], [])
            return d

    def create_ann_template(self, name, field_names):
        with self._db_transaction() as cur:
            cur.execute("INSERT INTO ann_templates (name, field_names) VALUES (?, ?)",
                        (name, json.dumps(field_names)))
            return cur.lastrowid

    def update_ann_template(self, template_id, name):
        with self._db_transaction() as cur:
            cur.execute("UPDATE ann_templates SET name = ? WHERE id = ?", (name, template_id))

    def delete_ann_template(self, template_id):
        with self._db_transaction() as cur:
            cur.execute("DELETE FROM ann_templates WHERE id = ?", (template_id,))

    def get_ann_template_layouts(self, template_id):
        """Returns dict keyed by output_name."""
        with self._db_transaction(commit=False) as cur:
            cur.execute("SELECT * FROM ann_template_layouts WHERE template_id = ?", (template_id,))
            rows = cur.fetchall()
            result = {}
            for r in rows:
                d = dict(r)
                d['text_boxes'] = self._parse_json_field(d['text_boxes'], [])
                result[d['output_name']] = d
            return result

    def upsert_ann_template_layout(self, template_id, output_name, background_type, background_value, text_boxes):
        with self._db_transaction() as cur:
            cur.execute('''
                INSERT INTO ann_template_layouts (template_id, output_name, background_type, background_value, text_boxes)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(template_id, output_name) DO UPDATE SET
                    background_type = excluded.background_type,
                    background_value = excluded.background_value,
                    text_boxes = excluded.text_boxes
            ''', (template_id, output_name, background_type, background_value, json.dumps(text_boxes)))

    def delete_ann_template_layout(self, template_id, output_name):
        with self._db_transaction() as cur:
            cur.execute("DELETE FROM ann_template_layouts WHERE template_id = ? AND output_name = ?",
                        (template_id, output_name))

    # ---- Announcement library CRUD ----

    def get_all_announcements(self):
        with self._db_transaction(commit=False) as cur:
            cur.execute('''SELECT a.id, a.template_id, a.title, a.field_values, a.theme_map,
                                  t.name AS template_name, t.field_names
                           FROM announcements a
                           LEFT JOIN ann_templates t ON a.template_id = t.id
                           ORDER BY a.title ASC''')
            rows = cur.fetchall()
            result = []
            for r in rows:
                d = dict(r)
                d['field_values'] = self._parse_json_field(d['field_values'], [])
                d['field_names'] = self._parse_json_field(d['field_names'], []) if d.get('field_names') else []
                d['theme_map'] = self._parse_json_field(d.get('theme_map'), {})
                result.append(d)
            return result

    def get_announcement(self, announcement_id):
        with self._db_transaction(commit=False) as cur:
            cur.execute('''SELECT a.id, a.template_id, a.title, a.field_values, a.theme_map,
                                  t.name AS template_name, t.field_names
                           FROM announcements a
                           LEFT JOIN ann_templates t ON a.template_id = t.id
                           WHERE a.id = ?''', (announcement_id,))
            row = cur.fetchone()
            if not row:
                return None
            d = dict(row)
            d['field_values'] = self._parse_json_field(d['field_values'], [])
            d['field_names'] = self._parse_json_field(d['field_names'], []) if d.get('field_names') else []
            d['theme_map'] = self._parse_json_field(d.get('theme_map'), {})
            return d

    def create_announcement(self, template_id, title, field_values, theme_map=None):
        with self._db_transaction() as cur:
            cur.execute("INSERT INTO announcements (template_id, title, field_values, theme_map) VALUES (?, ?, ?, ?)",
                        (template_id, title, json.dumps(field_values), json.dumps(theme_map or {})))
            return cur.lastrowid

    def update_announcement(self, announcement_id, title, field_values, theme_map=None):
        with self._db_transaction() as cur:
            if theme_map is None:
                cur.execute("UPDATE announcements SET title = ?, field_values = ? WHERE id = ?",
                            (title, json.dumps(field_values), announcement_id))
            else:
                cur.execute("UPDATE announcements SET title = ?, field_values = ?, theme_map = ? WHERE id = ?",
                            (title, json.dumps(field_values), json.dumps(theme_map or {}), announcement_id))

    def delete_announcement(self, announcement_id):
        with self._db_transaction() as cur:
            cur.execute("DELETE FROM announcements WHERE id = ?", (announcement_id,))

    def add_announcement_to_service(self, service_id, template_id, field_values, title=''):
        with self._db_transaction() as cur:
            self._insert_service_item(cur, service_id, 'announcement',
                                      {'template_id': template_id, 'field_values': field_values, 'title': title})

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
        with self._db_transaction() as cur:
            cur.execute("DELETE FROM songs WHERE id = ?", (song_id,))
            # Referencing service_items rows are removed by ON DELETE CASCADE.

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

    def reorder_services(self, ordered_ids):
        """Apply an explicit order to a set of sibling services (one bucket)."""
        with self._db_transaction() as cur:
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

    def _insert_service_item(self, cur, service_id, item_type, data, song_id=None):
        """Append a service item of the given type, JSON-encoding its data payload."""
        next_order = self._get_next_service_item_order(cur, service_id)
        cur.execute(
            "INSERT INTO service_items (service_id, song_id, order_num, item_type, data) VALUES (?, ?, ?, ?, ?)",
            (service_id, song_id, next_order, item_type, json.dumps(data))
        )
        return cur.lastrowid

    def add_song_to_service(self, service_id, song_id):
        with self._db_transaction() as cur:
            cur.execute("SELECT title, lyrics, verse_order, theme_map FROM songs WHERE id = ?", (song_id,))
            row = cur.fetchone()
            snapshot = {'user_modified': False}
            if row:
                snapshot['title'] = row['title'] or ''
                snapshot['lyrics'] = row['lyrics'] or ''
                snapshot['verse_order'] = row['verse_order'] or ''
                snapshot['theme_map'] = self._parse_json_field(row['theme_map'], {})
            self._insert_service_item(cur, service_id, 'song', snapshot, song_id=song_id)

    def add_songs_to_service(self, service_id, song_ids):
        """Batch add: snapshot each song's title/lyrics/verse_order/theme_map and append
        as a service item, all in one transaction. Skips ids that don't exist."""
        if not song_ids:
            return 0
        added = 0
        with self._db_transaction() as cur:
            placeholders = ','.join('?' * len(song_ids))
            cur.execute(f"SELECT id, title, lyrics, verse_order, theme_map FROM songs WHERE id IN ({placeholders})",
                        list(song_ids))
            rows_by_id = {r['id']: r for r in cur.fetchall()}
            for sid in song_ids:
                row = rows_by_id.get(sid)
                if not row:
                    continue
                snapshot = {
                    'user_modified': False,
                    'title': row['title'] or '',
                    'lyrics': row['lyrics'] or '',
                    'verse_order': row['verse_order'] or '',
                    'theme_map': self._parse_json_field(row['theme_map'], {}),
                }
                self._insert_service_item(cur, service_id, 'song', snapshot, song_id=sid)
                added += 1
            return added

    def add_bible_to_service(self, service_id, bible_data):
        with self._db_transaction() as cur:
            self._insert_service_item(cur, service_id, 'bible', bible_data)

    def add_video_to_service(self, service_id, video_data):
        with self._db_transaction() as cur:
            self._insert_service_item(cur, service_id, 'video', video_data)

    def remove_items_from_service(self, item_ids):
        """Batch delete service items in one transaction. Returns the number deleted."""
        if not item_ids:
            return 0
        with self._db_transaction() as cur:
            placeholders = ','.join('?' * len(item_ids))
            cur.execute(f"DELETE FROM service_items WHERE id IN ({placeholders})", list(item_ids))
            return cur.rowcount

    def delete_songs(self, song_ids):
        """Batch delete library songs in one transaction. Returns the number deleted."""
        if not song_ids:
            return 0
        with self._db_transaction() as cur:
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

    def compute_updated_service_item_data(self, update, item_type, existing_data, song_id):
        """Build the new `data` payload for a service-item update.

        On reset, re-snapshots from the library (songs/announcements) or strips overrides
        (bible); otherwise merges the provided title/lyrics/verse_order/theme_map onto the
        existing payload. Returns the dict to persist (or None to clear).
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
            new_data = {}
        for key in ('title', 'lyrics', 'verse_order', 'theme_map'):
            if key in update:
                new_data[key] = update[key]
        return new_data

    def _reset_service_item_data(self, item_type, existing_data, song_id):
        """Re-snapshot a service item from its library source (reset path)."""
        if item_type == 'bible':
            # Keep ref/bible_id fields but remove overrides
            existing_data.pop('theme_map', None)
            return existing_data if existing_data else None
        if item_type == 'song':
            new_data = {'user_modified': False}
            if song_id:
                with self._db_transaction(commit=False) as cur:
                    cur.execute("SELECT title, lyrics, verse_order, theme_map FROM songs WHERE id = ?", (song_id,))
                    srow = cur.fetchone()
                if srow:
                    new_data['title'] = srow['title'] or ''
                    new_data['lyrics'] = srow['lyrics'] or ''
                    new_data['verse_order'] = srow['verse_order'] or ''
                    new_data['theme_map'] = self._parse_json_field(srow['theme_map'], {})
            return new_data
        if item_type == 'announcement':
            # Announcement service items are self-contained snapshots with no stored
            # link back to a library announcement, so a reset just drops the per-item
            # theme override and keeps the content (template_id/field_values/title),
            # mirroring the bible reset above.
            existing_data.pop('theme_map', None)
            return existing_data if existing_data else None
        return None

    def get_service_items(self, service_id):
        """Returns items with song details."""
        with self._db_transaction(commit=False) as cur:
            # Use LEFT JOIN to allow null song_id
            query = '''
                SELECT si.id as item_id, si.order_num, si.item_type, si.data,
                       s.id as song_id, s.title, s.lyrics, s.verse_order, s.theme_map as song_theme_map_raw
                FROM service_items si
                LEFT JOIN songs s ON si.song_id = s.id
                WHERE si.service_id = ?
                ORDER BY si.order_num ASC
            '''
            cur.execute(query, (service_id,))
            rows = cur.fetchall()
            ann_template_map = self._load_ann_templates(cur, rows)
            return [self._resolve_service_item(dict(r), ann_template_map) for r in rows]

    def _load_ann_templates(self, cur, rows):
        """Batch-load the announcement templates referenced by the given service rows."""
        ann_template_ids = set()
        for r in rows:
            if r['item_type'] == 'announcement' and r['data']:
                try:
                    tid = json.loads(r['data']).get('template_id')
                    if tid:
                        ann_template_ids.add(tid)
                except Exception:
                    pass
        if not ann_template_ids:
            return {}
        placeholders = ','.join('?' * len(ann_template_ids))
        cur.execute(f"SELECT id, name, field_names FROM ann_templates WHERE id IN ({placeholders})",
                    list(ann_template_ids))
        ann_template_map = {}
        for tr in cur.fetchall():
            td = dict(tr)
            td['field_names'] = self._parse_json_field(td['field_names'], [])
            ann_template_map[td['id']] = td
        return ann_template_map

    def _resolve_service_item(self, d, ann_template_map):
        """Resolve one service-item row dict into its display form, dispatching on item_type."""
        # Parse original song theme_map and set defaults shared by every item type.
        d['song_theme_map'] = self._parse_json_field(d.pop('song_theme_map_raw', None), {})
        d['theme_map'] = {}
        d['has_overrides'] = False

        item_type = d['item_type']
        if not d['data']:
            return d

        if item_type == 'bible':
            self._apply_bible_item(d)
        elif item_type == 'announcement':
            self._apply_announcement_item(d, ann_template_map)
        elif item_type in SIMPLE_SERVICE_ITEM_PARSERS:
            d.update(SIMPLE_SERVICE_ITEM_PARSERS[item_type](self._parse_json_field(d['data'], {})))
            d['lyrics'] = ''
        elif item_type == 'song':
            self._apply_song_item(d)
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
    def _apply_announcement_item(d, ann_template_map):
        try:
            adata = json.loads(d['data'])
            template_id = adata.get('template_id')
            field_values = adata.get('field_values', [])
            tmpl = ann_template_map.get(template_id) if template_id else None
            d['template_id'] = template_id
            d['template_name'] = tmpl['name'] if tmpl else 'Unknown Template'
            d['field_names'] = tmpl['field_names'] if tmpl else []
            d['field_values'] = field_values
            raw_title = adata.get('title') or (field_values[0] if field_values else '')
            d['title'] = re.sub(r'<[^>]+>', '', raw_title).strip() or 'Announcement'
            d['lyrics'] = ''
            d['theme_map'] = adata.get('theme_map') or {}
            d['has_overrides'] = bool(d['theme_map'])
        except json.JSONDecodeError:
            d['title'] = 'Announcement'
            d['lyrics'] = ''
            d['template_id'] = None
            d['field_names'] = []
            d['field_values'] = []

    @staticmethod
    def _apply_song_item(d):
        try:
            snap = json.loads(d['data'])
        except json.JSONDecodeError:
            return
        if 'title' in snap:
            # Snapshot format: data is the source of truth
            d['title'] = snap['title']
            d['lyrics'] = snap.get('lyrics', d.get('lyrics', ''))
            d['verse_order'] = snap.get('verse_order', d.get('verse_order', ''))
            d['theme_map'] = snap.get('theme_map') or {}
            d['has_overrides'] = snap.get('user_modified', True)
        else:
            # Legacy override format: apply on top of live JOIN data
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
            cur.execute('SELECT DISTINCT book FROM verses WHERE bible_id=? ORDER BY id ASC', (bible_id,))
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

    def search_bible(self, bible_id, query):
        with self._db_transaction(commit=False) as cur:
            q = f"%{query}%"
            cur.execute('SELECT book, chapter, verse_num, text FROM verses WHERE bible_id=? AND text LIKE ? LIMIT 50', (bible_id, q))
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
        with self._db_transaction() as cur:
            # sort_order is scoped per parent; siblings of one parent are ordered
            # independently of other branches (tree rendering only compares within a parent).
            cur.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM image_folders WHERE parent_id IS ?",
                (parent_id,)
            )
            next_order = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO image_folders (name, sort_order, parent_id) VALUES (?, ?, ?)",
                (name, next_order, parent_id)
            )
            return cur.lastrowid

    def rename_image_folder(self, folder_id, name):
        with self._db_transaction() as cur:
            cur.execute("UPDATE image_folders SET name = ? WHERE id = ?", (name, folder_id))

    @staticmethod
    def _descendant_folder_ids(cur, folder_id):
        """Return [folder_id] plus every nested descendant folder id (depth-first)."""
        result = [folder_id]
        stack = [folder_id]
        while stack:
            pid = stack.pop()
            cur.execute("SELECT id FROM image_folders WHERE parent_id = ?", (pid,))
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
        with self._db_transaction() as cur:
            if new_parent_id is not None:
                if new_parent_id in self._descendant_folder_ids(cur, folder_id):
                    return False
            cur.execute("UPDATE image_folders SET parent_id = ? WHERE id = ?", (new_parent_id, folder_id))
            if ordered_ids:
                cur.executemany(
                    "UPDATE image_folders SET sort_order = ? WHERE id = ?",
                    list(enumerate(ordered_ids))
                )
            return True

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
        with self._db_transaction(commit=False) as cur:
            cur.execute("SELECT data FROM service_items "
                        "WHERE item_type = 'video' AND data IS NOT NULL")
            rows = cur.fetchall()
        count = 0
        for row in rows:
            try:
                data = json.loads(row['data'])
            except (json.JSONDecodeError, TypeError):
                continue
            if data.get('filename') == filename:
                count += 1
        return count

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
            if os.path.exists(p):
                try: os.unlink(p)
                except OSError: pass
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
            if os.path.exists(p):
                try: os.unlink(p)
                except OSError: pass
        return len(to_unlink)

    def reorder_image_folders(self, ordered_folder_ids):
        with self._db_transaction() as cur:
            cur.executemany(
                "UPDATE image_folders SET sort_order = ? WHERE id = ?",
                list(enumerate(ordered_folder_ids))
            )

    def add_image_to_service(self, service_id, filename):
        with self._db_transaction() as cur:
            self._insert_service_item(cur, service_id, 'image', {'filename': filename})

    def add_images_to_service(self, service_id, filenames):
        """Batch add: append each filename as a standalone single-image service item, one transaction."""
        if not filenames:
            return 0
        with self._db_transaction() as cur:
            for fn in filenames:
                self._insert_service_item(cur, service_id, 'image', {'filename': fn})
        return len(filenames)

    def add_image_folder_to_service(self, service_id, folder_id, folder_name):
        with self._db_transaction() as cur:
            cur.execute(
                "SELECT filename FROM image_folder_items WHERE folder_id = ? ORDER BY sort_order ASC",
                (folder_id,)
            )
            images = [row['filename'] for row in cur.fetchall()]
            self._insert_service_item(cur, service_id, 'image_folder',
                                      {'folder_id': folder_id, 'folder_name': folder_name, 'images': images})

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
            iid = r.get('item_id'); idx = r.get('index')
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
            placeholders = ','.join('?' * len(item_ids))
            cur.execute(f"SELECT id, item_type, data FROM service_items WHERE id IN ({placeholders})", list(item_ids))
            rows = {r['id']: dict(r) for r in cur.fetchall()}
            if to_item_id not in rows:
                return False
            data_by_id = {}
            for iid, row in rows.items():
                if row['item_type'] != 'image_folder':
                    return False
                data_by_id[iid] = self._parse_json_field(row['data'], {})
            # Capture the selected filenames (in selection order) before any removal.
            ordered = []
            for s in selections:
                iid = s.get('item_id'); idx = s.get('index')
                if iid not in data_by_id or idx is None:
                    return False
                imgs = data_by_id[iid].get('images', [])
                if idx < 0 or idx >= len(imgs):
                    return False
                ordered.append((iid, idx, imgs[idx]))
            # Adjust to_index for removals from the target at positions < to_index.
            adjusted_to_index = to_index
            if to_index is not None:
                shifted = sum(1 for (iid, idx, _fn) in ordered if iid == to_item_id and idx < to_index)
                adjusted_to_index = to_index - shifted
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
            for iid, d in data_by_id.items():
                cur.execute("UPDATE service_items SET data = ? WHERE id = ?", (json.dumps(d), iid))
            return True

    def add_divider_to_service(self, service_id, title):
        with self._db_transaction() as cur:
            self._insert_service_item(cur, service_id, 'divider', {'title': title})

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


__all__ = [
    'DatabaseManager',
    'sqlite3',
]
