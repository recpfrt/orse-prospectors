"""Daily local prospecting pipeline. Writes only to data/state.sqlite and data/out/. No outreach."""

import datetime as dt
import json
import sys

from . import collect, enrich, state
from .customer_type import installer_check
from .score import score


def apply_review(con, p: dict) -> dict:
    review = state.kvk_review(con, p["domain"])
    if review:
        p = {**p, "kvk_review": review["result"], "kvk_review_number": review["kvk_number"],
             "kvk_review_note": review["note"]}
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
    pool = [c for c in pool if c["domain"] not in blocked_domains]
    chosen, skipped_recent = select_batch(con, pool, batch)
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
    meta = {"pool_size": len(pool), "skipped_recently_processed": skipped_recent,
            "date": dt.date.today().isoformat()}
    return results, meta


def _red_list() -> tuple[set[str], set[str]]:
    import csv
    from pathlib import Path
    path = Path(state.DB_PATH).parent / "do_not_contact.csv"
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
        if r.get("fetch_status") == 200 and "is_installer" not in r:
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


def _reload(con, domain: str) -> dict:
    return json.loads(con.execute("select data from prospects where domain = ?", (domain,)).fetchone()["data"])
