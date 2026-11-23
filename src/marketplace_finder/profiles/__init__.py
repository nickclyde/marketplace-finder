"""Profiles: one per kind of item an install can look for."""

from __future__ import annotations

import re
from importlib import resources
from pathlib import Path

from ..errors import ConfigError
from .base import Profile

# Profiles written in Python. Everything else that ships is a TOML file in this directory.
CODED = ("crt",)
NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def built_in() -> list[str]:
    packaged = resources.files("marketplace_finder") / "profiles"
    described = sorted(
        entry.name[: -len(".toml")] for entry in packaged.iterdir() if entry.name.endswith(".toml")
    )
    return [*CODED, *described]


def load_profile(section: dict, config_dir: Path) -> Profile:
    """The profile named by config.toml's [profile] section: `name` for one that ships
    with the project, or `path` for a TOML file of your own (relative to config.toml)."""
    name, path = section.get("name"), section.get("path")
    if path:
        from .declarative import TomlProfile

        return TomlProfile.from_file((config_dir / str(path)).expanduser())
    if name == "crt":
        from .crt import CrtProfile

        return CrtProfile()
    if isinstance(name, str) and NAME.match(name):
        packaged = resources.files("marketplace_finder") / "profiles" / f"{name}.toml"
        if packaged.is_file():
            from .declarative import TomlProfile

            return TomlProfile.from_text(packaged.read_text(encoding="utf-8"), name, f"profile {name}")
    raise ConfigError(
        "config.toml needs a [profile] section with `name` set to one of "
        f"{', '.join(built_in())}, or `path` set to a profile file of your own"
    )
