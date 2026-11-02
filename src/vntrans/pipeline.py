"""Batch orchestration shared by the Python API and CLI."""

import hashlib
import json
import multiprocessing
import os
import shutil
import tempfile
import warnings
from collections import deque
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from contextlib import closing, contextmanager, nullcontext
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path

import orjson

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
        raise VnTransError(
            f"destination already exists (choose a new directory): {output}"
        )


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
        for right in paths[i + 1 :]:
            if left.is_relative_to(right) or right.is_relative_to(left):
                raise VnTransError(
                    f"input, translations, and output must not overlap: {left}, {right}"
                )


def _open(path):
    safe_path(path)
    return path.open("rb") if path.is_file() else None


def opened_files(paths):
    """Overlap slow file opens, retaining at most four handles and reading payloads on demand."""
    with ThreadPoolExecutor(max_workers=min(4, max(1, len(paths)))) as pool:
        remaining = iter(paths)
        pending = deque(
            (path, pool.submit(_open, path)) for path in islice(remaining, 4)
        )
        try:
            while pending:
                path, future = pending.popleft()
                stream = future.result()
                following = next(remaining, None)
                if following is not None:
                    pending.append((following, pool.submit(_open, following)))
                with stream if stream is not None else nullcontext():
                    yield path, stream
        finally:
            for _, future in pending:
                if future.cancel():
                    continue
                try:
                    stream = future.result()
                    if stream is not None:
                        stream.close()
                except (OSError, VnTransError):
                    pass


def source_files(path: Path, engine, progress=_no_progress):
    if not path.exists():
        raise VnTransError(f"input does not exist: {path}")
    progress("Scanning scripts", 0, 0, str(path))
    candidates = [path] if path.is_file() else sorted(path.rglob("*"))
    for item in candidates:
        safe_path(item)
    found = False
    with closing(opened_files(candidates)) as opened:
        for index, (item, stream) in enumerate(opened):
            progress("Extracting", index, len(candidates), str(item))
            if stream is None:
                continue
            header = stream.read(8)
            if not engine.probe(item.name, header):
                continue
            data = header + stream.read()
            relative = Path(item.name) if path.is_file() else item.relative_to(path)
            found = True
            yield relative, data
            progress("Extracting", index + 1, len(candidates), str(relative))
    progress("Extracting", len(candidates), len(candidates), "")
    if not found:
        raise VnTransError("no supported scripts found in input")


def document(relative, data, engine, script, language_index):
    return dict(
        schema_version=1,
        engine=engine,
        format=script.format,
        source=str(relative.as_posix()),
        sha256=hashlib.sha256(data).hexdigest(),
        encoding=script.encoding,
        language_index=language_index,
        units=[unit.document() for unit in script.units],
    )


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise VnTransError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def invalid_constant(value):
    raise VnTransError(f"invalid JSON constant: {value}")


def read_document(path, stream=None):
    try:
        text = (
            path.read_text(encoding="utf-8")
            if stream is None
            else stream.read().decode("utf-8")
        )
        value = json.loads(
            text, object_pairs_hook=strict_object, parse_constant=invalid_constant
        )
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
            raise VnTransError(
                f"source metadata changed: {key}; extract again from the original"
            )
    units = actual["units"]
    if not isinstance(units, list):
        raise VnTransError("units must be an array")
    original = {unit["id"]: unit for unit in expected["units"]}
    seen = set()
    translations = {}
    for unit in units:
        if not isinstance(unit, dict) or set(unit) != {
            "id",
            "kind",
            "source",
            "translation",
            "context",
        }:
            raise VnTransError("invalid unit fields")
        id = unit["id"]
        if not isinstance(id, str) or id not in original or id in seen:
            raise VnTransError(f"unknown or duplicate unit ID: {id!r}")
        seen.add(id)
        for key in ("kind", "source"):
            if not isinstance(unit[key], str) or unit[key] != original[id][key]:
                raise VnTransError(
                    f"{id}: only translation may be edited ({key} changed)"
                )
        if json.dumps(unit["context"], sort_keys=True) != json.dumps(
            original[id]["context"], sort_keys=True
        ):
            raise VnTransError(
                f"{id}: only translation may be edited (context changed)"
            )
        value = unit["translation"]
        if value is not None:
            if not isinstance(value, str):
                raise VnTransError(f"{id}: translation must be a string or null")
            translations[id] = value
    if seen != original.keys():
        raise VnTransError("translation document is missing units")
    return translations


