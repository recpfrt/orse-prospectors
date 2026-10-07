"""Deterministic qualification and queue assignment. No AI.

fit_score (0-100): is this a good LeadFlow fit? It deliberately EXCLUDES legal form, so a strong
homeowner business whose B.V. status is not visible on its website can still reach the KvK check.

Queues (CEO policy, 2026-10-06; email ONLY for confirmed B.V.):
  READY        confirmed B.V. (strong website evidence or manual KvK verification) + homeowner fit
               + public role email + fit >= FIT_THRESHOLD
  KVK_CHECK    homeowner fit + public role email + fit >= FIT_THRESHOLD, legal form unknown or
               B.V. evidence ambiguous -> CEO verifies on kvk.nl by hand
  MANUAL_REVIEW relevant business, not eligible for cold email (not B.V., no role email, mixed clientele)
  REJECTED     any exclusion, or fit below threshold. Every rejection carries an explicit reason.
  CONTACTED    already in outreach (CONTACTED or later status): never shown as a new prospect.
  DO_NOT_CONTACT  outreach status DO_NOT_CONTACT.

Legal-form provenance (legal_form_status), never upgraded beyond what actually happened:
  KVK_VERIFIED  B.V. checked by hand on the official KvK register (CEO review)
  WEBSITE_BV    B.V. stated on the company's own website, tied to its own name; NOT checked on KvK
  NOT_BV        V.O.F. / eenmanszaak on the website, or KvK check says not a B.V.
  UNKNOWN       no reliable legal-form evidence (incl. weak/ambiguous mentions, uncertain KvK checks)
"""

from .customer_type import decide_installer, dual_audience
from .places import landing_page_mismatch

FIT_THRESHOLD = 50
BV_BONUS = 10

# READY / KVK_CHECK need concrete residential evidence, not just "homeowner" by a narrow margin:
# at least MIN_RESIDENTIAL distinct terms from this list must appear on the site.
RESIDENTIAL_CORE = {
    "particulier", "particulieren", "uw woning", "uw huis", "thuis", "huiseigenaar", "huiseigenaren",
    "huurder", "woonhuis", "woningeigenaar", "cv-ketel", "cv ketel", "ketelonderhoud", "ketel vervangen",
    "lekkage", "verstopping", "ontstoppen", "storing", "boiler", "badkamer", "badkamerrenovatie", "toilet",
    "groepenkast", "meterkast", "warmtepomp", "radiator", "vloerverwarming", "geiser", "laadpaal thuis",
}
MIN_RESIDENTIAL = 2
# A mixed-audience business still passes when homeowner work clearly dominates (Batch 2 diagnosis).
MIXED_MIN_HOMEOWNER_RATIO = 3      # homeowner_signal >= 3 x business_signal (inclusive)
MIXED_MIN_RESIDENTIAL = 3          # and at least 3 distinct residential terms

READY, KVK_CHECK, MANUAL_REVIEW, REJECTED = "READY", "KVK_CHECK", "MANUAL_REVIEW", "REJECTED"


