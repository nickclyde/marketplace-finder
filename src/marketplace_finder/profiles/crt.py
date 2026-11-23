"""CRT televisions and monitors: the search this project was first written for.

Written in Python rather than TOML because its keyword rules were tuned by hand
against real results, and because it looks models up on crtdatabase.com.
"""

from __future__ import annotations

import re

from .. import crtdb
from ..models import Listing
from ..text import clip, flat, plain
from .base import CONDITION_LABELS, GREY, Profile

VERDICT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "is_crt_display",
        "kind",
        "has_vcr",
        "has_dvd",
        "brand",
        "model_number",
        "is_trinitron",
        "screen_size_inches",
        "weight_lbs",
        "working",
        "best_input",
        "input_evidence",
        "rgb_moddable",
        "local_pickup_plausible",
        "summary",
        "concerns",
    ],
    "properties": {
        "is_crt_display": {
            "type": "boolean",
            "description": "A complete direct-view CRT television, TV/VCR or TV/DVD combo, "
            "professional video monitor or CRT computer monitor, of any brand. False for flat "
            "panels, rear-projection sets, parts, remotes, manuals, stands and wanted ads.",
        },
        "kind": {
            "type": "string",
            "enum": ["tv", "video_monitor", "pc_monitor", "other"],
            "description": "'tv' includes combos; 'video_monitor' is a professional or broadcast "
            "monitor (PVM, BVM and the like); 'pc_monitor' is a computer monitor; 'other' when "
            "is_crt_display is false.",
        },
        "has_vcr": {
            "type": "boolean",
            "description": "The set has a VCR built in (a TV/VCR combo). False when a separate "
            "VCR is merely included or mentioned.",
        },
        "has_dvd": {
            "type": "boolean",
            "description": "The set has a DVD player built in. False when a separate player is "
            "merely included or mentioned.",
        },
        "brand": {"type": ["string", "null"], "description": "The brand on the set, e.g. Sony, JVC."},
        "model_number": {"type": ["string", "null"]},
        "is_trinitron": {
            "type": "boolean",
            "description": "A Sony set with a Trinitron tube (FD Trinitron, WEGA, PVM and BVM count).",
        },
        "screen_size_inches": {
            "type": ["number", "null"],
            "description": "Diagonal screen size; infer it from the model number when not stated.",
        },
        "weight_lbs": {
            "type": ["number", "null"],
            "description": "The set's weight in pounds, only when the listing or the reference "
            "entry states it (convert kilograms). Null otherwise; do not estimate it.",
        },
        "working": {
            "type": "string",
            "enum": ["yes", "no", "unknown"],
            "description": "'no' if described as broken, for parts, not powering on or with a "
            "serious picture fault; 'unknown' if untested or not stated.",
        },
        "best_input": {
            "type": "string",
            "enum": ["rgb", "component", "vga", "svideo", "composite", "rf_only", "unknown"],
            "description": "Best video input the set has as built. 'rgb' means 15 kHz RGB "
            "(SCART, or RGB on BNC as on a video monitor); a computer monitor's input is 'vga'.",
        },
        "input_evidence": {
            "type": "string",
            "enum": ["listing_text", "photo", "crt_database", "model_knowledge", "none"],
            "description": "'crt_database' when the answer rests on the reference entry "
            "supplied with the listing.",
        },
        "rgb_moddable": {
            "type": "string",
            "enum": ["yes", "no", "unknown"],
            "description": "Whether this model can be modified to take RGB. 'yes' only when the "
            "reference entry lists an RGB mod or the model is well documented as taking one; "
            "'no' when it is known not to, or already has RGB or VGA; otherwise 'unknown'.",
        },
        "local_pickup_plausible": {
            "type": "boolean",
            "description": "False only if the listing says shipping only, or places the item "
            "clearly outside the stated search radius.",
        },
        "summary": {
            "type": "string",
            "description": "One or two plain sentences for a post in a chat channel. The post "
            "already shows the price, place, distance and size, so do not repeat them.",
        },
        "concerns": {
            "type": "array",
            "items": {"type": "string"},
            "description": "At most four points a buyer should confirm with the seller, a few "
            "words each, most important first.",
        },
    },
}

# -- keywords ---------------------------------------------------------------

