"""GM Core state: daily runs, steps, incidents, CEO approvals, decisions and an append-only event log.

Lives in the same local SQLite database as the prospecting state (data/state.sqlite), as separate gm_* tables.
The schema is applied ONLY by GM commands (ensure_schema), never by state.connect(), so opening the database for
prospecting work does not change it. Versioned in gm_meta; additive only (create ... if not exists).

Nothing in this module performs an external action. CEO approval requests are records: the GM prepares them,
the CEO decides, and approval never triggers execution by itself.

CEO boundary: approve / reject / done require the CEO PIN, which only the CEO sets (interactively, see gm_cli).
The state layer itself verifies the PIN, so no code path can decide an approval without it. Each decision stores an
HMAC proof keyed from the PIN; `gm audit` (CEO, with PIN) detects decisions that were edited outside this path.

Timestamps are explicit, in the GM operating timezone (GM_TIMEZONE), with UTC offset.
"""

import datetime as dt
import hashlib
import hmac
import json
import secrets
import sqlite3
from zoneinfo import ZoneInfo

# Operating timezone of the GM (scheduling, run dates, timestamps). Market-specific schedules can pass their own
# IANA zone to timestamp() / operating_tz() / gm_run.run_daily(tz=...) without changing the core.
GM_TIMEZONE = "Europe/Amsterdam"

PIN_ITERATIONS = 200_000
PIN_MIN_LENGTH = 6

GM_SCHEMA_VERSION = 1

RUN_STATUSES = ("RUNNING", "COMPLETED", "PARTIAL", "FAILED", "ABORTED")
STEP_STATUSES = ("PENDING", "RUNNING", "DONE", "SKIPPED", "FAILED")
SEVERITIES = ("P0", "P1", "P2", "P3")
INCIDENT_STATUSES = ("OPEN", "ACKNOWLEDGED", "RESOLVED")
APPROVAL_STATUSES = ("PENDING", "APPROVED", "REJECTED", "WITHDRAWN", "DONE")

# Actions that always require explicit CEO approval (CLAUDE.md §7). The GM may prepare them, never execute them.
APPROVAL_CATEGORIES = {
    "SEND_EMAIL": "send an external email",
    "SEND_FOLLOW_UP": "send a follow-up to a contacted company",
    "CONTACT_PROSPECT": "contact a prospect (first outreach)",
    "CREATE_EXTERNAL_DRAFT": "create a draft in an external mailbox or system",
    "PRODUCTION_DEPLOY": "deploy to production",
    "PRODUCTION_DB_CHANGE": "change production data or schema",
    "ENABLE_EXTERNAL_WORKFLOW": "enable an automation with external effects (e.g. n8n)",
    "SPEND_MONEY": "spend money",
    "PAID_SERVICE": "enable or subscribe to a paid service or API",
    "PRICING_CHANGE": "change pricing",
    "LEGAL_COMMITMENT": "contract, agreement or legal commitment",
    "DOMAIN_PURCHASE": "purchase a domain",
    "DESTRUCTIVE_OPERATION": "delete data or perform a destructive operation",
    "CREDENTIAL_CHANGE": "change credentials, secrets or environment variables",
    "POLICY_CHANGE": "change a CEO business policy (e.g. who may be emailed)",
    "IRREVERSIBLE_EXTERNAL_ACTION": "any other irreversible external action",
}

# Allowed approval moves. GM-side: request (-> PENDING), withdraw (PENDING -> WITHDRAWN), re-request
# (WITHDRAWN -> PENDING). CEO-side: approve / reject a PENDING request, mark an APPROVED one DONE after doing it.
APPROVAL_TRANSITIONS = {
    "PENDING": {"APPROVED", "REJECTED", "WITHDRAWN"},
    "APPROVED": {"DONE"},
    "WITHDRAWN": {"PENDING"},
    "REJECTED": set(),
    "DONE": set(),
}

