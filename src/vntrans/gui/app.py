"""Slint front end. Workspace and model mutations run on the event-loop thread only."""

import asyncio
import gc
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from collections import Counter, deque
from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from itertools import islice
from pathlib import Path

import slint

from .. import config, workspace
from ..model import VnTransError
from ..translation import glossary, prompts, quality, runner
from ..translation.providers import PROVIDERS, REASONING, Profile, ProviderError
from . import geometry, jobs
from .dialogs import pick
from .jobs import Cancelled, Latest, Progress
from .texts import (
    STATES,
    TextState,
    category,
    check_rows,
    diff_markdown,
    filter_rows,
    filter_snapshot,
    load,
    review_files,
    review_queue,
    review_targets,
    state,
)

UI = slint.load_file(Path(__file__).parent / "ui" / "main.slint", style="fluent-dark")
ISSUES = ("",) + tuple(quality.LABELS)
TASKS = ("translate", "discover")
REVIEW_CATEGORIES = (*prompts.REVIEW_ERRORS.values(), "Other")


def styled_diff(old, new):
    try:
        return slint.StyledText.from_markdown(diff_markdown(old, new))
    except ValueError:
        return slint.StyledText.from_plain_text(new)


def context_row(row):
    return dict(
        speaker=row.speaker if row.kind != "speaker" else "",
        source=row.source,
        translation=row.translation or "",
    )


SCOPES = ("source", "translation", "both", "speaker", "id")
KINDS = ("lines", "dialogue", "speaker", "choice", "title")
ABOUT = {
    "characters": "Approved character entries whose names appear in the batch.",
    "glossary": "Approved terms that appear in the batch.",
    "context": "Nearby lines from the same scene, with existing translations, as read-only reference.",
    "thinking": "Strengthens instruction following by having the model write brief structured reasoning (context, "
    "then decisions) before its translations. In reviews it checks every line against the source and "
    "argues for the current wording before suggesting a fix. Turning it off saves output tokens per batch "
    "but noticeably lowers quality. On by default.",
    "contract": "Fixed. VnTrans relies on this format to read and check replies.",
}


def bars(values, count=60):
    """The last `count` values scaled to 0..1, left-padded with zeros so bars keep their width."""
    recent = list(values)[-count:]
    top = max(recent, default=0) or 1
    return [0.0] * (count - len(recent)) + [value / top for value in recent]


def compact(number) -> str:
    for size, suffix in ((1_000_000, "M"), (1_000, "k")):
        if number >= size:
            return f"{number / size:.1f}{suffix}"
    return str(number)


def duration(seconds) -> str:
    seconds = int(seconds)
    return (
        f"{seconds // 3600}:{seconds // 60 % 60:02}:{seconds % 60:02}"
        if seconds >= 3600
        else f"{seconds // 60}:{seconds % 60:02}"
    )


def oneline(text: str) -> str:
    return text.replace("\n", " ⏎ ")


class UnitModel(slint.Model):
    def __init__(self, rows, indices, picked=frozenset()):
        super().__init__()
        self.rows = rows
        self.indices = indices
        self.picked = picked

    def row_count(self):
        return len(self.indices)

    def row_data(self, index):
        position = self.indices[index]
        row = self.rows[position]
        return dict(
            n=position + 1,
            kind=row.kind,
            speaker=row.speaker,
            source=oneline(row.source),
            translation=oneline(row.translation or ""),
            state=state(row),
            picked=position in self.picked,
        )

    def reset(self, rows, indices):
        """Show other rows. Notifying the view is near-instant, while assigning a new model to the
        property makes Slint read every row up front."""
        removed = len(self.indices)
        self.rows, self.indices = rows, indices
        if removed:
            self.notify_row_removed(0, removed)
        if indices:
            self.notify_row_added(0, len(indices))


