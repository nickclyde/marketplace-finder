"""Getting an install going: first files, a health check, and the hourly timer."""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import sys
from importlib import resources
from pathlib import Path

from . import discord as discord_module
from .config import CLAUDE_ENV_KEYS, CONFIG_ENV, DATA_ENV, Config
from .db import Database
from .errors import ConfigError
from .evaluate import EvaluationError, check_model_name, find_claude, run_claude
from .geo import lookup_city, nearby_states
from .profiles import built_in, load_profile
from .sources import SOURCE_NAMES

ENV_KEYS = (
    ("NTFY_TOPIC", "ntfy topic for private alerts when something breaks. Anyone who knows the name can read them, so keep it long and random."),
    ("DISCORD_WEBHOOK_URL", "Webhook address of the channel listings are posted to (Channel settings > Integrations > Webhooks). Anyone who has it can post there."),
    ("DISCORD_TEST_WEBHOOK_URL", "Optional: a second webhook, in a test channel, for `notify-test --test`."),
    ("EBAY_CLIENT_ID", "eBay Browse API production keyset (https://developer.ebay.com/my/keys). Leave blank to skip eBay."),
    ("EBAY_CLIENT_SECRET", ""),
)
DEFAULT_UNIT = "marketplace-finder"

# -- init -------------------------------------------------------------------


def system_timezone() -> str | None:
    """The machine's zone name, e.g. "America/Chicago", when it can be found."""
    try:
        name = Path("/etc/timezone").read_text().strip()
        if name:
            return name
    except OSError:
        pass
    try:
        target = os.path.realpath("/etc/localtime")
    except OSError:
        return None
    return target.split("/zoneinfo/", 1)[1] if "/zoneinfo/" in target else None


def render_config(
    *, profile_line: str, postal_code: str, latitude: float, longitude: float, area: str, timezone: str
) -> str:
    template = (resources.files("marketplace_finder") / "data" / "config.template.toml").read_text(
        encoding="utf-8"
    )
    values = {
        "@PROFILES@": ", ".join(built_in()),
        "@PROFILE_LINE@": profile_line,
        "@POSTAL_CODE@": postal_code,
        "@LATITUDE@": f"{latitude:.4f}",
        "@LONGITUDE@": f"{longitude:.4f}",
        "@AREA@": area,
        "@TIMEZONE@": timezone,
    }
    for placeholder, value in values.items():
        template = template.replace(placeholder, value)
    return template


def profile_line(choice: str, config_dir: Path) -> str:
    """The [profile] line for a built-in name or a path to a profile file. Checks it loads."""
    if choice in built_in():
        section = {"name": choice}
        line = f'name = "{choice}"'
    else:
        path = Path(choice).expanduser()
        resolved = path if path.is_absolute() else Path.cwd() / path
        if not resolved.is_file():
            raise ConfigError(
                f"{choice!r} is not a built-in profile ({', '.join(built_in())}) or a profile file"
            )
        try:
            shown = resolved.resolve().relative_to(config_dir.resolve())
        except ValueError:
            shown = resolved.resolve()
        section = {"path": str(shown)}
        line = f'path = "{shown}"'
    load_profile(section, config_dir)
    return line


def new_config(config_path: Path, *, profile: str | None, postal_code: str | None, place: str | None) -> str:
    """The text of a first config.toml. Raises ConfigError saying what is still needed."""
    missing = [
        flag
        for flag, value in (("--profile", profile), ("--zip", postal_code), ("--place", place))
        if not value
    ]
    if missing:
        raise ConfigError(
            f"to create {config_path}, also pass {', '.join(missing)}. For example:\n\n"
            '    marketplace-finder init --profile keyboards --zip 97201 --place "Portland, OR"\n\n'
            f"Profiles that ship with the project: {', '.join(built_in())}."
        )
    coords = lookup_city(place)
    if coords is None:
        raise ConfigError(
            f"{place!r} was not found. Write it as \"City, ST\"; if it is a small place, "
            "use the nearest town of some size."
        )
    timezone = system_timezone()
    city = place.split(",")[0].strip()
    return render_config(
        profile_line=profile_line(profile, config_path.parent),
        postal_code=postal_code.strip(),
        latitude=coords[0],
        longitude=coords[1],
        area=city if city != city.lower() else city.title(),
        timezone=timezone or "America/New_York",
    ) + ("" if timezone else "\n# NOTE: the machine's timezone could not be found; check `timezone` above.\n")


