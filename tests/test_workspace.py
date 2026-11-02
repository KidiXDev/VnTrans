import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fixtures import scenario

from vntrans import VnTransError, extract, workspace
from vntrans.cli import main


def digest(folder):
    return {
        p.relative_to(folder): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in folder.rglob("*")
        if p.is_file()
    }


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "game"
        (self.source / "nested").mkdir(parents=True)
        (self.source / "nested/test.ks.scn").write_bytes(scenario())
        (self.source / "test.ks").write_bytes(
            b"; comment\r\n*start|Opening\n[name name=Alice]Hello[l]world\n"
        )
        (self.source / "readme.txt").write_text("not a script")
        self.ws_root = self.root / "work"

    def create(self):
        workspace.create(
            self.source, self.ws_root, name="Test", target_language="Indonesian"
        )
        ws = workspace.Workspace(self.ws_root)
        self.addCleanup(ws.close)
        return ws

    def row(self, ws, file, kind):
        return next(r for r in ws.rows() if r.file == file and r.kind == kind)

    def japanese(self, script):
        game = self.root / "jp"
        game.mkdir()
        (game / "a.ks").write_text(script, encoding="utf-8-sig")
        workspace.create(game, self.ws_root, name="JP")
        ws = workspace.Workspace(self.ws_root)
        self.addCleanup(ws.close)
        return ws

    def test_untranslatable_picks_lines_without_source_letters(self):
        ws = self.japanese(
            "[name name=里久]「ふぁ……」\n[name name=？？？]……\nＯＫ！ 123\n"
        )
        rows = ws.rows()
        self.assertEqual(
            sorted(row.source for row in ws.untranslatable(rows)),
            sorted(["？？？", "……", "ＯＫ！ 123"]),
        )
        ws.save(next(row for row in rows if row.source == "……"), "...")
        self.assertNotIn("……", [row.source for row in ws.untranslatable(rows)])

    def test_linguagacha_message_json_round_trip(self):
        ws = self.japanese(
            "[name name=里久]「ふぁ」\n[name name=里久]はい[l]そう\n[name name=里久]ええ\nまた\n"
        )
        rows = ws.rows()
        exported = self.root / "lg"
        self.assertEqual(ws.export_messages(exported, rows), 1)
        self.assertEqual(
            json.loads((exported / "a.ks.json").read_text(encoding="utf-8")),
            [
                dict(name="里久", message="「ふぁ」"),
                dict(name="里久", message="はい⟦0⟧そう"),
                dict(name="里久", message="ええ"),
                dict(name="里久", message="また"),
            ],
        )

        output = self.root / "out"
        (output / "extra").mkdir(parents=True)
        (output / "b").mkdir()
        translated = [
            dict(name="Riku", message='"Fwah"'),
            dict(name="Riku", message="Yes so"),
            dict(name="Riku", message="ええ"),
            dict(name="Riku", message="Again"),
        ]
        (output / "extra/a.ks.json").write_text(
            json.dumps(translated), encoding="utf-8"
        )
        (output / "extra/missing.ks.json").write_text("[]", encoding="utf-8")
        (output / "b/a.ks.json").write_text("[]", encoding="utf-8")
        ws.save(next(row for row in rows if row.source == "また"), "Mine")
        pairs, skipped, ignored = ws.import_messages(output, rows)
        self.assertEqual(
            sorted((row.kind, text) for row, text in pairs),
            [("dialogue", '"Fwah"')] + [("speaker", "Riku")] * 3,
        )
        self.assertEqual((skipped, ignored), (1, ["b/a.ks", "extra/missing.ks"]))

    def test_ruby_base_letters_are_not_auto_excluded(self):
        ws = self.japanese("[ruby text=x char=2]東京[p]\n……\n")
        self.assertEqual([row.source for row in ws.untranslatable(ws.rows())], ["……"])

    def test_create_copies_extracts_and_indexes(self):
        before = digest(self.source)
        ws = self.create()
        self.assertEqual(before, digest(self.source))
        self.assertEqual(
            sorted(p.name for p in (self.ws_root / "source").rglob("*") if p.is_file()),
            ["test.ks", "test.ks.scn"],
        )
        self.assertEqual(
            ws.manifest["languages"], {"source": "Japanese", "target": "Indonesian"}
        )
        self.assertEqual(ws.formats, {"nested/test.ks.scn": "scn", "test.ks": "ks"})
        rows = ws.rows()
        docs = [
            json.loads(p.read_text(encoding="utf-8"))
            for p in (self.ws_root / "extracted").rglob("*.json")
        ]
        self.assertEqual(len(rows), sum(len(doc["units"]) for doc in docs))
        self.assertTrue(all(r.status == "new" and r.translation is None for r in rows))
        self.assertEqual(self.row(ws, "test.ks", "dialogue").speaker, "Alice")
        self.assertEqual(self.row(ws, "test.ks", "dialogue").scene, "*start")

    def test_rejects_nonempty_or_overlapping_workspace_and_cleans_up(self):
        self.ws_root.mkdir()
        (self.ws_root / "keep.txt").write_text("x")
        with self.assertRaisesRegex(VnTransError, "must be empty"):
            workspace.create(self.source, self.ws_root, name="Test")
        self.assertEqual([p.name for p in self.ws_root.iterdir()], ["keep.txt"])
        with self.assertRaisesRegex(VnTransError, "overlap"):
            workspace.create(self.source, self.source / "work", name="Test")
        broken = self.root / "broken"
        broken.mkdir()
        (broken / "bad.ks").write_bytes(b"[if exp=1]\n")
        with self.assertRaises(VnTransError):
            workspace.create(broken, self.root / "new", name="Test")
        self.assertFalse((self.root / "new").exists())
        with self.assertRaisesRegex(VnTransError, "not a VnTrans workspace"):
            workspace.Workspace(self.root)

    def test_save_is_gated_by_render_checks(self):
        ws = self.create()
        row = self.row(ws, "test.ks", "dialogue")
        self.assertEqual(row.source, "Hello⟦0⟧world")
        with self.assertRaisesRegex(VnTransError, "placeholders"):
            ws.save(row, "Halo dunia")
        self.assertIsNone(row.translation)
        ws.save(row, "Halo⟦0⟧dunia\r\nbaru")
        self.assertEqual(
            (row.translation, row.status), ("Halo⟦0⟧dunia\nbaru", "edited")
        )
        reopened = workspace.Workspace(self.ws_root)
        self.addCleanup(reopened.close)
        self.assertEqual(
            self.row(reopened, "test.ks", "dialogue").translation, "Halo⟦0⟧dunia\nbaru"
        )
        ws.save(row, None)
        self.assertEqual((row.translation, row.status), (None, "new"))

    def test_build_matches_cli_rebuild_and_skips_excluded(self):
        ws = self.create()
        dialogue = self.row(ws, "test.ks", "dialogue")
        title = self.row(ws, "test.ks", "title")
        ws.save(dialogue, "Halo⟦0⟧dunia")
        ws.save(title, "Pembukaan")
        ws.exclude(title, True)
        target, report = ws.build()
        self.assertEqual(report.files, 2)
        saved = json.loads((target / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(
            (saved["files"], saved["units"], saved["wrap_columns"]),
            (2, report.units, None),
        )
        exported = json.loads(
            (target / "json/test.ks.json").read_text(encoding="utf-8")
        )
        values = {u["id"]: u["translation"] for u in exported["units"]}
        self.assertEqual(values[dialogue.id], "Halo⟦0⟧dunia")
        self.assertIsNone(values[title.id])

        manual = self.root / "manual"
        extract(self.source, output=manual)
        doc = json.loads((manual / "test.ks.json").read_text(encoding="utf-8"))
        next(u for u in doc["units"] if u["id"] == dialogue.id)["translation"] = (
            "Halo⟦0⟧dunia"
        )
        (manual / "test.ks.json").write_text(
            json.dumps(doc, ensure_ascii=False), encoding="utf-8"
        )
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(
                main(
                    [
                        "rebuild",
                        str(self.source),
                        "--translations",
                        str(manual),
                        "--output",
                        str(self.root / "cli"),
                    ]
                ),
                0,
            )
        self.assertEqual(digest(self.root / "cli"), digest(target / "patch"))
        self.assertEqual(ws.builds(), [target])
        second, _ = ws.build()
        self.assertNotEqual(second, target)
        with patch("vntrans.pipeline.PARALLEL_BYTES", 0):
            third, _ = ws.build()
        self.assertEqual(digest(third), digest(second))

    def test_machine_commit_checks_every_row_before_writing(self):
        ws = self.create()
        title = self.row(ws, "test.ks", "title")
        dialogue = self.row(ws, "test.ks", "dialogue")
        with self.assertRaisesRegex(VnTransError, "placeholders"):
            ws.commit(
                [
                    (row, text, row.status, row.translation)
                    for row, text in [(title, "Opening"), (dialogue, "broken")]
                ]
            )
        self.assertIsNone(title.translation)
        self.assertEqual(ws.translations(), {})

    def test_snapshot_restore_and_bulk_exclude(self):
        ws = self.create()
        rows = [r for r in ws.rows() if r.file == "test.ks"]
        dialogue = next(r for r in rows if r.kind == "dialogue")
        ws.save(dialogue, "Halo⟦0⟧dunia")
        dialogue.error = None
        snapshot = ws.snapshot(rows)
        ws.save_many([(row, None) for row in rows])
        ws.exclude_many(rows, True)
        self.assertTrue(
            all(r.status == "excluded" and r.translation is None for r in rows)
        )
        ws.restore(snapshot)
        stored = {
            r.id: (r.translation, r.status) for r in ws.rows() if r.file == "test.ks"
        }
        self.assertEqual(stored[dialogue.id], ("Halo⟦0⟧dunia", "edited"))
        self.assertTrue(
            all(
                value == (None, "new")
                for key, value in stored.items()
                if key != dialogue.id
            )
        )

    def test_review_suggestions_are_replaced_atomically(self):
        ws = self.create()
        row = next(row for row in ws.rows() if row.kind == "dialogue")
        ws.suggest(row, "Previous", "Previous reason")
        with self.assertRaises(sqlite3.IntegrityError):
            ws.replace_suggestions(
                [row],
                [(row, "New", "New reason"), (row, "Duplicate", "Duplicate reason")],
            )
        self.assertEqual(
            ws.suggestions(), {(row.file, row.id): ("Previous", "Previous reason")}
        )

    def test_review_verdicts_remember_the_reviewed_translation(self):
        ws = self.create()
        first, second = [row for row in ws.rows() if "⟦" not in row.source][:2]
        ws.save_many([(first, "One"), (second, "Two")])
        ws.replace_suggestions([first, second], [(second, "Two!", "Grammar: mark")])
        self.assertEqual(ws.reviews(), {(first.file, first.id): ("One", "passed")})
        ws.dismiss([(second.file, second.id)], "rejected")
        self.assertEqual(ws.suggestions(), {})
        self.assertEqual(ws.reviews()[(second.file, second.id)], ("Two", "rejected"))
        ws.replace_suggestions([first], [(first, "Uno", "Mistranslation: wrong")])
        self.assertNotIn((first.file, first.id), ws.reviews())
        workspace.store_rows(
            self.ws_root,
            [replace(first, translation="Uno", status="edited")],
            dismiss="accepted",
        )
        self.assertEqual(ws.suggestions(), {})
        self.assertEqual(ws.reviews()[(first.file, first.id)], ("Uno", "accepted"))

    def test_cancelled_build_leaves_nothing_behind(self):
        ws = self.create()

        class Stop(Exception):
            pass

        def progress(phase, completed, total, name):
            if phase == "Rebuilding" and completed:
                raise Stop

        with self.assertRaises(Stop):
            ws.build(progress=progress)
        self.assertEqual(list((self.ws_root / "builds").iterdir()), [])
        self.assertEqual(ws.builds(), [])

    def test_create_reads_originals_once_and_indexes_without_reloading_json(self):
        with patch(
            "vntrans.pipeline.read_document",
            side_effect=AssertionError("unexpected JSON reload"),
        ):
            ws = self.create()
        self.assertTrue(ws.rows())

    def test_creation_and_build_progress_use_one_overall_counter(self):
        for index in range(32):
            (self.source / f"fast-{index}.ks").write_bytes(b"Hello\n")
        events = []
        workspace.create(
            self.source,
            self.ws_root,
            name="Test",
            progress=lambda *args: events.append(args),
        )
        self.assertEqual(
            {phase for phase, *_ in events}, {"Scanning scripts", "Extracting"}
        )
        extracting = [event for event in events if event[0] == "Extracting"]
        self.assertEqual({total for _, _, total, _ in extracting}, {36})
        self.assertEqual(extracting[-1], ("Extracting", 36, 36, ""))
        ratios = [
            completed / total if total else 0 for _, completed, total, _ in events
        ]
        self.assertEqual(ratios, sorted(ratios))
        ws = workspace.Workspace(self.ws_root)
        self.addCleanup(ws.close)
        events.clear()
        ws.build(progress=lambda *args: events.append(args))
        self.assertEqual(
            {phase for phase, *_ in events},
            {"Scanning translations", "Rebuilding", "Writing output"},
        )
        rebuilding = [event for event in events if event[0] == "Rebuilding"]
        self.assertEqual({total for _, _, total, _ in rebuilding}, {34})
        ratios = [
            completed / total if total else 0 for _, completed, total, _ in events
        ]
        self.assertEqual(ratios, sorted(ratios))
        self.assertEqual(events[-1], ("Writing output", 34, 34, ""))

    def test_cancelled_parsing_preserves_empty_workspace_and_closes_database(self):
        self.ws_root.mkdir()

        def cancel(phase, completed, total, filename):
            if phase == "Extracting" and total and completed == total and not filename:
                raise RuntimeError("cancelled")

        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            workspace.create(self.source, self.ws_root, name="Test", progress=cancel)
        self.assertEqual(list(self.ws_root.iterdir()), [])
        self.assertFalse(list(self.root.glob(".vntrans-*")))
        self.create()

    def test_database_initialization_failure_closes_before_staging_cleanup(self):
        with (
            patch("vntrans.workspace.SCHEMA", "invalid SQL"),
            self.assertRaises(sqlite3.DatabaseError),
        ):
            workspace.create(self.source, self.ws_root, name="Test")
        self.assertFalse(self.ws_root.exists())
        self.assertFalse(list(self.root.glob(".vntrans-*")))

    def test_create_does_not_replace_a_destination_created_during_processing(self):
        def collide(phase, completed, total, filename):
            if phase == "Extracting" and total and completed == total and not filename:
                self.ws_root.mkdir()

        with self.assertRaisesRegex(VnTransError, "destination appeared"):
            workspace.create(self.source, self.ws_root, name="Test", progress=collide)
        self.assertTrue(self.ws_root.is_dir())
        self.assertEqual(list(self.ws_root.iterdir()), [])

    def test_build_keeps_complete_directory_private_until_success(self):
        ws = self.create()

        def progress(phase, completed, total, filename):
            self.assertEqual(ws.builds(), [])
            if phase == "Writing output":
                raise RuntimeError("cancelled")

        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            ws.build(progress=progress)
        self.assertEqual(list((ws.root / "builds").iterdir()), [])

    def test_detached_edits_validate_the_entire_transaction_before_writing(self):
        ws = self.create()
        title, dialogue = (
            self.row(ws, "test.ks", "title"),
            self.row(ws, "test.ks", "dialogue"),
        )
        entries = [
            replace(title, translation="Opening", status="edited"),
            replace(dialogue, translation="missing controls", status="edited"),
        ]
        with self.assertRaises(VnTransError):
            workspace.store_rows(ws.root, entries)
        self.assertEqual(ws.translations(), {})
        entries[1].translation = "Hello⟦0⟧world"
        workspace.store_rows(ws.root, entries)
        self.assertEqual(len(ws.translations()), 2)
        self.assertIsNone(title.translation)


if __name__ == "__main__":
    unittest.main()
