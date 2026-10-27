"""Protect engine controls and wrap visible text without splitting them."""

import re
import unicodedata
import warnings

from .model import Unit, VnTransError

MARKER = re.compile(r"⟦(\d+)⟧")
SCN_CONTROL = re.compile(
    r"\\.|\[[^\]\r\n]+\]|%f[^;]*;|%[bise].|%\d+;|%[BSrCRL]|"
    r"%[pdwD]\d*;|%D\$[^;]*;|\#[^;\r\n]*;|\$[^;\r\n]*;|&[^;\r\n]*;"
)
KS_CONTROL = re.compile(r'''\[\[|\[(?:[^\]"']|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')*\]''')


def make_unit(id: str, kind: str, raw: str, format: str, **context) -> Unit:
    if "⟦" in raw or "⟧" in raw:
        raise VnTransError(f"{id}: source contains reserved placeholder delimiters")
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    if context.get("literal"):
        return Unit(id, kind, normalized, dict(context, original=raw, controls=[]))
    if format == "scn":
        pattern = SCN_CONTROL
    else:
        pattern = KS_CONTROL
    controls = []

    def replace(match):
        if format == "scn" and match[0] == r"\n":
            return "\n"
        controls.append(match[0])
        return f"⟦{len(controls) - 1}⟧"

    if format == "ks":
        parts = []
        pos = 0
        for match in pattern.finditer(normalized):
            if match.start() < pos:
                continue
            end = match.end()
            if match[0] == "[r]":
                parts.append(normalized[pos:match.start()] + "\n")
                pos = end
                continue
            if re.match(r"\[ruby(?:\s|\])", match[0], re.I):
                count = re.search(r"\bchar\s*=\s*['\"]?(\d+)", match[0], re.I)
                size = int(count[1]) if count else 1
                if end + size > len(normalized):
                    raise VnTransError(f"{id}: ruby extends beyond text span")
                end += size
            parts.append(normalized[pos:match.start()])
            controls.append(normalized[match.start():end])
            parts.append(f"⟦{len(controls) - 1}⟧")
            pos = end
        parts.append(normalized[pos:])
        source = "".join(parts)
    else:
        source = pattern.sub(replace, normalized)
    context = dict(context, original=raw, controls=controls)
    return Unit(id, kind, source, context)


def width(text: str) -> int:
    return sum(0 if unicodedata.combining(c) else
               2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def wrap(text: str, columns: int, controls: list[str], format: str = "scn") -> str:
    """Wrap at spaces only; protected constructs are indivisible."""
    def visible_width(word):
        def visible(match):
            control = controls[int(match[1])]
            if format == "ks":
                if control == "[[":
                    return "["
                if re.match(r"\[ruby(?:\s|\])", control, re.I):
                    return control[control.index("]") + 1:]
                return ""
            if control.startswith("\\"):
                return control[1:]
            # shortcut: opaque SCN ruby syntax uses annotation width as an
            # estimate; add game-specific ruby metrics for pixel wrapping.
            return control[1:-1] if control.startswith("[") else ""
        return width(MARKER.sub(visible, word))

    lines = []
    for line in text.split("\n"):
        parts = re.findall(r"\S+|[^\S\n]+", line)
        current = ""
        pending = ""
        for part in parts:
            if part.isspace():
                pending += part
                continue
            if visible_width(part) > columns:
                warnings.warn(f"unbreakable word exceeds {columns} columns: {part!r}", stacklevel=2)
            if current and visible_width(current + pending + part) > columns:
                lines.append(current)
                current = part
            else:
                current += pending + part
            pending = ""
        lines.append(current + pending)
    return "\n".join(lines)


def render(unit: Unit, translated: str, format: str, columns: int | None = None) -> str:
    try:
        translated.encode("utf-8")
    except UnicodeError as exc:
        raise VnTransError(f"{unit.id}: invalid Unicode translation") from exc
    controls = unit.context["controls"]
    expected = [str(i) for i in range(len(controls))]
    if MARKER.findall(translated) != expected:
        raise VnTransError(f"{unit.id}: protected placeholders must appear once, in original order")
    plain = MARKER.sub("", translated)
    if "⟦" in plain or "⟧" in plain or "\x00" in plain or "\r" in plain:
        raise VnTransError(f"{unit.id}: invalid placeholder, NUL, or carriage return")
    if unit.kind in ("speaker", "title") and "\n" in translated:
        raise VnTransError(f"{unit.id}: speaker/title cannot contain line breaks")
    if format == "scn" and (SCN_CONTROL.search(plain) or "\\" in plain):
        raise VnTransError(f"{unit.id}: new engine controls must not be introduced in translations")
    if columns and unit.kind == "dialogue":
        translated = wrap(translated, columns, controls, format)
    if format == "ks":
        if not unit.context.get("literal"):
            translated = translated.replace("[", "[[")
        translated = translated.replace("\n", "[r]")
    else:
        translated = translated.replace("\n", r"\n")
    return MARKER.sub(lambda m: controls[int(m[1])], translated)