GM_SCHEMA = """
create table if not exists gm_meta (key text primary key, value text not null);
create table if not exists gm_runs (
  run_id text primary key,
  run_date text not null,
  status text not null check (status in ('RUNNING','COMPLETED','PARTIAL','FAILED','ABORTED')),
  attempt integer not null default 1,
  started_at text not null,
  heartbeat_at text not null,
  finished_at text,
  summary text,
  report_path text,
  report_sha256 text,
  error text
);
create table if not exists gm_steps (
  run_id text not null references gm_runs(run_id),
  step text not null,
  seq integer not null,
  status text not null check (status in ('PENDING','RUNNING','DONE','SKIPPED','FAILED')),
  attempts integer not null default 0,
  started_at text,
  finished_at text,
  result text,
  error text,
  primary key (run_id, step)
);
create table if not exists gm_incidents (
  id integer primary key autoincrement,
  fingerprint text not null unique,
  severity text not null check (severity in ('P0','P1','P2','P3')),
  title text not null check (length(trim(title)) > 0),
  detail text,
  source text,
  needs_ceo integer not null default 0 check (needs_ceo in (0, 1)),
  status text not null check (status in ('OPEN','ACKNOWLEDGED','RESOLVED')),
  first_seen text not null,
  last_seen text not null,
  occurrences integer not null default 1,
  first_run_id text,
  last_run_id text,
  resolved_at text,
  resolution text
);
create table if not exists gm_approvals (
  id integer primary key autoincrement,
  fingerprint text not null unique,
  category text not null,
  title text not null check (length(trim(title)) > 0),
  details text,
  risk text,
  recommendation text,
  status text not null check (status in ('PENDING','APPROVED','REJECTED','WITHDRAWN','DONE')),
  requested_at text not null,
  updated_at text not null,
  run_id text,
  decided_at text,
  decided_by text,
  decision_note text,
  decision_proof text
);
create table if not exists gm_decisions (
  id integer primary key autoincrement,
  decided_at text not null,
  decision text not null check (length(trim(decision)) > 0),
  reason text not null check (length(trim(reason)) > 0),
  evidence text,
  alternatives text,
  expected_outcome text,
  review_condition text,
  recorded_by text not null
);
create table if not exists gm_events (
  id integer primary key autoincrement,
  at text not null,
  run_id text,
  kind text not null,
  ref text,
  message text not null,
  data text
);
create index if not exists gm_events_run on gm_events(run_id);
create index if not exists gm_approvals_status on gm_approvals(status);
create index if not exists gm_incidents_status on gm_incidents(status);
create trigger if not exists gm_events_no_update before update on gm_events
  begin select raise(abort, 'gm_events is append-only'); end;
create trigger if not exists gm_events_no_delete before delete on gm_events
  begin select raise(abort, 'gm_events is append-only'); end;
create trigger if not exists gm_decisions_no_update before update on gm_decisions
  begin select raise(abort, 'gm_decisions is append-only'); end;
create trigger if not exists gm_decisions_no_delete before delete on gm_decisions
  begin select raise(abort, 'gm_decisions is append-only'); end;
"""


class GmStateError(ValueError):
    pass


class CeoAuthError(PermissionError):
    """A CEO-only action was attempted without a valid CEO PIN."""


def operating_tz(name: str | None = None) -> ZoneInfo:
    return ZoneInfo(name or GM_TIMEZONE)


def timestamp(when: dt.datetime | None = None, tz: str | None = None) -> str:
    """ISO timestamp in the operating timezone, with UTC offset. Naive datetimes are taken as machine-local."""
    when = when or dt.datetime.now(dt.timezone.utc)
    return when.astimezone(operating_tz(tz)).isoformat(timespec="seconds")


def ensure_schema(con: sqlite3.Connection) -> None:
    """Create the gm_* tables if missing (additive, idempotent) and record the schema version."""
    con.executescript(GM_SCHEMA)
    row = con.execute("select value from gm_meta where key = 'schema_version'").fetchone()
    if row is None:
        con.execute("insert into gm_meta (key, value) values ('schema_version', ?)", (str(GM_SCHEMA_VERSION),))
        con.execute("insert into gm_meta (key, value) values ('schema_applied_at', ?)", (timestamp(),))
    elif int(row[0]) > GM_SCHEMA_VERSION:
        raise GmStateError(f"database has GM schema v{row[0]}, this code knows v{GM_SCHEMA_VERSION}")
    con.commit()


def _json(value) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def fingerprint(*parts: str) -> str:
    """Stable identity for a recurring incident/approval (readable prefix + short hash of the parts)."""
    raw = "|".join(str(p) for p in parts)
    return f"{parts[0]}:{hashlib.sha256(raw.encode()).hexdigest()[:12]}" if len(raw) > 120 else raw


# ---------------------------------------------------------------- CEO PIN (only the CEO knows it)

def _derive(pin: str, salt_hex: str, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), bytes.fromhex(salt_hex), iterations)


def _meta(con: sqlite3.Connection, key: str) -> str | None:
    row = con.execute("select value from gm_meta where key = ?", (key,)).fetchone()
    return row[0] if row else None


