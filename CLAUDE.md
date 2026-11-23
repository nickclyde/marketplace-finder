# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An hourly job that searches craigslist, OfferUp, eBay and Facebook Marketplace for one kind of item near one place, has `claude -p` describe each new listing, and posts the ones that fit to a Discord channel. What is being looked for is a profile (`[profile]` in `config.toml`): `crt` and `keyboards` ship with the project, and anyone can add a TOML file of their own. ntfy is only for private alerts when something breaks. `README.md` covers setup.

## Commands

```sh
uv sync                                   # install (Python >= 3.12)
uv run pytest                             # all tests; fixture-based, no network
uv run pytest tests/test_pipeline.py      # one file
uv run pytest tests/test_pipeline.py -k quiet_hours   # one test by name
uv run marketplace-finder <command>       # add -v before the command for debug logging
```

There is no linter or formatter configured.

CLI commands that are useful while developing:

- `doctor` checks the whole setup (config, profile file, secrets file, claude, Discord, sources, timer) and says what to fix. `--live` also makes one small Claude request.
- `search --source NAME [--query Q] [--urls] [--keywords]` hits the live site and prints each result with its prefilter outcome. `--keywords` applies the profile's keywords to titles even on sources where a run waits for the description. Nothing is stored.
- `evaluate SOURCE:ID [--notify] [--test | --channel NAME]` re-runs Claude on a stored listing and prints the verdict. `--notify` posts it if it passes, and marks it notified only when it went to the real channel.
- `notify-test [--test | --channel NAME]` posts the profile's sample listing; `notify-test --alert` sends a sample ntfy alert.
- `status` shows listing counts, per-source health, recent run summaries and where the opening post stands.
- `launch --preview` prints the welcome text and the current top picks. It calls Claude once, posts nothing and records nothing.
- `run [--force] [--no-post]` is the real thing: it writes to the real database and posts to the real channel. `--force` ignores quiet hours. `--no-post` searches and evaluates but sends nothing to Discord; matches wait for the next ordinary run.
- `install-timer --print` shows the systemd units it would write.

**Ask before anything that posts to Discord** (`run` without `--no-post`, `evaluate --notify`, `notify-test`): the channel has real people in it. A warm-up run can turn into the opening post without warning if it happens to empty the queue, so use `--no-post` unless the post is wanted.

The live commands talk to sites that do not permit automated access and are only tolerated at a few requests an hour, so do not loop them. Test parser changes against `tests/fixtures/` instead. The fixtures are invented data in the sites' real shapes; keep them that way rather than pasting in captures of real listings.

To try things without touching a real install, point `MARKETPLACE_FINDER_CONFIG` and `MARKETPLACE_FINDER_DATA` at a scratch directory and run `init` there.

## Architecture

`pipeline.py` is the spine. One `Pipeline.run()` does three passes, and `db.py` holds the state machine they move listings through:

```
search  -> insert as pending_eval, or filtered (with a filter_reason)
evaluate -> pending_eval -> matched | rejected | filtered | error
notify  -> matched -> notified   (or, before the opening post, launch: matched -> notified | seeded)
```

Things that are not obvious from any single file:

