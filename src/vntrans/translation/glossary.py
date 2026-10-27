"""Project terminology: approved entries are injected only where their source text occurs."""

from dataclasses import asdict, dataclass, field
import json
import re

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
        if not isinstance(source, str) or not source.strip() or not isinstance(target, str) \
                or not isinstance(notes, str) or kind not in KINDS:
            raise VnTransError(f"invalid glossary entry: {value!r}")
        terms.append(Term(source.strip(), target.strip(), kind, notes, bool(value.get("case_sensitive", False)),
                          bool(value.get("approved", True))))
    return terms


def dumps(terms) -> str:
    return json.dumps([{k: v for k, v in asdict(term).items() if k not in ("id", "evidence")} for term in terms],
                      ensure_ascii=False, indent=2) + "\n"


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
                raise VnTransError(f"line {number}: expected `pre|post FIND => REPLACE`")
            stage, find, replace = found[1], found[2], found[3] or ""
            if len(find) > 2 and find.startswith("/") and find.endswith("/"):
                try:
                    pattern = re.compile(find[1:-1])
                    pattern.sub(replace, "")
                except re.error as exc:
                    raise VnTransError(f"line {number}: {exc}") from None
                getattr(rules, stage).append((pattern, replace))
            else:
                getattr(rules, stage).append((re.compile(re.escape(find)), lambda match, value=replace: value))
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
