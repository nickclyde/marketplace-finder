"""A profile written as a TOML file, for searches that need no code of their own.

The file says what is wanted, which keywords keep unrelated listings away from
Claude, which extra fields Claude should fill in, and what each answer means:
`reject` lists the answers that fail a listing, `badges` the ones that earn
flair. Claude only ever fills in the fields; the rules are applied here.

See `keyboards.toml` next to this file for an annotated example.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import ConfigError
from ..models import Listing
from ..text import clip, flat, plain
from .base import CONDITION_LABELS, GREY, Profile

CORE_FIELDS = (
    "is_wanted_item",
    "brand",
    "model",
    "working",
    "local_pickup_plausible",
    "summary",
    "concerns",
)
FIELD_TYPES = ("enum", "boolean", "number", "string")
FIELD_NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
COLOUR = re.compile(r"^#[0-9A-Fa-f]{6}$")
TOP_LEVEL_KEYS = {
    "label", "item", "audience", "wanted", "not_wanted", "notes", "photo_hint", "ranking",
    "queries", "source_queries", "allow_not_working", "welcome", "colour", "keywords", "fields",
}
FIELD_KEYS = {"name", "type", "description", "values", "label", "reject", "badges", "display"}
KEYWORD_KEYS = {"include", "loose", "exclude_title"}
# Discord shows at most 25 fields in an embed; Condition takes one.
MAX_LABELLED_FIELDS = 20
MAX_LABEL, MAX_BADGE, MAX_VALUE = 60, 40, 100
ACCENT = "#2EC4B6"


def key(value) -> str:
    """How a field's answer is written in `reject`, `badges` and `display`."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def phrases(words: list[str]) -> re.Pattern | None:
    """Matches any of the words or phrases, as whole words, whatever the case."""
    if not words:
        return None
    alternatives = "|".join(re.escape(word.strip()) for word in words if word.strip())
    return re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)", re.IGNORECASE)


@dataclass
class Field:
    name: str
    type: str
    description: str = ""
    values: list[str] = field(default_factory=list)
    label: str | None = None
    reject: frozenset[str] = frozenset()
    badges: dict[str, str] = field(default_factory=dict)
    display: dict[str, str] = field(default_factory=dict)

    def schema(self) -> dict:
        if self.type == "enum":
            schema: dict = {"type": "string", "enum": list(self.values)}
        elif self.type == "boolean":
            schema = {"type": "boolean"}
        else:
            schema = {"type": [self.type, "null"]}
        if self.description:
            schema["description"] = self.description
        return schema

    def shown(self, value) -> str | None:
        """The answer as it appears in a post, or None when there is nothing to say."""
        if value is None or value == "" or value == "unknown":
            return None
        if key(value) in self.display:
            return self.display[key(value)]
        if isinstance(value, bool):
            return "Yes" if value else "No"
        if isinstance(value, (int, float)):
            return f"{value:g}"
        text = str(value)
        if self.type == "enum":
            text = text.replace("_", " ").capitalize()
        # Free text from Claude traces back to the listing, so it is made inert.
        return plain(clip(text, MAX_VALUE)) or None


