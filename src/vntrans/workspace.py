"""Project workspace: copied sources, immutable extraction, and a SQLite translation store."""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3

from . import pipeline
from .engines import get_engine
from .model import Unit, VnTransError
from .text import render
from .translation.glossary import Term
from .translation.quality import translatable

MANIFEST = "manifest.json"
DATABASE = "project.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS files(file TEXT PRIMARY KEY, format TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS units(
    file TEXT NOT NULL, id TEXT NOT NULL, ord INTEGER NOT NULL, kind TEXT NOT NULL,
    source TEXT NOT NULL, speaker TEXT NOT NULL, scene TEXT NOT NULL, context TEXT NOT NULL,
    translation TEXT, status TEXT NOT NULL DEFAULT 'new', error TEXT,
    PRIMARY KEY(file, id));
CREATE TABLE IF NOT EXISTS glossary(
    id INTEGER PRIMARY KEY, source TEXT NOT NULL, target TEXT NOT NULL, kind TEXT NOT NULL,
    notes TEXT NOT NULL, case_sensitive INTEGER NOT NULL, approved INTEGER NOT NULL, evidence TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS attempts(
    id INTEGER PRIMARY KEY, at TEXT NOT NULL, profile TEXT NOT NULL, model TEXT NOT NULL, prompt TEXT NOT NULL,
    units INTEGER NOT NULL, outcome TEXT NOT NULL, error TEXT, tokens_in INTEGER, tokens_out INTEGER);
CREATE TABLE IF NOT EXISTS suggestions(
    file TEXT NOT NULL, id TEXT NOT NULL, text TEXT NOT NULL, reason TEXT NOT NULL, PRIMARY KEY(file, id));
"""
ROWS = ("SELECT file, id, kind, source, speaker, scene, context, translation, status, error "
        "FROM units ORDER BY file, ord")


@dataclass(slots=True)
class Row:
    file: str
    id: str
    kind: str
    source: str
    speaker: str
    scene: str
    context: str
    translation: str | None
    status: str
    error: str | None

    def unit(self) -> Unit:
        return Unit(self.id, self.kind, self.source, json.loads(self.context))


def connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    return db


def load_rows(root) -> list[Row]:
    """A workspace's rows read on a private connection, so a worker thread can load them while the
    workspace's own connection stays on the thread that opened it."""
    db = connect(Path(root) / DATABASE)
    try:
        return [Row(*values) for values in db.execute(ROWS)]
    finally:
        db.close()


def write_json(path: Path, value):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def speakers(units):
    """Display speaker per unit: an explicit speaker unit in the same record wins."""
    names = {}
    for unit in units:
        record = unit["id"].rsplit("/", 1)[0]
        if unit["kind"] == "speaker":
            names[record] = unit["source"]
        speaking = unit["kind"] in ("dialogue", "speaker")
        yield (names.get(record) or unit["context"].get("speaker") or "") if speaking else ""


def scene(context) -> str:
    value = context.get("scene", context.get("label"))
    return "" if value is None else str(value)


def create(original, root, *, name, engine="kirikiri", encoding=None, language_index=0,
           source_language="Japanese", target_language="English",
           progress=pipeline._no_progress) -> Path:
    original, root = pipeline.safe_path(original), pipeline.safe_path(root)
    pipeline.disjoint(original, root)
    if root.is_file() or (root.is_dir() and any(root.iterdir())):
        raise VnTransError(f"workspace folder must be empty: {root}")
    created_root = not root.exists()
    root.mkdir(parents=True, exist_ok=True)
    try:
        pipeline.publish(root / "source", pipeline.source_files(original, get_engine(engine), progress), progress)
        pipeline.extract(root / "source", output=root / "extracted", engine=engine, encoding=encoding,
                         language_index=language_index, progress=progress)
        db = connect(root / DATABASE)
        with db:
            docs = sorted((root / "extracted").rglob("*.json"))
            for index, path in enumerate(docs):
                progress("Indexing", index, len(docs), path.name)
                doc = pipeline.read_document(path)
                db.execute("INSERT INTO files VALUES (?, ?)", (doc["source"], doc["format"]))
                db.executemany(
                    "INSERT INTO units(file, id, ord, kind, source, speaker, scene, context) VALUES (?,?,?,?,?,?,?,?)",
                    [(doc["source"], unit["id"], ord, unit["kind"], unit["source"], speaker,
                      scene(unit["context"]),
                      json.dumps(unit["context"], ensure_ascii=False))
                     for ord, (unit, speaker) in enumerate(zip(doc["units"], speakers(doc["units"]), strict=True))])
        db.close()
        write_json(root / MANIFEST, dict(
            schema_version=1, name=name, engine=engine, encoding=encoding, language_index=language_index,
            original=str(original), created=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            languages=dict(source=source_language, target=target_language),
            build=dict(wrap_columns=None, output_encoding=None)))
    except BaseException:
        for item in ("source", "extracted", DATABASE, DATABASE + "-wal", DATABASE + "-shm", MANIFEST + ".tmp"):
            path = root / item
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.exists():
                path.unlink()
        if created_root and not any(root.iterdir()):
            root.rmdir()
        raise
    return root


class Workspace:
    def __init__(self, root):
        self.root = Path(root).absolute()
        if not (self.root / MANIFEST).is_file() or not (self.root / DATABASE).is_file():
            raise VnTransError(f"not a VnTrans workspace (missing {MANIFEST} or {DATABASE}): {self.root}")
        try:
            self.manifest = json.loads((self.root / MANIFEST).read_text(encoding="utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise VnTransError(f"invalid {MANIFEST}: {exc}") from exc
        if self.manifest.get("schema_version") != 1:
            raise VnTransError(f"unsupported workspace version: {self.manifest.get('schema_version')}")
        self.db = connect(self.root / DATABASE)
        self.formats = dict(self.db.execute("SELECT file, format FROM files"))

    def close(self):
        self.db.close()

    def save_manifest(self):
        write_json(self.root / MANIFEST, self.manifest)

    def rows(self) -> list[Row]:
        return [Row(*values) for values in self.db.execute(ROWS)]

    def check(self, row: Row, text: str) -> str:
        """Normalize line endings and apply the rebuild's per-unit checks."""
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        render(row.unit(), text, self.formats[row.file])
        return text

    def save(self, row: Row, text: str | None, status: str = "edited"):
        self.save_many([(row, text)], status)

    def save_many(self, pairs, status: str = "edited"):
        """Check every (row, text) first, then store them in one transaction."""
        checked = [(row, None if text is None else self.check(row, text)) for row, text in pairs]
        with self.db:
            self.db.executemany(
                "UPDATE units SET translation = ?, status = ?, error = NULL WHERE file = ? AND id = ?",
                [(text, "new" if text is None else status, row.file, row.id) for row, text in checked])
        for row, text in checked:
            row.translation, row.status, row.error = text, "new" if text is None else status, None

    def commit(self, entries) -> list[Row]:
        """Store machine translations in one transaction; a unit changed since planning keeps its value.

        entries: (row, text, planned status, planned translation)."""
        committed = []
        with self.db:
            for row, text, status, translation in entries:
                if self.db.execute(
                        "UPDATE units SET translation = ?, status = 'machine', error = NULL "
                        "WHERE file = ? AND id = ? AND status = ? AND translation IS ?",
                        (text, row.file, row.id, status, translation)).rowcount:
                    row.translation, row.status, row.error = text, "machine", None
                    committed.append(row)
        return committed

    def mark_errors(self, rows, message: str):
        with self.db:
            self.db.executemany("UPDATE units SET error = ? WHERE file = ? AND id = ?",
                                [(message, row.file, row.id) for row in rows])
        for row in rows:
            row.error = message

    def record_attempt(self, profile, model, prompt, units, outcome, error=None, tokens_in=None, tokens_out=None):
        with self.db:
            self.db.execute("INSERT INTO attempts(at, profile, model, prompt, units, outcome, error, tokens_in, tokens_out) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (datetime.now(timezone.utc).isoformat(timespec="seconds"), profile, model, prompt, units,
                             outcome, error, tokens_in, tokens_out))

    def suggestions(self) -> dict[tuple[str, str], tuple[str, str]]:
        return {(file, id): (text, reason) for file, id, text, reason in self.db.execute(
            "SELECT file, id, text, reason FROM suggestions")}

    def suggest(self, row: Row, text: str, reason: str):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO suggestions VALUES (?, ?, ?, ?)", (row.file, row.id, text, reason))

    def dismiss(self, row: Row):
        with self.db:
            self.db.execute("DELETE FROM suggestions WHERE file = ? AND id = ?", (row.file, row.id))

    def snapshot(self, rows) -> list[tuple]:
        return [(row, row.translation, row.status, row.error) for row in rows]

    def restore(self, snapshot):
        """Put rows back exactly as `snapshot()` recorded them (translation, status and error)."""
        with self.db:
            self.db.executemany("UPDATE units SET translation = ?, status = ?, error = ? WHERE file = ? AND id = ?",
                                [(text, status, error, row.file, row.id) for row, text, status, error in snapshot])
        for row, text, status, error in snapshot:
            row.translation, row.status, row.error = text, status, error

    def exclude_many(self, rows, excluded: bool):
        values = [("excluded" if excluded else ("new" if row.translation is None else "edited"), row) for row in rows]
        with self.db:
            self.db.executemany("UPDATE units SET status = ? WHERE file = ? AND id = ?",
                                [(status, row.file, row.id) for status, row in values])
        for status, row in values:
            row.status = status

    def untranslatable(self, rows) -> list[Row]:
        """Untouched rows without letters of the source language; excluding them keeps their original text."""
        language = self.manifest["languages"]["source"]
        return [row for row in rows if row.status == "new" and row.translation is None and not row.error
                and not translatable(row.source, language)]

    def export_messages(self, folder: Path, rows) -> int:
        """LinguaGacha MESSAGE JSON: one file per script with every non-speaker unit in order, speakers as `name`.

        Uses the given rows, not the database, so it can run on a worker thread."""
        scripts = {}
        for row in rows:
            if row.kind != "speaker":
                scripts.setdefault(row.file, []).append(
                    dict(name=row.speaker, message=row.source) if row.speaker else dict(message=row.source))
        for file, items in scripts.items():
            path = folder / f"{file}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            write_json(path, items)
        return len(scripts)

    def import_messages(self, folder: Path, rows) -> tuple[list[tuple[Row, str]], int, list[str]]:
        """(row, text) pairs from LinguaGacha's translated MESSAGE JSON, the count failing checks, and unmatched files.

        Files match scripts by path and lines by position; a translated `name` fills that speaker everywhere.
        Only new and machine rows are filled, never manual edits or excluded rows."""
        lines, speakers = {}, {}
        for row in rows:
            if row.kind == "speaker":
                speakers.setdefault(row.source, []).append(row)
            else:
                lines.setdefault(row.file, []).append(row)
        candidates, names, ignored = [], {}, []
        for path in sorted(folder.rglob("*.json")):
            relative = path.relative_to(folder).as_posix().removesuffix(".json")
            file = max((name for name in lines if f"/{relative}".endswith(f"/{name}")), key=len, default=None)
            try:
                items = json.loads(path.read_text(encoding="utf-8-sig"))
            except (UnicodeError, ValueError):
                items = None
            if file is None or not isinstance(items, list) or len(items) != len(lines[file]):
                ignored.append(relative)
                continue
            for row, item in zip(lines[file], items):
                item = item if isinstance(item, dict) else {}
                candidates.append((row, item.get("message")))
                name = item.get("name")
                if row.speaker and isinstance(name, str) and name != row.speaker:
                    names.setdefault(row.speaker, name)
        candidates += [(row, name) for source, name in names.items() for row in speakers.get(source, ())]
        pairs, skipped = [], 0
        for row, text in candidates:
            if row.status not in ("new", "machine") or not isinstance(text, str) or not text.strip() \
                    or text in (row.source, row.translation):
                continue
            try:
                pairs.append((row, self.check(row, text)))
            except VnTransError:
                skipped += 1
        return pairs, skipped, ignored

    def exclude(self, row: Row, excluded: bool):
        status = "excluded" if excluded else ("new" if row.translation is None else "edited")
        with self.db:
            self.db.execute("UPDATE units SET status = ? WHERE file = ? AND id = ?", (status, row.file, row.id))
        row.status = status

    def terms(self) -> list[Term]:
        return [Term(source, target, kind, notes, bool(case), bool(approved), json.loads(evidence), id)
                for id, source, target, kind, notes, case, approved, evidence in self.db.execute(
                    "SELECT id, source, target, kind, notes, case_sensitive, approved, evidence FROM glossary ORDER BY id")]

    def save_term(self, term: Term):
        term.source, term.target = term.source.strip(), term.target.strip()
        if not term.source:
            raise VnTransError("source term is required")
        values = (term.source, term.target, term.kind, term.notes, int(term.case_sensitive), int(term.approved),
                  json.dumps(term.evidence, ensure_ascii=False))
        with self.db:
            if term.id is None:
                term.id = self.db.execute(
                    "INSERT INTO glossary(source, target, kind, notes, case_sensitive, approved, evidence) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)", values).lastrowid
            else:
                self.db.execute("UPDATE glossary SET source = ?, target = ?, kind = ?, notes = ?, case_sensitive = ?, "
                                "approved = ?, evidence = ? WHERE id = ?", (*values, term.id))

    def delete_term(self, id: int):
        with self.db:
            self.db.execute("DELETE FROM glossary WHERE id = ?", (id,))

    def import_terms(self, terms) -> tuple[int, int]:
        """Add new entries; an entry with the same source and kind is updated instead."""
        existing = {(term.source, term.kind): term for term in self.terms()}
        added = updated = 0
        for term in terms:
            current = existing.get((term.source, term.kind))
            if current:
                term.id, term.evidence = current.id, current.evidence
                updated += 1
            else:
                added += 1
            self.save_term(term)
            existing[(term.source, term.kind)] = term
        return added, updated

    def builds(self) -> list[Path]:
        return sorted((p for p in (self.root / "builds").glob("*") if (p / "patch").is_dir()), reverse=True)

    def translations(self) -> dict[tuple[str, str], str]:
        return {(file, id): text for file, id, text in self.db.execute(
            "SELECT file, id, translation FROM units WHERE translation IS NOT NULL AND status != 'excluded'")}

    def build(self, translations=None, *, wrap_columns=None, output_encoding=None, progress=pipeline._no_progress):
        """Export complete translation JSON, then rebuild it into a new patch folder.

        Pass `translations()` taken on the connection's thread to run the rest elsewhere."""
        if translations is None:
            translations = self.translations()
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = self.root / "builds" / stamp
        suffix = 1
        while target.exists():
            suffix += 1
            target = self.root / "builds" / f"{stamp}-{suffix}"
        extracted = self.root / "extracted"
        files = []
        for path in sorted(extracted.rglob("*.json")):
            doc = pipeline.read_document(path)
            for unit in doc["units"]:
                unit["translation"] = translations.get((doc["source"], unit["id"]))
            files.append((path.relative_to(extracted),
                          (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8")))
        try:
            pipeline.publish(target / "json", files, progress)
            report = pipeline.rebuild(self.root / "source", translations=target / "json", output=target / "patch",
                                      wrap_columns=wrap_columns, output_encoding=output_encoding, progress=progress)
            write_json(target / "report.json", dict(
                files=report.files, units=report.units, warnings=report.warnings,
                wrap_columns=wrap_columns, output_encoding=output_encoding))
        except BaseException:
            shutil.rmtree(target, ignore_errors=True)
            raise
        return target, report
