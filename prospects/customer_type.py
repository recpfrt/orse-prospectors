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
