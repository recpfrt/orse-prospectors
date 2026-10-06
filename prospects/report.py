"""Daily report and queue exports (Markdown + CSV, local files only)."""

import csv
import urllib.parse
from collections import Counter
from pathlib import Path

from .score import KVK_CHECK, MANUAL_PHONE, READY, REJECTED


def _ev(p: dict, key: str) -> str:
    return ", ".join(p.get("customer_type_evidence", {}).get(key, [])[:4]) or "-"


def bv_evidence_text(p: dict) -> str:
    if p.get("kvk_review") == "bv":
        return f"KvK verified by CEO ({p.get('kvk_review_number') or 'no number noted'})"
    if p.get("legal_form") == "bv":
        return f"[{p.get('legal_form_confidence')}] \"{p.get('legal_form_evidence')}\""
    return "none on website"


def kvk_search_url(p: dict) -> str:
    """Link for the CEO to open by hand. The pipeline never queries kvk.nl itself."""
    q = p.get("kvk_number") or short_name(p)
    return "https://www.kvk.nl/zoeken/?source=all&q=" + urllib.parse.quote(str(q))


def short_name(p: dict) -> str:
    """Core business name for a KvK search: text before the first separator, at most 4 words."""
    import re
    name = str(p.get("business_name") or p["domain"])
    name = re.split(r"\s[|\-–—:]\s|\s\|", name)[0]
    return " ".join(name.split()[:4])


def metrics(results: list[dict]) -> dict:
    q = Counter(p["queue"] for p in results)
    return {
        "Domains processed": len(results),
        "Websites reachable": sum(1 for p in results if p.get("fetch_status") == 200),
        "Unreachable (dead/refused)": sum(1 for p in results if p.get("fetch_status") not in (200, 429, -1)),
        "Blocked by robots.txt / bot protection": sum(1 for p in results if p.get("robots_blocked")),
        "Rate-limited (HTTP 429)": sum(1 for p in results if p.get("fetch_status") == 429),
        "Public business emails": sum(1 for p in results if p.get("public_email")),
        "Homeowner-fit": sum(1 for p in results if p.get("customer_type") == "homeowner"),
        "B.V. confirmed": sum(1 for p in results if p.get("bv_status") == "confirmed"),
        "KVK checks required": q[KVK_CHECK],
        "Ready for outreach": q[READY],
        "Manual-review / phone": q[MANUAL_PHONE],
        "Rejected": q[REJECTED],
        "Duplicates": sum(1 for p in results if p.get("duplicate_of")),
    }


def write_queue_csv(path: Path, rows: list[dict]) -> None:
    fields = ["business_name", "city", "website", "public_email", "service_category", "fit_score",
              "qualification_score", "customer_type", "homeowner_evidence", "bv_status", "bv_evidence",
              "kvk_number_on_site", "queue_reason", "kvk_search_url", "source", "domain"]
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for p in rows:
            w.writerow({
                "business_name": p.get("business_name"), "city": p.get("city"), "website": p.get("final_url") or p.get("website"),
                "public_email": p.get("public_email"), "service_category": p.get("service_category"),
                "fit_score": p.get("fit_score"), "qualification_score": p.get("qualification_score"),
                "customer_type": p.get("customer_type"), "homeowner_evidence": _ev(p, "homeowner"),
                "bv_status": p.get("bv_status"), "bv_evidence": bv_evidence_text(p),
                "kvk_number_on_site": p.get("kvk_number"), "queue_reason": p.get("queue_reason"),
                "kvk_search_url": kvk_search_url(p), "source": p.get("source"), "domain": p["domain"],
            })


def prospect_block(p: dict) -> str:
    return (
        f"### {p.get('business_name')} — {p.get('city') or '?'}  (fit {p.get('fit_score')}, score {p.get('qualification_score')})\n"
        f"- Website: {p.get('final_url') or p.get('website')}\n"
        f"- Email (public role address): `{p.get('public_email')}`\n"
        f"- Category: {p.get('service_category')} · Source: {p.get('source')}\n"
        f"- Homeowner evidence: {_ev(p, 'homeowner')} (business: {_ev(p, 'business')})\n"
        f"- B.V. evidence: {bv_evidence_text(p)}\n"
        f"- Why: {p.get('queue_reason')}\n"
    )


def write_daily(out_dir: Path, results: list[dict], meta: dict) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    by_queue = {q: sorted([p for p in results if p["queue"] == q], key=lambda p: -(p.get("fit_score") or 0))
                for q in (READY, KVK_CHECK, MANUAL_PHONE, REJECTED)}
    for q, rows in by_queue.items():
        write_queue_csv(out_dir / f"{q.lower()}.csv", rows)

    m = metrics(results)
    lines = [f"# OrseBusiness Daily Prospecting Report — {meta.get('date')}", "",
             "Local dry run. Nothing was sent, drafted or stored outside this machine.", "",
             f"Domain pool: {meta.get('pool_size')} · skipped (processed < 30 days ago or cooling down): "
             f"{meta.get('skipped_recently_processed')}", "", "| Metric | Count |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in m.items()]
    lines += ["", "## Ready for outreach (B.V. + homeowner + role email)", ""]
    lines += [prospect_block(p) for p in by_queue[READY]] or ["_none today_", ""]
    lines += ["## KVK CHECK — verify by hand on kvk.nl", ""]
    for p in by_queue[KVK_CHECK]:
        lines.append(prospect_block(p) + f"- KvK lookup (open manually): {kvk_search_url(p)}\n"
                     f"- Mark result: `python run.py kvk mark {p['domain']} --bv --kvk <nummer>` "
                     f"or `--not-bv`\n")
    if not by_queue[KVK_CHECK]:
        lines += ["_none today_", ""]
    lines += ["## Manual / phone (not eligible for cold email)", ""]
    lines += [f"- {p.get('business_name')} ({p.get('city') or '?'}) {p.get('final_url') or p.get('website')} — {p.get('queue_reason')}"
              for p in by_queue[MANUAL_PHONE]] or ["_none_"]
    lines += ["", "## Rejection reasons", ""]
    reasons = Counter(r.split("(")[0].split(":")[0].strip() for p in by_queue[REJECTED] for r in p["disqualify_reasons"][:1])
    lines += [f"- {k}: {v}" for k, v in reasons.most_common()]
    path = out_dir / "report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
