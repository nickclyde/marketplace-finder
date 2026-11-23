from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import shutil
import sys

from . import db as dbm
from . import launch as launching
from . import prefilter, setup
from .config import CONFIG_ENV, DATA_ENV, config_candidates, load_config
from .db import Database, row_to_listing
from .discord import DiscordError, build_notifier, discord_post
from .errors import ConfigError
from .evaluate import ClaudeEvaluator, EvaluationError
from .launch import ClaudeRanker
from .notify import Notifications, Notifier
from .pipeline import Pipeline, evaluate_listing, in_quiet_hours
from .sources import SOURCE_NAMES, build_source, enabled_sources
from .sources.base import ListingGone, SourceError, SourceSkipped

log = logging.getLogger("marketplace_finder")


def cmd_run(args) -> int:
    config = load_config()
    if in_quiet_hours(config) and not args.force:
        log.info("quiet hours; not running (use --force to override)")
        return 0

    lock = open(config.data_dir / "run.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.warning("another run is in progress; exiting")
        return 0

    db = Database(config.db_path)
    sources = enabled_sources(config)
    references = config.profile.references(db, config)
    try:
        pipeline = Pipeline(
            config,
            db,
            sources,
            ClaudeEvaluator(config),
            Notifications(build_notifier(config), Notifier(config)),
            references,
            ClaudeRanker(config),
        )
        summary = pipeline.run(post=not args.no_post)
    finally:
        for source in sources.values():
            source.close()
        if references:
            references.close()
        db.close()
    log.info("run finished: %s", json.dumps(summary))
    return 0


def cmd_search(args) -> int:
    """Query one source and print what it returns. Nothing is stored."""
    config = load_config()
    if args.source not in config.sources:
        print(f"config.toml has no [sources.{args.source}] section")
        return 1
    source = build_source(args.source, config)
    queries = [args.query] if args.query else source.source_config.queries
    seen = set()
    try:
        for query in queries:
            print(f"== {args.source}: {query!r}")
            for listing in source.search(query):
                if listing.source_id in seen:
                    continue
                seen.add(listing.source_id)
                reason = prefilter.check(
                    listing,
                    config,
                    keywords=source.title_keywords or args.keywords,
                    strict=source.strict_keywords,
                )
                distance = "?" if listing.distance_mi is None else f"{listing.distance_mi:.0f}mi"
                print(
                    f"  [{reason or 'ok':9}] {listing.price_text:>8}  {distance:>6}  "
                    f"{listing.location_text[:22]:22}  {listing.title[:70]}"
                )
                if args.urls:
                    print(f"              {listing.url}")
    except SourceSkipped as exc:
        print(f"skipped: {exc}")
    except SourceError as exc:
        print(f"error: {type(exc).__name__}: {exc}")
        return 1
    finally:
        source.close()
    print(f"{len(seen)} listings")
    return 0


def cmd_evaluate(args) -> int:
    """Evaluate one stored listing again and print the verdict (optionally post it)."""
    config = load_config()
    profile = config.profile
    db = Database(config.db_path)
    source_name, _, source_id = args.listing.partition(":")
    row = db.get(source_name, source_id)
    if row is None:
        print(f"no stored listing {args.listing}")
        return 1
    listing = row_to_listing(row)
    if not row["detail_fetched"]:
        source = build_source(listing.source, config)
        try:
            source.enrich(listing)
            prefilter.fill_distance(listing, config)
            db.save_detail(listing)
        except ListingGone:
            print("listing is no longer available")
            return 1
        finally:
            source.close()
    evaluator = ClaudeEvaluator(config)
    references = profile.references(db, config)
    try:
        verdict, _ = evaluate_listing(evaluator, references, listing, profile)
    except EvaluationError as exc:
        print(f"evaluation failed: {exc}")
        return 1
    finally:
        if references:
            references.close()
    passed = profile.passes(verdict)
    db.add_evaluation(listing, ", ".join(evaluator.last_models) or config.model, verdict, passed)
    print(f"{listing.title}\n{listing.price_text} · {listing.location_text} · {listing.url}")
    shown = dict(verdict)
    reference = shown.pop("reference", None)
    print(json.dumps(shown, indent=2))
    if reference:
        print(f"reference ({reference['matched']}): {reference['name']}  {reference['url']}")
    print("=> MATCH" if passed else "=> rejected")
    if args.notify and not passed:
        print("not posted: the listing did not pass")
    elif args.notify:
        discord = build_notifier(config, channel=args.channel, test=args.test)
        sent = discord.send_match(listing, verdict)
        print(f"posted to {discord.target}" if sent else "not posted (see log above)")
        if sent and discord.is_main:
            # It is in the real channel now, so a run must not post it again.
            db.add_notification(listing)
            db.set_status(listing, dbm.NOTIFIED)
    return 0


def print_post(post: dict, indent: str = "    ") -> None:
    """A Discord post as text, for the terminal."""
    lines = [post["author"]] if post["author"] else []
    lines += post["description"].splitlines()
    lines += [f"{name}: {value}".replace("\n", " · ") for name, value, _ in post["fields"]]
    lines.append(post["footer"])
    print(indent + f"\n{indent}".join(lines))


def cmd_matches(args) -> int:
    config = load_config()
    db = Database(config.db_path)
    rows = db.with_status(dbm.NOTIFIED) + db.with_status(dbm.SEEDED) + db.with_status(dbm.MATCHED)
    for row in rows:
        listing = row_to_listing(row)
        verdict = db.latest_verdict(listing.source, listing.source_id) or {}
        post = discord_post(listing, verdict, config)
        print(f"{row['first_seen_at'][:16]}  [{row['status']}]  {post['title']}")
        print(f"    {listing.url}")
        print_post(post)
    print(f"{len(rows)} matches")
    return 0


def launch_status(db: Database) -> str:
    record = db.get_state(launching.STATE_KEY) or {}
    if record.get("done"):
        return f"opening post sent ({len(record['posted'])} picks); new listings post as they appear"
    if "picks" in record:
        return f"opening post partly sent ({len(record['posted'])} of {len(record['picks'])} picks)"
    counts = db.counts()
    return (
        f"warming up, nothing posted yet: {counts.get(dbm.MATCHED, 0)} listings passed, "
        f"{counts.get(dbm.PENDING, 0)} waiting to be evaluated"
    )


def cmd_launch(args) -> int:
    """Show where the opening post stands, or preview it without posting anything."""
    config = load_config()
    db = Database(config.db_path)
    print(launch_status(db))
    if not args.preview:
        return 0
    candidates = []
    for row in db.with_status(dbm.MATCHED):
        verdict = db.latest_verdict(row["source"], row["source_id"])
        if verdict is not None:
            candidates.append((row_to_listing(row), verdict))
    if not candidates:
        print("no listings have passed yet, so there is nothing to choose from")
        return 0
    wanted = config.discord_top_picks
    try:
        picks = launching.choose(ClaudeRanker(config)(candidates, wanted), candidates, wanted)
    except EvaluationError as exc:
        print(f"choosing the picks failed: {exc}")
        return 1
    by_key = {listing.key: (listing, verdict) for listing, verdict in candidates}
    print(f"\nPreview from {len(candidates)} listings. Nothing is posted or recorded.\n")
    print(config.discord_welcome + "\n")
    for position, pick in enumerate(picks, start=1):
        listing, verdict = by_key[pick["key"]]
        post = discord_post(
            listing, verdict, config, rank=(position, len(picks)), reason=pick["reason"]
        )
        print(f"{post['title']}\n    {listing.url}")
        print_post(post)
        print()
    return 0


def cmd_status(args) -> int:
    config = load_config()
    db = Database(config.db_path)
    print(f"profile: {config.profile.name}")
    print(f"database: {config.db_path}")
    print("listings: " + (", ".join(f"{k}={v}" for k, v in sorted(db.counts().items())) or "none"))
    print(f"ntfy topic: {'set' if config.ntfy_topic else 'NOT SET (run `marketplace-finder init`)'}")
    try:
        discord = build_notifier(config)
    except DiscordError:
        discord = None
    if discord is not None and discord.configured:
        print(f"discord: {discord.target}; {launch_status(db)}")
    else:
        print("discord: NOT SET UP (run `marketplace-finder doctor`); listings will not be posted")
    for name in SOURCE_NAMES:
        source_config = config.sources.get(name)
        state = "enabled" if source_config and source_config.enabled else "disabled"
        if name == "ebay" and state == "enabled" and not config.ebay_client_id:
            state = "enabled, waiting for API keys"
        print(f"  {name}: {state}")
    for row in db.all_health():
        if row["failures"] or row["needs_attention"]:
            flag = "NEEDS ATTENTION" if row["needs_attention"] else f"{row['failures']} failures"
            pause = f", paused until {row['paused_until']}" if row["paused_until"] else ""
            print(f"  ! {row['name']}: {flag}{pause}: {row['last_error']}")
    for row in db.last_runs():
        print(f"run {row['id']}  {row['started_at']} -> {row['finished_at'] or 'unfinished'}")
        if row["summary"]:
            summary = json.loads(row["summary"])
            for name, result in summary.get("sources", {}).items():
                print(f"    {name}: {result}")
            print(f"    evaluated: {summary.get('evaluated')}  posted: {summary.get('notified')}")
            if summary.get("launch"):
                print(f"    opening post: {summary['launch']}")
    return 0


def cmd_init(args) -> int:
    """Create config.toml and .env where they are missing. Nothing that exists is replaced."""
    # The first place a run would look: the path in the environment, else this directory.
    config_path = config_candidates()[0]
    if config_path.is_file():
        print(f"{config_path} already exists; left as it is.")
    else:
        text = setup.new_config(
            config_path, profile=args.profile, postal_code=args.zip, place=args.place
        )
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(text)
        print(f"wrote {config_path}")
    config = load_config(config_path)
    had_env = config.env_path.exists()
    topic = setup.ensure_env(config.env_path)
    print(f"{'checked' if had_env else 'wrote'} {config.env_path} (readable only by you)")
    print(
        f"""
Next:
  1. Discord: in the channel's settings, Integrations > Webhooks > New Webhook, copy its
     URL and put it in {config.env_path.name} as DISCORD_WEBHOOK_URL.
  2. Alerts: in the ntfy app, subscribe to this topic on {config.ntfy_server}:

         {topic}

  3. Look over {config_path.name} (radius, price limit, sources), then:

         marketplace-finder doctor
         marketplace-finder notify-test --alert
         marketplace-finder run --no-post
"""
    )
    return 0


def cmd_doctor(args) -> int:
    """Check that everything a run needs is in place, and say what to do about what is not."""
    config = load_config()
    checks = setup.doctor(config, live=args.live)
    width = max(len(what) for _, what, _ in checks)
    for status, what, detail in checks:
        print(f"[{status:^4}] {what:{width}}  {detail}")
    failed = sum(1 for status, _, _ in checks if status == setup.FAIL)
    print("\nready to run" if not failed else f"\n{failed} thing(s) to fix before a run will work")
    return 1 if failed else 0


def cmd_install_timer(args) -> int:
    """Install (or remove) the systemd user timer that runs `run` every hour."""
    config = load_config()
    service = setup.unit_dir() / f"{args.name}.service"
    timer = setup.unit_dir() / f"{args.name}.timer"
    units = setup.timer_units(
        config.path.resolve(),
        setup.program_path(),
        data_dir=os.environ.get(DATA_ENV),
        extra_path=setup.tool_dirs(),
    )
    if args.print:
        print(f"# {service}\n{units['service']}\n# {timer}\n{units['timer']}", end="")
        return 0
    if not shutil.which("systemctl"):
        print(
            "systemd was not found on this machine. Schedule this command hourly some other "
            f"way (cron, for example):\n\n    {CONFIG_ENV}={config.path.resolve()} "
            f"{setup.program_path()} run"
        )
        return 1
    if args.remove:
        setup.systemctl("disable", "--now", timer.name)
        for path in (service, timer):
            path.unlink(missing_ok=True)
        setup.systemctl("daemon-reload")
        print(f"removed {timer.name} and {service.name}")
        return 0
    service.parent.mkdir(parents=True, exist_ok=True)
    service.write_text(units["service"])
    timer.write_text(units["timer"])
    print(f"wrote {service}\nwrote {timer}")
    if not (setup.systemctl("daemon-reload") and setup.systemctl("enable", "--now", timer.name)):
        print("systemctl reported a problem; see above")
        return 1
    print(
        f"""
{timer.name} is running: one run an hour, outside the quiet hours in config.toml.

  Keep it running while you are logged out:   loginctl enable-linger "$USER"
  See what a run did:                         journalctl --user -u {args.name}
  Stop it:                                    marketplace-finder install-timer --remove"""
    )
    return 0


def cmd_notify_test(args) -> int:
    """Post a sample listing to Discord, or with --alert send a sample ntfy alert."""
    config = load_config()
    if args.alert:
        ok = Notifier(config).send_alert(
            f"{config.profile.label}: test alert",
            "This is a test from marketplace-finder. Nothing is wrong.",
        )
        print("sent" if ok else "not sent (see log above)")
        return 0 if ok else 1
    listing, verdict = config.profile.sample(config)
    discord = build_notifier(config, channel=args.channel, test=args.test)
    ok = discord.send_match(listing, verdict)
    print(f"posted to {discord.target}" if ok else "not posted (see log above)")
    return 0 if ok else 1


def cmd_fb_login(args) -> int:
    from .sources.facebook import interactive_login

    config = load_config()
    interactive_login(config)
    Database(config.db_path).update_health(
        "facebook", needs_attention=0, failures=0, last_error=None, paused_until=None, alerted=0
    )
    print("Facebook session saved. Set [sources.facebook] enabled = true in config.toml.")
    return 0


def add_target_options(parser) -> None:
    parser.add_argument("--channel", help="discli: post to this channel instead of the configured one")
    parser.add_argument(
        "--test", action="store_true", help="webhook: post to DISCORD_TEST_WEBHOOK_URL instead"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="marketplace-finder")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="search all sources, evaluate new listings, post matches")
    run.add_argument("--force", action="store_true", help="run even during quiet hours")
    run.add_argument(
        "--no-post", action="store_true", help="search and evaluate only; send nothing to Discord"
    )
    run.set_defaults(func=cmd_run)

    search = sub.add_parser("search", help="print one source's results without storing them")
    search.add_argument("--source", required=True, choices=SOURCE_NAMES)
    search.add_argument("--query")
    search.add_argument("--urls", action="store_true")
    search.add_argument(
        "--keywords",
        action="store_true",
        help="apply the profile's keywords to titles, as a run does once it has the description",
    )
    search.set_defaults(func=cmd_search)

    evaluate = sub.add_parser("evaluate", help="re-evaluate a stored listing, e.g. craigslist:123")
    evaluate.add_argument("listing")
    evaluate.add_argument("--notify", action="store_true", help="also post it to Discord")
    add_target_options(evaluate)
    evaluate.set_defaults(func=cmd_evaluate)

    launch = sub.add_parser("launch", help="show where the opening post stands")
    launch.add_argument(
        "--preview", action="store_true", help="print the welcome and picks; posts nothing"
    )
    launch.set_defaults(func=cmd_launch)

    sub.add_parser("matches", help="list listings that passed").set_defaults(func=cmd_matches)
    sub.add_parser("status", help="show counts, source health and recent runs").set_defaults(
        func=cmd_status
    )

    init = sub.add_parser("init", help="create config.toml and .env")
    init.add_argument("--profile", help="what to look for: a built-in name, or a profile file")
    init.add_argument("--zip", help="postal code searches are centred on")
    init.add_argument("--place", help='the same place as "City, ST", for distances')
    init.set_defaults(func=cmd_init)

    doctor = sub.add_parser("doctor", help="check the setup and say what is missing")
    doctor.add_argument(
        "--live", action="store_true", help="also make one small request to check claude is signed in"
    )
    doctor.set_defaults(func=cmd_doctor)

    timer = sub.add_parser("install-timer", help="run every hour with a systemd user timer")
    timer.add_argument("--remove", action="store_true", help="stop and delete the timer")
    timer.add_argument("--print", action="store_true", help="show the unit files; change nothing")
    timer.add_argument(
        "--name", default=setup.DEFAULT_UNIT, help="unit name, for a second install on one machine"
    )
    timer.set_defaults(func=cmd_install_timer)

    test = sub.add_parser("notify-test", help="post a sample listing to Discord")
    add_target_options(test)
    test.add_argument("--alert", action="store_true", help="send a sample ntfy alert instead")
    test.set_defaults(func=cmd_notify_test)
    sub.add_parser("fb-login", help="open a browser window to log in to Facebook").set_defaults(
        func=cmd_fb_login
    )

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs each request's address, which for a webhook is the credential itself.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        return args.func(args)
    except (ConfigError, DiscordError) as exc:
        print(f"marketplace-finder: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
