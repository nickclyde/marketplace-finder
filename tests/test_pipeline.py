from datetime import datetime, timezone

import pytest

from marketplace_finder import db as dbm
from marketplace_finder import launch
from marketplace_finder.config import SourceConfig
from marketplace_finder.db import Database
from marketplace_finder.evaluate import EvaluationError
from marketplace_finder.pipeline import Pipeline, in_quiet_hours
from marketplace_finder.sources.base import (
    ListingGone,
    Source,
    SourceBlocked,
    SourceNeedsAttention,
    SourceSkipped,
)

GOOD = {
    "is_crt_display": True, "kind": "tv", "brand": "Sony", "model_number": "KV-27FS120",
    "is_trinitron": True, "screen_size_inches": 27, "working": "yes",
    "best_input": "component", "input_evidence": "model_knowledge", "rgb_moddable": "no",
    "local_pickup_plausible": True,
    "summary": "27 inch flat-tube Trinitron with component input.", "concerns": [],
}
BAD = {**GOOD, "is_crt_display": False, "kind": "other", "summary": "A remote control."}


class FakeSource(Source):
    name = "craigslist"
    delay = (0, 0)

    def __init__(self, config, results=(), details=None, error=None):
        super().__init__(config, SourceConfig(enabled=True, queries=["trinitron"]))
        self.results = list(results)
        self.details = details or {}
        self.error = error
        self.enriched = []

    def search(self, query):
        if self.error:
            raise self.error
        return list(self.results)

    def enrich(self, listing):
        self.enriched.append(listing.source_id)
        detail = self.details.get(listing.source_id, "Sony Trinitron, works.")
        if isinstance(detail, Exception):
            raise detail
        listing.description = detail


class FakeEvaluator:
    def __init__(self, verdicts=None, default=GOOD):
        self.verdicts = verdicts or {}
        self.default = default
        self.calls = []
        self.references = []

    def __call__(self, listing, reference=None):
        self.calls.append(listing.source_id)
        self.references.append(reference)
        verdict = self.verdicts.get(listing.source_id, self.default)
        if isinstance(verdict, Exception):
            raise verdict
        return verdict


class FakeNotifier:
    def __init__(self, ok=True):
        self.ok = ok
        self.matches = []
        self.alerts = []

    def send_match(self, listing, verdict, **kwargs):
        if self.ok:
            self.matches.append(listing.source_id)
        return self.ok

    def send_alert(self, title, message):
        self.alerts.append(title)
        return True


@pytest.fixture
def database(config):
    database = Database(config.db_path)
    # The opening post has already gone out (test_launch.py covers the time before it).
    database.set_state(launch.STATE_KEY, {"done": True, "picks": [], "posted": []})
    yield database
    database.close()


def run(config, database, source, evaluator=None, notifier=None):
    evaluator = evaluator or FakeEvaluator()
    notifier = notifier or FakeNotifier()
    summary = Pipeline(config, database, {source.name: source}, evaluator, notifier).run()
    return summary, evaluator, notifier


def test_new_listing_is_evaluated_and_notified_once(config, database, make_listing):
    source = FakeSource(config, [make_listing(source_id="1")])

    summary, evaluator, notifier = run(config, database, source)
    assert summary["sources"]["craigslist"] == {"found": 1, "new": 1, "queued": 1}
    assert evaluator.calls == ["1"] and notifier.matches == ["1"]
    assert database.get("craigslist", "1")["status"] == dbm.NOTIFIED

    summary, evaluator, notifier = run(config, database, source)
    assert summary["sources"]["craigslist"] == {"found": 1, "new": 0, "queued": 0}
    assert evaluator.calls == [] and notifier.matches == []


def test_only_unseen_listings_reach_claude(config, database, make_listing):
    run(config, database, FakeSource(config, [make_listing(source_id="1")]))
    source = FakeSource(config, [make_listing(source_id="1"), make_listing(source_id="2")])
    _, evaluator, notifier = run(config, database, source)
    assert evaluator.calls == ["2"] and notifier.matches == ["2"]


def test_prefiltered_listings_never_reach_claude(config, database, make_listing):
    source = FakeSource(config, [
        make_listing(source_id="pricey", price_cents=80000),
        make_listing(source_id="far", lat=34.05, lon=-118.24),
        make_listing(source_id="junk", title="Tech Craft TV Stand"),
    ], details={"junk": "Glass TV stand, fits 36 inch."})
    _, evaluator, _ = run(config, database, source)
    assert evaluator.calls == []
    assert database.get("craigslist", "pricey")["filter_reason"] == "price"
    assert database.get("craigslist", "far")["filter_reason"] == "distance"
    # The keyword check waits for the description, so this one cost a detail fetch but no Claude call.
    assert source.enriched == ["junk"]
    assert database.get("craigslist", "junk")["filter_reason"] == "keyword"


