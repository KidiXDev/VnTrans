"""Review signals: they flag translations for a human and never block a build."""

import json
import re

from ..model import VnTransError
from ..text import MARKER, plain

LABELS = {
    "residue": "Source script left",
    "glossary": "Glossary term missing",
    "same": "Same as source",
    "lines": "Line breaks differ",
    "invalid": "Fails checks",
    "speaker": "Speaker name added",
    "ruby": "Furigana kept",
    "quote": "Quote marks wrong",
}
LABEL = re.compile(r"\s*([^\s:：⟦]+(?: [^\s:：⟦]+){0,2})\s*[:：]\s*")
QUOTE = re.compile(r"[「『“\"'‘（(]")
SPEECH = re.compile(r"(?:\s|⟦\d+⟧)*「[^「」]*」(?:\s|⟦\d+⟧)*")
EDGES = re.compile(r"((?:\s|⟦\d+⟧)*)(.*?)((?:\s|⟦\d+⟧)*)", re.S)
MARKS = "\"“”„«»「」『』"
KANA = re.compile(r"[぀-ヿｦ-ﾟ]")
KANJI = re.compile(r"[㐀-䶿一-鿿]")
HANGUL = re.compile(r"[가-힯]")
LETTERS = {
    "Japanese": re.compile(f"[ぁ-ゖゝゞァ-ヺヽヾㇰ-ㇿｦ-ｯｱ-ﾝ々]|{KANJI.pattern}"),
    "Chinese": KANJI,
    "Korean": re.compile(f"[ㄱ-ㆎ]|{HANGUL.pattern}|{KANJI.pattern}"),
}


def translatable(source: str, language: str) -> bool:
    """False for units without letters of the source language (symbols, digits, ……), like LinguaGacha's prefilter."""
    text = MARKER.sub("", source)
    letters = LETTERS.get(language)
    return bool(letters.search(text)) if letters else any(c.isalpha() for c in text)


def speaker_names(rows, terms=()) -> set[str]:
    """Casefolded character names a model might put before a line: speakers as written and translated,
    and glossary characters."""
    names = {name for row in rows if row.kind == "speaker" for name in (row.source, row.translation) if name}
    names |= {row.speaker for row in rows if row.speaker}
    names |= {name for term in terms if term.kind == "character" for name in (term.source, term.target) if name}
    return {name.strip().casefold() for name in names if name.strip()}


def unprefix(text: str, source: str, names) -> str:
    """`text` without a "Name:" label the model put in front, when the source has none.

    The label goes when it is a known character name, or when both the source and the rest of the
    translation open with a quote, which only a prepended speaker produces."""
    match = LABEL.match(text)
    if not match or LABEL.match(source):
        return text
    rest = text[match.end():]
    if match[1].casefold() in names or (QUOTE.match(source.lstrip()) and QUOTE.match(rest)):
        return rest
    return text


def unruby(text: str, row, formats) -> str:
    """`text` without the ruby markers of `row`'s source: furigana is a Japanese reading aid."""
    if "⟦" not in text:
        return text
    return plain(text, json.loads(row.context).get("controls", []), formats.get(row.file, ""))


def requote(text: str, source: str) -> str:
    """`text` inside “ ” when `source` is one 「…」 quote, replacing wrong or missing outer quote marks.

    Single quotes are left alone (apostrophes), and an odd mark at both ends is ambiguous, so it stays."""
    head, core, tail = EDGES.fullmatch(text).groups()
    if not core or not SPEECH.fullmatch(source):
        return text
    lead, trail = core[0] in MARKS, core[-1] in MARKS
    odd = sum(char in MARKS for char in core) % 2
    if lead and trail and not odd:
        core = core[1:-1]
    elif odd and lead != trail:
        core = core[1:] if lead else core[:-1]
    elif odd:
        return text
    return f"{head}“{core}”{tail}"


def tidy(text: str, row, names, formats) -> str:
    """The automatic fixes for a translation of `row`: no added speaker label, no furigana, “ ” for 「」."""
    if row.kind != "speaker":
        text = unprefix(text, row.source, names)
    return requote(unruby(text, row, formats), row.source)


def residue(target: str):
    """Source scripts that shouldn't survive translation into `target`."""
    if target.startswith("Japanese"):
        return HANGUL
    if target.startswith("Chinese"):
        return re.compile(f"{KANA.pattern}|{HANGUL.pattern}")
    if target.startswith("Korean"):
        return re.compile(f"{KANA.pattern}|{KANJI.pattern}")
    return re.compile(f"{KANA.pattern}|{KANJI.pattern}|{HANGUL.pattern}")


class Checker:
    def __init__(self, check, target: str, terms, names=frozenset(), formats=None):
        self.check = check
        self.names = names
        self.formats = formats or {}
        self.residue = residue(target)
        self.terms = [(term.source if term.case_sensitive else term.source.casefold(), term.case_sensitive,
                       term.target.casefold()) for term in terms
                      if term.approved and term.source and term.target.strip()]

    def issues(self, row) -> list[str]:
        if row.translation is None or row.status == "excluded":
            return []
        found = []
        try:
            self.check(row, row.translation)
        except VnTransError:
            found.append("invalid")
        if self.residue.search(MARKER.sub("", row.translation)):
            found.append("residue")
        if row.translation == row.source and (KANA.search(row.source) or KANJI.search(row.source)):
            found.append("same")
        if row.kind == "dialogue" and row.translation.count("\n") != row.source.count("\n"):
            found.append("lines")
        source, folded = row.source.casefold(), row.translation.casefold()
        if any((needle in row.source if case else needle in source) and target not in folded
               for needle, case, target in self.terms):
            found.append("glossary")
        if row.kind != "speaker" and unprefix(row.translation, row.source, self.names) != row.translation:
            found.append("speaker")
        if unruby(row.translation, row, self.formats) != row.translation:
            found.append("ruby")
        if requote(row.translation, row.source) != row.translation:
            found.append("quote")
        return found
