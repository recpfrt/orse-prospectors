"""Website enrichment: extracts ONLY publicly displayed business information.

Collected: role email address shown on the site, KvK number, legal form, booking/emergency
signals, team-size signal (a number, never names), basic website quality, a short text
excerpt of the business's own homepage for personalization.
Not collected: personal names, personal email addresses, mobile numbers, social profiles.
"""

import datetime
import html as htmllib
import re
from urllib.parse import urljoin, urlparse

from . import config
from .collect import domain_of
from .customer_type import classify
from .places import best_place, place_names
from .http import polite_get

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
KVK_RE = re.compile(r"kvk[\s.:\-]*(?:nummer|nr\.?|no\.?)?[\s.:\-]*(\d{8})", re.I)
LINK_RE = re.compile(r"""<a\s[^>]*href=["']([^"'#]+)["'][^>]*>(.*?)</a>""", re.I | re.S)
TEAM_RE = re.compile(
    r"(\d{1,3})\s*(?:\+\s*)?(?:vaste\s+)?(?:medewerkers|monteurs|vakmensen|collega'?s|werknemers|installateurs|man\s+personeel|elektriciens|loodgieters)",
    re.I,
)
BRANCHES_RE = re.compile(r"(\d{1,2})\s+vestigingen", re.I)
SUBPAGE_HINTS = ("contact", "over-ons", "over ons", "overons", "afspraak", "team",
                 "wie-zijn-wij", "wie zijn wij", "bedrijf", "about", "diensten", "particulier")


def visible_text(page_html: str) -> str:
    text = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", page_html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", htmllib.unescape(text)).strip()


def extract_role_emails(page_html: str, site_domain: str) -> list[str]:
    """Role mailboxes on the business's own domain only (e.g. info@bedrijf.nl)."""
    raw = set(EMAIL_RE.findall(htmllib.unescape(page_html)))
    raw |= {m for m in re.findall(r"mailto:([^\"'?>\s]+)", page_html, re.I)}
    out = []
    for email in raw:
        email = email.strip().strip(".").lower()
        local, _, dom = email.partition("@")
        dom = dom[4:] if dom.startswith("www.") else dom
        if local in config.ROLE_LOCAL_PARTS and (dom == site_domain or dom.endswith("." + site_domain)):
            out.append(email)
    return sorted(set(out))


def _name_tokens(business_name: str | None, domain: str) -> set[str]:
    words = re.findall(r"[a-z0-9]{3,}", (business_name or "").lower())
    words += re.findall(r"[a-z0-9]{3,}", domain.split(".")[0])
    stop = {"installatie", "installatiebedrijf", "techniek", "loodgieter", "elektro", "service", "the", "and", "van", "voor"}
    return {w for w in words if w not in stop}


def detect_legal_form_with_evidence(text: str, business_name: str | None, domain: str) -> tuple[str, str | None]:
    """Conservative: 'bv' only when 'B.V.' is attached to this business's own name
    (or the OSM name itself ends with B.V.). Ambiguous -> 'unknown'.
    Returns (legal_form, evidence_snippet) so a human can check the classification."""
    if business_name and re.search(r"\bB\.?\s?V\.?$", business_name.strip()):
        return "bv", f"OSM name: {business_name}"
    tokens = _name_tokens(business_name, domain)
    found: dict[str, str] = {}
    for m in re.finditer(r"([A-Za-z0-9&'\-. ]{2,60}?)\s*,?\s+(B\.\s?V\.|B\.V|BV)(?=[\s,.)]|$)", text):
        if tokens and any(t in m.group(1).lower() for t in tokens):
            found.setdefault("bv", m.group(0).strip()[-80:])
    for m in re.finditer(r"([A-Za-z0-9&'\-. ]{2,60}?)\s+(V\.O\.F\.?|VOF|v\.o\.f\.?)(?=[\s,.)]|$)", text):
        if tokens and any(t in m.group(1).lower() for t in tokens):
            found.setdefault("vof", m.group(0).strip()[-80:])
    m = re.search(r".{0,40}\beenmanszaak\b.{0,20}", text, re.I)
    if m:
        found.setdefault("eenmanszaak", m.group(0).strip())
    if len(found) == 1:
        return next(iter(found.items()))
    return "unknown", ("conflicting: " + " | ".join(found.values())) if found else None


def detect_legal_form(text: str, business_name: str | None, domain: str) -> str:
    return detect_legal_form_with_evidence(text, business_name, domain)[0]


def detect_team(text: str) -> tuple[str, int | None]:
    numbers = [int(n) for n in TEAM_RE.findall(text) if 0 < int(n) < 1000]
    if numbers:
        n = max(numbers)
        return ("small" if n <= 15 else "medium" if n <= 25 else "large"), n
    if re.search(r"\b(familiebedrijf|klein team|ons team van)\b", text, re.I):
        return "small", None
    return "unknown", None


def website_quality(final_url: str, page_html: str, text: str) -> int:
    score = 0
    if final_url.startswith("https://"):
        score += 1
    if re.search(r"<meta[^>]+name=[\"']viewport", page_html, re.I):
        score += 1
    years = [int(y) for y in re.findall(r"(?:©|&copy;|copyright)\s*(?:\d{4}\s*[-–]\s*)?(20\d{2})", page_html + text, re.I)]
    if years and max(years) >= datetime.date.today().year - 2:
        score += 1
    return score


