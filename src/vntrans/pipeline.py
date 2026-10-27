"""Batch orchestration shared by the Python API and CLI."""

from dataclasses import dataclass, field
from collections.abc import Callable
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import warnings

from .engines import get_engine
from .model import VnTransError


@dataclass
class Report:
    files: int = 0
    units: int = 0
    warnings: list[str] = field(default_factory=list)


ProgressCallback = Callable[[str, int, int, str], None]


def _no_progress(phase, completed, total, filename):
    pass


def check_destination(output: Path):
    if output.exists() or output.is_symlink():
        raise VnTransError(f"destination already exists (choose a new directory): {output}")


def safe_path(value) -> Path:
    path = Path(value).absolute()
    if ".." in path.parts:
        raise VnTransError(f"parent traversal is not allowed: {path}")
    for part in (path, *path.parents):
        if part.is_symlink():
            raise VnTransError(f"symlinks are not allowed: {part}")
    return path


def disjoint(*paths):
    for i, left in enumerate(paths):
        for right in paths[i + 1:]:
            if left.is_relative_to(right) or right.is_relative_to(left):
                raise VnTransError(f"input, translations, and output must not overlap: {left}, {right}")


def source_files(path: Path, engine, progress=_no_progress):
    if not path.exists():
        raise VnTransError(f"input does not exist: {path}")
    progress("Scanning scripts", 0, 0, str(path))
    candidates = [path] if path.is_file() else sorted(path.rglob("*"))
    found = []
    for index, item in enumerate(candidates):
        progress("Reading scripts", index, len(candidates), str(item))
        safe_path(item)
        if not item.is_file():
            continue
        with item.open("rb") as stream:
            header = stream.read(8)
            if not engine.probe(item.name, header):
                continue
            data = header + stream.read()
        relative = Path(item.name) if path.is_file() else item.relative_to(path)
        found.append((relative, data))
    progress("Reading scripts", len(candidates), len(candidates), "")
    if not found:
        raise VnTransError("no supported scripts found in input")
    return found


def document(relative, data, engine, script, language_index):
    return dict(schema_version=1, engine=engine, format=script.format,
                source=str(relative.as_posix()), sha256=hashlib.sha256(data).hexdigest(),
                encoding=script.encoding, language_index=language_index,
                units=[unit.document() for unit in script.units])


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise VnTransError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def invalid_constant(value):
    raise VnTransError(f"invalid JSON constant: {value}")