class Window(UI.AppWindow):
    def __init__(self):
        super().__init__()
        self.ws = None
        self.rows = []
        self.picked = set()
        self.model = UnitModel([], [], self.picked)
        self.file_names = []
        self.files = slint.ListModel([])
        self.terms = []
        self.term_view = []
        self.term = None
        self.sections = []
        self.build_reports = []
        self.positions = {}
        self.anchor = None
        self.undo_entry = None
        self.tags = {}
        self.suggestions = {}
        self.reviews = {}
        self.review_file_name = None
        self.review_label = "All"
        self.review_labels = ["All"]
        self.review_keys = []
        self.checker = None
        self.texts = TextState([], {})
        self.filter_job = Latest(self.filter_error)
        self.check_job = Latest(self.fail)
        self.filter_reset = False
        self.progress_updates = []
        self.editing = False
        self.loading_depth = 0
        self.open_serial = 0
        self.last_action = time.monotonic()
        self.run = None
        self.parallel = set()
        self.cancel = threading.Event()
        self.log = slint.ListModel([])
        self.batches = slint.ListModel([])
        self.batch_views = []
        self.rate_samples = deque(maxlen=120)
        self.debounce = slint.Timer()
        self.section_timer = slint.Timer()
        self.rules_timer = slint.Timer()
        self.run_timer = slint.Timer()
        self.settings = config.load()
        self.profiles = [
            Profile.load(value) for value in self.settings.get("profiles", [])
        ]
        self.geometry = geometry.normalize(self.settings.get("window"))
        self.native = None
        self.geometry_timer = slint.Timer()
        self.gc_timer = slint.Timer()
        self.gc_timer.start(
            slint.TimerMode.Repeated, timedelta(seconds=5), self.collect
        )
        self.ui.log = self.log
        self.ui.batches = self.batches
        self.ui.units = self.model
        self.show_recent()
        self.show_profiles()
        self.profile_new()

    @property
    def ui(self):
        return self.App

    def collect(self):
        if (
            time.monotonic() - self.last_action < 2
            or self.ui.loading
            or self.ui.busy
            or self.editing
            or self.run
        ):
            return
        if all(
            job.task is None or job.task.done()
            for job in (self.filter_job, self.check_job)
        ):
            gc.collect(0)

    def save_config(self):
        self.settings["profiles"] = [profile.dump() for profile in self.profiles]
        try:
            config.save(self.settings)
        except OSError as exc:
            self.fail(exc)

    def restore_geometry(self):
        """Apply the saved size; call before the window is first shown."""
        self.saved_width = self.geometry.get("width", self.saved_width)
        self.saved_height = self.geometry.get("height", self.saved_height)

    async def track_geometry(self):
        """Once the event loop has created the native window, restore its position (Windows) and maximized
        state, then follow its moves.

        Maximizing before the window exists lets Slint shrink it back to its preferred size while it stays
        flagged as maximized, stuck at the screen's top-left corner. Without a native window to wait for
        (other platforms), a short delay stands in for window creation."""
        position, maximized = self.geometry.get("position"), self.geometry["maximized"]
        if geometry.Native is not None:
            for _ in range(100):
                self.native = geometry.Native.find()
                if self.native:
                    break
                await asyncio.sleep(0.02)
        if self.native and position:
            self.native.restore(position, maximized)
        else:
            if self.native is None:
                await asyncio.sleep(0.1)
            self.window_maximized = maximized
        if self.native:
            self.native.watch(self.capture_geometry)
            self.capture_geometry()

    @slint.callback()
    def geometry_changed(self):
        self.geometry_timer.start(
            slint.TimerMode.SingleShot,
            timedelta(milliseconds=250),
            self.capture_geometry,
        )

    def capture_geometry(self):
        """Record the current state. A minimized window keeps its earlier state, so the app never starts
        minimized, and the size is taken only while the window is neither maximized nor minimized."""
        state = self.geometry
        found = self.native.read() if self.native else None
        if found:
            (left, top, _, _), shown, state["maximized"] = found
            state["position"] = [left, top]
            normal = shown == geometry.NORMAL
        else:
            normal = True
            if not self.window_minimized:
                state["maximized"] = self.window_maximized
        if normal and not self.window_maximized and not self.window_minimized:
            state["width"], state["height"] = self.window_width, self.window_height

    def save_geometry(self):
        """Store the window state at exit; the native window is gone by then, so Slint's last values count."""
        self.geometry_timer.stop()
        if self.native:
            self.native.close()
            self.native = None
        self.capture_geometry()
        self.settings["window"] = self.geometry
        self.save_config()

    def show_recent(self):
        self.ui.recent = slint.ListModel(
            dict(name=item.get("name", ""), path=item["path"])
            for item in self.settings.get("recent", [])
        )

    @slint.callback(global_name="App")
    def remove_recent(self, path):
        settings = self.settings | {
            "recent": [
                item
                for item in self.settings.get("recent", [])
                if item.get("path") != path
            ]
        }
        try:
            config.save(settings)
        except OSError as exc:
            self.fail(exc)
            return
        self.settings = settings
        self.ui.missing_workspace = ""
        self.show_recent()
        self.say("Removed from recent history")

    def say(self, message, error=False):
        self.ui.message = message
        self.ui.message_error = error

    def fail(self, exc):
        self.say(str(exc), error=True)

    def busy(self, text):
        self.cancel.clear()
        self.ui.busy = True
        self.ui.busy_text = text
        self.ui.busy_progress = 0
        self.say("")

    def idle(self):
        for progress in self.progress_updates:
            progress.close()
        self.progress_updates.clear()
        self.ui.busy = False

    def progress(self):
        def show(phase, completed, total, filename):
            self.ui.busy_text = f"{phase} {filename}".strip()
            self.ui.busy_progress = completed / total if total else 0

        update = Progress(show, self.cancel)
        self.progress_updates.append(update)
        return update

    @slint.callback(global_name="App")
    def cancel_busy(self):
        if self.ui.busy:
            self.cancel.set()
            self.ui.busy_text = "Cancelling…"

    @slint.callback(global_name="App")
    async def browse(self, target):
        titles = dict(
            open="Open workspace", original="Game scripts", workspace="Workspace folder"
        )
        path = await jobs.thread(pick, titles[target])
        if not path:
            return
        if target == "open":
            await self.open(path)
        elif target == "original":
            self.ui.new_original = path
            if not self.ui.new_name:
                self.ui.new_name = Path(path).name
        else:
            self.ui.new_workspace = path

    @slint.callback(global_name="App")
    async def open_workspace(self, path):
        self.say("")
        try:
            exists = Path(path).expanduser().exists()
        except OSError as exc:
            self.fail(exc)
            return
        if not exists:
            self.ui.missing_workspace = path
            return
        await self.open(path)

    @slint.callback(global_name="App")
    async def create_project(self):
        ui = self.ui
        encoding = list(self.Languages.encodings)[ui.new_encoding]
        self.busy("Creating project")
        try:
            root = await jobs.run(
                workspace.create,
                Path(ui.new_original).expanduser(),
                Path(ui.new_workspace).expanduser(),
                name=ui.new_name.strip(),
                encoding=None if encoding == "Auto" else encoding,
                language_index=ui.new_language_index,
                source_language=list(self.Languages.sources)[ui.new_source],
                target_language=list(self.Languages.targets)[ui.new_target],
                progress=self.progress(),
                cancel=self.cancel,
            )
        except Cancelled:
            self.say("Cancelled")
            return
        except (VnTransError, OSError) as exc:
            self.fail(exc)
            return
        finally:
            self.idle()
        ui.creating = False
        ui.new_name = ui.new_original = ui.new_workspace = ""
        await self.open(root)

    @contextmanager
    def overlay(self, text):
        """Block input behind the loading overlay until the block ends; its spinner fades in after a moment."""
        self.ui.loading_text = text
        self.loading_depth += 1
        self.ui.loading = True
        try:
            yield
        finally:
            self.loading_depth -= 1
            self.ui.loading = self.loading_depth > 0

    async def open(self, path):
        """Read the workspace's rows on a worker thread behind the loading overlay, then show it."""
        if self.run:
            return
        self.open_serial += 1
        serial = self.open_serial
        path = Path(path).expanduser()
        name = (
            next(
                (
                    item.get("name")
                    for item in self.settings.get("recent", [])
                    if item.get("path") == str(path.absolute())
                ),
                None,
            )
            or path.name
        )
        with self.overlay(f"Opening {name}…"):
            try:
                ws = workspace.Workspace(path)
                try:
                    filters = self.filters() | dict(
                        file=None, query="", status="all", tags={}
                    )
                    data = await jobs.thread(load, ws.root, filters)
                except BaseException:
                    ws.close()
                    raise
            except (VnTransError, OSError, ValueError, sqlite3.Error) as exc:
                if serial == self.open_serial:
                    self.fail(exc)
                return
            if serial == self.open_serial:
                self.show_workspace(ws, data)
                self.last_action = time.monotonic()
            else:
                ws.close()

    def show_workspace(self, ws, data):
        self.filter_job.invalidate()
        self.check_job.invalidate()
        self.filter_reset = False
        if self.ws:
            self.ws.close()
        self.ws, self.rows = ws, data.rows
        ws.manifest = data.manifest
        self.texts = data.texts
        self.index_of = data.texts.index_of
        self.tags = {}
        self.terms = data.terms
        self.suggestions = data.suggestions
        self.reviews = data.reviews
        self.review_file_name, self.review_label, self.review_keys = None, "All", []
        self.forget_undo()
        skipped = len(data.undo)
        if skipped:
            self.undo_entry = ("auto-exclude", data.undo)
            self.ui.can_undo, self.ui.undo_text = True, "Undo auto-exclude"
        config.remember(self.settings, ws.root, ws.manifest["name"])
        self.save_config()
        self.show_recent()
        manifest = ws.manifest
        ui = self.ui
        ui.project_name = manifest["name"]
        self.show_info()
        self.file_names = sorted(ws.formats)
        ui.has_ks = "ks" in ws.formats.values()
        build = manifest.get("build", {})
        ui.wrap = build.get("wrap_columns") is not None
        ui.wrap_columns = build.get("wrap_columns") or 48
        encodings = list(self.Encodings.output)
        ui.output_encoding = (
            encodings.index(build["output_encoding"])
            if build.get("output_encoding") in encodings
            else 0
        )
        ui.file_index = -1
        ui.query = ""
        ui.status_filter = "all"
        ui.current = -1
        self.show_builds()
        self.update_stats()
        self.filtered(*data.initial)
        self.recheck()
        ui.rules_text = ws.manifest.get("replacements", "")
        self.check_rules()
        ui.term_query = ""
        self.show_terms()
        self.term_new()
        self.load_prompt()
        self.load_run_settings()
        ui.page = "texts"
        ui.has_project = True
        self.say(
            f"Excluded {skipped:,} lines with nothing to translate" if skipped else ""
        )

    @slint.callback(global_name="App")
    def navigate(self, page):
        self.last_action = time.monotonic()
        self.ui.page = page
        if page == "prompt":
            self.update_preview()
        elif page == "review":
            self.show_review()

    @slint.callback(global_name="App")
    def close_project(self):
        if self.run:
            return
        self.open_serial += 1
        if self.ws:
            self.ws.close()
        self.ws, self.rows = None, []
        self.filter_job.invalidate()
        self.check_job.invalidate()
        self.texts = TextState([], {})
        self.checker = None
        self.picked.clear()
        self.model.reset([], [])
        self.ui.has_project = False
        gc.collect()
        self.say("")

    def show_info(self):
        manifest = self.ws.manifest
        languages = manifest["languages"]
        self.ui.project_info = f"{manifest['engine'].title()} · {languages['source']} → {languages['target']}"

    def update_stats(self):
        included, translated, failed = self.texts.totals
        ui = self.ui
        ui.suggestion_count = len(self.suggestions)
        ui.total, ui.translated, ui.failed = included, translated, failed
        ui.progress_text = f"{translated:,} / {included:,}"
        ui.status_text = f"{len(self.rows):,} units · {translated:,} translated" + (
            f" · {failed:,} failed" if failed else ""
        )
        files = [
            dict(
                name=name,
                total=self.texts.files[name][0],
                done=self.texts.files[name][1],
            )
            for name in self.file_names
        ]
        if [row["name"] for row in self.files] == self.file_names:
            for index, value in enumerate(files):
                if self.files[index] != value:
                    self.files[index] = value
        else:
            self.files = slint.ListModel(files)
            ui.files = self.files

    def filters(self):
        ui = self.ui
        return dict(
            file=self.file_names[ui.file_index] if ui.file_index >= 0 else None,
            query=ui.query,
            scope=SCOPES[ui.scope],
            regex=ui.regex,
            case=ui.match_case,
            kind=KINDS[ui.kind_index],
            status=ui.status_filter,
            tags=self.tags,
            issue=ISSUES[ui.issue_index],
        )

    @slint.callback(global_name="App")
    def query_edited(self, text):
        self.last_action = time.monotonic()
        self.ui.query = text
        self.filter_job.invalidate()
        self.debounce.start(
            slint.TimerMode.SingleShot, timedelta(milliseconds=200), self.apply_filter
        )

    @slint.callback(global_name="App")
    def filter_changed(self):
        self.last_action = time.monotonic()
        self.apply_filter()

    def apply_filter(self):
        self.queue_filter(True)

    def filter_error(self, exc):
        if isinstance(exc, re.error):
            self.ui.regex_error = True
        else:
            self.fail(exc)

    def queue_filter(self, reset=False):
        if self.ws is None:
            return
        self.filter_reset |= reset
        texts = self.texts
        revision = texts.revision
        rows = texts.snapshot()
        options = self.filters() | dict(tags=dict(self.tags))

        def apply(result):
            if texts is not self.texts:
                return
            if revision != texts.revision:
                self.queue_filter()
                return
            indices, counts = result
            if self.filter_reset:
                self.filtered(indices, counts)
                self.filter_reset = False
            else:
                self.show_counts(counts)

        self.filter_job.submit(lambda: filter_snapshot(rows, **options), apply)

    def filtered(self, indices, counts):
        ui = self.ui
        ui.regex_error = False
        selected = (
            self.model.indices[ui.current]
            if 0 <= ui.current < len(self.model.indices)
            else None
        )
        self.picked.clear()
        self.anchor = None
        self.model.reset(self.rows, indices)
        self.positions = {index: position for position, index in enumerate(indices)}
        self.show_counts(counts)
        try:
            self.select(self.positions[selected])
        except KeyError:
            self.select(0 if indices else -1)

    def refresh_counts(self):
        self.queue_filter()

    def show_counts(self, counts):
        self.ui.counts = slint.ListModel(f"{counts[name]:,}" for name in STATES)
        self.ui.shown_text = f"{len(self.model.indices):,}"

    def current_row(self):
        index = self.ui.current
        return (
            self.rows[self.model.indices[index]]
            if 0 <= index < len(self.model.indices)
            else None
        )

    @slint.callback(global_name="App")
    def select(self, index):
        ui = self.ui
        if not -1 <= index < len(self.model.indices):
            return
        ui.current = index
        row = self.current_row()
        if row is None:
            ui.cur_source = ui.cur_legend = ui.cur_meta = ui.cur_state = (
                ui.cur_error
            ) = ui.cur_translation = ""
            ui.cur_speaker = ui.cur_number = ""
            ui.cur_issues = ui.cur_suggestion = ui.cur_reason = ""
            return
        controls = json.loads(row.context).get("controls", [])
        ui.cur_source = row.source
        ui.cur_legend = "\n".join(
            f"⟦{i}⟧  {control}" for i, control in enumerate(controls)
        )
        ui.cur_meta = f"{row.file} · {row.kind} · {row.id}"
        ui.cur_speaker = row.speaker if row.kind != "speaker" else ""
        ui.cur_number = f"{self.model.indices[index] + 1:,}"
        ui.cur_state = state(row)
        ui.cur_error = row.error or ""
        ui.cur_translation = row.translation or ""
        self.show_marks(row)

    def show_marks(self, row):
        ui = self.ui
        marks = self.tags.get(self.index_of[(row.file, row.id)], ())
        ui.cur_issues = " · ".join(
            quality.LABELS[mark] for mark in marks if mark != "review"
        )
        suggestion, ui.cur_reason = self.suggestions.get((row.file, row.id), ("", ""))
        if suggestion:
            ui.cur_diff = styled_diff(row.translation or "", suggestion)
        ui.cur_suggestion = suggestion

    def retag(self, index):
        row = self.rows[index]
        marks = set(self.checker.issues(row)) if self.checker else set()
        if (row.file, row.id) in self.suggestions:
            marks.add("review")
        if marks:
            self.tags[index] = marks
        else:
            self.tags.pop(index, None)

    def recheck(self):
        """Recompute issue tags for every row off the event loop, then refresh the view."""
        ws, texts = self.ws, self.texts
        revision = texts.revision
        rows, suggestions = texts.snapshot(), dict(self.suggestions)
        terms = [replace(term) for term in self.terms]
        target = ws.manifest["languages"]["target"]
        honorifics = prompts.keeps_honorifics(self.sections, ws.manifest["languages"])

        def scan():
            checker = quality.Checker(
                ws.check,
                target,
                terms,
                quality.speaker_names(rows, terms),
                ws.formats,
                honorifics,
            )
            return checker, check_rows(checker, rows, suggestions)

        def apply(result):
            if ws is not self.ws or texts is not self.texts:
                return
            if revision != texts.revision:
                self.recheck()
                return
            self.checker, tags = result
            self.tags = tags
            if self.ui.status_filter in ("issues", "review"):
                self.apply_filter()
            else:
                self.refresh_counts()
            row = self.current_row()
            if row is not None:
                self.show_marks(row)

        self.check_job.submit(scan, apply)

    def row_changed(self, row):
        self.texts.changed(self.index_of[(row.file, row.id)])
        self.retag(self.index_of[(row.file, row.id)])
        self.model.notify_row_changed(self.ui.current)
        self.ui.cur_state = state(row)
        self.show_marks(row)
        self.update_stats()
        self.refresh_counts()

    @slint.callback(global_name="App")
    async def save(self, text, advance):
        row = self.current_row()
        if row is None:
            return
        text = text or None
        try:
            if text is not None:
                self.ws.check(row, text)
        except VnTransError as exc:
            self.ui.cur_error = str(exc).removeprefix(f"{row.id}: ")
            return
        twins = [] if text is None else self.twins(row)
        entries = await self.edit(
            "edit" if twins else None,
            lambda: [
                replace(
                    other,
                    translation=text,
                    status="new" if text is None else "edited",
                    error=None,
                )
                for other in (row, *twins)
            ],
            dismiss="edited",
        )
        if entries is None:
            return
        self.ui.cur_error = ""
        if twins:
            self.say(
                f"Also applied to {len(twins):,} identical line{'s' * (len(twins) != 1)}"
            )
        if advance and self.ui.current + 1 < len(self.model.indices):
            self.select(self.ui.current + 1)

    def twins(self, row) -> list:
        """Other rows with matching text and protected controls whose translation follows `row`'s.

        Manual edits that differ from `row`'s current translation and excluded rows are left alone."""
        return self.texts.twins(row)

    @slint.callback(global_name="App")
    def revert(self):
        row = self.current_row()
        if row is not None:
            self.ui.cur_translation = row.translation or ""
            self.ui.cur_error = row.error or ""

    @slint.callback(global_name="App")
    def toggle_exclude(self):
        row = self.current_row()
        if row is not None:
            self.ws.exclude(row, row.status != "excluded")
            self.row_changed(row)

    def show_builds(self):
        rows, self.build_reports = [], []
        for path in self.ws.builds():
            try:
                report = json.loads((path / "report.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                report = {}
            self.build_reports.append(report)
            try:
                date = f"{datetime.strptime(path.name[:15], '%Y%m%d-%H%M%S'):%Y-%m-%d  %H:%M:%S}"
            except ValueError:
                date = path.name
            options = []
            if report:
                options.append(
                    f"wrap at {report['wrap_columns']}"
                    if report.get("wrap_columns")
                    else "no wrap"
                )
                options.append(report.get("output_encoding") or "original encoding")
            rows.append(
                dict(
                    name=path.name,
                    date=date,
                    path=str(path / "patch"),
                    files=f"{report['files']:,}" if "files" in report else "—",
                    units=f"{report['units']:,}" if "units" in report else "—",
                    settings=" · ".join(options) or "—",
                    warnings=len(report.get("warnings", [])),
                )
            )
        self.ui.builds = slint.ListModel(rows)
        self.ui.build_summary = f"{len(rows)} build{'s' * (len(rows) != 1)}"
        self.build_select(0 if rows else -1)

    @slint.callback(global_name="App")
    def build_select(self, index):
        self.ui.build_current = index
        warnings = self.build_reports[index].get("warnings", []) if index >= 0 else []
        self.ui.build_warnings = slint.ListModel(warnings)

    @slint.callback(global_name="App")
    async def build(self):
        ui, ws = self.ui, self.ws
        encoding = list(self.Encodings.output)[ui.output_encoding]
        settings = dict(
            wrap_columns=ui.wrap_columns if ui.wrap else None,
            output_encoding=None if encoding == "Keep" else encoding,
        )
        ws.manifest["build"] = settings
        self.busy("Building")
        try:
            ws.save_manifest()
            target, report = await jobs.run(
                jobs.build,
                ws.root,
                **settings,
                progress=self.progress(),
                cancel=self.cancel,
            )
        except Cancelled:
            self.say("Build cancelled")
            return
        except (VnTransError, OSError) as exc:
            self.fail(f"Build failed: {exc}")
            return
        finally:
            self.idle()
        self.show_builds()
        self.say(
            f"Built {report.files:,} files"
            + (f" with {len(report.warnings):,} warnings" if report.warnings else "")
        )

    @slint.callback(global_name="App")
    def open_folder(self, path):
        try:
            if sys.platform == "win32":
                os.startfile(path)
            else:
                subprocess.Popen(
                    ["open" if sys.platform == "darwin" else "xdg-open", path]
                )
        except OSError as exc:
            self.fail(exc)

    def show_terms(self):
        query = self.ui.term_query.casefold()
        self.term_view = [
            term
            for term in self.terms
            if not query
            or any(
                query in value.casefold()
                for value in (term.source, term.target, term.notes)
            )
        ]
        self.ui.terms = slint.ListModel(
            dict(
                source=term.source,
                target=term.target,
                kind=term.kind,
                notes=oneline(term.notes),
                approved=term.approved,
            )
            for term in self.term_view
        )
        self.ui.term_total = len(self.terms)
        self.ui.term_current = next(
            (i for i, term in enumerate(self.term_view) if term is self.term), -1
        )

    @slint.callback(global_name="App")
    def term_query_edited(self, text):
        self.ui.term_query = text
        self.show_terms()

    @slint.callback(global_name="App")
    def term_select(self, index):
        term = self.term = self.term_view[index]
        ui = self.ui
        ui.term_current = index
        ui.t_source, ui.t_target, ui.t_notes = term.source, term.target, term.notes
        ui.t_kind = glossary.KINDS.index(term.kind)
        ui.t_case, ui.t_approved, ui.t_error = term.case_sensitive, term.approved, ""
        ui.t_evidence = "\n".join(oneline(line) for line in term.evidence)

    @slint.callback(global_name="App")
    def term_new(self):
        ui = self.ui
        self.term = None
        ui.term_current = -1
        ui.t_source = ui.t_target = ui.t_notes = ui.t_error = ui.t_evidence = ""
        ui.t_kind, ui.t_case, ui.t_approved = 0, False, True

    @slint.callback(global_name="App")
    def term_save(self):
        ui = self.ui
        term = self.term or glossary.Term("")
        values = (
            term.source,
            term.target,
            term.kind,
            term.notes,
            term.case_sensitive,
            term.approved,
        )
        term.source, term.target, term.notes = ui.t_source, ui.t_target, ui.t_notes
        term.kind, term.case_sensitive, term.approved = (
            glossary.KINDS[ui.t_kind],
            ui.t_case,
            ui.t_approved,
        )
        try:
            self.ws.save_term(term)
        except (VnTransError, OSError) as exc:
            (
                term.source,
                term.target,
                term.kind,
                term.notes,
                term.case_sensitive,
                term.approved,
            ) = values
            ui.t_error = str(exc)
            return
        if self.term is None:
            self.terms.append(term)
            self.term = term
        ui.t_error = ""
        self.show_terms()
        self.recheck()
        self.say("Saved")

    @slint.callback(global_name="App")
    def term_delete(self):
        if self.term is not None:
            self.ws.delete_term(self.term.id)
            self.terms.remove(self.term)
            self.term_new()
            self.show_terms()
            self.recheck()

    @slint.callback(global_name="App")
    async def term_import(self):
        path = await jobs.thread(pick, "Import glossary", False)
        if not path:
            return
        try:
            added, updated = self.ws.import_terms(
                glossary.loads(Path(path).read_text(encoding="utf-8"))
            )
        except (VnTransError, OSError, UnicodeError) as exc:
            self.fail(exc)
            return
        self.terms = self.ws.terms()
        self.term_new()
        self.show_terms()
        self.recheck()
        self.say(f"Imported: {added} new, {updated} updated")

    @slint.callback(global_name="App")
    def term_export(self):
        path = (
            self.ws.root / "exports" / f"glossary-{datetime.now():%Y%m%d-%H%M%S}.json"
        )
        try:
            path.parent.mkdir(exist_ok=True)
            path.write_text(glossary.dumps(self.terms), encoding="utf-8")
        except OSError as exc:
            self.fail(exc)
            return
        self.say(f"Exported to {path}")

    @slint.callback(global_name="App")
    async def lg_export(self):
        folder = (
            self.ws.root / "exports" / f"linguagacha-{datetime.now():%Y%m%d-%H%M%S}"
        )
        with self.overlay("Exporting LinguaGacha JSON…"):
            try:
                count = await jobs.thread(
                    self.ws.export_messages, folder, list(self.rows)
                )
            except OSError as exc:
                self.fail(exc)
                return
        self.say(f"Exported {count:,} scripts to {folder}")

    @slint.callback(global_name="App")
    async def lg_import(self):
        path = await jobs.thread(pick, "LinguaGacha output folder")
        if not path or self.ws is None or self.run:
            return
        with self.overlay("Importing LinguaGacha translations…"):
            try:
                pairs, skipped, ignored = await jobs.thread(
                    self.ws.import_messages, Path(path).expanduser(), self.rows
                )
            except OSError as exc:
                self.fail(exc)
                return
            rows = [row for row, _ in pairs]
            if (
                await self.edit(
                    "import",
                    lambda: [
                        replace(row, translation=text, status="machine", error=None)
                        for row, text in pairs
                    ],
                )
                is None
            ):
                return
        notes = [f"{skipped:,} failed checks"] * bool(skipped) + [
            f"{len(ignored):,} files not matched"
        ] * bool(ignored)
        self.say(
            f"Imported {len(rows):,} translations"
            + (f" ({', '.join(notes)})" if notes else ""),
            error=not rows and bool(notes),
        )

    def load_prompt(self):
        manifest = self.ws.manifest
        self.sections = prompts.chain(manifest.get("prompt", {}))
        sources, targets = list(self.Languages.sources), list(self.Languages.targets)
        languages = manifest["languages"]
        self.ui.p_source = (
            sources.index(languages["source"]) if languages["source"] in sources else 0
        )
        self.ui.p_target = (
            targets.index(languages["target"]) if languages["target"] in targets else 0
        )
        self.show_sections()
        self.section_select(0)

    def show_sections(self):
        self.ui.sections = slint.ListModel(
            dict(
                title=prompts.BY_KEY[item["key"]].title,
                enabled=item["enabled"],
                locked=item["key"] == "contract",
            )
            for item in self.sections
        )

    def store_prompt(self):
        self.ws.manifest["prompt"] = dict(
            sections=[item for item in self.sections if item["key"] != "contract"]
        )
        try:
            self.ws.save_manifest()
        except OSError as exc:
            self.fail(exc)

    @slint.callback(global_name="App")
    def section_select(self, index):
        ui = self.ui
        item = self.sections[index]
        key = item["key"]
        ui.section_current = index
        ui.section_key = key
        ui.section_editable = prompts.BY_KEY[key].editable
        ui.section_about = ABOUT.get(key, "")
        ui.section_text = (
            prompts.CONTRACT
            if key == "contract"
            else (
                item["text"]
                if item["text"] is not None
                else prompts.DEFAULTS.get(key, "")
            )
        )

    @slint.callback(global_name="App")
    def section_toggle(self, index):
        item = self.sections[index]
        if item["key"] != "contract":
            item["enabled"] = not item["enabled"]
            self.prompt_changed()

    @slint.callback(global_name="App")
    def section_move(self, index, delta):
        other = index + delta
        if 0 <= other < len(self.sections) - 1 and index < len(self.sections) - 1:
            self.sections[index], self.sections[other] = (
                self.sections[other],
                self.sections[index],
            )
            self.prompt_changed()
            self.section_select(other)

    @slint.callback(global_name="App")
    def section_edited(self, text):
        self.sections[self.ui.section_current]["text"] = text
        self.section_timer.start(
            slint.TimerMode.SingleShot, timedelta(milliseconds=400), self.prompt_changed
        )

    @slint.callback(global_name="App")
    def section_reset(self):
        self.sections[self.ui.section_current]["text"] = None
        self.section_select(self.ui.section_current)
        self.prompt_changed()

    @slint.callback(global_name="App")
    def style_preset(self, name):
        index = next(
            i for i, item in enumerate(self.sections) if item["key"] == "style"
        )
        self.sections[index]["text"] = prompts.STYLE_PRESETS[name]
        self.section_select(index)
        self.prompt_changed()

    def prompt_changed(self):
        self.store_prompt()
        self.show_sections()
        self.update_preview()
        if self.checker and self.checker.honorifics != prompts.keeps_honorifics(
            self.sections, self.ws.manifest["languages"]
        ):
            self.recheck()

    @slint.callback(global_name="App")
    def languages_changed(self):
        ui = self.ui
        self.ws.manifest["languages"] = dict(
            source=list(self.Languages.sources)[ui.p_source],
            target=list(self.Languages.targets)[ui.p_target],
        )
        self.store_prompt()
        self.show_info()
        self.update_preview()
        self.recheck()

    def batch_settings(self):
        return prompts.BATCH | {
            key: value
            for key, value in self.ws.manifest.get("translation", {}).items()
            if key in prompts.BATCH
        }

    def update_preview(self):
        ui = self.ui
        settings = self.batch_settings()
        try:
            rules = glossary.Rules.parse(self.ws.manifest.get("replacements", ""))
        except VnTransError as exc:
            ui.preview_system = ui.preview_user = ""
            ui.preview_stats = f"Replacement rules: {exc}"
            return
        selected = (
            self.model.indices[ui.current]
            if 0 <= ui.current < len(self.model.indices)
            else 0
        )
        candidates = list(
            islice(
                (
                    i
                    for i in range(selected, len(self.rows))
                    if self.rows[i].kind != "speaker"
                ),
                settings["batch_lines"],
            )
        )
        if not candidates:
            ui.preview_system = ui.preview_user = ui.preview_stats = ""
            return
        batch = prompts.plan(
            self.rows,
            candidates,
            lines=settings["batch_lines"],
            tokens=settings["batch_tokens"],
        )[0]
        prompt = prompts.build(
            self.sections,
            self.ws.manifest["languages"],
            self.rows,
            batch,
            terms=self.terms,
            before=settings["context_before"],
            after=settings["context_after"],
            pre=rules.pre,
            formats=self.ws.formats,
        )
        ui.preview_system, ui.preview_user = prompt.system, prompt.user
        ui.preview_stats = (
            f"~{prompt.tokens:,} tokens (estimate) · {len(prompt.rows)} records from "
            f"{prompt.rows[0].file} · {len(prompt.terms)} glossary hits · prompt {prompt.version}"
        )

    def show_profiles(self):
        self.ui.profiles = slint.ListModel(
            dict(
                name=profile.name,
                detail=f"{PROVIDERS[profile.provider].label} · {profile.model or 'no model'}",
            )
            for profile in self.profiles
        )
        self.ui.profile_names = slint.ListModel(
            profile.name for profile in self.profiles
        )
        self.show_parallel()

    def form_profile(self) -> Profile:
        ui = self.ui
        return Profile(
            ui.pf_name.strip(),
            list(PROVIDERS)[ui.pf_provider],
            ui.pf_model.strip(),
            [key.strip() for key in re.split(r"[,\s]+", ui.pf_keys) if key.strip()],
            round(ui.pf_temperature, 2),
            ui.pf_max_tokens,
            REASONING[ui.pf_reasoning],
            ui.pf_timeout,
            ui.pf_thinking,
        )

    @slint.callback(global_name="App")
    def profile_select(self, index):
        profile = self.profiles[index]
        ui = self.ui
        ui.profile_current = index
        ui.pf_name, ui.pf_model, ui.pf_keys = (
            profile.name,
            profile.model,
            "\n".join(profile.keys),
        )
        ui.pf_provider = list(PROVIDERS).index(profile.provider)
        ui.pf_temperature, ui.pf_max_tokens, ui.pf_timeout = (
            profile.temperature,
            profile.max_output_tokens,
            profile.timeout,
        )
        ui.pf_reasoning = REASONING.index(profile.reasoning)
        ui.pf_thinking = profile.show_thinking
        ui.pf_models = slint.ListModel([])
        ui.pf_status = ""

    @slint.callback(global_name="App")
    def profile_new(self):
        ui = self.ui
        defaults = Profile("")
        ui.profile_current = -1
        ui.pf_name = ui.pf_model = ui.pf_keys = ui.pf_status = ""
        ui.pf_provider = 0
        ui.pf_temperature, ui.pf_max_tokens, ui.pf_timeout = (
            defaults.temperature,
            defaults.max_output_tokens,
            defaults.timeout,
        )
        ui.pf_reasoning = 0
        ui.pf_thinking = defaults.show_thinking
        ui.pf_models = slint.ListModel([])

    def profile_status(self, text, error=False):
        self.ui.pf_status = text
        self.ui.pf_status_error = error

    @slint.callback(global_name="App")
    def profile_save(self):
        profile = self.form_profile()
        current = self.ui.profile_current
        if any(
            other.name == profile.name
            for i, other in enumerate(self.profiles)
            if i != current
        ):
            self.profile_status("A profile with this name exists", error=True)
            return
        if current >= 0:
            self.profiles[current] = profile
        else:
            self.profiles.append(profile)
            current = len(self.profiles) - 1
        self.save_config()
        self.show_profiles()
        self.profile_select(current)
        self.profile_status("Saved")

    @slint.callback(global_name="App")
    def profile_delete(self):
        if self.ui.profile_current >= 0:
            del self.profiles[self.ui.profile_current]
            self.save_config()
            self.show_profiles()
            self.profile_new()

    @slint.callback(global_name="App")
    async def profile_fetch(self):
        profile = self.form_profile()
        self.ui.pf_working = "fetch"
        try:
            models = await jobs.thread(
                PROVIDERS[profile.provider].models, profile, profile.api_keys()[0]
            )
        except (ProviderError, IndexError) as exc:
            self.profile_status(str(exc) or "No API key", error=True)
            return
        finally:
            self.ui.pf_working = ""
        self.ui.pf_models = slint.ListModel(models)
        self.ui.pf_model_index = (
            models.index(self.ui.pf_model) if self.ui.pf_model in models else -1
        )
        self.profile_status(f"{len(models)} models")

    @slint.callback(global_name="App")
    async def profile_test(self):
        profile = self.form_profile()
        user = 'Translate these 1 records into English:\n{"i": 1, "kind": "dialogue", "text": "おはよう、いい天気だね。"}'
        self.ui.pf_working = "test"
        self.profile_status("")
        started = time.monotonic()
        try:
            reply = await jobs.thread(
                PROVIDERS[profile.provider].complete,
                profile,
                profile.api_keys()[0],
                prompts.CONTRACT,
                user,
            )
            text = runner.decode(reply.text)[0].get(1, "")
        except (ProviderError, ValueError, IndexError) as exc:
            self.profile_status(str(exc) or "No API key", error=True)
            return
        finally:
            self.ui.pf_working = ""
        self.profile_status(f"OK in {time.monotonic() - started:.1f} s: {text}")

    def load_run_settings(self):
        ui = self.ui
        settings = runner.SETTINGS | self.ws.manifest.get("translation", {})
        names = [profile.name for profile in self.profiles]
        ui.run_profile = (
            names.index(settings.get("profile"))
            if settings.get("profile") in names
            else 0
        )
        ui.run_lines, ui.run_tokens = settings["batch_lines"], settings["batch_tokens"]
        ui.run_before, ui.run_after = (
            settings["context_before"],
            settings["context_after"],
        )
        ui.run_concurrency, ui.run_rpm = settings["concurrency"], settings["rpm"]
        self.parallel = set(settings.get("parallel", []))
        self.show_parallel()

    def helpers(self) -> list:
        """Profiles chosen to run alongside the main one, in profile order."""
        main = (
            self.profiles[self.ui.run_profile].name
            if 0 <= self.ui.run_profile < len(self.profiles)
            else None
        )
        return [
            profile
            for profile in self.profiles
            if profile.name in self.parallel and profile.name != main
        ]

    def show_parallel(self):
        main = (
            self.profiles[self.ui.run_profile].name
            if 0 <= self.ui.run_profile < len(self.profiles)
            else None
        )
        self.ui.run_parallel = slint.ListModel(
            dict(name=profile.name, on=profile.name in self.parallel)
            for profile in self.profiles
            if profile.name != main
        )

    @slint.callback(global_name="App")
    def run_parallel_toggled(self, name):
        if self.run or self.ws is None:
            return
        self.parallel ^= {name}
        self.run_settings_changed()

    def run_settings(self) -> dict:
        ui = self.ui
        return dict(
            batch_lines=ui.run_lines,
            batch_tokens=ui.run_tokens,
            context_before=ui.run_before,
            context_after=ui.run_after,
            concurrency=ui.run_concurrency,
            rpm=ui.run_rpm,
        )

    @slint.callback(global_name="App")
    def run_settings_changed(self):
        settings = self.run_settings()
        if 0 <= self.ui.run_profile < len(self.profiles):
            settings["profile"] = self.profiles[self.ui.run_profile].name
        settings["parallel"] = [profile.name for profile in self.helpers()]
        self.show_parallel()
        self.ws.manifest["translation"] = settings
        try:
            self.ws.save_manifest()
        except OSError as exc:
            self.fail(exc)

    def log_event(self, kind, text):
        self.log.insert(
            0, dict(time=f"{datetime.now():%H:%M:%S}", kind=kind, text=text)
        )
        if len(self.log) > 500:
            del self.log[500]

    def row_committed(self, index):
        self.texts.changed(index)
        self.retag(index)
        position = self.positions.get(index)
        if position is not None:
            self.model.notify_row_changed(position)
            if position == self.ui.current:
                row = self.rows[index]
                self.ui.cur_state = state(row)
                self.ui.cur_error = row.error or ""
                if not self.ui.cur_translation and row.translation:
                    self.ui.cur_translation = row.translation
                self.show_marks(row)

    def show_run(self):
        ui, stats = self.ui, self.run.stats
        ui.run_total, ui.run_done, ui.run_failed = stats.total, stats.done, stats.failed
        elapsed = time.monotonic() - stats.started
        finished = stats.done + stats.failed
        left = (
            elapsed / finished * (stats.total - finished)
            if finished and ui.running
            else None
        )
        ui.run_meta = (
            " + ".join(worker.label for worker in self.run.workers)
            + f" · {duration(elapsed)} elapsed"
        )
        ui.m_percent = f"{100 * finished / stats.total:.0f}%" if stats.total else "—"
        ui.m_progress = finished / stats.total if stats.total else 0
        ui.m_progress_detail = (
            f"{stats.done:,} done · {stats.failed:,} failed · {stats.total:,} total"
        )
        ui.m_eta = (
            f"~{duration(left)} left"
            if left is not None
            else ""
            if ui.running
            else f"took {duration(elapsed)}"
        )
        rate = stats.rate() if ui.running else 0.0
        self.rate_samples.append(rate)
        ui.m_rate = f"{rate:,.0f}"
        ui.m_spark = slint.ListModel(bars(self.rate_samples))
        ui.m_rate_detail = (
            f"peak {max(self.rate_samples):,.0f} · {stats.latency / stats.replies:.1f} s avg reply"
            if stats.replies
            else "waiting for the first reply"
            if ui.running
            else "no replies"
        )
        ui.m_active = stats.active
        busy = [f"{stats.queued:,} queued"] + (
            [f"{stats.cooling} cooling down"] if stats.cooling else []
        )
        ui.m_active_detail = " · ".join(busy) if ui.running else "idle"
        ui.m_usage = f"{compact(stats.tokens_in)} / {compact(stats.tokens_out)}"
        ui.m_usage_detail = f"{stats.requests:,} requests · {stats.replies:,} replies"
        self.update_stats()

    def batch_done(self, batch):
        if batch.task == "review":
            self.suggestions = self.ws.suggestions()
            if self.ui.page == "review":
                self.show_review()
        self.batch_views.insert(0, batch)
        del self.batch_views[200:]
        accepted = sum(record[3] in ("ok", "changed") for record in batch.records)
        failed = sum(record[3] == "failed" for record in batch.records)
        outcome = (
            "failed"
            if batch.error or (failed and not accepted)
            else "partial"
            if failed
            else "ok"
        )
        rate = batch.tokens_out / batch.latency if batch.latency else 0
        detail = (
            batch.error
            or f"{len(batch.records)} lines · {batch.latency:.1f} s · {rate:,.0f} tok/s"
        )
        if self.run and len(self.run.workers) > 1:
            detail = f"{batch.worker.split(' · ')[0]} · {detail}"
        self.batches.insert(
            0,
            dict(
                number=batch.number,
                outcome=outcome,
                detail=detail,
                title=f"#{batch.number} · {Path(batch.file).name}"
                + (f" · retry {batch.attempt}" if batch.attempt else ""),
            ),
        )
        if len(self.batches) > 200:
            del self.batches[200]
        if self.ui.batch_follow or self.ui.batch_current < 0:
            self.batch_select(0)
        elif self.ui.batch_current + 1 < len(self.batch_views):
            self.ui.batch_current += 1

    @slint.callback(global_name="App")
    def batch_select(self, index):
        if not 0 <= index < len(self.batch_views):
            return
        ui, batch = self.ui, self.batch_views[index]
        ui.batch_current = index
        task = dict(
            translate="Translation", review="Review", discover="Term discovery"
        )[batch.task]
        meta = [
            task,
            batch.worker,
            f"{len(batch.records)} {'terms' if batch.task == 'discover' else 'lines'}",
        ]
        if batch.latency:
            meta += [
                f"{batch.latency:.1f} s",
                f"{batch.tokens_out / batch.latency:,.0f} tok/s",
                f"{batch.tokens_in:,} in / {batch.tokens_out:,} out",
            ]
        if batch.context:
            meta.append(f"{batch.context} context lines")
        if batch.attempt:
            meta.append(f"retry {batch.attempt}")
        ui.insp_title = f"Batch {batch.number} · {batch.file}"
        ui.insp_meta = " · ".join(meta)
        ui.insp_error = batch.error
        ui.insp_records = slint.ListModel(
            dict(
                speaker=speaker,
                source=source,
                output=output,
                outcome=outcome,
                note=note,
            )
            for speaker, source, output, outcome, note in batch.records
        )
        ui.insp_thinking = batch.thinking
        outcomes = Counter(record[3] for record in batch.records)
        checks = {
            "translate": [f"{outcomes['ok']} accepted and saved"],
            "review": [
                f"{outcomes['changed']} suggestions",
                f"{outcomes['ok']} left as they are",
            ],
            "discover": [
                f"{outcomes['ok']} new draft terms",
                f"{outcomes['skipped']} skipped",
            ],
        }[batch.task]
        checks += [
            f"{count} failed: {reason}"
            for reason, count in Counter(
                record[4] for record in batch.records if record[3] == "failed"
            ).most_common()
        ]
        if batch.task != "discover" and not batch.thinking and not batch.error:
            checks.append("The model returned no thinking")
        ui.insp_checks = slint.ListModel(checks)
        ui.insp_terms = slint.ListModel(batch.terms)

    @slint.callback(global_name="App")
    async def run_start(self):
        ui = self.ui
        task = TASKS[ui.run_task]
        pool = range(len(self.rows)) if ui.run_scope == 0 else self.model.indices
        if ui.run_scope != 0 and KINDS[ui.kind_index] == "lines" and not ui.regex_error:
            pool = filter_rows(self.rows, **self.filters() | dict(kind=""))[0]
        if task == "translate":
            wanted = ("new",) if ui.run_scope == 0 else ("new", "machine")
            indices = [i for i in pool if self.rows[i].status in wanted]
        else:
            indices = list(pool)
        await self.start_run(task, indices)

    async def start_run(self, task, indices):
        ui = self.ui
        self.run_settings_changed()
        self.forget_undo()
        try:
            rules = glossary.Rules.parse(self.ws.manifest.get("replacements", ""))
        except VnTransError as exc:
            self.log_event("error", f"Replacement rules, {exc}")
            return
        try:
            profile = self.profiles[ui.run_profile]
            self.run = runner.Run(
                self.ws,
                self.rows,
                indices,
                profile=profile,
                provider=PROVIDERS[profile.provider],
                sections=self.sections,
                terms=self.terms,
                settings=self.run_settings(),
                task=task,
                rules=rules,
                on_event=self.log_event,
                on_commit=self.row_committed,
                on_batch=self.batch_done,
                offload=jobs.thread,
                parallel=[
                    (other, PROVIDERS[other.provider]) for other in self.helpers()
                ],
            )
        except (VnTransError, IndexError) as exc:
            self.log_event("error", str(exc) or "Choose a profile")
            return
        ui.running = True
        ui.stopping = False
        ui.run_kind = task
        self.batch_views.clear()
        self.batches = slint.ListModel([])
        ui.batches = self.batches
        ui.batch_current = -1
        ui.batch_follow = True
        self.rate_samples.clear()
        ui.m_slots = self.run.slots
        self.log_event(
            "info",
            "Started with "
            + ", ".join(
                f"{worker.profile.name} ({worker.profile.model})"
                for worker in self.run.workers
            ),
        )
        self.run_timer.start(
            slint.TimerMode.Repeated, timedelta(milliseconds=500), self.show_run
        )
        try:
            await self.run.run()
        except (
            Exception
        ) as exc:  # keep the window alive; accepted work is already committed
            self.log_event("error", f"Run failed: {exc!r}")
        finally:
            self.run_timer.stop()
            ui.running = ui.stopping = False
            self.show_run()
            self.run = None
            self.terms = self.ws.terms()
            self.suggestions = self.ws.suggestions()
            self.reviews = self.ws.reviews()
            ui.suggestion_count = len(self.suggestions)
            self.show_terms()
            self.recheck()
            if ui.page == "review":
                self.show_review()

    @slint.callback(global_name="App")
    def run_stop(self):
        if self.run:
            self.run.stop()
            self.ui.stopping = True
            self.log_event("info", "Stopping after requests in flight")

    @slint.callback(global_name="App")
    def accept_suggestion(self):
        row = self.current_row()
        if row is None or (row.file, row.id) not in self.suggestions:
            return
        error = self.settle(row, "accepted", self.suggestions[(row.file, row.id)][0])
        if error:
            self.ui.cur_error = error
        else:
            self.next_suggestion()

    @slint.callback(global_name="App")
    def dismiss_suggestion(self):
        row = self.current_row()
        if row is not None and (row.file, row.id) in self.suggestions:
            self.settle(row, "rejected")
            self.next_suggestion()

    def settle(self, row, verdict, text=None) -> str:
        """Resolve the row's suggestion with `verdict`, saving `text` first when given. Returns an error message."""
        key = (row.file, row.id)
        try:
            if text is not None:
                self.ws.save(row, text)
            self.ws.dismiss([key], verdict)
        except VnTransError as exc:
            return str(exc).removeprefix(f"{row.id}: ")
        self.suggestions.pop(key, None)
        if row.translation is not None:
            self.reviews[key] = (row.translation, verdict)
        self.row_committed(self.index_of[key])
        if self.current_row() is row:
            self.ui.cur_translation = row.translation or ""
        self.update_stats()
        self.refresh_counts()
        if self.ui.page == "review":
            self.show_review()
        return ""

    def next_suggestion(self):
        """Select the next listed row with a suggestion, wrapping around; stay on the current row when none is left."""
        indices = self.model.indices
        for step in range(1, len(indices) + 1):
            position = (self.ui.current + step) % len(indices)
            row = self.rows[indices[position]]
            if (row.file, row.id) in self.suggestions:
                self.select(position)
                return
        self.select(self.ui.current)

    @slint.callback(global_name="App")
    async def suggestions_action(self, accept):
        await self.resolve(list(self.suggestions), accept)

    async def resolve(self, keys, accept):
        """Accept or reject the suggestions of `keys` together, remembering the rows as reviewed."""
        if not self.ws or self.run or self.editing:
            return
        ws, texts = self.ws, self.texts
        suggestions = {
            key: self.suggestions[key] for key in keys if key in self.suggestions
        }
        if not suggestions:
            return
        if accept:

            def prepare():
                entries = []
                for key, (text, _) in suggestions.items():
                    if key not in texts.index_of:
                        continue
                    row = texts.views[texts.index_of[key]]
                    entries.append(
                        replace(
                            row,
                            translation=text,
                            error=None,
                            status="excluded" if row.status == "excluded" else "edited",
                        )
                    )
                return entries

            entries = await self.edit("suggestions", prepare, dismiss="accepted")
            if entries is not None:
                self.say(f"Accepted {len(entries):,} suggestions")
                if self.ui.page == "review":
                    self.show_review()
            return

        def discard():
            with closing(workspace.Workspace(ws.root)) as worker:
                worker.dismiss(list(suggestions), "rejected")

        self.editing = True
        self.last_action = time.monotonic()
        try:
            with self.overlay("Rejecting suggestions…"):
                await jobs.thread(discard)
                if ws is not self.ws:
                    return
                for key in suggestions:
                    self.suggestions.pop(key, None)
                    index = self.index_of.get(key)
                    if index is not None and self.rows[index].translation is not None:
                        self.reviews[key] = (self.rows[index].translation, "rejected")
                self.ui.suggestion_count = len(self.suggestions)
                if self.ui.page == "review":
                    self.show_review()
                self.recheck()
                self.select(self.ui.current)
                self.show_selection()
                self.say(f"Rejected {len(suggestions):,} suggestions")
        except (VnTransError, OSError, sqlite3.Error) as exc:
            if ws is self.ws:
                self.fail(exc)
        finally:
            self.editing = False
            self.last_action = time.monotonic()

    # Review page
    def show_review(self):
        """Rebuild the Review page: file progress, category filters and the suggestion queue."""
        if self.ws is None:
            return
        ui = self.ui
        files = review_files(self.rows, self.reviews, self.suggestions, self.file_names)
        overall = {
            field: sum(file[field] for file in files)
            for field in ("total", "done", "pending")
        }
        ui.rv_files = slint.ListModel([dict(name="All files", **overall), *files])
        if self.review_file_name not in self.file_names:
            self.review_file_name = None
        ui.rv_file = (
            self.file_names.index(self.review_file_name) + 1
            if self.review_file_name
            else 0
        )
        scope = files[ui.rv_file - 1] if self.review_file_name else overall
        ui.rv_summary = (
            f"{scope['done']:,} of {scope['total']:,} lines reviewed"
            f" · {scope['pending']:,} suggestion{'s' * (scope['pending'] != 1)} waiting"
        )
        ui.rv_targets = len(
            review_targets(
                self.rows,
                self.reviews,
                self.suggestions,
                self.review_file_name,
                ui.rv_scope == 1,
            )
        )
        queue = review_queue(
            self.rows, self.suggestions, self.index_of, self.review_file_name
        )
        labels = {
            index: category(self.suggestions[self.key_of(index)][1]) for index in queue
        }
        counts = Counter(labels.values())
        self.review_labels = [
            "All",
            *(name for name in REVIEW_CATEGORIES if counts[name]),
        ]
        if self.review_label not in self.review_labels:
            self.review_label = "All"
        ui.rv_categories = slint.ListModel(
            dict(label=name, count=len(queue) if name == "All" else counts[name])
            for name in self.review_labels
        )
        ui.rv_category = self.review_labels.index(self.review_label)
        if self.review_label != "All":
            queue = [index for index in queue if labels[index] == self.review_label]
        selected = (
            self.review_keys[ui.rv_current]
            if 0 <= ui.rv_current < len(self.review_keys)
            else None
        )
        self.review_keys = [self.key_of(index) for index in queue]
        ui.rv_items = slint.ListModel(
            dict(
                category=labels[index],
                speaker=self.rows[index].speaker,
                translation=self.rows[index].translation or "",
            )
            for index in queue
        )
        self.review_select(
            self.review_keys.index(selected)
            if selected in self.review_keys
            else min(max(ui.rv_current, 0), len(self.review_keys) - 1)
        )

    def key_of(self, index):
        return self.rows[index].file, self.rows[index].id

    def review_row(self):
        position = self.ui.rv_current
        if 0 <= position < len(self.review_keys):
            key = self.review_keys[position]
            if key in self.suggestions:
                return self.rows[self.index_of[key]]
        return None

    @slint.callback(global_name="App")
    def review_select(self, position):
        ui = self.ui
        ui.rv_current = position if 0 <= position < len(self.review_keys) else -1
        ui.rv_editing = False
        ui.rv_error = ""
        row = self.review_row()
        if row is None:
            return
        index = self.index_of[(row.file, row.id)]
        text, reason = self.suggestions[(row.file, row.id)]
        ui.rv_source = row.source
        ui.rv_speaker = row.speaker if row.kind != "speaker" else ""
        ui.rv_meta = f"{row.file} · Unit {index + 1:,} · {row.kind}"
        ui.rv_translation = row.translation or ""
        ui.rv_suggestion = ui.rv_text = text
        ui.rv_label = category(reason)
        ui.rv_reason = (
            reason if ui.rv_label == "Other" else reason.split(": ", 1)[1].strip()
        )
        ui.rv_diff = styled_diff(row.translation or "", text)
        before, after = prompts.neighbours(self.rows, [index], 2, 2)
        ui.rv_before = slint.ListModel(context_row(other) for other in before)
        ui.rv_after = slint.ListModel(context_row(other) for other in after)

    @slint.callback(global_name="App")
    def review_file(self, position):
        self.review_file_name = self.file_names[position - 1] if position > 0 else None
        self.review_label, self.review_keys = "All", []
        self.ui.rv_current = 0
        self.show_review()

    @slint.callback(global_name="App")
    def review_category(self, position):
        if 0 <= position < len(self.review_labels):
            self.review_label, self.review_keys = self.review_labels[position], []
            self.ui.rv_current = 0
            self.show_review()

    @slint.callback(global_name="App")
    def review_scope_changed(self):
        self.show_review()

    @slint.callback(global_name="App")
    def review_accept(self, text):
        row = self.review_row()
        if row is None or self.editing:
            return
        suggestion = self.suggestions[(row.file, row.id)][0]
        error = self.settle(row, "accepted" if text == suggestion else "edited", text)
        if error:
            self.ui.rv_error = error

    @slint.callback(global_name="App")
    def review_reject(self):
        row = self.review_row()
        if row is not None and not self.editing:
            self.settle(row, "rejected")

    @slint.callback(global_name="App")
    async def review_bulk(self, accept):
        await self.resolve(list(self.review_keys), accept)

    @slint.callback(global_name="App")
    async def review_start(self):
        if self.run or self.ws is None:
            return
        indices = review_targets(
            self.rows,
            self.reviews,
            self.suggestions,
            self.review_file_name,
            self.ui.rv_scope == 1,
        )
        if not indices:
            self.say(f"Nothing left to review in {self.review_file_name or 'any file'}")
            return
        await self.start_run("review", indices)

    @slint.callback(global_name="App")
    async def replace_all(self):
        ui = self.ui
        try:
            pattern = re.compile(
                ui.query if ui.regex else re.escape(ui.query),
                0 if ui.match_case else re.IGNORECASE,
            )
            replace_with = ui.replace_with
            replacement = replace_with if ui.regex else (lambda match: replace_with)
        except re.error as exc:
            self.fail(f"Replace: {exc}")
            return
        rows = [self.texts.views[index] for index in self.model.indices]
        ws = self.ws
        skipped = 0

        def prepare():
            nonlocal skipped
            entries = []
            for row in rows:
                if row.translation is None:
                    continue
                text = pattern.sub(replacement, row.translation)
                if text == row.translation:
                    continue
                try:
                    entries.append(
                        replace(
                            row,
                            translation=ws.check(row, text),
                            status="edited",
                            error=None,
                        )
                    )
                except VnTransError:
                    skipped += 1
            return entries

        entries = await self.edit("replace", prepare)
        if entries is not None:
            self.say(
                f"Replaced in {len(entries)} units"
                + (f", {skipped} skipped (checks failed)" if skipped else "")
            )

    @slint.callback(global_name="App")
    def rules_edited(self, text):
        self.ui.rules_text = text
        self.rules_timer.start(
            slint.TimerMode.SingleShot, timedelta(milliseconds=400), self.store_rules
        )

    def check_rules(self):
        try:
            glossary.Rules.parse(self.ui.rules_text)
            self.ui.rules_error = ""
        except VnTransError as exc:
            self.ui.rules_error = str(exc)

    def store_rules(self):
        self.check_rules()
        self.ws.manifest["replacements"] = self.ui.rules_text
        try:
            self.ws.save_manifest()
        except OSError as exc:
            self.fail(exc)

    def targets(self) -> list[int]:
        """Row indices an action applies to: the multi-selection in list order, else the current row."""
        if self.picked:
            return [index for index in self.model.indices if index in self.picked]
        row = self.current_row()
        return [self.model.indices[self.ui.current]] if row is not None else []

    def repaint(self, indices):
        if len(indices) > 250:
            self.model.reset(self.rows, self.model.indices)
            return
        for index in indices:
            position = self.positions.get(index)
            if position is not None:
                self.model.notify_row_changed(position)

    def show_selection(self):
        rows = [self.rows[index] for index in self.targets()]
        ui = self.ui
        ui.sel_count = len(rows)
        ui.sel_has_translation = any(row.translation is not None for row in rows)
        ui.sel_has_suggestion = any(
            (row.file, row.id) in self.suggestions for row in rows
        )
        ui.sel_all_excluded = bool(rows) and all(
            row.status == "excluded" for row in rows
        )
        ui.sel_fixable = any(
            self.tags.get(index, set()) & {"speaker", "ruby", "quote"}
            for index in self.targets()
        )

    @slint.callback(global_name="App")
    def row_pressed(self, position, toggle, extend):
        self.last_action = time.monotonic()
        indices = self.model.indices
        if not 0 <= position < len(indices):
            return
        before = set(self.picked)
        current = self.ui.current
        if extend:
            anchor = self.anchor if self.anchor is not None else max(current, 0)
            low, high = sorted((anchor, position))
            if not toggle:
                self.picked.clear()
            self.picked.update(indices[low : high + 1])
            self.anchor = anchor
        elif toggle:
            if not self.picked and 0 <= current < len(indices):
                self.picked.add(indices[current])
            self.picked ^= {indices[position]}
            self.anchor = position
        else:
            self.picked.clear()
            self.anchor = position
        if len(self.picked) == 1 and indices[position] in self.picked:
            self.picked.clear()
        self.repaint(before ^ self.picked)
        self.select(position)
        self.show_selection()

    @slint.callback(global_name="App")
    def row_menu(self, position):
        if self.model.indices[position] not in self.picked:
            self.row_pressed(position, False, False)
        self.show_selection()

    @slint.callback(global_name="App")
    def select_all(self):
        self.picked.update(self.model.indices)
        self.repaint(self.model.indices)
        self.show_selection()

    @slint.callback(global_name="App")
    def clear_selection(self):
        if self.picked:
            before = set(self.picked)
            self.picked.clear()
            self.repaint(before)
            self.show_selection()

    def forget_undo(self):
        self.undo_entry = None
        self.ui.can_undo = False

    async def edit(self, label, prepare, *, check=True, dismiss=None):
        """Validate and commit detached rows, then apply them on the UI thread after the transaction.

        `dismiss` is the review verdict that resolves the rows' suggestions, if any."""
        if self.editing:
            return None
        self.editing = True
        self.last_action = time.monotonic()
        ws, texts, run = self.ws, self.texts, self.run
        if run:
            # Replies may finish during an edit; accept them after the manual changes are visible.
            run.writes_ready.clear()
        before = texts.snapshot()

        def store():
            return workspace.store_rows(
                ws.root, prepare(), check=check, dismiss=dismiss
            )

        try:
            with self.overlay("Saving changes…"):
                entries = await jobs.thread(store)
                if ws is not self.ws:
                    return None
                undo = []
                for start in range(0, len(entries), 250):
                    for entry in entries[start : start + 250]:
                        index = self.index_of[(entry.file, entry.id)]
                        row, old = self.rows[index], before[index]
                        undo.append((row, old.translation, old.status, old.error))
                        row.translation, row.status, row.error = (
                            entry.translation,
                            entry.status,
                            entry.error,
                        )
                        if (
                            dismiss
                            and self.suggestions.pop((row.file, row.id), None)
                            and row.translation is not None
                        ):
                            self.reviews[(row.file, row.id)] = (
                                row.translation,
                                dismiss,
                            )
                        texts.changed(index, entry)
                        position = self.positions.get(index)
                        if position is not None:
                            self.model.notify_row_changed(position)
                    await asyncio.sleep(0.001)
                if label:
                    self.undo_entry = label, undo
                    self.ui.can_undo, self.ui.undo_text = True, f"Undo {label}"
                else:
                    self.forget_undo()
                self.update_stats()
                self.refresh_counts()
                self.recheck()
                self.select(self.ui.current)
                self.show_selection()
                return entries
        except (VnTransError, OSError, sqlite3.Error, re.error) as exc:
            self.fail(exc)
            return None
        finally:
            self.editing = False
            self.last_action = time.monotonic()
            if run:
                run.writes_ready.set()

    @slint.callback(global_name="App")
    async def undo(self):
        if self.undo_entry is None:
            return
        label, snapshot = self.undo_entry
        if (
            await self.edit(
                None,
                lambda: [
                    replace(row, translation=text, status=status, error=error)
                    for row, text, status, error in snapshot
                ],
                check=False,
            )
            is not None
        ):
            self.say(f"Undid {label}")

    @slint.callback(global_name="App")
    async def menu_action(self, action):
        indices = self.targets()
        rows = [self.rows[index] for index in indices]
        if not rows:
            return
        count = f"{len(rows):,} unit{'s' * (len(rows) != 1)}"
        if action == "open":
            self.ui.editor_open = True
        elif action in ("copy-source", "copy-translation"):
            texts = [
                row.source if action == "copy-source" else row.translation or ""
                for row in rows
            ]
            self.copy_text("\n".join(texts))
            self.say(f"Copied {count}")
        elif action == "clear":
            rows = [row for row in rows if row.translation is not None]
            if (
                await self.edit(
                    "clear",
                    lambda: [
                        replace(row, translation=None, status="new", error=None)
                        for row in rows
                    ],
                )
                is None
            ):
                return
            self.say(f"Cleared {len(rows):,} translation{'s' * (len(rows) != 1)}")
        elif action == "exclude":
            excluded = not all(row.status == "excluded" for row in rows)
            if (
                await self.edit(
                    "exclude" if excluded else "include",
                    lambda: [
                        replace(
                            row,
                            status="excluded"
                            if excluded
                            else ("new" if row.translation is None else "edited"),
                        )
                        for row in rows
                    ],
                    check=False,
                )
                is None
            ):
                return
            self.say(f"{'Excluded' if excluded else 'Included'} {count}")
        elif action == "tidy":
            names = (
                self.checker.names
                if self.checker
                else quality.speaker_names(self.rows, self.terms)
            )
            formats = self.ws.formats

            def prepare():
                return [
                    replace(row, translation=text, error=None)
                    for row in rows
                    if row.translation is not None
                    and (text := quality.tidy(row.translation, row, names, formats))
                    != row.translation
                ]

            entries = await self.edit("fixes", prepare)
            if entries is None:
                return
            self.say(f"Fixed {len(entries):,} translation{'s' * (len(entries) != 1)}")
        elif action == "suggest":
            pairs = [
                (row, self.suggestions[(row.file, row.id)][0])
                for row in rows
                if (row.file, row.id) in self.suggestions
            ]
            if (
                await self.edit(
                    "suggestions",
                    lambda: [
                        replace(row, translation=text, status="edited", error=None)
                        for row, text in pairs
                    ],
                    dismiss="accepted",
                )
                is None
            ):
                return
            self.say(f"Applied {len(pairs):,} suggestion{'s' * (len(pairs) != 1)}")
        elif action in ("translate", "review"):
            if action == "translate":
                chosen = [
                    i for i in indices if self.rows[i].status in ("new", "machine")
                ]
                skipped = len(indices) - len(chosen)
            else:
                chosen = [
                    i
                    for i in indices
                    if self.rows[i].translation is not None
                    and self.rows[i].status != "excluded"
                ]
                skipped = 0
            if action == "translate":
                self.ui.run_task = TASKS.index(action)
            self.navigate(action)
            if skipped:
                self.log_event(
                    "info",
                    f"Skipped {skipped:,} edited or excluded units; manual edits are never overwritten",
                )
            asyncio.ensure_future(self.start_run(action, chosen))
