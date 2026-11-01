"""Prompt section chain: batches and deterministic system/user messages."""

import hashlib
import json
from dataclasses import dataclass

from ..text import plain
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
    Section("thinking", "Prompt enhancement", "system", False),
    Section("contract", "Output rules", "system", False),
)
BY_KEY = {section.key: section for section in SECTIONS}
BATCH = dict(batch_lines=20, batch_tokens=1200, context_before=5, context_after=2)

STYLE_PRESETS = {
    "Keep honorifics": (
        "- Keep Japanese honorifics attached to names, romanized: Sayu-chan, Saigawa-san, Taro-kun, "
        "Hana-sama, Kei-senpai.\n"
        "- Keep how characters address each other, romanized: お兄ちゃん → Onii-chan, 兄さん → Nii-san, "
        "お姉ちゃん → Onee-chan, 先輩 → Senpai. Never turn them into Brother, Sister or a bare name.\n"
        "- Keep name order as written in the source.\n"
        "- Use “ (opening) and ” (closing) for source 「…」 and 『…』; keep unquoted narration unquoted.\n"
        "- Keep ellipses, dashes and stammering where they show hesitation or emotion.\n"
        "- Write sound effects and interjections the way a {target} reader would hear them."
    ),
    "Localize": (
        "- Drop honorifics and convey the relationship through word choice instead.\n"
        "- Adapt jokes, idioms and wordplay to natural {target} equivalents.\n"
        "- Use “ (opening) and ” (closing) for source 「…」 and 『…』; keep unquoted narration unquoted.\n"
        "- Keep ellipses, dashes and stammering where they show hesitation or emotion.\n"
        "- Write sound effects and interjections the way a {target} reader would hear them."
    ),
}

DEFAULTS = {
    "role": (
        "You translate {source} visual novel scripts into natural, fluent {target}.\n"
        "Preserve each line's meaning, tone and the speaker's personality. Translate faithfully, "
        "without adding, omitting or softening content.\n"
        "Lines must fit the game's text box. When a faithful translation would run noticeably longer than the "
        "source, say the same thing more concisely: tighter phrasing, shorter words, no filler. Never drop "
        "meaning, nuance, names, honorifics or emotion to save space."
    ),
    "style": STYLE_PRESETS["Keep honorifics"],
    "instructions": "",
}

CONTRACT = (
    "Output rules:\n"
    '- Reply with only this JSON object: {"translations": [{"i": <number>, "t": "<translation>"}]}, '
    'with one entry per input record, reusing its "i".\n'
    '- Translate only each record\'s "text". Context, glossary and character notes are reference material.\n'
    '- Each translation is only the line itself. Never add the speaker\'s name, a label or a "Name:" prefix; '
    '"speaker" only tells you who is talking.\n'
    "- Use “ (opening) and ” (closing) when converting source 「…」 or 『…』 quotation marks. "
    "Leave unquoted narration unquoted.\n"
    "- Keep every placeholder such as ⟦0⟧ exactly once and in the same order. Placeholders stand for game "
    "commands: never translate, remove or add them.\n"
    "- Use \\n for a line break inside a translation. Speaker names, titles and choices stay on one line.\n"
    "- Use the glossary and character translations whenever those names or terms appear."
)

THINKING = (
    'Before translating, think in the reply\'s "why" field, which comes before "translations" in the JSON '
    "object. Keep it extremely concise and structured:\n"
    "- Context: context, references, tone and who is speaking to whom that affect the meaning\n"
    "- Decisions: how these instructions apply to the key constraints, ambiguities or difficult lines"
)

REVIEW_ROLE = (
    "You proofread existing {target} translations of {source} visual novel scripts.\n"
    "Preserve fluent wording and make only the smallest correction supported by the source and context."
)

REVIEW_ERRORS = {
    "meaning": "Mistranslation",
    "omission": "Missing content",
    "addition": "Added content",
    "name": "Name or term",
    "honorific": "Honorific",
    "grammar": "Grammar",
    "quotes": "Quotes",
}

