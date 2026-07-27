"""Database close, root HTML allowlist, and Electron probe checks."""
import ast
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

from seventhslide.database import DatabaseManager

ROOT = Path(__file__).resolve().parents[1]


class DatabaseCloseTests(unittest.TestCase):
    def test_close_drops_thread_connection(self):
        with tempfile.TemporaryDirectory() as td:
            db = DatabaseManager(os.path.join(td, 't.db'))
            conn = db._get_conn()
            self.assertIsNotNone(conn)
            db.close()
            self.assertIsNone(getattr(db._local, 'conn', None))
            # Reconnect works after close.
            again = db._get_conn()
            self.assertIsNotNone(again)
            db.close()

    def test_connect_uses_same_thread_check(self):
        """Per-thread connections keep the default check_same_thread=True."""
        src = (ROOT / 'seventhslide' / 'database.py').read_text(encoding='utf-8')
        self.assertNotIn('check_same_thread=False', src)
        tree = ast.parse(src)
        connects = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'connect'
        ]
        self.assertTrue(connects)
        for call in connects:
            for kw in call.keywords:
                if kw.arg == 'check_same_thread':
                    self.fail('check_same_thread should use sqlite3 default (True)')


class RootHtmlAllowlistTests(unittest.TestCase):
    def setUp(self):
        import lyrics
        self.lyrics = lyrics
        self._td = tempfile.TemporaryDirectory()
        self._prev = lyrics.APP_STATE
        fake = mock.Mock()
        fake.export_dir = self._td.name
        lyrics.APP_STATE = fake
        self.html_name = 'Main.html'
        with open(os.path.join(self._td.name, self.html_name), 'w', encoding='utf-8') as f:
            f.write('<html></html>')
        with open(os.path.join(self._td.name, 'notes.txt'), 'w', encoding='utf-8') as f:
            f.write('nope')

    def tearDown(self):
        self.lyrics.APP_STATE = self._prev
        self._td.cleanup()

    def _call(self, name):
        import asyncio
        return asyncio.run(self.lyrics.get_root_file(name))

    def test_serves_html_basename(self):
        resp = self._call(self.html_name)
        self.assertTrue(os.path.isfile(getattr(resp, 'path', '') or ''))

    def test_rejects_non_html(self):
        with self.assertRaises(HTTPException) as ctx:
            self._call('notes.txt')
        self.assertEqual(ctx.exception.status_code, 404)

    def test_rejects_path_component(self):
        with self.assertRaises(HTTPException):
            self._call('../Main.html')


class ElectronProbeSourceTests(unittest.TestCase):
    def test_probe_requires_http_200(self):
        src = (ROOT / 'electron' / 'server-process.js').read_text(encoding='utf-8')
        self.assertIn('res.statusCode === 200', src)
        self.assertNotIn('statusCode < 500', src)


if __name__ == '__main__':
    unittest.main()