def ensure_env(env_path: Path) -> str:
    """Create .env, or add what is missing from it. Returns the ntfy topic. Nothing set is changed."""
    lines = env_path.read_text().splitlines() if env_path.exists() else []

    def name_of(line: str) -> str | None:
        if "=" not in line or line.lstrip().startswith("#"):
            return None
        return line.split("=", 1)[0].strip()

    def value_of(line: str) -> str:
        return line.split("=", 1)[1].strip().strip("'\"")

    # As the file is read at run time: the last line for a name is the one that counts.
    present = {name_of(line): value_of(line) for line in lines if name_of(line)}
    topic = present.get("NTFY_TOPIC") or "marketplace-" + secrets.token_urlsafe(18)
    if "NTFY_TOPIC" in present and not present["NTFY_TOPIC"]:
        lines = [
            f"NTFY_TOPIC={topic}" if name_of(line) == "NTFY_TOPIC" and not value_of(line) else line
            for line in lines
        ]
    for name, comment in ENV_KEYS:
        if name in present:
            continue
        if comment:
            lines += ["", f"# {comment}"]
        lines.append(f"{name}={topic if name == 'NTFY_TOPIC' else ''}")
    if not any(key in present for key in CLAUDE_ENV_KEYS) and not any("claude setup-token" in line for line in lines):
        lines += [
            "",
            "# Only on a machine where `claude` has not been signed in: run `claude setup-token`",
            "# somewhere with a browser and put the token it prints here.",
            "# CLAUDE_CODE_OAUTH_TOKEN=",
        ]
    env_path.write_text("\n".join(lines).lstrip("\n") + "\n")
    env_path.chmod(0o600)
    return topic


# -- doctor -----------------------------------------------------------------

OK, WARN, FAIL = "ok", "warn", "FAIL"


def _git(config_dir: Path, *args: str) -> int | None:
    """A git command's exit status in the config directory, or None when git is not in play."""
    if not shutil.which("git"):
        return None
    inside = subprocess.run(
        ["git", "-C", str(config_dir), "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True
    )
    if inside.returncode != 0:
        return None
    return subprocess.run(["git", "-C", str(config_dir), *args], capture_output=True, text=True).returncode


def check_env_file(config: Config) -> tuple[str, str]:
    path = config.env_path
    if not path.exists():
        return WARN, f"{path} does not exist; run `marketplace-finder init`"
    if _git(config.config_dir, "ls-files", "--error-unmatch", ".env") == 0:
        return FAIL, f"{path} is tracked by git. Run `git rm --cached .env`, and change every secret in it if it was ever pushed."
    if path.stat().st_mode & 0o077:
        return WARN, f"{path} can be read by other users of this machine; run `chmod 600 {path}`"
    if _git(config.config_dir, "check-ignore", "-q", ".env") == 1:
        return WARN, f"{path} is not ignored by git; add `.env` to .gitignore"
    return OK, f"{path} is private and not tracked by git"


def check_claude(config: Config, live: bool) -> tuple[str, str]:
    try:
        check_model_name(config.model)
        path = find_claude()
    except EvaluationError as exc:
        return FAIL, str(exc)
    if not live:
        return OK, f"{path} (add --live to check it is signed in; that makes one small request)"
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["ok"],
        "properties": {"ok": {"type": "boolean"}},
    }
    try:
        _, models = run_claude(
            None, config.model, "Reply with ok set to true.", schema, config.data_dir,
            credentials=config.claude_env,
        )
    except EvaluationError as exc:
        return FAIL, f"{exc}. Sign in by running `claude` once, or put a token from `claude setup-token` in .env as CLAUDE_CODE_OAUTH_TOKEN."
    return OK, f"{path} answered, using {', '.join(models) or config.model}"