def ceo_pin_configured(con: sqlite3.Connection) -> bool:
    return _meta(con, "ceo_pin") is not None


def verify_ceo_pin(con: sqlite3.Connection, pin: str | None) -> bool:
    stored = _meta(con, "ceo_pin")
    if not stored or not pin:
        return False
    _, iterations, salt, digest = stored.split("$")
    return hmac.compare_digest(_derive(pin, salt, int(iterations)).hex(), digest)


def _require_ceo(con: sqlite3.Connection, pin: str | None) -> None:
    if not ceo_pin_configured(con):
        raise CeoAuthError("no CEO PIN configured: the CEO must run `python run.py gm ceo-pin` in their own terminal")
    if not verify_ceo_pin(con, pin):
        raise CeoAuthError("CEO PIN rejected")


def _proof(con: sqlite3.Connection, pin: str, payload: str) -> str:
    stored = _meta(con, "ceo_pin")
    iterations = int(stored.split("$")[1])
    key = _derive(pin, _meta(con, "ceo_proof_salt"), iterations)
    return hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def set_ceo_pin(con: sqlite3.Connection, new_pin: str, old_pin: str | None = None) -> None:
    """First setup, or change with the current PIN. Stores only a salted PBKDF2 verifier, never the PIN."""
    changed = ceo_pin_configured(con)
    if changed:
        _require_ceo(con, old_pin)
    if len(new_pin or "") < PIN_MIN_LENGTH:
        raise GmStateError(f"the CEO PIN must have at least {PIN_MIN_LENGTH} characters")
    old_payloads = []
    if changed:   # proofs are keyed from the PIN: re-sign verified existing decisions under the new PIN
        old_payloads = _verified_payloads(con, old_pin)
    salt = secrets.token_hex(16)
    verifier = f"pbkdf2_sha256${PIN_ITERATIONS}${salt}${_derive(new_pin, salt, PIN_ITERATIONS).hex()}"
    con.execute("insert into gm_meta (key, value) values ('ceo_pin', ?) "
                "on conflict(key) do update set value = excluded.value", (verifier,))
    con.execute("insert into gm_meta (key, value) values ('ceo_proof_salt', ?) "
                "on conflict(key) do update set value = excluded.value", (secrets.token_hex(16),))
    log_event(con, "ceo_pin_changed" if changed else "ceo_pin_set", "CEO PIN " + ("changed" if changed else "set"))
    for payload in old_payloads:
        log_event(con, "approval_proof_resigned", payload, data={"payload": payload,
                                                                 "proof": _proof(con, new_pin, payload)})


# ---------------------------------------------------------------- events (append-only audit log)

def log_event(con: sqlite3.Connection, kind: str, message: str, *, run_id: str | None = None,
              ref: str | None = None, data=None, at: str | None = None) -> None:
    con.execute("insert into gm_events (at, run_id, kind, ref, message, data) values (?, ?, ?, ?, ?, ?)",
                (at or timestamp(), run_id, kind, ref, message, _json(data)))


def events(con: sqlite3.Connection, run_id: str | None = None, limit: int = 200) -> list[sqlite3.Row]:
    if run_id:
        return con.execute("select * from gm_events where run_id = ? order by id", (run_id,)).fetchall()
    return con.execute("select * from gm_events order by id desc limit ?", (limit,)).fetchall()[::-1]


# ---------------------------------------------------------------- incidents

def raise_incident(con: sqlite3.Connection, fp: str, severity: str, title: str, *, detail: str | None = None,
                   source: str | None = None, needs_ceo: bool = False, run_id: str | None = None,
                   at: str | None = None) -> dict:
    """Record a detected problem. Re-detection updates the same row (occurrences, last_seen) instead of
    duplicating it; a RESOLVED incident that is detected again is reopened."""
    if severity not in SEVERITIES:
        raise GmStateError(f"unknown severity {severity!r}")
    ts = at or timestamp()
    row = con.execute("select * from gm_incidents where fingerprint = ?", (fp,)).fetchone()
    if row is None:
        con.execute("""insert into gm_incidents (fingerprint, severity, title, detail, source, needs_ceo, status,
                         first_seen, last_seen, occurrences, first_run_id, last_run_id)
                       values (?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, 1, ?, ?)""",
                    (fp, severity, title, detail, source, int(needs_ceo), ts, ts, run_id, run_id))
        log_event(con, "incident_opened", f"[{severity}] {title}", run_id=run_id, ref=fp, at=ts)
    else:
        status = row["status"]
        if status == "RESOLVED":
            status = "OPEN"
            log_event(con, "incident_reopened", f"[{severity}] {title}", run_id=run_id, ref=fp, at=ts)
        if row["last_run_id"] == run_id and run_id is not None and row["status"] != "RESOLVED":
            occurrences = row["occurrences"]          # same run re-detecting (resume/rerun step): count once
        else:
            occurrences = row["occurrences"] + 1
        con.execute("""update gm_incidents set severity = ?, title = ?, detail = ?, source = ?, needs_ceo = ?,
                         status = ?, last_seen = ?, occurrences = ?, last_run_id = ?,
                         resolved_at = case when ? = 'OPEN' and status = 'RESOLVED' then null else resolved_at end,
                         resolution = case when ? = 'OPEN' and status = 'RESOLVED' then null else resolution end
                       where fingerprint = ?""",
                    (severity, title, detail, source, int(needs_ceo), status, ts, occurrences, run_id,
                     status, status, fp))
    return dict(con.execute("select * from gm_incidents where fingerprint = ?", (fp,)).fetchone())


