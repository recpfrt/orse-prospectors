"""OrseBusiness prospect pipeline — DRY-RUN tooling.

Writes only to ./data/out. No database writes, no Gmail, no email, no contact with prospects.

  python run.py dryrun --city Utrecht --pbf data/raw/utrecht-latest.osm.pbf
  python run.py drafts --scored data/out/<file>-scored.json --env ../leadflow/.env.local --count 5
"""

import argparse
import csv
import datetime
import json
import re
import sys
from collections import Counter
from pathlib import Path

from prospects import collect, drafts, enrich, pipeline, report, score, state

OUT = Path(__file__).parent / "data" / "out"
RED_LIST = Path(__file__).parent / "data" / "do_not_contact.csv"   # local stand-in until Supabase is approved

CSV_FIELDS = ["status", "outreach_channel", "strong_prospect", "qualification_score", "customer_type", "business_name", "city", "service_category",
              "website", "public_email", "email_source_url", "legal_form", "legal_form_evidence", "kvk_number", "online_booking",
              "emergency_service", "team_signal", "website_quality", "score_reasons", "disqualify_reasons"]


def load_red_list() -> tuple[set[str], set[str]]:
    domains, emails = set(), set()
    if RED_LIST.exists():
        for row in csv.DictReader(RED_LIST.open(encoding="utf-8")):
            if row.get("domain"):
                domains.add(row["domain"].strip().lower())
            if row.get("email"):
                emails.add(row["email"].strip().lower())
    return domains, emails


_OPEN_CONNECTIONS = []


def _con(args):
    """Open the state database chosen for this command: --db, else $ORSE_PROSPECTS_DB, else the real default."""
    con = state.connect(getattr(args, "db", None))
    _OPEN_CONNECTIONS.append(con)
    return con


def close_connections():
    """Close every connection opened by commands (called when the CLI exits; tests call it directly)."""
    while _OPEN_CONNECTIONS:
        _OPEN_CONNECTIONS.pop().close()


def cmd_dryrun(args):
    OUT.mkdir(parents=True, exist_ok=True)
    label = ("cc" if args.source == "commoncrawl" else
             "province" if args.city == "*" else args.city.lower())
    stamp = f"{datetime.date.today():%Y%m%d}-{label}"

    if args.source == "commoncrawl":
        known = set()
        for f in OUT.glob("*-scored.json"):
            known |= {r["domain"] for r in json.loads(f.read_text(encoding="utf-8"))}
        raw = collect.collect_commoncrawl(args.cc_tsv, args.sample, args.seed, set())
        source = "commoncrawl"
        cross_source_dupes = [c for c in raw if c["domain"] in known]
        raw = [c for c in raw if c["domain"] not in known]
        blocked_domains, blocked_emails = load_red_list()
        candidates, dedupe_stats = collect.dedupe(raw, blocked_domains, blocked_emails)
        dedupe_stats["duplicate_of_earlier_run"] = len(cross_source_dupes)
        return _enrich_score_report(args, stamp, source, raw, candidates, dedupe_stats)

    source = "overpass"
    try:
        if args.skip_overpass:
            raise RuntimeError("skipped by flag")
        raw = collect.collect_overpass(args.city)
    except RuntimeError as e:
        print(f"[collect] Overpass unavailable ({str(e)[:120]}) -> Geofabrik fallback", file=sys.stderr)
        if not args.pbf:
            sys.exit("No --pbf given for fallback.")
        source = "geofabrik"
        raw = collect.collect_geofabrik(args.pbf, None if args.city == "*" else args.city)
        if args.exclude_city:
            raw = [c for c in raw if (c["city"] or "").lower() != args.exclude_city.lower()]

    blocked_domains, blocked_emails = load_red_list()
    candidates, dedupe_stats = collect.dedupe(raw, blocked_domains, blocked_emails)
    for c in raw:
        c.setdefault("source", "osm" if source == "overpass" else "geofabrik")
    return _enrich_score_report(args, stamp, source, raw, candidates, dedupe_stats)


