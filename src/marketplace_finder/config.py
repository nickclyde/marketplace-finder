from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from .errors import ConfigError

if TYPE_CHECKING:
    from .profiles.base import Profile

CONFIG_ENV = "MARKETPLACE_FINDER_CONFIG"
DATA_ENV = "MARKETPLACE_FINDER_DATA"

# Credentials the claude CLI reads from its environment. On a machine where nobody
# has run `claude` and logged in, one of these goes in .env and is handed on to it.
CLAUDE_ENV_KEYS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")
DISCORD_BACKENDS = ("discli", "webhook")


@dataclass
class SourceConfig:
    enabled: bool = False
    queries: list[str] = field(default_factory=list)
    options: dict = field(default_factory=dict)


@dataclass
class Config:
    path: Path  # the config file itself, whatever it is called
    config_dir: Path
    data_dir: Path
    profile: Profile
    postal_code: str
    latitude: float
    longitude: float
    radius_miles: float
    area: str
    max_price: int
    quiet_hours: tuple[int, int]
    timezone: str
    model: str
    max_evals_per_run: int
    max_photos: int
    crtdatabase: bool
    ntfy_server: str
    ntfy_topic: str
    discord_enabled: bool
    discord_via: str
    discord_channel: str
    discord_webhook_url: str
    discord_test_webhook_url: str
    discord_max_posts: int
    discord_top_picks: int
    discord_welcome: str
    ebay_client_id: str
    ebay_client_secret: str
    claude_env: dict[str, str]
    sources: dict[str, SourceConfig]

    @property
    def home(self) -> tuple[float, float]:
        return (self.latitude, self.longitude)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "listings.db"

    @property
    def env_path(self) -> Path:
        return self.config_dir / ".env"


def config_candidates() -> list[Path]:
    candidates = []
    if os.environ.get(CONFIG_ENV):
        candidates.append(Path(os.environ[CONFIG_ENV]))
    candidates.append(Path.cwd() / "config.toml")
    candidates.append(Path.home() / ".config" / "marketplace-finder" / "config.toml")
    return candidates


def find_config() -> Path:
    named = os.environ.get(CONFIG_ENV)
    if named and not Path(named).is_file():
        # Never fall back to another install's config when one was asked for by name.
        raise ConfigError(f"{CONFIG_ENV} is set to {named}, which is not a file")
    for path in config_candidates():
        if path.is_file():
            return path
    raise ConfigError(
        "config.toml not found. Run `marketplace-finder init` in the project directory "
        f"to create one, or set {CONFIG_ENV} to where it is."
    )


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def default_data_dir() -> Path:
    return Path(os.environ.get(DATA_ENV) or Path.home() / ".local" / "share" / "marketplace-finder")


def _table(raw: dict, name: str) -> dict:
    section = raw.get(name, {})
    if not isinstance(section, dict):
        raise ConfigError(f"config.toml: `{name}` must be a [{name}] section, not a single value")
    return section


def _required(section: dict, key: str, name: str):
    if key not in section:
        raise ConfigError(f"config.toml: [{name}] needs `{key}`")
    return section[key]


def load_config(path: Path | None = None) -> Config:
    from .profiles import load_profile

    path = path or find_config()
    try:
        raw = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc
    config_dir = path.parent
    env_file = read_env_file(config_dir / ".env")
    secrets = {**env_file, **os.environ}

    profile = load_profile(_table(raw, "profile"), config_dir)

    data_dir = default_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)

    search = _table(raw, "search")
    evaluator = _table(raw, "evaluator")
    discord = _table(raw, "discord")
    sources = {}
    for name, section in _table(raw, "sources").items():
        if not isinstance(section, dict):
            raise ConfigError(f"config.toml: `{name}` under sources must be a [sources.{name}] section")
        section = dict(section)
        queries = section.pop("queries", None)
        if queries is not None and not (
            isinstance(queries, list) and all(isinstance(query, str) for query in queries)
        ):
            raise ConfigError(f"config.toml: [sources.{name}] queries must be a list of strings")
        sources[name] = SourceConfig(
            enabled=bool(section.pop("enabled", False)),
            queries=list(queries) if queries is not None else profile.queries_for(name),
            options=section,
        )

    timezone = str(_required(search, "timezone", "search"))
    try:
        ZoneInfo(timezone)
    except (KeyError, ValueError) as exc:
        raise ConfigError(
            f"config.toml: [search] timezone {timezone!r} is not a known zone "
            "(use a name such as \"America/Chicago\")"
        ) from exc
    via = str(discord.get("via", "webhook"))
    if via not in DISCORD_BACKENDS:
        raise ConfigError(f"config.toml: [discord] via must be one of {', '.join(DISCORD_BACKENDS)}")
    quiet = search.get("quiet_hours", [0, 0])

    try:
        config = Config(
            path=path,
            config_dir=config_dir,
            data_dir=data_dir,
            profile=profile,
            postal_code=str(_required(search, "postal_code", "search")),
            latitude=float(_required(search, "latitude", "search")),
            longitude=float(_required(search, "longitude", "search")),
            radius_miles=float(_required(search, "radius_miles", "search")),
            area=str(_required(search, "area", "search")),
            max_price=int(_required(search, "max_price", "search")),
            quiet_hours=(int(quiet[0]), int(quiet[1])),
            timezone=timezone,
            model=str(evaluator.get("model", "opus")),
            max_evals_per_run=int(evaluator.get("max_per_run", 30)),
            max_photos=int(evaluator.get("max_photos", 4)),
            crtdatabase=bool(_table(raw, "reference").get("crtdatabase", True)),
            ntfy_server=str(_table(raw, "notify").get("server", "https://ntfy.sh")).rstrip("/"),
            ntfy_topic=secrets.get("NTFY_TOPIC", ""),
            discord_enabled=bool(discord.get("enabled", False)),
            discord_via=via,
            discord_channel=str(discord.get("channel", "")),
            discord_webhook_url=secrets.get("DISCORD_WEBHOOK_URL", ""),
            discord_test_webhook_url=secrets.get("DISCORD_TEST_WEBHOOK_URL", ""),
            discord_max_posts=int(discord.get("max_posts_per_run", 10)),
            discord_top_picks=int(discord.get("top_picks", 10)),
            discord_welcome="",
            ebay_client_id=secrets.get("EBAY_CLIENT_ID", ""),
            ebay_client_secret=secrets.get("EBAY_CLIENT_SECRET", ""),
            # Only what the environment does not already provide, so it keeps precedence.
            claude_env={
                key: env_file[key]
                for key in CLAUDE_ENV_KEYS
                if env_file.get(key) and key not in os.environ
            },
            sources=sources,
        )
    except (TypeError, ValueError, IndexError) as exc:
        raise ConfigError(f"config.toml has a value of the wrong kind: {exc}") from exc
    # An empty `welcome` means no welcome; leaving the key out means the profile's own.
    config.discord_welcome = (
        str(discord["welcome"]).strip() if "welcome" in discord else profile.welcome(config)
    )
    return config
