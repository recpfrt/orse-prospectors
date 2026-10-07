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
from .customer_type import classify, dual_audience, installer_check
from .places import best_place, norm, place_names
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


BV_SUFFIX = r"(?i:B\.\s?V\.?|BV)"
VOF_SUFFIX = r"(?i:V\.\s?O\.\s?F\.?|VOF)"


def _compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _prefix_matches(prefix: str, tokens: set[str]) -> bool:
    """Business-name/domain token near the legal form; also matches with spaces removed
    ('FOEK installatie vof' <-> foekinstallatie.nl)."""
    if not tokens:
        return False
    low, compact = prefix.lower(), _compact(prefix)
    tail = _compact(" ".join(prefix.split()[-3:]))
    return any(t in low or t in compact or (len(tail) >= 4 and tail in t) for t in tokens)


def detect_legal_form_with_evidence(text: str, business_name: str | None, domain: str,
                                    bare_eenmanszaak: bool = True) -> tuple[str, str | None]:
    """Conservative: 'bv' only when B.V. (any case: B.V., b.v., BV) is attached to this business's own name,
    or the business name itself ends with it. Ambiguous -> 'unknown'.
    bare_eenmanszaak=False ignores a lone word 'eenmanszaak' (legal/terms templates use it generically).
    Returns (legal_form, evidence_snippet) so a human can check the classification."""
    if business_name and re.search(r"\b" + BV_SUFFIX + r"$", business_name.strip()):
        return "bv", f"business name: {business_name}"
    tokens = _name_tokens(business_name, domain)
    found: dict[str, str] = {}
    for m in re.finditer(r"([A-Za-z0-9&'\-. ]{2,60}?)\s*,?\s+" + BV_SUFFIX + r"(?=[\s,.)]|$)", text):
        if _prefix_matches(m.group(1), tokens):
            found.setdefault("bv", m.group(0).strip()[-80:])
    for m in re.finditer(r"([A-Za-z0-9&'\-. ]{2,60}?)\s*,?\s+" + VOF_SUFFIX + r"(?=[\s,.)]|$)", text):
        if _prefix_matches(m.group(1), tokens):
            found.setdefault("vof", m.group(0).strip()[-80:])
    m = re.search(r".{0,40}\beenmanszaak\b.{0,20}", text, re.I) if bare_eenmanszaak else None
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


GENERIC_NAMES = {"bedrijf", "home", "homepage", "welkom", "index", "contact", "website", "startpagina"}


def site_name(page_html: str, domain: str) -> str:
    name = _raw_site_name(page_html, domain)
    label = domain.split(".")[0]
    # generic words, or a different website's name (e.g. the web agency's credit) -> use the domain
    if name.lower().strip() in GENERIC_NAMES or len(name.strip()) < 3 or (
            re.fullmatch(r"[\w\-]+\.(nl|com|eu)", name.strip().lower()) and label not in name.lower()):
        return domain
    return name


def _raw_site_name(page_html: str, domain: str) -> str:
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



FRANCHISE_PHRASES = ["franchise", "franchisenemer", "franchiseformule", "onderdeel van de", "vestiging bij u in de buurt",
                     "vestigingen in", "landelijk werkzaam", "landelijke dekking", "regiokantoor", "regiokantoren"]


def town_link_count(page_html: str) -> int:
    """Number of distinct links whose anchor text is exactly a Dutch place name.
    Many such links = a network of regional landing pages ('loodgieter Amersfoort', 'loodgieter Baarn', ...)."""
    places = place_names()
    towns = set()
    for _, label in LINK_RE.findall(page_html):
        text = visible_text(label)
        words = text.split()
        if not words or len(words) > 4:
            continue
        candidate = norm(words[-1]) if len(words) > 1 else norm(text)
        if candidate in places:
            towns.add(candidate)
    return len(towns)


def bv_evidence_strength(evidence: str | None, business_name: str | None, domain: str) -> str | None:
    """'strong' when the business name itself ends in B.V., or a name/domain token appears within the words
    directly before B.V. (any case). Otherwise 'weak' (e.g. 'KVK nummer M&I BV' on mirva.nl): human KvK check."""
    if not evidence:
        return None
    if evidence.startswith(("OSM name:", "business name:")):
        return "strong"
    tokens = _name_tokens(business_name, domain)
    before = re.split(r"\s*,?\s+" + BV_SUFFIX + r"\s*$", evidence.strip())[0]
    near = " ".join(before.split()[-4:])
    return "strong" if _prefix_matches(near, tokens) else "weak"