def check_discord(config: Config) -> tuple[str, str]:
    if not config.discord_enabled:
        return WARN, "posting is switched off ([discord] enabled = false); matches are only stored"
    if config.discord_via == "webhook":
        if not config.discord_webhook_url:
            return FAIL, "put the channel's webhook address in .env as DISCORD_WEBHOOK_URL"
        if not discord_module.WEBHOOK_URL.match(config.discord_webhook_url):
            return FAIL, "DISCORD_WEBHOOK_URL does not look like a Discord webhook address"
        return OK, "webhook address is set (try it with `notify-test`)"
    if not config.discord_channel:
        return FAIL, "set `channel` under [discord] in config.toml"
    try:
        path = discord_module.find_discli()
    except discord_module.DiscordError as exc:
        return FAIL, f"{exc}; install it and run `discli init`, or switch to via = \"webhook\""
    return OK, f"{path}, posting to #{config.discord_channel} (try it with `notify-test --channel NAME`)"


def _needs_attention(config: Config, name: str) -> bool:
    """Whether the last run found this source waiting on a person (logged out, say)."""
    if not config.db_path.exists():
        return False
    db = Database(config.db_path)
    try:
        return bool(db.health(name)["needs_attention"])
    finally:
        db.close()


def check_source(config: Config, name: str) -> tuple[str, str]:
    source = config.sources.get(name)
    if source is None or not source.enabled:
        return OK, "off"
    queries = f"{len(source.queries)} searches"
    if not source.queries:
        return FAIL, "on, but there is nothing to search for; add `queries` to the profile or this source"
    if name == "ebay" and not (config.ebay_client_id and config.ebay_client_secret):
        return WARN, "on, but skipped until EBAY_CLIENT_ID and EBAY_CLIENT_SECRET are in .env"
    if name == "facebook":
        if not str(source.options.get("location") or "").strip():
            return FAIL, "on, but `location` is not set under [sources.facebook]"
        if not (config.data_dir / "facebook-profile").is_dir() or _needs_attention(config, name):
            return FAIL, "on, but not logged in; run `marketplace-finder fb-login`"
        if not (os.environ.get("DISPLAY") or shutil.which("xvfb-run")):
            return WARN, f"on, {queries}; it needs a display, so install xvfb for timer runs"
    return OK, f"on, {queries}"


def unit_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user"


def installed_units(config: Config) -> list[str]:
    """Names of the timers that `install-timer` wrote for this config file."""
    marker = f"{CONFIG_ENV}={config.path.resolve()}".replace("%", "%%")
    names = []
    for service in sorted(unit_dir().glob("*.service")):
        try:
            if marker in service.read_text() and service.with_suffix(".timer").exists():
                names.append(service.stem)
        except OSError:
            continue
    return names


def check_timer(config: Config) -> tuple[str, str]:
    if not shutil.which("systemctl"):
        return WARN, "systemd was not found; schedule `marketplace-finder run` hourly some other way"
    for unit in installed_units(config):
        result = subprocess.run(
            ["systemctl", "--user", "is-active", f"{unit}.timer"], capture_output=True, text=True
        )
        if result.stdout.strip() == "active":
            return OK, f"{unit}.timer is active"
        return WARN, f"{unit}.timer is installed but not running; run `marketplace-finder install-timer` again"
    return WARN, "not scheduled yet; run `marketplace-finder install-timer` when a manual run looks right"


