"""Daily GM run: the orchestration foundation for the operating loop in CLAUDE.md §23.

One run per working day, identified by a stable run id (gm-YYYY-MM-DD):
  * a completed run is not repeated (no-op) unless rerun=True;
  * a PARTIAL / FAILED / interrupted run resumes: only steps that are not DONE run again (plus the summary steps);
  * a run that is still RUNNING blocks a second run, unless its heartbeat is older than STALE_AFTER (then it is
    marked ABORTED, an incident is recorded, and this run resumes it).

Every step runs in its own transaction. A failing step is rolled back, recorded as FAILED with its error, raises
a P2 incident, and the run continues; a failing CRITICAL step skips the remaining steps (except the report).

Steps declare their effect: 'read' or 'local_write'. The orchestrator refuses any step declaring an external
effect — sending, contacting, deploying, spending, etc. are only ever prepared as CEO approval requests.
"""

import datetime as dt
import hashlib
import json
import sqlite3
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import config, gm_state, outreach, pipeline, state
from .score import KVK_CHECK, MANUAL_REVIEW, READY, REJECTED

STALE_AFTER = dt.timedelta(hours=2)
ALLOWED_EFFECTS = ("read", "local_write")
REPORT_DIR = state.PROJECT_ROOT / "data" / "out" / "gm"
DISCOVERY_STALE_DAYS = 7

# Read-only production probes (public pages only). /nl is linked from generated drafts.
HEALTH_PROBES = (
    ("LeadFlow home", "https://orsebusiness.vercel.app/", "P1"),
    ("LeadFlow booking page", "https://orsebusiness.vercel.app/book", "P1"),
    ("Outreach landing page (/nl, linked from drafts)", "https://orsebusiness.vercel.app/nl", "P2"),
)


class GmRunInProgress(RuntimeError):
    pass


class ExternalEffectRefused(RuntimeError):
    pass


@dataclass
class Outcome:
    status: str                      # DONE or SKIPPED
    summary: dict = field(default_factory=dict)


@dataclass
class Step:
    name: str
    func: Callable[["Context"], Outcome]
    effect: str = "read"             # 'read' | 'local_write'
    critical: bool = False           # failure stops the remaining steps (except the report)
    always: bool = False             # re-run on every resume/rerun (summary/report steps)


@dataclass
class Context:
    con: sqlite3.Connection
    run_id: str
    run_date: dt.date
    now: Callable[[], dt.datetime]
    offline: bool
    http_get: Callable[[str], int]
    report_dir: Path
    tz: str
    results: dict = field(default_factory=dict)       # step name -> summary of this run
    observed: dict = field(default_factory=dict)      # incident prefix -> fingerprints seen by this step

    def ts(self) -> str:
        return gm_state.timestamp(self.now(), self.tz)

    def incident(self, prefix: str, key: str, severity: str, title: str, *, detail: str | None = None,
                 needs_ceo: bool = False, source: str | None = None) -> None:
        fp = f"{prefix}{key}"
        self.observed.setdefault(prefix, set()).add(fp)
        gm_state.raise_incident(self.con, fp, severity, title, detail=detail, source=source, needs_ceo=needs_ceo,
                                run_id=self.run_id, at=self.ts())

    def settle(self, prefix: str) -> None:
        """Called at the end of a successful detector: resolve its open incidents no longer observed."""
        gm_state.resolve_unobserved(self.con, prefix, self.observed.get(prefix, set()), run_id=self.run_id,
                                    at=self.ts())


def _http_status(url: str) -> int:
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": config.USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=config.REQUEST_TIMEOUT_S) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


# ======================================================================== steps

