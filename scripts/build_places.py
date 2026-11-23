"""Rebuild src/marketplace_finder/data/places_us.csv from the Census Bureau's gazetteer.

The gazetteer lists every incorporated place and census-designated place in the
United States with a representative point. It is a work of the US government
and in the public domain.

    python scripts/build_places.py                # downloads the file
    python scripts/build_places.py places.zip     # or use a copy already downloaded

Marketplaces give a listing's location as "City, ST"; this table turns that
into coordinates when the listing has none of its own.
"""

from __future__ import annotations

import csv
import io
import re
import sys
import urllib.request
import zipfile
from pathlib import Path

YEAR = 2025
URL = (
    "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/"
    f"{YEAR}_Gazetteer/{YEAR}_Gaz_place_national.zip"
)
OUTPUT = Path(__file__).resolve().parent.parent / "src" / "marketplace_finder" / "data" / "places_us.csv"

# The kind of place, which the gazetteer appends to the name ("Sacramento city").
KIND = re.compile(
    r"\s+(city and borough|city|town|village|borough|municipality|CDP|comunidad|zona urbana|"
    r"corporation|urban county|unified government|consolidated government|"
    r"metropolitan government|metro government|government)$"
)
# Consolidated city-counties, as people actually write them.
RENAMED = {
    "Nashville-Davidson": "Nashville",
    "Louisville/Jefferson County": "Louisville",
    "Athens-Clarke County": "Athens",
    "Augusta-Richmond County": "Augusta",
    "Lexington-Fayette": "Lexington",
    "Macon-Bibb County": "Macon",
    "Butte-Silver Bow": "Butte",
    "Anaconda-Deer Lodge County": "Anaconda",
    "Urban Honolulu": "Honolulu",
    "Boise City": "Boise",
}


def place_name(raw: str) -> str:
    name = raw.replace(" (balance)", "").strip()
    name = KIND.sub("", name)
    return RENAMED.get(name, name)


def read_rows(source: str | None):
    if source:
        data = Path(source).read_bytes()
    else:
        with urllib.request.urlopen(URL, timeout=60) as response:
            data = response.read()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        text = archive.read(archive.namelist()[0]).decode("utf-8")
    delimiter = "|" if "|" in text.splitlines()[0] else "\t"
    for row in csv.DictReader(io.StringIO(text), delimiter=delimiter):
        yield {key.strip(): (value or "").strip() for key, value in row.items()}


def main() -> None:
    places: dict[tuple[str, str], tuple[int, str, str]] = {}
    for row in read_rows(sys.argv[1] if len(sys.argv) > 1 else None):
        key = (place_name(row["NAME"]), row["USPS"])
        land = int(row["ALAND"] or 0)
        # Two places in one state can share a name; the larger is the one people mean.
        if key not in places or land > places[key][0]:
            places[key] = (land, f"{float(row['INTPTLAT']):.4f}", f"{float(row['INTPTLONG']):.4f}")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(["name", "state", "lat", "lon"])
        for (name, state), (_, lat, lon) in sorted(places.items(), key=lambda item: (item[0][1], item[0][0])):
            writer.writerow([name, state, lat, lon])
    print(f"wrote {len(places)} places to {OUTPUT}")


if __name__ == "__main__":
    main()
