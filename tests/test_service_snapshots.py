"""Service items are independent snapshots of library content (copy-on-add)."""
import json
import os
import tempfile
import unittest

from seventhslide.database import DatabaseManager


class ServiceSnapshotTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.db = DatabaseManager(os.path.join(self._td.name, 'test.db'))

    def tearDown(self):
        self.db.close()
        self._td.cleanup()

    def _add_library_song(self, **overrides):
        fields = dict(
            title='Hello',
            lyrics='---[Verse:1]---\nLine one\n',
            verse_order='v1',
            authors=['Writer'],
            songbook_name='Book',
            songbook_entry='1',
            theme_map={'Main': {'text': 't1'}},
            copyright='© 2020',
            ccli_song_number='111',
            show_copyright=True,
            key='G',
        )
        fields.update(overrides)
        return self.db.add_song(**fields)

    def test_add_snapshots_full_metadata(self):
        sid = self._add_library_song()
        svc = self.db.create_service('Sunday')
        self.db.add_song_to_service(svc, sid)
        items = self.db.get_service_items(svc)
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual(it['title'], 'Hello')
        self.assertIn('Line one', it['lyrics'])
        self.assertEqual(it['authors'], ['Writer'])
        self.assertEqual(it['copyright'], '© 2020')
        self.assertTrue(it['show_copyright'])
        self.assertEqual(it['ccli_song_number'], '111')
        self.assertEqual(it['key'], 'G')
        self.assertEqual(it['theme_map'], {'Main': {'text': 't1'}})
        self.assertFalse(it['has_overrides'])
        self.assertEqual(it['song_id'], sid)

    def test_library_edit_does_not_change_service_item(self):
        sid = self._add_library_song()
        svc = self.db.create_service('Sunday')
        self.db.add_song_to_service(svc, sid)

        self.db.update_song(
            sid, 'Changed', '---[Verse:1]---\nNew lyrics\n', 'v1',
            authors=['Other'], songbook_name='X', songbook_entry='2',
            theme_map={'Main': {'text': 'other'}}, copyright='© NEW',
            ccli_song_number='999', show_copyright=False, key='A')

        it = self.db.get_service_items(svc)[0]
        self.assertEqual(it['title'], 'Hello')
        self.assertIn('Line one', it['lyrics'])
        self.assertEqual(it['authors'], ['Writer'])
        self.assertEqual(it['copyright'], '© 2020')
        self.assertTrue(it['show_copyright'])
        self.assertEqual(it['theme_map'], {'Main': {'text': 't1'}})

    def test_delete_library_song_keeps_service_snapshot(self):
        sid = self._add_library_song()
        svc = self.db.create_service('Sunday')
        self.db.add_song_to_service(svc, sid)
        self.db.delete_song(sid)

        items = self.db.get_service_items(svc)
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertIsNone(it['song_id'])
        self.assertEqual(it['title'], 'Hello')
        self.assertIn('Line one', it['lyrics'])
        self.assertEqual(it['copyright'], '© 2020')
        self.assertTrue(it['has_overrides'])

    def test_reset_re_copies_from_library(self):
        sid = self._add_library_song()
        svc = self.db.create_service('Sunday')
        self.db.add_song_to_service(svc, sid)
        item_id = self.db.get_service_items(svc)[0]['item_id']

        self.db.update_service_item(item_id, {
            'user_modified': True,
            'title': 'Edited in service',
            'lyrics': '---[Verse:1]---\nService only\n',
            'verse_order': 'v1',
            'theme_map': {},
            'authors': [],
            'copyright': '',
            'show_copyright': False,
            'ccli_song_number': '',
            'key': '',
            'songbook_name': '',
            'songbook_entry': '',
        })

        self.db.update_song(
            sid, 'Library New', '---[Verse:1]---\nLibrary lyrics\n', 'v1',
            authors=['Lib'], songbook_name='', songbook_entry='',
            theme_map={}, copyright='© Lib', ccli_song_number='',
            show_copyright=True, key='C')

        new_data, err = self.db.compute_updated_service_item_data(
            {'reset': True}, 'song', {}, sid)
        self.assertIsNone(err)
        self.db.update_service_item(item_id, new_data)
        it = self.db.get_service_items(svc)[0]
        self.assertEqual(it['title'], 'Library New')
        self.assertIn('Library lyrics', it['lyrics'])
        self.assertEqual(it['copyright'], '© Lib')
        self.assertFalse(it['has_overrides'])

    def test_reset_fails_when_library_song_gone(self):
        sid = self._add_library_song()
        svc = self.db.create_service('Sunday')
        self.db.add_song_to_service(svc, sid)
        self.db.delete_song(sid)
        it = self.db.get_service_items(svc)[0]
        self.assertIsNone(it['song_id'])

        new_data, err = self.db.compute_updated_service_item_data(
            {'reset': True}, 'song', {'title': 'Hello'}, None)
        self.assertIsNone(new_data)
        self.assertIn('no longer linked', err)

        new_data, err = self.db.compute_updated_service_item_data(
            {'reset': True}, 'song', {'title': 'Hello'}, sid)
        self.assertIsNone(new_data)
        self.assertIn('no longer exists', err)

    def test_backfill_legacy_snapshot_missing_copyright(self):
        sid = self._add_library_song()
        svc = self.db.create_service('Sunday')
        # Insert a legacy-shaped snapshot (pre-copyright fields) directly.
        with self.db._db_transaction() as cur:
            cur.execute(
                "INSERT INTO service_items (service_id, song_id, order_num, item_type, data) "
                "VALUES (?, ?, 0, 'song', ?)",
                (svc, sid, json.dumps({
                    'user_modified': False,
                    'title': 'Hello',
                    'lyrics': '---[Verse:1]---\nLine one\n',
                    'verse_order': 'v1',
                    'theme_map': {'Main': {'text': 't1'}},
                })))
            # Constructor already ran the one-shot; clear the flag so we can re-run.
            cur.execute("DELETE FROM app_settings WHERE key = ?",
                        ('song_snapshot_copyright_backfilled',))

        # Read path must not fill gaps once the one-shot has already run.
        self.db.save_app_settings({'song_snapshot_copyright_backfilled': True})
        it = self.db.get_service_items(svc)[0]
        self.assertEqual(it.get('copyright') or '', '')
        self.assertNotIn('song_theme_map', it)

        # One-shot backfill fills and persists, then sets the flag.
        self.db.save_app_settings({'song_snapshot_copyright_backfilled': False})
        self.db.backfill_song_snapshot_copyright()
        it = self.db.get_service_items(svc)[0]
        self.assertEqual(it['copyright'], '© 2020')
        self.assertTrue(it['show_copyright'])
        self.assertEqual(it['authors'], ['Writer'])
        self.assertNotIn('song_theme_map', it)
        self.assertTrue(self.db.load_app_settings().get('song_snapshot_copyright_backfilled'))

        # Persisted into data for independence
        with self.db._db_transaction(commit=False) as cur:
            cur.execute("SELECT data FROM service_items WHERE service_id = ?", (svc,))
            snap = json.loads(cur.fetchone()['data'])
        self.assertEqual(snap['copyright'], '© 2020')
        self.assertIn('authors', snap)

        # Second call is a no-op (flag already set).
        self.db.backfill_song_snapshot_copyright()

    def test_legacy_override_resolves_without_join(self):
        """Override-shaped data (no title key) still gets library lyrics via batch fetch."""
        sid = self._add_library_song()
        svc = self.db.create_service('Sunday')
        with self.db._db_transaction() as cur:
            cur.execute(
                "INSERT INTO service_items (service_id, song_id, order_num, item_type, data) "
                "VALUES (?, ?, 0, 'song', ?)",
                (svc, sid, json.dumps({
                    'lyrics': '---[Verse:1]---\nOverride only\n',
                    'theme_map': {'Main': {'text': 'x'}},
                })))
            # Prevent constructor backfill from rewriting this row mid-test setup:
            # DatabaseManager already ran backfills; insert after means we test the
            # read-time legacy path (skip full backfill by leaving flag set).
            cur.execute(
                "INSERT INTO app_settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ('song_snapshot_full_backfilled', json.dumps(True)))

        it = self.db.get_service_items(svc)[0]
        self.assertEqual(it['title'], 'Hello')  # from library
        self.assertIn('Override only', it['lyrics'])  # overlay wins
        self.assertTrue(it['has_overrides'])
        self.assertEqual(it['theme_map'], {'Main': {'text': 'x'}})

        one = self.db.get_service_item(it['item_id'])
        self.assertEqual(one['title'], 'Hello')
        self.assertIn('Override only', one['lyrics'])

    def test_backfill_legacy_overrides_to_full_snapshot(self):
        sid = self._add_library_song()
        svc = self.db.create_service('Sunday')
        with self.db._db_transaction() as cur:
            cur.execute(
                "INSERT INTO service_items (service_id, song_id, order_num, item_type, data) "
                "VALUES (?, ?, 0, 'song', ?)",
                (svc, sid, json.dumps({
                    'lyrics': '---[Verse:1]---\nOverride only\n',
                    'theme_map': {},
                })))
            cur.execute("DELETE FROM app_settings WHERE key = ?",
                        ('song_snapshot_full_backfilled',))

        self.db.backfill_legacy_song_overrides()
        with self.db._db_transaction(commit=False) as cur:
            cur.execute("SELECT data FROM service_items WHERE service_id = ?", (svc,))
            snap = json.loads(cur.fetchone()['data'])
        self.assertEqual(snap['title'], 'Hello')
        self.assertIn('Override only', snap['lyrics'])
        self.assertIn('copyright', snap)
        self.assertTrue(self.db.load_app_settings().get('song_snapshot_full_backfilled'))

    def test_video_theme_update_preserves_media_payload(self):
        """Saving a theme_map on a video item must not wipe filename/autoplay/loop."""
        svc = self.db.create_service('Sunday')
        self.db.add_video_to_service(svc, {
            'filename': 'clip.mp4',
            'title': 'Welcome Clip',
            'autoplay': True,
            'loop': False,
        })
        it = self.db.get_service_items(svc)[0]
        self.assertEqual(it['title'], 'Welcome Clip')
        self.assertFalse(it['has_overrides'])
        self.assertEqual(it['theme_map'], {})

        theme_map = {'Main': {'text': 't1', 'bg': 'b1'}}
        existing = {
            'filename': 'clip.mp4',
            'title': 'Welcome Clip',
            'autoplay': True,
            'loop': False,
        }
        new_data, err = self.db.compute_updated_service_item_data(
            {'theme_map': theme_map}, 'video', existing, None)
        self.assertIsNone(err)
        self.assertEqual(new_data['filename'], 'clip.mp4')
        self.assertEqual(new_data['title'], 'Welcome Clip')
        self.assertTrue(new_data['autoplay'])
        self.assertFalse(new_data['loop'])
        self.assertEqual(new_data['theme_map'], theme_map)

        self.db.update_service_item(it['item_id'], new_data)
        resolved = self.db.get_service_items(svc)[0]
        self.assertEqual(resolved['title'], 'Welcome Clip')
        self.assertEqual(resolved['theme_map'], theme_map)
        self.assertTrue(resolved['has_overrides'])

        # Raw payload still has media fields (resolve only surfaces title helpers).
        with self.db._db_transaction(commit=False) as cur:
            cur.execute("SELECT data FROM service_items WHERE id = ?", (it['item_id'],))
            raw = json.loads(cur.fetchone()['data'])
        self.assertEqual(raw['filename'], 'clip.mp4')
        self.assertTrue(raw['autoplay'])
        self.assertFalse(raw['loop'])

        # Reset strips theme_map only.
        reset_data, err = self.db.compute_updated_service_item_data(
            {'reset': True}, 'video', dict(raw), None)
        self.assertIsNone(err)
        self.assertNotIn('theme_map', reset_data or {})
        self.assertEqual(reset_data['filename'], 'clip.mp4')
        self.assertTrue(reset_data['autoplay'])
        self.db.update_service_item(it['item_id'], reset_data)
        after = self.db.get_service_items(svc)[0]
        self.assertEqual(after['theme_map'], {})
        self.assertFalse(after['has_overrides'])
        self.assertEqual(after['title'], 'Welcome Clip')


if __name__ == '__main__':
    unittest.main()