# Legal / company-information pages, in priority order (pages most likely to name the legal entity first).
LEGAL_PAGE_HINTS = (
    ("colofon", "bedrijfsgegevens", "company-information", "company information", "impressum"),
    ("algemene-voorwaarden", "algemene voorwaarden", "algemenevoorwaarden", "voorwaarden", "terms"),
    ("privacyverklaring", "privacybeleid", "privacy-policy", "privacy statement", "privacy"),
    ("disclaimer",),
    ("cookiebeleid", "cookieverklaring", "cookie-policy", "cookies", "cookie"),
)
# Legal pages phrase it as "ingeschreven bij de KvK / Kamer van Koophandel onder nummer 12345678".
# Exactly 8 digits (phone numbers have 10); used only on legal pages, the main-page KVK_RE is unchanged.
LEGAL_KVK_RE = re.compile(r"(?:\bkvk|kamer van koophandel)\D{0,40}?(?<!\d)(\d{8})(?!\d)", re.I)
NON_HTML_EXT = (".pdf", ".doc", ".docx", ".jpg", ".jpeg", ".png", ".zip")


def legal_page_links(base_url: str, page_html: str, site_domain: str, exclude=()) -> list[str]:
    """Same-site links to legal/company-information pages, best first, at most MAX_LEGAL_PAGES."""
    skip = {u.rstrip("/") for u in exclude} | {base_url.rstrip("/")}
    ranked = []
    for href, label in LINK_RE.findall(page_html):
        url = urljoin(base_url, htmllib.unescape(href.strip()))
        if not url.startswith("http") or domain_of(url) != site_domain or url.rstrip("/") in skip:
            continue
        path = urlparse(url).path.lower()
        if path.endswith(NON_HTML_EXT):
            continue
        hay = (path + " " + visible_text(label)).lower()
        for rank, group in enumerate(LEGAL_PAGE_HINTS):
            if any(h in hay for h in group):
                ranked.append((rank, url))
                break
    out = []
    for _, url in sorted(ranked, key=lambda x: x[0]):
        if url.rstrip("/") not in {u.rstrip("/") for u in out}:
            out.append(url)
    return out[: config.MAX_LEGAL_PAGES]


def evidence_page(pages: list[tuple[str, str]], evidence: str | None) -> str | None:
    """URL of the fetched page that contains the evidence snippet (provenance)."""
    if not evidence or evidence.startswith(("business name:", "OSM name:", "conflicting")):
        return None
    needle = _compact(evidence)
    for url, body in pages:
        if needle and needle in _compact(visible_text(body)):
            return url
    return None


def needs_legal_pages(p: dict) -> bool:
    """Only when the normal pages gave no usable legal form: nothing found, or a weak B.V. mention.
    Conflicting forms or a stated V.O.F./eenmanszaak are left alone."""
    form, evidence = p.get("legal_form"), str(p.get("legal_form_evidence") or "")
    if form == "unknown" or form is None:
        return not evidence.startswith("conflicting")
    return form == "bv" and p.get("legal_form_confidence") != "strong"


def fetch_legal_evidence(base_url: str, home_html: str, site_domain: str, business_name: str | None,
                         exclude=()) -> dict:
    """Fetch up to MAX_LEGAL_PAGES legal pages (robots-aware, rate-limited) and detect the legal form there.
    Legal-page text is used ONLY for legal form and KvK number, never for customer-type or trade signals."""
    pages = []
    for url in legal_page_links(base_url, home_html, site_domain, exclude):
        status, final, body = polite_get(url)
        if status == 200 and body:
            pages.append((final, body))
    text = visible_text("\n".join(b for _, b in pages))
    form, evidence = detect_legal_form_with_evidence(text, business_name, site_domain, bare_eenmanszaak=False)
    kvk = KVK_RE.search(text) or LEGAL_KVK_RE.search(text)
    return {
        "pages": [u for u, _ in pages],
        "legal_form": form,
        "legal_form_evidence": evidence,
        "legal_form_confidence": bv_evidence_strength(evidence, business_name, site_domain) if form == "bv" else None,
        "source_url": evidence_page(pages, evidence),
        "kvk_number": kvk.group(1) if kvk else None,
        "kvk_source_url": evidence_page(pages, kvk.group(0)) if kvk else None,
    }