def test_strict_source_filters_brand_only_titles_before_any_fetch(config, database, make_listing):
    class Strict(FakeSource):
        title_keywords = True
        strict_keywords = True

    source = Strict(config, [
        make_listing(source_id="speaker", title="Sony SRS-XV800 Wireless Party Speaker"),
        make_listing(source_id="bravia", title="Sony Bravia 55 TV"),
        make_listing(source_id="set", title="Sony TV"),
    ])
    _, evaluator, _ = run(config, database, source)
    assert source.enriched == ["set"] and evaluator.calls == ["set"]
    assert database.get("craigslist", "speaker")["filter_reason"] == "keyword"
    assert database.get("craigslist", "bravia")["filter_reason"] == "keyword"


def test_queued_listings_are_rechecked_before_fetching(config, database, make_listing):
    # Queued under looser rules, e.g. before the source became strict.
    database.insert(make_listing(source_id="speaker", title="Sony party speaker"), dbm.PENDING, None)
    database.insert(make_listing(source_id="set", title="Sony TV"), dbm.PENDING, None)

    class Strict(FakeSource):
        title_keywords = True
        strict_keywords = True

    source = Strict(config, [])
    summary, evaluator, _ = run(config, database, source)
    assert source.enriched == ["set"] and evaluator.calls == ["set"]
    assert database.get("craigslist", "speaker")["status"] == dbm.FILTERED
    assert summary["evaluated"]["filtered"] == 1


class FakeReferences:
    """Knows one model, the way the crtdatabase lookup would."""

    ENTRY = {"name": "Sony KV-27FS120", "url": "https://crtdatabase.com/x", "matched": "exact"}

    def __init__(self):
        self.asked = []

    def find(self, *texts, brand=None):
        self.asked.append((*texts, brand))
        return dict(self.ENTRY) if any("KV-27FS120" in (text or "") for text in texts) else None


def run_with_references(config, database, source, evaluator):
    references = FakeReferences()
    pipeline = Pipeline(
        config, database, {source.name: source}, evaluator, FakeNotifier(), references
    )
    return pipeline.run(), references


def test_model_named_in_listing_is_looked_up_before_the_one_evaluation(
    config, database, make_listing
):
    source = FakeSource(config, [make_listing(source_id="1", title="Sony KV-27FS120")])
    evaluator = FakeEvaluator()
    run_with_references(config, database, source, evaluator)
    assert evaluator.calls == ["1"]
    assert evaluator.references[0]["name"] == "Sony KV-27FS120"
    # Stored with the verdict, which is what the notification is built from.
    assert database.latest_verdict("craigslist", "1")["reference"]["name"] == "Sony KV-27FS120"


def test_model_found_by_claude_gets_a_second_pass_with_the_entry(config, database, make_listing):
    # Nothing in the text names the model; the first pass reads it off a photo.
    source = FakeSource(
        config, [make_listing(source_id="1", title="Sony Trinitron TV")], details={"1": "Works."}
    )
    evaluator = FakeEvaluator()  # GOOD says the model is KV-27FS120
    _, references = run_with_references(config, database, source, evaluator)
    assert evaluator.calls == ["1", "1"]
    assert evaluator.references[0] is None
    assert evaluator.references[1]["name"] == "Sony KV-27FS120"
    # The second lookup goes by the model and brand the evaluator read off the set.
    assert references.asked[-1] == ("KV-27FS120", "Sony")
    assert "reference" in database.latest_verdict("craigslist", "1")


def test_no_second_pass_without_an_entry_or_for_things_that_are_not_crts(config, database, make_listing):
    source = FakeSource(config, [
        make_listing(source_id="unknown", title="Sony Trinitron TV"),
        make_listing(source_id="other", title="Sony Trinitron remote"),
    ], details={"unknown": "Works.", "other": "Works."})
    evaluator = FakeEvaluator({
        "unknown": {**GOOD, "model_number": "KV-1370R"},
        "other": {**BAD, "model_number": "KV-27FS120"},
    })
    run_with_references(config, database, source, evaluator)
    assert sorted(evaluator.calls) == ["other", "unknown"]
    assert "reference" not in database.latest_verdict("craigslist", "unknown")