def doctor(config: Config, *, live: bool = False) -> list[tuple[str, str, str]]:
    """Every check as (status, what, detail)."""
    profile = config.profile
    states = ", ".join(sorted(nearby_states(config.home, config.radius_miles))) or "no known places"
    checks = [
        (OK, "config", str(config.path)),
        (OK, "profile", f"{profile.name}: {profile.label}"),
        (
            OK if states != "no known places" else WARN,
            "location",
            f"{config.radius_miles:g} miles around {config.area} ({config.postal_code}), reaching {states}",
        ),
    ]
    status, detail = check_env_file(config)
    checks.append((status, "secrets", detail))
    status, detail = check_claude(config, live)
    checks.append((status, "claude", detail))
    status, detail = check_discord(config)
    checks.append((status, "discord", detail))
    checks.append(
        (OK, "alerts", f"ntfy topic is set on {config.ntfy_server}")
        if config.ntfy_topic
        else (WARN, "alerts", "NTFY_TOPIC is not set in .env, so failures will go unnoticed; run `marketplace-finder init`")
    )
    for name in SOURCE_NAMES:
        status, detail = check_source(config, name)
        checks.append((status, name, detail))
    if not any(s.enabled for s in config.sources.values()):
        checks.append((FAIL, "sources", "no source is switched on in config.toml"))
    status, detail = check_timer(config)
    checks.append((status, "timer", detail))
    return checks


# -- timer ------------------------------------------------------------------


def _quote(value: str) -> str:
    """A value for a unit file: specifiers switched off, quoted when it has spaces."""
    value = value.replace("%", "%%")
    return f'"{value}"' if any(ch.isspace() for ch in value) else value


def program_path() -> Path:
    """The installed `marketplace-finder` command this process was started from."""
    started = Path(sys.argv[0])
    if started.name == "marketplace-finder" and started.exists():
        return started.absolute()
    return Path(sys.executable).parent / "marketplace-finder"


def timer_units(config_path: Path, program: Path, *, data_dir: str | None = None, extra_path: list[str] | None = None) -> dict[str, str]:
    """The text of the service and timer units, keyed by file suffix."""
    literal = [directory.replace("%", "%%") for directory in extra_path or []]
    path = ":".join(dict.fromkeys([*literal, "%h/.local/bin", "/usr/local/bin", "/usr/bin", "/bin"]))
    path_line = f"PATH={path}"
    if any(ch.isspace() for ch in path_line):
        path_line = f'"{path_line}"'
    environment = [f"Environment={_quote(f'{CONFIG_ENV}={config_path}')}"]
    if data_dir:
        environment.append(f"Environment={_quote(f'{DATA_ENV}={data_dir}')}")
    service = f"""[Unit]
Description=marketplace-finder: search marketplaces and post the listings that fit
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory={str(config_path.parent).replace("%", "%%")}
Environment={path_line}
{chr(10).join(environment)}
# The Facebook source needs a display; use a virtual one when xvfb is installed.
ExecStart=/bin/sh -c 'if command -v xvfb-run >/dev/null; then exec xvfb-run -a "$$0" run; else exec "$$0" run; fi' "{str(program).replace("%", "%%")}"
TimeoutStartSec=45min
"""
    timer = """[Unit]
Description=Run marketplace-finder every hour

[Timer]
# Quiet hours are set in config.toml; a run that starts inside them does nothing.
OnCalendar=hourly
RandomizedDelaySec=10min
Persistent=false

[Install]
WantedBy=timers.target
"""
    return {"service": service, "timer": timer}


def tool_dirs() -> list[str]:
    """Where claude and discli live, so the timer's PATH can find them."""
    found = []
    for tool in ("claude", "discli"):
        path = shutil.which(tool)
        if path:
            found.append(str(Path(path).parent))
    return found


def systemctl(*args: str) -> bool:
    return subprocess.run(["systemctl", "--user", *args]).returncode == 0
