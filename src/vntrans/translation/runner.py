"""Batch translation run: plan, prompt, request, decode, validate, checkpoint.

Runs on one asyncio loop; only provider calls leave it (asyncio.to_thread), so workspace
writes are never concurrent. Accepted units are committed per batch, so stopping or
crashing keeps paid work, and the next run picks up the remaining `new` units."""

import asyncio
from collections import deque
from dataclasses import dataclass, field
import json
import re
import time

from ..model import VnTransError
from ..text import MARKER
from . import prompts, quality
from .glossary import Rules, Term
from .providers import REVIEW_SCHEMA, SCHEMA, TERMS_SCHEMA, THINKING_SCHEMA, ProviderError

SETTINGS = prompts.BATCH | dict(concurrency=4, rpm=0)
TRANSPORT_TRIES = 5
FENCE = re.compile(r"^```[\w-]*\s*|\s*```$")


def parse(text: str):
    """JSON from a reply, tolerating code fences and stray prose around it. Raises ValueError."""
    text = FENCE.sub("", text.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
        end = max(text.rfind("}"), text.rfind("]"))
        if not starts or end <= min(starts):
            raise ValueError("reply is not JSON") from None
        return json.loads(text[min(starts):end + 1])


def listed(text: str, key: str) -> list[dict]:
    value = parse(text)
    if isinstance(value, dict):
        value = value.get(key)
    if not isinstance(value, list):
        raise ValueError(f"reply has no {key}")
    return [item for item in value if isinstance(item, dict)]


def why(text: str) -> str:
    """A reply's prompt-enhancement notes, or "" when it has none."""
    try:
        value = parse(text)
    except ValueError:
        return ""
    notes = value.get("why") if isinstance(value, dict) else None
    return notes.strip() if isinstance(notes, str) else ""


def decode(text: str) -> tuple[dict[int, str], set[int]]:
    """Candidates by record number, and numbers that appear more than once. Raises ValueError."""
    value = parse(text)
    if isinstance(value, dict) and isinstance(value.get("translations"), list):
        value = value["translations"]
    if isinstance(value, dict):
        items = list(value.items())
    elif isinstance(value, list):
        items = [(item.get("i"), item.get("t")) for item in value if isinstance(item, dict)]
    else:
        raise ValueError("reply has no translations")
    found, duplicates = {}, set()
    for number, candidate in items:
        if isinstance(number, bool) or not isinstance(candidate, str):
            continue
        try:
            number = int(number)
        except (TypeError, ValueError):
            continue
        if number in found:
            duplicates.add(number)
        found[number] = candidate
    return found, duplicates


@dataclass
class Stats:
    total: int = 0
    done: int = 0
    failed: int = 0
    requests: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    active: int = 0
    queued: int = 0
    cooling: int = 0
    replies: int = 0
    latency: float = 0.0
    started: float = field(default_factory=time.monotonic)
    output: deque = field(default_factory=lambda: deque(maxlen=4096))

    def rate(self, window: float = 10.0) -> float:
        """Output tokens per second over the last `window` seconds."""
        now = time.monotonic()
        tokens = sum(count for at, count in self.output if now - at <= window)
        return tokens / max(1.0, min(window, now - self.started))


@dataclass
class Batch:
    """One finished request, for live inspection. records: (speaker, source, output, outcome, note)."""
    number: int
    task: str
    file: str
    records: list
    thinking: str = ""
    latency: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    terms: list = field(default_factory=list)
    context: int = 0
    attempt: int = 0
    error: str = ""
    worker: str = ""


@dataclass(eq=False)
class Worker:
    """One profile taking batches from a run, with its own key rotation, rate-limit cooldowns and RPM pacing."""
    profile: object
    provider: object
    keys: list
    turn: int = 0
    next_start: float = 0.0
    cooldown: dict = field(default_factory=dict)
    error: str | None = None

    @property
    def label(self) -> str:
        return f"{self.profile.name} · {self.profile.model}"


class Run:
    """task: "translate" fills units, "review" stores suggested revisions, "discover" adds draft glossary entries.

    `parallel` adds (profile, provider) pairs that take batches from the same queue as the main profile. Every
    batch goes to exactly one free slot, and all workspace writes stay on the event loop, so workers never race
    on a unit; a unit edited since planning keeps its value."""

    def __init__(self, ws, rows, indices, *, profile, provider, sections, terms, settings, task="translate",
                 rules=None, on_event=lambda kind, text: None, on_commit=lambda index: None,
                 on_batch=lambda batch: None, parallel=()):
        self.workers = []
        for each, service in [(profile, provider), *parallel]:
            keys = each.api_keys()
            if not keys:
                raise VnTransError(f"profile {each.name!r} has no API key")
            if not each.model:
                raise VnTransError(f"profile {each.name!r} has no model")
            self.workers.append(Worker(each, service, keys))
        self.ws, self.rows, self.indices = ws, rows, indices
        self.task, self.rules = task, rules or Rules()
        self.found = 0
        self.profile, self.provider = profile, provider
        self.sections, self.terms = sections, terms
        self.names = quality.speaker_names(rows, terms)
        self.settings = SETTINGS | settings
        self.languages = dict(ws.manifest["languages"])
        self.planned = {index: (rows[index].status, rows[index].translation) for index in indices}
        self.stats = Stats(total=len(indices))
        self.on_event, self.on_commit, self.on_batch = on_event, on_commit, on_batch
        self.counter = 0
        self.stopping = False
        self.error = None
        concurrency = max(1, self.settings["concurrency"])
        self.slots = concurrency * len(self.workers)
        self.pool = asyncio.Queue()
        for _ in range(concurrency):
            for worker in self.workers:
                self.pool.put_nowait(worker)
        self.aliases = {}

    def stop(self):
        self.stopping = True

    async def acquire(self) -> Worker | None:
        """A free slot of any working profile, or None once every profile has failed."""
        while True:
            worker = await self.pool.get()
            if worker is None:
                self.pool.put_nowait(None)
                return None
            if worker.error is None:
                return worker

    def release(self, worker: Worker):
        if worker.error is None:
            self.pool.put_nowait(worker)

    def disable(self, worker: Worker, message: str):
        """Take a profile out of the run (bad key, unknown model); the run stops once none is left."""
        if worker.error is not None:
            return
        worker.error = message
        if all(other.error for other in self.workers):
            self.error = message
            self.stopping = True
            self.pool.put_nowait(None)
        else:
            self.on_event("error", f"{worker.profile.name} left the run: {message}")

    def pending(self, index) -> bool:
        row = self.rows[index]
        return (row.status, row.translation) == self.planned[index]

    def commit(self, pairs):
        """Store (index, text) pairs; the text also goes to the unit's pending identical occurrences."""
        targets = [(target, text) for index, text in pairs
                   for target in (index, *(alias for alias in self.aliases.get(index, ()) if self.pending(alias)))]
        committed = {id(row) for row in self.ws.commit(
            [(self.rows[target], text, *self.planned[target]) for target, text in targets])}
        for target, text in targets:
            if id(self.rows[target]) in committed:
                self.stats.done += 1
                if self.rows[target].kind == "speaker" and text.strip():
                    self.names.add(text.strip().casefold())
                self.on_commit(target)

    def report(self, worker, prompt, batch, records, *, reply=None, depth=0, error=""):
        self.counter += 1
        self.on_batch(Batch(
            self.counter, self.task, self.rows[batch[0]].file, records,
            reply.thinking if reply else "", reply.latency if reply else 0.0,
            (reply.tokens_in or 0) if reply else 0, (reply.tokens_out or 0) if reply else 0,
            [f"{term.source} → {term.target}" for term in prompt.terms], prompt.context, depth, error, worker.label))

    def outcomes(self, batch, outputs, failed):
        return [(row.speaker if row.kind != "speaker" else "", row.source, outputs.get(index, ""),
                 "failed" if index in failed else "ok", failed.get(index, ""))
                for index, row in ((index, self.rows[index]) for index in batch)]

    def give_up(self, failed: dict[int, str]):
        if self.stopping:
            return
        for index, message in failed.items():
            group = [i for i in (index, *self.aliases.get(index, ())) if self.pending(i)]
            self.ws.mark_errors([self.rows[i] for i in group], message)
            self.stats.failed += len(group)
            for i in group:
                self.on_commit(i)

    async def run(self):
        size, tokens = self.settings["batch_lines"], self.settings["batch_tokens"]
        if self.task == "translate":
            await self.translate()
        elif self.task == "review":
            batches = prompts.plan(self.rows, self.indices, lines=size, tokens=tokens)
            self.on_event("info", f"Reviewing {self.stats.total} units in {len(batches)} batches")
            await asyncio.gather(*(self.side(batch, self.review) for batch in batches))
        else:
            unique, seen = [], set()
            for index in self.indices:
                if self.rows[index].source not in seen:
                    seen.add(self.rows[index].source)
                    unique.append(index)
            self.stats.total = len(unique)
            batches = prompts.plan(self.rows, unique, lines=size * 5, tokens=tokens * 5)
            self.on_event("info", f"Scanning {len(unique)} unique lines in {len(batches)} batches")
            await asyncio.gather(*(self.side(batch, self.discover) for batch in batches))
        found = dict(translate="", review=f", {self.found} suggestions", discover=f", {self.found} new terms")[self.task]
        if self.error:
            self.on_event("error", f"Stopped: {self.error}")
        else:
            self.on_event("done", ("Stopped" if self.stopping else "Finished")
                          + f": {self.stats.done} {'translated' if self.task == 'translate' else 'checked'}, "
                          + f"{self.stats.failed} failed{found}")

    async def side(self, batch, accept):
        """One review/discovery request; failures never touch translations."""
        if self.stopping:
            return
        self.stats.queued += 1
        worker = await self.acquire()
        self.stats.queued -= 1
        if worker is None:
            return
        moved = False
        try:
            if self.stopping:
                return
            if self.task == "review":
                prompt = prompts.build(self.sections, self.languages, self.rows, batch, terms=self.terms,
                                       before=0, after=0, task="review", pre=self.rules.pre, formats=self.ws.formats)
                schema = REVIEW_SCHEMA
            else:
                prompt, schema = prompts.discovery(self.languages, self.rows, batch, self.ws.formats), TERMS_SCHEMA
            reply = None
            try:
                reply = await self.request(worker, prompt, schema)
                records = accept(batch, reply)
                self.stats.done += len(batch)
                self.report(worker, prompt, batch, records, reply=reply)
            except ProviderError as exc:
                if exc.kind in ("auth", "request"):
                    self.disable(worker, str(exc))
                    moved = True
                else:
                    self.stats.failed += len(batch)
                    self.on_event("error", f"{len(batch)} units skipped: {exc}")
                    self.report(worker, prompt, batch, self.outcomes(batch, {}, dict.fromkeys(batch, str(exc))),
                                error=str(exc))
            except ValueError as exc:
                self.stats.failed += len(batch)
                self.on_event("error", f"{len(batch)} units skipped: unreadable reply ({exc})")
                self.report(worker, prompt, batch, self.outcomes(batch, {}, dict.fromkeys(batch, "unreadable reply")),
                            reply=reply, error=f"unreadable reply: {exc}")
        finally:
            self.release(worker)
        if moved:
            await self.side(batch, accept)

    def review(self, batch, reply):
        suggested = {}
        for item in listed(reply.text, "translations"):
            number, text, reason = item.get("i"), item.get("t"), item.get("r", "")
            if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= len(batch) \
                    or not isinstance(text, str) or not isinstance(reason, str):
                continue
            row = self.rows[batch[number - 1]]
            try:
                text = self.ws.check(row, quality.tidy(Rules.apply(self.rules.post, text), row, self.names,
                                                       self.ws.formats))
            except VnTransError:
                continue
            if row.translation is not None and text != row.translation:
                self.ws.suggest(row, text, reason.strip())
                suggested[batch[number - 1]] = (text, reason.strip())
                self.found += 1
                self.on_commit(batch[number - 1])
        return [(row.speaker if row.kind != "speaker" else "", row.source,
                 suggested[index][0] if index in suggested else row.translation or "",
                 "changed" if index in suggested else "ok", suggested[index][1] if index in suggested else "")
                for index, row in ((index, self.rows[index]) for index in batch)]

    def discover(self, batch, reply):
        known = {term.source for term in self.terms}
        records = []
        for item in listed(reply.text, "terms"):
            source, target = item.get("source"), item.get("target")
            if not isinstance(source, str) or not isinstance(target, str):
                continue
            source = source.strip()
            examples = [row.source for row in self.rows if source and source in row.source][:3]
            if not source or source in known or not examples:
                if source:
                    records.append(("", source, target.strip(), "skipped",
                                    "already in glossary" if source in known else "not found in the text"))
                continue
            notes = item.get("notes") if isinstance(item.get("notes"), str) else ""
            term = Term(source, target.strip(), "character" if item.get("kind") == "character" else "term",
                        notes.strip(), approved=False, evidence=examples)
            self.ws.save_term(term)
            self.terms.append(term)
            known.add(source)
            self.found += 1
            records.append(("", source, term.target, "ok", f"{term.kind}" + (f" · {term.notes}" if term.notes else "")))
        return records

    async def translate(self):
        """Identical units (same kind, format and source) are sent once; the rest follow as aliases.

        New units reuse an identical translation from outside the run, preferring manual edits."""
        glossary = {term.source: term.target for term in self.terms if term.approved and term.target}
        earlier = {}
        for index, row in enumerate(self.rows):
            if index not in self.planned and row.translation is not None and row.status in ("edited", "machine"):
                key = (row.kind, self.ws.formats[row.file], row.source)
                if row.status == "edited" or key not in earlier:
                    earlier[key] = row.translation
        exact, representatives = [], {}
        for index in self.indices:
            row = self.rows[index]
            key = (row.kind, self.ws.formats[row.file], row.source)
            known = glossary.get(row.source) if row.kind == "speaker" else None
            if known is None and row.status == "new" and key in earlier:
                known = quality.tidy(earlier[key], row, self.names, self.ws.formats)
            if known is not None:
                try:
                    exact.append((index, self.ws.check(row, known)))
                    continue
                except VnTransError:
                    pass
            if key in representatives:
                self.aliases[representatives[key]].append(index)
            else:
                representatives[key] = index
                self.aliases[index] = []
        if exact:
            self.commit(exact)
        size = self.settings["batch_lines"]
        names = [index for index in representatives.values() if self.rows[index].kind == "speaker"]
        lines = [index for index in representatives.values() if self.rows[index].kind != "speaker"]
        name_batches = [names[i:i + size] for i in range(0, len(names), size)]
        batches = prompts.plan(self.rows, lines, lines=size, tokens=self.settings["batch_tokens"])
        self.on_event("info", f"{self.stats.total} units in {len(name_batches) + len(batches)} batches"
                              + (f", {len(exact)} filled from glossary or identical lines" if exact else ""))
        await asyncio.gather(*(self.handle(batch, context=False) for batch in name_batches),
                             *(self.handle(batch) for batch in batches))

    async def handle(self, batch, depth=0, context=True):
        batch = [index for index in batch if self.pending(index)]
        if not batch or self.stopping:
            return
        self.stats.queued += 1
        worker = await self.acquire()
        self.stats.queued -= 1
        if worker is None:
            return
        failed, moved = {}, False
        try:
            if self.stopping:
                return
            prompt = prompts.build(self.sections, self.languages, self.rows, batch, terms=self.terms,
                                   before=self.settings["context_before"] if context else 0,
                                   after=self.settings["context_after"] if context else 0, pre=self.rules.pre,
                                   formats=self.ws.formats)
            try:
                reply = await self.request(worker, prompt, THINKING_SCHEMA if prompt.thinking else SCHEMA)
            except ProviderError as exc:
                if exc.kind in ("auth", "request"):
                    self.disable(worker, str(exc))
                    moved = True
                else:
                    failed = dict.fromkeys(batch, str(exc))
                    self.report(worker, prompt, batch, self.outcomes(batch, {}, failed), depth=depth, error=str(exc))
                    if exc.kind != "blocked":
                        self.give_up(failed)
                        return
            else:
                if prompt.thinking:
                    reply.thinking = "\n\n".join(part for part in (reply.thinking, why(reply.text)) if part)
                failed, outputs = self.accept(batch, reply)
                self.report(worker, prompt, batch, self.outcomes(batch, outputs, failed), reply=reply, depth=depth)
        finally:
            self.release(worker)
        if moved:
            await self.handle(batch, depth, context)
        elif failed and not self.stopping:
            await self.retry(failed, depth)

    async def retry(self, failed, depth):
        rest = sorted(failed)
        if len(rest) > 1:
            if depth < 2:
                half = len(rest) // 2
                jobs = [(rest[:half], depth + 1, True), (rest[half:], depth + 1, True)]
            else:
                jobs = [([index], 3, False) for index in rest]
        elif depth < 4:
            jobs = [(rest, max(depth + 1, 3), False)]
        else:
            self.give_up(failed)
            return
        self.on_event("retry", f"Retrying {len(rest)} unit(s): {failed[rest[0]]}")
        await asyncio.gather(*(self.handle(batch, d, context) for batch, d, context in jobs))

    def accept(self, batch, reply) -> tuple[dict[int, str], dict[int, str]]:
        """Commit valid candidates; returns (failure reason by index, candidate text by index)."""
        try:
            found, duplicates = decode(reply.text)
        except ValueError as exc:
            reason = "reply was cut off (raise max output tokens)" if reply.finish == "length" else f"unreadable reply: {exc}"
            return dict.fromkeys(batch, reason), {}
        failed, accepted, outputs = {}, [], {}
        for number, index in enumerate(batch, 1):
            row = self.rows[index]
            text = found.get(number)
            if text is not None:
                text = outputs[index] = quality.tidy(Rules.apply(self.rules.post, text), row, self.names,
                                                     self.ws.formats)
            if number in duplicates:
                failed[index] = "duplicate entries in reply"
            elif text is None:
                failed[index] = "missing from reply" + (" (cut off)" if reply.finish == "length" else "")
            elif not text.strip() and MARKER.sub("", row.source).strip():
                failed[index] = "empty translation"
            else:
                try:
                    accepted.append((index, self.ws.check(row, text)))
                except VnTransError as exc:
                    failed[index] = str(exc).removeprefix(f"{row.id}: ")
        if accepted:
            self.commit(accepted)
        return failed, outputs

    async def request(self, worker, prompt, schema=SCHEMA):
        profile = worker.profile
        for attempt in range(TRANSPORT_TRIES):
            key = await self.key(worker)
            await self.pace(worker)
            self.stats.requests += 1
            self.stats.active += 1
            started = time.monotonic()
            try:
                reply = await asyncio.to_thread(worker.provider.complete, profile, key, prompt.system, prompt.user,
                                                schema)
            except ProviderError as exc:
                self.ws.record_attempt(profile.name, profile.model, prompt.version, len(prompt.rows),
                                       exc.kind, str(exc))
                if exc.kind not in ("rate", "transient") or attempt == TRANSPORT_TRIES - 1 or self.stopping:
                    raise
                delay = exc.retry_after or min(60, 2 ** (attempt + 1))
                self.on_event("retry", f"{profile.name}: {exc} · retry in {delay:.0f}s"
                              if len(self.workers) > 1 else f"{exc} · retry in {delay:.0f}s")
                if exc.kind == "rate":
                    worker.cooldown[key] = time.monotonic() + delay
                else:
                    self.stats.cooling += 1
                    try:
                        await asyncio.sleep(delay)
                    finally:
                        self.stats.cooling -= 1
                continue
            finally:
                self.stats.active -= 1
            reply.latency = time.monotonic() - started
            self.stats.replies += 1
            self.stats.latency += reply.latency
            self.stats.output.append((time.monotonic(), reply.tokens_out or 0))
            self.stats.tokens_in += reply.tokens_in or 0
            self.stats.tokens_out += reply.tokens_out or 0
            self.ws.record_attempt(profile.name, profile.model, prompt.version, len(prompt.rows),
                                   "ok", None, reply.tokens_in, reply.tokens_out)
            return reply

    async def key(self, worker) -> str:
        """Round-robin over the worker's keys, skipping any cooling down after a rate limit."""
        while True:
            now = time.monotonic()
            for _ in worker.keys:
                key = worker.keys[worker.turn % len(worker.keys)]
                worker.turn += 1
                if worker.cooldown.get(key, 0) <= now:
                    return key
            self.stats.cooling += 1
            try:
                await asyncio.sleep(max(0.05, min(worker.cooldown.values()) - now))
            finally:
                self.stats.cooling -= 1

    async def pace(self, worker):
        """The RPM limit applies to each profile (each model) separately."""
        if self.settings["rpm"] > 0:
            now = time.monotonic()
            start = max(now, worker.next_start)
            worker.next_start = start + 60 / self.settings["rpm"]
            if start > now:
                await asyncio.sleep(start - now)
