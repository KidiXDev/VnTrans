from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import tempfile
import threading
import unittest

from vntrans import extract
from vntrans.cli import main
from vntrans.progress import Progress


class Terminal(io.StringIO):
    def __init__(self):
        super().__init__()
        self.animated = threading.Event()
        self.frames = 0

    def isatty(self):
        return True

    def write(self, value):
        result = super().write(value)
        if "Extracting" in value:
            self.frames += 1
            if self.frames >= 2:
                self.animated.set()
        return result


class ProgressTests(unittest.TestCase):
    def test_terminal_animates_during_one_file_and_cleans_up_on_error(self):
        stream = Terminal()
        with redirect_stderr(stream):
            display = Progress()
            with self.assertRaisesRegex(ValueError, "failed"):
                with display as progress:
                    progress("Extracting", 0, 1, "scenario.ks")
                    self.assertTrue(stream.animated.wait(2), "spinner did not animate")
                    raise ValueError("failed")
        self.assertFalse(display.thread.is_alive())
        self.assertIn("0/1", stream.getvalue())
        self.assertTrue(stream.getvalue().endswith("\r"))

    def test_redirected_cli_has_plain_progress_and_clean_stdout(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "a.ks"
            source.write_bytes(b"Hello\n")
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(["extract", str(source), "--output", str(Path(temp) / "json")])
        self.assertEqual(code, 0)
        self.assertEqual(stdout.getvalue(), "extract: 1 file(s), 1 unit(s)\n")
        self.assertIn("Extracting 1/1", stderr.getvalue())
        self.assertIn("Writing output 1/1", stderr.getvalue())
        self.assertNotIn("\r", stderr.getvalue())
        self.assertNotIn("\x1b", stderr.getvalue())

    def test_python_api_is_quiet_by_default(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "a.ks"
            source.write_bytes(b"Hello\n")
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                extract(source, output=Path(temp) / "json")
        self.assertEqual(stderr.getvalue(), "")