def step_system_health(ctx: Context) -> Outcome:
    integrity = ctx.con.execute("pragma integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError(f"state database integrity check failed: {integrity}")
    tables = {r[0] for r in ctx.con.execute("select name from sqlite_master where type = 'table'")}
    missing = sorted({"prospects", "domains", "outreach", "outreach_events", "manual_holds", "kvk_reviews"} - tables)
    if missing:
        raise RuntimeError(f"state database is missing tables: {', '.join(missing)}")

    probes = {}
    if ctx.offline:
        probes = {name: "NOT CHECKED (offline)" for name, _, _ in HEALTH_PROBES}
    else:
        for name, url, severity in HEALTH_PROBES:
            try:
                code = ctx.http_get(url)
            except Exception as e:               # network failure is a finding, not a crash
                code = None
                err = f"{type(e).__name__}: {e}"
            probes[name] = code if code is not None else f"ERROR {err}"
            if code != 200:
                ctx.incident("health:http:", url, severity, f"{name} not available ({probes[name]})",
                             detail=f"GET {url} returned {probes[name]}; expected 200", source="system_health",
                             needs_ceo=severity in ("P0", "P1"))
        ctx.settle("health:http:")              # only when the probes actually ran

    last = ctx.con.execute("select max(last_attempt) from domains").fetchone()[0]
    stale_days = None
    if last:
        stale_days = (ctx.run_date - dt.date.fromisoformat(last[:10])).days
        if stale_days > DISCOVERY_STALE_DAYS:
            ctx.incident("health:discovery-stale", "", "P3", f"no prospect discovery for {stale_days} days",
                         detail=f"last domain attempt {last}", source="system_health")
    ctx.settle("health:discovery-stale")
    return Outcome("DONE", {"database_integrity": integrity, "probes": probes,
                            "last_discovery_attempt": last, "days_since_discovery": stale_days})


def step_sales_state(ctx: Context) -> Outcome:
    queues = {r[0]: r[1] for r in ctx.con.execute("select queue, count(*) from prospects group by queue")}
    statuses = {r[0]: r[1] for r in ctx.con.execute("select status, count(*) from outreach group by status")}
    ready_new = pipeline.prospect_list(ctx.con)
    contacted_ever = ctx.con.execute("select count(*) from outreach where status != 'NOT_CONTACTED'").fetchone()[0]
    return Outcome("DONE", {
        "prospect_queues": queues,
        "ready_not_contacted": len(ready_new),
        "outreach_by_status": statuses,
        "contacted_total": contacted_ever,
        "replied": sum(statuses.get(s, 0) for s in ("REPLIED", "INTERESTED", "NOT_INTERESTED", "PILOT", "CUSTOMER")),
        "pilots": statuses.get("PILOT", 0),
        "customers": statuses.get("CUSTOMER", 0),
        "revenue": "UNKNOWN (no revenue source connected)",
    })


def step_discovery(ctx: Context) -> Outcome:
    return Outcome("SKIPPED", {"reason": "not automated in the GM Core phase; run `python run.py daily` manually"})


def step_qualification(ctx: Context) -> Outcome:
    return Outcome("SKIPPED", {"reason": "not automated in the GM Core phase (rescore / legal-recheck are manual)"})


def step_contact_checks(ctx: Context) -> Outcome:
    """Invariant: nothing recommended as a new prospect may be contacted, held, duplicated or red-listed."""
    blocked_domains, blocked_emails = pipeline._red_list()
    violations = []
    for p in pipeline.prospect_list(ctx.con):
        why = []
        match = outreach.contact_match(ctx.con, p)
        if match:
            why.append(f"already in outreach ({match[1]})")
        if state.active_hold(ctx.con, p["domain"]):
            why.append("active manual hold")
        if p["domain"] in blocked_domains or (p.get("public_email") or "").lower() in blocked_emails:
            why.append("on do-not-contact list")
        if p.get("duplicate_of"):
            why.append(f"duplicate of {p['duplicate_of']}")
        if why:
            violations.append(f"{p['domain']}: {', '.join(why)}")
            ctx.incident("contact-check:", p["domain"], "P1", f"{p['domain']} would be recommended despite: "
                         + ", ".join(why), source="contact_checks", needs_ceo=True)
    held_ready = [r[0] for r in ctx.con.execute(
        "select p.domain from prospects p join manual_holds h on h.domain = p.domain "
        "where h.released_at is null and p.queue = ?", (READY,))]
    for d in held_ready:
        violations.append(f"{d}: READY despite an active hold")
        ctx.incident("contact-check:", f"hold:{d}", "P1", f"{d} is READY despite an active manual hold",
                     source="contact_checks", needs_ceo=True)
    ctx.settle("contact-check:")
    return Outcome("DONE", {"violations": violations, "checked": True})


def step_outreach_queue(ctx: Context) -> Outcome:
    """Prepare (never send) first-contact requests for READY, not-contacted prospects."""
    rows = pipeline.prospect_list(ctx.con)
    wanted = set()
    for p in rows:
        fp = f"contact:{p['domain']}"
        wanted.add(fp)
        gm_state.request_approval(
            ctx.con, fp, "CONTACT_PROSPECT", f"First outreach to {p.get('business_name') or p['domain']}",
            details={"domain": p["domain"], "website": p.get("website"), "email": p.get("public_email"),
                     "city": p.get("city"), "service_category": p.get("service_category"),
                     "legal_form_status": p.get("legal_form_status"), "qualification_score":
                     p.get("qualification_score"), "why": p.get("queue_reason")},
            risk="external communication (cold email) — sent by the CEO by hand from the outreach mailbox",
            recommendation="review evidence; a draft can be prepared on request", run_id=ctx.run_id, at=ctx.ts())
    withdrawn = gm_state.withdraw_unrequested(ctx.con, "contact:", wanted, reason="no longer a READY new prospect",
                                              run_id=ctx.run_id, at=ctx.ts())
    by_status = {}
    for p in rows:
        by_status[p.get("legal_form_status")] = by_status.get(p.get("legal_form_status"), 0) + 1
    return Outcome("DONE", {"prepared": len(rows), "by_legal_form_status": by_status, "withdrawn": withdrawn})


def step_follow_up_queue(ctx: Context) -> Outcome:
    today = ctx.run_date.isoformat()
    due, missing = [], []
    wanted = set()
    for r in ctx.con.execute("select * from outreach where status in ('CONTACTED', 'FOLLOW_UP') order by domain"):
        if not r["follow_up_at"]:
            missing.append(r["domain"])
        elif r["follow_up_at"] <= today:
            fp = f"follow-up:{r['domain']}:{r['follow_up_at']}"
            wanted.add(fp)
            due.append(r["domain"])
            gm_state.request_approval(
                ctx.con, fp, "SEND_FOLLOW_UP", f"Follow-up to {r['company']} (due {r['follow_up_at']})",
                details={"domain": r["domain"], "email": r["email"], "contacted_at": r["contacted_at"],
                         "subject": r["subject"], "message_version": r["message_version"],
                         "next_action": r["next_action"]},
                risk="external communication", recommendation="CEO decides; a draft can be prepared on request",
                run_id=ctx.run_id, at=ctx.ts())
    gm_state.withdraw_unrequested(ctx.con, "follow-up:", wanted, reason="follow-up no longer due (status changed)",
                                  run_id=ctx.run_id, at=ctx.ts())
    if missing:
        ctx.incident("follow-up:missing-date", "", "P2", f"{len(missing)} contacted companies have no follow-up date",
                     detail=", ".join(missing), source="follow_up_queue", needs_ceo=True)
    ctx.settle("follow-up:missing-date")
    return Outcome("DONE", {"due": due, "missing_follow_up_date": missing})


def step_problem_detection(ctx: Context) -> Outcome:
    failed = [r["step"] for r in gm_state.steps(ctx.con, ctx.run_id) if r["status"] == "FAILED"]
    kvk = ctx.con.execute("select count(*) from prospects where queue = ?", (KVK_CHECK,)).fetchone()[0]
    if kvk:
        ctx.incident("sales:kvk-backlog", "", "P3", f"{kvk} prospects wait for a manual KvK check",
                     detail="`python run.py kvk` lists them with search links", source="problem_detection",
                     needs_ceo=True)
    ctx.settle("sales:kvk-backlog")
    ready = len(pipeline.prospect_list(ctx.con))
    if ready == 0:
        ctx.incident("sales:queue-empty", "", "P2", "no READY prospects available for outreach",
                     source="problem_detection")
    ctx.settle("sales:queue-empty")
    # CEO boundary: approvals can only be decided with the CEO PIN; a decision without proof was not made that way.
    if not gm_state.ceo_pin_configured(ctx.con):
        ctx.incident("ceo:pin-missing", "", "P2", "CEO PIN not configured — approval requests cannot be decided",
                     detail="the CEO runs `python run.py gm ceo-pin` once, in their own terminal",
                     source="problem_detection", needs_ceo=True)
    ctx.settle("ceo:pin-missing")
    unproven = gm_state.unproven_decisions(ctx.con)
    for a in unproven:
        ctx.incident("ceo:unproven-decision:", str(a["id"]), "P1",
                     f"approval #{a['id']} is {a['status']} without a CEO proof (not decided via the CEO PIN)",
                     detail=f"{a['category']}: {a['title']} — treat as NOT approved; CEO: run `gm audit`",
                     source="problem_detection", needs_ceo=True)
    ctx.settle("ceo:unproven-decision:")
    return Outcome("DONE", {"failed_steps_so_far": failed, "kvk_check_backlog": kvk, "ready_new": ready,
                            "ceo_pin_configured": gm_state.ceo_pin_configured(ctx.con),
                            "unproven_decisions": [a["id"] for a in unproven]})


def step_safe_fixes(ctx: Context) -> Outcome:
    return Outcome("SKIPPED", {"reason": "no safe-fix handlers registered in the GM Core phase"})


def step_approval_queue(ctx: Context) -> Outcome:
    pending = gm_state.pending_ceo_actions(ctx.con)
    by_cat = {}
    for a in pending["approvals_pending"]:
        by_cat[a["category"]] = by_cat.get(a["category"], 0) + 1
    return Outcome("DONE", {"pending_by_category": by_cat, "approvals_pending": len(pending["approvals_pending"]),
                            "approved_not_done": len(pending["approved_not_done"]),
                            "incidents_for_ceo": len(pending["incidents"])})


def step_report(ctx: Context) -> Outcome:
    text = render_report(ctx)
    ctx.report_dir.mkdir(parents=True, exist_ok=True)
    path = ctx.report_dir / f"{ctx.run_date.isoformat()}.md"
    path.write_text(text, encoding="utf-8")                   # same path per day: reruns overwrite, never pile up
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    ctx.con.execute("update gm_runs set report_path = ?, report_sha256 = ? where run_id = ?",
                    (str(path), sha, ctx.run_id))
    return Outcome("DONE", {"path": str(path), "sha256": sha})


DEFAULT_STEPS = (
    Step("system_health", step_system_health, "read", critical=True),
    Step("sales_state", step_sales_state, "read"),
    Step("prospect_discovery", step_discovery, "read"),
    Step("qualification", step_qualification, "read"),
    Step("contact_checks", step_contact_checks, "local_write"),
    Step("outreach_queue", step_outreach_queue, "local_write"),
    Step("follow_up_queue", step_follow_up_queue, "local_write"),
    Step("problem_detection", step_problem_detection, "local_write"),
    Step("safe_fixes", step_safe_fixes, "local_write"),
    Step("approval_queue", step_approval_queue, "read", always=True),
    Step("daily_report", step_report, "local_write", always=True),
)


# ======================================================================== report

def _fmt(value) -> str:
    if isinstance(value, dict):
        return ", ".join(f"{k}: {v}" for k, v in value.items()) or "—"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) or "—"
    return str(value)


