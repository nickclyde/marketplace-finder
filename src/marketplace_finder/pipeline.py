"""One run: search every source, record what is new, evaluate it, post the matches."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import db as dbm
from . import launch as launching
from . import prefilter
from .config import Config
from .db import Database, row_to_listing
from .evaluate import EvaluationError
from .models import Listing
from .sources.base import (
    ListingGone,
    Source,
    SourceNeedsAttention,
    SourceSkipped,
)

log = logging.getLogger(__name__)

FAILURES_BEFORE_ALERT = 3
PAUSE_AFTER_FAILURES = timedelta(hours=24)
MAX_EVAL_ATTEMPTS = 3
EVALUATOR = "evaluator"
DISCORD = "discord"


def evaluate_listing(evaluator, references, listing: Listing, profile) -> tuple[dict, int]:
    """Evaluate one listing, with its reference entry when there is one.

    Returns the verdict and the number of Claude calls made. A model named in the
    listing text is looked up first, so the entry goes in with the listing. When
    the model only comes out of the evaluation itself (read off a photo, or a
    corrected typo) and the reference site knows it, the listing is evaluated once
    more with the entry. The entry is stored with the verdict for the post.
    """
    reference = references.find(listing.title, listing.description) if references else None
    verdict = evaluator(listing, reference)
    calls = 1
    query = profile.reference_query(verdict) if references and reference is None else None
    if query:
        model, brand = query
        reference = references.find(model, brand=brand)
        if reference:
            calls = 2
            try:
                verdict = evaluator(listing, reference)
            except EvaluationError as exc:
                log.warning("second pass for %s failed, keeping the first: %s", listing.key, exc)
    if reference:
        verdict = {**verdict, "reference": reference}
    return verdict, calls


def in_quiet_hours(config: Config, now: datetime | None = None) -> bool:
    local = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(config.timezone))
    start, end = config.quiet_hours
    if start <= end:
        return start <= local.hour < end
    return local.hour >= start or local.hour < end


class Pipeline:
    def __init__(
        self,
        config: Config,
        db: Database,
        sources: dict[str, Source],
        evaluator,
        notifier,
        references=None,
        ranker=None,
    ):
        self.config = config
        self.profile = config.profile
        self.label = config.profile.label  # starts the title of every alert
        self.references = references
        self.ranker = ranker
        self.db = db
        self.sources = sources
        self.evaluator = evaluator
        self.notifier = notifier
        self.unavailable: set[str] = set()  # sources that failed during this run
        self.started = dbm.now_iso()

    def run(self, post: bool = True) -> dict:
        """One full run. With `post` off, nothing is sent to Discord: matches wait for a later run."""
        self.started = dbm.now_iso()
        run_id = self.db.start_run()
        summary: dict = {"sources": {}}
        for name, source in self.sources.items():
            summary["sources"][name] = self.search_source(name, source)
        summary["evaluated"] = self.evaluate_pending()
        if not post:
            summary["notified"] = 0
        elif (self.db.get_state(launching.STATE_KEY) or {}).get("done"):
            summary["notified"] = self.notify_matches()
        else:
            # Until the opening post has gone out, matches are held back for it.
            summary["launch"] = self.launch()
            summary["notified"] = summary["launch"].get("posted", 0)
        summary["totals"] = self.db.counts()
        self.db.finish_run(run_id, summary)
        return summary

    # -- search -------------------------------------------------------------

    def search_source(self, name: str, source: Source) -> dict:
        health = self.db.health(name)
        if health["needs_attention"]:
            self.unavailable.add(name)
            return {"skipped": f"needs attention: {health['last_error']}"}
        if health["paused_until"] and health["paused_until"] > dbm.now_iso():
            self.unavailable.add(name)
            return {"skipped": f"paused until {health['paused_until']}"}

        result = {"found": 0, "new": 0, "queued": 0}
        seen: set[str] = set()
        try:
            for query in source.source_config.queries:
                for listing in source.search(query):
                    if listing.source_id in seen:
                        continue
                    seen.add(listing.source_id)
                    result["found"] += 1
                    new, queued = self.ingest(listing, source)
                    result["new"] += new
                    result["queued"] += queued
        except SourceSkipped as exc:
            self.unavailable.add(name)
            return {"skipped": str(exc)}
        except Exception as exc:  # one source failing must not stop the others
            self.source_failed(name, exc)
            result["error"] = f"{type(exc).__name__}: {exc}"
            return result

        self.db.update_health(name, failures=0, last_error=None, paused_until=None, alerted=0)
        return result

    def ingest(self, listing: Listing, source: Source) -> tuple[int, int]:
        """Store a search result. Returns (is_new, queued_for_evaluation)."""
        existing = self.db.get(listing.source, listing.source_id)
        if existing is None:
            reason = self.title_check(listing, source)
            self.db.insert(listing, dbm.FILTERED if reason else dbm.PENDING, reason)
            return 1, 0 if reason else 1

        self.db.touch(listing)
        if existing["status"] == dbm.FILTERED and existing["filter_reason"] == "price":
            # A price drop can bring an earlier reject back into range.
            current = row_to_listing(self.db.get(listing.source, listing.source_id))
            if self.title_check(current, source) is None:
                self.db.set_status(current, dbm.PENDING)
                return 0, 1
        return 0, 0

    def title_check(self, listing: Listing, source: Source) -> str | None:
        """The prefilter as it applies before the listing page has been fetched."""
        return prefilter.check(
            listing,
            self.config,
            keywords=source.title_keywords,
            strict=source.strict_keywords,
        )

    def source_failed(self, name: str, exc: Exception) -> None:
        self.unavailable.add(name)
        log.error("%s failed: %s: %s", name, type(exc).__name__, exc)
        health = self.db.health(name)
        error = f"{type(exc).__name__}: {exc}"[:500]

        if isinstance(exc, SourceNeedsAttention):
            self.db.update_health(name, needs_attention=1, last_error=error)
            if self.notifier.send_alert(f"{self.label}: {name} needs you", str(exc)):
                self.db.update_health(name, alerted=1)
            return

        failures = health["failures"] + 1
        fields = {"failures": failures, "last_error": error}
        if failures >= FAILURES_BEFORE_ALERT:
            paused_until = datetime.now(timezone.utc) + PAUSE_AFTER_FAILURES
            fields["paused_until"] = paused_until.isoformat(timespec="seconds")
            if not health["alerted"] and self.notifier.send_alert(
                f"{self.label}: {name} is failing",
                f"{failures} runs in a row. Paused for 24 hours.\n{error}",
            ):
                fields["alerted"] = 1
        self.db.update_health(name, **fields)

    # -- evaluate -----------------------------------------------------------

    def evaluate_pending(self) -> dict:
        result = {"matched": 0, "rejected": 0, "filtered": 0, "errors": 0, "deferred": 0}
        enriched: dict[str, int] = {}
        consecutive_errors = 0

        evaluations = 0

        for row in self.db.with_status(dbm.PENDING):
            listing = row_to_listing(row)
            # The cap is on Claude calls; anything not reached stays queued for the next run.
            if evaluations >= self.config.max_evals_per_run:
                result["deferred"] += 1
                continue

            if not row["detail_fetched"]:
                source = self.sources.get(listing.source)
                if source is None:
                    result["deferred"] += 1
                    continue
                # The rules may have changed since this was queued; check again before
                # spending a page load on it.
                reason = self.title_check(listing, source)
                if reason:
                    self.db.set_status(listing, dbm.FILTERED, reason)
                    result["filtered"] += 1
                    continue
                if (
                    listing.source in self.unavailable
                    or enriched.get(listing.source, 0) >= source.max_enrich_per_run
                ):
                    result["deferred"] += 1
                    continue
                enriched[listing.source] = enriched.get(listing.source, 0) + 1
                try:
                    source.enrich(listing)
                except ListingGone:
                    self.db.set_status(listing, dbm.FILTERED, "gone")
                    result["filtered"] += 1
                    continue
                except Exception as exc:
                    self.source_failed(listing.source, exc)
                    result["deferred"] += 1
                    continue
                reason = prefilter.check(
                    listing, self.config, keywords=True, strict=source.strict_keywords
                )
                self.db.save_detail(listing)
                if reason:
                    self.db.set_status(listing, dbm.FILTERED, reason)
                    result["filtered"] += 1
                    continue

            try:
                verdict, calls = evaluate_listing(
                    self.evaluator, self.references, listing, self.profile
                )
                evaluations += calls
            except EvaluationError as exc:
                evaluations += 1
                log.error("evaluation of %s failed: %s", listing.key, exc)
                result["errors"] += 1
                consecutive_errors += 1
                if self.db.bump_attempts(listing) >= MAX_EVAL_ATTEMPTS:
                    self.db.set_status(listing, dbm.ERROR, str(exc)[:200])
                self.evaluator_failed(exc)
                if consecutive_errors >= 2:
                    log.error("stopping evaluation for this run after repeated failures")
                    break
                continue

            consecutive_errors = 0
            self.db.update_health(EVALUATOR, failures=0, last_error=None, alerted=0)
            passed = self.profile.passes(verdict)
            used = ", ".join(getattr(self.evaluator, "last_models", [])) or self.config.model
            self.db.add_evaluation(listing, used, verdict, passed)
            self.db.set_status(listing, dbm.MATCHED if passed else dbm.REJECTED)
            result["matched" if passed else "rejected"] += 1
            log.info(
                "%s %s: %s", "MATCH" if passed else "reject", listing.key, verdict.get("summary")
            )
        return result

    def evaluator_failed(self, exc: Exception) -> None:
        health = self.db.health(EVALUATOR)
        failures = health["failures"] + 1
        fields = {"failures": failures, "last_error": str(exc)[:500]}
        if failures >= FAILURES_BEFORE_ALERT and not health["alerted"]:
            if self.notifier.send_alert(
                f"{self.label}: Claude evaluation is failing",
                f"{failures} failures in a row. New listings are queued until it works.\n{exc}",
            ):
                fields["alerted"] = 1
        self.db.update_health(EVALUATOR, **fields)

    # -- notify -------------------------------------------------------------

    def notify_matches(self) -> int:
        sent = 0
        for row in self.db.with_status(dbm.MATCHED):
            if sent >= self.config.discord_max_posts:
                break  # the rest go out next run, so a busy hour does not flood the channel
            listing = row_to_listing(row)
            verdict = self.db.latest_verdict(listing.source, listing.source_id)
            if verdict is None:
                continue
            if not self.notifier.send_match(listing, verdict):
                self.discord_failed()
                break  # leave the rest as matched; they are retried next run
            self.db.add_notification(listing)
            self.db.set_status(listing, dbm.NOTIFIED)
            sent += 1
        if sent:
            self.db.update_health(DISCORD, failures=0, last_error=None, alerted=0)
        return sent

    def discord_failed(self) -> None:
        health = self.db.health(DISCORD)
        failures = health["failures"] + 1
        fields = {"failures": failures, "last_error": "a post to Discord failed; see the log"}
        if failures >= FAILURES_BEFORE_ALERT and not health["alerted"]:
            if self.notifier.send_alert(
                f"{self.label}: posting to Discord is failing",
                f"{failures} runs in a row. Listings are held until it works.",
            ):
                fields["alerted"] = 1
        self.db.update_health(DISCORD, **fields)

    # -- launch -------------------------------------------------------------

    def launch(self) -> dict:
        """Work towards the opening post: a welcome, then the best-value listings.

        Runs are silent until everything currently for sale has been evaluated.
        Then the picks are chosen once and recorded, so a run that fails partway
        through posting carries on where it stopped instead of repeating itself.
        Whatever else had matched when the picks were chosen is marked seeded and
        never posted.
        """
        record = self.db.get_state(launching.STATE_KEY) or {}
        if "picks" not in record:
            matched = self.db.with_status(dbm.MATCHED)
            waiting = sum(
                1
                for row in self.db.with_status(dbm.PENDING)
                if row["source"] in self.sources and row["source"] not in self.unavailable
            )
            progress = {"warming_up": True, "matched": len(matched), "waiting": waiting}
            wanted = self.config.discord_top_picks
            picks: list[dict] = []
            if waiting:
                return progress
            if wanted > 0:
                # Only listings a search returned this run, so the picks are still for sale.
                candidates = []
                for row in matched:
                    verdict = self.db.latest_verdict(row["source"], row["source_id"])
                    if verdict is not None and row["last_seen_at"] >= self.started:
                        candidates.append((row_to_listing(row), verdict))
                if not candidates:
                    return progress
                try:
                    picks = launching.choose(self.ranker(candidates, wanted), candidates, wanted)
                except EvaluationError as exc:
                    log.error("choosing the top picks failed: %s", exc)
                    self.evaluator_failed(exc)
                    return {**progress, "error": str(exc)[:200]}
                if not picks:
                    log.error("no usable top picks came back; trying again next run")
                    return {**progress, "error": "no usable picks"}
            record = {
                "picks": picks,
                "chosen_at": dbm.now_iso(),
                "welcome_sent": False,
                "posted": [],
                "done": False,
            }
            self.db.set_state(launching.STATE_KEY, record)

        result = {"posted": 0}
        if not record["welcome_sent"] and self.config.discord_welcome:
            if not self.notifier.send_text(self.config.discord_welcome):
                self.discord_failed()
                return {**result, "incomplete": True}
            record["welcome_sent"] = True
            self.db.set_state(launching.STATE_KEY, record)

        total = len(record["picks"])
        for position, pick in enumerate(record["picks"], start=1):
            if pick["key"] in record["posted"]:
                continue
            source, _, source_id = pick["key"].partition(":")
            row = self.db.get(source, source_id)
            verdict = self.db.latest_verdict(source, source_id)
            if row is not None and verdict is not None and row["status"] == dbm.MATCHED:
                listing = row_to_listing(row)
                if not self.notifier.send_match(
                    listing, verdict, rank=(position, total), reason=pick["reason"]
                ):
                    self.discord_failed()
                    return {**result, "incomplete": True}
                self.db.add_notification(listing)
                self.db.set_status(listing, dbm.NOTIFIED)
                result["posted"] += 1
            record["posted"].append(pick["key"])
            self.db.set_state(launching.STATE_KEY, record)

        # Seed what the picks were chosen from. Anything first seen after that (when
        # posting had to be finished by a later run) is new, and is posted as such.
        chosen_at = record.get("chosen_at") or self.started
        leftovers = [
            row for row in self.db.with_status(dbm.MATCHED) if row["first_seen_at"] <= chosen_at
        ]
        for row in leftovers:
            self.db.set_status(row_to_listing(row), dbm.SEEDED)
        record["done"] = True
        self.db.set_state(launching.STATE_KEY, record)
        self.db.update_health(DISCORD, failures=0, last_error=None, alerted=0)
        log.info("opening post sent: %d picks, %d other listings seeded", total, len(leftovers))
        return {**result, "seeded": len(leftovers), "done": True}
