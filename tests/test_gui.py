import asyncio
import json
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from vntrans import config
from vntrans.text import make_unit
from vntrans.translation import prompts
from vntrans.gui.geometry import normalize, place
from vntrans.workspace import Row

try:
    from vntrans.gui.app import Window, bars, compact, duration, filter_rows
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
        self.assertEqual(counts, dict(all=4, untranslated=2, new=1, machine=0, edited=1, failed=1, excluded=1,
                                      issues=0, review=0))
        self.assertEqual(filter_rows(self.rows, status="untranslated")[0], [1, 2])

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
        self.assertEqual(filter_rows(self.rows, kind="lines")[0], [0, 2, 3])
        self.assertEqual(filter_rows(self.rows, kind="")[0], [0, 1, 2, 3])


@unittest.skipIf(filter_rows is None, "gui extra not installed")
class FormatTests(unittest.TestCase):
    def test_bars_and_numbers(self):
        self.assertEqual(bars([], count=3), [0.0, 0.0, 0.0])
        self.assertEqual(bars([0, 10, 5], count=4), [0.0, 0.0, 1.0, 0.5])
        self.assertEqual(bars(range(10), count=2), [8 / 9, 1.0])
        self.assertEqual((compact(999), compact(12_345), compact(2_500_000)), ("999", "12.3k", "2.5M"))
        self.assertEqual((duration(75), duration(3725)), ("1:15", "1:02:05"))


@unittest.skipIf(filter_rows is None, "gui extra not installed")
class EditingTests(unittest.TestCase):
    def test_twins_require_matching_controls_and_literal_handling(self):
        rows = []
        for index, (text, context) in enumerate([
                ('Go [ruby text=x char=5]Tokyo', {}), ('Go [ruby text=y char=5]Osaka', {}),
                ('Go [l]', {}), ('Go [ruby text=x char=5]Tokyo', {"line": 4}),
                ('Hello', {}), ('Hello', {"literal": True})]):
            unit = make_unit(str(index), "dialogue", text, "ks", **context)
            rows.append(Row("a.ks", unit.id, unit.kind, unit.source, "", "", json.dumps(unit.context),
                            None, "new", None))
        window = SimpleNamespace(rows=rows, ws=SimpleNamespace(formats={"a.ks": "ks"}))
        self.assertEqual(Window.twins(window, rows[0]), [rows[3]])
        self.assertEqual(Window.twins(window, rows[4]), [])

    def test_prompt_preview_applies_source_replacement_rules(self):
        ui = SimpleNamespace(current=0)
        ws = SimpleNamespace(manifest={"languages": dict(source="English", target="Indonesian"),
                                       "replacements": "pre Hello => Goodbye"}, formats={"a.ks": "ks"})
        window = SimpleNamespace(ui=ui, ws=ws, rows=[row("a.ks", "dialogue", "Hello")],
                                 model=SimpleNamespace(indices=[0]), batch_settings=lambda: prompts.BATCH,
                                 sections=prompts.chain({}), terms=[])
        Window.update_preview(window)
        self.assertIn('"text": "Goodbye"', ui.preview_user)
        self.assertNotIn('"text": "Hello"', ui.preview_user)
        ws.manifest["replacements"] = "pre /(/ => invalid"
        Window.update_preview(window)
        self.assertEqual(ui.preview_user, "")
        self.assertIn("Replacement rules:", ui.preview_stats)