def render_report(ctx: Context) -> str:
    run = gm_state.get_run(ctx.con, ctx.run_id)
    st = gm_state.steps(ctx.con, ctx.run_id)
    res = ctx.results
    pending = gm_state.pending_ceo_actions(ctx.con)
    incidents = gm_state.open_incidents(ctx.con)
    sales = res.get("sales_state", {})
    failed = [s["step"] for s in st if s["status"] == "FAILED"]
    lines = [f"# GM daily report — {ctx.run_date.isoformat()}", "",
             f"Run `{ctx.run_id}` · attempt {run['attempt']} · generated {ctx.ts()}", "",
             "## Executive summary", ""]
    lines.append(f"- Steps: {sum(s['status'] == 'DONE' for s in st)} done, "
                 f"{sum(s['status'] == 'SKIPPED' for s in st)} skipped, {len(failed)} failed"
                 + (f" ({', '.join(failed)})" if failed else "") + ".")
    if sales:
        lines.append(f"- READY new prospects: {sales.get('ready_not_contacted')} · contacted total: "
                     f"{sales.get('contacted_total')} · replies: {sales.get('replied')} · pilots: "
                     f"{sales.get('pilots')} · customers: {sales.get('customers')} · revenue: {sales.get('revenue')}.")
    lines.append(f"- CEO: {len(pending['approvals_pending'])} approval requests pending, "
                 f"{len(pending['approved_not_done'])} approved but not yet done, "
                 f"{len(pending['incidents'])} incidents need a CEO decision.")
    lines += ["", "## System health", ""]
    health = res.get("system_health")
    if health:
        lines.append(f"- State database: {health['database_integrity']}")
        for name, code in health["probes"].items():
            lines.append(f"- {name}: {code}")
        lines.append(f"- Last discovery attempt: {health['last_discovery_attempt'] or 'UNKNOWN'}")
    else:
        lines.append("- UNKNOWN (health step did not complete in this run)")
    lines.append("- LeadFlow data, FieldFlow, n8n, Supabase, Vercel, GitHub, AI services: NOT CHECKED (not yet "
                 "implemented)")
    lines += ["", "## Sales", ""]
    lines += [f"- Prospect queues: {_fmt(sales.get('prospect_queues', {}))}",
              f"- Outreach by status: {_fmt(sales.get('outreach_by_status', {}))}"] if sales else ["- UNKNOWN"]
    fu = res.get("follow_up_queue", {})
    if fu:
        lines.append(f"- Follow-ups due: {_fmt(fu.get('due', []))} · missing follow-up date: "
                     f"{_fmt(fu.get('missing_follow_up_date', []))}")
    lines += ["", "## Steps", "", "| # | Step | Status | Attempts | Summary / error |", "|---|---|---|---|---|"]
    for s in st:
        detail = s["error"] or (_fmt(res[s["step"]]) if s["step"] in res else (s["result"] or ""))
        status = s["status"]
        if s["step"] == "daily_report" and status == "RUNNING":
            status, detail = "WRITING", "this report"
        lines.append(f"| {s['seq']} | {s['step']} | {status} | {s['attempts']} | "
                     f"{str(detail).replace('|', '/')[:220]} |")
    lines += ["", "## Problems (open incidents)", ""]
    if incidents:
        lines += ["| Sev | Incident | Status | Seen | CEO |", "|---|---|---|---|---|"]
        for i in incidents:
            lines.append(f"| {i['severity']} | {i['title']} | {i['status']} | {i['occurrences']}× since "
                         f"{i['first_seen'][:10]} | {'yes' if i['needs_ceo'] else 'no'} |")
    else:
        lines.append("- none")
    lines += ["", "## Actions taken", ""]
    acts = [e for e in gm_state.events(ctx.con, ctx.run_id) if e["kind"] not in ("step_started",)]
    lines += [f"- {e['at'][11:19]} {e['kind']}: {e['message']}" for e in acts[-40:]] or ["- none"]
    lines += ["", "## CEO approval required", ""]
    if pending["approvals_pending"] or pending["approved_not_done"] or pending["incidents"]:
        for a in pending["approvals_pending"]:
            lines.append(f"- [#{a['id']} {a['category']}] {a['title']} — risk: {a['risk'] or '—'}")
        for a in pending["approved_not_done"]:
            lines.append(f"- [#{a['id']} APPROVED, not done] {a['title']}")
        for i in pending["incidents"]:
            lines.append(f"- [incident #{i['id']} {i['severity']}] {i['title']}")
        lines.append("")
        lines.append("The CEO decides in their own terminal: `python run.py gm approve|reject|done <id>` "
                     "(asks for the CEO PIN; recording a decision never executes the action).")
    else:
        lines.append("- nothing")
    return "\n".join(lines) + "\n"


