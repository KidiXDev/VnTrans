"""HexenHaus NORI 1.0 bytecode and its length-prefixed string references."""

import warnings
from binascii import crc_hqx
from codecs import lookup
from struct import iter_unpack, pack, pack_into, unpack_from

from ...model import VnTransError
from ...text import NORI_ENCODING as ENCODING
from ...text import make_unit, render, width, wrap

NORI = b"NORI\0\0\x01\0"
FORMAT = "nori"
XOR = bytes(value ^ 0x53 for value in range(256))
MAX_DIALOGUE_LINES = 3


def crc(data):
    return crc_hqx(data, 0).to_bytes(2, "big")


def check_encoding(encoding):
    if encoding is None:
        return
    try:
        name = lookup(encoding).name
    except LookupError as exc:
        raise VnTransError(f"unknown encoding: {encoding}") from exc
    if name != ENCODING:
        raise VnTransError("HexenHaus scripts use CP932")


def wrap_dialogue(lines, columns, slots, unit_id):
    if len(lines) <= slots and max(map(width, lines)) <= columns:
        return lines
    paragraph = " ".join(lines)
    lower = max(columns, max(map(width, paragraph.split()), default=0))
    upper = max(lower, width(paragraph))
    # Widen only as far as needed to keep all words within the display's line count.
    while lower < upper:
        middle = (lower + upper) // 2
        if wrap(paragraph, middle, [], FORMAT).count("\n") < slots:
            upper = middle
        else:
            lower = middle + 1
    lines = wrap(paragraph, lower, [], FORMAT).split("\n")
    longest = max(map(width, lines))
    if longest > columns:
        warnings.warn(
            f"{unit_id}: needs {longest} columns with {slots} NORI line slot(s); target is {columns}",
            stacklevel=2,
        )
    return lines