def _exclusions(p: dict) -> list[str]:
    if p.get("kvk_genuine_installer") == "no":
        return ["CEO KvK review: not a genuine local installer" + (f" ({p['kvk_review_note']})" if p.get("kvk_review_note") else "")]
    if p.get("duplicate_of"):
        return [f"duplicate business ({p['duplicate_of']})"]
    if p.get("robots_blocked"):
        return ["robots.txt or bot protection disallows fetching"]
    if p.get("fetch_status") == 429:
        return ["rate-limited by the website (HTTP 429); retry after cool-down"]
    if p.get("fetch_status") != 200:
        return [f"website not reachable (status {p.get('fetch_status')})"]

    out = []
    if not p.get("is_dutch"):
        out.append("website not in Dutch")
    if "trade_signal" in p:
        ev = p.get("installer_evidence", {})
        ok, why = decide_installer(p["trade_signal"], ev.get("unrelated", []), p.get("domain") or "")
        if not ok:
            out.append(f"not an installation business ({why}; trade {ev.get('trade', [])[:3]}, "
                       f"unrelated {ev.get('unrelated', [])[:3]})")
    elif p.get("is_installer") is False:
        out.append("not an installation business")
    if p.get("probable_chain") or (p.get("branches") or 0) > 1:
        out.append("chain / multiple branches")
    if p.get("team_signal") == "large":
        out.append(f"large company ({p.get('team_count')} staff)")
    if p.get("booking_tools"):
        out.append("already uses an online booking tool: " + ", ".join(p["booking_tools"]))
    mismatch = landing_page_mismatch(p.get("domain") or "", p.get("city"))
    if mismatch:
        out.append(mismatch)
    if (p.get("network_kvk_count") or 0) >= 2:
        out.append(f"network: same KvK number on {p['network_kvk_count']} websites")
    if len(p.get("broker_markers") or []) >= 2:
        out.append("lead-broker / referral network (" + ", ".join(p["broker_markers"][:3]) + ")")
    if len(p.get("franchise_markers") or []) >= 2:
        out.append("franchise / network (" + ", ".join(p["franchise_markers"][:3]) + ")")
    if (p.get("town_link_count") or 0) >= 10:
        out.append(f"network of regional landing pages ({p['town_link_count']} town links)")
    if len(p.get("webshop_markers") or []) >= 2:
        out.append("webshop / retailer (" + ", ".join(p["webshop_markers"][:3]) + ")")
    if p.get("source") == "commoncrawl" and not (p.get("nl_address_found") or p.get("kvk_number")
                                                 or p.get("nl_phone_found")):
        out.append("no Dutch business presence on website (no postcode, KvK number or Dutch phone number)")
    if p.get("customer_type") == "business":
        ev = ", ".join(p.get("customer_type_evidence", {}).get("business", [])[:4])
        out.append(f"serves mainly commercial/industrial clients ({ev})")
    return out


def _fit(p: dict) -> tuple[int, list[str]]:
    total, reasons = 0, []
    if not p.get("online_booking"):
        total += 30
        reasons.append("+30 no online booking (phone/form only)")
    else:
        reasons.append("+0 site suggests online planning: " + ", ".join(p.get("booking_phrases", [])))
    ctype = p.get("customer_type")
    if ctype == "homeowner":
        total += 20
        ev = ", ".join(p.get("customer_type_evidence", {}).get("homeowner", [])[:4])
        reasons.append(f"+20 serves homeowners ({ev})")
    elif ctype == "mixed":
        total += 5
        reasons.append("+5 mixed homeowner/business clientele")
    else:
        reasons.append("+0 customer type unclear from website")
    if p.get("team_signal") == "small":
        total += 20
        reasons.append("+20 small team signal")
    elif p.get("team_signal") == "medium":
        total += 10
        reasons.append("+10 medium team signal")
    if p.get("emergency_service"):
        total += 15
        reasons.append("+15 offers emergency/24-7 service")
    q = p.get("website_quality")
    if q is not None and 1 <= q <= 2:
        total += 10
        reasons.append("+10 decent but dated website")
    elif q == 3:
        total += 5
        reasons.append("+5 modern website")
    if p.get("public_email"):
        total += 10
        reasons.append("+10 public role email on own site")
    if p.get("kvk_number"):
        total += 5
        reasons.append("+5 KvK number published")
    return min(total, 100), reasons


def residential_terms(p: dict) -> list[str]:
    found = [e.rpartition("×")[0] for e in p.get("customer_type_evidence", {}).get("homeowner", [])]
    return sorted({t for t in found if t in RESIDENTIAL_CORE})


