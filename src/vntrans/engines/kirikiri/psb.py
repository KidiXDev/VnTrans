"""PSB v3 codec, retaining original scalar bytes and string/resource identities."""

from dataclasses import dataclass
import struct
import zlib

from ...model import VnTransError


@dataclass
class Node:
    tag: int
    value: object
    raw: bytes | None = None

    @classmethod
    def string(cls, value: str):
        return cls(0x15, value)


def pack_array(values: list[int]) -> bytes:
    count_width = max(1, (len(values).bit_length() + 7) // 8)
    width = max(1, (max(values, default=0).bit_length() + 7) // 8)
    return (bytes([12 + count_width]) + len(values).to_bytes(count_width, "little")
            + bytes([12 + width]) + b"".join(v.to_bytes(width, "little") for v in values))


class PSB:
    def __init__(self, data: bytes):
        self.data = data
        self._nodes = 0
        if len(data) < 44 or data[:4] != b"PSB\0":
            raise VnTransError("expected an unencrypted PSB v3 file")
        signature, version, flags, *fields = struct.unpack_from("<4sHH9I", data)
        if version != 3 or flags:
            raise VnTransError(f"unsupported PSB version/encryption: version={version}, flags={flags}")
        (header, names, strings, string_data, chunk_offsets, chunk_lengths,
         chunk_data, entries, checksum) = fields
        if header != 44 or not (44 == names < entries < strings < string_data
                               <= chunk_offsets < chunk_lengths < chunk_data <= len(data)):
            raise VnTransError("invalid or unsupported PSB section offsets")
        if zlib.adler32(data[8:40]) != checksum:
            raise VnTransError("invalid PSB header checksum")
        self.entry_start, self.entry_end = entries, strings
        self.name_data = data[names:entries]
        self._name_arrays = []
        p = names
        for _ in range(3):
            a, p = self.array(p, entries)
            self._name_arrays.append(a)
        if p != entries:
            raise VnTransError("unsupported PSB name-table padding")
        self.names = [self._name(i) for i in range(len(self._name_arrays[2]))]
        offsets, end = self.array(strings, string_data)
        if end != string_data:
            raise VnTransError("invalid PSB string-offset table")
        self.strings = []
        for offset in offsets:
            p = string_data + offset
            if not string_data <= p < chunk_offsets:
                raise VnTransError("PSB string offset out of bounds")
            end = data.find(b"\0", p, chunk_offsets)
            if end < 0:
                raise VnTransError("unterminated PSB string")
            try:
                self.strings.append(data[p:end].decode("utf-8"))
            except UnicodeError as exc:
                raise VnTransError(f"invalid UTF-8 PSB string: {exc}") from exc
        resources, end = self.array(chunk_offsets, chunk_lengths)
        if end != chunk_lengths:
            raise VnTransError("invalid resource-offset table")
        sizes, end = self.array(chunk_lengths, chunk_data)
        if end != chunk_data or len(resources) != len(sizes):
            raise VnTransError("invalid resource-length table")
        if any(o + n > len(data) - chunk_data for o, n in zip(resources, sizes)):
            raise VnTransError("resource outside PSB file")
        self.resource_count = len(resources)
        self.resource_offsets = data[chunk_offsets:chunk_lengths]
        self.resource_lengths = data[chunk_lengths:chunk_data]
        self.resource_data = data[chunk_data:]
        self.root = self.node(entries)

    def uint(self, p: int, width: int, limit: int) -> int:
        if width < 1 or width > 8 or p < 0 or p + width > limit:
            raise VnTransError("PSB integer/offset out of bounds")
        return int.from_bytes(self.data[p:p + width], "little")

    def array(self, p: int, limit: int) -> tuple[list[int], int]:
        if p >= limit:
            raise VnTransError("truncated PSB array")
        n = self.data[p] - 12
        count = self.uint(p + 1, n, limit)
        p += 1 + n
        if p >= limit:
            raise VnTransError("truncated PSB array width")
        width = self.data[p] - 12
        p += 1
        if not 1 <= width <= 8 or count > (limit - p) // width:
            raise VnTransError("PSB array exceeds section")
        return ([self.uint(p + i * width, width, limit) for i in range(count)], p + count * width)

    def _name(self, index: int) -> str:
        first, second, third = self._name_arrays
        try:
            v = second[third[index]]
            chars = []
            seen = set()
            while True:
                if v in seen:
                    raise VnTransError("cyclic PSB name table")
                seen.add(v)
                parent = second[v]
                chars.append(v - first[parent])
                v = parent
                if not v:
                    return bytes(reversed(chars)).decode("utf-8")
        except (IndexError, ValueError, UnicodeError) as exc:
            raise VnTransError(f"invalid PSB name table: {exc}") from exc

    def node(self, p: int, depth: int = 0) -> Node:
        self._nodes += 1
        if depth > 128 or self._nodes > 1_000_000:
            raise VnTransError("PSB nesting or node limit exceeded")
        if not self.entry_start <= p < self.entry_end:
            raise VnTransError("PSB value offset outside entries")
        start = p
        tag = self.data[p]
        p += 1
        if tag in (32, 33):
            keys = None
            if tag == 33:
                keys, p = self.array(p, self.entry_end)
                if any(k >= len(self.names) for k in keys) or len(set(keys)) != len(keys):
                    raise VnTransError("invalid PSB dictionary keys")
            offsets, p = self.array(p, self.entry_end)
            if keys is not None and len(keys) != len(offsets):
                raise VnTransError("PSB dictionary key/value count differs")
            children = [self.node(p + offset, depth + 1) for offset in offsets]
            return Node(tag, dict(zip((self.names[k] for k in keys), children))
                        if keys is not None else children)
        if 13 <= tag <= 20:
            value, p = self.array(start, self.entry_end)
        elif tag in (1, 2, 3, 4, 29):
            value = {1: None, 2: False, 3: True, 4: 0, 29: 0.0}[tag]
        elif 5 <= tag <= 12:
            width = tag - 4
            self.uint(p, width, self.entry_end)
            value = int.from_bytes(self.data[p:p + width], "little", signed=True)
            p += width
        elif 21 <= tag <= 28:
            width = tag - (20 if tag <= 24 else 24)
            index = self.uint(p, width, self.entry_end)
            p += width
            if tag <= 24:
                if index >= len(self.strings):
                    raise VnTransError("PSB string index out of bounds")
                value = self.strings[index]
            else:
                if index >= self.resource_count:
                    raise VnTransError("PSB resource index out of bounds")
                value = index
        elif tag in (30, 31):
            size = 4 if tag == 30 else 8
            self.uint(p, size, self.entry_end)
            value = struct.unpack_from("<f" if tag == 30 else "<d", self.data, p)[0]
            p += size
        else:
            raise VnTransError(f"unsupported PSB value tag 0x{tag:02x} at 0x{start:x}")
        return Node(tag, value, self.data[start:p])

    def build(self) -> bytes:
        strings = list(self.strings)

        def encode(node):
            if node.raw is not None:
                return node.raw
            if node.tag in (32, 33):
                children = node.value.values() if node.tag == 33 else node.value
                parts = [encode(child) for child in children]
                offsets = []
                length = 0
                for part in parts:
                    offsets.append(length)
                    length += len(part)
                keys = pack_array([self.names.index(k) for k in node.value]) if node.tag == 33 else b""
                return bytes([node.tag]) + keys + pack_array(offsets) + b"".join(parts)
            if node.tag == 21 and isinstance(node.value, str):
                index = len(strings)
                strings.append(node.value)
                size = max(1, (index.bit_length() + 7) // 8)
                return bytes([20 + size]) + index.to_bytes(size, "little")
            raise VnTransError("cannot serialize a newly constructed non-string PSB scalar")

        entries_data = encode(self.root)
        offsets = []
        parts = []
        length = 0
        for string in strings:
            part = string.encode("utf-8") + b"\0"
            offsets.append(length)
            length += len(part)
            parts.append(part)
        table = pack_array(offsets)
        names = 44
        entries = names + len(self.name_data)
        strings_offset = entries + len(entries_data)
        string_data = strings_offset + len(table)
        chunk_offsets = string_data + length
        chunk_lengths = chunk_offsets + len(self.resource_offsets)
        chunk_data = chunk_lengths + len(self.resource_lengths)
        fields = struct.pack("<8I", 44, names, strings_offset, string_data,
                             chunk_offsets, chunk_lengths, chunk_data, entries)
        header = b"PSB\0\x03\0\0\0" + fields + struct.pack("<I", zlib.adler32(fields))
        return (header + self.name_data + entries_data + table + b"".join(parts)
                + self.resource_offsets + self.resource_lengths + self.resource_data)
