"""Review signals: they flag translations for a human and never block a build."""

import re

from ..model import VnTransError
from ..text import MARKER

LABELS = {
    "residue": "Source script left",
    "glossary": "Glossary term missing",
    "same": "Same as source",
    "lines": "Line breaks differ",
    "invalid": "Fails checks",
}
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
    def __init__(self, check, target: str, terms):
        self.check = check
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
        return found
