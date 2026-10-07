# OrseBusiness System Map

Audit date: 2026-10-07 · Method: read-only inspection of local repositories, git history, configuration
(environment variable **names** only, never values), offline test runs, and public HTTP GETs against production.
No files, databases, deployments, workflows or environment variables were changed by the audit.

Labels: **[V]** VERIFIED · **[I]** INFERRED · **[U]** UNKNOWN · **[RFI]** REQUIRES FURTHER INVESTIGATION

---

## GM North Star

The ultimate objective is:

> "Operate OrseBusiness as a reliable, revenue-focused business every working day: monitor the systems, discover
> and qualify customers, prepare outreach for CEO approval, track the sales pipeline, detect and resolve safe
> operational problems, report the company's state, and continuously improve based on evidence."

The current objective is building a true **operational GM** for OrseBusiness — not merely improving the
prospecting script. Prospecting is one subsystem of the GM.

## Current Strategic Constraint

- First priority is acquiring the **first paying customer**.
- Do not start another startup.
- Do not overbuild.
- Do not add paid infrastructure before the $0-first threshold (first $500 revenue; see CLAUDE.md §9, §67).
- Sales / customer acquisition has priority over speculative product development.

## GM Operating Decisions (CEO, 2026-10-07)

- **Claude Code + `CLAUDE.md` is the primary GM operating interface.** The separate `orsebusiness-gm` Next.js
  application is not revived or expanded unless a future audit proves a concrete need.
- **`orse-prospects` is the sales/prospecting engine.** It must not become a replacement CRM for LeadFlow.
- **The existing SQLite database (`data/state.sqlite`) remains the primary prospecting operational store** unless a
  future requirement proves otherwise.
- The GM will maintain operational state: **runs, incidents, approvals, decisions, daily reports and pending CEO
  actions.**
- The GM must eventually perform the **daily operating loop without the CEO explaining the process again.**
- **Automatic external outreach remains forbidden** unless explicitly approved by the CEO.
- The **CEO remains the final approval authority** for external, financial, legal, destructive, production-risky or
  irreversible actions.
- **Multi-market target:** Netherlands, UK, USA, Germany, France, Belgium, Austria, Denmark and other suitable EU
  markets. Netherlands assumptions must not be hardcoded into the core prospect engine; country-specific
  behaviour moves into adapters/configuration over time.
- **Prospecting target: ~10 QUALIFIED prospects per working day** — not a scraping volume. Discovery volume is
  determined dynamically from the actual measured qualification yield per market/source. Quality over quantity:
  if only 4 genuinely qualified prospects exist, report 4.

---

## 1. Systems inventory

| System | Location | Stack | Git / remote | State |
|---|---|---|---|---|
| orse-prospects (sales/prospecting engine) | Desktop/orse-prospects | Python 3.12, SQLite | `master`, 16 commits, **no remote** [V] | clean except untracked `CLAUDE.md` [V]; 139/139 tests pass [V] |
| LeadFlow (product, production) | Desktop/leadflow | Next.js 14, Supabase, nodemailer, Groq | github.com/recpfrt/leadflowv1, `main` = `origin/main` [V] | 45/45 tests pass [V]; typecheck/lint/build NOT TESTED (they write build artifacts) |
| FieldFlow (product, MVP) | Desktop/fieldflow | n8n (Docker), Supabase, Groq Whisper, PDF service | github.com/recpfrt/fieldflow, 1 commit (2026-10-02) [V] | Docker daemon not running at audit time [V] |
| FieldFlow n8n workflows | Desktop/fieldflow-workflows | 23 n8n JSON exports | **not in git** [V] | all exports `active=false` [V]; live n8n state [U] |
| orsebusiness-gm (separate GM web app) | Desktop/orsebusiness-gm | Next.js 16, Supabase, Groq (`openai/gpt-oss-20b`), Tavily | branch `feature/gm-hardening`, 2 commits, **no remote** [V] | 17 uncommitted files (CEO auth, tool approval, login) [V]; deployed? [U], probably not [I]; **frozen by CEO decision** |
| VITA, "digital products" | Desktop | unrelated (health app; printable designs) [I] | — | out of scope |

Production: `https://orsebusiness.vercel.app` → 200; `/book`, `/demo`, `/impressum` → 200; **`/nl` → 404** [V].
Its pages match LeadFlow's routes, so production is LeadFlow [I]. Deployed commit [U].
`orsebusiness.com` does not resolve but is LeadFlow's canonical URL (`app/layout.tsx:40`) [V].

LeadFlow branches [V]: `fix/gm-audit-p0` and `fix/pilot-blockers` merged; `feature/nl-landing` **unmerged**
(adds `app/nl/page.tsx` and `lib/supabase/0014_prospects.sql`, marked "PREPARED ONLY. Not applied").
Untracked `app/demo/page.backup.tsx` [V].

