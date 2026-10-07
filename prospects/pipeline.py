"""Daily local prospecting pipeline. Writes only to data/state.sqlite and data/out/. No outreach."""

import datetime as dt
import json
import sys

from . import collect, enrich, outreach, state
from .customer_type import installer_check
from .score import KVK_CHECK, MANUAL_REVIEW, READY, UNKNOWN, KVK_VERIFIED, WEBSITE_BV, legal_form_status, score


HOLD_KEYS = ("manual_hold_reason", "manual_hold_at")
OUTREACH_KEYS = ("outreach_status", "outreach_match")


def apply_review(con, p: dict) -> dict:
    """Attach the CEO's manual decisions (KvK review, per-business hold) from local state."""
    p = {k: v for k, v in p.items() if k not in HOLD_KEYS + OUTREACH_KEYS}   # no stale hold/outreach data
    contact = outreach.contact_match(con, p)
    if contact:
        p = {**p, "outreach_status": contact[0]["status"], "outreach_match": contact[1]}
    hold = state.active_hold(con, p["domain"])
    if hold:
        p = {**p, "manual_hold_reason": hold["reason"], "manual_hold_at": hold["created_at"]}
    review = state.kvk_review(con, p["domain"])
    if review:
        p = {**p, "kvk_review": review["result"], "kvk_review_number": review["kvk_number"],
             "kvk_review_note": review["note"], "kvk_entity_match": review["entity_match"],
             "kvk_active": review["active"], "kvk_genuine_installer": review["genuine_installer"],
             "kvk_homeowner_ok": review["homeowner_ok"]}
    return p


def process(con, candidate: dict) -> dict:
    """Enrich -> record attempt -> duplicate check -> score -> save."""
    p = enrich.enrich(candidate)
    state.record_attempt(con, p["domain"], p.get("source", "unknown"), p.get("fetch_status"))
    if p.get("fetch_status") == 200:
        dup = state.find_duplicate(con, p)
        if dup:
            p["duplicate_of"] = dup
    p = score(apply_review(con, p))
    state.save_prospect(con, p)
    con.commit()
    return p


def select_batch(con, pool: list[dict], batch: int) -> tuple[list[dict], int]:
    chosen, not_due = [], 0
    for c in pool:
        if len(chosen) >= batch:
            break
        if state.is_due(con, c["domain"]):
            chosen.append(c)
        else:
            not_due += 1
    return chosen, not_due


def run_daily(con, cc_tsv: str, batch: int, seed: int) -> tuple[list[dict], dict]:
    pool = collect.collect_commoncrawl(cc_tsv, sample=10**9, seed=seed, exclude_domains=set())
    blocked_domains, blocked_emails = _red_list()
    blocked_domains = blocked_domains | outreach.excluded_domains(con)   # contacted companies: never re-selected
    pool = [c for c in pool if c["domain"] not in blocked_domains]
    chosen, skipped_recent = select_batch(con, pool, batch)
    started = dt.datetime.now()
    results = []
    for i, c in enumerate(chosen, 1):
        print(f"[daily] {i}/{len(chosen)} {c['domain']}", file=sys.stderr)
        p = process(con, c)
        if (p.get("public_email") or "").lower() in blocked_emails:
            p = score({**p, "duplicate_of": "email on do-not-contact list"})
            state.save_prospect(con, p)
            con.commit()
        results.append(p)
    network_sweep(con)
    results = [_reload(con, r["domain"]) for r in results]  # pick up network flags set by the sweep
    elapsed = (dt.datetime.now() - started).total_seconds()
    attempted = {r["domain"] for r in con.execute("select domain from domains")}
    meta = {"pool_size": len(pool), "skipped_recently_processed": skipped_recent,
            "date": dt.date.today().isoformat(), "started": started.isoformat(timespec="seconds"),
            "elapsed_min": round(elapsed / 60, 1),
            "sec_per_domain": round(elapsed / max(len(results), 1), 1),
            "pool_remaining": sum(1 for c in pool if c["domain"] not in attempted)}
    return results, meta


def _red_list() -> tuple[set[str], set[str]]:
    import csv
    from pathlib import Path
    path = state.PROJECT_ROOT / "data" / "do_not_contact.csv"
    domains, emails = set(), set()
    if path.exists():
        for row in csv.DictReader(path.open(encoding="utf-8")):
            if row.get("domain"):
                domains.add(row["domain"].strip().lower())
            if row.get("email"):
                emails.add(row["email"].strip().lower())
    return domains, emails


def import_previous(con, rows: list[dict]) -> int:
    """Seed state with results from earlier dry runs so those domains are not re-fetched within 30 days.
    Older rows lack some safeguard fields; the installer check is recomputed from the stored homepage text."""
    n = 0
    for r in rows:
        r = {k: v for k, v in r.items() if k not in ("queue", "queue_reason", "status", "outreach_channel",
                                                    "strong_prospect", "disqualify_reasons", "score_reasons")}
        if r.get("legal_form") == "bv" and not r.get("legal_form_confidence"):
            r["legal_form_confidence"] = enrich.bv_evidence_strength(
                r.get("legal_form_evidence"), r.get("business_name"), r["domain"])
        # Legacy rows (enriched before the installer check existed) have no trade_signal; only those are
        # recomputed. Current-format rows keep their full multi-page evidence (is_installer is obsolete).
        if r.get("fetch_status") == 200 and "trade_signal" not in r:
            r.update(installer_check((r.get("homepage_excerpt") or "").lower()))
            r["safeguards_partial"] = True
        state.record_attempt(con, r["domain"], r.get("source", "imported"), r.get("fetch_status"))
        if r.get("fetch_status") == 200:
            dup = state.find_duplicate(con, r)
            if dup:
                r["duplicate_of"] = dup
        state.save_prospect(con, score(apply_review(con, r)))
        n += 1
    con.commit()
    return n