def clear_homeowner(p: dict) -> tuple[bool, str]:
    """Homeowner classification AND concrete residential evidence AND commercial signals not close behind."""
    if p.get("customer_type") != "homeowner":
        return False, f"customer type '{p.get('customer_type')}' (not clearly homeowner)"
    if p.get("kvk_homeowner_ok") == "no":
        return False, "CEO review: homeowner fit not reasonable"
    terms = residential_terms(p)
    if len(terms) < MIN_RESIDENTIAL:
        return False, f"homeowner evidence too thin ({', '.join(terms) or 'no core residential terms'})"
    home, biz = p.get("homeowner_signal") or 0, p.get("business_signal") or 0
    if biz and biz * 2 > home:
        return False, f"commercial signals too strong (homeowner {home} vs business {biz})"
    dual = dual_audience_evidence(p)
    if dual:
        if home >= MIXED_MIN_HOMEOWNER_RATIO * biz and len(terms) >= MIXED_MIN_RESIDENTIAL:
            return True, f"{', '.join(terms[:4])}; also serves business customers ({', '.join(dual[:3])})"
        return False, f"mixed audience: serves business customers too ({', '.join(dual[:3])})"
    return True, ", ".join(terms[:4])


def dual_audience_evidence(p: dict) -> list[str]:
    """Stored evidence from enrichment (all fetched pages); older records fall back to the homepage excerpt."""
    if "dual_audience_evidence" in p:
        return p["dual_audience_evidence"] or []
    return dual_audience((p.get("homepage_excerpt") or "").lower())


def _registrable(host: str | None) -> str:
    host = (host or "").lower().split(":")[0].strip(".")
    return host[4:] if host.startswith("www.") else host


def domain_guard(p: dict) -> str | None:
    """Reason why automatic READY is unsafe: the site redirects to another domain, or the public role email
    is on a different domain than the business website. None when consistent (or nothing to compare)."""
    found = _registrable(p.get("domain"))
    if not found:
        return None                      # no website domain known: nothing to compare against
    final_url = p.get("final_url") or ""
    final = _registrable(final_url.split("/")[2]) if final_url.count("/") >= 2 else found
    reasons = []
    if final and found and final != found:
        reasons.append(f"website redirects from {found} to {final} (different domain/entity)")
    email = (p.get("public_email") or "").lower()
    if "@" in email:
        email_domain = _registrable(email.split("@", 1)[1])
        if email_domain not in {found, final}:
            reasons.append(f"public email domain {email_domain} differs from website domain {final or found}")
    return "; ".join(reasons) or None


def bv_status(p: dict) -> str:
    """'confirmed' | 'ambiguous' | 'unknown' | 'not_bv'"""
    review = p.get("kvk_review")
    if review == "bv":
        if p.get("kvk_entity_match") == "no" or p.get("kvk_active") == "no":
            return "review_problem"
        return "confirmed"
    if review == "not_bv":
        return "not_bv"
    if review == "uncertain":
        return "review_uncertain"
    if p.get("legal_form") == "bv":
        # A missing confidence is never assumed to be strong.
        return "confirmed" if p.get("legal_form_confidence") == "strong" else "ambiguous"
    if p.get("legal_form") in ("vof", "eenmanszaak", "other"):
        return "not_bv"
    return "unknown"


KVK_VERIFIED, WEBSITE_BV, NOT_BV, UNKNOWN = "KVK_VERIFIED", "WEBSITE_BV", "NOT_BV", "UNKNOWN"
CONTACTED, DO_NOT_CONTACT = "CONTACTED", "DO_NOT_CONTACT"


def legal_form_status(p: dict) -> str:
    bv = bv_status(p)
    if p.get("kvk_review") == "bv":
        return KVK_VERIFIED if bv == "confirmed" else UNKNOWN   # entity mismatch / inactive: not verified
    if bv == "not_bv":
        return NOT_BV
    if bv == "confirmed":
        return WEBSITE_BV                                       # strong website evidence, no KvK check
    return UNKNOWN


def missing_bv_evidence(p: dict) -> str:
    if p.get("legal_form") == "bv":
        return f"B.V. mentioned but not clearly tied to this business: \"{p.get('legal_form_evidence')}\""
    if str(p.get("legal_form_evidence") or "").startswith("conflicting"):
        return f"conflicting legal forms on site: {p['legal_form_evidence']}"
    kvk = p.get("kvk_number")
    return "no legal form shown on website" + (f"; KvK number on site: {kvk}" if kvk else "; no KvK number on site")