def set_incident_status(con: sqlite3.Connection, incident_id: int, status: str, note: str,
                        by: str, at: str | None = None) -> dict:
    if status not in ("ACKNOWLEDGED", "RESOLVED"):
        raise GmStateError("incident status can be set to ACKNOWLEDGED or RESOLVED")
    if not note or not note.strip():
        raise GmStateError("a note is required")
    row = con.execute("select * from gm_incidents where id = ?", (incident_id,)).fetchone()
    if row is None:
        raise GmStateError(f"no incident {incident_id}")
    if row["status"] == "RESOLVED":
        raise GmStateError(f"incident {incident_id} is already resolved")
    ts = at or timestamp()
    con.execute("""update gm_incidents set status = ?, resolved_at = ?, resolution = ? where id = ?""",
                (status, ts if status == "RESOLVED" else None, note if status == "RESOLVED" else row["resolution"],
                 incident_id))
    log_event(con, f"incident_{status.lower()}", f"{row['title']} — {note} (by {by})", ref=row["fingerprint"], at=ts)
    return dict(con.execute("select * from gm_incidents where id = ?", (incident_id,)).fetchone())


def resolve_unobserved(con: sqlite3.Connection, prefix: str, observed: set[str], *, run_id: str | None,
                       at: str | None = None) -> list[str]:
    """A detector that completed successfully resolves its own open incidents it no longer observes."""
    ts = at or timestamp()
    rows = con.execute("select fingerprint, title from gm_incidents where status != 'RESOLVED' "
                       "and fingerprint like ? escape '\\'", (prefix.replace("%", "\\%").replace("_", "\\_") + "%",)
                       ).fetchall()
    resolved = []
    for r in rows:
        if r["fingerprint"] not in observed:
            con.execute("update gm_incidents set status = 'RESOLVED', resolved_at = ?, "
                        "resolution = 'no longer detected' where fingerprint = ?", (ts, r["fingerprint"]))
            log_event(con, "incident_resolved", f"{r['title']} — no longer detected", run_id=run_id,
                      ref=r["fingerprint"], at=ts)
            resolved.append(r["fingerprint"])
    return resolved


def open_incidents(con: sqlite3.Connection, include_resolved: bool = False) -> list[sqlite3.Row]:
    sql = "select * from gm_incidents" + ("" if include_resolved else " where status != 'RESOLVED'")
    return con.execute(sql + " order by severity, last_seen desc").fetchall()


# ---------------------------------------------------------------- CEO approvals

def request_approval(con: sqlite3.Connection, fp: str, category: str, title: str, *, details=None,
                     risk: str | None = None, recommendation: str | None = None, run_id: str | None = None,
                     at: str | None = None) -> dict:
    """Prepare an action for the CEO. Idempotent per fingerprint: a PENDING request is refreshed, a WITHDRAWN one
    becomes PENDING again, a decided one (APPROVED / REJECTED / DONE) is left exactly as the CEO decided it."""
    if category not in APPROVAL_CATEGORIES:
        raise GmStateError(f"unknown approval category {category!r}")
    ts = at or timestamp()
    row = con.execute("select * from gm_approvals where fingerprint = ?", (fp,)).fetchone()
    if row is None:
        con.execute("""insert into gm_approvals (fingerprint, category, title, details, risk, recommendation, status,
                         requested_at, updated_at, run_id) values (?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?)""",
                    (fp, category, title, _json(details), risk, recommendation, ts, ts, run_id))
        log_event(con, "approval_requested", f"{category}: {title}", run_id=run_id, ref=fp, at=ts)
    elif row["status"] in ("PENDING", "WITHDRAWN"):
        if row["status"] == "WITHDRAWN":
            log_event(con, "approval_rerequested", f"{category}: {title}", run_id=run_id, ref=fp, at=ts)
        con.execute("""update gm_approvals set category = ?, title = ?, details = ?, risk = ?, recommendation = ?,
                         status = 'PENDING', updated_at = ?, run_id = ? where fingerprint = ?""",
                    (category, title, _json(details), risk, recommendation, ts, run_id, fp))
    return dict(con.execute("select * from gm_approvals where fingerprint = ?", (fp,)).fetchone())


