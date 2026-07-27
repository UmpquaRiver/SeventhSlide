"""Unit tests for lyrics._split_by_words (incremental greedy wrap)."""
import unittest

from lyrics import _split_by_words, wrap_plain_text_to_width


def _len_measure(s: str) -> float:
    return float(len(s))


class SplitByWordsTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(_split_by_words('', _len_measure, 10, 2), [])
        self.assertEqual(_split_by_words('   ', _len_measure, 10, 2), [])

    def test_fits_one_visual_line(self):
        # "aa bb" length 5 fits width 10 → one sub-line
        self.assertEqual(
            _split_by_words('aa bb', _len_measure, 10, 1),
            ['aa bb'],
        )

    def test_flushes_when_visual_lines_exceeded(self):
        # width 5: "aaaa" alone, then "bbbb" alone → with max_visual_lines=1
        # each word is its own sub-line once the next won't fit on the same visual line.
        text = 'aaaa bbbb cccc'
        got = _split_by_words(text, _len_measure, 5, 1)
        self.assertEqual(got, ['aaaa', 'bbbb', 'cccc'])

    def test_two_visual_lines_per_sub_line(self):
        # width 5, max 2 visual lines: "aaaa" + "bbbb" share a sub-line (2 lines),
        # then "cccc" starts the next.
        got = _split_by_words('aaaa bbbb cccc', _len_measure, 5, 2)
        self.assertEqual(got, ['aaaa bbbb', 'cccc'])

    def test_matches_wrap_plain_text_boundaries(self):
        """Each sub-line must wrap to <= max_visual_lines under the same measure."""
        text = 'one two three four five six seven eight nine ten'
        max_w, max_vl = 12, 2
        for sub in _split_by_words(text, _len_measure, max_w, max_vl):
            wrapped = wrap_plain_text_to_width(sub, _len_measure, max_w)
            self.assertLessEqual(len(wrapped), max_vl, msg=repr(sub))

    def test_oversized_single_word(self):
        # Word longer than max_width still becomes its own sub-line.
        got = _split_by_words('abcdefghij next', _len_measure, 5, 1)
        self.assertEqual(got, ['abcdefghij', 'next'])


if __name__ == '__main__':
    unittest.main()