def _subpage_links(base_url: str, page_html: str, site_domain: str) -> list[str]:
    links = []
    for href, label in LINK_RE.findall(page_html):
        url = urljoin(base_url, href.strip())
        if domain_of(url) != site_domain or not url.startswith("http"):
            continue
        hay = (urlparse(url).path + " " + visible_text(label)).lower()
        if any(h in hay for h in SUBPAGE_HINTS) and url not in links and url.rstrip("/") != base_url.rstrip("/"):
            links.append(url)
    return links[: config.MAX_PAGES_PER_SITE - 1]



POSTCODE_CITY_RE = re.compile(r"\b[1-9]\d{3}\s?(?!SA|SD|SS)[A-Z]{2}[\s,]{1,3}((?:['A-Za-z\-]+\s?){1,5})")
# Lead-broker / referral networks sell homeowner requests to tradespeople; not our target.
BROKER_PHRASES = ["aangesloten vakman", "aangesloten vakmensen", "aangesloten bedrijven", "ons netwerk van",
                  "landelijk netwerk", "in heel nederland", "door heel nederland", "landelijk dekkend",
                  "wij koppelen", "vergelijk offertes", "gratis offertes", "partners in uw regio",
                  "binnen 30 minuten", "in uw regio actief", "vakspecialisten in de buurt"]
WEBSHOP_MARKERS = ["winkelwagen", "winkelmand", "in mijn winkelwagen", "add to cart", "toevoegen aan winkelwagen",
                   "afrekenen", "woocommerce", "shopify", "gratis verzending", "bezorging"]


def site_name(page_html: str, domain: str) -> str:
    m = re.search(r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']{2,80})', page_html, re.I)
    if m:
        return htmllib.unescape(m.group(1)).strip()
    m = re.search(r"<title[^>]*>(.*?)</title>", page_html, re.I | re.S)
    if m:
        title = re.sub(r"\s+", " ", htmllib.unescape(m.group(1))).strip()
        parts = [x.strip() for x in re.split(r"\s[|\-–—:]\s", title) if x.strip()]
        generic = {"home", "homepage", "welkom", "contact", "startpagina"}
        parts = [x for x in parts if x.lower() not in generic]
        if parts:
            return min(parts, key=len) if len(parts) > 1 and len(parts[0]) > 45 else parts[0]
    return domain


def postcode_city(text: str) -> str | None:
    """Town after the first Dutch postcode, validated against the CBS place list when available."""
    for m in POSTCODE_CITY_RE.finditer(text):
        tokens = m.group(1).split()
        if place_names():
            place = best_place(tokens)
            if place:
                return place
        elif tokens and tokens[0][:1].isupper():
            return tokens[0]
    return None


def enrich(candidate: dict) -> dict:
    site = candidate["website"]
    domain = candidate["domain"]
    result = {**candidate, "fetch_status": None, "pages_fetched": 0, "robots_blocked": False}

    status, final_url, home = polite_get(site)
    result["fetch_status"] = status
    if status == -1:
        result["robots_blocked"] = True
        return result
    if status != 200 or not home:
        return result

    final_domain = domain_of(final_url) or domain
    pages = [(final_url, home)]
    for url in _subpage_links(final_url, home, final_domain):
        s, u, body = polite_get(url)
        if s == 200 and body:
            pages.append((u, body))
    result["pages_fetched"] = len(pages)

    all_html = "\n".join(p for _, p in pages)
    all_text = visible_text(all_html)
    lower_html, lower_text = all_html.lower(), all_text.lower()

    emails, email_source = [], None
    for url, body in pages:
        found = extract_role_emails(body, final_domain)
        if found:
            emails, email_source = found, url
            break

    kvk = KVK_RE.search(all_text)
    legal_form, legal_evidence = detect_legal_form_with_evidence(
        all_text, candidate.get("business_name") or site_name(home, final_domain), final_domain)
    team_size, team_n = detect_team(all_text)
    branches = [int(n) for n in BRANCHES_RE.findall(all_text)]
    tools = [m for m in config.BOOKING_TOOL_MARKERS if m in lower_html]
    phrases = [p for p in config.BOOKING_PHRASES if p in lower_text]
    words = re.findall(r"[a-zà-ÿ]+", lower_text)
    dutch_words = {"de", "het", "een", "en", "van", "voor", "uw", "wij", "met", "op", "is", "zijn", "u", "onze", "ook"}
    dutch_ratio = sum(w in dutch_words for w in words) / max(len(words), 1)

    business_name = candidate.get("business_name") or site_name(home, final_domain)
    city = candidate.get("city") or postcode_city(all_text)
    webshop_hits = [m for m in WEBSHOP_MARKERS if m in lower_text or m in lower_html]
    result.update({
        "business_name": business_name,
        "city": city,
        "nl_address_found": bool(POSTCODE_CITY_RE.search(all_text)),
        "webshop_markers": webshop_hits,
        "broker_markers": [m for m in BROKER_PHRASES if m in lower_text],
        "final_url": final_url,
        "public_email": emails[0] if emails else None,
        "email_source_url": email_source,
        "kvk_number": kvk.group(1) if kvk else None,
        "legal_form": legal_form,
        "legal_form_evidence": legal_evidence,
        "booking_tools": tools,
        "booking_phrases": phrases,
        "online_booking": bool(tools or phrases),
        "emergency_service": any(p in lower_text for p in config.EMERGENCY_PHRASES),
        "team_signal": team_size,
        "team_count": team_n,
        "branches": max(branches) if branches else None,
        "website_quality": website_quality(final_url, home, visible_text(home)),
        "is_dutch": 'lang="nl' in lower_html[:5000] or dutch_ratio >= 0.08,
        "homepage_excerpt": visible_text(home)[:3000],
        **classify(lower_text),
    })
    return result
