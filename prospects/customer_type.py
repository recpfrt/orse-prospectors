"""Deterministic homeowner-vs-business classification from a business's public website text.

Explainable by design: returns the matched terms (with counts) that produced the decision.
Each term contributes weight * min(count, 3), so one repeated word cannot dominate.
"""

import re

# Signals that a business serves homeowners / consumers (strong fit for LeadFlow).
HOMEOWNER_TERMS = {
    3: ["particulier", "particulieren", "uw woning", "uw huis", "thuis", "huiseigenaar", "huiseigenaren",
        "huurder", "cv-ketel", "cv ketel", "ketelonderhoud", "ketel vervangen", "lekkage", "verstopping",
        "ontstoppen", "badkamer", "badkamerrenovatie", "toilet", "groepenkast", "meterkast",
        "laadpaal thuis", "woonhuis", "woningeigenaar"],
    2: ["storing", "spoed", "boiler", "radiator", "vloerverwarming", "thermostaat", "airco",
        "warmtepomp", "keuken", "dakgoot", "riolering", "kraan", "geiser", "zonnepanelen",
        "onderhoudsabonnement", "onderhoudscontract"],
}

# Signals that a business primarily serves commercial / industrial / project clients (weak fit).
BUSINESS_TERMS = {
    3: ["utiliteit", "utiliteitsbouw", "industrie", "industriële", "industriele", "hoofdaannemer",
        "aannemers", "zakelijke klanten", "kantoorgebouwen", "zorginstellingen", "ziekenhuizen",
        "medische gassen", "datacenter", "datacenters", "nieuwbouwprojecten", "bouwteam",
        "werktuigbouwkundige", "werktuigbouwkundig", "gebouwbeheer", "facilitair", "woningcorporaties",
        "vastgoedeigenaren", "overheid", "b2b", "infrastructuren", "procesinstallaties", "engineering"],
    1: ["zakelijk", "projecten", "retail", "horeca", "kantoren", "winkels", "bedrijfspanden",
        "scholen", "aannemer", "gebouwgebonden"],
}

MIN_SIGNAL = 6     # below this on both sides -> unknown
DOMINANCE = 2.0    # one side must be at least 2x the other to win outright


def _weighted_hits(text: str, table: dict[int, list[str]]) -> tuple[int, list[str]]:
    total, evidence = 0, []
    for weight, terms in table.items():
        for term in terms:
            n = len(re.findall(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text))
            if n:
                total += weight * min(n, 3)
                evidence.append(f"{term}×{n}")
    return total, evidence


def classify(lower_text: str) -> dict:
    home, home_ev = _weighted_hits(lower_text, HOMEOWNER_TERMS)
    biz, biz_ev = _weighted_hits(lower_text, BUSINESS_TERMS)
    if home < MIN_SIGNAL and biz < MIN_SIGNAL:
        kind = "unknown"
    elif home >= DOMINANCE * biz and home >= MIN_SIGNAL:
        kind = "homeowner"
    elif biz >= DOMINANCE * home and biz >= MIN_SIGNAL:
        kind = "business"
    else:
        kind = "mixed"
    return {
        "customer_type": kind,
        "homeowner_signal": home,
        "business_signal": biz,
        "customer_type_evidence": {"homeowner": home_ev[:10], "business": biz_ev[:10]},
    }


# Positive evidence that the site belongs to an installation / repair trade business.
TRADE_TERMS = ["installateur", "installatiebedrijf", "installatietechniek", "loodgieter", "loodgietersbedrijf",
               "cv-ketel", "cv ketel", "elektricien", "elektrotechniek", "elektrotechnisch", "groepenkast",
               "meterkast", "airco", "airconditioning", "warmtepomp", "ventilatie", "riolering", "ontstoppen",
               "ontstopping", "sanitair", "verwarming", "vloerverwarming", "laadpaal", "lekkage", "boiler",
               "storingsdienst", "installaties", "monteur", "monteurs"]
# Evidence of an unrelated business that happens to match trade keywords (e.g. motor/automotive, retail, training).
UNRELATED_TERMS = ["apk", "autobedrijf", "autogarage", "auto's", "occasions", "banden", "motorfiets", "motoren",
                   "scooter", "fietsen", "caravan", "camper", "jachtbouw", "makelaar", "kapsalon", "restaurant",
                   "webdesign", "opleidingen", "cursussen", "groothandel", "fabrikant", "distributeur",
                   "importeur", "uitzendbureau", "detachering", "geluidsinstallatie", "geluidsinstallaties",
                   "geluidstechniek", "audio", "licht en geluid", "omroepinstallatie"]
# Installers often have job openings; never count these as unrelated (older stored evidence may contain them).
IGNORED_UNRELATED = {"vacatures"}
DOMAIN_TRADE_RE = re.compile(r"install|loodgiet|sanitair|elektr|electr|verwarm|klimaat|warmtepomp|airco|riool|ontstop|lekkage")