# Words that mean a CRT on their own, whatever the brand.
CRT_TERMS = re.compile(r"\b(crts?\b|trinitron|wega|[pb]vm|kv[- ]?\d{2})", re.IGNORECASE)
# "Tube" needs company: there are tube radios, tube amps and inner tubes.
TUBE = re.compile(r"\btubes?\b", re.IGNORECASE)
SONY = re.compile(r"\bsony\b", re.IGNORECASE)
TV_TERMS = re.compile(
    r"\b(tvs?|hdtv|television|televisi[oó]n|televisor|monitor)\b", re.IGNORECASE
)
# Words that only describe flat panels. "Flat screen", "HD" and "XBR" are left out
# on purpose: all three are also used for late Trinitron sets.
FLAT_PANEL = re.compile(
    r"\b(bravia|smart|4k|8k|uhd|oled|qled|lcd|led|plasma|roku)\b|\bkdl[- ]?\d", re.IGNORECASE
)
# A stated screen size no direct-view CRT came in.
BIG_SCREEN = re.compile(r"\b([4-9]\d|1\d\d)\s*(?:[\"”″]|''|-?\s?in(?:ch(?:es)?)?\b)", re.IGNORECASE)
# Title words that make an unspecified TV worth a look even on a strict source.
OLD = re.compile(r"\b(old|older|vintage|retro|antique|classic|[89]0'?s)\b", re.IGNORECASE)
# Things that go with a TV. "TV remote" is not a TV; "TV with remote" is.
ACCESSORY = re.compile(
    r"\b(stands?|mounts?|brackets?|remotes?|antennas?|manuals?|cables?)\b", re.IGNORECASE
)
COMES_WITH = re.compile(r"(\bwith|\bw/|\band|\bplus|\binclud\w*|[&+,])\s*(?:\w+\s+){0,2}$", re.IGNORECASE)


def is_accessory(title: str) -> bool:
    return any(
        not COMES_WITH.search(title[: match.start()]) for match in ACCESSORY.finditer(title)
    )


def plain_tv(text: str, title: str) -> bool:
    """A TV of some kind, with nothing in the title saying it is a flat panel or an accessory.

    Only the title is checked for those, because descriptions of old sets often
    mention the new TV that replaced them or the stand they come with.
    """
    return bool(TV_TERMS.search(text)) and not (
        FLAT_PANEL.search(title) or BIG_SCREEN.search(title) or is_accessory(title)
    )


def relevant(title: str, description: str, *, strict: bool) -> bool:
    """Whether the text could be describing a CRT set.

    `strict` is for sources whose search returns anything loosely related (every
    TV, anything by a brand), where a TV word alone says nothing: there a TV
    without a CRT word passes only if it is a Sony (a rule tuned against real
    results) or the title calls it old.
    """
    text = f"{title}\n{description}"
    if CRT_TERMS.search(text):
        return True
    sony = bool(SONY.search(text))
    if TUBE.search(text) and (sony or TV_TERMS.search(text)):
        return True
    if strict and not sony and not OLD.search(title):
        return False
    return plain_tv(text, title)


# -- criteria ---------------------------------------------------------------


def passes(verdict: dict) -> bool:
    """Apply the criteria. Unknowns pass; explicit failures do not."""
    if not verdict.get("is_crt_display"):
        return False
    if verdict.get("working") == "no":
        return False
    if verdict.get("local_pickup_plausible") is False:
        return False
    return True


# -- the post ---------------------------------------------------------------

INPUT_LABELS = {
    "rgb": "RGB",
    "component": "Component",
    "vga": "VGA",
    "svideo": "S-Video",
    "composite": "Composite",
    "rf_only": "RF only",
    "unknown": "Not known",
}
EVIDENCE_LABELS = {
    "listing_text": "per the listing",
    "photo": "seen in photos",
    "crt_database": "per CRT Database",
    "model_knowledge": "per the model number",
}

GOLD, TEAL = "#E8B923", "#2EC4B6"
# Discord's limit for an embed field.
MAX_FIELD = 1024


def badges(verdict: dict) -> list[str]:
    """The flair a listing has earned, decided here from what the evaluator described."""
    found = []
    pc_monitor = verdict.get("kind") == "pc_monitor"
    if verdict.get("is_trinitron"):
        found.append("⭐ Trinitron")
    if verdict.get("best_input") == "rgb" and not pc_monitor:
        found.append("🌈 RGB")
    elif verdict.get("rgb_moddable") == "yes":
        found.append("🌈 RGB moddable")
    if pc_monitor:
        found.append("🖥️ PC monitor")
    if verdict.get("has_vcr"):
        found.append("📼 VCR")
    if verdict.get("has_dvd"):
        found.append("📀 DVD")
    return found


