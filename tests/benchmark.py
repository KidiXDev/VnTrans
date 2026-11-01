"""Temporary synthetic workloads: uv run --extra gui python tests/benchmark.py."""

import argparse
import asyncio
import cProfile
import gc
import json
import shutil
import statistics
import tempfile
import time
import tracemalloc
from contextlib import contextmanager
from pathlib import Path

from fixtures import make_psb

from vntrans import pipeline, workspace
from vntrans.engines.kirikiri.ks import KsScript
from vntrans.gui.texts import TextState, check_rows, filter_snapshot, load
from vntrans.translation.quality import Checker
from vntrans.workspace import Row


def sources(folder, files, lines, format):
    folder.mkdir()
    ks = b"\xef\xbb\xbf" + (("Original text " + "x" * 80 + "[l]\n") * lines).encode()
    scn = make_psb(
        {
            "name": "scene.ks",
            "scenes": [
                {
                    "texts": [
                        [None, None, "Original text " + "x" * 80] for _ in range(lines)
                    ]
                }
            ],
        }
    )
    for index in range(files):
        binary = format == "scn" or (format == "mixed" and index % 2)
        (folder / f"{index:05}.{'scn' if binary else 'ks'}").write_bytes(
            scn if binary else ks
        )


@contextmanager
def workload(args, case, source=None, prepared=None):
    with tempfile.TemporaryDirectory(prefix="vntrans-benchmark-") as temporary:
        root = Path(temporary)
        if case.startswith("gui-"):
            rows = [
                Row(
                    f"{i // args.lines}.ks",
                    str(i),
                    "dialogue",
                    "Original text ⟦0⟧",
                    "Alice",
                    "start",
                    '{"controls":["[l]"],"original":"Original text [l]"}',
                    "Translated text ⟦0⟧",
                    "machine",
                    None,
                )
                for i in range(args.files * args.lines)
            ]
            formats = {row.file: "ks" for row in rows}
            texts = TextState(rows, formats)
            snapshot = texts.snapshot()
            if case == "gui-filter":
                yield lambda: filter_snapshot(snapshot, query="Original", kind="lines")
            elif case == "gui-duplicates":
                yield lambda: texts.twins(rows[0])
            elif case == "gui-stats":
                yield lambda: (
                    tuple(texts.totals),
                    [(name, *counts) for name, counts in texts.files.items()],
                )
            else:
                from types import SimpleNamespace

                checker = Checker(
                    lambda row, text: workspace.Workspace.check(
                        SimpleNamespace(formats=formats), row, text
                    ),
                    "English",
                    [],
                )
                yield lambda: check_rows(checker, snapshot, {})
            return
        if case.startswith("ks-"):
            data = (
                b"\xef\xbb\xbf"
                + (("Original text " + "x" * 80 + "[l]\n") * args.large_lines).encode()
            )
            if case == "ks-parse":
                yield lambda: KsScript(data)
            else:
                script = KsScript(data)
                values = {
                    u.id: u.source.replace("Original", "Translated")
                    for u in script.units
                }
                yield lambda: script.rebuild(values)
            return
        if source is None:
            source = root / "source"
            sources(source, args.files, args.lines, args.format)
        if case == "extract":
            yield lambda: pipeline.extract(source, output=root / "json")
            return
        if case == "create":
            yield lambda: workspace.create(
                source, root / "work", name="Benchmark", source_language="English"
            )
            return
        project = prepared or root / "work"
        if prepared is None:
            workspace.create(
                source, project, name="Benchmark", source_language="English"
            )
        if case == "open":
            yield lambda: load(project)
            return
        ws = workspace.Workspace(project)
        built = []
        try:
            if case == "validate":
                yield lambda: pipeline.validate(
                    ws.root / "source", translations=ws.root / "extracted"
                )
            else:
                rows = ws.rows()
                step = 100 if case == "build-sparse" else 1
                values = (
                    {}
                    if case == "build-unchanged"
                    else {
                        (row.file, row.id): row.source.replace("Original", "Translated")
                        for row in rows[::step]
                    }
                )

                def build():
                    target, report = ws.build(values)
                    built.append(target)
                    return target, report

                yield build
        finally:
            ws.close()
            for target in built:
                assert target.resolve().is_relative_to(project.resolve() / "builds")
                shutil.rmtree(target)


