"""Deterministic qualification. No AI.

Two separate decisions:
1. score (0-100): is this business a good fit for LeadFlow?
2. outreach_channel: may we email them? Email ONLY for B.V. businesses (CEO policy,
   2026-10-06). Non-B.V. fits go to manual_review / phone_or_contact_form, regardless of score.
"""

from . import config


def score(p: dict) -> dict:
    reasons: list[str] = []
    excluded: list[str] = []

    # ---- hard exclusions --------------------------------------------------
    if p.get("robots_blocked"):
        excluded.append("robots.txt disallows fetching")
    elif p.get("fetch_status") != 200:
        excluded.append(f"website not reachable (status {p.get('fetch_status')})")
    else:
        if not p.get("is_dutch"):
            excluded.append("website not in Dutch")
        if p.get("probable_chain") or (p.get("branches") or 0) > 1:
            excluded.append("chain / multiple branches")
        if p.get("team_signal") == "large":
            excluded.append(f"large company ({p.get('team_count')} staff)")
        if p.get("booking_tools"):
            excluded.append("already uses an online booking tool: " + ", ".join(p["booking_tools"]))
        if p.get("customer_type") == "business":
            ev = ", ".join(p.get("customer_type_evidence", {}).get("business", [])[:4])
            excluded.append(f"serves mainly commercial/industrial clients ({ev})")

    total = 0
    if not excluded:
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
        if p.get("legal_form") == "bv":
            total += 10
            reasons.append("+10 B.V.")
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

    total = min(total, 100)
    fit = not excluded and total >= config.QUALIFY_THRESHOLD

    if excluded:
        status, channel = "disqualified", "none"
    elif not fit:
        status, channel = "disqualified", "none"
        excluded.append(f"score {total} below threshold {config.QUALIFY_THRESHOLD}")
    elif p.get("legal_form") == "bv" and p.get("public_email"):
        status, channel = "qualified", "email"
    elif p.get("legal_form") == "bv":
        status, channel = "manual_review", "phone_or_contact_form"
        reasons.append("B.V. but no public role email -> phone/contact form")
    else:
        status, channel = "manual_review", "phone_or_contact_form"
        reasons.append(f"legal form '{p.get('legal_form')}' is not B.V. -> no email (policy)")

    strong = channel == "email" and p.get("customer_type") == "homeowner"
    return {**p, "strong_prospect": strong, "qualification_score": total, "score_reasons": reasons,
            "disqualify_reasons": excluded, "status": status, "outreach_channel": channel}
