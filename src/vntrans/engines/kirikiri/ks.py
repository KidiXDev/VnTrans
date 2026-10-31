"""A span-preserving KAG scanner. Unknown commands are never evaluated."""

import codecs
import re

from ...model import VnTransError
from ...text import make_unit, render

# Game conventions belong here; never extract every attribute of a known tag.
ATTRIBUTES = {
    "name": ("speaker", {"name", "text"}),
    "speaker": ("speaker", {"name", "text"}),
    "nm": ("speaker", {"t", "name", "text"}),
    "cn": ("speaker", {"name", "text"}),
    "talk": ("speaker", {"name", "text"}),
    "名前": ("speaker", {"name", "text"}),
    "set_title": ("title", {"text", "title"}),
    "button": ("choice", {"text", "caption"}),
    "glink": ("choice", {"text"}),
    "addselect": ("choice", {"text", "caption"}),
    **{f"sel0{i}": ("choice", {"text", "caption"}) for i in range(1, 5)},
}
ATTRIBUTE = re.compile(r'''([^\s=]+)\s*=\s*("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s]+)''')


def command(text: str):
    match = re.match(r"\s*([^\s]+)", text)
    return (match[1].lower(), match.end()) if match else ("", 0)


def tag_end(line: str, start: int) -> int:
    quote = None
    p = start + 1
    while p < len(line):
        char = line[p]
        if quote:
            if char == "\\":
                p += 2
                continue
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "]":
            return p + 1
        p += 1
    raise VnTransError("unterminated KAG tag (multiline tags are unsupported in v1)")


