"""Dutch place names (CBS open data, table 85516NED 'Woonplaatsen') for town parsing and
regional-landing-page detection."""

import json
import re
from functools import lru_cache
from pathlib import Path

CBS_FILE = Path(__file__).resolve().parent.parent / "data" / "raw" / "cbs" / "woonplaatsen.json"

# Trade words stripped from a domain before looking for a place name in what remains.
SERVICE_WORDS = sorted([
    "ontstoppingsbedrijf", "ontstoppingsdienst", "ontstoppingsservice", "ontstoppen", "ontstopping",
    "rioolservice", "rioolontstopping", "riolering", "riool", "loodgietersbedrijf", "loodgieters",
    "loodgieter", "installatiebedrijf", "installatietechniek", "installaties", "installatie", "installateur",
    "elektrotechniek", "elektriciens", "elektricien", "electriciens", "electricien", "elektro", "electro",
    "elektra", "verwarming",
    "klimaattechniek", "klimaat", "airco", "aircoservice", "sanitair", "lekkage", "warmtepompen", "warmtepomp",
    "service", "spoed", "techniek", "bedrijf", "cv",
], key=len, reverse=True)


def norm(text: str) -> str:
    return re.sub(r"[^a-z]", "", text.lower())


@lru_cache(maxsize=1)
def place_names() -> dict[str, str]:
    """normalized -> official name. Empty if the CBS file is not present (feature then disabled)."""
    if not CBS_FILE.exists():
        return {}
    data = json.loads(CBS_FILE.read_text(encoding="utf-8"))["value"]
    names: dict[str, str] = {}
    for item in data:
        title = item.get("Title")
        if not title:
            continue
        names[norm(title)] = title
        base = re.sub(r"\s*\(.*?\)\s*$", "", title)   # "Bergen (NH)" -> "Bergen"
        names.setdefault(norm(base), base)
    return names


def best_place(tokens: list[str]) -> str | None:
    """Longest prefix of tokens that is an official place name (e.g. ['Hedel', 'Info'] -> 'Hedel')."""
    places = place_names()
    for n in range(min(len(tokens), 5), 0, -1):
        candidate = norm(" ".join(tokens[:n]))
        if candidate in places:
            return places[candidate]
    return None


def domain_place(domain: str) -> str | None:
    """Town advertised in a domain in the broker/landing-page pattern '<trade word><town>',
    e.g. ontstoppingsbedrijfvelp.nl -> Velp, electricien-voorburg.nl -> Voorburg.
    A place name BEFORE the trade word is usually a surname (vandongeninstallatie, nijbroekinstallatiebedrijf)
    and is ignored."""
    places = place_names()
    label = domain.split(".")[0].lower()
    matches = [(label.find(w), label.find(w) + len(w)) for w in SERVICE_WORDS if w in label]
    if not matches:
        return None
    earliest = min(start for start, _ in matches)
    first_trade_end = max(end for start, end in matches if start == earliest)  # longest word at that spot
    rest = label[first_trade_end:]
    for word in SERVICE_WORDS:
        rest = rest.replace(word, " ")
    for token in re.split(r"[\s\-_0-9]+", rest):
        if len(token) >= 4 and token in places:
            return places[token]
    return None


def landing_page_mismatch(domain: str, city: str | None) -> str | None:
    """Reason string if the domain advertises a different town than the business's address."""
    place = domain_place(domain)
    if not place or not city:
        return None
    if norm(place) in norm(city) or norm(city) in norm(place):
        return None
    return f"regional landing page: domain names '{place}', business address in '{city}'"
