"""Tests for seventhslide.render_safe (CSS/HTML render hardening)."""
import unittest

from seventhslide.render_safe import (
    convert_size_tags,
    escape_rich_text,
    safe_css_color,
    safe_css_url,
    safe_font_family,
    safe_text_align,
)


class SafeCssColorTests(unittest.TestCase):
    def test_hex_forms(self):
        self.assertEqual(safe_css_color('#fff'), '#fff')
        self.assertEqual(safe_css_color('#c9a86a'), '#c9a86a')
        self.assertEqual(safe_css_color('#AABBCCDD'), '#AABBCCDD')

    def test_rejects_injection(self):
        self.assertEqual(safe_css_color('red'), '#ffffff')
        self.assertEqual(safe_css_color('red" onmouseover="x'), '#ffffff')
        self.assertEqual(
            safe_css_color('#fff; } * { color:red', default='#111111'),
            '#111111',
        )


class SafeFontFamilyTests(unittest.TestCase):
    def test_common_names(self):
        self.assertEqual(safe_font_family('Helvetica'), 'Helvetica')
        self.assertEqual(safe_font_family('Helvetica Neue'), 'Helvetica Neue')
        self.assertEqual(
            safe_font_family('Helvetica, Arial, sans-serif'),
            'Helvetica, Arial, sans-serif',
        )

    def test_rejects_quotes_and_css(self):
        self.assertEqual(safe_font_family("Foo'; color:red"), 'Helvetica')
        self.assertEqual(safe_font_family("x</style><script>"), 'Helvetica')
        self.assertEqual(safe_font_family("Evil\\) url("), 'Helvetica')


class SafeTextAlignTests(unittest.TestCase):
    def test_allowlist(self):
        self.assertEqual(safe_text_align('left'), 'left')
        self.assertEqual(safe_text_align('CENTER'), 'center')
        self.assertEqual(safe_text_align('justify'), 'justify')
        self.assertEqual(safe_text_align('middle'), 'center')
        self.assertEqual(safe_text_align('left; color:red'), 'center')


class SafeCssUrlTests(unittest.TestCase):
    def test_static_paths(self):
        self.assertEqual(
            safe_css_url('/static/theme_backgrounds/abc.png'),
            '/static/theme_backgrounds/abc.png',
        )
        self.assertEqual(
            safe_css_url('/static/images/photo.jpg'),
            '/static/images/photo.jpg',
        )

    def test_rejects_unsafe(self):
        self.assertIsNone(safe_css_url("https://evil.example/x.png"))
        self.assertIsNone(safe_css_url("/static/x.png'); background:url('http://e"))
        self.assertIsNone(safe_css_url('/static/../etc/passwd'))
        self.assertIsNone(safe_css_url('/static/foo bar.png'))
        self.assertIsNone(safe_css_url(None))


class EscapeRichTextTests(unittest.TestCase):
    def test_allows_bare_formatting(self):
        self.assertEqual(escape_rich_text('<b>Hi</b>'), '<b>Hi</b>')
        self.assertEqual(escape_rich_text('a\nb'), 'a<br>b')

    def test_strips_attributes_on_allowed_tags(self):
        self.assertEqual(
            escape_rich_text('<b onclick="alert(1)">x</b>'),
            '<b>x</b>',
        )
        self.assertEqual(
            escape_rich_text('<i style="color:red">y</i>'),
            '<i>y</i>',
        )

    def test_size_tags_convert(self):
        out = escape_rich_text('<size=80>Small</size>')
        self.assertIn('font-size:80%', out)
        self.assertIn('Small', out)
        self.assertNotIn('<size=', out)

    def test_disallowed_tags_escaped(self):
        out = escape_rich_text('<img src=x onerror=alert(1)>')
        self.assertNotIn('<img', out.lower())
        self.assertIn('&lt;', out)

    def test_convert_size_tags_helper(self):
        self.assertEqual(
            convert_size_tags('<size=120>A</size>'),
            '<span style="font-size:120%">A</span>',
        )


if __name__ == '__main__':
    unittest.main()