async def heartbeat(action):
    gaps = []
    running = True

    async def tick():
        previous = time.perf_counter()
        while running:
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            gaps.append((now - previous) * 1000)
            previous = now

    task = asyncio.create_task(tick())
    await asyncio.sleep(0)
    try:
        await asyncio.to_thread(action)
    finally:
        running = False
        await task
    gaps.sort()
    return gaps[min(len(gaps) - 1, int(len(gaps) * 0.95))], max(gaps)


def main():
    cases = (
        "ks-parse",
        "ks-rebuild",
        "extract",
        "create",
        "open",
        "validate",
        "build-unchanged",
        "build-sparse",
        "build-full",
        "gui-filter",
        "gui-duplicates",
        "gui-stats",
        "gui-quality",
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=1000)
    parser.add_argument("--lines", type=int, default=100)
    parser.add_argument("--large-lines", type=int, default=16000)
    parser.add_argument("--format", choices=("mixed", "ks", "scn"), default="mixed")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--case", choices=cases, action="append")
    parser.add_argument(
        "--profile",
        help="Write cProfile data instead of timing/memory/heartbeat checks",
    )
    parser.add_argument(
        "--encoder", choices=("default", "stdlib", "orjson"), default="default"
    )
    parser.add_argument("--output", type=Path, help="Optional JSON results file")
    args = parser.parse_args()
    if args.encoder == "stdlib":
        pipeline.encode_document = lambda doc: (
            json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
    if args.encoder == "orjson":
        import orjson

        pipeline.encode_document = lambda doc: orjson.dumps(
            doc, option=orjson.OPT_INDENT_2 | orjson.OPT_APPEND_NEWLINE
        )
    if min(args.files, args.lines, args.large_lines, args.runs) < 1:
        parser.error("sizes and runs must be positive")
    with tempfile.TemporaryDirectory(prefix="vntrans-fixture-") as directory:
        selected = args.case or cases
        source = prepared = None
        if any(not case.startswith(("ks-", "gui-")) for case in selected):
            source = Path(directory) / "source"
            sources(source, args.files, args.lines, args.format)
        if any(
            case
            in ("open", "validate", "build-unchanged", "build-sparse", "build-full")
            for case in selected
        ):
            prepared = Path(directory) / "work"
            workspace.create(
                source, prepared, name="Benchmark", source_language="English"
            )
        results = [measure(args, case, source, prepared) for case in selected]
    if args.output:
        args.output.write_text(
            json.dumps(
                dict(
                    workload=vars(args) | {"output": str(args.output)}, results=results
                ),
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )


def measure(args, case, source, prepared):
    if args.profile:
        with workload(args, case, source, prepared) as action:
            cProfile.runctx(
                "action()", globals(), locals(), f"{args.profile}-{case}.prof"
            )
        return dict(case=case, profile=f"{args.profile}-{case}.prof")
    samples = []
    for _ in range(args.runs):
        with workload(args, case, source, prepared) as action:
            gc.collect()
            start = time.perf_counter()
            action()
            samples.append(time.perf_counter() - start)
    with workload(args, case, source, prepared) as action:
        gc.collect()
        tracemalloc.start()
        try:
            action()
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
    with workload(args, case, source, prepared) as action:
        gc.collect()
        p95, maximum = asyncio.run(heartbeat(action))
    result = dict(
        case=case,
        median_s=statistics.median(samples),
        samples_s=samples,
        peak_python_mib=peak / 1024**2,
        heartbeat_p95_ms=p95,
        heartbeat_max_ms=maximum,
    )
    print(json.dumps(result), flush=True)
    return result


if __name__ == "__main__":
    main()
