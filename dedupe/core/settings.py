"""User settings: $XDG_CONFIG_HOME/dedupe/settings.toml, validated against a dataclass.

Unknown keys produce a warning (almost certainly a typo); wrong types are an error that
names the key. Missing keys take the defaults; falsy values (0, false, []) are real values.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

import tomli_w

from dedupe.core import paths
from dedupe.core.models import (
    DEFAULT_EXCLUDES,
    MAX_SIMILARITY_THRESHOLD,
    DeleteMode,
    ScanOptions,
)

__all__ = [
    "LoadedSettings",
    "Settings",
    "SettingsError",
    "load_settings",
    "save_settings",
]


class SettingsError(ValueError):
    """The settings file is unreadable or a value has the wrong type."""


@dataclass(frozen=True, slots=True)
class Settings:
    exclude: tuple[str, ...] = DEFAULT_EXCLUDES
    protected_folders: tuple[str, ...] = ()
    min_size: int = 1
    follow_symlinks: bool = False
    cross_filesystems: bool = False
    include_hidden: bool = True
    paranoid: bool = False
    workers: int = 0  # 0 = automatic: min(4, cpu_count)
    default_delete_mode: str = DeleteMode.TRASH.value
    use_cache: bool = True
    similar_images: bool = False  # also find near-identical images (needs dedupe[similar])
    similarity_threshold: int = 8  # differing bits out of 64: strict 4, normal 8, loose 12

    def to_scan_options(self) -> ScanOptions:
        return ScanOptions(
            min_size=self.min_size,
            include_hidden=self.include_hidden,
            follow_symlinks=self.follow_symlinks,
            cross_filesystems=self.cross_filesystems,
            exclude=self.exclude,
            paranoid=self.paranoid,
            workers=self.workers,
            use_cache=self.use_cache,
            protected_folders=self.protected_folders,
            similar_images=self.similar_images,
            similarity_threshold=self.similarity_threshold,
        )

    def with_protected_folder(self, folder: str) -> Settings:
        if folder in self.protected_folders:
            return self
        return replace(self, protected_folders=(*self.protected_folders, folder))


@dataclass(frozen=True, slots=True)
class LoadedSettings:
    settings: Settings
    warnings: tuple[str, ...] = field(default_factory=tuple)


_KNOWN = {f.name: f for f in fields(Settings)}


def _validate(key: str, value: Any, default: Any) -> Any:
    where = f"setting '{key}'"
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise SettingsError(f"{where} must be true or false, got {value!r}")
        return value
    if isinstance(default, int):
        # bool is an int subclass; `min_size = true` must not pass as 1.
        if isinstance(value, bool) or not isinstance(value, int):
            raise SettingsError(f"{where} must be an integer, got {value!r}")
        if value < 0:
            raise SettingsError(f"{where} must not be negative, got {value}")
        if key == "workers" and value > 64:
            raise SettingsError(f"{where} must be at most 64, got {value}")
        if key == "similarity_threshold" and value > MAX_SIMILARITY_THRESHOLD:
            raise SettingsError(
                f"{where} must be between 0 and {MAX_SIMILARITY_THRESHOLD}, got {value}"
            )
        return value
    if isinstance(default, tuple):
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise SettingsError(f"{where} must be a list of strings, got {value!r}")
        return tuple(value)
    if isinstance(default, str):
        if not isinstance(value, str):
            raise SettingsError(f"{where} must be a string, got {value!r}")
        if key == "default_delete_mode" and value not in {m.value for m in DeleteMode}:
            allowed = ", ".join(m.value for m in DeleteMode)
            raise SettingsError(f"{where} must be one of {allowed}, got {value!r}")
        return value
    raise AssertionError(f"unhandled setting type for {key}")  # pragma: no cover


def load_settings(path: Path | None = None) -> LoadedSettings:
    path = path or paths.settings_file()
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return LoadedSettings(Settings())
    except OSError as e:
        raise SettingsError(f"cannot read {path}: {e.strerror}") from e
    try:
        data = tomllib.loads(raw.decode())
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise SettingsError(f"{path} is not valid TOML: {e}") from e

    defaults = Settings()
    values: dict[str, Any] = {}
    warnings: list[str] = []
    for key, value in data.items():
        if key not in _KNOWN:
            warnings.append(f"unknown setting '{key}' in {path} ignored")
            continue
        values[key] = _validate(key, value, getattr(defaults, key))
    return LoadedSettings(replace(defaults, **values), tuple(warnings))


def save_settings(settings: Settings, path: Path | None = None) -> Path:
    """Write settings atomically (temp file in the same directory, then replace)."""
    path = path or paths.settings_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {f.name: _to_toml(getattr(settings, f.name)) for f in fields(Settings)}
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(tomli_w.dumps(data))
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    return path


def _to_toml(value: Any) -> Any:
    return list(value) if isinstance(value, tuple) else value