## 2. Data stores

| Store | Used by | Notes |
|---|---|---|
| `data/state.sqlite` | orse-prospects | tables: prospects, domains, kvk_reviews, manual_holds, outreach, outreach_events; `integrity_check` ok [V]; git-ignored [V]; backups in `data/backups/` on the **same disk** [V] |
| Supabase project A | LeadFlow | migrations 0001–0013 in `lib/supabase/` [V]; applied state [U]; production row counts (businesses, leads, bookings, customers) [U] — not queried |
| Supabase project B | FieldFlow; LeadFlow via `FIELDFlow_*` service-role key [V] | migrations in `fieldflow/supabase/migrations/` [V] |
| Supabase project C | orsebusiness-gm | tables projects/conversations/messages/memories/skills/tools [I, from code]; **no migrations in repo** [V] |
| n8n SQLite backup | `leadflow/n8n-database-backup.sqlite` (git-ignored) [V] | contents not inspected |

Three distinct Supabase projects [V] (URL equality comparison; values not printed).

## 3. Prospecting engine (orse-prospects)

Flow: collect → dedupe / red list / contacted exclusion → enrich (own website, robots-aware, 1.2 s delay,
sequential) → classify (customer type, installer, mixed audience, marine/shipping B2B) → legal form (website pages +
legal pages) → score → queue → CEO KvK review → READY list → local drafts.

- **Sources** [V]: Common Crawl NL trade-domain list (12,528 domains; ~10,429 unprocessed at last measurement);
  Geofabrik OSM extract (Utrecht only; 37 prospects); Overpass (code present; last success [U]);
  CBS woonplaatsen (city / landing-page checks).
- **KvK open dataset** is downloaded but unused and **unusable for per-company verification**: no company name,
  no KvK number (columns: start date, active, insolvency, legal form, postcode region, SBI codes) [V].
  Usable only for market sizing [I].
- **Throughput** [V]: ~4.6–4.9 s per domain; a 250-domain batch takes ~20 min.
- **Qualification yield** [V]: 16 READY-or-contacted out of 787 processed domains (~2%) for the current NL
  Common Crawl source. Yield is source- and market-specific and must be re-measured per market [I].
- **Queues** [V]: READY 13 (KVK_VERIFIED 9, WEBSITE_BV 4) · KVK_CHECK 65 · MANUAL_REVIEW 142 · REJECTED 564 ·
  CONTACTED 3.
- **Legal status** [V]: KVK_VERIFIED 13 · WEBSITE_BV 75 · NOT_BV 13 · UNKNOWN 686. Current CEO policy: cold
  email only for confirmed B.V. KVK_VERIFIED and WEBSITE_BV are kept distinct.
- **Categories** [V]: hvac 339 · electrician 281 · plumber 167.
- **Country**: no country field; Netherlands is implicit throughout (Dutch terms, KvK, B.V. rules, CBS, `.nl`
  sources) [V]. This is the main obstacle to multi-market prospecting.
- **Phone numbers stored**: 22 of 787 [V].
- **KvK verification**: manual CEO review imported from CSV (26 reviews) [V]; no automated kvk.nl access [V].
- **CLI commands** [V]: dryrun, daily, rescore, import, kvk, hold, outreach, prospects, legal-recheck, measure, drafts.

## 4. Outreach and follow-up

- `outreach` table + `outreach_events` audit log, validated status transitions; contacted companies are excluded
  by domain, email, email domain, KvK number and name + town [V].
- Contacted [V]: 4 companies on 2026-10-06, message version V1, **no follow-up date**, reply `none`
  (loodgietersbedrijf-wisman.nl, aircorrect.nl, feikencv.nl, ventilatietotaal.nl — the last with no recorded address).
- Follow-up: only a due-date flag in `outreach list`; no follow-up queue and no default dates, so **nothing will
  surface** [V].
- Reply capture: manual CLI only; the outreach mailbox (orsebusiness@hotmail.com) is not integrated [V].
- Drafts: `prospects/drafts.py` (Groq) writes local files only, never mailbox drafts, never sends [V]. Its landing
  link and signature point to `/nl`, which returns 404 [V]. Whether the 4 sent emails contained that link [U].
- Lifecycle gaps vs CLAUDE.md §20: no QUALIFIED / PRESENTED / LOST statuses; no country / industry on outreach
  records; no per-message-version metrics [V].

## 5. Automation and scheduling

- No Windows scheduled tasks for any OrseBusiness system [V]; `run.py daily` is run manually [V].
- n8n: LeadFlow `lead-intake.json` export (inactive) [V]. LeadFlow's local env points the app URL and n8n at
  localhost [V]; production values [U]. FieldFlow `WEBHOOK_URL` uses a tunnel host [V]; n8n image is unpinned
  `latest` [V].

## 6. AI services (Groq)