class KsScript:
    format = "ks"

    def __init__(self, data: bytes, encoding: str | None = None):
        self.data = data
        self.bom = b""
        detected = None
        for bom, codec in ((codecs.BOM_UTF8, "utf-8"),
                           (codecs.BOM_UTF16_LE, "utf-16-le"),
                           (codecs.BOM_UTF16_BE, "utf-16-be")):
            if data.startswith(bom):
                self.bom, detected = bom, codec
                break
        try:
            self.encoding = codecs.lookup(encoding or detected or "cp932").name
            # Normalize BOM-aware codec aliases; BOM is tracked independently.
            if self.encoding in ("utf-8-sig", "utf-16"):
                self.encoding = detected or ("utf-8" if self.encoding == "utf-8-sig" else "utf-16-le")
            if detected and self.encoding != detected:
                raise VnTransError("encoding override conflicts with the script BOM")
            self.text = data[len(self.bom):].decode(self.encoding)
        except (LookupError, UnicodeError) as exc:
            raise VnTransError(f"cannot decode KS script: {exc}; specify --encoding") from exc
        if "\x00" in self.text:
            raise VnTransError("NUL in KS script; check encoding or compiled format")
        self.units = []
        self.locations = {}
        self.warnings = []
        self.label = None
        self.speaker = None
        self.block = None
        self.macro_depth = 0
        self.link = False
        offset = 0
        for number, full_line in enumerate(self.text.splitlines(keepends=True), 1):
            line = full_line.rstrip("\r\n")
            self.scan_line(line, offset, number)
            offset += len(full_line)
        if self.block or self.macro_depth:
            raise VnTransError("unterminated script or macro block")
        if not self.units:
            raise VnTransError("KS script contains no supported translatable text")

    def add(self, start, end, kind, line, quote=None):
        raw = self.text[start:end]
        if quote:
            raw = raw.replace("\\" + quote, quote).replace("\\\\", "\\")
        unit = make_unit(f"span/{start}:{end}/{kind}", kind, raw, self.format,
                         line=line, label=self.label, speaker=self.speaker,
                         literal=quote is not None or kind in ("speaker", "title"))
        if not re.sub(r"⟦\d+⟧", "", unit.source).strip():
            return
        self.units.append(unit)
        self.locations[unit.id] = (start, end, quote)
        if kind == "speaker":
            self.speaker = raw
        if any(c.startswith("[ruby") for c in unit.context["controls"]):
            self.warnings.append(f"{unit.id}: ruby spans are protected; translations may leave them out")

    def attributes(self, body, base, line):
        name, end = command(body)
        mapping = ATTRIBUTES.get(name)
        if not mapping:
            return
        kind, allowed = mapping
        for match in ATTRIBUTE.finditer(body, end):
            if match[1].lower() not in allowed:
                continue
            value = match[2]
            if value.startswith(("&", "%")):
                continue
            start, stop = match.span(2)
            quote = value[0] if value.startswith(("'", '"')) else ""
            if quote:
                start += 1
                stop -= 1
                if value[1:].startswith(("&", "%")):
                    continue
            self.add(base + start, base + stop, kind, line, quote)

    def scan_line(self, line, offset, number):
        trimmed = line.lstrip(" \t")
        indent = len(line) - len(trimmed)
        if self.block:
            if re.fullmatch(r"(?:@endscript|\[endscript\])\s*", trimmed, re.I):
                self.block = None
            return
        if self.macro_depth:
            if re.match(r"(?:@macro\b|\[macro\b)", trimmed, re.I):
                self.macro_depth += 1
            elif re.fullmatch(r"(?:@endmacro|\[endmacro\])\s*", trimmed, re.I):
                self.macro_depth -= 1
            return
        if not trimmed or trimmed.startswith(";"):
            return
        if trimmed.startswith("*"):
            self.label = trimmed.split("|", 1)[0]
            if "|" in trimmed:
                p = line.index("|") + 1
                self.add(offset + p, offset + len(line), "title", number)
            return
        if trimmed.startswith("#"):
            self.add(offset + indent + 1, offset + len(line), "speaker", number)
            return
        if trimmed.startswith("@"):
            body = trimmed[1:]
            name, _ = command(body)
            if name == "iscript":
                self.block = "script"
            elif name == "macro":
                self.macro_depth = 1
            elif name == "link":
                self.link = True
            elif name == "endlink":
                self.link = False
            self.attributes(body, offset + indent + 1, number)
            return
        p = indent
        start = p
        kind = "choice" if self.link else "dialogue"
        while p < len(line):
            if line.startswith("[[", p):
                p += 2
                continue
            if line[p] != "[":
                p += 1
                continue
            end = tag_end(line, p)
            body = line[p + 1:end - 1]
            name, _ = command(body)
            if name in ("iscript", "macro"):
                self.add(offset + start, offset + p, kind, number)
                close = re.search(r"\[(endscript|endmacro)\]", line[end:], re.I)
                if close:
                    expected = "endscript" if name == "iscript" else "endmacro"
                    if close[1].lower() != expected:
                        raise VnTransError(f"line {number}: mismatched script/macro block")
                    p = end + close.end()
                    start = p
                    continue
                if name == "iscript":
                    self.block = "script"
                else:
                    self.macro_depth = 1
                return
            if name in ATTRIBUTES:
                self.add(offset + start, offset + p, kind, number)
                self.attributes(body, offset + p + 1, number)
                start = end
            elif name == "link":
                self.add(offset + start, offset + p, kind, number)
                start = p
                kind = "choice"
                self.link = True
            elif name == "endlink":
                self.add(offset + start, offset + end, kind, number)
                start = end
                kind = "dialogue"
                self.link = False
            p = end
        self.add(offset + start, offset + len(line), kind, number)

    def rebuild(self, translations, *, wrap_columns=None, output_encoding=None):
        changes = []
        for unit in self.units:
            translated = translations.get(unit.id)
            if translated is None or translated == unit.source:
                continue
            start, end, quote = self.locations[unit.id]
            value = render(unit, translated, self.format, wrap_columns)
            if quote is not None:
                if value.lstrip().startswith(("&", "%")):
                    raise VnTransError(f"{unit.id}: translation would introduce an attribute expression")
                delimiter = quote or '"'
                value = value.replace("\\", "\\\\").replace(delimiter, "\\" + delimiter)
                if not quote:
                    value = '"' + value + '"'
            line_start = max(self.text.rfind("\n", 0, start), self.text.rfind("\r", 0, start)) + 1
            if unit.kind in ("dialogue", "choice") and not self.text[line_start:start].strip(" \t"):
                if value.lstrip().startswith(("@", ";", "*", "#")):
                    raise VnTransError(f"{unit.id}: translation would introduce a line command")
            changes.append((start, end, value))
        if not changes and output_encoding is None:
            return self.data
        text = self.text
        for start, end, value in sorted(changes, reverse=True):
            text = text[:start] + value + text[end:]
        try:
            encoding = codecs.lookup(output_encoding or self.encoding).name
            bom = self.bom
            if output_encoding:
                bom = b""
                if encoding in ("utf-8-sig", "utf-16", "utf-16-le", "utf-16-be"):
                    bom = codecs.BOM_UTF8 if encoding == "utf-8-sig" else (
                        codecs.BOM_UTF16_BE if encoding == "utf-16-be" else codecs.BOM_UTF16_LE)
                    encoding = "utf-8" if encoding == "utf-8-sig" else (
                        "utf-16-be" if encoding == "utf-16-be" else "utf-16-le")
            return bom + text.encode(encoding)
        except (LookupError, UnicodeError) as exc:
            raise VnTransError(f"cannot encode rebuilt KS script: {exc}; specify --output-encoding") from exc