def _move_approval(con, row, new_status: str, *, by: str, note: str | None, run_id: str | None, at: str,
                   proof: dict | None = None) -> dict:
    if new_status not in APPROVAL_TRANSITIONS[row["status"]]:
        raise GmStateError(f"approval {row['id']}: {row['status']} -> {new_status} is not allowed")
    decided = new_status in ("APPROVED", "REJECTED")
    sig = proof["proof"] if proof else None
    con.execute("""update gm_approvals set status = ?, updated_at = ?,
                     decided_at = case when ? then ? else decided_at end,
                     decided_by = case when ? then ? else decided_by end,
                     decision_note = coalesce(?, decision_note),
                     decision_proof = coalesce(?, decision_proof) where id = ?""",
                (new_status, at, decided, at, decided, by, note, sig, row["id"]))
    log_event(con, f"approval_{new_status.lower()}", f"{row['category']}: {row['title']}"
              + (f" — {note}" if note else "") + f" (by {by})", run_id=run_id, ref=row["fingerprint"], at=at,
              data=proof)
    return dict(con.execute("select * from gm_approvals where id = ?", (row["id"],)).fetchone())


def decide_approval(con: sqlite3.Connection, approval_id: int, decision: str, *, pin: str | None,
                    note: str | None = None, at: str | None = None) -> dict:
    """CEO decision: APPROVED / REJECTED (from PENDING) or DONE (CEO carried out an APPROVED action).
    Requires the CEO PIN. Recording a decision never executes anything."""
    if decision not in ("APPROVED", "REJECTED", "DONE"):
        raise GmStateError("decision must be APPROVED, REJECTED or DONE")
    _require_ceo(con, pin)
    row = con.execute("select * from gm_approvals where id = ?", (approval_id,)).fetchone()
    if row is None:
        raise GmStateError(f"no approval {approval_id}")
    ts = at or timestamp()
    payload = f"{row['id']}|{row['fingerprint']}|{decision}|{ts}|{note or ''}"
    return _move_approval(con, row, decision, by="CEO", note=note, run_id=None, at=ts,
                          proof={"payload": payload, "proof": _proof(con, pin, payload)})


def unproven_decisions(con: sqlite3.Connection) -> list[dict]:
    """Decided approvals with no CEO proof recorded (e.g. edited directly in the database). Needs no PIN."""
    return [dict(r) for r in con.execute(
        "select * from gm_approvals where status in ('APPROVED','REJECTED','DONE') and decision_proof is null")]


def _verified_payloads(con: sqlite3.Connection, pin: str) -> list[str]:
    """Decision payloads whose proof verifies under the current PIN (latest proof per payload counts)."""
    latest = {}
    for e in con.execute("select data from gm_events where kind in ('approval_approved', 'approval_rejected', "
                         "'approval_done', 'approval_proof_resigned') order by id"):
        data = json.loads(e[0]) if e[0] else {}
        if data.get("payload") and data.get("proof"):
            latest[data["payload"]] = data["proof"]
    return [p for p, sig in latest.items() if hmac.compare_digest(_proof(con, pin, p), sig)]


def audit_decisions(con: sqlite3.Connection, pin: str) -> list[str]:
    """CEO audit: every decided approval's current status must be backed by a decision event whose PIN-keyed proof
    verifies. Returns the problems found (empty = all decisions genuine)."""
    _require_ceo(con, pin)
    proven = {}
    for payload in _verified_payloads(con, pin):
        aid, _, decision, _, _ = payload.split("|", 4)
        proven.setdefault(int(aid), set()).add(decision)
    problems = []
    for e in con.execute("select ref, kind, data from gm_events where kind in "
                         "('approval_approved', 'approval_rejected', 'approval_done')"):
        if not e["data"] or "proof" not in json.loads(e["data"]):
            problems.append(f"event {e['kind']} for {e['ref']} has no CEO proof")
    for r in con.execute("select * from gm_approvals where status in ('APPROVED','REJECTED','DONE')"):
        if r["status"] not in proven.get(r["id"], set()):
            problems.append(f"approval #{r['id']} is {r['status']} without a valid CEO decision")
    return problems


