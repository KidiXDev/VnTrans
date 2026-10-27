import re
import unittest

from vntrans.workspace import Row

try:
    from vntrans.gui.app import bars, compact, duration, filter_rows
except ImportError:
    filter_rows = None


def row(file, kind, source, translation=None, status="new", error=None, speaker=""):
    return Row(file, f"{file}/{source}", kind, source, speaker, "", "{}", translation, status, error)


@unittest.skipIf(filter_rows is None, "gui extra not installed")
class FilterTests(unittest.TestCase):
    rows = [
        row("a.ks", "dialogue", "Hello there", "Halo", "edited"),
        row("a.ks", "speaker", "Alice", speaker="Alice"),
        row("b.ks", "dialogue", "hello again", error="bad output"),
        row("b.ks", "choice", "Go left", "Kiri", "excluded"),
    ]

    def test_counts_ignore_state_filter(self):
        indices, counts = filter_rows(self.rows, status="new")
        self.assertEqual(indices, [1])
        self.assertEqual(counts, dict(all=4, new=1, machine=0, edited=1, failed=1, excluded=1, issues=0, review=0))

    def test_issue_and_review_tags(self):
        tags = {0: {"residue", "review"}, 3: {"lines"}}
        indices, counts = filter_rows(self.rows, status="issues", tags=tags)
        self.assertEqual((indices, counts["issues"], counts["review"]), ([0, 3], 2, 1))
        self.assertEqual(filter_rows(self.rows, status="issues", tags=tags, issue="lines")[0], [3])
        self.assertEqual(filter_rows(self.rows, status="review", tags=tags)[0], [0])

    def test_search_scope_case_and_regex(self):
        self.assertEqual(filter_rows(self.rows, query="hello")[0], [0, 2])
        self.assertEqual(filter_rows(self.rows, query="hello", case=True)[0], [2])
        self.assertEqual(filter_rows(self.rows, query="^h.*o$", regex=True, scope="translation")[0], [0])
        self.assertEqual(filter_rows(self.rows, query="a.", scope="source")[0], [])
        self.assertEqual(filter_rows(self.rows, query="alice", scope="speaker")[0], [1])
        with self.assertRaises(re.error):
            filter_rows(self.rows, query="(", regex=True)

    def test_file_and_kind(self):
        self.assertEqual(filter_rows(self.rows, file="b.ks")[0], [2, 3])
        self.assertEqual(filter_rows(self.rows, kind="dialogue", status="failed")[0], [2])


@unittest.skipIf(filter_rows is None, "gui extra not installed")
class FormatTests(unittest.TestCase):
    def test_bars_and_numbers(self):
        self.assertEqual(bars([], count=3), [0.0, 0.0, 0.0])
        self.assertEqual(bars([0, 10, 5], count=4), [0.0, 0.0, 1.0, 0.5])
        self.assertEqual(bars(range(10), count=2), [8 / 9, 1.0])
        self.assertEqual((compact(999), compact(12_345), compact(2_500_000)), ("999", "12.3k", "2.5M"))
        self.assertEqual((duration(75), duration(3725)), ("1:15", "1:02:05"))


if __name__ == "__main__":
    unittest.main()