Used by LeadFlow lead analysis (with tested fallback) [V], FieldFlow Whisper + extraction [V], orsebusiness-gm chat
[V] and orse-prospects drafts [V]. Quotas, usage and whether keys are shared [U]. orsebusiness-gm also uses Tavily
web search [V] (plan / cost [U]).

## 7. Security controls

Verified in place [V]:
- No env or database files tracked or ever committed in any repo; no secret-shaped strings in code or n8n exports.
- `.env*` files git-ignored everywhere; local business data, backups and SQLite files git-ignored in orse-prospects.
- LeadFlow `/api/fieldflow/*`: all 12 routes call `requireFieldFlowContext()` (server-side business resolution) and
  filter by business; download proxy has a host allow-list (SSRF protection).
- orsebusiness-gm (uncommitted): fail-closed tool approval, CEO-only auth, debug routes 404 by default.
- orse-prospects test suite asserts the real database is byte-identical before and after tests.

Open:
- LeadFlow public `POST /api/leads` and `POST /api/book` send email to a requester-supplied address (and
  `/api/leads` calls Groq) with **no rate limit / CAPTCHA in code** [V]; platform-level protection [U].
- LeadFlow download proxy is host-scoped, not business-object-scoped [RFI].
- orsebusiness-gm committed baseline has unauthenticated `/api/ai-test` and `/api/health` [V]; relevant only if
  deployed [U].
- No off-machine copy of orse-prospects (code + data), orsebusiness-gm, or the n8n workflow exports [V].

## 8. Overlaps — do not duplicate

- **Prospect store**: orse-prospects SQLite is the operational store (CEO decision). LeadFlow branch
  `feature/nl-landing` `0014_prospects.sql` defines a second, divergent prospect schema (different statuses incl.
  `drafted_in_gmail`, OSM-only sources) [V] — **must not be applied** as a parallel tracker. Its `gmail_draft_id`
  field conflicts with the rule that cold outreach is sent manually from orsebusiness@hotmail.com [I].
- **GM interface**: Claude Code + `CLAUDE.md` (CEO decision). orsebusiness-gm is frozen, not extended.
- **CRM**: LeadFlow owns customer/lead/booking data for its users; orse-prospects owns OrseBusiness's own
  sales prospecting and outreach tracking. No replacement CRM.

## 9. What should not be built

- A new prospect database, CRM or dashboard.
- Any extension of the orsebusiness-gm app (unless a future audit proves need).
- A kvk.nl scraper or a matcher on the anonymised KvK open dataset.
- Automatic sending of cold email or follow-ups.
- Paid enrichment, scraping, proxy or database services.
- n8n flows for prospecting (the Python engine already does this).
- A volume-driven scraping target; volume follows qualified yield.

---

## 10. Current capabilities [V]

- Discover and qualify Dutch installer prospects with evidence and explicit rejection reasons.
- Exclude contacted, held, duplicate and network businesses persistently.
- Track outreach locally with an audit trail.
- Generate personalised drafts locally (never sent).
- LeadFlow product live in production (intake, AI analysis with fallback, booking, email).
- FieldFlow MVP exists (not currently running).

## 11. GM gaps vs CLAUDE.md

- No run state, incident log, approval queue, decision log or pending-CEO-actions record (§52, §63).
- No daily report (§24) and no scheduler (§61).
- No system-health checks for LeadFlow / Vercel / Supabase / n8n (§23 step 1).
- No follow-up queue; no reply capture (§21).
- Outreach lifecycle incomplete (§20); no market or message-version metrics (§49, §50).
- Single-market engine: no country field, no adapter boundary (§22, §48).
- No CEO SEND / SKIP / FOLLOW UP presentation pack (§19).
- No dynamic discovery volume derived from measured yield (§11).

## 12. Critical issues (evidence only)

| # | Sev | Issue | Evidence |
|---|---|---|---|
| 1 | P2 (P1 if the link was sent) | Generated drafts link and sign off to `/nl`, which is 404 | [V] drafts.py + live GET; sent-email content [U] |
| 2 | P2 | LeadFlow public intake/booking endpoints unthrottled: email to arbitrary addresses + Groq calls | [V] code; platform protection [U] |
| 3 | P2 | 4 contacted companies have no follow-up date; follow-ups will not surface | [V] |
| 4 | P2 | No off-machine copy of the prospect engine + data, orsebusiness-gm, n8n workflow exports | [V] |
| 5 | P2 | Divergent second prospect schema on an unmerged LeadFlow branch | [V] schema; intent [I] |
| 6 | P3 | Canonical `orsebusiness.com` does not resolve | [V] |
| 7 | P3 | orsebusiness-gm committed baseline has unauthenticated routes; fix uncommitted | [V]; deployment [U] |
| 8 | P3 | FieldFlow n8n down at audit time; webhooks rely on a tunnel URL | [V] now; customer impact [U] |

