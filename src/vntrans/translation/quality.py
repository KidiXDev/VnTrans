"""Review signals: they flag translations for a human and never block a build."""

import json
import re
import unicodedata
from difflib import SequenceMatcher

from ..model import VnTransError
from ..text import MARKER, plain
from .glossary import ADDRESS, SUFFIXES, Address

LABELS = {
    "residue": "Source script left",
    "glossary": "Glossary term missing",
    "same": "Same as source",
    "lines": "Line breaks differ",
    "invalid": "Fails checks",
    "speaker": "Speaker name added",
    "ruby": "Furigana kept",
    "quote": "Quote marks wrong",
    "honorific": "Honorific missing",
}
HONORIFIC = re.compile(
    r"\b[^\W\d_]+-(?:san|chan|kun|sama|senpai|sensei|dono|tan)\b|\b(?:aniki|aneki|senpai|sensei)\b",
    re.IGNORECASE,
)
ROMAJI = {
    kana: spelled
    for row, spellings in (
        ("あいうえお", "a i u e o"),
        ("かきくけこ", "ka ki ku ke ko"),
        ("さしすせそ", "sa shi su se so"),
        ("たちつてと", "ta chi tsu te to"),
        ("なにぬねの", "na ni nu ne no"),
        ("はひふへほ", "ha hi fu he ho"),
        ("まみむめも", "ma mi mu me mo"),
        ("やゆよ", "ya yu yo"),
        ("らりるれろ", "ra ri ru re ro"),
        ("わをん", "wa o n"),
        ("がぎぐげご", "ga gi gu ge go"),
        ("ざじずぜぞ", "za ji zu ze zo"),
        ("だぢづでど", "da ji zu de do"),
        ("ばびぶべぼ", "ba bi bu be bo"),
        ("ぱぴぷぺぽ", "pa pi pu pe po"),
        ("ぁぃぅぇぉ", "a i u e o"),
        ("ゃゅょゎゔ", "ya yu yo wa vu"),
    )
    for kana, spelled in zip(row, spellings.split(), strict=True)
}
HIRAGANA = re.compile(r"[ぁ-ゖー]+")
LONG_VOWEL = re.compile(r"ou|oo|([aeiu])\1")
KEPT_KANA = sorted((*ADDRESS, *SUFFIXES), key=len, reverse=True)
LABEL = re.compile(r"\s*([^\s:：⟦]+(?: [^\s:：⟦]+){0,2})\s*[:：]\s*")
QUOTE = re.compile(r"[「『“\"'‘（(]")
SPEECH = re.compile(r"(?:\s|⟦\d+⟧)*(?:「[^「」]*」|『[^『』]*』)(?:\s|⟦\d+⟧)*")
EDGES = re.compile(r"((?:\s|⟦\d+⟧)*)(.*?)((?:\s|⟦\d+⟧)*)", re.S)
MARKS = '"“”„«»「」『』'
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
    names = {
        name
        for row in rows
        if row.kind == "speaker"
        for name in (row.source, row.translation)
        if name
    }
    names |= {row.speaker for row in rows if row.speaker}
    names |= {
        name
        for term in terms
        if term.kind == "character"
        for name in (term.source, term.target)
        if name
    }
    return {name.strip().casefold() for name in names if name.strip()}


def unprefix(text: str, source: str, names) -> str:
    """`text` without a "Name:" label the model put in front, when the source has none.

    The label goes when it is a known character name, or when both the source and the rest of the
    translation open with a quote, which only a prepended speaker produces."""
    match = LABEL.match(text)
    if not match or LABEL.match(source):
        return text
    rest = text[match.end() :]
    if match[1].casefold() in names or (
        QUOTE.match(source.lstrip()) and QUOTE.match(rest)
    ):
        return rest
    return text


def unruby(text: str, row, formats) -> str:
    """`text` without the ruby markers of `row`'s source: furigana is a Japanese reading aid."""
    if "⟦" not in text:
        return text
    return plain(
        text, json.loads(row.context).get("controls", []), formats.get(row.file, "")
    )


