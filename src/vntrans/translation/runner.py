"""Batch translation run: plan, prompt, request, decode, validate, checkpoint.

Runs on one asyncio loop; only provider calls leave it (asyncio.to_thread), so workspace
writes are never concurrent. Accepted units are committed per batch, so stopping or
crashing keeps paid work, and the next run picks up the remaining `new` units."""

import asyncio
from dataclasses import dataclass, field
import json
import re
import time

from ..model import VnTransError
from ..text import MARKER
from . import prompts
from .glossary import Rules, Term
from .providers import REVIEW_SCHEMA, SCHEMA, TERMS_SCHEMA, ProviderError

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
    started: float = field(default_factory=time.monotonic)


class Run:
    """task: "translate" fills units, "review" stores suggested revisions, "discover" adds draft glossary entries."""

    def __init__(self, ws, rows, indices, *, profile, provider, sections, terms, settings, task="translate",
                 rules=None, on_event=lambda kind, text: None, on_commit=lambda index: None):
        self.keys = profile.api_keys()
        if not self.keys:
            raise VnTransError(f"profile {profile.name!r} has no API key")
        if not profile.model:
            raise VnTransError(f"profile {profile.name!r} has no model")
        self.ws, self.rows, self.indices = ws, rows, indices
        self.task, self.rules = task, rules or Rules()
        self.found = 0
        self.profile, self.provider = profile, provider
        self.sections, self.terms = sections, terms
        self.settings = SETTINGS | settings
        self.languages = dict(ws.manifest["languages"])
        self.planned = {index: (rows[index].status, rows[index].translation) for index in indices}
        self.stats = Stats(total=len(indices))
        self.on_event, self.on_commit = on_event, on_commit
        self.stopping = False
        self.error = None
        self.slot = asyncio.Semaphore(max(1, self.settings["concurrency"]))
        self.next_start = 0.0
        self.turn = 0
        self.cooldown = {}
        self.aliases = {}

    def stop(self):
        self.stopping = True

    def pending(self, index) -> bool:
        row = self.rows[index]
        return (row.status, row.translation) == self.planned[index]

    def commit(self, pairs):
        """Store (index, text) pairs; a speaker name's text also goes to its other pending occurrences."""
        targets = [(target, text) for index, text in pairs
                   for target in (index, *(alias for alias in self.aliases.get(index, ()) if self.pending(alias)))]
        committed = {id(row) for row in self.ws.commit(
            [(self.rows[target], text, *self.planned[target]) for target, text in targets])}
        for target, _ in targets:
            if id(self.rows[target]) in committed:
                self.stats.done += 1
                self.on_commit(target)

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
        async with self.slot:
            if self.stopping:
                return
            if self.task == "review":
                prompt = prompts.build(self.sections, self.languages, self.rows, batch, terms=self.terms,
                                       before=0, after=0, task="review", pre=self.rules.pre)
                schema = REVIEW_SCHEMA
            else:
                prompt, schema = prompts.discovery(self.languages, self.rows, batch), TERMS_SCHEMA
            try:
                reply = await self.request(prompt, schema)
                accept(batch, reply)
                self.stats.done += len(batch)
            except ProviderError as exc:
                if exc.kind in ("auth", "request"):
                    self.error = str(exc)
                    self.stopping = True
                    return
                self.stats.failed += len(batch)
                self.on_event("error", f"{len(batch)} units skipped: {exc}")
            except ValueError as exc:
                self.stats.failed += len(batch)
                self.on_event("error", f"{len(batch)} units skipped: unreadable reply ({exc})")

    def review(self, batch, reply):
        for item in listed(reply.text, "translations"):
            number, text, reason = item.get("i"), item.get("t"), item.get("r", "")
            if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= len(batch) \
                    or not isinstance(text, str) or not isinstance(reason, str):
                continue
            row = self.rows[batch[number - 1]]
            try:
                text = self.ws.check(row, Rules.apply(self.rules.post, text))
            except VnTransError:
                continue
            if row.translation is not None and text != row.translation:
                self.ws.suggest(row, text, reason.strip())
                self.found += 1
                self.on_commit(batch[number - 1])

    def discover(self, batch, reply):
        known = {term.source for term in self.terms}
        for item in listed(reply.text, "terms"):
            source, target = item.get("source"), item.get("target")
            if not isinstance(source, str) or not isinstance(target, str):
                continue
            source = source.strip()
            examples = [row.source for row in self.rows if source and source in row.source][:3]
            if not source or source in known or not examples:
                continue
            notes = item.get("notes") if isinstance(item.get("notes"), str) else ""
            term = Term(source, target.strip(), "character" if item.get("kind") == "character" else "term",
                        notes.strip(), approved=False, evidence=examples)
            self.ws.save_term(term)
            self.terms.append(term)
            known.add(source)
            self.found += 1

    async def translate(self):
        names, lines = [], []
        for index in self.indices:
            (names if self.rows[index].kind == "speaker" else lines).append(index)
        known = {term.source: term.target for term in self.terms if term.approved and term.target}
        exact, representatives = [], {}
        for index in names:
            source = self.rows[index].source
            if source in known:
                try:
                    exact.append((index, self.ws.check(self.rows[index], known[source])))
                    continue
                except VnTransError:
                    pass
            if source in representatives:
                self.aliases[representatives[source]].append(index)
            else:
                representatives[source] = index
                self.aliases[index] = []
        if exact:
            self.commit(exact)
        size = self.settings["batch_lines"]
        unique = list(representatives.values())
        name_batches = [unique[i:i + size] for i in range(0, len(unique), size)]
        batches = prompts.plan(self.rows, lines, lines=size, tokens=self.settings["batch_tokens"])
        self.on_event("info", f"{self.stats.total} units in {len(name_batches) + len(batches)} batches"
                              + (f", {len(exact)} names from glossary" if exact else ""))
        await asyncio.gather(*(self.handle(batch, context=False) for batch in name_batches),
                             *(self.handle(batch) for batch in batches))

    async def handle(self, batch, depth=0, context=True):
        batch = [index for index in batch if self.pending(index)]
        if not batch or self.stopping:
            return
        async with self.slot:
            if self.stopping:
                return
            prompt = prompts.build(self.sections, self.languages, self.rows, batch, terms=self.terms,
                                   before=self.settings["context_before"] if context else 0,
                                   after=self.settings["context_after"] if context else 0, pre=self.rules.pre)
            try:
                reply = await self.request(prompt)
            except ProviderError as exc:
                if exc.kind in ("auth", "request"):
                    self.error = str(exc)
                    self.stopping = True
                    return
                if exc.kind != "blocked":
                    self.give_up(dict.fromkeys(batch, str(exc)))
                    return
                failed = dict.fromkeys(batch, str(exc))
            else:
                failed = self.accept(batch, reply)
        if failed and not self.stopping:
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

    def accept(self, batch, reply) -> dict[int, str]:
        try:
            found, duplicates = decode(reply.text)
        except ValueError as exc:
            reason = "reply was cut off (raise max output tokens)" if reply.finish == "length" else f"unreadable reply: {exc}"
            return dict.fromkeys(batch, reason)
        failed, accepted = {}, []
        for number, index in enumerate(batch, 1):
            row = self.rows[index]
            text = found.get(number)
            if number in duplicates:
                failed[index] = "duplicate entries in reply"
            elif text is None:
                failed[index] = "missing from reply" + (" (cut off)" if reply.finish == "length" else "")
            elif not text.strip() and MARKER.sub("", row.source).strip():
                failed[index] = "empty translation"
            else:
                try:
                    accepted.append((index, self.ws.check(row, Rules.apply(self.rules.post, text))))
                except VnTransError as exc:
                    failed[index] = str(exc).removeprefix(f"{row.id}: ")
        if accepted:
            self.commit(accepted)
        return failed

    async def request(self, prompt, schema=SCHEMA):
        for attempt in range(TRANSPORT_TRIES):
            key = await self.key()
            await self.pace()
            self.stats.requests += 1
            try:
                reply = await asyncio.to_thread(self.provider.complete, self.profile, key, prompt.system, prompt.user,
                                                schema)
            except ProviderError as exc:
                self.ws.record_attempt(self.profile.name, self.profile.model, prompt.version, len(prompt.rows),
                                       exc.kind, str(exc))
                if exc.kind not in ("rate", "transient") or attempt == TRANSPORT_TRIES - 1 or self.stopping:
                    raise
                delay = exc.retry_after or min(60, 2 ** (attempt + 1))
                self.on_event("retry", f"{exc} · retry in {delay:.0f}s")
                if exc.kind == "rate":
                    self.cooldown[key] = time.monotonic() + delay
                else:
                    await asyncio.sleep(delay)
                continue
            self.stats.tokens_in += reply.tokens_in or 0
            self.stats.tokens_out += reply.tokens_out or 0
            self.ws.record_attempt(self.profile.name, self.profile.model, prompt.version, len(prompt.rows),
                                   "ok", None, reply.tokens_in, reply.tokens_out)
            return reply

    async def key(self) -> str:
        """Round-robin over keys, skipping any cooling down after a rate limit."""
        while True:
            now = time.monotonic()
            for _ in self.keys:
                key = self.keys[self.turn % len(self.keys)]
                self.turn += 1
                if self.cooldown.get(key, 0) <= now:
                    return key
            await asyncio.sleep(max(0.05, min(self.cooldown.values()) - now))

    async def pace(self):
        if self.settings["rpm"] > 0:
            now = time.monotonic()
            start = max(now, self.next_start)
            self.next_start = start + 60 / self.settings["rpm"]
            if start > now:
                await asyncio.sleep(start - now)