def reference_field(reference: dict) -> str:
    """What crtdatabase.com says about the model: a link, then its inputs and mods."""
    specs = reference.get("specs", {})
    name = plain(reference.get("name", "entry"))
    lines = [f"[{name}]({reference['url']})" if reference.get("url") else name]
    if reference.get("matched") == "closest":
        lines[0] += " (closest entry; check it is the same model)"
    if specs.get("Input Signals"):
        lines.append(f"Inputs: {plain(specs['Input Signals'])}")
    if specs.get("Mods"):
        lines.append(f"Mods: {plain(specs['Mods'])}")
    return clip("\n".join(lines), MAX_FIELD)


def weight_text(verdict: dict) -> str | None:
    """How heavy the set is, when the reference entry or the listing says. Never a guess."""
    reference = verdict.get("reference") or {}
    stated = (reference.get("specs") or {}).get("Weight")
    if stated:
        source = "CRT Database, closest entry" if reference.get("matched") == "closest" else "CRT Database"
        return f"{plain(clip(str(stated), 60))}, per {source}"
    pounds = verdict.get("weight_lbs")
    if isinstance(pounds, (int, float)) and not isinstance(pounds, bool) and pounds > 0:
        return f"{pounds:g} lbs, per the listing"
    return None


def candidate_line(listing: Listing, verdict: dict) -> str:
    size = verdict.get("screen_size_inches")
    distance = "distance unknown" if listing.distance_mi is None else f"{listing.distance_mi:.0f} mi"
    name = flat(" ".join(str(part) for part in (verdict.get("brand"), verdict.get("model_number")) if part))
    parts = [
        listing.key,
        name or f"model unknown ({flat(listing.title[:60])})",
        flat(verdict.get("kind", "tv")),
        f'{size:g}"' if size else "size unknown",
        listing.price_text,
        distance,
        f"best input {flat(verdict.get('best_input', 'unknown'))}",
        f"working {flat(verdict.get('working', 'unknown'))}",
        "Trinitron" if verdict.get("is_trinitron") else "",
        "RGB mod known" if verdict.get("rgb_moddable") == "yes" else "",
        "built-in VCR" if verdict.get("has_vcr") else "",
        "built-in DVD" if verdict.get("has_dvd") else "",
        flat(verdict.get("summary")),
    ]
    return " | ".join(part for part in parts if part)


