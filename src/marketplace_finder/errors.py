from __future__ import annotations


class ConfigError(Exception):
    """config.toml, .env or a profile file is missing something or says something unusable."""