def requote(text: str, source: str) -> str:
    """`text` inside “ ” when `source` is one 「…」 or 『…』 quote, replacing wrong or missing outer marks.

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
    """The automatic fixes for a translation of `row`: no added speaker label, no furigana, “ ” for source quotes."""
    if row.kind != "speaker":
        text = unprefix(text, row.source, names)
    return requote(unruby(text, row, formats), row.source)


def _unquote(text: str) -> str:
    head, core, tail = EDGES.fullmatch(text).groups()
    if len(core) >= 2 and core[0] + core[-1] in (
        '""',
        "“”",
        "„“",
        "„”",
        "«»",
        "「」",
        "『』",
    ):
        return head + core[1:-1] + tail
    return text


def review_text(text: str, row, names, formats) -> str:
    """Normalize a revision without introducing outer quotes absent from both source and current text."""
    text = tidy(text, row, names, formats)
    current = row.translation or ""
    if _unquote(row.source) == row.source and _unquote(current) == current:
        text = _unquote(text)
    return text


def oversteps(current: str, revision: str, error: str, honorifics: bool) -> bool:
    """A revision that trims the line by over a fifth without a meaning or added-content error, or that
    loses a romanized honorific the current translation has."""
    if len(revision) < 0.8 * len(current) and error not in ("meaning", "addition"):
        return True
    return (
        honorifics
        and error != "addition"
        and len(HONORIFIC.findall(revision)) < len(HONORIFIC.findall(current))
    )


def romaji(kana: str) -> str:
    """Hepburn spelling of hiragana: small っ doubles the next consonant, small ゃゅょ join the syllable."""
    spelled, double = [], False
    for char in kana:
        if char == "っ":
            double = True
            continue
        if char in "ゃゅょ" and spelled and spelled[-1].endswith("i"):
            last = spelled.pop()
            stem = last[:-1] if last.endswith(("shi", "chi", "ji")) else last[:-1] + "y"
            spelled.append(stem + ROMAJI[char][-1])
            continue
        syllable = ROMAJI.get(char, "")
        if double and syllable:
            syllable = syllable[0] + syllable
        double = False
        spelled.append(syllable)
    return "".join(spelled)


def _letters(text: str) -> str:
    """Latin letters only, without accents and with long vowels folded, so spelling variants compare equal."""
    plain_text = unicodedata.normalize("NFKD", text.casefold())
    return LONG_VOWEL.sub(
        lambda match: match[1] or "o",
        "".join(char for char in plain_text if "a" <= char <= "z"),
    )


def romanized(source: str, text: str) -> bool:
    """True when `text` spells most of the source's hiragana out in Latin letters instead of translating it.

    Honorifics and forms of address are left out: keeping them romanized is intended."""
    for kana in KEPT_KANA:
        source = source.replace(kana, " ")
    spelled = _letters("".join(map(romaji, HIRAGANA.findall(source))))
    if len(spelled) < 6:
        return False
    blocks = SequenceMatcher(
        None, spelled, _letters(text), autojunk=False
    ).get_matching_blocks()
    return 2 * sum(block.size for block in blocks if block.size >= 3) >= len(spelled)


def unfit(revision: str, current: str, source: str, script) -> str:
    """Why `revision` can't replace `current`, or "" when it can: it brings back source-language script,
    copies the source, or romanizes it. `script` is the pattern from `residue()`."""
    text, before = MARKER.sub("", revision), MARKER.sub("", current)
    original = MARKER.sub("", source).strip()
    if script.search(text) and not script.search(before):
        return "it leaves source-language text in the line"
    if text.strip() == original and before.strip() != original:
        return "it copies the source text"
    if romanized(source, text) and not romanized(source, before):
        return "it romanizes the source instead of translating it"
    return ""


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
    def __init__(
        self,
        check,
        target: str,
        terms,
        names=frozenset(),
        formats=None,
        honorifics=False,
    ):
        self.check = check
        self.names = names
        self.formats = formats or {}
        self.residue = residue(target)
        self.honorifics = honorifics
        self.address = Address(terms) if honorifics else None
        self.terms = [
            (
                term.source if term.case_sensitive else term.source.casefold(),
                term.case_sensitive,
                term.target.casefold(),
            )
            for term in terms
            if term.approved and term.source and term.target.strip()
        ]

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
        if row.translation == row.source and (
            KANA.search(row.source) or KANJI.search(row.source)
        ):
            found.append("same")
        if row.kind == "dialogue" and row.translation.count("\n") != row.source.count(
            "\n"
        ):
            found.append("lines")
        source, folded = row.source.casefold(), row.translation.casefold()
        if any(
            (needle in row.source if case else needle in source)
            and target not in folded
            for needle, case, target in self.terms
        ):
            found.append("glossary")
        if (
            row.kind != "speaker"
            and unprefix(row.translation, row.source, self.names) != row.translation
        ):
            found.append("speaker")
        if unruby(row.translation, row, self.formats) != row.translation:
            found.append("ruby")
        if requote(row.translation, row.source) != row.translation:
            found.append("quote")
        if self.address and any(
            target.casefold() not in folded
            for target in self.address.found(row.source).values()
        ):
            found.append("honorific")
        return found