def _strings(raw: dict, name: str, where: str) -> list[str]:
    value = raw.get(name, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{where}: `{name}` must be a list of strings")
    return value


def _text(raw: dict, name: str, where: str, default: str = "") -> str:
    value = raw.get(name, default)
    if not isinstance(value, str):
        raise ConfigError(f"{where}: `{name}` must be a string")
    return " ".join(value.split())


def _unknown_keys(raw: dict, known: set[str], where: str) -> None:
    extra = sorted(set(raw) - known)
    if extra:
        raise ConfigError(f"{where}: unknown key {', '.join(extra)}; expected one of {', '.join(sorted(known))}")


def parse_field(raw: dict, where: str) -> Field:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: each [[fields]] entry must be a table")
    name = raw.get("name")
    if not isinstance(name, str) or not FIELD_NAME.match(name):
        raise ConfigError(f"{where}: a field's `name` must be lower-case letters, digits and underscores")
    where = f"{where}, field {name}"
    _unknown_keys(raw, FIELD_KEYS, where)
    if name in CORE_FIELDS:
        raise ConfigError(f"{where}: that name is taken by a built-in field ({', '.join(CORE_FIELDS)})")
    kind = raw.get("type")
    if kind not in FIELD_TYPES:
        raise ConfigError(f"{where}: `type` must be one of {', '.join(FIELD_TYPES)}")

    values = _strings(raw, "values", where)
    if kind == "enum" and not values:
        raise ConfigError(f"{where}: an enum field needs `values`")
    if kind != "enum" and values:
        raise ConfigError(f"{where}: only an enum field takes `values`")
    allowed = set(values) if kind == "enum" else {"true", "false"} if kind == "boolean" else set()

    reject = raw.get("reject", [])
    if not isinstance(reject, list):
        raise ConfigError(f"{where}: `reject` must be a list")
    tables = {}
    for option in ("badges", "display"):
        table = raw.get(option, {})
        if not isinstance(table, dict) or not all(isinstance(text, str) for text in table.values()):
            raise ConfigError(f"{where}: `{option}` must be a table of answer = \"text\"")
        tables[option] = {key(answer): " ".join(text.split()) for answer, text in table.items()}
    for option, answers in (("reject", [key(v) for v in reject]), *((o, list(t)) for o, t in tables.items())):
        if answers and not allowed:
            raise ConfigError(f"{where}: `{option}` only works on enum and boolean fields")
        stray = sorted(set(answers) - allowed)
        if stray:
            raise ConfigError(f"{where}: `{option}` names {', '.join(stray)}, which is not one of its answers")
    if any(len(text) > MAX_BADGE for text in tables["badges"].values()):
        raise ConfigError(f"{where}: keep each badge to {MAX_BADGE} characters")
    shown = [*tables["badges"].values(), *tables["display"].values(), str(raw.get("label") or "")]
    if any("|" in text for text in shown):
        raise ConfigError(f"{where}: `label`, `badges` and `display` text cannot contain \"|\"")

    label = raw.get("label")
    if label is not None and (not isinstance(label, str) or not label.strip() or len(label) > MAX_LABEL):
        raise ConfigError(f"{where}: `label` must be a short string")
    return Field(
        name=name,
        type=kind,
        description=_text(raw, "description", where),
        values=values,
        label=label.strip() if label else None,
        reject=frozenset(key(v) for v in reject),
        badges=tables["badges"],
        display=tables["display"],
    )


class TomlProfile(Profile):
    def __init__(self, raw: dict, name: str, where: str):
        _unknown_keys(raw, TOP_LEVEL_KEYS, where)
        self.name = name
        self.item = _text(raw, "item", where)
        if not self.item:
            raise ConfigError(f"{where}: `item` is required; say in a few words what is being looked for")
        self.label = _text(raw, "label", where, f"{name.replace('_', ' ').replace('-', ' ').capitalize()} finder")
        self.audience = _text(raw, "audience", where, f"someone looking for {self.item}")
        self.wanted = _strings(raw, "wanted", where) or [self.item]
        self.not_wanted = _text(raw, "not_wanted", where)
        self.notes = _text(raw, "notes", where)
        self.photo_hint = _text(raw, "photo_hint", where, Profile.photo_hint)
        self.ranking_criteria = _strings(raw, "ranking", where) or [
            "what similar items usually sell for",
            "condition: confirmed working beats untested",
        ]
        self.roundup = (
            f"the best-value listings for sale nearby right now, for {self.audience}. "
            f"What is wanted: {self.item}"
        )
        self.queries = _strings(raw, "queries", where)
        if not self.queries:
            raise ConfigError(f"{where}: `queries` is required; list the searches to run")
        source_queries = raw.get("source_queries", {})
        if not isinstance(source_queries, dict):
            raise ConfigError(f"{where}: `source_queries` must be a table of source = [queries]")
        self.source_queries = {
            source: _strings(source_queries, source, f"{where}, source_queries") for source in source_queries
        }
        self.allow_not_working = raw.get("allow_not_working", False)
        if not isinstance(self.allow_not_working, bool):
            raise ConfigError(f"{where}: `allow_not_working` must be true or false")
        self.welcome_text = _text(raw, "welcome", where)
        self.accent = raw.get("colour", ACCENT)
        if not isinstance(self.accent, str) or not COLOUR.match(self.accent):
            raise ConfigError(f"{where}: `colour` must look like \"#2EC4B6\"")

        keywords = raw.get("keywords", {})
        if not isinstance(keywords, dict):
            raise ConfigError(f"{where}: [keywords] must be a table")
        _unknown_keys(keywords, KEYWORD_KEYS, f"{where}, [keywords]")
        self.include = phrases(_strings(keywords, "include", f"{where}, [keywords]"))
        self.loose = phrases(_strings(keywords, "loose", f"{where}, [keywords]"))
        self.exclude_title = phrases(_strings(keywords, "exclude_title", f"{where}, [keywords]"))

        raw_fields = raw.get("fields", [])
        if not isinstance(raw_fields, list):
            raise ConfigError(f"{where}: fields must be written as [[fields]] tables")
        self.custom = [parse_field(entry, where) for entry in raw_fields]
        names = [entry.name for entry in self.custom]
        repeated = sorted({n for n in names if names.count(n) > 1})
        if repeated:
            raise ConfigError(f"{where}: field {', '.join(repeated)} is defined more than once")
        if sum(1 for entry in self.custom if entry.label) > MAX_LABELLED_FIELDS:
            raise ConfigError(f"{where}: at most {MAX_LABELLED_FIELDS} fields can have a `label`")
        self.schema = self._schema()

    @classmethod
    def from_text(cls, text: str, name: str, where: str) -> TomlProfile:
        try:
            raw = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{where}: not valid TOML: {exc}") from exc
        return cls(raw, name, where)

    @classmethod
    def from_file(cls, path: Path) -> TomlProfile:
        if not path.is_file():
            raise ConfigError(f"profile file {path} was not found")
        return cls.from_text(path.read_text(), path.stem, str(path))

    def _schema(self) -> dict:
        wanted = f"True when the listing is selling this: {self.item}."
        unwanted = f" False for {self.not_wanted}." if self.not_wanted else ""
        properties = {
            "is_wanted_item": {
                "type": "boolean",
                "description": wanted + unwanted + " False for wanted ads and for anything else.",
            },
            "brand": {"type": ["string", "null"], "description": "The maker's name, when it can be told."},
            "model": {"type": ["string", "null"], "description": "The model name or number, when it can be told."},
            "working": {
                "type": "string",
                "enum": ["yes", "no", "unknown"],
                "description": "'no' if described as broken, for parts or not working; "
                "'unknown' if untested or not stated.",
            },
            **{entry.name: entry.schema() for entry in self.custom},
            "local_pickup_plausible": {
                "type": "boolean",
                "description": "False only if the listing says shipping only, or places the item "
                "clearly outside the stated search radius.",
            },
            "summary": {
                "type": "string",
                "description": "One or two plain sentences for a post in a chat channel. The post "
                "already shows the price, place and distance, so do not repeat them.",
            },
            "concerns": {
                "type": "array",
                "items": {"type": "string"},
                "description": "At most four points a buyer should confirm with the seller, a few "
                "words each, most important first.",
            },
        }
        return {
            "type": "object",
            "additionalProperties": False,
            "required": list(properties),
            "properties": properties,
        }

    # -- evaluation ---------------------------------------------------------

    def brief(self, config) -> str:
        wants = "\n".join(f"- {point}" for point in self.wanted)
        unwanted = f"\n\nThey are not interested in {self.not_wanted}." if self.not_wanted else ""
        notes = f" {self.notes}" if self.notes else ""
        return f"""You are screening a used-goods listing for {self.audience}.

They are interested in:
{wants}
- available for local pickup within {config.radius_miles:g} miles of postal code {config.postal_code}{unwanted}

Describe this listing by filling in the structured fields. Do not guess: use "unknown" or null when the listing and the photos do not settle something, and put the open question in concerns.{notes}"""

    def passes(self, verdict: dict) -> bool:
        if not verdict.get("is_wanted_item"):
            return False
        if verdict.get("working") == "no" and not self.allow_not_working:
            return False
        if verdict.get("local_pickup_plausible") is False:
            return False
        for entry in self.custom:
            value = verdict.get(entry.name)
            if value is not None and key(value) in entry.reject:
                return False
        return True

    def relevant(self, title: str, description: str, *, strict: bool) -> bool:
        """A word from `include` anywhere is enough. Failing that, a word from `loose`
        will do on sources with a precise search, unless the title has a word from
        `exclude_title`. With neither list, everything goes on to Claude except
        titles with a word from `exclude_title`."""
        text = f"{title}\n{description}"
        if self.include and self.include.search(text):
            return True
        excluded = bool(self.exclude_title and self.exclude_title.search(title))
        if not (self.include or self.loose):
            return not excluded
        if strict or not self.loose or not self.loose.search(text):
            return False
        return not excluded

    # -- the post -----------------------------------------------------------

    def badges(self, verdict: dict) -> list[str]:
        found = []
        for entry in self.custom:
            value = verdict.get(entry.name)
            if value is not None and key(value) in entry.badges:
                found.append(entry.badges[key(value)])
        return found

    def colour(self, verdict: dict, flair: list[str]) -> str:
        return self.accent if flair else GREY

    def fields(self, verdict: dict) -> list[tuple[str, str, bool]]:
        fields = [("Condition", CONDITION_LABELS.get(verdict.get("working", "unknown"), "Not stated"), True)]
        for entry in self.custom:
            shown = entry.shown(verdict.get(entry.name)) if entry.label else None
            if shown:
                fields.append((entry.label, shown, True))
        return fields

    # -- the opening post ---------------------------------------------------

    def candidate_facts(self, verdict: dict) -> list[str]:
        facts = [f"working {flat(verdict.get('working', 'unknown'))}"]
        for entry in self.custom:
            value = verdict.get(entry.name)
            if value is not None and value != "":
                facts.append(f"{entry.name.replace('_', ' ')} {flat(clip(key(value), MAX_VALUE))}")
        return facts

    def welcome(self, config) -> str:
        return self.welcome_text or super().welcome(config)

    # -- testing ------------------------------------------------------------

    def sample(self, config) -> tuple[Listing, dict]:
        listing = Listing(
            source="test",
            source_id="0",
            url="https://example.org/sample-listing",
            title=clip(f"Sample: {self.item}", 80),
            price_cents=5000,
            location_text=config.area,
            distance_mi=5.0,
        )
        verdict: dict = {
            "is_wanted_item": True,
            "brand": None,
            "model": None,
            "working": "unknown",
            "local_pickup_plausible": True,
            "summary": "This is a test post from marketplace-finder, not a real listing.",
            "concerns": ["nothing; it is only a test"],
        }
        for entry in self.custom:
            if entry.type == "enum":
                verdict[entry.name] = entry.values[0]
            elif entry.type == "boolean":
                verdict[entry.name] = True
            else:
                verdict[entry.name] = None
        return listing, verdict
