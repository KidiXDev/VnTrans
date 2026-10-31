from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from fixtures import scenario
from vntrans import VnTransError, extract, workspace
from vntrans.cli import main


def digest(folder):
    return {p.relative_to(folder): hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.rglob("*") if p.is_file()}


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "game"
        (self.source / "nested").mkdir(parents=True)
        (self.source / "nested/test.ks.scn").write_bytes(scenario())
        (self.source / "test.ks").write_bytes(b"; comment\r\n*start|Opening\n[name name=Alice]Hello[l]world\n")
        (self.source / "readme.txt").write_text("not a script")
        self.ws_root = self.root / "work"

    def create(self):
        workspace.create(self.source, self.ws_root, name="Test", target_language="Indonesian")
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
        ws = self.japanese("[name name=里久]「ふぁ……」\n[name name=？？？]……\nＯＫ！ 123\n")
        rows = ws.rows()
        self.assertEqual(sorted(row.source for row in ws.untranslatable(rows)), sorted(["？？？", "……", "ＯＫ！ 123"]))
        ws.save(next(row for row in rows if row.source == "……"), "...")
        self.assertNotIn("……", [row.source for row in ws.untranslatable(rows)])

    def test_create_copies_extracts_and_indexes(self):
        before = digest(self.source)
        ws = self.create()
        self.assertEqual(before, digest(self.source))
        self.assertEqual(sorted(p.name for p in (self.ws_root / "source").rglob("*") if p.is_file()),
                         ["test.ks", "test.ks.scn"])
        self.assertEqual(ws.manifest["languages"], {"source": "Japanese", "target": "Indonesian"})
        self.assertEqual(ws.formats, {"nested/test.ks.scn": "scn", "test.ks": "ks"})
        rows = ws.rows()
        docs = [json.loads(p.read_text(encoding="utf-8")) for p in (self.ws_root / "extracted").rglob("*.json")]
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
        self.assertEqual((row.translation, row.status), ("Halo⟦0⟧dunia\nbaru", "edited"))
        reopened = workspace.Workspace(self.ws_root)
        self.addCleanup(reopened.close)
        self.assertEqual(self.row(reopened, "test.ks", "dialogue").translation, "Halo⟦0⟧dunia\nbaru")
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
        self.assertEqual((saved["files"], saved["units"], saved["wrap_columns"]), (2, report.units, None))
        exported = json.loads((target / "json/test.ks.json").read_text(encoding="utf-8"))
        values = {u["id"]: u["translation"] for u in exported["units"]}
        self.assertEqual(values[dialogue.id], "Halo⟦0⟧dunia")
        self.assertIsNone(values[title.id])

        manual = self.root / "manual"
        extract(self.source, output=manual)
        doc = json.loads((manual / "test.ks.json").read_text(encoding="utf-8"))
        next(u for u in doc["units"] if u["id"] == dialogue.id)["translation"] = "Halo⟦0⟧dunia"
        (manual / "test.ks.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["rebuild", str(self.source), "--translations", str(manual),
                                   "--output", str(self.root / "cli")]), 0)
        self.assertEqual(digest(self.root / "cli"), digest(target / "patch"))
        self.assertEqual(ws.builds(), [target])
        second, _ = ws.build()
        self.assertNotEqual(second, target)


    def test_snapshot_restore_and_bulk_exclude(self):
        ws = self.create()
        rows = [r for r in ws.rows() if r.file == "test.ks"]
        dialogue = next(r for r in rows if r.kind == "dialogue")
        ws.save(dialogue, "Halo⟦0⟧dunia")
        dialogue.error = None
        snapshot = ws.snapshot(rows)
        ws.save_many([(row, None) for row in rows])
        ws.exclude_many(rows, True)
        self.assertTrue(all(r.status == "excluded" and r.translation is None for r in rows))
        ws.restore(snapshot)
        stored = {r.id: (r.translation, r.status) for r in ws.rows() if r.file == "test.ks"}
        self.assertEqual(stored[dialogue.id], ("Halo⟦0⟧dunia", "edited"))
        self.assertTrue(all(value == (None, "new") for key, value in stored.items() if key != dialogue.id))

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


if __name__ == "__main__":
    unittest.main()