def test_failed_second_pass_keeps_the_first_verdict(config, database, make_listing):
    class FailsWithReference(FakeEvaluator):
        def __call__(self, listing, reference=None):
            verdict = super().__call__(listing, reference)
            if reference:
                raise EvaluationError("timed out")
            return verdict

    source = FakeSource(
        config, [make_listing(source_id="1", title="Sony Trinitron TV")], details={"1": "Works."}
    )
    run_with_references(config, database, source, FailsWithReference())
    row = database.get("craigslist", "1")
    assert row["status"] == dbm.NOTIFIED and row["attempts"] == 0
    assert database.latest_verdict("craigslist", "1")["reference"]["name"] == "Sony KV-27FS120"


def test_second_passes_count_toward_the_per_run_cap(config, database, make_listing):
    config.max_evals_per_run = 2
    source = FakeSource(config, [
        make_listing(source_id="1", title="Sony Trinitron TV"),
        make_listing(source_id="2", title="Sony Trinitron TV"),
    ], details={"1": "Works.", "2": "Works."})
    evaluator = FakeEvaluator()
    summary, _ = run_with_references(config, database, source, evaluator)
    assert evaluator.calls == ["1", "1"]
    assert summary["evaluated"]["deferred"] == 1


def test_rejected_listing_is_not_notified(config, database, make_listing):
    source = FakeSource(config, [make_listing(source_id="1")])
    _, _, notifier = run(config, database, source, FakeEvaluator(default=BAD))
    assert notifier.matches == []
    assert database.get("craigslist", "1")["status"] == dbm.REJECTED


def test_price_drop_requeues_a_listing(config, database, make_listing):
    run(config, database, FakeSource(config, [make_listing(source_id="1", price_cents=65000)]))
    assert database.get("craigslist", "1")["status"] == dbm.FILTERED
    source = FakeSource(config, [make_listing(source_id="1", price_cents=45000)])
    summary, evaluator, notifier = run(config, database, source)
    assert summary["sources"]["craigslist"]["queued"] == 1
    assert evaluator.calls == ["1"] and notifier.matches == ["1"]


def test_evaluations_are_capped_per_run_and_resume(config, database, make_listing):
    config.max_evals_per_run = 2
    source = FakeSource(config, [make_listing(source_id=str(i)) for i in range(5)])
    _, evaluator, _ = run(config, database, source)
    assert len(evaluator.calls) == 2
    _, evaluator, _ = run(config, database, source)
    assert len(evaluator.calls) == 2
    _, evaluator, _ = run(config, database, source)
    assert len(evaluator.calls) == 1
    assert database.counts() == {dbm.NOTIFIED: 5}


def test_gone_listing_is_dropped(config, database, make_listing):
    source = FakeSource(config, [make_listing(source_id="1")], details={"1": ListingGone()})
    _, evaluator, _ = run(config, database, source)
    assert evaluator.calls == []
    assert database.get("craigslist", "1")["filter_reason"] == "gone"


def test_failed_notification_is_retried_next_run(config, database, make_listing):
    source = FakeSource(config, [make_listing(source_id="1")])
    run(config, database, source, notifier=FakeNotifier(ok=False))
    assert database.get("craigslist", "1")["status"] == dbm.MATCHED
    _, evaluator, notifier = run(config, database, source)
    assert evaluator.calls == []  # not evaluated a second time
    assert notifier.matches == ["1"]


def test_posts_are_capped_per_run_and_the_rest_follow(config, database, make_listing):
    config.discord_max_posts = 2
    source = FakeSource(config, [make_listing(source_id=str(i)) for i in range(5)])
    summary, _, notifier = run(config, database, source)
    assert notifier.matches == ["0", "1"] and summary["notified"] == 2
    assert database.counts() == {dbm.NOTIFIED: 2, dbm.MATCHED: 3}
    _, evaluator, notifier = run(config, database, source)
    assert evaluator.calls == [] and notifier.matches == ["2", "3"]
    _, _, notifier = run(config, database, source)
    assert notifier.matches == ["4"]


def test_discord_failure_alerts_once_after_three_runs_and_clears(config, database, make_listing):
    source = FakeSource(config, [make_listing(source_id="1")])
    alerts = []
    for _ in range(4):
        _, _, notifier = run(config, database, source, notifier=FakeNotifier(ok=False))
        alerts += notifier.alerts
    assert alerts == ["CRT finder: posting to Discord is failing"]
    _, _, notifier = run(config, database, source)
    assert notifier.matches == ["1"]
    assert database.health("discord")["failures"] == 0


