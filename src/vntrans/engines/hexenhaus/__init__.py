"""HexenHaus script and ARCC v2 archive handler."""

from pathlib import Path
from struct import pack, pack_into, unpack_from

from ...model import VnTransError
from .nori import ENCODING, FORMAT, NoriScript, check_encoding, crc

ARCC = b"ARCC\x02\0\0\0"
NAME_XOR = bytes(value ^ 0x69 for value in range(256))


class ArccScript:
    format = FORMAT
    encoding = ENCODING

    def __init__(self, data):
        self.data = data
        self.units = []
        self.warnings = []
        self.members = []
        if len(data) < 60 or not data.startswith(ARCC) or data[42:46] != b"NAME":
            raise VnTransError("expected a HexenHaus ARCC v2 archive")
        count = unpack_from("<I", data, 20)[0]
        addr = unpack_from("<Q", data, 46)[0]
        eidx = 60 + 8 * count
        cinf = eidx + 4 + 8 * count
        self.files_start = addr + 4 + 12 * count
        if (
            not cinf + 4 <= addr <= self.files_start <= len(data)
            or data[56:60] != b"NIDX"
            or data[eidx : eidx + 4] != b"EIDX"
            or data[cinf : cinf + 4] != b"CINF"
            or data[addr : addr + 4] != b"ADDR"
            or crc(data[:40]) != data[40:42]
            or crc(data[42:54]) != data[54:56]
        ):
            raise VnTransError("invalid ARCC index")
        name_pos = cinf + 4
        file_pos = self.files_start
        for index in range(count):
            row = addr + 4 + 12 * index
            if name_pos + 12 > addr or file_pos + 36 > len(data):
                raise VnTransError("truncated ARCC member")
            name_size = unpack_from("<H", data, name_pos + 6)[0]
            next_name = name_pos + 12 + name_size
            offset = unpack_from("<Q", data, row + 2)[0]
            size = unpack_from("<I", data, file_pos + 24)[0]
            start = file_pos + 34
            end = start + size
            if (
                next_name > addr
                or offset != file_pos
                or end + 2 > len(data)
                or data[file_pos : file_pos + 4] != b"FILE"
                or data[start - 6 : start - 2] != b"DAEN"
                or crc(data[row : row + 10]) != data[row + 10 : row + 12]
                or crc(data[file_pos : start - 2]) != data[start - 2 : start]
            ):
                raise VnTransError(f"invalid ARCC member {index}")
            name = data[name_pos + 10 : next_name - 2].translate(NAME_XOR)
            try:
                name = name.decode(ENCODING)
            except UnicodeError as exc:
                raise VnTransError(
                    f"invalid ARCC member name at index {index}"
                ) from exc
            payload = memoryview(data)[start:end]
            if crc(payload) != data[end : end + 2]:
                raise VnTransError(f"{name}: invalid ARCC payload checksum")
            script = None
            if name.lower().endswith(".bin") and data[start : start + 4] == b"NORI":
                try:
                    script = NoriScript(bytes(payload), prefix=f"{index}/", scene=name)
                except VnTransError as exc:
                    raise VnTransError(f"{name}: {exc}") from exc
                self.units.extend(script.units)
            self.members.append((row, file_pos, end, script))
            name_pos = next_name
            file_pos = end + 2
        if name_pos != addr or file_pos != len(data):
            raise VnTransError("unexpected data after ARCC records")

    def rebuild(self, translations, *, wrap_columns=None, output_encoding=None):
        check_encoding(output_encoding)
        if not translations:
            return self.data
        data = self.data
        output = bytearray(data[: self.files_start])
        for row, file_pos, end, script in self.members:
            pack_into("<Q", output, row + 2, len(output))
            output[row + 10 : row + 12] = crc(output[row : row + 10])
            if script is None:
                output += data[file_pos : end + 2]
                continue
            payload = script.rebuild(translations, wrap_columns=wrap_columns)
            if payload is script.data:
                output += data[file_pos : end + 2]
                continue
            if len(payload) > 0xFFFFFFFF:
                raise VnTransError("ARCC member exceeds the archive size field")
            header = data[file_pos : file_pos + 24] + pack("<I", len(payload)) + b"DAEN"
            output += header + crc(header) + payload + crc(payload)
        return bytes(output)


class HexenHaus:
    def probe(self, name, data):
        suffix = Path(name).suffix.lower()
        return (suffix == ".arc" and data.startswith(b"ARCC")) or (
            suffix == ".bin" and data.startswith(b"NORI")
        )

    def extract(self, name, data, *, encoding=None, language_index=0):
        if language_index:
            raise VnTransError(
                "language index selection applies only to multilingual SCN files"
            )
        check_encoding(encoding)
        if Path(name).suffix.lower() == ".arc":
            return ArccScript(data)
        return NoriScript(data, scene=Path(name).name)
