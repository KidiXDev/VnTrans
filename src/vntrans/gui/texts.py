"""Text indexes and worker-side workspace preparation, independent of Slint."""

import re
import time
from dataclasses import dataclass, replace

from .. import workspace

STATES = (
    "all",
    "untranslated",
    "new",
    "machine",
    "edited",
    "failed",
    "excluded",
    "issues",
    "review",
)
FIELDS = {
    "source": lambda row: (row.source,),
    "translation": lambda row: (row.translation or "",),
    "both": lambda row: (row.source, row.translation or ""),
    "speaker": lambda row: (row.speaker,),
    "id": lambda row: (row.id,),
}


def state(row):
    return "failed" if row.error and row.status == "new" else row.status


def cooperative(rows, batch=250):
    for index, row in enumerate(rows):
        yield row
        if index % batch == batch - 1:
            time.sleep(
                0.001
            )  # Yield the GIL long enough for Slint to render and handle input.


def check_rows(checker, rows, suggestions):
    tags = {}
    for index, row in enumerate(cooperative(rows)):
        marks = set(checker.issues(row))
        if (row.file, row.id) in suggestions:
            marks.add("review")
        if marks:
            tags[index] = marks
    return tags


def filter_rows(
    rows,
    *,
    file=None,
    query="",
    scope="both",
    regex=False,
    case=False,
    kind="",
    status="all",
    tags=None,
    issue="",
):
    """Matching indices and counts ignoring the status filter. Invalid regexes raise re.error."""
    tags = tags or {}
    search = (
        re.compile(
            query if regex else re.escape(query), 0 if case else re.IGNORECASE
        ).search
        if query
        else None
    )
    fields = FIELDS[scope]
    counts = dict.fromkeys(STATES, 0)
    indices = []
    for index, row in enumerate(rows):
        if file is not None and row.file != file:
            continue
        if (row.kind == "speaker") if kind == "lines" else (kind and row.kind != kind):
            continue
        if search and not any(search(value) for value in fields(row)):
            continue
        current = state(row)
        marks = tags.get(index, ())
        flagged = bool(marks) and any(mark != "review" for mark in marks)
        untranslated = current in ("new", "failed")
        counts["all"] += 1
        counts[current] += 1
        counts["untranslated"] += untranslated
        counts["issues"] += flagged
        counts["review"] += "review" in marks
        if (
            status in ("all", current)
            or (status == "untranslated" and untranslated)
            or (status == "review" and "review" in marks)
            or (status == "issues" and (issue in marks if issue else flagged))
        ):
            indices.append(index)
    return indices, counts


def filter_snapshot(rows, **options):
    return filter_rows(cooperative(rows, 4000), **options)


class TextState:
    def __init__(self, rows, formats):
        self.rows = rows
        self.index_of = {}
        self.views = []
        self.keys = []
        self.groups = {}
        self.files = {file: [0, 0, 0] for file in formats}
        self.totals = [0, 0, 0]
        self.revision = 0
        for index, row in enumerate(cooperative(rows)):
            self.index_of[(row.file, row.id)] = index
            self.views.append(replace(row))
            key = row.translation_key(formats[row.file])
            self.keys.append(key)
            self.groups.setdefault(key, []).append(index)
            self._count(row, 1)

    def _count(self, row, direction):
        values = (
            row.status != "excluded",
            row.status != "excluded" and row.translation is not None,
            row.status != "excluded" and state(row) == "failed",
        )
        for index, value in enumerate(values):
            self.totals[index] += direction * value
            self.files[row.file][index] += direction * value

    def changed(self, index, view=None):
        self._count(self.views[index], -1)
        self.views[index] = replace(self.rows[index]) if view is None else view
        self._count(self.views[index], 1)
        self.revision += 1

    def snapshot(self):
        # Views are replaced, never mutated, so copying references is enough.
        return tuple(self.views)

    def twins(self, row):
        key = self.keys[self.index_of[(row.file, row.id)]]
        return [
            other
            for index in self.groups[key]
            if (other := self.rows[index]) is not row
            and other.status != "excluded"
            and (
                other.translation is None
                or other.status == "machine"
                or other.translation == row.translation
            )
        ]


@dataclass
class Loaded:
    rows: list
    texts: TextState
    manifest: dict
    terms: list
    suggestions: dict
    undo: list
    initial: tuple


def load(root, filters=None):
    """Read and prepare a workspace entirely on the worker connection."""
    ws = workspace.Workspace(root)
    try:
        rows = [
            workspace.Row(*values)
            for values in cooperative(ws.db.execute(workspace.ROWS))
        ]
        undo = []
        if not ws.manifest.get("prefiltered"):
            skipped = ws.untranslatable(rows)
            undo = ws.snapshot(skipped)
            ws.exclude_many(skipped, True)
            ws.manifest["prefiltered"] = True
            ws.save_manifest()
        return Loaded(
            rows,
            TextState(rows, ws.formats),
            ws.manifest,
            ws.terms(),
            ws.suggestions(),
            undo,
            filter_snapshot(
                rows, **(filters if filters is not None else dict(kind="lines"))
            ),
        )
    finally:
        ws.close()