def test_evaluator_failure_keeps_listing_queued_then_gives_up(config, database, make_listing):
    source = FakeSource(config, [make_listing(source_id="1")])
    failing = lambda: FakeEvaluator(default=EvaluationError("not logged in"))
    for _ in range(2):
        run(config, database, source, failing())
        assert database.get("craigslist", "1")["status"] == dbm.PENDING
    _, _, notifier = run(config, database, source, failing())
    assert database.get("craigslist", "1")["status"] == dbm.ERROR
    assert notifier.alerts == ["CRT finder: Claude evaluation is failing"]


def test_evaluation_stops_after_two_failures_in_a_row(config, database, make_listing):
    source = FakeSource(config, [make_listing(source_id=str(i)) for i in range(6)])
    _, evaluator, _ = run(config, database, source, FakeEvaluator(default=EvaluationError("x")))
    assert len(evaluator.calls) == 2


def test_source_failure_alerts_once_and_pauses(config, database):
    alerts = []
    for _ in range(4):
        source = FakeSource(config, error=SourceBlocked("HTTP 403"))
        summary, _, notifier = run(config, database, source)
        alerts += notifier.alerts
    assert alerts == ["CRT finder: craigslist is failing"]
    assert "paused until" in summary["sources"]["craigslist"]["skipped"]
    assert database.health("craigslist")["failures"] == 3  # the fourth run did not hit the site


def test_source_recovers_after_a_failure(config, database, make_listing):
    run(config, database, FakeSource(config, error=SourceBlocked("HTTP 403")))
    assert database.health("craigslist")["failures"] == 1
    run(config, database, FakeSource(config, [make_listing()]))
    assert database.health("craigslist")["failures"] == 0


def test_needs_attention_alerts_once_and_stops_trying(config, database):
    source = FakeSource(config, error=SourceNeedsAttention("log in again"))
    _, _, notifier = run(config, database, source)
    assert notifier.alerts == ["CRT finder: craigslist needs you"]
    summary, _, notifier = run(config, database, source)
    assert notifier.alerts == []
    assert "needs attention" in summary["sources"]["craigslist"]["skipped"]


def test_unconfigured_source_is_not_a_failure(config, database):
    summary, _, notifier = run(config, database, FakeSource(config, error=SourceSkipped("no keys")))
    assert summary["sources"]["craigslist"] == {"skipped": "no keys"}
    assert notifier.alerts == [] and database.health("craigslist")["failures"] == 0


def test_listings_waiting_on_a_down_source_do_not_use_up_the_cap(config, database, make_listing):
    config.max_evals_per_run = 0
    run(config, database, FakeSource(config, [make_listing(source_id=str(i)) for i in range(3)]))
    config.max_evals_per_run = 2

    class Other(FakeSource):
        name = "offerup"

    down = FakeSource(config, error=SourceBlocked("403"))
    up = Other(config, [make_listing(source="offerup", source_id=f"o{i}") for i in range(3)])
    evaluator = FakeEvaluator()
    summary = Pipeline(
        config, database, {"craigslist": down, "offerup": up}, evaluator, FakeNotifier()
    ).run()
    assert evaluator.calls == ["o0", "o1"]
    assert summary["evaluated"]["deferred"] == 4  # three craigslist, one offerup over the cap


def test_pending_listings_wait_when_their_source_is_down(config, database, make_listing):
    config.max_evals_per_run = 0
    run(config, database, FakeSource(config, [make_listing(source_id="1")]))
    config.max_evals_per_run = 30
    summary, evaluator, _ = run(config, database, FakeSource(config, error=SourceBlocked("403")))
    assert evaluator.calls == [] and summary["evaluated"]["deferred"] == 1
    assert database.get("craigslist", "1")["status"] == dbm.PENDING


def test_detail_fetches_respect_a_per_run_limit(config, database, make_listing):
    source = FakeSource(config, [make_listing(source_id=str(i)) for i in range(4)])
    source.max_enrich_per_run = 1
    summary, evaluator, _ = run(config, database, source)
    assert len(evaluator.calls) == 1 and summary["evaluated"]["deferred"] == 3


@pytest.mark.parametrize("hour_utc,quiet", [
    (8, True),    # 00:00 PST
    (12, True),   # 04:00 PST
    (13, False),  # 05:00 PST
    (7, False),   # 23:00 PST
    (20, False),  # noon PST
])
def test_quiet_hours_are_pacific(config, hour_utc, quiet):
    now = datetime(2026, 11, 12, hour_utc, 30, tzinfo=timezone.utc)
    assert in_quiet_hours(config, now) is quiet


def test_quiet_hours_follow_daylight_saving(config):
    # 12:30 UTC in July is 05:30 PDT: not quiet.
    assert in_quiet_hours(config, datetime(2026, 7, 1, 12, 30, tzinfo=timezone.utc)) is False