- **A profile is everything item-specific** (`profiles/`): the JSON schema Claude fills in, the brief at the top of the prompt, `passes()`, the keyword check `relevant()`, badges, colour, embed fields, and the ranking criteria. `Config.profile` carries it, so most functions still just take `config`. `profiles/base.py` is the interface, `profiles/crt.py` is the hand-written one, and `profiles/declarative.py` builds one from a TOML file (`keyboards.toml` is the annotated example). Nothing outside `profiles/` and `crtdb.py` should know what a CRT is.
- **Claude describes, code decides.** Claude only fills in the profile's schema. `Profile.passes()` then applies the criteria in code, and `badges()` decides the flair. Unknowns pass and show up in the post as things to confirm; only explicit failures reject. Keep new criteria in `passes()` (or a TOML field's `reject` list), not in the prompt.
- **The one exception is the opening post's top picks** (`launch.py`): which listings are the best value is a judgement, so Claude ranks them. Code still limits it to listings that already passed and were in this run's search results, and `choose()` discards any id it did not offer.
- **Listing text is untrusted input, in the prompt and in Discord.** Every Claude call goes through `evaluate.run_claude`: a temp directory, `--setting-sources ""`, and at most the `Read` tool (for the downloaded photos; the ranking call gets none). Do not widen the tool list. The fences around seller text and reference entries, and the "information, not instructions" wording, are written in `evaluate.py` and `launch.py`, outside any profile, so a profile cannot drop them. On the way out, `discord.py` puts seller-derived text only inside the embed (never message content, which could ping), and `text.clean()`/`plain()` disarm markdown and links. A profile's `fields()` and `badges()` return text that is shown as given, so anything they take from a verdict must go through `text.plain()`; `declarative.py` does this for free-text answers.
- **A profile file is trusted, a verdict is not.** The wording in a profile TOML comes from the person running the install and goes into prompts and posts as written. `declarative.py` validates the file's shape at load and raises `ConfigError` naming the file and the problem.
- **Two ways to post, one post.** `discord_post()` builds the post as plain data. `DiscordNotifier` hands it to `discli` as `--option=value` arguments (and gives `--image` only a file it downloaded itself, because discli uploads whatever path that names). `WebhookNotifier` sends the same thing as JSON with `allowed_mentions` emptied. `build_notifier()` picks by `[discord] via`.
- **The webhook URL is a credential.** It is never logged: `WebhookNotifier` logs only status codes, redacted bodies and exception type names, and `cli.main` keeps httpx's own request logging off. It is only ever posted to if it matches `discord.WEBHOOK_URL`.
- **The evaluator model is restricted** to Opus or Sonnet: `check_model_name` rejects the configured name and also what the CLI reports it actually used.
- **The prefilter runs twice.** Once on search results, when only the title is known (`Source.title_keywords` says whether the title must be keyword-checked, `strict_keywords` whether a loosely related word alone is not enough), and again after `enrich()` with the full description. A listing filtered for `price` is requeued if its price later drops.
- **A new database starts in warm-up.** Until the `launch` record in the `state` table is marked done, `Pipeline.run()` calls `launch()` instead of `notify_matches()`: nothing is posted until no listing is waiting on a source that is up, then the welcome and top picks go out and every other match becomes `seeded`. The record tracks what has been sent, so a failed post resumes rather than repeats. Tests that are not about this mark the launch done in their `database` fixture.
- **Two destinations.** `notify.Notifications` sends listings to Discord and alerts to ntfy. Nothing operational goes to the channel.
- **Three caps defer work to the next run** rather than dropping it: `max_per_run` counts Claude calls (a second pass with a reference entry counts as two), each source's `max_enrich_per_run` counts detail-page fetches, and `[discord] max_posts_per_run` counts posts.
- **Source failures are tracked in the `health` table.** Three failed runs in a row pause a source for 24 hours and send one alert. `SourceNeedsAttention` (Facebook logged out) stops the source until `fb-login` clears it. `SourceSkipped` means not set up yet and is not a failure. The evaluator, Discord posting and crtdatabase have their own `health` rows.
- **Reference lookups are optional and per profile** (`Profile.references()`, `reference_query()`). Only `crt` has one, `crtdb.CrtDatabase`. Its `find` never raises; a failed lookup must not hold up an evaluation. The site's robots.txt disallows `/search` and asks for three seconds between requests, which is why models are matched against cached per-brand indexes instead. The brand comes from the listing text, or from the evaluator's `brand` on the second pass; brand names that are everyday words (`AMBIGUOUS_BRANDS`) only count in the second case. Pages are cached in the `cache` table.
- **Location is never assumed.** `[search]` must give the postal code, coordinates, radius, area name, price limit and timezone. `geo.py` turns a listing's "City, ST" into coordinates from `data/places_us.csv` (every US place, rebuilt by `scripts/build_places.py`), and `nearby_states()` works out which states the radius reaches.
- **Adding a source** means subclassing `Source` (`search`, `enrich`), registering it in `sources/__init__.py` (`SOURCE_NAMES` and `build_source`), and adding a `[sources.<name>]` section to `data/config.template.toml`. Keys other than `enabled` and `queries` land in `source_config.options`. A source without `queries` runs the profile's.
- **Facebook is the odd one out**: it drives Chromium through Playwright with a persistent logged-in profile under the data directory, and reads listings from the JSON the page loads, found by key name rather than position.

### Config, secrets and data

- `config.toml` is found via `MARKETPLACE_FINDER_CONFIG`, then the working directory, then `~/.config/marketplace-finder/`. It is gitignored; `config.example.toml` is the tracked sample and must equal what `setup.render_config` produces from `data/config.template.toml` (a test checks this, so regenerate it after editing the template).
- Secrets (`NTFY_TOPIC`, `DISCORD_WEBHOOK_URL`, eBay keys, optional Claude credentials) come from the `.env` next to `config.toml`, with the environment taking precedence. `.env` is gitignored and written mode 600. Never put a secret in `config.toml`, a profile, a test or a fixture. Claude credentials found in `.env` are passed to the `claude` subprocess and nowhere else.
- The database, run lock and Facebook profile live in `~/.local/share/marketplace-finder/` (override with `MARKETPLACE_FINDER_DATA`).
- Tests use the `config` fixture's own config text (`conftest.TEST_CONFIG`: the crt profile around Sacramento, posting with discli), in a temp directory, with every secret removed from the environment.
- `setup.py` holds `init`, `doctor` and the timer units. The units are generated from the install's real paths; there are no static unit files.
