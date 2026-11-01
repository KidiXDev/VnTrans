import asyncio
import json
from pathlib import Path
import re
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from vntrans import config
from vntrans.text import make_unit
from vntrans.translation import prompts
from vntrans.gui.geometry import normalize, place
from vntrans.workspace import Row
from vntrans.gui.texts import TextState
from vntrans.gui.jobs import Cancelled, Latest, Progress, build, run
from vntrans.gui import jobs
import time
from contextlib import nullcontext
from dataclasses import replace
from vntrans import workspace

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
        window = SimpleNamespace(rows=rows, ws=SimpleNamespace(formats={"a.ks": "ks"}),
                                 texts=TextState(rows, {"a.ks": "ks"}))
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


class TextStateTests(unittest.TestCase):
    def test_snapshots_and_incremental_counts(self):
        rows = [row("a.ks", "dialogue", "Hello"), row("b.ks", "choice", "Go", status="excluded")]
        texts = TextState(rows, {"a.ks": "ks", "b.ks": "ks"})
        snapshot = texts.snapshot()
        rows[0].translation, rows[0].status = "Halo", "edited"
        texts.changed(0)
        self.assertEqual(texts.totals, [1, 1, 0])
        self.assertEqual(texts.files["a.ks"], [1, 1, 0])
        self.assertIsNone(snapshot[0].translation)
        rows[0].translation, rows[0].status, rows[0].error = None, "new", "failed"
        texts.changed(0)
        self.assertEqual(texts.totals, [1, 0, 1])
        rows[0].status = "excluded"
        texts.changed(0)
        self.assertEqual(texts.totals, [0, 0, 0])


class JobTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipIf(filter_rows is None, "gui extra not installed")
    async def test_a_slower_workspace_open_cannot_replace_a_newer_one(self):
        started, release = threading.Event(), threading.Event()
        opened = {}
        def workspace_for(path):
            ws = SimpleNamespace(root=path, close=Mock())
            opened[path.name] = ws
            return ws
        def load_workspace(path, filters):
            if path.name == "old":
                started.set()
                release.wait(5)
            return path.name
        window = SimpleNamespace(run=None, open_serial=0, settings={}, overlay=lambda text: nullcontext(),
                                 show_workspace=Mock(), fail=Mock(), filters=lambda: {})
        with patch("vntrans.gui.app.workspace.Workspace", side_effect=workspace_for), \
                patch("vntrans.gui.app.load", side_effect=load_workspace):
            old = asyncio.create_task(Window.open(window, "old"))
            await asyncio.to_thread(started.wait, 5)
            await Window.open(window, "new")
            release.set()
            await old
        window.show_workspace.assert_called_once_with(opened["new"], "new")
        opened["old"].close.assert_called_once()
        window.fail.assert_not_called()

    async def test_worker_handoff_does_not_call_the_native_threadsafe_bridge(self):
        loop = asyncio.get_running_loop()
        def work():
            time.sleep(.02)
            return 42
        def fail():
            raise ValueError("worker error")
        with patch.object(loop, "call_soon_threadsafe", side_effect=AssertionError("off-thread UI callback")):
            result = await jobs.thread(work)
        self.assertEqual(result, 42)
        with self.assertRaisesRegex(ValueError, "worker error"):
            await jobs.thread(fail)

    async def test_worker_pool_preserves_parallel_provider_capacity(self):
        barrier = threading.Barrier(4)
        results = await asyncio.gather(*(jobs.thread(barrier.wait, 2) for _ in range(4)))
        self.assertEqual(set(results), {0, 1, 2, 3})
    async def test_spawned_build_and_cooperative_cancellation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            source.mkdir()
            (source / "a.ks").write_bytes(b"Hello[l]world\n")
            workspace.create(source, root / "work", name="Test")
            updates, cancel = [], threading.Event()
            target, report = await run(build, root / "work", cancel=cancel, progress=lambda *args: updates.append(args))
            self.assertTrue((target / "patch/a.ks").is_file())
            self.assertEqual(report.files, 1)
            self.assertEqual(updates[-1], ("Writing output", 1, 1, ""))
            cancel.set()
            with self.assertRaises(Cancelled):
                await run(workspace.create, source, root / "cancelled", name="Test", cancel=cancel,
                          progress=lambda *args: None)
            self.assertFalse((root / "cancelled").exists())
            self.assertFalse(list(root.glob(".vntrans-*")))

    async def test_only_latest_pending_request_runs_and_applies(self):
        started, release = threading.Event(), threading.Event()
        applied, called = [], []
        errors = []
        jobs = Latest(errors.append)
        def old():
            called.append("old")
            started.set()
            release.wait(5)
            return "old"
        jobs.submit(old, applied.append)
        await asyncio.to_thread(started.wait, 5)
        jobs.submit(lambda: called.append("discarded"), applied.append)
        jobs.submit(lambda: "latest", applied.append)
        release.set()
        await jobs.task
        self.assertEqual(called, ["old"])
        self.assertEqual(applied, ["latest"])
        self.assertEqual(errors, [])
        jobs.submit(lambda: "closed workspace", applied.append)
        jobs.invalidate()
        await jobs.task
        self.assertEqual(applied, ["latest"])

    async def test_progress_coalesces_and_delivers_final_update(self):
        shown, cancel = [], threading.Event()
        progress = Progress(lambda *value: shown.append(value), cancel)
        await asyncio.to_thread(lambda: [progress("Working", i, 10000, "a.ks") for i in range(10001)])
        progress.close()
        await asyncio.sleep(0)
        self.assertLessEqual(len(shown), 3)
        self.assertEqual(shown[-1], ("Working", 10000, 10000, "a.ks"))
        cancel.set()
        with self.assertRaises(Cancelled):
            progress("Working", 1, 2, "a.ks")

    @unittest.skipIf(filter_rows is None, "gui extra not installed")
    async def test_filter_rejects_a_result_after_an_edit(self):
        rows = [row("a.ks", "dialogue", "Hello")]
        texts = TextState(rows, {"a.ks": "ks"})
        started, release = threading.Event(), threading.Event()
        window = SimpleNamespace(ws=object(), texts=texts, tags={}, filter_reset=False,
                                 filters=lambda: dict(query="Halo"), filtered=Mock(), show_counts=Mock())
        window.queue_filter = lambda reset=False: Window.queue_filter(window, reset)
        window.filter_job = Latest(lambda exc: self.fail(str(exc)))
        def delayed(snapshot, **options):
            started.set()
            release.wait(5)
            return filter_rows(snapshot, **options)
        with patch("vntrans.gui.app.filter_snapshot", side_effect=delayed):
            window.queue_filter(True)
            await asyncio.to_thread(started.wait, 5)
            rows[0].translation = "Halo"
            texts.changed(0)
            release.set()
            await window.filter_job.task
        self.assertEqual(window.filtered.call_count, 1)
        self.assertEqual(window.filtered.call_args.args[0], [0])

    @unittest.skipIf(filter_rows is None, "gui extra not installed")
    async def test_bulk_edit_waits_for_commit_and_preserves_undo(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            source.mkdir()
            (source / "a.ks").write_bytes(b"Hello[l]world\n")
            workspace.create(source, root / "work", name="Test")
            ws = workspace.Workspace(root / "work")
            try:
                rows = ws.rows()
                texts = TextState(rows, ws.formats)
                ready = asyncio.Event()
                ready.set()
                window = SimpleNamespace(ws=ws, texts=texts, rows=rows, index_of=texts.index_of, editing=False,
                                         positions={0: 0},
                                         run=SimpleNamespace(writes_ready=ready), ui=SimpleNamespace(current=0),
                                         overlay=lambda text: nullcontext(), model=Mock(indices=[0]),
                                         update_stats=Mock(), refresh_counts=Mock(), recheck=Mock(),
                                         select=Mock(), show_selection=Mock(), forget_undo=Mock(), fail=Mock())
                def prepare():
                    self.assertFalse(ready.is_set())
                    self.assertIsNone(rows[0].translation)
                    return [replace(rows[0], translation="Halo⟦0⟧dunia", status="edited")]
                await Window.edit(window, "edit", prepare)
                self.assertEqual(rows[0].translation, "Halo⟦0⟧dunia")
                self.assertEqual(ws.translations()[(rows[0].file, rows[0].id)], rows[0].translation)
                self.assertIsNone(window.undo_entry[1][0][1])
                self.assertTrue(ready.is_set())
                window.fail.assert_not_called()
                await Window.edit(window, "invalid", lambda: [replace(rows[0], translation="missing control")])
                self.assertEqual(rows[0].translation, "Halo⟦0⟧dunia")
                self.assertTrue(ready.is_set())
                self.assertEqual(window.fail.call_count, 1)
            finally:
                ws.close()


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