## 13. Business priorities (next 10)

1. Present the 13 READY prospects (9 KVK_VERIFIED) as SEND / SKIP / FOLLOW UP packs with evidence and drafts —
   sending, not discovery, is the current bottleneck (4 sent, 13 waiting) [V].
2. Fix the dead `/nl` link (local draft fix, or merge + deploy `/nl` with CEO approval).
3. Follow-up dates (CEO-supplied) and a follow-up queue for contacted companies.
4. Reply logging and per-message-version metrics.
5. Rate limiting / honeypot on LeadFlow public endpoints (production change → CEO approval).
6. Off-machine backup in private repositories (external → CEO approval).
7. GM core state + daily report on the existing SQLite store.
8. Daily scheduled discovery whose volume is set from measured qualified yield to approach ~10 qualified
   prospects per working day.
9. Faster KVK_CHECK throughput (65 waiting): CEO review in batches of 10 with search links.
10. Country field + market adapter boundary (NL first), then additional markets.

## 14. Proposed GM architecture

- **Interface:** Claude Code operating under `CLAUDE.md`.
- **Sales engine:** orse-prospects, extended with a small `gm` command layer; GM operational state (runs, incidents,
  approvals, decisions, daily reports, pending CEO actions) as new tables in the existing `data/state.sqlite`.
- **Deterministic jobs** (discovery batch, rescore, prospect list, health checks, report) run on a free local
  scheduler (e.g. Windows Task Scheduler); **judgement work** (drafts, review, escalation) runs in the Claude Code
  session and lands in the report and approval queue.
- **Health checks** read-only: tests, git status, production GETs, n8n/Docker availability, Supabase reachability.
- **Markets:** core engine country-agnostic; each market supplies an adapter/config (sources, language, terms,
  legal verification, exclusion rules, scoring adjustments, contact conventions). NL is the first adapter.
- **Discovery volume:** computed per run from the trailing qualified yield per market/source and the daily target,
  bounded by rate limits and source capacity.

## 15. Daily GM loop

1. **Health** — tests, git status, production routes, n8n/Docker, database integrity; record incidents.
2. **Sales state** — outreach by status, due follow-ups, recorded replies, pilots, customers.
3. **Discovery** — per-market batch sized from measured yield; rate-limited, resumable, duplicate-safe.
4. **Qualification** — rescore, network sweep, legal recheck for new KVK_CHECK.
5. **Duplicate / contact check** — contact match, holds, red list.
6. **Outreach queue** — best READY prospects (KVK_VERIFIED first) as §19 packs with local drafts.
7. **Follow-up queue** — due and overdue, with prepared follow-up drafts.
8. **Problem detection** — yield drops, source failures, rate-limit spikes, data anomalies.
9. **Safe fixes** — local, reversible, tested only.
10. **CEO escalation** — approval queue.
11. **Daily report** — written locally (e.g. `data/out/gm/YYYY-MM-DD.md`).

## 16. Approval boundaries (CEO only)

Sending any email or follow-up; contacting prospects; creating mailbox drafts; production deploys; merging to
LeadFlow `main`; applying Supabase migrations; querying or changing production data; enabling n8n workflows;
creating GitHub repositories or pushing; any paid API or service; pricing, pilot terms, contracts; credential or
env-var changes; deleting data; changing the email-only-for-B.V. policy; promoting a prospect to KVK_VERIFIED
(only via the CEO's manual KvK check).

## 17. Implementation phases

0. **Quick wins** — fix the draft landing link locally; CEO supplies follow-up dates for the 4 contacted; CEO
   decisions on backups and committing `CLAUDE.md`.
1. **GM core state** — runs / incidents / approvals / decisions / pending CEO actions tables + read-only
   `gm status`; unit tests.
2. **Daily report** generated from existing data; golden-file test.
3. **Outreach lifecycle** — PRESENTED / LOST, country, industry, message-version metrics, follow-up queue;
   transition tests.
4. **CEO presentation pack** (§19); test that contacted / held businesses never appear.
5. **Scheduler** — discovery + report with yield-driven volume; idempotency and crash-resume test (Scenario G).
6. **Health checks + incident records**; simulated-failure tests (Scenarios A, B).
7. **Market adapters** — country field, NL adapter extracted from the core; proposal for the next market(s).
8. **GM acceptance run** — Scenarios A–G (CLAUDE.md §73).

## 18. Unknowns to resolve

Vercel deployed commit and production env · LeadFlow production data (businesses, leads, bookings, real
customers) · whether any customer or pilot uses FieldFlow · live n8n workflow state · Groq / Tavily quotas and
key sharing · whether the 4 sent emails contained the `/nl` link · whether orsebusiness-gm is deployed anywhere ·
qualification yield for sources and markets other than NL Common Crawl.