STRIP = ("queue", "queue_reason", "status", "outreach_channel", "strong_prospect", "disqualify_reasons", "score_reasons")


def network_sweep(con) -> int:
    """A KvK number shown on 2+ websites = one company running several sites (broker/landing-page network).
    Every domain in that group, including the first one seen, is flagged and re-scored."""
    groups = con.execute(
        "select json_extract(data, '$.kvk_number') k, count(*) n from prospects "
        "where k is not null group by k having n > 1").fetchall()
    flagged = 0
    for g in groups:
        for row in con.execute("select data from prospects where json_extract(data, '$.kvk_number') = ?", (g["k"],)).fetchall():
            p = {k: v for k, v in json.loads(row["data"]).items() if k not in STRIP}
            p["network_kvk_count"] = g["n"]
            state.save_prospect(con, score(apply_review(con, p)))
            flagged += 1
    con.commit()
    return flagged


def legal_recheck_candidates(con, force: bool = False) -> list[dict]:
    """KVK_CHECK prospects, plus MANUAL_REVIEW prospects with a public email, whose legal form is UNKNOWN and
    that have no CEO KvK review, no active hold, no outreach record, and no rate-limit history."""
    out = []
    for p in state.load_prospects(con):
        if p.get("queue") not in (KVK_CHECK, MANUAL_REVIEW) or p.get("kvk_review"):
            continue
        if p["queue"] == MANUAL_REVIEW and not p.get("public_email"):
            continue
        if p.get("fetch_status") != 200 or legal_form_status(p) != UNKNOWN:
            continue
        if not enrich.needs_legal_pages(p) or (p.get("legal_recheck_at") and not force):
            continue
        if state.active_hold(con, p["domain"]) or outreach.contact_match(con, p):
            continue
        d = con.execute("select skip_reason, count_429 from domains where domain = ?", (p["domain"],)).fetchone()
        if d and (d["skip_reason"] or d["count_429"]):
            continue
        out.append(p)
    return sorted(out, key=lambda p: (p["queue"] != KVK_CHECK, -(p.get("fit_score") or 0), p["domain"]))


def legal_recheck(con, p: dict, apply: bool = True) -> dict:
    """Fetch legal pages for one stored prospect, merge the evidence, re-score. Saves only when apply=True."""
    fetched = enrich.legal_recheck(p)
    status = fetched["status"]
    new = {k: v for k, v in p.items() if k not in STRIP}
    new["legal_recheck_status"] = status
    new["legal_recheck_at"] = state.now()
    if fetched["legal"] is not None:
        new = enrich.apply_legal_pages(new, fetched["legal"])
    new = score(apply_review(con, new))
    if apply:
        if status == 429:
            state.record_attempt(con, p["domain"], p.get("source", "unknown"), 429)
        state.save_prospect(con, new)
        con.commit()
    return {"domain": p["domain"], "http_status": status,
            "before_queue": p.get("queue"), "after_queue": new["queue"],
            "before_legal_status": legal_form_status(p), "after_legal_status": new["legal_form_status"],
            "legal_form": new.get("legal_form"), "evidence": new.get("legal_form_evidence"),
            "source": new.get("legal_form_source"), "source_url": new.get("legal_form_source_url"),
            "legal_pages_checked": new.get("legal_pages_checked") or [],
            "kvk_number": new.get("kvk_number"), "business_name": new.get("business_name"),
            "public_email": new.get("public_email")}


LEGAL_STATUS_RANK = {KVK_VERIFIED: 0, WEBSITE_BV: 1}


def prospect_list(con) -> list[dict]:
    """New-prospect recommendations: READY only, never contacted, one row per business.
    KVK_VERIFIED first, then WEBSITE_BV; deduplicated by domain, public email, KvK number and name + town."""
    rows = [p for p in state.load_prospects(con, READY)
            if not p.get("duplicate_of") and outreach.contact_match(con, p) is None]
    rows.sort(key=lambda p: (LEGAL_STATUS_RANK.get(legal_form_status(p), 9),
                             -(p.get("qualification_score") or 0), p["domain"]))
    seen, out = set(), []
    for p in rows:
        ids = {("domain", p["domain"])}
        if p.get("public_email"):
            ids.add(("email", p["public_email"].lower()))
        for k in (p.get("kvk_number"), p.get("kvk_review_number")):
            if k:
                ids.add(("kvk", k))
        key = state.business_key(p.get("business_name"), p.get("city"))
        if key:
            ids.add(("business", key))
        if ids & seen:
            continue
        seen |= ids
        out.append(p)
    return out


def _reload(con, domain: str) -> dict:
    return json.loads(con.execute("select data from prospects where domain = ?", (domain,)).fetchone()["data"])