# ======================================================================== orchestrator

def run_daily(con: sqlite3.Connection, *, run_date: dt.date | None = None, rerun: bool = False,
              offline: bool = False, http_get: Callable[[str], int] | None = None,
              now: Callable[[], dt.datetime] | None = None, report_dir: Path | None = None,
              steps: tuple[Step, ...] = DEFAULT_STEPS, tz: str | None = None) -> dict:
    """tz: IANA timezone that defines the run date and timestamps (default gm_state.GM_TIMEZONE)."""
    now = now or (lambda: dt.datetime.now(dt.timezone.utc))
    tz = tz or gm_state.GM_TIMEZONE
    gm_state.ensure_schema(con)
    for s in steps:
        if s.effect not in ALLOWED_EFFECTS:
            raise ExternalEffectRefused(f"step {s.name!r} declares effect {s.effect!r}; the GM run may only "
                                        f"read or write local state — external actions need CEO approval")
    run_date = run_date or now().astimezone(gm_state.operating_tz(tz)).date()
    run_id = f"gm-{run_date.isoformat()}"
    ts = lambda: gm_state.timestamp(now(), tz)  # noqa: E731

    existing = gm_state.get_run(con, run_id)
    if existing and existing["status"] == "RUNNING":
        beat = dt.datetime.fromisoformat(existing["heartbeat_at"])
        if now().astimezone() - beat < STALE_AFTER:
            raise GmRunInProgress(f"{run_id} is already running (heartbeat {existing['heartbeat_at']})")
        con.execute("update gm_runs set status = 'ABORTED', finished_at = ?, error = ? where run_id = ?",
                    (ts(), "interrupted: heartbeat older than " + str(STALE_AFTER), run_id))
        gm_state.raise_incident(con, f"run:interrupted:{run_id}", "P2", f"GM run {run_id} was interrupted",
                                detail=f"last heartbeat {existing['heartbeat_at']}; resumed", source="orchestrator",
                                run_id=run_id, at=ts())
        gm_state.log_event(con, "run_aborted", f"{run_id} marked ABORTED (stale)", run_id=run_id, at=ts())
        con.commit()
        existing = gm_state.get_run(con, run_id)

    if existing and existing["status"] == "COMPLETED" and not rerun:
        gm_state.log_event(con, "run_noop", f"{run_id} already completed; nothing to do", run_id=run_id, at=ts())
        con.commit()
        return {"run_id": run_id, "status": "COMPLETED", "noop": True, "report_path": existing["report_path"]}

    if existing is None:
        con.execute("insert into gm_runs (run_id, run_date, status, attempt, started_at, heartbeat_at) "
                    "values (?, ?, 'RUNNING', 1, ?, ?)", (run_id, run_date.isoformat(), ts(), ts()))
        mode = "new"
    else:
        con.execute("update gm_runs set status = 'RUNNING', attempt = attempt + 1, heartbeat_at = ?, "
                    "finished_at = null, error = null where run_id = ?", (ts(), run_id))
        mode = "rerun" if existing["status"] == "COMPLETED" else "resume"
    for seq, s in enumerate(steps, 1):
        con.execute("insert or ignore into gm_steps (run_id, step, seq, status) values (?, ?, ?, 'PENDING')",
                    (run_id, s.name, seq))
    if mode == "rerun":
        con.execute("update gm_steps set status = 'PENDING' where run_id = ?", (run_id,))
    attempt = gm_state.get_run(con, run_id)["attempt"]
    gm_state.log_event(con, "run_started", f"{run_id} attempt {attempt} ({mode})", run_id=run_id, at=ts())
    con.commit()

    ctx = Context(con, run_id, run_date, now, offline, http_get or _http_status, report_dir or REPORT_DIR, tz)
    # Summaries of steps that are already DONE (resume) feed the report.
    for r in gm_state.steps(con, run_id):
        if r["status"] in ("DONE", "SKIPPED") and r["result"]:
            ctx.results[r["step"]] = json.loads(r["result"])

    aborted_by = None
    for s in steps:
        row = con.execute("select status from gm_steps where run_id = ? and step = ?", (run_id, s.name)).fetchone()
        if row["status"] in ("DONE", "SKIPPED") and not s.always:
            continue
        if aborted_by and s.name != "daily_report":
            con.execute("update gm_steps set status = 'SKIPPED', result = ?, finished_at = ? "
                        "where run_id = ? and step = ?",
                        (json.dumps({"reason": f"skipped after critical failure of {aborted_by}"}), ts(), run_id,
                         s.name))
            con.commit()
            continue
        con.execute("update gm_steps set status = 'RUNNING', attempts = attempts + 1, started_at = ?, "
                    "finished_at = null, error = null where run_id = ? and step = ?", (ts(), run_id, s.name))
        con.execute("update gm_runs set heartbeat_at = ? where run_id = ?", (ts(), run_id))
        gm_state.log_event(con, "step_started", s.name, run_id=run_id, at=ts())
        con.commit()
        try:
            out = s.func(ctx)
            if out.status not in ("DONE", "SKIPPED"):
                raise RuntimeError(f"step returned invalid status {out.status!r}")
            ctx.results[s.name] = out.summary
            con.execute("update gm_steps set status = ?, result = ?, finished_at = ? where run_id = ? and step = ?",
                        (out.status, json.dumps(out.summary, ensure_ascii=False, default=str), ts(), run_id, s.name))
            gm_state.log_event(con, f"step_{out.status.lower()}", s.name, run_id=run_id, at=ts())
            con.commit()
        except Exception as e:
            con.rollback()                                  # discard this step's partial writes
            err = f"{type(e).__name__}: {e}"
            con.execute("update gm_steps set status = 'FAILED', error = ?, finished_at = ? "
                        "where run_id = ? and step = ?", (err, ts(), run_id, s.name))
            gm_state.raise_incident(con, f"run:step-failed:{s.name}", "P1" if s.critical else "P2",
                                    f"GM step {s.name} failed", detail=err + "\n"
                                    + "".join(traceback.format_exception_only(type(e), e)).strip(),
                                    source="orchestrator", run_id=run_id, at=ts())
            gm_state.log_event(con, "step_failed", f"{s.name}: {err}", run_id=run_id, at=ts())
            con.commit()
            if s.critical:
                aborted_by = s.name
        else:
            gm_state.resolve_unobserved(con, f"run:step-failed:{s.name}", set(), run_id=run_id, at=ts())
            con.commit()

    final = gm_state.steps(con, run_id)
    status = "FAILED" if aborted_by else ("PARTIAL" if any(r["status"] == "FAILED" for r in final) else "COMPLETED")
    summary = {r["step"]: r["status"] for r in final}
    con.execute("update gm_runs set status = ?, finished_at = ?, heartbeat_at = ?, summary = ?, error = ? "
                "where run_id = ?", (status, ts(), ts(), json.dumps(summary), aborted_by and
                                     f"critical step {aborted_by} failed", run_id))
    gm_state.log_event(con, "run_finished", f"{run_id} {status}", run_id=run_id, data=summary, at=ts())
    con.commit()
    if status == "COMPLETED":
        gm_state.resolve_unobserved(con, f"run:interrupted:{run_id}", set(), run_id=run_id, at=ts())
        con.commit()
    run = gm_state.get_run(con, run_id)
    return {"run_id": run_id, "status": status, "noop": False, "attempt": run["attempt"],
            "steps": summary, "report_path": run["report_path"]}