@contextmanager
def staging(output: Path, *, empty=False):
    replace_empty = (
        empty
        and not output.is_symlink()
        and output.is_dir()
        and not any(output.iterdir())
    )
    identity = output.stat() if replace_empty else None
    if not replace_empty:
        check_destination(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".vntrans-", dir=output.parent))
    try:
        yield temporary
        if replace_empty and not output.is_symlink() and output.is_dir():
            if not os.path.samestat(identity, output.stat()):
                raise VnTransError(f"destination appeared during processing: {output}")
            output.rmdir()  # Only an empty, user-selected workspace can be removed.
        if output.exists() or output.is_symlink():
            raise VnTransError(f"destination appeared during processing: {output}")
        os.rename(temporary, output)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def write_file(folder, relative, data):
    target = folder / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def encode_document(doc):
    try:
        return orjson.dumps(doc, option=orjson.OPT_INDENT_2 | orjson.OPT_APPEND_NEWLINE)
    except orjson.JSONEncodeError:
        # Engine extensions can still use JSON integers larger than 64 bits.
        return (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def publish(output: Path, files, progress=_no_progress):
    total = len(files) if hasattr(files, "__len__") else 0
    count = 0
    with staging(output) as folder:
        for relative, data in files:
            progress("Writing output", count, total, str(relative))
            write_file(folder, relative, data)
            count += 1
        progress("Writing output", count, count, "")


def extract(
    input,
    *,
    output,
    engine="kirikiri",
    encoding=None,
    language_index=0,
    progress: ProgressCallback = _no_progress,
) -> Report:
    """Export all supported scripts to a new JSON directory."""
    if type(language_index) is not int or language_index < 0:
        raise VnTransError("language index must be a nonnegative integer")
    source, destination = safe_path(input), safe_path(output)
    disjoint(source, destination)
    check_destination(destination)
    handler = get_engine(engine)
    report = Report()
    with staging(destination) as folder:
        for relative, data in source_files(source, handler, progress):
            try:
                script = handler.extract(
                    str(relative),
                    data,
                    encoding=encoding,
                    language_index=language_index,
                )
                doc = document(relative, data, engine, script, language_index)
                write_file(folder, Path(str(relative) + ".json"), encode_document(doc))
                report.files += 1
                report.units += len(script.units)
                report.warnings.extend(f"{relative}: {w}" for w in script.warnings)
            except VnTransError as exc:
                raise VnTransError(f"{relative}: {exc}") from exc
        progress("Writing output", report.files, report.files, "")
    return report


def check_wrap(wrap_columns):
    if wrap_columns is not None and (type(wrap_columns) is not int or wrap_columns < 1):
        raise VnTransError("wrap columns must be a positive integer")


def translation_sources(source, json_dir, progress=_no_progress, *, phase="Rebuilding"):
    if not json_dir.is_dir():
        raise VnTransError(f"translation directory does not exist: {json_dir}")
    progress("Scanning translations", 0, 0, str(json_dir))
    for item in json_dir.rglob("*"):
        safe_path(item)
    docs = sorted(json_dir.rglob("*.json"), key=lambda path: path.with_suffix(""))
    if not docs:
        raise VnTransError("no translation JSON files found")
    with closing(opened_files(docs)) as opened:
        first_path, stream = next(opened)
        first = read_document(first_path, stream)
        engine = first.get("engine")
        if not isinstance(engine, str):
            raise VnTransError("translation batch must identify one engine")
        handler = get_engine(engine)
        completed = 0

        def update(_phase, _completed, _total, filename):
            progress(phase, completed, len(docs), filename)

        with closing(source_files(source, handler, update)) as sources:
            for index, (relative, data) in enumerate(sources):
                item = (first_path, None) if index == 0 else next(opened, None)
                if item is None or item[0].relative_to(json_dir) != Path(
                    str(relative) + ".json"
                ):
                    raise VnTransError(
                        "translation files do not match source scripts; missing or extra JSON"
                    )
                path, stream = item
                try:
                    doc = first if index == 0 else read_document(path, stream)
                except VnTransError as exc:
                    raise VnTransError(f"{relative}: {exc}") from exc
                first = None
                if doc.get("engine") != engine:
                    raise VnTransError("translation batch must identify one engine")
                yield relative, data, doc, engine, len(docs)
                completed += 1
        if next(opened, None) is not None:
            raise VnTransError(
                "translation files do not match source scripts; missing or extra JSON"
            )


def rebuild_file(
    relative, data, doc, engine, report, *, wrap_columns=None, output_encoding=None
):
    try:
        language = doc.get("language_index")
        if type(language) is not int or language < 0:
            raise VnTransError("invalid language index")
        encoding = doc.get("encoding")
        if encoding is not None and not isinstance(encoding, str):
            raise VnTransError("invalid encoding")
        script = get_engine(engine).extract(
            str(relative), data, encoding=encoding, language_index=language
        )
        values = translations_for(
            doc, document(relative, data, engine, script, language)
        )
        with warnings.catch_warnings(record=True) as notices:
            warnings.simplefilter("always", UserWarning)
            rebuilt = script.rebuild(
                values,
                wrap_columns=wrap_columns,
                output_encoding=output_encoding
                if script.encoding is not None
                else None,
            )
        report.files += 1
        report.units += len(values)
        report.warnings.extend(f"{relative}: {w}" for w in script.warnings)
        report.warnings.extend(f"{relative}: {w.message}" for w in notices)
        return rebuilt
    except VnTransError as exc:
        raise VnTransError(f"{relative}: {exc}") from exc


# Below this much input, starting worker processes costs more than it saves.
PARALLEL_BYTES = 1 << 20


def _rebuild_job(relative, data, doc, engine, options):
    part = Report()
    return rebuild_file(relative, data, doc, engine, part, **options), part


def _merge(item, report):
    relative, doc, total, future = item
    rebuilt, part = future.result()
    report.files += part.files
    report.units += part.units
    report.warnings += part.warnings
    return relative, doc, rebuilt, total


def rebuilt_files(sources, report, **options):
    """Yield (relative, doc, rebuilt, total) in source order; large batches use worker processes."""
    # Each worker can peak near 200 MiB on large PSB scenarios.
    workers = min(8, os.process_cpu_count() or 1)
    pending = deque()
    pool = None
    size = 0
    try:
        for relative, data, doc, engine, total in sources:
            size += len(data)
            if pool is None and (workers == 1 or size < PARALLEL_BYTES):
                rebuilt = rebuild_file(relative, data, doc, engine, report, **options)
                yield relative, doc, rebuilt, total
                continue
            if pool is None:
                pool = ProcessPoolExecutor(
                    workers, mp_context=multiprocessing.get_context("spawn")
                )
            future = pool.submit(_rebuild_job, relative, data, doc, engine, options)
            pending.append((relative, doc, total, future))
            if len(pending) >= 2 * workers:
                yield _merge(pending.popleft(), report)
        while pending:
            yield _merge(pending.popleft(), report)
    finally:
        if pool is not None:
            pool.shutdown(cancel_futures=True)


def files_done(progress, report):
    """Count finished files; reading runs ahead of rebuilding in worker processes."""
    return lambda phase, _read, total, name: progress(phase, report.files, total, name)


def _rebuild(
    input,
    translations,
    *,
    output=None,
    wrap_columns=None,
    output_encoding=None,
    progress=_no_progress,
):
    check_wrap(wrap_columns)
    source, json_dir = safe_path(input), safe_path(translations)
    destination = safe_path(output) if output is not None else None
    disjoint(source, json_dir, *([destination] if destination is not None else []))
    report = Report()
    phase = "Rebuilding" if destination is not None else "Validating"
    with (
        staging(destination) if destination is not None else nullcontext(None) as folder
    ):
        sources = translation_sources(
            source, json_dir, files_done(progress, report), phase=phase
        )
        results = rebuilt_files(
            sources, report, wrap_columns=wrap_columns, output_encoding=output_encoding
        )
        with closing(results):
            for relative, _doc, rebuilt, total in results:
                if folder is not None:
                    write_file(folder, relative, rebuilt)
                progress(phase, report.files, total, str(relative))
        progress(phase, report.files, report.files, str(relative))
        if folder is not None:
            progress("Writing output", report.files, report.files, "")
    return report


def rebuild(
    input,
    *,
    translations,
    output,
    wrap_columns=None,
    output_encoding=None,
    progress: ProgressCallback = _no_progress,
) -> Report:
    """Validate and write rebuilt scripts to a new output directory."""
    return _rebuild(
        input,
        translations,
        output=output,
        wrap_columns=wrap_columns,
        output_encoding=output_encoding,
        progress=progress,
    )


def validate(
    input,
    *,
    translations,
    wrap_columns=None,
    output_encoding=None,
    progress: ProgressCallback = _no_progress,
) -> Report:
    """Run the same checks and in-memory rebuild without writing output."""
    return _rebuild(
        input,
        translations,
        wrap_columns=wrap_columns,
        output_encoding=output_encoding,
        progress=progress,
    )