REVIEW = (
    "Review task:\n"
    "- Most translations are already acceptable: keep them exactly as written and leave them out of the "
    "reply. Review the existing translation rather than translating the source again. When unsure, leave "
    "the record out.\n"
    '- Report a record only for one of these errors, given as "c": meaning (the translation says something '
    "the source does not), omission (source content is missing), addition (content absent from the source), "
    "name (a wrong name or glossary term), honorific (an honorific or form of address translated, dropped or "
    "changed), grammar (clearly broken {target}), quotes (quotation marks contradict the source). Style, tone, "
    "flow, naturalness, brevity and word choice are never errors, and an alternative phrasing is not a "
    "correction.\n"
    "- Fix only the wrong words and copy the rest of the line unchanged. Never shorten, compress, formalize "
    "or tidy a line: casual register, contractions, slang, fillers, interjections, stammering, repetition, "
    "sentence fragments and implicit subjects are deliberate. Preserve the existing register, rhythm, point "
    "of view and sentence structure. Do not invent a subject, pronoun, relationship or emotion that the "
    "source and context do not establish.\n"
    "- The kind 'dialogue' can include narration and internal monologue. It and the speaker metadata never "
    "require quotation marks. Use “ (opening) and ” (closing) for source quotations. "
    "Preserve the source's quotation structure: 「うん」 needs quotes around the "
    "translated speech; うん and unquoted narration do not. Keep quotations within narration in their place; "
    "do not wrap the whole narration in quotes.\n"
    '- Reply with only this JSON object: {"translations": [{"i": <number>, "c": "<error>", '
    '"t": "<corrected translation>", "r": "<short reason>"}]}. Use an empty list when nothing needs fixing.\n'
    "- The reason names the source words and the concrete error, e.g. 'the source 晶穂 names Akiho, not "
    "Akio'. Generic claims such as 'more natural', 'better nuance', 'smoother' or 'matches dialogue format' "
    "are not reasons.\n"
    "- Keep every placeholder such as ⟦0⟧ exactly once and in the same order. Use \\n for line breaks.\n"
    '- Remove an added "Name:" speaker label absent from the source. Keep character names used as part of '
    "the sentence when the source mentions them; a sentence beginning with a name is not itself an error."
)

REVIEW_HONORIFICS = (
    "- Keep honorifics and forms of address exactly as the translation has them (Onii-chan, Nii-san, "
    "Sayu-chan, Saigawa-san); never replace them with Brother, Sister or a bare name. When the source has "
    "one that the translation translated or dropped, restore it romanized as an honorific error."
)

DISCOVER = (
    "You prepare a translation glossary for a {source} visual novel that will be translated into {target}.\n"
    "From the text below, list character names, nicknames, places, organizations, invented terms and other "
    "expressions that must be translated the same way every time. Skip ordinary words. List names "
    "without honorific suffixes (紗有, not 紗有ちゃん).\n"
    'Reply with only this JSON object: {"terms": [{"source": "<exact text as written>", '
    '"target": "<{target} translation>", "kind": "character" or "term", "notes": "<gender, role or meaning>"}]}. '
    "Use an empty list when there is nothing to add."
)


def chain(config: dict) -> list[dict]:
    """Stored section order and settings over the defaults; output rules always come last."""
    result, seen = [], set()
    for item in config.get("sections", []):
        key = item.get("key") if isinstance(item, dict) else None
        if key in BY_KEY and key != "contract" and key not in seen:
            seen.add(key)
            text = item.get("text")
            result.append(
                dict(
                    key=key,
                    enabled=bool(item.get("enabled", True)),
                    text=text
                    if isinstance(text, str) and BY_KEY[key].editable
                    else None,
                )
            )
    result += [
        dict(key=s.key, enabled=True, text=None)
        for s in SECTIONS
        if s.key not in seen and s.key != "contract"
    ]
    return result + [dict(key="contract", enabled=True, text=None)]


def keeps_honorifics(sections, languages) -> bool:
    """Japanese honorifics stay romanized in Latin-script targets unless an enabled style section drops
    them, as the Localize preset does."""
    if not languages["source"].startswith("Japanese") or languages["target"].startswith(
        ("Chinese", "Japanese", "Korean")
    ):
        return False
    style = next((item for item in sections if item["key"] == "style"), None)
    if style is None or not style["enabled"]:
        return True
    text = style["text"] if style["text"] is not None else DEFAULTS["style"]
    return "drop honorifics" not in text.casefold()


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
        if current and (
            (row.file, row.scene) != key
            or len(current) >= lines
            or size + cost > tokens
        ):
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
        while (
            0 <= index < len(rows)
            and len(found) < limit
            and (rows[index].file, rows[index].scene) == key
        ):
            if rows[index].kind != "speaker" and index not in members:
                found.append(rows[index])
            index += step
        return found

    return collect(batch[0] - 1, -1, before)[::-1], collect(batch[-1] + 1, 1, after)


