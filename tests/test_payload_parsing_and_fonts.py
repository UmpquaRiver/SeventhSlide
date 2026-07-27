"""Admin payload slimming, XML parse hardening, fonts, and select-item helpers."""
import io
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from seventhslide.database import DatabaseManager
from seventhslide.parsing import _safe_xml_parse
from seventhslide.render_safe import _clamp_size_pct


class AdminServiceItemsPayloadTests(unittest.TestCase):
    def test_strips_lyrics(self):
        from lyrics import ConnectionManager
        items = [
            {'item_id': 1, 'item_type': 'song', 'title': 'A', 'lyrics': 'line\n'},
            {'item_id': 2, 'item_type': 'video', 'title': 'V'},
        ]
        slim = ConnectionManager._admin_service_items_payload(items)
        self.assertNotIn('lyrics', slim[0])
        self.assertEqual(slim[0]['title'], 'A')
        self.assertEqual(slim[1]['title'], 'V')
        # Original untouched
        self.assertIn('lyrics', items[0])


class ClampSizePctTests(unittest.TestCase):
    def test_clamp_bounds(self):
        self.assertEqual(_clamp_size_pct(100), 100)
        self.assertEqual(_clamp_size_pct(5), 10)
        self.assertEqual(_clamp_size_pct(999), 400)
        self.assertEqual(_clamp_size_pct('bad'), 100)

    def test_lyrics_imports_shared_clamp(self):
        import lyrics
        from seventhslide import render_safe
        self.assertIs(lyrics._clamp_size_pct, render_safe._clamp_size_pct)


class SafeXmlParseTests(unittest.TestCase):
    def test_parses_without_doctype(self):
        with tempfile.NamedTemporaryFile('w', suffix='.xml', delete=False) as f:
            f.write('<?xml version="1.0"?><root><child>hi</child></root>')
            path = f.name
        try:
            tree = _safe_xml_parse(path)
            self.assertEqual(tree.getroot().find('child').text, 'hi')
        finally:
            os.unlink(path)

    def test_rejects_doctype(self):
        with tempfile.NamedTemporaryFile('w', suffix='.xml', delete=False) as f:
            f.write('<!DOCTYPE foo [<!ENTITY x "y">]><root/>')
            path = f.name
        try:
            with self.assertRaises(RuntimeError):
                _safe_xml_parse(path)
        finally:
            os.unlink(path)


class GetServiceItemTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.db = DatabaseManager(os.path.join(self._td.name, 't.db'))

    def tearDown(self):
        self.db.close()
        self._td.cleanup()

    def test_get_service_item_roundtrip(self):
        sid = self.db.add_song(
            'Hello', '---[Verse:1]---\nLine\n', 'v1',
            authors=[], songbook_name='', songbook_entry='',
            theme_map={}, copyright='', ccli_song_number='',
            show_copyright=False, key='')
        svc = self.db.create_service('S')
        self.db.add_song_to_service(svc, sid)
        items = self.db.get_service_items(svc)
        self.assertEqual(len(items), 1)
        full = self.db.get_service_item(items[0]['item_id'])
        self.assertIsNotNone(full)
        self.assertIn('Line', full['lyrics'])
        self.assertEqual(full['title'], 'Hello')
        self.assertIsNone(self.db.get_service_item(999999))


class FontCacheDebounceTests(unittest.TestCase):
    def test_schedule_coalesces_saves(self):
        import seventhslide.fonts as fonts
        # Isolate disk cache globals for this test.
        fonts._FC_DISK_CACHE = {}
        fonts._FC_DISK_CACHE_PATH = os.path.join(tempfile.gettempdir(), 'ss-font-test-cache.json')
        fonts._FC_DISK_CACHE_DIRTY = False
        with fonts._FC_DISK_SAVE_LOCK:
            if fonts._FC_DISK_SAVE_TIMER is not None:
                fonts._FC_DISK_SAVE_TIMER.cancel()
                fonts._FC_DISK_SAVE_TIMER = None

        calls = []
        real_save = fonts._fc_disk_cache_save

        def counting_save():
            calls.append(1)
            # Cancel further timer work; don't need real disk for the count.
            fonts._FC_DISK_CACHE_DIRTY = False

        with mock.patch.object(fonts, '_fc_disk_cache_save', counting_save):
            fonts._fc_disk_cache_schedule_save()
            fonts._fc_disk_cache_schedule_save()
            fonts._fc_disk_cache_schedule_save()
            # Before debounce fires, no save yet.
            self.assertEqual(len(calls), 0)
            time.sleep(fonts._FC_DISK_SAVE_DEBOUNCE_S + 0.4)
            self.assertEqual(len(calls), 1)

        with fonts._FC_DISK_SAVE_LOCK:
            if fonts._FC_DISK_SAVE_TIMER is not None:
                fonts._FC_DISK_SAVE_TIMER.cancel()
                fonts._FC_DISK_SAVE_TIMER = None
        fonts._FC_DISK_CACHE = None
        fonts._FC_DISK_CACHE_PATH = None


class CoerceSelectItemIndexTests(unittest.TestCase):
    """api_select_item uses _coerce_int; verify the helper used there."""

    def test_coerce_int_string(self):
        from lyrics import _coerce_int
        self.assertEqual(_coerce_int('3', -1), 3)
        self.assertEqual(_coerce_int(None, -1), -1)
        self.assertEqual(_coerce_int('x', -1), -1)


if __name__ == '__main__':
    unittest.main()
