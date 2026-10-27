"""Prompt section chain: batches and deterministic system/user messages."""

from dataclasses import dataclass
import hashlib
import json

from . import glossary


@dataclass(frozen=True)
class Section:
    key: str
    title: str
    role: str
    editable: bool


SECTIONS = (
    Section("role", "Role", "system", True),
    Section("style", "Style", "system", True),
    Section("instructions", "Project notes", "system", True),
    Section("characters", "Characters", "user", False),
    Section("glossary", "Glossary", "user", False),
    Section("context", "Context", "user", False),
    Section("contract", "Output rules", "system", False),
)
BY_KEY = {section.key: section for section in SECTIONS}
BATCH = dict(batch_lines=20, batch_tokens=1200, context_before=5, context_after=2)

STYLE_PRESETS = {
    "Keep honorifics": (
        "- Keep Japanese honorifics (-san, -kun, -chan, -sama, -senpai) attached to names, romanized.\n"
        "- Keep name order as written in the source.\n"
        "- Turn 「」 and 『』 into regular quotation marks.\n"
        "- Keep ellipses, dashes and stammering where they show hesitation or emotion.\n"
        "- Write sound effects and interjections the way a {target} reader would hear them."),
    "Localize": (
        "- Drop honorifics and convey the relationship through word choice instead.\n"
        "- Adapt jokes, idioms and wordplay to natural {target} equivalents.\n"
        "- Turn 「」 and 『』 into regular quotation marks.\n"
        "- Keep ellipses, dashes and stammering where they show hesitation or emotion.\n"
        "- Write sound effects and interjections the way a {target} reader would hear them."),
}

DEFAULTS = {
    "role": (
        "You translate {source} visual novel scripts into natural, fluent {target}.\n"
        "Preserve each line's meaning, tone and the speaker's personality. Translate faithfully, "
        "without adding, omitting or softening content."),
    "style": STYLE_PRESETS["Keep honorifics"],
    "instructions": "",
}

CONTRACT = (
    "Output rules:\n"
    '- Reply with only this JSON object: {"translations": [{"i": <number>, "t": "<translation>"}]}, '
    'with one entry per input record, reusing its "i".\n'
    '- Translate only each record\'s "text". Context, glossary and character notes are reference material.\n'
    "- Keep every placeholder such as ⟦0⟧ exactly once and in the same order. Placeholders stand for game "
    "commands: never translate, remove or add them.\n"
    "- Use \\n for a line break inside a translation. Speaker names, titles and choices stay on one line.\n"
    "- Use the glossary and character translations whenever those names or terms appear.")

REVIEW = (
    "Review task:\n"
    "- Compare each record's existing translation with its source text.\n"
    "- Report only records with a clear problem: wrong meaning, omitted or invented content, a wrong name or "
    "term, a voice that doesn't fit the speaker, or clearly unnatural {target}. Leave acceptable ones out.\n"
    '- Reply with only this JSON object: {"translations": [{"i": <number>, "t": "<corrected translation>", '
    '"r": "<short reason>"}]}. Use an empty list when nothing needs fixing.\n'
    "- Keep every placeholder such as ⟦0⟧ exactly once and in the same order. Use \\n for line breaks.")

DISCOVER = (
    "You prepare a translation glossary for a {source} visual novel that will be translated into {target}.\n"
    "From the text below, list character names, nicknames, places, organizations, invented terms and other "
    "expressions that must be translated the same way every time. Skip ordinary words.\n"
    'Reply with only this JSON object: {"terms": [{"source": "<exact text as written>", '
    '"target": "<{target} translation>", "kind": "character" or "term", "notes": "<gender, role or meaning>"}]}. '
    "Use an empty list when there is nothing to add.")


def chain(config: dict) -> list[dict]:
    """Stored section order and settings over the defaults; output rules always come last."""
    result, seen = [], set()
    for item in config.get("sections", []):
        key = item.get("key") if isinstance(item, dict) else None
        if key in BY_KEY and key != "contract" and key not in seen:
            seen.add(key)
            text = item.get("text")
            result.append(dict(key=key, enabled=bool(item.get("enabled", True)),
                               text=text if isinstance(text, str) and BY_KEY[key].editable else None))
    result += [dict(key=s.key, enabled=True, text=None) for s in SECTIONS if s.key not in seen and s.key != "contract"]
    return result + [dict(key="contract", enabled=True, text=None)]


def estimate(text: str) -> int:
    """Rough token count: CJK characters ~1 token each, other text ~4 characters per token."""
    wide = sum(1 for char in text if ord(char) >= 0x2E80)
    return wide + (len(text) - wide + 3) // 4


