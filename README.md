# marketplace-finder

Searches craigslist, OfferUp, eBay and Facebook Marketplace every hour for
whatever you collect, has Claude look at each new listing (text and photos),
and posts the ones that fit to a Discord channel.

What "fits" is a **profile**. Two ship with the project: `crt` (CRT televisions
and monitors, which is what this was first written for) and `keyboards` (vintage
and mechanical computer keyboards). A new one is a single TOML file.

> **Status:** shared as-is. It runs for its author, but it is not actively
> maintained and comes with no support. The marketplaces change their sites
> without notice, so expect to fix things yourself.

Limits worth knowing before you start: United States only, and local pickup
only (it looks for things within a radius of you, not for things that ship).

## Quick start

You need Linux, Python 3.12 or newer, [uv](https://docs.astral.sh/uv/), the
`claude` command (Claude Code) installed and signed in, and a Discord channel
you can add a webhook to.

```sh
git clone <this repo> marketplace-finder && cd marketplace-finder
uv sync
uv run marketplace-finder init --profile keyboards --zip 97201 --place "Portland, OR"
```

`init` writes `config.toml` and a private `.env`, and prints what to do next:

1. In Discord, open the channel's settings, then Integrations > Webhooks > New
   Webhook. Copy its URL into `.env` as `DISCORD_WEBHOOK_URL`.
2. Install the ntfy app (iOS/Android) and subscribe to the topic `init`
   printed. That is where you hear about problems.
3. Look over `config.toml`: radius, price limit, which sources are on.

Then check it, try it, and schedule it:

```sh
uv run marketplace-finder doctor            # says what is missing, if anything
uv run marketplace-finder notify-test       # posts one sample listing to the channel
uv run marketplace-finder run --no-post     # a real search and evaluation; posts nothing
uv run marketplace-finder install-timer     # hourly from now on
loginctl enable-linger "$USER"              # keep it running while you are logged out
```

Nothing is posted for the first few runs. See [The opening post](#the-opening-post).

Each new listing that gets past the keyword check costs one Claude call (two
when a reference entry turns up on a second look), capped at `max_per_run` per
run. Keep an eye on your usage for the first day.

## How a run works

1. Each enabled source is searched; results are stored in SQLite
   (`~/.local/share/marketplace-finder/listings.db`).
2. Listings not seen before go through code checks: price, distance, pickup,
   and the profile's keywords. Failures are recorded and never reach Claude.
3. Survivors get their full description and photos fetched.
4. `claude -p` fills in the profile's fields (what it is, brand, model, whether
   it works, and whatever else the profile asks) from the listing and its
   photos.
5. Code applies the profile's rules to those answers. Passing listings are
   posted to Discord with the first photo, any badges they earned, and a short
   list of things to confirm with the seller.

Claude describes; code decides. Unknowns (untested, not stated) do not reject
a listing, they show up in the post as things to confirm. Only an explicit
failure rejects.

At most `max_posts_per_run` listings are posted per run; the rest follow on the
next one.

## Profiles

Set the profile in `config.toml`:

```toml
[profile]
name = "keyboards"          # or "crt"
```

### Writing your own

Copy [`src/marketplace_finder/profiles/keyboards.toml`](src/marketplace_finder/profiles/keyboards.toml)
next to your `config.toml`, edit it, and point at the copy:

```toml
[profile]
path = "cameras.toml"
```

That file is commented line by line. In short, a profile has:

| Part | What it does |
|---|---|
| `item`, `audience`, `wanted`, `not_wanted` | Tell Claude what is being looked for and for whom |
| `queries` | The searches to run on each source |
| `[keywords]` | Checked in code first, so unrelated search results cost nothing |
| `[[fields]]` | Extra things Claude fills in for each listing |
| `reject` on a field | Answers that rule a listing out |
| `badges` on a field | Answers that earn flair at the top of the post |
| `label` on a field | Show the answer in the post |
| `ranking` | What "best value" means for the opening post |

Only `item` and `queries` are required. `marketplace-finder doctor` reports any
mistake in the file, and `search --source craigslist --keywords` shows which
titles your keywords would keep.

The `crt` profile is written in Python
([`profiles/crt.py`](src/marketplace_finder/profiles/crt.py)) because its
keyword rules were tuned by hand and it looks models up on
[crtdatabase.com](https://crtdatabase.com). Use it as the model if TOML is not
enough for your search.

To run two searches on one machine, use two clones (or two config directories),
each with its own `MARKETPLACE_FINDER_DATA` directory, and give the second
timer its own name: `install-timer --name keyboard-finder`.

### The crt profile

Any brand and size of CRT television, TV/VCR combo, professional video monitor
or CRT computer monitor, working or untested. Badges: ⭐ Sony Trinitron, 🌈 RGB
input or a known RGB mod, 🖥️ computer monitor, 📼 built-in VCR, 📀 built-in DVD.

When a listing names a model that crtdatabase.com has an entry for, the entry
goes to Claude with the listing and is linked in the post, along with the set's
weight. Lookups use the site's brand and model lists, wait three seconds between
requests as its robots.txt asks, and are cached (lists for a week, entries for a
month). Turn them off with `crtdatabase = false` under `[reference]`. Entry text
is CC BY-NC-SA 4.0 and is not redistributed here.

## Sources

**craigslist** and **OfferUp** need no setup.

**eBay** is skipped until keys exist. Join the eBay developer program, create a
Production keyset, and on its Alerts & Notifications page choose the exemption
for apps that do not persist eBay user data (this tool stores no seller or
buyer details). Put the App ID and Cert ID in `.env` as `EBAY_CLIENT_ID` and
`EBAY_CLIENT_SECRET`.

**Facebook Marketplace** is off by default and takes the most work:

```sh
uv run playwright install chromium
uv run playwright install-deps chromium   # system libraries; asks for sudo
sudo apt install xvfb
uv run marketplace-finder fb-login        # opens a browser: log in, set your location and radius
```

`fb-login` needs a screen. On a headless machine, connect from a computer that
has one with `ssh -Y <host>` and run it there; the window opens on your
computer (macOS needs XQuartz first).

Then, under `[sources.facebook]` in `config.toml`, set `location` to the last
part of `facebook.com/marketplace/<location>/` and `enabled = true`. If Facebook
ever shows a login or verification page, the source stops, sends one alert, and
waits for you to run `fb-login` again. Consider using an account you would not
mind losing.

## Discord

**Webhook** (the default): one URL in `.env`, nothing to install. A webhook
posts to one channel. To try things out somewhere else first, make a second
webhook in a test channel, put it in `.env` as `DISCORD_TEST_WEBHOOK_URL`, and
use `notify-test --test`.

**discli**: if you already run a bot through
[`discli`](https://www.npmjs.com/package/@ibbybuilds/discli), set
`via = "discli"` and `channel = "your-channel"` under `[discord]`. The bot needs
to send messages, embeds and attachments there. `notify-test --channel NAME`
posts to any channel the bot can see.

Text from listings only ever appears inside the post's embed, with markdown and
links disarmed, so a seller cannot ping your server or plant a link.

## Alerts

Problems (a source failing three runs in a row, Facebook logged out,
evaluations or Discord posts failing) go to your phone through
[ntfy](https://ntfy.sh), never to the channel. `notify-test --alert` sends a
sample. The topic name is the only thing protecting those alerts, which is why
`init` makes a long random one.

## The opening post

A new database starts in a warm-up. Runs search and evaluate as usual but post
nothing, until a run finishes with every listing currently for sale evaluated.
That run posts a welcome message, then the ten best-value listings
(`top_picks`), which Claude chooses from the ones that passed and were still in
the search results. Everything else that had passed is marked `seeded` and never
posted. From then on only new listings are posted.

Warm-up usually takes several runs, because a run is limited to `max_per_run`
evaluations. `status` and `launch` show how far along it is, and
`launch --preview` prints what the opening post would look like right now
without posting anything. The welcome wording comes from the profile; set
`welcome` under `[discord]` to write your own, or `top_picks = 0` and
`welcome = ""` to skip the opening post.

## Running on a server

- **Claude sign-in.** If you cannot log in to `claude` on the server itself,
  run `claude setup-token` on a computer with a browser and put the token in
  the server's `.env` as `CLAUDE_CODE_OAUTH_TOKEN` (or use `ANTHROPIC_API_KEY`).
  `doctor --live` makes one small request to confirm it works.
- **Schedule.** `install-timer` writes a systemd user timer that points at
  wherever you cloned the project. `install-timer --print` shows the unit files
  without installing them, and `--remove` takes them out again. Quiet hours
  (no runs overnight, by default) are set in `config.toml`.
- **Logs.** `journalctl --user -u marketplace-finder`.
- **No systemd?** Run `marketplace-finder run` from cron once an hour.

## Where your settings and secrets live

| What | Where | In git? |
|---|---|---|
| Settings | `config.toml` | No (ignored); `config.example.toml` is the tracked sample |
| Secrets: webhook URL, ntfy topic, eBay keys, Claude token | `.env`, mode 600 | No (ignored) |
| Database, run lock, Facebook browser profile | `~/.local/share/marketplace-finder/` | Outside the repo |
| discli's bot token, if you use discli | `~/.discli/` | Outside the repo |

`config.toml` is looked for at `$MARKETPLACE_FINDER_CONFIG`, then in the
current directory, then in `~/.config/marketplace-finder/`. `.env` is read from
the same directory, and real environment variables take precedence over it.
`doctor` warns if `.env` is readable by others or tracked by git.

## Commands

| Command | What it does |
|---|---|
| `init --profile P --zip Z --place "City, ST"` | Create `config.toml` and `.env`; never replaces either |
| `doctor [--live]` | Check the setup and say what is missing |
| `run [--force] [--no-post]` | One full run (`--force` ignores quiet hours; `--no-post` sends nothing to Discord) |
| `install-timer [--print] [--remove] [--name N]` | Run hourly with a systemd user timer |
| `search --source NAME [--query Q] [--urls] [--keywords]` | Print a source's results and what the first checks make of them (`--keywords` applies the profile's keywords to titles); nothing is stored |
| `evaluate SOURCE:ID [--notify]` | Re-evaluate a stored listing and print Claude's answers; `--notify` posts it |
| `matches` | Listings that passed |
| `status` | Counts, source health, recent runs |
| `launch [--preview]` | Where the opening post stands |
| `notify-test [--test] [--channel NAME]` | Post a sample listing to Discord |
| `notify-test --alert` | Send a sample ntfy alert |
| `fb-login` | Log in to Facebook in a browser window |

Add `-v` before the command for debug logging.

## Notes

- craigslist, OfferUp and Facebook do not offer a search API. This uses the
  same endpoints their websites use, a few requests an hour, and their terms do
  not permit automated access. That is your risk to weigh. A source that
  returns 403 or a challenge is skipped, and after three failed runs it is
  paused for 24 hours with one alert. Do not loop the live commands.
- Those endpoints are undocumented and can change. Parsers are covered by
  fixture tests, and `status` shows which source is failing.
- Distances come from the listing's coordinates when it has them, otherwise
  from its "City, ST" using the Census Bureau's list of places
  (`scripts/build_places.py` rebuilds it).

## Development

```sh
uv run pytest        # fixture-based; no network, no posting
```

Adding a marketplace means subclassing `Source` in
`src/marketplace_finder/sources/` and registering it in `sources/__init__.py`.
`CLAUDE.md` has an overview of how the pieces fit.

## License

MIT. See [LICENSE](LICENSE).
