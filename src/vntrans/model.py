"""Shared translation contract; format-specific locations stay in the handlers."""

from dataclasses import dataclass, field
from typing import Any, Protocol


class VnTransError(ValueError):
    """Invalid or unsupported input, safe to display at the command line."""


@dataclass
class Unit:
    id: str
    kind: str
    source: str
    context: dict[str, Any] = field(default_factory=dict)

    def document(self) -> dict[str, Any]:
        return dict(id=self.id, kind=self.kind, source=self.source,
                    translation=None, context=self.context)


class Script(Protocol):
    format: str
    encoding: str | None
    units: list[Unit]
    warnings: list[str]

    def rebuild(self, translations: dict[str, str], *, wrap_columns: int | None,
                output_encoding: str | None) -> bytes: ...


class Engine(Protocol):
    def probe(self, name: str, data: bytes) -> bool: ...

    def extract(self, name: str, data: bytes, *, encoding: str | None,
                language_index: int) -> Script: ...