def read_document(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=strict_object,
                           parse_constant=invalid_constant)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise VnTransError(f"invalid translation JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise VnTransError("translation JSON must be an object")
    return value


def translations_for(actual, expected):
    if set(actual) != set(expected):
        raise VnTransError("translation document fields differ from schema")
    for key in expected.keys() - {"units"}:
        if type(actual[key]) is not type(expected[key]) or actual[key] != expected[key]:
            raise VnTransError(f"source metadata changed: {key}; extract again from the original")
    units = actual["units"]
    if not isinstance(units, list):
        raise VnTransError("units must be an array")
    original = {unit["id"]: unit for unit in expected["units"]}
    seen = set()
    translations = {}
    for unit in units:
        if not isinstance(unit, dict) or set(unit) != {"id", "kind", "source", "translation", "context"}:
            raise VnTransError("invalid unit fields")
        id = unit["id"]
        if not isinstance(id, str) or id not in original or id in seen:
            raise VnTransError(f"unknown or duplicate unit ID: {id!r}")
        seen.add(id)
        for key in ("kind", "source", "context"):
            if json.dumps(unit[key], sort_keys=True) != json.dumps(original[id][key], sort_keys=True):
                raise VnTransError(f"{id}: only translation may be edited ({key} changed)")
        value = unit["translation"]
        if value is not None:
            if not isinstance(value, str):
                raise VnTransError(f"{id}: translation must be a string or null")
            translations[id] = value
    if seen != original.keys():
        raise VnTransError("translation document is missing units")
    return translations


def publish(output: Path, files, progress=_no_progress):
    check_destination(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".vntrans-", dir=output.parent))
    try:
        for index, (relative, data) in enumerate(files):
            progress("Writing output", index, len(files), str(relative))
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        if output.exists() or output.is_symlink():
            raise VnTransError(f"destination appeared during processing: {output}")
        os.rename(staging, output)
        progress("Writing output", len(files), len(files), "")
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def extract(input, *, output, engine="kirikiri", encoding=None, language_index=0,
            progress: ProgressCallback = _no_progress) -> Report:
    """Export all supported scripts to a new JSON directory."""
    if type(language_index) is not int or language_index < 0:
        raise VnTransError("language index must be a nonnegative integer")
    source, destination = safe_path(input), safe_path(output)
    disjoint(source, destination)
    check_destination(destination)
    handler = get_engine(engine)
    files = []
    report = Report()
    sources = source_files(source, handler, progress)
    for index, (relative, data) in enumerate(sources):
        progress("Extracting", index, len(sources), str(relative))
        try:
            script = handler.extract(str(relative), data, encoding=encoding, language_index=language_index)
            doc = document(relative, data, engine, script, language_index)
            encoded = (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            files.append((Path(str(relative) + ".json"), encoded))
            report.files += 1
            report.units += len(script.units)
            report.warnings.extend(f"{relative}: {w}" for w in script.warnings)
            progress("Extracting", index + 1, len(sources), str(relative))
        except VnTransError as exc:
            raise VnTransError(f"{relative}: {exc}") from exc
    publish(destination, files, progress)
    return report


def _rebuild(input, translations, *, output=None, wrap_columns=None, output_encoding=None,
             progress=_no_progress):
    if wrap_columns is not None and (type(wrap_columns) is not int or wrap_columns < 1):
        raise VnTransError("wrap columns must be a positive integer")
    source, json_dir = safe_path(input), safe_path(translations)
    destination = safe_path(output) if output is not None else None
    disjoint(source, json_dir, *([destination] if destination is not None else []))
    if destination is not None:
        check_destination(destination)
    if not json_dir.is_dir():
        raise VnTransError(f"translation directory does not exist: {json_dir}")
    progress("Scanning translations", 0, 0, str(json_dir))
    for item in json_dir.rglob("*"):
        safe_path(item)
    docs = sorted(json_dir.rglob("*.json"))
    if not docs:
        raise VnTransError("no translation JSON files found")
    loaded = {}
    for index, path in enumerate(docs):
        progress("Loading JSON", index, len(docs), str(path.relative_to(json_dir)))
        try:
            loaded[path.relative_to(json_dir)] = read_document(path)
        except VnTransError as exc:
            raise VnTransError(f"{path.name}: {exc}") from exc
    progress("Loading JSON", len(docs), len(docs), "")
    engine_names = {doc.get("engine") for doc in loaded.values() if isinstance(doc.get("engine"), str)}
    if len(engine_names) != 1:
        raise VnTransError("translation batch must identify one engine")
    engine = engine_names.pop()
    handler = get_engine(engine)
    sources = source_files(source, handler, progress)
    if set(loaded) != {Path(str(relative) + ".json") for relative, _ in sources}:
        raise VnTransError("translation files do not match source scripts; missing or extra JSON")
    files = []
    report = Report()
    phase = "Rebuilding" if destination is not None else "Validating"
    for index, (relative, data) in enumerate(sources):
        progress(phase, index, len(sources), str(relative))
        try:
            doc = loaded[Path(str(relative) + ".json")]
            language = doc.get("language_index")
            if type(language) is not int or language < 0:
                raise VnTransError("invalid language index")
            encoding = doc.get("encoding")
            if encoding is not None and not isinstance(encoding, str):
                raise VnTransError("invalid encoding")
            script = handler.extract(str(relative), data, encoding=encoding, language_index=language)
            values = translations_for(doc, document(relative, data, engine, script, language))
            with warnings.catch_warnings(record=True) as notices:
                warnings.simplefilter("always", UserWarning)
                rebuilt = script.rebuild(values, wrap_columns=wrap_columns,
                                         output_encoding=output_encoding if script.encoding is not None else None)
            files.append((relative, rebuilt))
            report.files += 1
            report.units += len(values)
            report.warnings.extend(f"{relative}: {w}" for w in script.warnings)
            report.warnings.extend(f"{relative}: {w.message}" for w in notices)
            progress(phase, index + 1, len(sources), str(relative))
        except VnTransError as exc:
            raise VnTransError(f"{relative}: {exc}") from exc
    if destination is not None:
        publish(destination, files, progress)
    return report


def rebuild(input, *, translations, output, wrap_columns=None, output_encoding=None,
            progress: ProgressCallback = _no_progress) -> Report:
    """Validate and write rebuilt scripts to a new output directory."""
    return _rebuild(input, translations, output=output, wrap_columns=wrap_columns,
                    output_encoding=output_encoding, progress=progress)


def validate(input, *, translations, wrap_columns=None, output_encoding=None,
             progress: ProgressCallback = _no_progress) -> Report:
    """Run the same checks and in-memory rebuild without writing output."""
    return _rebuild(input, translations, wrap_columns=wrap_columns,
                    output_encoding=output_encoding, progress=progress)
