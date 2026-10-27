"""Explicit engine registry: add an engine here without changing the pipeline."""

from ..model import Engine, VnTransError
from .kirikiri import Kirikiri

ENGINES: dict[str, Engine] = {"kirikiri": Kirikiri()}


def get_engine(name: str) -> Engine:
    try:
        return ENGINES[name]
    except KeyError:
        raise VnTransError(f"unsupported engine: {name}") from None