class CrtProfile(Profile):
    name = "crt"
    label = "CRT finder"
    queries = ["crt tv", "tube tv", "trinitron"]
    source_queries = {
        "craigslist": ["crt tv", "tube tv", "crt monitor", "trinitron", "sony pvm"],
        "ebay": ["crt tv", "crt monitor", "trinitron", "sony pvm"],
    }
    schema = VERDICT_SCHEMA
    photo_hint = (
        "the rear panel, front jacks and model sticker often show the model and inputs "
        "when the text does not."
    )
    roundup = (
        "the best-value CRTs for sale nearby right now, for people who play old consoles "
        "and computers on them"
    )
    ranking_criteria = [
        "picture and inputs for the price: RGB and component beat S-Video, which beats "
        "composite, which beats RF only; Trinitrons and professional video monitors are "
        "prized; a known RGB mod adds value",
        "a size that suits games, and what similar sets usually sell for",
        "condition: confirmed working beats untested",
    ]

    def brief(self, config) -> str:
        return f"""You are screening a used-goods listing for a group of retro gaming enthusiasts. They want CRT displays for old consoles and computers and for watching VHS and DVD.

They are interested in:
- any complete CRT television, TV/VCR or TV/DVD combo, professional video monitor or CRT computer monitor, of any brand and any size
- in working condition, or untested
- with the best video input they can get: RGB or component first, then S-Video, then composite, then RF only
- available for local pickup within {config.radius_miles:g} miles of postal code {config.postal_code}

Describe this listing by filling in the structured fields. Use what you know about model numbers (for example, the digits after "KV-" in a Sony model give the screen size, and model families differ in their inputs) and say in input_evidence where your answer about inputs came from. Do not guess: use "unknown" or null when the listing, the photos and the model number do not settle something, and put the open question in concerns."""

    def passes(self, verdict: dict) -> bool:
        return passes(verdict)

    def relevant(self, title: str, description: str, *, strict: bool) -> bool:
        return relevant(title, description, strict=strict)

    # -- crtdatabase.com ----------------------------------------------------

    def references(self, db, config):
        return crtdb.CrtDatabase(db) if config.crtdatabase else None

    def reference_query(self, verdict: dict) -> tuple[str, str | None] | None:
        if verdict.get("is_crt_display") and verdict.get("model_number"):
            return verdict["model_number"], verdict.get("brand")
        return None

    def reference_intro(self, reference: dict) -> str:
        if reference.get("matched") == "closest":
            how = (
                f"The closest entry on crtdatabase.com to the model named in the listing is "
                f"{reference['name']}; check it is the same model before relying on it."
            )
        else:
            how = f"crtdatabase.com has an entry for {reference['name']}, the model named in the listing."
        return (
            f"{how} It describes the model as built, not this particular set: rely on it for "
            "screen size, inputs and how the model can be modified, unless the listing or photos "
            "show this set differs. Readers are shown the entry next to your answer, so do not "
            "repeat it in the summary or concerns; keep concerns to what needs confirming with "
            "the seller about this set."
        )

    def reference_text(self, reference: dict) -> str:
        return crtdb.prompt_text(reference)

    # -- the post -----------------------------------------------------------

    def badges(self, verdict: dict) -> list[str]:
        return badges(verdict)

    def colour(self, verdict: dict, flair: list[str]) -> str:
        if verdict.get("is_trinitron"):
            return GOLD
        if any(badge.startswith("🌈") for badge in flair):
            return TEAL
        return GREY

    def title_name(self, verdict: dict) -> str | None:
        model, brand = verdict.get("model_number"), verdict.get("brand")
        if not model:
            return None
        return model if not brand or brand.lower() in model.lower() else f"{brand} {model}"

    def title_extras(self, verdict: dict) -> list[str]:
        size = verdict.get("screen_size_inches")
        return [f'{size:g}"'] if size else []

    def fields(self, verdict: dict) -> list[tuple[str, str, bool]]:
        best_input = verdict.get("best_input", "unknown")
        input_text = INPUT_LABELS.get(best_input, "Not known")
        evidence = EVIDENCE_LABELS.get(verdict.get("input_evidence", ""))
        if evidence and best_input != "unknown":
            input_text += f" ({evidence})"
        fields = [
            ("Best input", input_text, True),
            ("Condition", CONDITION_LABELS.get(verdict.get("working", "unknown"), "Not stated"), True),
        ]
        weight = weight_text(verdict)
        if weight:
            fields.append(("Weight", weight, True))
        reference = verdict.get("reference")
        if reference:
            fields.append(("CRT Database", reference_field(reference), False))
        return fields

    # -- the opening post ---------------------------------------------------

    def candidate_line(self, listing: Listing, verdict: dict) -> str:
        return candidate_line(listing, verdict)

    def welcome(self, config) -> str:
        return (
            "**CRT hunting is live.** This channel now gets a post whenever a CRT TV or monitor "
            f"turns up for sale within about {config.radius_miles:g} miles of {config.area}: any "
            f"brand, any size, under ${config.max_price}. ⭐ marks Sony Trinitrons and 🌈 marks "
            "sets with RGB input or a known RGB mod. Details come from the listing, so confirm "
            "with the seller before you drive out. To start things off, here are the best-value "
            "sets up right now."
        )

    # -- testing ------------------------------------------------------------

    def sample(self, config) -> tuple[Listing, dict]:
        listing = Listing(
            source="test",
            source_id="0",
            url="https://crtdatabase.com/crts/sony/sony-kv-27fs120",
            title="Sony Trinitron KV-27FS120",
            price_cents=15000,
            location_text=config.area,
            distance_mi=16.0,
        )
        verdict = {
            "kind": "tv",
            "brand": "Sony",
            "model_number": "KV-27FS120",
            "is_trinitron": True,
            "screen_size_inches": 27,
            "working": "yes",
            "best_input": "component",
            "input_evidence": "model_knowledge",
            "rgb_moddable": "no",
            "summary": "This is a test post from marketplace-finder, not a real listing.",
            "concerns": ["nothing; it is only a test"],
        }
        return listing, verdict