def plan(rows, indices, *, lines=20, tokens=1200) -> list[list[int]]:
    """Group sorted row indices into batches that never cross a file or scene."""
    batches, current, size, key = [], [], 0, None
    for index in indices:
        row = rows[index]
        cost = estimate(row.source) + estimate(row.speaker) + 8
        if current and ((row.file, row.scene) != key or len(current) >= lines or size + cost > tokens):
            batches.append(current)
            current, size = [], 0
        current.append(index)
        size += cost
        key = (row.file, row.scene)
    if current:
        batches.append(current)
    return batches


def neighbours(rows, batch, before, after):
    """Nearby non-speaker lines in the same file and scene, outside the batch."""
    members = set(batch)
    key = (rows[batch[0]].file, rows[batch[0]].scene)

    def collect(start, step, limit):
        found, index = [], start
        while 0 <= index < len(rows) and len(found) < limit and (rows[index].file, rows[index].scene) == key:
            if rows[index].kind != "speaker" and index not in members:
                found.append(rows[index])
            index += step
        return found

    return collect(batch[0] - 1, -1, before)[::-1], collect(batch[-1] + 1, 1, after)


def line(row, translated: bool) -> str:
    text = row.source.replace("\n", "\\n")
    if row.speaker and row.kind != "speaker":
        text = f"{row.speaker}: {text}"
    if translated and row.translation and row.status != "excluded":
        text += "\n  → " + row.translation.replace("\n", "\\n")
    return "- " + text


@dataclass
class Prompt:
    system: str
    user: str
    rows: list
    terms: list
    version: str
    context: int = 0

    @property
    def tokens(self) -> int:
        return estimate(self.system) + estimate(self.user)


def fill(text, languages):
    return text.replace("{source}", languages["source"]).replace("{target}", languages["target"])


def build(sections, languages, rows, batch, *, terms=(), before=5, after=2, task="translate", pre=()) -> Prompt:
    """task: "translate" or "review"; `pre` replacement rules rewrite the source text the model sees."""
    records = [rows[index] for index in batch]
    matched = glossary.match(terms, [row.source for row in records] + [row.speaker for row in records if row.speaker])
    earlier, later = neighbours(rows, batch, before, after)

    def entry(term):
        return f"- {term.source} → {term.target}" + (f" ({term.notes.strip()})" if term.notes.strip() else "")

    system, user = [], []
    for item in sections:
        section = BY_KEY[item["key"]]
        if not item["enabled"]:
            continue
        if section.editable:
            text = fill(item["text"] if item["text"] is not None else DEFAULTS[section.key], languages).strip()
        elif section.key == "contract":
            text = CONTRACT if task == "translate" else fill(REVIEW, languages)
        elif section.key == "characters":
            found = [entry(term) for term in matched if term.kind == "character"]
            text = "Characters:\n" + "\n".join(found) if found else ""
        elif section.key == "glossary":
            found = [entry(term) for term in matched if term.kind != "character"]
            text = "Glossary:\n" + "\n".join(found) if found else ""
        else:
            parts = []
            if earlier:
                parts.append("Earlier lines (reference only):\n" + "\n".join(line(row, True) for row in earlier))
            if later:
                parts.append("Later lines (reference only):\n" + "\n".join(line(row, False) for row in later))
            text = "\n\n".join(parts)
        if text:
            (system if section.role == "system" else user).append(text)

    def record(number, row):
        value = dict(i=number, kind=row.kind)
        if row.speaker and row.kind != "speaker":
            value["speaker"] = row.speaker
        value["text"] = glossary.Rules.apply(pre, row.source)
        if task == "review":
            value["translation"] = row.translation
        return json.dumps(value, ensure_ascii=False)

    heading = (f"Translate these {len(records)} records into {languages['target']}:" if task == "translate"
               else f"Review these {len(records)} translations:")
    user.append(heading + "\n" + "\n".join(record(number, row) for number, row in enumerate(records, 1)))
    system_text = "\n\n".join(system)
    return Prompt(system_text, "\n\n".join(user), records, matched,
                  hashlib.sha256(system_text.encode()).hexdigest()[:12], len(earlier) + len(later))


def discovery(languages, rows, batch) -> Prompt:
    records = [rows[index] for index in batch]
    system = fill(DISCOVER, languages)
    user = "Text:\n" + "\n".join(line(row, False) for row in records)
    return Prompt(system, user, records, [], hashlib.sha256(system.encode()).hexdigest()[:12])
