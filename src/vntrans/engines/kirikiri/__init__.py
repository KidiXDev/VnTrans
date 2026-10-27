from pathlib import Path

from ...model import VnTransError
from .ks import KsScript
from .scn import ScnScript


class Kirikiri:
    def probe(self, name: str, data: bytes) -> bool:
        return Path(name).suffix.lower() in (".ks", ".scn")

    def extract(self, name, data, *, encoding=None, language_index=0):
        if data.startswith(b"PSB"):
            if encoding:
                raise VnTransError("encoding overrides apply only to KS files")
            return ScnScript(data, language_index)
        if data.startswith((b"mdf\0", b"\xfe\xfe")):
            raise VnTransError("compressed/scrambled scripts must be decoded externally before extraction")
        if Path(name).suffix.lower() != ".ks":
            raise VnTransError("unsupported compiled script; expected unencrypted PSB v3")
        if language_index:
            raise VnTransError("language index selection applies only to multilingual SCN files")
        return KsScript(data, encoding)
