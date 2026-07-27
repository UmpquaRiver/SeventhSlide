"""Export frame builder uses a cloned AppState (does not mutate live state)."""
import copy
import os
import tempfile
import types
import unittest

from seventhslide.database import DatabaseManager
from seventhslide.models import DEFAULT_THEME_PRIORITY, OutputConfig


class ExportFramesCloneTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.db = DatabaseManager(os.path.join(self._td.name, 'test.db'))
        import lyrics
        self.lyrics = lyrics
        self._prev = lyrics.APP_STATE

        song_id = self.db.add_song(
            title='Hello',
            lyrics='---[Verse:1]---\nLine one\nLine two\n',
            verse_order='v1',
            authors=['Writer'],
            songbook_name='',
            songbook_entry='',
            theme_map={},
            copyright='© 2020',
            ccli_song_number='',
            show_copyright=True,
            key='',
        )
        self.svc = self.db.create_service('Sunday')
        self.db.add_song_to_service(self.svc, song_id)
        # Skipped types should not produce frames.
        self.db.add_video_to_service(self.svc, {
            'filename': 'clip.mp4', 'title': 'Clip', 'loop': False, 'autoplay': True,
        })
        self.db.add_divider_to_service(self.svc, 'Break')
        self.items = self.db.get_service_items(self.svc)

        # Live sentinel: if export mutated live state, these would change.
        live = types.SimpleNamespace(
            db=self.db,
            current_mode='song',
            current_item_index=99,
            current_song_lyrics='LIVE_ONLY',
            current_service_id=self.svc,
            current_service_items=[],
            outputs=[],
        )
        lyrics.APP_STATE = live
        self.live = live

    def tearDown(self):
        self.lyrics.APP_STATE = self._prev
        self.db.close()
        self._td.cleanup()

    def _bundle(self, output_name='Main'):
        oc = OutputConfig(name=output_name)
        return {
            'output_name': output_name,
            'current_service_id': self.svc,
            'current_service_items': copy.deepcopy(self.items),
            'theme_priority': list(DEFAULT_THEME_PRIORITY),
            'bundle_local_fonts': False,
            'ccli_licence_number': '',
            'preview_video_mode': 'still',
            'bundled_font_css_map': {},
            'export_dir': self._td.name,
            'active_profile_id': None,
            'outputs': [OutputConfig.from_dict(copy.deepcopy(oc.to_dict()))],
        }

    def test_build_frames_ok_and_skips_video(self):
        result = self.lyrics._build_service_export_frames(self._bundle())
        self.assertTrue(result.get('ok'), result)
        self.assertGreater(result.get('count', 0), 0)
        self.assertEqual(result['skipped']['videos'], 1)
        self.assertEqual(result['output'], 'Main')
        self.assertEqual(result['frames'][0]['type'], 'state_update')
        # Live APP_STATE must remain untouched by the walk.
        self.assertEqual(self.live.current_mode, 'song')
        self.assertEqual(self.live.current_item_index, 99)
        self.assertEqual(self.live.current_song_lyrics, 'LIVE_ONLY')

    def test_unknown_output_in_bundle(self):
        bundle = self._bundle('Main')
        bundle['output_name'] = 'Missing'
        result = self.lyrics._build_service_export_frames(bundle)
        self.assertFalse(result.get('ok'))
        self.assertEqual(result.get('error'), 'unknown_output')


if __name__ == '__main__':
    unittest.main()
