from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fixtures import scenario
from vntrans import VnTransError, extract, rebuild, validate
from vntrans.cli import main
from vntrans.engines.kirikiri.scn import ScnScript


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "nested").mkdir()
        self.original = scenario()
        (self.source / "nested/test.ks.scn").write_bytes(self.original)
        (self.source / "test.ks").write_bytes(b"; comment\r\nHello\n")
        self.json_dir = self.root / "json"
        self.output = self.root / "patch"

    def export(self):
        return extract(self.source, output=self.json_dir)

    def edit(self, filename, fn):
        path = self.json_dir / filename
        doc = json.loads(path.read_text())
        fn(doc)
        path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    def test_noop_batch_and_metadata(self):
        (self.source / "image.psb").write_bytes(b"PSB\0image asset")
        report = self.export()
        self.assertEqual(report.files, 2)
        self.assertTrue((self.json_dir / "nested/test.ks.scn.json").exists())
        before = {p: p.read_bytes() for p in self.json_dir.rglob("*.json")}
        self.assertEqual(validate(self.source, translations=self.json_dir).files, 2)
        self.assertFalse(self.output.exists())
        rebuild(self.source, translations=self.json_dir, output=self.output)
        self.assertEqual((self.output / "nested/test.ks.scn").read_bytes(), self.original)
        self.assertEqual((self.output / "test.ks").read_bytes(), b"; comment\r\nHello\n")
        self.assertEqual(before, {p: p.read_bytes() for p in self.json_dir.rglob("*.json")})

    def test_reordered_units_and_cli_api_parity(self):
        self.export()
        def edit(doc):
            doc["units"].reverse()
            for unit in doc["units"]:
                if unit["kind"] == "dialogue":
                    unit["translation"] = "Translated line"
        self.edit("nested/test.ks.scn.json", edit)
        rebuild(self.source, translations=self.json_dir, output=self.output)
        cli_output = self.root / "cli-patch"
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["rebuild", str(self.source), "--translations", str(self.json_dir),
                                   "--output", str(cli_output)]), 0)
        self.assertEqual((cli_output / "nested/test.ks.scn").read_bytes(),
                         (self.output / "nested/test.ks.scn").read_bytes())
        script = ScnScript((cli_output / "nested/test.ks.scn").read_bytes(), 0)
        self.assertTrue(all(u.source == "Translated line" for u in script.units if u.kind == "dialogue"))

    def test_output_encoding_override_applies_only_to_text_scripts(self):
        self.export()
        rebuild(self.source, translations=self.json_dir, output=self.output, output_encoding="utf-8-sig")
        self.assertTrue((self.output / "test.ks").read_bytes().startswith(b"\xef\xbb\xbf"))
        self.assertEqual((self.output / "nested/test.ks.scn").read_bytes(), self.original)

    def test_wrap_warnings_are_reported(self):
        self.export()
        self.edit("test.ks.json", lambda doc: doc["units"][0].update(translation="unbreakable"))
        report = validate(self.source, translations=self.json_dir, wrap_columns=3)
        self.assertTrue(any("test.ks: unbreakable word" in w for w in report.warnings))

    def test_invalid_edits_fail_before_output(self):
        self.export()
        path = self.json_dir / "test.ks.json"
        original = path.read_text()
        edits = {
            "missing": lambda d: d["units"].clear(),
            "duplicate": lambda d: d["units"].append(d["units"][0].copy()),
            "unknown": lambda d: d["units"][0].update(id="other"),
            "source": lambda d: d["units"][0].update(source="other"),
            "translation type": lambda d: d["units"][0].update(translation=1),
            "version": lambda d: d.update(schema_version=True),
            "hash": lambda d: d.update(sha256="0" * 64),
            "path": lambda d: d.update(source="../../escape"),
            "context": lambda d: d["units"][0]["context"].update(line=True),
            "unicode": lambda d: d["units"][0].update(translation="\ud800"),
        }
        for label, fn in edits.items():
            with self.subTest(label=label):
                doc = json.loads(original)
                fn(doc)
                path.write_text(json.dumps(doc))
                with self.assertRaises(VnTransError):
                    rebuild(self.source, translations=self.json_dir, output=self.output)
                self.assertFalse(self.output.exists())
        path.write_text(original)
        self.assertEqual((self.source / "nested/test.ks.scn").read_bytes(), self.original)

    def test_bad_json_extra_files_and_stale_source(self):
        self.export()
        path = self.json_dir / "test.ks.json"
        original = path.read_bytes()
        for bad in (b"{", b'{"engine":"kirikiri","engine":"other"}', b"[]", b'{"units": NaN}'):
            path.write_bytes(bad)
            with self.subTest(bad=bad), self.assertRaises(VnTransError):
                validate(self.source, translations=self.json_dir)
        path.write_bytes(original)
        extra = self.json_dir / "extra.json"
        extra.write_bytes(original)
        with self.assertRaises(VnTransError):
            validate(self.source, translations=self.json_dir)
        extra.unlink()
        (self.source / "test.ks").write_bytes(b"Changed\n")
        with self.assertRaises(VnTransError):
            validate(self.source, translations=self.json_dir)

    def test_existing_destination_and_overlaps(self):
        self.export()
        with self.assertRaises(VnTransError):
            self.export()
        for output in (self.source, self.source / "patch", self.json_dir / "patch", self.root):
            with self.subTest(output=output), self.assertRaises(VnTransError):
                rebuild(self.source, translations=self.json_dir, output=output)
        self.output.mkdir()
        (self.output / "marker").write_text("keep")
        with self.assertRaises(VnTransError):
            rebuild(self.source, translations=self.json_dir, output=self.output)
        self.assertEqual((self.output / "marker").read_text(), "keep")

    def test_existing_destination_fails_before_processing(self):
        self.json_dir.mkdir()
        with patch("vntrans.pipeline.source_files") as sources:
            with self.assertRaisesRegex(VnTransError, "destination already exists"):
                self.export()
            sources.assert_not_called()
        self.output.mkdir()
        with patch("vntrans.pipeline.source_files") as sources, patch("vntrans.pipeline.read_document") as read:
            with self.assertRaisesRegex(VnTransError, "destination already exists"):
                rebuild(self.source, translations=self.json_dir, output=self.output)
            sources.assert_not_called()
            read.assert_not_called()
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = main(["extract", str(self.source), "--output", str(self.json_dir)])
        self.assertEqual(code, 1)
        self.assertIn("destination already exists", stderr.getvalue())
        self.assertNotIn("Scanning", stderr.getvalue())

    def test_progress_callback_covers_each_operation(self):
        events = []
        callback = lambda *args: events.append(args)
        extract(self.source, output=self.json_dir, progress=callback)
        self.assertIn(("Extracting", 2, 2, "test.ks"), events)
        self.assertEqual(events[-1], ("Writing output", 2, 2, ""))
        events.clear()
        validate(self.source, translations=self.json_dir, progress=callback)
        self.assertIn(("Loading JSON", 2, 2, ""), events)
        self.assertEqual(events[-1], ("Validating", 2, 2, "test.ks"))
        events.clear()
        rebuild(self.source, translations=self.json_dir, output=self.output, progress=callback)
        self.assertIn(("Rebuilding", 2, 2, "test.ks"), events)
        self.assertEqual(events[-1], ("Writing output", 2, 2, ""))

    def test_symlinks_and_traversal(self):
        link = self.root / "link"
        link.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(VnTransError):
            extract(link, output=self.json_dir)
        with self.assertRaises(VnTransError):
            extract(self.source, output=self.root / "new/../escape")
        (self.source / "linked.ks").symlink_to(self.source / "test.ks")
        with self.assertRaises(VnTransError):
            self.export()
        self.assertFalse(self.json_dir.exists())

    def test_failed_batch_publishes_nothing(self):
        (self.source / "bad.scn").write_bytes(b"mdf\0bad")
        with self.assertRaises(VnTransError):
            self.export()
        self.assertFalse(self.json_dir.exists())
        self.assertFalse(list(self.root.glob(".vntrans-*")))

    def test_single_file_and_invalid_options(self):
        report = extract(self.source / "test.ks", output=self.json_dir)
        self.assertEqual(report.files, 1)
        with self.assertRaises(VnTransError):
            validate(self.source / "test.ks", translations=self.json_dir, wrap_columns=0)
        for engine in ("missing",):
            with self.assertRaises(VnTransError):
                extract(self.source, output=self.output, engine=engine)
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(main(["extract", str(self.root / "missing"), "--output", str(self.output)]), 1)
        self.assertIn("does not exist", err.getvalue())