@unittest.skipIf(filter_rows is None, "gui extra not installed")
class RecentWorkspaceTests(unittest.TestCase):
    def window(self, recent):
        window = SimpleNamespace(settings={"recent": recent, "other": "keep"},
                                 ui=SimpleNamespace(missing_workspace=""), open=AsyncMock(),
                                 say=Mock(), fail=Mock())
        window.show_recent = Mock(side_effect=lambda: Window.show_recent(window))
        return window

    def open_recent(self, window, path):
        async def click():
            pending = asyncio.all_tasks()
            Window.open_workspace(window, path)
            await asyncio.gather(*(asyncio.all_tasks() - pending))
        asyncio.run(click())

    def test_removal_persists_history_and_keeps_workspace_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            workspace = root / "workspace"
            workspace.mkdir()
            marker = workspace / "keep.txt"
            marker.write_text("Keep this workspace", encoding="utf-8")
            recent = [dict(name="Remove me", path=str(workspace)), dict(name="Keep me", path="other")]
            window = self.window(recent)
            with patch("vntrans.config.config_dir", return_value=root / "config"):
                Window.remove_recent(window, str(workspace))
                self.assertEqual(config.load(), {"recent": recent[1:], "other": "keep"})
            self.assertEqual(window.settings["recent"], recent[1:])
            self.assertEqual(list(window.ui.recent), recent[1:])
            self.assertEqual(marker.read_text(encoding="utf-8"), "Keep this workspace")
            window.open.assert_not_awaited()
            window.fail.assert_not_called()

    def test_missing_workspace_offers_removal_without_changing_history(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "deleted")
            recent = [dict(name="Missing", path=path)]
            window = self.window(recent)
            with patch("vntrans.gui.app.config.save") as save:
                self.open_recent(window, path)
                self.assertEqual(window.ui.missing_workspace, path)
                self.assertEqual(window.settings["recent"], recent)
                save.assert_not_called()
                window.open.assert_not_awaited()
                Window.remove_recent(window, path)
                save.assert_called_once_with({"recent": [], "other": "keep"})
            self.assertEqual(window.settings["recent"], [])
            self.assertEqual(window.ui.missing_workspace, "")
            self.assertEqual(list(window.ui.recent), [])

    def test_existing_workspace_opens_normally(self):
        with tempfile.TemporaryDirectory() as folder:
            window = self.window([dict(name="Existing", path=folder)])
            self.open_recent(window, folder)
            window.open.assert_awaited_once_with(folder)
            self.assertEqual(window.ui.missing_workspace, "")
            window.fail.assert_not_called()

    def test_access_error_does_not_claim_workspace_was_deleted(self):
        window = self.window([dict(name="Private", path="private")])
        error = PermissionError("Access denied")
        with patch("vntrans.gui.app.Path.exists", side_effect=error):
            self.open_recent(window, "private")
        window.fail.assert_called_once_with(error)
        window.open.assert_not_awaited()
        self.assertEqual(window.ui.missing_workspace, "")
        self.assertEqual(len(window.settings["recent"]), 1)

    def test_save_failure_keeps_recent_entry_and_missing_dialog(self):
        window = self.window([dict(name="Missing", path="missing")])
        window.ui.missing_workspace = "missing"
        settings = window.settings
        error = OSError("Could not save settings")
        with patch("vntrans.gui.app.config.save", side_effect=error):
            Window.remove_recent(window, "missing")
        self.assertIs(window.settings, settings)
        self.assertEqual(window.ui.missing_workspace, "missing")
        window.fail.assert_called_once_with(error)
        window.show_recent.assert_not_called()
        window.say.assert_not_called()


class ConfigTests(unittest.TestCase):
    def test_repeated_invalid_configs_keep_all_recovery_copies(self):
        with tempfile.TemporaryDirectory() as folder, patch("vntrans.config.config_dir", return_value=Path(folder)):
            directory = Path(folder)
            original = "{first invalid config}"
            (directory / "config.json.bad").write_text(original, encoding="utf-8")
            for number in range(1, 3):
                text = f"{{invalid config {number}}}"
                (directory / "config.json").write_text(text, encoding="utf-8")
                self.assertEqual(config.load(), {})
                self.assertEqual((directory / f"config.json.bad.{number}").read_text(encoding="utf-8"), text)
            self.assertEqual((directory / "config.json.bad").read_text(encoding="utf-8"), original)


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