def shown(row, formats) -> str:
    """The source a model sees: ruby readings left out (KS ruby keeps its base text), other markers kept."""
    if "⟦" not in row.source:
        return row.source
    return plain(
        row.source,
        json.loads(row.context).get("controls", []),
        formats.get(row.file, ""),
    )


def line(row, translated: bool, formats=None) -> str:
    """A context line. The speaker is bracketed, never written as "Name: text", which models copy into replies."""
    text = shown(row, formats or {}).replace("\n", "\\n")
    if row.speaker and row.kind != "speaker":
        text = f"[{row.speaker}] {text}"
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
    thinking: bool = False

    @property
    def tokens(self) -> int:
        return estimate(self.system) + estimate(self.user)


def fill(text, languages):
    return text.replace("{source}", languages["source"]).replace(
        "{target}", languages["target"]
    )


def build(
    sections,
    languages,
    rows,
    batch,
    *,
    terms=(),
    before=5,
    after=2,
    task="translate",
    pre=(),
    formats=None,
) -> Prompt:
    """task: "translate" or "review"; `pre` replacement rules rewrite the source text the model sees.

    formats maps each file to its script format, which tells which markers are ruby."""
    formats = formats or {}
    records = [rows[index] for index in batch]
    texts = {id(row): shown(row, formats) for row in records}
    searched = list(texts.values()) + [row.speaker for row in records if row.speaker]
    matched = glossary.match(terms, searched)
    honorifics = keeps_honorifics(sections, languages)
    if honorifics:
        matched += [
            glossary.Term(source, target, "character", "keep this form of address")
            for source, target in glossary.Address(terms)
            .found("\n".join(searched))
            .items()
        ]
    earlier, later = neighbours(rows, batch, before, after)

    def entry(term):
        return f"- {term.source} → {term.target}" + (
            f" ({term.notes.strip()})" if term.notes.strip() else ""
        )

    system, user, thinking = [], [], False
    for item in sections:
        section = BY_KEY[item["key"]]
        if not item["enabled"]:
            continue
        if section.editable:
            default = (
                REVIEW_ROLE
                if task == "review" and section.key == "role"
                else DEFAULTS[section.key]
            )
            text = fill(
                item["text"] if item["text"] is not None else default, languages
            ).strip()
        elif section.key == "contract":
            text = (
                CONTRACT
                if task == "translate"
                else fill(
                    REVIEW + ("\n" + REVIEW_HONORIFICS if honorifics else ""),
                    languages,
                )
            )
        elif section.key == "thinking":
            thinking = task == "translate"
            text = THINKING if thinking else ""
        elif section.key == "characters":
            found = [entry(term) for term in matched if term.kind == "character"]
            text = "Characters:\n" + "\n".join(found) if found else ""
        elif section.key == "glossary":
            found = [entry(term) for term in matched if term.kind != "character"]
            text = "Glossary:\n" + "\n".join(found) if found else ""
        else:
            parts = []
            if earlier:
                parts.append(
                    "Earlier lines (reference only):\n"
                    + "\n".join(line(row, True, formats) for row in earlier)
                )
            if later:
                parts.append(
                    "Later lines (reference only):\n"
                    + "\n".join(line(row, task == "review", formats) for row in later)
                )
            text = "\n\n".join(parts)
        if text:
            (system if section.role == "system" else user).append(text)

    def record(number, row):
        value = dict(i=number, kind=row.kind)
        if row.speaker and row.kind != "speaker":
            value["speaker"] = row.speaker
        value["text"] = glossary.Rules.apply(pre, texts[id(row)])
        if task == "review":
            value["translation"] = row.translation
        return json.dumps(value, ensure_ascii=False)

    heading = (
        f"Translate these {len(records)} records into {languages['target']}:"
        if task == "translate"
        else f"Review these {len(records)} translations:"
    )
    user.append(
        heading
        + "\n"
        + "\n".join(record(number, row) for number, row in enumerate(records, 1))
    )
    system_text = "\n\n".join(system)
    return Prompt(
        system_text,
        "\n\n".join(user),
        records,
        matched,
        hashlib.sha256(system_text.encode()).hexdigest()[:12],
        len(earlier) + len(later),
        thinking,
    )


def discovery(languages, rows, batch, formats=None) -> Prompt:
    records = [rows[index] for index in batch]
    system = fill(DISCOVER, languages)
    user = "Text:\n" + "\n".join(line(row, False, formats) for row in records)
    return Prompt(
        system, user, records, [], hashlib.sha256(system.encode()).hexdigest()[:12]
    )