def _enrich_score_report(args, stamp, source, raw, candidates, dedupe_stats):
    candidates = candidates[: args.max_sites]
    print(f"[collect] source={source} raw={len(raw)} kept={len(candidates)} {dict(dedupe_stats)}", file=sys.stderr)

    enriched = []
    for i, c in enumerate(candidates, 1):
        print(f"[enrich] {i}/{len(candidates)} {c['domain']}", file=sys.stderr)
        enriched.append(enrich.enrich(c))

    scored = sorted((score.score(p) for p in enriched), key=lambda p: -p["qualification_score"])
    (OUT / f"{stamp}-scored.json").write_text(json.dumps(scored, ensure_ascii=False, indent=2), encoding="utf-8")
    with (OUT / f"{stamp}-scored.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for p in scored:
            w.writerow({**p, "score_reasons": "; ".join(p["score_reasons"]),
                        "disqualify_reasons": "; ".join(p["disqualify_reasons"])})

    stats = {
        "city": getattr(args, "city", None), "source": source, "raw_osm_entries": len(raw),
        "by_category_raw": dict(Counter(c["service_category"] for c in raw)),
        "dedupe": dict(dedupe_stats), "sites_enriched": len(enriched),
        "reachable": sum(1 for p in enriched if p["fetch_status"] == 200),
        "robots_blocked": sum(1 for p in enriched if p["robots_blocked"]),
        "with_public_role_email": sum(1 for p in enriched if p.get("public_email")),
        "legal_form": dict(Counter(p.get("legal_form", "n/a") for p in enriched)),
        "online_booking": sum(1 for p in enriched if p.get("online_booking")),
        "customer_type": dict(Counter(p.get("customer_type", "n/a") for p in enriched)),
        "probable_bv": sum(1 for p in enriched if p.get("legal_form") == "bv"),
        "status": dict(Counter(p["status"] for p in scored)),
        "strong_prospects": sum(1 for p in scored if p.get("strong_prospect")),
        "outreach_channel": dict(Counter(p["outreach_channel"] for p in scored)),
        "top_disqualify_reasons": Counter(re.sub(r"\(.*?\)|: .*", "", r).strip()
                                          for p in scored for r in p["disqualify_reasons"]).most_common(8),
    }
    (OUT / f"{stamp}-stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(stats, ensure_ascii=False, indent=2))


def read_env(path: str, key: str) -> str:
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        m = re.match(rf"\s*{key}\s*=\s*(.*)", line)
        if m:
            return m.group(1).strip().strip('"').strip("'")
    sys.exit(f"{key} not found in {path}")


def cmd_drafts(args):
    scored = json.loads(Path(args.scored).read_text(encoding="utf-8"))
    pool = [p for p in scored if p["status"] == "qualified" and p["outreach_channel"] == "email"]
    print(f"[drafts] {len(pool)} email-eligible (B.V.) prospects; generating {args.count}", file=sys.stderr)
    results = drafts.generate(pool, read_env(args.env, "GROQ_API_KEY"), args.model, args.count)
    out = Path(args.scored).with_name(Path(args.scored).stem.replace("-scored", "") + "-drafts.json")
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    md = out.with_suffix(".md")
    with md.open("w", encoding="utf-8") as f:
        f.write("# Sample drafts — LOCAL ONLY, NOT SENT, NOT IN GMAIL\n\n")
        for i, r in enumerate(results, 1):
            f.write(f"## {i}. {r['business_name']} ({r['domain']}) — score {r['score']}\n\n")
            f.write(f"- To (public role address): `{r['to']}`\n- Legal form: {r['legal_form']}\n")
            f.write(f"- Personalisation fact: {r['observation']}\n- Validation: {'OK' if not r['problems'] else ', '.join(r['problems'])}\n\n")
            f.write(f"**Onderwerp:** {r['subject']}\n\n```text\n{r['email_text']}\n```\n\n")
    print(f"[drafts] wrote {out} and {md}", file=sys.stderr)


BATCH_LOG = OUT / "batches.jsonl"


def cmd_daily(args):
    con = _con(args)
    results, meta = pipeline.run_daily(con, args.cc_tsv, args.batch, args.seed)
    out = OUT / "daily" / meta["started"].replace(":", "")
    path = report.write_daily(out, results, meta)
    m = report.metrics(results)
    entry = {"batch": args.label or meta["started"], **meta, "metrics": m,
             "domains": [r["domain"] for r in results], "report": str(path)}
    OUT.mkdir(parents=True, exist_ok=True)
    with BATCH_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(json.dumps({**m, "elapsed_min": meta["elapsed_min"], "pool_remaining": meta["pool_remaining"]},
                     ensure_ascii=False, indent=2))
    print(f"[daily] report: {path}", file=sys.stderr)


def cmd_import(args):
    con = _con(args)
    rows = json.loads(Path(args.file).read_text(encoding="utf-8"))
    print(f"[import] {pipeline.import_previous(con, rows)} prospects imported into "
          f"{state.resolve_db_path(getattr(args, 'db', None))}", file=sys.stderr)


def cmd_kvk_list(args):
    con = _con(args)
    rows = sorted(state.load_prospects(con, "KVK_CHECK"), key=lambda p: -(p.get("fit_score") or 0))
    OUT.mkdir(parents=True, exist_ok=True)
    report.write_queue_csv(OUT / "kvk_check.csv", rows)
    lines = ["# KVK CHECK queue — verify each company by hand on kvk.nl", "",
             "For each: open the KvK link, confirm legal form (Rechtsvorm). Then run the mark command.", ""]
    for p in rows:
        lines.append(report.prospect_block(p) + f"- KvK lookup (open manually): {report.kvk_search_url(p)}\n"
                     f"- B.V.: `python run.py kvk mark {p['domain']} --bv --kvk <nummer>`  ·  "
                     f"not B.V.: `python run.py kvk mark {p['domain']} --not-bv`\n")
    (OUT / "kvk_check.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[kvk] {len(rows)} companies to verify -> {OUT / 'kvk_check.md'} and kvk_check.csv")


KVK_SHEET_COLUMNS = ["domain", "business_name", "city", "website", "public_email", "kvk_search_url",
                     "kvk_number_on_site", "homeowner_evidence", "queue_reason",
                     # filled in by the CEO after checking kvk.nl by hand:
                     "is_bv (yes/no/uncertain)", "kvk_number", "entity_matches (yes/no)", "active (yes/no)",
                     "genuine_local_installer (yes/no)", "homeowner_fit_reasonable (yes/no)", "notes"]


def cmd_kvk_sheet(args):
    """Freeze the first N KVK CHECK prospects (highest fit first) into a CSV for manual verification."""
    import csv
    con = _con(args)
    rows = sorted(state.load_prospects(con, "KVK_CHECK"), key=lambda p: (-(p.get("fit_score") or 0), p["domain"]))
    rows = rows[: args.first]
    path = OUT / f"kvk_review_{args.batch}.csv"
    if path.exists() and not args.force:
        sys.exit(f"{path} already exists (frozen). Use --force to overwrite.")
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(KVK_SHEET_COLUMNS)
        for p in rows:
            w.writerow([p["domain"], p.get("business_name"), p.get("city"), p.get("final_url") or p.get("website"),
                        p.get("public_email"), report.kvk_search_url(p), p.get("kvk_number"),
                        ", ".join(p.get("customer_type_evidence", {}).get("homeowner", [])[:5]),
                        p.get("queue_reason"), "", "", "", "", "", "", ""])
    print(f"[kvk] {len(rows)} prospects frozen -> {path}")


def _yn(value: str) -> str:
    v = (value or "").strip().lower()
    return {"yes": "yes", "ja": "yes", "y": "yes", "j": "yes", "no": "no", "nee": "no", "n": "no"}.get(v, "unknown")


def cmd_kvk_import(args):
    """Record the CEO's filled-in verification sheet and re-score those prospects."""
    import csv
    con = _con(args)
    n = 0
    with open(args.file, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            raw = (row.get("is_bv (yes/no/uncertain)") or "").strip().lower()
            if not raw:
                continue  # not checked yet
            result = {"yes": "bv", "ja": "bv", "no": "not_bv", "nee": "not_bv"}.get(raw, "uncertain")
            state.record_kvk_review(
                con, row["domain"], result, (row.get("kvk_number") or "").strip() or None,
                (row.get("notes") or "").strip() or None,
                entity_match=_yn(row.get("entity_matches (yes/no)")), active=_yn(row.get("active (yes/no)")),
                genuine_installer=_yn(row.get("genuine_local_installer (yes/no)")),
                homeowner_ok=_yn(row.get("homeowner_fit_reasonable (yes/no)")),
                review_batch=pathlib_stem(args.file))
            _rescore_domain(con, row["domain"])
            n += 1
    con.commit()
    print(f"[kvk] {n} reviews recorded")
    cmd_kvk_stats(args)


def pathlib_stem(path: str) -> str:
    return Path(path).stem


def _rescore_domain(con, domain: str) -> dict:
    row = con.execute("select data from prospects where domain = ?", (domain,)).fetchone()
    p = {k: v for k, v in json.loads(row["data"]).items() if k not in pipeline.STRIP}
    p = score.score(pipeline.apply_review(con, p))
    state.save_prospect(con, p)
    return p


def cmd_kvk_stats(args):
    con = _con(args)
    rows = con.execute("select * from kvk_reviews").fetchall()
    total = len(rows)
    c = {k: sum(1 for r in rows if r["result"] == k) for k in ("bv", "not_bv", "uncertain")}
    usable = sum(1 for r in rows if r["result"] == "bv" and r["entity_match"] != "no" and r["active"] != "no"
                 and r["genuine_installer"] != "no" and r["homeowner_ok"] != "no")
    print(json.dumps({
        "reviewed": total, "bv": c["bv"], "not_bv": c["not_bv"], "uncertain": c["uncertain"],
        "kvk_check_to_bv_rate": round(c["bv"] / total, 2) if total else None,
        "kvk_check_to_ready_rate": round(usable / total, 2) if total else None,
        "entity_mismatch": sum(1 for r in rows if r["entity_match"] == "no"),
        "inactive": sum(1 for r in rows if r["active"] == "no"),
        "not_genuine_installer": sum(1 for r in rows if r["genuine_installer"] == "no"),
        "homeowner_fit_wrong": sum(1 for r in rows if r["homeowner_ok"] == "no"),
    }, indent=2))


def cmd_measure(args):
    """Aggregate all measurement batches + KvK review results into the requested ratios."""
    if not BATCH_LOG.exists():
        sys.exit("no batches logged yet")
    batches = [json.loads(l) for l in BATCH_LOG.read_text(encoding="utf-8").splitlines() if l.strip()]
    if args.only:
        batches = [b for b in batches if b["batch"].startswith(args.only)]
    con = _con(args)
    domains = [d for b in batches for d in b["domains"]]
    rows = [json.loads(r["data"]) for d in domains
            for r in [con.execute("select data from prospects where domain = ?", (d,)).fetchone()] if r]
    m = report.metrics(rows)   # current state (after KvK reviews and rule changes)
    reviews = con.execute("select result from kvk_reviews").fetchall()
    bv_rate = (sum(1 for r in reviews if r["result"] == "bv") / len(reviews)) if reviews else None
    n = max(m["Domains processed"], 1)
    ready_per_100 = 100 * m["Ready for outreach"] / n
    kvk_per_100 = 100 * m["KVK checks required"] / n
    expected_ready_per_100 = ready_per_100 + (kvk_per_100 * bv_rate if bv_rate is not None else 0)
    minutes = sum(b["elapsed_min"] for b in batches)
    sec_per_domain = 60 * minutes / n
    need = lambda target: round(100 * target / expected_ready_per_100) if expected_ready_per_100 else None
    out = {
        "batches": [b["batch"] for b in batches], "metrics_total": m,
        "ready_per_100_domains (website-confirmed B.V.)": round(ready_per_100, 2),
        "kvk_check_per_100_domains": round(kvk_per_100, 2),
        "kvk_check_to_bv_rate": round(bv_rate, 2) if bv_rate is not None else "not measured yet",
        "expected_ready_per_100_incl_kvk": round(expected_ready_per_100, 2),
        "reachable_to_email_rate": round(m["Public business emails"] / max(m["Websites reachable"], 1), 2),
        "homeowner_to_ready_rate": round(m["Ready for outreach"] / max(m["Homeowner-fit"], 1), 3),
        "avg_seconds_per_domain": round(sec_per_domain, 1),
        "domains_needed_for_5_ready_per_day": need(5),
        "domains_needed_for_10_ready_per_day": need(10),
        "pool_remaining": batches[-1].get("pool_remaining"),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


def cmd_kvk_mark(args):
    con = _con(args)
    row = con.execute("select data from prospects where domain = ?", (args.domain,)).fetchone()
    if not row:
        sys.exit(f"unknown domain: {args.domain}")
    result = "bv" if args.bv else ("not_bv" if args.not_bv else "uncertain")
    state.record_kvk_review(con, args.domain, result, args.kvk, args.note,
                            entity_match=args.entity_match, active=args.active,
                            genuine_installer=args.genuine, homeowner_ok=args.homeowner_ok)
    p = json.loads(row["data"])
    p = {k: v for k, v in p.items() if k not in ("queue", "queue_reason", "status", "outreach_channel",
                                                "strong_prospect", "disqualify_reasons", "score_reasons")}
    p = score.score(pipeline.apply_review(con, p))
    state.save_prospect(con, p)
    con.commit()
    label = {"bv": "kvk_verified_bv", "not_bv": "not_bv", "uncertain": "uncertain"}[result]
    print(f"[kvk] {args.domain}: marked {label} -> queue {p['queue']}")


def cmd_rescore(args):
    """Re-apply the current scoring rules to stored results (no website fetches)."""
    con = _con(args)
    n = 0
    order = {r["domain"]: r["first_seen"] for r in con.execute("select domain, first_seen from domains")}
    for p in sorted(state.load_prospects(con), key=lambda x: (order.get(x["domain"], ""), x["domain"])):
        p = {k: v for k, v in p.items() if k not in ("queue", "queue_reason", "status", "outreach_channel",
                                                    "strong_prospect", "disqualify_reasons", "score_reasons",
                                                    "duplicate_of")}
        if p.get("fetch_status") == 200:
            dup = state.find_duplicate(con, p)
            if dup:
                p["duplicate_of"] = dup
        state.save_prospect(con, score.score(pipeline.apply_review(con, p)))
        n += 1
    con.commit()
    print(f"[rescore] {n} prospects re-scored; network sweep flagged {pipeline.network_sweep(con)} domains")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", help="state database path (default: $ORSE_PROSPECTS_DB or data/state.sqlite)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dryrun")
    d.add_argument("--city", default="*")
    d.add_argument("--source", choices=["osm", "commoncrawl"], default="osm")
    d.add_argument("--cc-tsv", default="data/raw/cc/nl-trade-domains.tsv")
    d.add_argument("--sample", type=int, default=120)
    d.add_argument("--seed", type=int, default=20261006)
    d.add_argument("--pbf")
    d.add_argument("--max-sites", type=int, default=80)
    d.add_argument("--skip-overpass", action="store_true")
    d.add_argument("--exclude-city", help="skip a city already processed")
    d.set_defaults(func=cmd_dryrun)
    g = sub.add_parser("drafts")
    g.add_argument("--scored", required=True)
    g.add_argument("--env", required=True)
    g.add_argument("--count", type=int, default=5)
    g.add_argument("--model", default="openai/gpt-oss-20b")
    g.set_defaults(func=cmd_drafts)
    dd = sub.add_parser("daily", help="process the next batch of due Common Crawl domains")
    dd.add_argument("--batch", type=int, default=120)
    dd.add_argument("--cc-tsv", default="data/raw/cc/nl-trade-domains.tsv")
    dd.add_argument("--seed", type=int, default=20261006)
    dd.add_argument("--label", help="batch label for the measurement log, e.g. measure-1")
    dd.set_defaults(func=cmd_daily)
    sub.add_parser("rescore", help="re-apply current rules to stored results (no fetching)").set_defaults(func=cmd_rescore)
    im = sub.add_parser("import", help="seed local state with an earlier scored JSON file")
    im.add_argument("file")
    im.set_defaults(func=cmd_import)
    kv = sub.add_parser("kvk", help="KVK CHECK queue")
    kvs = kv.add_subparsers(dest="kvk_cmd", required=True)
    kvs.add_parser("list").set_defaults(func=cmd_kvk_list)
    km = kvs.add_parser("mark")
    km.add_argument("domain")
    grp = km.add_mutually_exclusive_group(required=True)
    grp.add_argument("--bv", action="store_true", help="CEO verified on kvk.nl: legal form is B.V.")
    grp.add_argument("--not-bv", action="store_true", help="CEO verified on kvk.nl: not a B.V.")
    grp.add_argument("--uncertain", action="store_true", help="CEO could not determine the legal form")
    for flag, dest in (("--entity-match", "entity_match"), ("--active", "active"),
                       ("--genuine", "genuine"), ("--homeowner-ok", "homeowner_ok")):
        km.add_argument(flag, dest=dest, choices=["yes", "no", "unknown"], default="unknown")
    km.add_argument("--kvk", help="KvK number seen on kvk.nl")
    km.add_argument("--note")
    km.set_defaults(func=cmd_kvk_mark)
    ks = kvs.add_parser("sheet", help="freeze the first N KVK CHECK prospects into a review CSV")
    ks.add_argument("--first", type=int, default=15)
    ks.add_argument("--batch", default="batch1")
    ks.add_argument("--force", action="store_true")
    ks.set_defaults(func=cmd_kvk_sheet)
    ki = kvs.add_parser("import", help="record the CEO's filled-in review CSV")
    ki.add_argument("file")
    ki.set_defaults(func=cmd_kvk_import)
    kvs.add_parser("stats").set_defaults(func=cmd_kvk_stats)
    me = sub.add_parser("measure", help="aggregate measurement batches")
    me.add_argument("--only", help="only batches whose label starts with this")
    me.set_defaults(func=cmd_measure)
    args = ap.parse_args()
    try:
        args.func(args)
    finally:
        close_connections()


if __name__ == "__main__":
    main()
