"""Project terminology: approved entries are injected only where their source text occurs."""

import json
import re
from dataclasses import asdict, dataclass, field

from ..model import VnTransError

KINDS = ("term", "character")
PLACEHOLDER = re.compile(r"(⟦\d+⟧)")


@dataclass
class Term:
    source: str
    target: str = ""
    kind: str = "term"
    notes: str = ""
    case_sensitive: bool = False
    approved: bool = True
    evidence: list[str] = field(default_factory=list)
    id: int | None = None


def match(terms, texts) -> list[Term]:
    """Approved entries with a target whose source occurs in any text, longest source first.

    Overlapping entries are all kept so the model sees each candidate."""
    folded = [text.casefold() for text in texts]
    hits = []
    for term in terms:
        if not (term.approved and term.source and term.target.strip()):
            continue
        if term.case_sensitive:
            found = any(term.source in text for text in texts)
        else:
            needle = term.source.casefold()
            found = any(needle in text for text in folded)
        if found:
            hits.append(term)
    return sorted(hits, key=lambda term: (-len(term.source), term.source, term.kind))


# Built-in Japanese forms of address
ADDRESS = {
    **dict.fromkeys(
        ("お兄ちゃん", "おにいちゃん", "おにーちゃん", "おにぃちゃん"), "Onii-chan"
    ),
    **dict.fromkeys(("お兄さん", "おにいさん", "おにーさん"), "Onii-san"),
    **dict.fromkeys(("お兄様", "お兄さま", "おにいさま"), "Onii-sama"),
    **dict.fromkeys(("兄ちゃん", "にいちゃん", "にーちゃん"), "Nii-chan"),
    **dict.fromkeys(("兄さん", "にいさん", "にーさん"), "Nii-san"),
    **dict.fromkeys(("兄様", "兄さま", "にいさま"), "Nii-sama"),
    **dict.fromkeys(("兄貴", "アニキ", "あにき"), "Aniki"),
    **dict.fromkeys(
        ("お姉ちゃん", "おねえちゃん", "おねーちゃん", "おねぇちゃん"), "Onee-chan"
    ),
    **dict.fromkeys(("お姉さん", "おねえさん", "おねーさん"), "Onee-san"),
    **dict.fromkeys(("お姉様", "お姉さま", "おねえさま"), "Onee-sama"),
    **dict.fromkeys(("姉ちゃん", "ねえちゃん", "ねーちゃん"), "Nee-chan"),
    **dict.fromkeys(("姉さん", "ねえさん", "ねーさん"), "Nee-san"),
    **dict.fromkeys(("姉様", "姉さま", "ねえさま"), "Nee-sama"),
    **dict.fromkeys(("姉貴", "アネキ", "あねき"), "Aneki"),
    **dict.fromkeys(("先輩", "せんぱい", "センパイ"), "Senpai"),
}
SUFFIXES = {
    "さん": "san",
    "ちゃん": "chan",
    "くん": "kun",
    "君": "kun",
    "様": "sama",
    "さま": "sama",
    "先輩": "senpai",
    "先生": "sensei",
    "殿": "dono",
    "たん": "tan",
}


class Address:
    """Forms of address and character name + honorific pairs, romanized, that the glossary doesn't cover."""

    def __init__(self, terms):
        known = {term.source.casefold() for term in terms if term.target.strip()}
        table = dict(ADDRESS)
        for term in terms:
            if term.kind == "character" and term.approved and term.target.strip():
                for suffix, romaji in SUFFIXES.items():
                    table[term.source + suffix] = f"{term.target.strip()}-{romaji}"
        self.table = {
            source: target
            for source, target in table.items()
            if source.casefold() not in known
        }
        self.pattern = re.compile(
            "|".join(map(re.escape, sorted(self.table, key=len, reverse=True)))
            or "(?!)"
        )

    def found(self, text: str) -> dict[str, str]:
        return {match[0]: self.table[match[0]] for match in self.pattern.finditer(text)}


def loads(data: str) -> list[Term]:
    """Read VnTrans glossary JSON; `src`/`dst`/`info` keys from other tools are accepted too."""
    try:
        values = json.loads(data)
    except json.JSONDecodeError as exc:
        raise VnTransError(f"invalid glossary JSON: {exc}") from exc
    if not isinstance(values, list):
        raise VnTransError("glossary JSON must be an array")
    terms = []
    for value in values:
        if not isinstance(value, dict):
            raise VnTransError("glossary entries must be objects")
        source = value.get("source", value.get("src"))
        target = value.get("target", value.get("dst", ""))
        notes = value.get("notes", value.get("info", ""))
        kind = value.get("kind", "term")
        if (
            not isinstance(source, str)
            or not source.strip()
            or not isinstance(target, str)
            or not isinstance(notes, str)
            or kind not in KINDS
        ):
            raise VnTransError(f"invalid glossary entry: {value!r}")
        terms.append(
            Term(
                source.strip(),
                target.strip(),
                kind,
                notes,
                bool(value.get("case_sensitive", False)),
                bool(value.get("approved", True)),
            )
        )
    return terms


def dumps(terms) -> str:
    return (
        json.dumps(
            [
                {k: v for k, v in asdict(term).items() if k not in ("id", "evidence")}
                for term in terms
            ],
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


RULE = re.compile(r"^(pre|post)\s+(.+?)\s+=>(?:\s(.*))?$")


@dataclass
class Rules:
    """Replacement rules, one per line: `pre|post FIND => REPLACE`; `/FIND/` is a regex.

    `pre` rewrites source text sent to the model, `post` rewrites model output. Both touch
    only visible text, never ⟦n⟧ placeholders."""

    pre: list = field(default_factory=list)
    post: list = field(default_factory=list)

    @classmethod
    def parse(cls, text: str) -> "Rules":
        rules = cls()
        for number, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            found = RULE.match(line)
            if not found:
                raise VnTransError(
                    f"line {number}: expected `pre|post FIND => REPLACE`"
                )
            stage, find, replace = found[1], found[2], found[3] or ""
            if len(find) > 2 and find.startswith("/") and find.endswith("/"):
                try:
                    pattern = re.compile(find[1:-1])
                    pattern.sub(replace, "")
                except re.error as exc:
                    raise VnTransError(f"line {number}: {exc}") from None
                getattr(rules, stage).append((pattern, replace))
            else:
                getattr(rules, stage).append(
                    (re.compile(re.escape(find)), lambda match, value=replace: value)
                )
        return rules

    @staticmethod
    def apply(rules, text: str) -> str:
        if not rules:
            return text
        parts = PLACEHOLDER.split(text)
        for index in range(0, len(parts), 2):
            for pattern, replacement in rules:
                parts[index] = pattern.sub(replacement, parts[index])
        return "".join(parts)
