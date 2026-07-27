"""Static media URLs, video filename normalization, and SQLite busy_timeout."""
import os
import tempfile
import unittest
from unittest import mock

from seventhslide.database import DatabaseManager


class StaticMediaUrlTests(unittest.TestCase):
    def setUp(self):
        # Import after package path is available; lyrics pulls heavy deps at import.
        from lyrics import _static_media_url
        self.url = _static_media_url

    def test_encodes_spaces(self):
        self.assertEqual(
            self.url('videos', 'Worship Set.mp4'),
            '/static/videos/Worship%20Set.mp4')

    def test_strips_directory_component(self):
        self.assertEqual(
            self.url('images', 'subdir/photo.jpg'),
            '/static/images/photo.jpg')

    def test_empty_returns_none(self):
        self.assertIsNone(self.url('videos', ''))
        self.assertIsNone(self.url('videos', None))

    def test_simple_name_unchanged_enough(self):
        self.assertEqual(
            self.url('videos', 'clip.mp4'),
            '/static/videos/clip.mp4')


class NormalizeVideoFilenameTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.videos = os.path.join(self._td.name, 'videos')
        os.makedirs(self.videos)
        # Touch a real file the helper can accept.
        self.good = 'clip.mp4'
        with open(os.path.join(self.videos, self.good), 'wb') as f:
            f.write(b'x')

        import lyrics
        self.lyrics = lyrics
        # Point APP_STATE.export_dir at our temp tree without full init_app.
        self._prev_state = lyrics.APP_STATE
        fake = mock.Mock()
        fake.export_dir = self._td.name
        lyrics.APP_STATE = fake

    def tearDown(self):
        self.lyrics.APP_STATE = self._prev_state
        self._td.cleanup()

    def test_accepts_existing_basename(self):
        self.assertEqual(
            self.lyrics._normalize_video_filename(self.good),
            self.good)

    def test_rejects_missing_file(self):
        self.assertIsNone(self.lyrics._normalize_video_filename('nope.mp4'))

    def test_rejects_bad_extension(self):
        bad = 'notes.txt'
        with open(os.path.join(self.videos, bad), 'wb') as f:
            f.write(b'x')
        self.assertIsNone(self.lyrics._normalize_video_filename(bad))

    def test_path_traversal_cannot_escape_videos_dir(self):
        # Directory components are stripped; an existing basename inside videos is OK.
        self.assertEqual(
            self.lyrics._normalize_video_filename('../' + self.good),
            self.good)
        # A name that only exists outside videos/ must not resolve.
        outside = os.path.join(self._td.name, 'outside.mp4')
        with open(outside, 'wb') as f:
            f.write(b'x')
        self.assertIsNone(
            self.lyrics._normalize_video_filename('../outside.mp4'))

    def test_subdir_prefix_stripped_then_lookup(self):
        # basename becomes clip.mp4 which exists → accepted
        self.assertEqual(
            self.lyrics._normalize_video_filename('subdir/' + self.good),
            self.good)


class BusyTimeoutTests(unittest.TestCase):
    def test_busy_timeout_pragma(self):
        with tempfile.TemporaryDirectory() as td:
            db = DatabaseManager(os.path.join(td, 't.db'))
            try:
                row = db._get_conn().execute('PRAGMA busy_timeout').fetchone()
                self.assertEqual(int(row[0]), 5000)
            finally:
                db.close()


if __name__ == '__main__':
    unittest.main()
