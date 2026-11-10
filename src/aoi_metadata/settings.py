"""The settings file of the AOI export (``aoi_settings.yaml``, next to ``AOI.bat``).

The file is not in git, so pulling a new version never clashes with your
changes; a missing file means the defaults below. The folder settings use the
same names as the HRM + PRF dashboard, so this tool can become one of its
sources later.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SETTINGS_FILE = "aoi_settings.yaml"


class SettingsError(Exception):
    """The settings file cannot be read or holds a wrong value."""


def _default_cache_dir() -> str:
    if os.name == "nt":
        return r"%LOCALAPPDATA%\AOI_Metadata"
    return "~/.cache/aoi_metadata"


def _default_output_dir() -> str:
    if os.name == "nt":
        return r"%OneDriveCommercial%\AOI_Metadata"
    return "~/AOI_Metadata"


DEFAULTS: dict[str, Any] = {
    "source_root": r"L:\ale\ics_htb3_rnd\130 - measurement results",
    "lot_types": {
        "01_Customer shippable lots": "Shippable",
        "02_Engineering lots": "Engineering",
    },
    "skip_folders": ["Others"],
    "machine_folder": "AOI",
    "defects_sheet": "Defects",
    "output_dir": _default_output_dir(),
    "cache_dir": _default_cache_dir(),
    "workers": 4,
    "schedule_hours": 4,
}


def expand(text: str) -> Path:
    """'%OneDriveCommercial%\\x' or '~/x' -> a full path."""
    return Path(os.path.expandvars(os.path.expanduser(text)))


@dataclass
class Settings:
    source_root: str = DEFAULTS["source_root"]
    lot_types: dict[str, str] = field(default_factory=lambda: dict(DEFAULTS["lot_types"]))
    skip_folders: list[str] = field(default_factory=lambda: list(DEFAULTS["skip_folders"]))
    machine_folder: str = DEFAULTS["machine_folder"]
    defects_sheet: str = DEFAULTS["defects_sheet"]
    output_dir: str = DEFAULTS["output_dir"]
    cache_dir: str = DEFAULTS["cache_dir"]
    workers: int = DEFAULTS["workers"]
    schedule_hours: int = DEFAULTS["schedule_hours"]

    @property
    def root(self) -> Path:
        return expand(self.source_root)

    @property
    def output(self) -> Path:
        return expand(self.output_dir)

    @property
    def cache(self) -> Path:
        return expand(self.cache_dir)

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in DEFAULTS}


def load(path: Path) -> Settings:
    """The settings in ``path``; the defaults for anything missing (or no file at all)."""
    if not path.is_file():
        return Settings()
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise SettingsError(f"Settings file {path} cannot be read: {exc}") from exc
    if not isinstance(loaded, dict):
        raise SettingsError(f"Settings file {path} must hold 'name: value' lines.")
    unknown = sorted(set(loaded) - set(DEFAULTS))
    if unknown:
        raise SettingsError(f"Unknown setting(s) in {path}: {', '.join(unknown)}")
    settings = Settings()
    for name, value in loaded.items():
        default = DEFAULTS[name]
        if isinstance(default, int) and not isinstance(default, bool):
            try:
                value = int(value)
            except (TypeError, ValueError) as exc:
                raise SettingsError(f"Setting '{name}' must be a number, not {value!r}") from exc
            if value < 1:
                raise SettingsError(f"Setting '{name}' must be 1 or more, not {value}")
        elif isinstance(default, dict):
            if not isinstance(value, dict) or not value:
                raise SettingsError(f"Setting '{name}' must list 'folder: name' pairs.")
            value = {str(k): str(v) for k, v in value.items()}
        elif isinstance(default, list):
            value = [str(v) for v in (value or [])]
        else:
            value = str(value or "")
            if not value.strip():
                raise SettingsError(f"Setting '{name}' must not be empty.")
        setattr(settings, name, value)
    return settings


def save(settings: Settings, path: Path) -> None:
    path.write_text(
        yaml.safe_dump(settings.to_dict(), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
