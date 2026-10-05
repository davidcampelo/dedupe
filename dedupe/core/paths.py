"""XDG base-directory helpers with the standard fallbacks."""

from __future__ import annotations

import os
from pathlib import Path

APP_DIR = "dedupe"


def _xdg(var: str, fallback: str) -> Path:
    value = os.environ.get(var, "")
    # The spec says relative paths are invalid and must be ignored.
    if value and os.path.isabs(value):
        return Path(value)
    return Path.home() / fallback


def cache_dir() -> Path:
    return _xdg("XDG_CACHE_HOME", ".cache") / APP_DIR


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / APP_DIR


def data_dir() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share") / APP_DIR


def settings_file() -> Path:
    return config_dir() / "settings.toml"


def action_log_file() -> Path:
    return data_dir() / "actions.log"


def hash_db_file() -> Path:
    return cache_dir() / "hashes.db"


def thumbs_dir() -> Path:
    return cache_dir() / "thumbs"