def score(p: dict) -> dict:
    excluded = _exclusions(p)
    fit, reasons = (0, []) if excluded else _fit(p)
    if not excluded and fit < FIT_THRESHOLD:
        excluded.append(f"fit score {fit} below threshold {FIT_THRESHOLD}")

    bv = bv_status(p)
    homeowner, homeowner_why = clear_homeowner(p)
    email = bool(p.get("public_email"))
    guard = domain_guard(p)
    # A CEO KvK check that confirmed both B.V. status and the entity match clears the domain guard.
    entity_verified = p.get("kvk_review") == "bv" and p.get("kvk_entity_match") == "yes"

    if excluded:
        queue, why = REJECTED, "; ".join(excluded)
    elif bv == "confirmed" and homeowner and email and guard and not entity_verified:
        queue, why = KVK_CHECK, (f"domain guard: {guard} -> verify the legal entity on KvK before outreach "
                                 f"(clear homeowner fit, public role email, fit {fit})")
    elif bv == "confirmed" and homeowner and email:
        via = ("manual KvK verification" if p.get("kvk_review") == "bv"
               else "website evidence — WEBSITE_BV, not verified on KvK")
        queue, why = READY, f"B.V. confirmed ({via}), clear homeowner fit ({homeowner_why}), public role email, fit {fit}"
    elif homeowner and email and bv in ("unknown", "ambiguous"):
        queue, why = KVK_CHECK, f"clear homeowner fit ({homeowner_why}) + public role email; " + missing_bv_evidence(p)
    else:
        gaps = []
        if bv == "not_bv":
            gaps.append("not a B.V. (" + ("KvK check" if p.get("kvk_review") else str(p.get("legal_form"))) + ")")
        if bv == "review_uncertain":
            gaps.append("KvK check uncertain")
        if bv == "review_problem":
            gaps.append("KvK check: legal entity does not match or business inactive")
        if not email:
            gaps.append("no public role email")
        if not homeowner:
            gaps.append(homeowner_why)
        queue, why = MANUAL_REVIEW, "not eligible for cold email: " + ", ".join(gaps or ["insufficient evidence"])

    # Per-business manual hold (CEO decision): applies ONLY to prospects carrying a hold.
    # It can only lower READY / KVK_CHECK to MANUAL_REVIEW; a rejected prospect stays rejected.
    # General scoring (including how 'onzeker' is treated) is unaffected.
    hold = p.get("manual_hold_reason")
    if hold and queue in (READY, KVK_CHECK):
        why = f"manual hold (CEO): {hold} | would otherwise be {queue}: {why}"
        queue = MANUAL_REVIEW
    elif hold:
        why = f"{why} | manual hold (CEO): {hold}"

    # Outreach tracking: a company already contacted (or marked do-not-contact) is never a new prospect.
    contact = p.get("outreach_status")
    if contact and contact != "NOT_CONTACTED":
        why = f"already in outreach ({contact}; {p.get('outreach_match')}) — not a new prospect | would otherwise be {queue}: {why}"
        queue = DO_NOT_CONTACT if contact == "DO_NOT_CONTACT" else CONTACTED

    qualification = 0 if excluded else min(fit + (BV_BONUS if bv == "confirmed" else 0), 100)
    return {
        **p,
        "queue": queue,
        "queue_reason": why,
        "bv_status": bv,
        "legal_form_status": legal_form_status(p),
        "fit_score": fit,
        "qualification_score": qualification,
        "score_reasons": reasons + ([f"+{BV_BONUS} B.V. confirmed"] if bv == "confirmed" and not excluded else []),
        "disqualify_reasons": excluded,
        "domain_guard": guard,
        # compatibility with drafts.py and earlier reports
        "status": {READY: "qualified", KVK_CHECK: "kvk_check", MANUAL_REVIEW: "manual_review",
                   CONTACTED: "contacted", DO_NOT_CONTACT: "do_not_contact"}.get(queue, "disqualified"),
        "outreach_channel": "email" if queue == READY else ("none" if queue == REJECTED else "phone_or_contact_form"),
        "strong_prospect": queue == READY,
    }
