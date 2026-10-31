import re
import unittest

from vntrans.gui.geometry import normalize, place
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


class GeometryTests(unittest.TestCase):
    def test_normalize_keeps_only_valid_fields(self):
        self.assertEqual(normalize(None), dict(maximized=False))
        self.assertEqual(normalize("garbage"), dict(maximized=False))
        self.assertEqual(normalize(dict(width=1500, height=900.5, maximized=True, position=[-1920, 40])),
                         dict(maximized=True, width=1500.0, height=900.5, position=[-1920, 40]))
        malformed = dict(width=True, height=float("nan"), maximized="yes", position=[1.5, 2])
        self.assertEqual(normalize(malformed), dict(maximized=False))
        self.assertEqual(normalize(dict(width=0, height=10**9, position=[1, 2, 3])), dict(maximized=False))
        self.assertEqual(normalize(dict(width=float("inf"), position=[10**7, 0])), dict(maximized=False))

    def test_place_keeps_reachable_windows_and_rescues_lost_ones(self):
        primary, left = (0, 0, 1920, 1040), (-1920, 0, 0, 1040)
        on_left = (-1800, 50, -400, 850)
        self.assertEqual(place((100, 100, 1500, 900), [primary, left]), ((100, 100, 1500, 900), True))
        self.assertEqual(place(on_left, [primary, left]), (on_left, True))
        self.assertEqual(place(on_left, [primary]), ((0, 50, 1400, 850), True))
        self.assertEqual(place((100, -500, 1500, 300), [primary]), ((100, 0, 1500, 800), True))
        self.assertEqual(place((1880, 100, 3280, 900), [primary]), ((520, 100, 1920, 900), True))
        self.assertEqual(place((50, 50, 2650, 1490), [primary]), ((0, 0, 2600, 1440), False))


if __name__ == "__main__":
    unittest.main()
