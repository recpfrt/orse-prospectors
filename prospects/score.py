"""Deterministic qualification and queue assignment. No AI.

fit_score (0-100): is this a good LeadFlow fit? It deliberately EXCLUDES legal form, so a strong
homeowner business whose B.V. status is not visible on its website can still reach the KvK check.

Queues (CEO policy, 2026-10-06; email ONLY for confirmed B.V.):
  READY        confirmed B.V. (strong website evidence or manual KvK verification) + homeowner fit
               + public role email + fit >= FIT_THRESHOLD
  KVK_CHECK    homeowner fit + public role email + fit >= FIT_THRESHOLD, legal form unknown or
               B.V. evidence ambiguous -> CEO verifies on kvk.nl by hand
  MANUAL_PHONE relevant business, not eligible for cold email (not B.V., no role email, mixed clientele)
  REJECTED     any exclusion, or fit below threshold. Every rejection carries an explicit reason.
"""

from .customer_type import decide_installer
from .places import landing_page_mismatch

FIT_THRESHOLD = 50
BV_BONUS = 10

READY, KVK_CHECK, MANUAL_PHONE, REJECTED = "READY", "KVK_CHECK", "MANUAL_PHONE", "REJECTED"


def _exclusions(p: dict) -> list[str]:
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


def bv_status(p: dict) -> str:
    """'confirmed' | 'ambiguous' | 'unknown' | 'not_bv'"""
    review = p.get("kvk_review")
    if review == "bv":
        return "confirmed"
    if review == "not_bv":
        return "not_bv"
    if p.get("legal_form") == "bv":
        # A missing confidence is never assumed to be strong.
        return "confirmed" if p.get("legal_form_confidence") == "strong" else "ambiguous"
    if p.get("legal_form") in ("vof", "eenmanszaak", "other"):
        return "not_bv"
    return "unknown"


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
    homeowner = p.get("customer_type") == "homeowner"
    email = bool(p.get("public_email"))

    if excluded:
        queue, why = REJECTED, "; ".join(excluded)
    elif bv == "confirmed" and homeowner and email:
        via = "manual KvK verification" if p.get("kvk_review") == "bv" else "website evidence"
        queue, why = READY, f"B.V. confirmed ({via}), homeowner fit, public role email, fit {fit}"
    elif homeowner and email and bv in ("unknown", "ambiguous"):
        queue, why = KVK_CHECK, "homeowner fit + public role email; " + missing_bv_evidence(p)
    else:
        gaps = []
        if bv == "not_bv":
            gaps.append("not a B.V. (" + ("KvK check" if p.get("kvk_review") else str(p.get("legal_form"))) + ")")
        if not email:
            gaps.append("no public role email")
        if not homeowner:
            gaps.append(f"customer type '{p.get('customer_type')}' (not clearly homeowner)")
        queue, why = MANUAL_PHONE, "relevant business, not eligible for cold email: " + ", ".join(gaps or ["legal form unknown"])

    qualification = 0 if excluded else min(fit + (BV_BONUS if bv == "confirmed" else 0), 100)
    return {
        **p,
        "queue": queue,
        "queue_reason": why,
        "bv_status": bv,
        "fit_score": fit,
        "qualification_score": qualification,
        "score_reasons": reasons + ([f"+{BV_BONUS} B.V. confirmed"] if bv == "confirmed" and not excluded else []),
        "disqualify_reasons": excluded,
        # compatibility with drafts.py and earlier reports
        "status": {READY: "qualified", KVK_CHECK: "kvk_check", MANUAL_PHONE: "manual_review"}.get(queue, "disqualified"),
        "outreach_channel": "email" if queue == READY else ("none" if queue == REJECTED else "phone_or_contact_form"),
        "strong_prospect": queue == READY,
    }
