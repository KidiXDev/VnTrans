"""Per-user settings (provider profiles, recent workspaces), kept outside any workspace."""

import json
import os
from pathlib import Path
import sys


def config_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData/Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "vntrans"


def load() -> dict:
    path = config_dir() / "config.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            return value
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    # Keep an unreadable config (it may hold API keys) instead of overwriting it.
    os.replace(path, path.with_name("config.json.bad"))
    return {}


def save(config: dict):
    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / "config.json.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(config, stream, ensure_ascii=False, indent=2)
    os.replace(temporary, directory / "config.json")


def remember(config: dict, workspace: Path, name: str):
    path = str(workspace)
    config["recent"] = [dict(path=path, name=name)] + [
        item for item in config.get("recent", []) if item.get("path") != path][:9]
