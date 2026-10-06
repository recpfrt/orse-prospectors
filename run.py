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

from prospects import collect, drafts, enrich, score

OUT = Path(__file__).parent / "data" / "out"
RED_LIST = Path(__file__).parent / "data" / "do_not_contact.csv"   # local stand-in until Supabase is approved

CSV_FIELDS = ["status", "outreach_channel", "qualification_score", "business_name", "city", "service_category",
              "website", "public_email", "email_source_url", "legal_form", "kvk_number", "online_booking",
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


def cmd_dryrun(args):
    OUT.mkdir(parents=True, exist_ok=True)
    label = "province" if args.city == "*" else args.city.lower()
    stamp = f"{datetime.date.today():%Y%m%d}-{label}"

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
        "city": args.city, "source": source, "raw_osm_entries": len(raw),
        "by_category_raw": dict(Counter(c["service_category"] for c in raw)),
        "dedupe": dict(dedupe_stats), "sites_enriched": len(enriched),
        "reachable": sum(1 for p in enriched if p["fetch_status"] == 200),
        "robots_blocked": sum(1 for p in enriched if p["robots_blocked"]),
        "with_public_role_email": sum(1 for p in enriched if p.get("public_email")),
        "legal_form": dict(Counter(p.get("legal_form", "n/a") for p in enriched)),
        "online_booking": sum(1 for p in enriched if p.get("online_booking")),
        "status": dict(Counter(p["status"] for p in scored)),
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


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dryrun")
    d.add_argument("--city", required=True)
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
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
