"""First-time setup: config and secrets files, the health check and the timer."""

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from marketplace_finder import cli, prefilter, setup
from marketplace_finder.config import load_config
from marketplace_finder.errors import ConfigError
from marketplace_finder.evaluate import ClaudeEvaluator
from marketplace_finder.geo import nearby_states

from conftest import ROOT, SECRETS, TEST_CONFIG

PORTLAND = dict(profile="keyboards", postal_code="97201", place="Portland, OR")


@pytest.fixture
def project(tmp_path, monkeypatch):
    """An empty directory to set up in, with nothing inherited from the real machine."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MARKETPLACE_FINDER_DATA", str(tmp_path / "data"))
    monkeypatch.delenv("MARKETPLACE_FINDER_CONFIG", raising=False)
    for name in SECRETS:
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def test_the_example_config_is_what_init_writes():
    rendered = setup.render_config(
        profile_line='name = "crt"', postal_code="95814", latitude=38.5816,
        longitude=-121.4944, area="Sacramento", timezone="America/Los_Angeles",
    )
    assert (ROOT / "config.example.toml").read_text() == rendered


def test_init_writes_a_config_that_loads_and_a_private_env(project, capsys):
    assert cli.main(["init", "--profile", "keyboards", "--zip", "97201", "--place", "portland, or"]) == 0
    config = load_config(project / "config.toml")
    assert config.profile.name == "keyboards" and config.postal_code == "97201"
    assert config.area == "Portland" and config.home == pytest.approx((45.537, -122.65))
    assert config.discord_via == "webhook" and config.sources["craigslist"].queries
    assert not config.sources["facebook"].enabled
    assert stat.S_IMODE((project / ".env").stat().st_mode) == 0o600
    assert config.ntfy_topic.startswith("marketplace-") and len(config.ntfy_topic) > 30
    assert config.ntfy_topic in capsys.readouterr().out


def test_init_never_replaces_what_is_there(project):
    (project / "config.toml").write_text(TEST_CONFIG)
    (project / ".env").write_text("# mine\nNTFY_TOPIC=my-own-topic\nEBAY_CLIENT_ID=abc\n")
    assert cli.main(["init", "--profile", "keyboards", "--zip", "97201", "--place", "Portland, OR"]) == 0
    assert (project / "config.toml").read_text() == TEST_CONFIG
    env = (project / ".env").read_text()
    assert env.startswith("# mine\nNTFY_TOPIC=my-own-topic\nEBAY_CLIENT_ID=abc\n")
    # Keys it did not have are added, blank.
    assert "\nDISCORD_WEBHOOK_URL=\n" in env and "\nEBAY_CLIENT_SECRET=\n" in env
    assert env.count("NTFY_TOPIC=") == 1 and env.count("EBAY_CLIENT_ID=") == 1
    # And a second run changes nothing at all.
    cli.main(["init"])
    assert (project / ".env").read_text() == env


def test_init_fills_in_an_empty_topic(project):
    (project / ".env").write_text("NTFY_TOPIC=\n")
    topic = setup.ensure_env(project / ".env")
    assert f"NTFY_TOPIC={topic}\n" in (project / ".env").read_text()


@pytest.mark.parametrize("changes,complaint", [
    ({"profile": None}, "--profile"),
    ({"postal_code": None, "place": None}, "--zip, --place"),
    ({"place": "Atlantis, ZZ"}, "was not found"),
    ({"profile": "no-such-thing"}, "not a built-in profile"),
])
def test_init_says_what_it_still_needs(project, changes, complaint):
    with pytest.raises(ConfigError, match=complaint):
        setup.new_config(project / "config.toml", **{**PORTLAND, **changes})
    assert not (project / "config.toml").exists()


def test_init_accepts_a_profile_file(project):
    (project / "cameras.toml").write_text('item = "a film camera"\nqueries = ["film camera"]\n')
    assert cli.main(["init", "--profile", "cameras.toml", "--zip", "97201", "--place", "Portland, OR"]) == 0
    assert 'path = "cameras.toml"' in (project / "config.toml").read_text()
    assert load_config(project / "config.toml").profile.name == "cameras"


@pytest.mark.parametrize("line,complaint", [
    ('postal_code = "95814"\n', r"\[search\] needs `postal_code`"),
    ("latitude = 38.5816\n", r"\[search\] needs `latitude`"),
    ("max_price = 500\n", r"\[search\] needs `max_price`"),
    ('timezone = "America/Los_Angeles"\n', r"\[search\] needs `timezone`"),
])
def test_config_has_no_built_in_location(project, line, complaint):
    assert line in TEST_CONFIG
    (project / "config.toml").write_text(TEST_CONFIG.replace(line, ""))
    with pytest.raises(ConfigError, match=complaint):
        load_config(project / "config.toml")


@pytest.mark.parametrize("old,new,complaint", [
    ('"America/Los_Angeles"', '"Mars/Olympus"', "not a known zone"),
    ('via = "discli"', 'via = "pigeon"', "via must be one of"),
    ("latitude = 38.5816", 'latitude = "north"', "wrong kind"),
])
def test_config_mistakes_are_reported_plainly(project, old, new, complaint):
    (project / "config.toml").write_text(TEST_CONFIG.replace(old, new))
    with pytest.raises(ConfigError, match=complaint):
        load_config(project / "config.toml")


def test_a_missing_config_points_at_init(project, capsys):
    assert cli.main(["status"]) == 2
    assert "marketplace-finder init" in capsys.readouterr().err


def test_secrets_come_from_env_file_with_the_environment_winning(project, monkeypatch):
    (project / "config.toml").write_text(TEST_CONFIG)
    (project / ".env").write_text(
        "NTFY_TOPIC=from-file\nDISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/1/abc\n"
        "CLAUDE_CODE_OAUTH_TOKEN=token-from-file\n"
    )
    monkeypatch.setenv("NTFY_TOPIC", "from-environment")
    config = load_config(project / "config.toml")
    assert config.ntfy_topic == "from-environment"
    assert config.discord_webhook_url == "https://discord.com/api/webhooks/1/abc"
    assert config.claude_env == {"CLAUDE_CODE_OAUTH_TOKEN": "token-from-file"}


def test_claude_credentials_from_env_file_reach_the_cli(config, make_listing, tmp_path):
    config.claude_env = {"CLAUDE_CODE_OAUTH_TOKEN": "token-from-file"}
    seen = tmp_path / "seen.txt"
    verdict = {
        "is_crt_display": True, "kind": "tv", "has_vcr": False, "has_dvd": False, "brand": None,
        "model_number": None, "is_trinitron": False, "screen_size_inches": None, "weight_lbs": None,
        "working": "unknown", "best_input": "unknown", "input_evidence": "none",
        "rgb_moddable": "unknown", "local_pickup_plausible": True, "summary": "ok", "concerns": [],
    }
    script = tmp_path / "claude"
    script.write_text(
        f"#!/bin/sh\ncat > /dev/null\nprintf '%s' \"$CLAUDE_CODE_OAUTH_TOKEN\" > {seen}\n"
        f"cat <<'JSON'\n{json.dumps({'is_error': False, 'structured_output': verdict})}\nJSON\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    ClaudeEvaluator(config, command=str(script))(make_listing(image_urls=[]))
    assert seen.read_text() == "token-from-file"


# -- location ---------------------------------------------------------------


def test_nearby_states_follow_the_search_area():
    assert nearby_states((38.5816, -121.4944), 70) == {"CA"}
    assert nearby_states((39.5296, -119.8138), 70) == {"CA", "NV"}       # Reno
    assert nearby_states((45.537, -122.65), 70) == {"OR", "WA"}          # Portland
    assert {"NY", "NJ", "CT", "PA"} <= nearby_states((40.6627, -73.9387), 70)


def test_out_of_state_listings_are_judged_from_wherever_home_is(project, make_listing):
    (project / "config.toml").write_text(
        TEST_CONFIG.replace("38.5816", "45.537").replace("-121.4944", "-122.65")
    )
    config = load_config(project / "config.toml")
    # A place the table does not know, so only the state can be judged.
    across_the_river = make_listing(lat=None, lon=None, location_text="Somewhere Small, WA")
    assert prefilter.check(across_the_river, config, keywords=False) is None
    far = make_listing(lat=None, lon=None, location_text="Somewhere Small, CA")
    assert prefilter.check(far, config, keywords=False) == "distance"
    vancouver = make_listing(lat=None, lon=None, location_text="Vancouver, WA")
    assert prefilter.check(vancouver, config, keywords=False) is None
    assert vancouver.distance_mi == pytest.approx(7, abs=4)


# -- doctor -----------------------------------------------------------------


def checks(config, **kwargs):
    return {what: (status, detail) for status, what, detail in setup.doctor(config, **kwargs)}


def test_doctor_says_what_is_missing(project, monkeypatch):
    monkeypatch.setattr(setup, "check_timer", lambda config: (setup.WARN, "not scheduled yet"))
    monkeypatch.setattr(setup, "find_claude", lambda: "/usr/bin/claude")
    cli.main(["init", "--profile", "keyboards", "--zip", "97201", "--place", "Portland, OR"])
    found = checks(load_config(project / "config.toml"))
    assert found["profile"] == (setup.OK, "keyboards: Keyboard finder")
    assert found["location"][0] == setup.OK and "OR, WA" in found["location"][1]
    assert found["secrets"][0] == setup.OK
    assert found["discord"] == (setup.FAIL, "put the channel's webhook address in .env as DISCORD_WEBHOOK_URL")
    assert found["alerts"][0] == setup.OK
    assert found["ebay"][0] == setup.WARN and "EBAY_CLIENT_ID" in found["ebay"][1]
    assert found["facebook"] == (setup.OK, "off")
    assert found["craigslist"][0] == setup.OK
    assert cli.main(["doctor"]) == 1

    with (project / ".env").open("a") as env:
        env.write("DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/1/abc\n")
    env_text = (project / ".env").read_text().replace("DISCORD_WEBHOOK_URL=\n", "")
    (project / ".env").write_text(env_text)
    assert checks(load_config(project / "config.toml"))["discord"][0] == setup.OK
    assert cli.main(["doctor"]) == 0


def test_doctor_notices_a_secrets_file_that_is_exposed(project, monkeypatch):
    (project / "config.toml").write_text(TEST_CONFIG)
    setup.ensure_env(project / ".env")
    config = load_config(project / "config.toml")
    (project / ".env").chmod(0o644)
    assert setup.check_env_file(config)[0] == setup.WARN
    (project / ".env").chmod(0o600)

    git = ["git", "-C", str(project), "-c", "user.name=t", "-c", "user.email=t@example.org"]
    subprocess.run([*git, "init", "-q"], check=True)
    status, detail = setup.check_env_file(config)
    assert status == setup.WARN and ".gitignore" in detail
    (project / ".gitignore").write_text(".env\n")
    assert setup.check_env_file(config)[0] == setup.OK
    subprocess.run([*git, "add", "-f", ".env"], check=True)
    status, detail = setup.check_env_file(config)
    assert status == setup.FAIL and "tracked by git" in detail


def test_doctor_checks_facebook_only_when_it_is_on(config):
    assert setup.check_source(config, "facebook") == (setup.FAIL, "on, but not logged in; run `marketplace-finder fb-login`")
    config.sources["facebook"].options["location"] = ""
    assert "`location` is not set" in setup.check_source(config, "facebook")[1]
    config.sources["facebook"].enabled = False
    assert setup.check_source(config, "facebook") == (setup.OK, "off")


# -- timer ------------------------------------------------------------------


def test_timer_units_point_at_this_install():
    units = setup.timer_units(
        Path("/srv/finder/config.toml"),
        Path("/srv/finder/.venv/bin/marketplace-finder"),
        extra_path=["/opt/node/bin", "/usr/bin"],
    )
    service = units["service"]
    assert "WorkingDirectory=/srv/finder\n" in service
    assert "Environment=MARKETPLACE_FINDER_CONFIG=/srv/finder/config.toml\n" in service
    assert "Environment=PATH=/opt/node/bin:/usr/bin:%h/.local/bin:/usr/local/bin:/bin\n" in service
    assert service.rstrip().splitlines()[-2].endswith('"/srv/finder/.venv/bin/marketplace-finder"')
    assert "MARKETPLACE_FINDER_DATA" not in service
    assert "OnCalendar=hourly" in units["timer"] and "WantedBy=timers.target" in units["timer"]


def test_timer_units_cope_with_awkward_paths():
    units = setup.timer_units(
        Path("/home/me/my finder 100%/config.toml"),
        Path("/home/me/my finder 100%/.venv/bin/marketplace-finder"),
        data_dir="/data/second install",
    )
    service = units["service"]
    assert 'Environment="MARKETPLACE_FINDER_CONFIG=/home/me/my finder 100%%/config.toml"\n' in service
    assert 'Environment="MARKETPLACE_FINDER_DATA=/data/second install"\n' in service
    assert "WorkingDirectory=/home/me/my finder 100%%\n" in service
    assert '"/home/me/my finder 100%%/.venv/bin/marketplace-finder"' in service


def test_install_timer_can_show_the_units_without_touching_anything(project, monkeypatch, capsys):
    (project / "config.toml").write_text(TEST_CONFIG)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(project / "xdg"))
    assert cli.main(["install-timer", "--print", "--name", "second-finder"]) == 0
    out = capsys.readouterr().out
    assert f"WorkingDirectory={project}\n" in out and "second-finder.timer" in out
    assert f"MARKETPLACE_FINDER_DATA={os.environ['MARKETPLACE_FINDER_DATA']}" in out
    assert not (project / "xdg").exists()


# -- found in review --------------------------------------------------------


def test_a_named_config_that_is_missing_is_an_error_not_a_fallback(project, monkeypatch):
    (project / "config.toml").write_text(TEST_CONFIG)  # another install's, in the way
    monkeypatch.setenv("MARKETPLACE_FINDER_CONFIG", str(project / "scratch"))
    with pytest.raises(ConfigError, match="is not a file"):
        load_config()


def test_a_config_file_can_have_any_name(project, monkeypatch, capsys):
    (project / "keyboards.toml").write_text(TEST_CONFIG)
    monkeypatch.setenv("MARKETPLACE_FINDER_CONFIG", str(project / "keyboards.toml"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(project / "xdg"))
    assert load_config().path == project / "keyboards.toml"
    assert cli.main(["install-timer", "--print"]) == 0
    assert f"MARKETPLACE_FINDER_CONFIG={project / 'keyboards.toml'}\n" in capsys.readouterr().out


@pytest.mark.parametrize("old,new,complaint", [
    ("[sources.craigslist]\nenabled = true", '[sources.craigslist]\nenabled = true\nqueries = "sony pvm"', "must be a list of strings"),
    ('[profile]\nname = "crt"', 'profile = "crt"', r"must be a \[profile\] section"),
    ("[sources.ebay]\nenabled = true", "", None),
])
def test_config_sections_of_the_wrong_shape(project, old, new, complaint):
    assert old in TEST_CONFIG
    (project / "config.toml").write_text(TEST_CONFIG.replace(old, new))
    if complaint:
        with pytest.raises(ConfigError, match=complaint):
            load_config(project / "config.toml")
    else:
        assert "ebay" not in load_config(project / "config.toml").sources


def test_posting_defaults_to_a_webhook(project):
    (project / "config.toml").write_text(TEST_CONFIG.replace('via = "discli"\n', ""))
    assert load_config(project / "config.toml").discord_via == "webhook"


def test_env_file_keeps_a_topic_that_is_set_and_reads_through_quotes(project):
    env = project / ".env"
    env.write_text('NTFY_TOPIC="quoted-topic"\n')
    assert setup.ensure_env(env) == "quoted-topic"
    assert env.read_text().startswith('NTFY_TOPIC="quoted-topic"\n')
    # A set line followed by a blank one: only the blank line is filled in.
    env.write_text("NTFY_TOPIC=first\nNTFY_TOPIC=\n")
    topic = setup.ensure_env(env)
    assert env.read_text().startswith(f"NTFY_TOPIC=first\nNTFY_TOPIC={topic}\n")
    env.write_text('NTFY_TOPIC=""\n')
    topic = setup.ensure_env(env)
    assert topic.startswith("marketplace-") and f"NTFY_TOPIC={topic}\n" in env.read_text()


@pytest.mark.parametrize("typed,area", [
    ("portland, or", "Portland"), ("McKinney, TX", "McKinney"), ("Coeur d'Alene, ID", "Coeur d'Alene"),
])
def test_area_name_is_kept_as_typed(project, typed, area):
    text = setup.new_config(project / "config.toml", **{**PORTLAND, "place": typed})
    assert f'area = "{area}"' in text


def test_timer_path_copes_with_awkward_tool_directories():
    service = setup.timer_units(
        Path("/srv/f/config.toml"), Path("/srv/f/bin/marketplace-finder"),
        extra_path=["/opt/My Tools/bin", "/opt/100%/bin"],
    )["service"]
    assert 'Environment="PATH=/opt/My Tools/bin:/opt/100%%/bin:%h/.local/bin:/usr/local/bin:/usr/bin:/bin"\n' in service


def test_doctor_finds_this_installs_timer_whatever_it_is_called(config, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    units = setup.unit_dir()
    units.mkdir(parents=True)
    other = setup.timer_units(Path("/elsewhere/config.toml"), Path("/elsewhere/mf"))
    (units / "marketplace-finder.service").write_text(other["service"])
    (units / "marketplace-finder.timer").write_text(other["timer"])
    assert setup.installed_units(config) == []
    mine = setup.timer_units(config.path, Path("/x/marketplace-finder"))
    (units / "keyboard-finder.service").write_text(mine["service"])
    (units / "keyboard-finder.timer").write_text(mine["timer"])
    assert setup.installed_units(config) == ["keyboard-finder"]


def test_doctor_knows_when_facebook_was_logged_out(config):
    from marketplace_finder.db import Database

    (config.data_dir / "facebook-profile").mkdir()
    assert setup.check_source(config, "facebook")[0] in (setup.OK, setup.WARN)
    db = Database(config.db_path)
    db.update_health("facebook", needs_attention=1, last_error="logged out")
    db.close()
    assert setup.check_source(config, "facebook")[0] == setup.FAIL