def merge_legal_evidence(current: tuple, legal: tuple) -> tuple[str, str | None, str | None, bool]:
    """Combine (form, evidence, confidence) from the normal pages with the legal pages.
    Returns (form, evidence, confidence, legal_pages_used). Never upgrades on a conflict."""
    c_form, c_ev, c_conf = current
    l_form, l_ev, l_conf = legal
    c_form = c_form or "unknown"
    if l_form == "unknown":
        return c_form, c_ev, c_conf, False
    if c_form == "unknown":
        if str(c_ev or "").startswith("conflicting"):
            return c_form, c_ev, c_conf, False
        return l_form, l_ev, l_conf, True
    if c_form == l_form:
        if c_form == "bv" and c_conf != "strong" and l_conf == "strong":
            return l_form, l_ev, l_conf, True
        return c_form, c_ev, c_conf, False
    return "unknown", f"conflicting: {c_ev} | {l_ev}", None, False


def apply_legal_pages(p: dict, legal: dict) -> dict:
    """Merge legal-page evidence into a prospect record, keeping explicit provenance."""
    form, evidence, confidence, used = merge_legal_evidence(
        (p.get("legal_form"), p.get("legal_form_evidence"), p.get("legal_form_confidence")),
        (legal["legal_form"], legal["legal_form_evidence"], legal["legal_form_confidence"]))
    out = {**p, "legal_pages_checked": legal["pages"], "legal_form": form, "legal_form_evidence": evidence,
           "legal_form_confidence": confidence}
    if used:
        out["legal_form_source"] = "legal_page"
        out["legal_form_source_url"] = legal["source_url"]
    elif form == "unknown":
        out["legal_form_source"] = None
        out["legal_form_source_url"] = None
    if not p.get("kvk_number") and legal.get("kvk_number"):
        out["kvk_number"] = legal["kvk_number"]
        out["kvk_number_source_url"] = legal.get("kvk_source_url")
    return out


def legal_recheck(p: dict) -> dict:
    """For a stored prospect: re-fetch the homepage only to find legal links, then those pages.
    Returns {'status': http status, 'legal': evidence dict or None}."""
    site = p.get("final_url") or p.get("website")
    status, final_url, home = polite_get(site)
    if status != 200 or not home:
        return {"status": status, "legal": None}
    site_domain = domain_of(final_url) or p["domain"]
    return {"status": status,
            "legal": fetch_legal_evidence(final_url, home, site_domain, p.get("business_name"))}


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
        # boolean only: the phone number itself is not stored
        "nl_phone_found": bool(re.search(r"(?:\+31|0031|\b0)[1-9](?:[\s\-]?\d){8}\b", all_text)),
        "webshop_markers": webshop_hits,
        "broker_markers": [m for m in BROKER_PHRASES if m in lower_text],
        "franchise_markers": [m for m in FRANCHISE_PHRASES if m in lower_text],
        "town_link_count": town_link_count(all_html),
        "final_url": final_url,
        "public_email": emails[0] if emails else None,
        "email_source_url": email_source,
        "kvk_number": kvk.group(1) if kvk else None,
        "legal_form": legal_form,
        "legal_form_evidence": legal_evidence,
        "legal_form_confidence": (bv_evidence_strength(legal_evidence, business_name, final_domain)
                                  if legal_form == "bv" else None),
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
        **installer_check(lower_text),
        "dual_audience_evidence": dual_audience(lower_text),
        "legal_form_source": "website_pages" if legal_form != "unknown" else None,
        "legal_form_source_url": evidence_page(pages, legal_evidence) if legal_form != "unknown" else None,
        "legal_pages_checked": [],
    })
    if needs_legal_pages(result):
        legal = fetch_legal_evidence(final_url, home, final_domain, business_name, exclude=[u for u, _ in pages])
        result = apply_legal_pages(result, legal)
    return result
