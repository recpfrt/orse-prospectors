"""Candidate collection from OpenStreetMap: Overpass first, Geofabrik extract as fallback.

Only OSM business tags are kept: name, craft, website, city, business phone/email tags.
"""

import json
import re
import urllib.parse
import urllib.request
from collections import Counter
from urllib.parse import urlparse

from . import config


def normalize_website(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = raw.strip().split(";")[0].strip()
    if not raw:
        return None
    if not raw.lower().startswith(("http://", "https://")):
        raw = "https://" + raw
    parsed = urlparse(raw)
    if not parsed.netloc or "." not in parsed.netloc:
        return None
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path or '/'}"


def domain_of(url: str | None) -> str | None:
    if not url:
        return None
    host = urlparse(url).netloc.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


NAME_CATEGORY = [
    (re.compile(r"loodgiet|sanitair|riool|ontstop|lekkage", re.I), "plumber"),
    (re.compile(r"elektrotechn|elektro|elektricien|electric|installatiebedrijf.*elektr", re.I), "electrician"),
    (re.compile(r"installat|verwarming|klimaat|warmtepomp|airco|cv|cv-", re.I), "hvac"),
]
# Objects with these keys are shops, amenities or infrastructure, not trade businesses.
NON_BUSINESS_KEYS = ("shop", "amenity", "highway", "power", "railway", "route", "landuse",
                     "public_transport", "leisure", "tourism")


def _category(tags: dict) -> str | None:
    by_craft = config.CRAFT_CATEGORY.get(tags.get("craft", ""))
    if by_craft:
        return by_craft
    if tags.get("craft") or any(k in tags for k in NON_BUSINESS_KEYS):
        return None
    name = tags.get("name") or ""
    for pattern, category in NAME_CATEGORY:
        if pattern.search(name):
            return category
    return None


def _candidate(osm_type: str, osm_id: int, tags: dict) -> dict | None:
    category = _category(tags)
    if not category:
        return None
    website = normalize_website(tags.get("website") or tags.get("contact:website") or tags.get("url"))
    return {
        "osm_id": f"{osm_type}/{osm_id}",
        "business_name": tags.get("name"),
        "craft": tags.get("craft"),
        "matched_by": "craft" if tags.get("craft") in config.CRAFT_CATEGORY else "name",
        "service_category": category,
        "website": website,
        "domain": domain_of(website),
        "city": tags.get("addr:city"),
        "osm_email": tags.get("email") or tags.get("contact:email"),
        "osm_phone": tags.get("phone") or tags.get("contact:phone"),
    }


def collect_overpass(city: str) -> list[dict]:
    """Raises RuntimeError if every Overpass mirror fails."""
    crafts = "|".join(config.CRAFT_CATEGORY)
    query = (
        f'[out:json][timeout:50];'
        f'area["name"="{city}"]["admin_level"="8"]["boundary"="administrative"]->.a;'
        f'nwr["craft"~"^({crafts})$"](area.a);out tags;'
    )
    body = urllib.parse.urlencode({"data": query}).encode()
    errors = []
    for endpoint in config.OVERPASS_ENDPOINTS:
        try:
            req = urllib.request.Request(endpoint, data=body, headers={"User-Agent": config.USER_AGENT})
            with urllib.request.urlopen(req, timeout=70) as resp:
                elements = json.load(resp)["elements"]
            out = [_candidate(e["type"], e["id"], e.get("tags", {})) for e in elements]
            return [c for c in out if c]
        except Exception as e:  # noqa: BLE001 - try next mirror
            errors.append(f"{endpoint}: {e}")
    raise RuntimeError("All Overpass endpoints failed: " + " | ".join(errors))


def collect_geofabrik(pbf_path: str, city: str | None) -> list[dict]:
    """Reads a local Geofabrik .osm.pbf extract. city=None returns the whole extract."""
    import osmium  # local dependency, only needed for the fallback

    out = []
    for obj in osmium.FileProcessor(pbf_path).with_filter(osmium.filter.KeyFilter("name")):
        tags = {t.k: t.v for t in obj.tags}
        osm_type = {"n": "node", "w": "way", "r": "relation"}.get(obj.type_str(), obj.type_str())
        cand = _candidate(osm_type, obj.id, tags)
        if not cand:
            continue
        if city and (cand["city"] or "").strip().lower() != city.lower():
            continue
        out.append(cand)
    return out


def dedupe(candidates: list[dict], blocked_domains: set[str], blocked_emails: set[str]) -> tuple[list[dict], Counter]:
    """Drops entries without website, red-listed / already-contacted, and duplicate domains.
    A domain that appears on 3+ OSM entries is marked as a probable chain."""
    stats: Counter = Counter()
    domain_counts = Counter(c["domain"] for c in candidates if c["domain"])
    seen: set[str] = set()
    kept = []
    for c in candidates:
        if not c["domain"]:
            stats["no_website"] += 1
            continue
        if c["domain"] in blocked_domains or (c["osm_email"] or "").lower() in blocked_emails:
            stats["red_listed_or_contacted"] += 1
            continue
        if c["domain"] in seen:
            stats["duplicate_domain"] += 1
            continue
        seen.add(c["domain"])
        c["probable_chain"] = domain_counts[c["domain"]] >= 3
        kept.append(c)
    stats["kept"] = len(kept)
    return kept, stats


DOMAIN_CATEGORY = [
    (re.compile(r"loodgiet|sanitair|riool|ontstop|lekkage"), "plumber"),
    (re.compile(r"elektr|electr"), "electrician"),
    (re.compile(r"install|verwarm|klimaat|warmtepomp|airco|cv"), "hvac"),
]


def collect_commoncrawl(tsv_path: str, sample: int, seed: int, exclude_domains: set[str]) -> list[dict]:
    """Candidates from the Common Crawl domain list (pre-filtered to .nl trade keywords).
    Only the domain is known here; name and city come from the business's own website."""
    import random

    domains = []
    for line in open(tsv_path, encoding="utf-8"):
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 5:
            continue
        domain = ".".join(reversed(parts[4].split(".")))
        if domain.count(".") != 1 or domain in exclude_domains:
            continue  # registrable .nl domains only
        domains.append(domain)
    random.Random(seed).shuffle(domains)
    out = []
    for domain in domains[:sample]:
        category = next((c for p, c in DOMAIN_CATEGORY if p.search(domain)), "hvac")
        out.append({
            "osm_id": None, "business_name": None, "craft": None, "matched_by": "domain_keyword",
            "service_category": category, "website": f"https://{domain}/", "domain": domain,
            "city": None, "osm_email": None, "osm_phone": None, "source": "commoncrawl",
        })
    return out
