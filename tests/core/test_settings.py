from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from dedupe.core import paths
from dedupe.core.models import DEFAULT_EXCLUDES, ScanOptions
from dedupe.core.settings import Settings, SettingsError, load_settings, save_settings


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "s.toml"
    p.write_text(text)
    return p


def test_missing_file_gives_defaults(tmp_path: Path) -> None:
    loaded = load_settings(tmp_path / "nope.toml")
    assert loaded.settings == Settings() and loaded.warnings == ()


def test_round_trip_including_falsy_values(tmp_path: Path) -> None:
    s = Settings(
        exclude=(),
        protected_folders=("/a", "/b"),
        min_size=0,
        follow_symlinks=True,
        include_hidden=False,
        paranoid=True,
        workers=0,
        default_delete_mode="permanent",
        use_cache=False,
    )
    path = save_settings(s, tmp_path / "out" / "s.toml")
    assert load_settings(path).settings == s
    assert not list(path.parent.glob("*.tmp"))


def test_default_path_is_xdg_config() -> None:
    path = save_settings(Settings())
    assert path == paths.settings_file()
    assert load_settings().settings == Settings()


def test_partial_file_keeps_other_defaults(tmp_path: Path) -> None:
    s = load_settings(write(tmp_path, "min_size = 5\n")).settings
    assert s == replace(Settings(), min_size=5) and s.exclude == DEFAULT_EXCLUDES


def test_unknown_key_warns(tmp_path: Path) -> None:
    loaded = load_settings(write(tmp_path, "min_sise = 5\nparanoid = true\n"))
    assert loaded.settings.paranoid is True and loaded.settings.min_size == 1
    assert len(loaded.warnings) == 1 and "min_sise" in loaded.warnings[0]


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ('min_size = "big"', "min_size"),
        ("min_size = true", "min_size"),  # bool must not pass as int
        ("min_size = -1", "min_size"),
        ("workers = 1000", "workers"),
        ("paranoid = 1", "paranoid"),
        ('exclude = "x"', "exclude"),
        ("exclude = [1, 2]", "exclude"),
        ('default_delete_mode = "shred"', "default_delete_mode"),
        ("default_delete_mode = 3", "default_delete_mode"),
    ],
)
def test_wrong_types_fail_naming_the_key(tmp_path: Path, text: str, key: str) -> None:
    with pytest.raises(SettingsError, match=key):
        load_settings(write(tmp_path, text))


def test_invalid_toml_and_unreadable(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="not valid TOML"):
        load_settings(write(tmp_path, "= = ="))
    with pytest.raises(SettingsError, match="cannot read"):
        load_settings(tmp_path)  # a directory


def test_to_scan_options() -> None:
    o = Settings(min_size=0, protected_folders=("/p",), workers=3).to_scan_options()
    assert o == replace(ScanOptions(), min_size=0, protected_folders=("/p",), workers=3)


def test_defaults_are_not_aliased() -> None:
    assert Settings().exclude is DEFAULT_EXCLUDES  # immutable tuple, safe to share
    s = Settings().with_protected_folder("/x")
    assert Settings().protected_folders == () and s.protected_folders == ("/x",)
    assert s.with_protected_folder("/x") is s


@pytest.mark.parametrize(
    ("var", "fn", "fallback", "suffix"),
    [
        ("XDG_CACHE_HOME", paths.cache_dir, ".cache", "dedupe"),
        ("XDG_CONFIG_HOME", paths.config_dir, ".config", "dedupe"),
        ("XDG_DATA_HOME", paths.data_dir, ".local/share", "dedupe"),
    ],
)
def test_xdg_env_and_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    var: str,
    fn: object,
    fallback: str,
    suffix: str,
) -> None:
    monkeypatch.setenv(var, str(tmp_path / "custom"))
    assert fn() == tmp_path / "custom" / suffix  # type: ignore[operator]
    monkeypatch.delenv(var)
    assert fn() == Path.home() / fallback / suffix  # type: ignore[operator]
    monkeypatch.setenv(var, "relative/path")  # invalid per the spec: ignored
    assert fn() == Path.home() / fallback / suffix  # type: ignore[operator]
    assert paths.action_log_file().name == "actions.log"
    assert paths.thumbs_dir().name == "thumbs"


def test_trash_is_the_default_delete_mode() -> None:
    """Spec section 10: Trash is the default. Nothing else may become the default silently."""
    assert Settings().default_delete_mode == "trash"
    assert load_settings(Path("/nonexistent/settings.toml")).settings.default_delete_mode == "trash"


# -- similar images -----------------------------------------------------------------------------


def test_similar_defaults_are_off_and_normal() -> None:
    s = Settings()
    assert s.similar_images is False and s.similarity_threshold == 8
    opts = s.to_scan_options()
    assert (opts.similar_images, opts.similarity_threshold) == (False, 8)


def test_similar_settings_round_trip(tmp_path: Path) -> None:
    s = replace(Settings(), similar_images=True, similarity_threshold=0)
    assert load_settings(save_settings(s, tmp_path / "s.toml")).settings == s
    assert s.to_scan_options().similarity_threshold == 0  # zero is a real value


@pytest.mark.parametrize("value", ["17", "-1", "true", "false", "8.5", '"normal"'])
def test_bad_similarity_threshold_is_rejected_naming_the_key(tmp_path: Path, value: str) -> None:
    with pytest.raises(SettingsError, match="similarity_threshold"):
        load_settings(write(tmp_path, f"similarity_threshold = {value}\n"))


def test_similar_images_must_be_a_bool(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="similar_images"):
        load_settings(write(tmp_path, "similar_images = 1\n"))


def test_threshold_bounds_are_accepted(tmp_path: Path) -> None:
    assert (
        load_settings(write(tmp_path, "similarity_threshold = 16\n")).settings.similarity_threshold
        == 16
    )
    assert (
        load_settings(write(tmp_path, "similarity_threshold = 0\n")).settings.similarity_threshold
        == 0
    )