def withdraw_unrequested(con: sqlite3.Connection, prefix: str, still_wanted: set[str], *, reason: str,
                         run_id: str | None, at: str | None = None) -> list[str]:
    """GM-side: withdraw PENDING requests with this prefix that the latest run no longer prepares."""
    ts = at or timestamp()
    rows = con.execute("select * from gm_approvals where status = 'PENDING' and fingerprint like ? escape '\\'",
                       (prefix.replace("%", "\\%").replace("_", "\\_") + "%",)).fetchall()
    out = []
    for r in rows:
        if r["fingerprint"] not in still_wanted:
            _move_approval(con, r, "WITHDRAWN", by="GM", note=reason, run_id=run_id, at=ts)
            out.append(r["fingerprint"])
    return out


def approvals(con: sqlite3.Connection, statuses=("PENDING",)) -> list[sqlite3.Row]:
    if not statuses:
        return con.execute("select * from gm_approvals order by id").fetchall()
    marks = ",".join("?" * len(statuses))
    return con.execute(f"select * from gm_approvals where status in ({marks}) order by category, id",
                       tuple(statuses)).fetchall()


# ---------------------------------------------------------------- decisions (append-only)

def record_decision(con: sqlite3.Connection, decision: str, reason: str, *, recorded_by: str,
                    evidence: str | None = None, alternatives: str | None = None,
                    expected_outcome: str | None = None, review_condition: str | None = None,
                    at: str | None = None, pin: str | None = None) -> int:
    """Append to the decision log. Recording a decision as the CEO requires the CEO PIN."""
    if recorded_by.strip().upper() == "CEO":
        _require_ceo(con, pin)
    ts = at or timestamp()
    cur = con.execute("""insert into gm_decisions (decided_at, decision, reason, evidence, alternatives,
                           expected_outcome, review_condition, recorded_by) values (?, ?, ?, ?, ?, ?, ?, ?)""",
                      (ts, decision, reason, evidence, alternatives, expected_outcome, review_condition, recorded_by))
    log_event(con, "decision_recorded", decision, ref=f"decision:{cur.lastrowid}", at=ts)
    return cur.lastrowid


def decisions(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute("select * from gm_decisions order by id").fetchall()


# ---------------------------------------------------------------- pending CEO actions (derived, not stored twice)

def follow_up_send_blocked(con: sqlite3.Connection, approval) -> str | None:
    """Why an APPROVED follow-up must NOT be sent any more (the prospect replied, opted out or the record is gone
    since the CEO approved it), else None. Decided approvals cannot be withdrawn, so this is checked on display."""
    if approval["category"] != "SEND_FOLLOW_UP" or approval["status"] != "APPROVED":
        return None
    domain = (json.loads(approval["details"]) if approval["details"] else {}).get("domain")
    row = con.execute("select status from outreach where domain = ?", (domain,)).fetchone()
    if row is None:
        return f"no outreach record for {domain}"
    if row["status"] not in ("CONTACTED", "FOLLOW_UP"):
        return f"{domain} is now {row['status']} (reply/opt-out after approval)"
    return None


def pending_ceo_actions(con: sqlite3.Connection) -> dict:
    """Everything waiting on the CEO: undecided approval requests, approved actions the CEO has not carried out
    yet, and open incidents that need a CEO decision or are P0/P1."""
    return {
        "approvals_pending": [dict(r) for r in approvals(con, ("PENDING",))],
        "approved_not_done": [dict(r) for r in approvals(con, ("APPROVED",))],
        "incidents": [dict(r) for r in con.execute(
            "select * from gm_incidents where status = 'OPEN' and (needs_ceo = 1 or severity in ('P0','P1')) "
            "order by severity, id").fetchall()],
    }


# ---------------------------------------------------------------- runs

def get_run(con: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
    return con.execute("select * from gm_runs where run_id = ?", (run_id,)).fetchone()


def steps(con: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return con.execute("select * from gm_steps where run_id = ? order by seq", (run_id,)).fetchall()


def recent_runs(con: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    return con.execute("select * from gm_runs order by run_date desc, started_at desc limit ?", (limit,)).fetchall()