def decide_installer(trade: int, unrelated_evidence: list[str], domain: str) -> tuple[bool, str]:
    """Explicit installer rule, re-applicable to stored results without re-fetching."""
    unrelated = 0
    for item in unrelated_evidence:
        term, _, count = item.rpartition("×")
        if term not in IGNORED_UNRELATED:
            unrelated += min(int(count or 1), 3)
    domain_trade = bool(DOMAIN_TRADE_RE.search(domain or ""))
    if trade >= 6 and trade >= 2 * unrelated:
        return True, f"{trade} trade terms vs {unrelated} unrelated"
    if trade >= 3 and unrelated == 0:
        return True, f"{trade} trade terms, no unrelated terms"
    if trade >= 2 and unrelated == 0 and domain_trade:
        return True, f"{trade} trade terms + trade word in domain"
    return False, f"only {trade} trade terms vs {unrelated} unrelated"


def installer_check(lower_text: str) -> dict:
    trade, trade_ev = _weighted_hits(lower_text, {1: TRADE_TERMS})
    unrelated, unrelated_ev = _weighted_hits(lower_text, {1: UNRELATED_TERMS})
    return {
        "trade_signal": trade,
        "unrelated_signal": unrelated,
        "installer_evidence": {"trade": trade_ev[:8], "unrelated": unrelated_ev},
    }


# Marine / shipping B2B market (e.g. electrical installers for inland shipping). Strong terms name the sector
# itself; weak terms (ship, yacht, nautical, ...) only count alongside a strong term, never on their own.
MARINE_TERMS = {
    3: ["scheepvaart", "binnenvaart", "zeevaart", "koopvaardij", "maritiem", "maritieme", "scheepsbouw",
        "jachtbouw", "scheepswerf", "scheepswerven", "scheepselektra", "scheepsinstallatie", "scheepsinstallaties",
        "binnenvaartschepen", "droogladingschepen", "passagiersschepen", "tankvaart", "shipbuilding", "shipyard",
        "marine electrical"],
    1: ["schip", "schepen", "jacht", "jachten", "nautisch", "nautische", "marine", "offshore", "aan boord",
        "stuurhuis", "stuurhuizen", "tankdek", "lloyd's register", "bureau veritas"],
}
MARINE_MIN_SIGNAL = 6     # weighted score (each term capped at 3 hits, like the customer-type terms)
MARINE_MIN_TERMS = 2      # distinct terms: one isolated word is never enough


def marine_evidence(lower_text: str) -> list[str]:
    """Matched marine/shipping terms with counts (stored at enrichment, re-scored without re-fetching)."""
    return _weighted_hits(lower_text, MARINE_TERMS)[1]


def marine_b2b(evidence: list[str]) -> tuple[bool, int]:
    """Strong marine/shipping evidence: score >= MARINE_MIN_SIGNAL, >= MARINE_MIN_TERMS distinct terms and at
    least one sector term (weight 3). Returns (strong, weighted score)."""
    weights = {t: w for w, terms in MARINE_TERMS.items() for t in terms}
    score, strong_term = 0, False
    for item in evidence:
        term, _, count = item.rpartition("×")
        w = weights.get(term, 0)
        score += w * min(int(count or 1), 3)
        strong_term |= w == 3
    return strong_term and len(evidence) >= MARINE_MIN_TERMS and score >= MARINE_MIN_SIGNAL, score


# Explicit statements that a business serves BOTH private and business customers.
DUAL_AUDIENCE_PATTERNS = [
    r"particulier(?:en)?\s*(?:en|&|of|als|,)\s*(?:ook\s+)?(?:voor\s+)?(?:bedrijven|bedrijf|zakelijk\w*|ondernemers)",
    r"(?:bedrijven|zakelijk\w*|ondernemers)\s*(?:en|&|of|,)\s*(?:voor\s+)?particulier(?:en)?",
]
# Business-audience signals (separate pages/sections for business, VvE, contractor customers).
BUSINESS_AUDIENCE_PATTERNS = {
    "voor bedrijven": r"(?:voor|elektricien voor|installateur voor|loodgieter voor)\s+bedrijven",
    "voor VvE": r"(?:voor\s+)?vve'?s?\b",
    "voor aannemers": r"(?:voor|elektricien voor|installateur voor)\s+aannemers",
    "zakelijke klanten/markt": r"zakelijke?\s+(?:klanten|markt|dienstverlening)",
    "woningcorporaties": r"woningcorporaties",
}


def dual_audience(lower_text: str) -> list[str]:
    """Evidence that the site addresses business customers next to homeowners.
    Mixed when: one explicit 'particulieren en bedrijven'-type statement, or 2+ distinct business-audience signals."""
    explicit = [m.group(0) for pat in DUAL_AUDIENCE_PATTERNS for m in [re.search(pat, lower_text)] if m]
    signals = [label for label, pat in BUSINESS_AUDIENCE_PATTERNS.items() if re.search(pat, lower_text)]
    if explicit or len(signals) >= 2:
        return explicit[:2] + signals
    return []