def relocate_labels(header, offsets):
    position = 42
    for count, size in iter_unpack("<II", header[8:32]):
        end = position + size
        original = bytearray()
        updated = bytearray()
        for _ in range(count):
            if position + 8 > end:
                raise VnTransError("truncated NORI label")
            offset, encoded_length = unpack_from("<II", header, position)
            length = encoded_length ^ 0x53535353
            name_end = position + 8 + length
            if name_end > end or offset % 8 or offset // 8 >= len(offsets):
                raise VnTransError("invalid NORI label")
            name = bytes(header[position + 8 : name_end]).translate(XOR)
            address = offsets[offset // 8]
            original += pack("<II", offset, length) + name
            updated += pack("<II", address, length) + name
            pack_into("<I", header, position, address)
            position = name_end
        if position != end or crc(original) != header[end : end + 2]:
            raise VnTransError("invalid NORI label table checksum or size")
        header[end : end + 2] = crc(updated)
        position = end + 2


class NoriScript:
    format = FORMAT
    encoding = ENCODING

    def __init__(self, data, *, prefix="", scene=None):
        self.data = data
        self.units = []
        self.warnings = []
        self.fields = {}
        self.dialogue_commands = {}
        self.references = {}
        if len(data) < 52 or not data.startswith(NORI):
            raise VnTransError("expected a NORI 1.0 script")
        code_size, pool_size = unpack_from("<II", data, 32)
        self.pool_start = len(data) - 2 - pool_size
        self.code_start = self.pool_start - 2 - code_size
        section_sizes = unpack_from("<I4xI4xI", data, 12)
        if self.code_start != 48 + sum(section_sizes) or code_size % 8:
            raise VnTransError("invalid NORI section sizes")
        code = data[self.code_start : self.pool_start - 2]
        pool = data[self.pool_start : -2]
        if (
            crc(data[:40]) != data[40:42]
            or crc(code) != data[self.pool_start - 2 : self.pool_start]
            or crc(pool) != data[-2:]
        ):
            raise VnTransError("invalid NORI checksum")

        self.pool = pool.translate(XOR)
        slots = list(iter_unpack("<HHI", code))
        end = 0
        for index, (opcode, length, offset) in enumerate(slots):
            if opcode not in (3, 5):
                continue
            # Type 3 stores eight bytes of message metadata in the same pool.
            size = 8 if opcode == 3 else length
            if offset != end or offset + size > len(pool):
                raise VnTransError("unsupported NORI string pool reference")
            end += size
            self.references[index] = (offset, end)
        if end != len(pool):
            raise VnTransError("incomplete NORI string pool references")

        for index, (opcode, count, flags) in enumerate(slots):
            if opcode not in (1, 0x3C, 0x66):
                continue
            arguments = slots[index + 1 : index + 1 + count]
            record = f"{prefix}{index}"
            if opcode == 0x66 and (
                count
                and len(arguments) == count
                and all(arg[0] == 5 for arg in arguments)
            ):
                for field in range(index + 1, index + 1 + count):
                    self._unit(
                        f"{record}/choice-{field - index}", "choice", [field], scene
                    )
            elif (
                opcode in (1, 0x3C)
                and flags == count - 1
                and (
                    2 <= count <= 5
                    and len(arguments) == count
                    and arguments[0][:2] in ((3, 0), (3, 1))
                    and all(arg[0] == 5 for arg in arguments[1:])
                )
            ):
                first = index + 2
                if arguments[0][1]:
                    self._unit(f"{record}/speaker", "speaker", [first], scene)
                    first += 1
                if first < index + 1 + count:
                    self._unit(
                        f"{record}/dialogue",
                        "dialogue",
                        range(first, index + 1 + count),
                        scene,
                    )
                    self.dialogue_commands[f"{record}/dialogue"] = index

    def _unit(self, id, kind, fields, scene):
        lines = []
        for field in fields:
            start, end = self.references[field]
            try:
                line = self.pool[start:end].decode(ENCODING)
            except UnicodeError as exc:
                raise VnTransError(f"{id}: invalid CP932 text") from exc
            if any(char in line for char in "\x00\r\n"):
                raise VnTransError(f"{id}: unexpected line control in NORI text")
            lines.append(line)
        if any(line.strip() for line in lines):
            self.units.append(
                make_unit(id, kind, "\n".join(lines), FORMAT, literal=True, scene=scene)
            )
            self.fields[id] = fields

    def rebuild(self, translations, *, wrap_columns=None, output_encoding=None):
        check_encoding(output_encoding)
        replacements = {}
        added_lines = {}
        added_counts = {}
        for unit in self.units:
            value = translations.get(unit.id)
            if value is None or value == unit.source:
                continue
            rendered = render(unit, value, FORMAT)
            if rendered != value:
                warnings.warn(
                    f"{unit.id}: normalized text for NORI/CP932 in the patch",
                    stacklevel=2,
                )
            lines = rendered.split("\n")
            fields = self.fields[unit.id]
            if wrap_columns is not None and unit.kind == "dialogue":
                lines = wrap_dialogue(
                    lines, wrap_columns, max(len(fields), MAX_DIALOGUE_LINES), unit.id
                )
            elif len(lines) > len(fields):
                warnings.warn(
                    f"{unit.id}: joined extra line breaks to fit {len(fields)} NORI line slot(s)",
                    stacklevel=2,
                )
                last = len(fields) - 1
                lines[last:] = [" ".join(lines[last:])]
            lines.extend([""] * (len(fields) - len(lines)))
            encoded = [line.encode(ENCODING) for line in lines]
            if any(len(line) > 0xFFFF for line in encoded):
                raise VnTransError(f"{unit.id}: a NORI line exceeds 65535 bytes")
            replacements.update(zip(fields, encoded[: len(fields)], strict=True))
            if extra := encoded[len(fields) :]:
                added_lines[fields[-1]] = extra
                added_counts[self.dialogue_commands[unit.id]] = len(extra)
        if not replacements:
            return self.data

        header = bytearray(self.data[: self.pool_start - 2])
        pool = bytearray()
        inserted_code = {}
        for index, extra in added_counts.items():
            slot = self.code_start + index * 8
            opcode, count, flags = unpack_from("<HHI", header, slot)
            pack_into("<HHI", header, slot, opcode, count + extra, flags + extra)
        for index, (start, end) in self.references.items():
            slot = self.code_start + index * 8
            pack_into("<I", header, slot + 4, len(pool))
            value = replacements.get(index)
            if value is not None:
                pack_into("<H", header, slot + 2, len(value))
            span = self.pool[start:end] if value is None else value
            if len(pool) + len(span) > 0xFFFFFFFF:
                raise VnTransError("NORI string pool exceeds its size field")
            pool += span
            if extra := added_lines.get(index):
                operands = bytearray()
                for line in extra:
                    if len(pool) + len(line) > 0xFFFFFFFF:
                        raise VnTransError("NORI string pool exceeds its size field")
                    operands += pack("<HHI", 5, len(line), len(pool))
                    pool += line
                inserted_code[index] = operands
        if inserted_code:
            code = memoryview(header)[self.code_start :]
            relocated = bytearray()
            offsets = []
            for index, pos in enumerate(range(0, len(code), 8)):
                offsets.append(len(relocated))
                relocated += code[pos : pos + 8]
                if extra_code := inserted_code.get(index):
                    relocated += extra_code
            header = header[: self.code_start]
            relocate_labels(header, offsets)
            pack_into("<I", header, 32, len(relocated))
            header += relocated
        pack_into("<I", header, 36, len(pool))
        header[40:42] = crc(header[:40])
        encoded_pool = pool.translate(XOR)
        return bytes(
            header + crc(header[self.code_start :]) + encoded_pool + crc(encoded_pool)
        )
