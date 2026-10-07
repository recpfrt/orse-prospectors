"""Local outreach tracking (CEO sends every email by hand; this module never contacts anyone).

Stored in the local state database (git-ignored). No mailbox credentials or message bodies are stored:
only company, public role email, dates, subject, a message-version label, status and notes.

Any status other than NOT_CONTACTED removes the company from new-prospect recommendations, matched by
domain, public email, email domain, KvK number or business name + town (see `contact_match`).
"""

import datetime as dt
import re
import sqlite3

from . import state

NOT_CONTACTED = "NOT_CONTACTED"
CONTACTED = "CONTACTED"
REPLIED = "REPLIED"
INTERESTED = "INTERESTED"
NOT_INTERESTED = "NOT_INTERESTED"
FOLLOW_UP = "FOLLOW_UP"          # a follow-up was sent by the CEO; awaiting a reply
PILOT = "PILOT"
CUSTOMER = "CUSTOMER"
DO_NOT_CONTACT = "DO_NOT_CONTACT"

STATUSES = (NOT_CONTACTED, CONTACTED, REPLIED, INTERESTED, NOT_INTERESTED, FOLLOW_UP, PILOT, CUSTOMER,
            DO_NOT_CONTACT)

# Allowed moves. DO_NOT_CONTACT is reachable from every status and is final.
TRANSITIONS = {
    NOT_CONTACTED: {CONTACTED},
    CONTACTED: {REPLIED, INTERESTED, NOT_INTERESTED, FOLLOW_UP},
    FOLLOW_UP: {REPLIED, INTERESTED, NOT_INTERESTED, FOLLOW_UP},
    REPLIED: {INTERESTED, NOT_INTERESTED, FOLLOW_UP, PILOT},
    INTERESTED: {FOLLOW_UP, PILOT, CUSTOMER, NOT_INTERESTED},
    PILOT: {CUSTOMER, NOT_INTERESTED, FOLLOW_UP},
    NOT_INTERESTED: {INTERESTED, FOLLOW_UP},
    CUSTOMER: set(),
    DO_NOT_CONTACT: set(),
}
for _s in STATUSES:
    if _s != DO_NOT_CONTACT:
        TRANSITIONS[_s] = TRANSITIONS[_s] | {DO_NOT_CONTACT}

REPLY_TYPES = ("none", "positive", "question", "not_now", "not_interested", "unsubscribe", "bounce",
               "auto_reply", "other")

_UNUSED_SCHEMA_DOC = """(created by state.SCHEMA)
create table if not exists outreach (
  domain text primary key,
  company text not null check (length(trim(company)) > 0),
  email text,
  contacted_at text,
  subject text,
  message_version text,
  status text not null,
  follow_up_at text,
  reply_type text,
  next_action text,
  notes text,
  created_at text not null,
  updated_at text not null
);
create table if not exists outreach_events (
  id integer primary key autoincrement,
  domain text not null,
  at text not null,
  from_status text,
  to_status text not null,
  note text
);
"""

EMAIL_RE = re.compile(r"^[^\s@<>\",;]+@[^\s@<>\",;]+\.[a-z]{2,}$", re.I)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class OutreachError(ValueError):
    pass


def ensure_schema(con: sqlite3.Connection) -> None:
    """Tables are created by state.connect(); kept for callers that hold an older connection."""
    exists = con.execute("select 1 from sqlite_master where type='table' and name='outreach'").fetchone()
    if not exists:
        raise OutreachError("outreach table missing: open the database with state.connect()")


def _norm_domain(domain: str) -> str:
    d = domain.strip().lower()
    d = re.sub(r"^https?://", "", d).split("/")[0]
    return d[4:] if d.startswith("www.") else d


def _check_date(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    if not DATE_RE.match(value):
        raise OutreachError(f"{field} must be YYYY-MM-DD, got {value!r}")
    dt.date.fromisoformat(value)
    return value


def get(con: sqlite3.Connection, domain: str) -> sqlite3.Row | None:
    ensure_schema(con)
    return con.execute("select * from outreach where domain = ?", (_norm_domain(domain),)).fetchone()


def record_contact(con: sqlite3.Connection, domain: str, company: str, email: str | None, contacted_at: str,
                   subject: str | None = None, message_version: str | None = None,
                   follow_up_at: str | None = None, next_action: str | None = None,
                   notes: str | None = None) -> dict:
    """The CEO sent a first email by hand. Creates the record (or moves NOT_CONTACTED -> CONTACTED)."""
    ensure_schema(con)
    domain = _norm_domain(domain)
    if email is not None and not EMAIL_RE.match(email.strip()):
        raise OutreachError(f"not a single plain email address: {email!r}")
    _check_date(contacted_at, "contacted_at")
    _check_date(follow_up_at, "follow_up_at")
    existing = get(con, domain)
    if existing and existing["status"] != NOT_CONTACTED:
        raise OutreachError(f"{domain} is already {existing['status']}; use a status change instead")
    ts = state.now()
    con.execute(
        """insert into outreach (domain, company, email, contacted_at, subject, message_version, status,
                                 follow_up_at, reply_type, next_action, notes, created_at, updated_at)
           values (?, ?, ?, ?, ?, ?, ?, ?, 'none', ?, ?, ?, ?)
           on conflict(domain) do update set company = excluded.company, email = excluded.email,
             contacted_at = excluded.contacted_at, subject = excluded.subject,
             message_version = excluded.message_version, status = excluded.status,
             follow_up_at = excluded.follow_up_at, reply_type = excluded.reply_type,
             next_action = excluded.next_action, notes = excluded.notes, updated_at = excluded.updated_at""",
        (domain, company.strip(), email.strip().lower() if email else None, contacted_at, subject,
         message_version, CONTACTED, follow_up_at, next_action, notes, ts, ts))
    con.execute("insert into outreach_events (domain, at, from_status, to_status, note) values (?, ?, ?, ?, ?)",
                (domain, ts, existing["status"] if existing else None, CONTACTED, f"first contact {contacted_at}"))
    return dict(get(con, domain))


def add_not_contacted(con: sqlite3.Connection, domain: str, company: str, email: str | None = None,
                      notes: str | None = None) -> dict:
    """Track a company before contact (e.g. selected for a draft). Does NOT exclude it from prospect lists."""
    ensure_schema(con)
    domain = _norm_domain(domain)
    if get(con, domain):
        raise OutreachError(f"{domain} is already tracked")
    ts = state.now()
    con.execute("""insert into outreach (domain, company, email, status, notes, created_at, updated_at)
                   values (?, ?, ?, ?, ?, ?, ?)""",
                (domain, company.strip(), email.strip().lower() if email else None, NOT_CONTACTED, notes, ts, ts))
    con.execute("insert into outreach_events (domain, at, from_status, to_status, note) values (?, ?, ?, ?, ?)",
                (domain, ts, None, NOT_CONTACTED, notes))
    return dict(get(con, domain))


def set_status(con: sqlite3.Connection, domain: str, new_status: str, *, reply_type: str | None = None,
               next_action: str | None = None, follow_up_at: str | None = None, note: str | None = None) -> dict:
    ensure_schema(con)
    domain = _norm_domain(domain)
    row = get(con, domain)
    if row is None:
        raise OutreachError(f"{domain} is not tracked; record the first contact first")
    if new_status not in STATUSES:
        raise OutreachError(f"unknown status {new_status!r}")
    if new_status == CONTACTED and row["status"] == NOT_CONTACTED:
        raise OutreachError("use record_contact for the first contact (it needs the contact date)")
    if new_status not in TRANSITIONS[row["status"]]:
        raise OutreachError(f"transition {row['status']} -> {new_status} is not allowed")
    if reply_type is not None and reply_type not in REPLY_TYPES:
        raise OutreachError(f"unknown reply type {reply_type!r}")
    _check_date(follow_up_at, "follow_up_at")
    ts = state.now()
    notes = row["notes"]
    if note:
        notes = (notes + "\n" if notes else "") + f"[{ts[:10]}] {note}"
    con.execute("""update outreach set status = ?, reply_type = coalesce(?, reply_type),
                     next_action = coalesce(?, next_action), follow_up_at = coalesce(?, follow_up_at),
                     notes = ?, updated_at = ? where domain = ?""",
                (new_status, reply_type, next_action, follow_up_at, notes, ts, domain))
    con.execute("insert into outreach_events (domain, at, from_status, to_status, note) values (?, ?, ?, ?, ?)",
                (domain, ts, row["status"], new_status, note))
    return dict(get(con, domain))


def tracked(con: sqlite3.Connection, include_not_contacted: bool = False) -> list[sqlite3.Row]:
    ensure_schema(con)
    sql = "select * from outreach"
    if not include_not_contacted:
        sql += f" where status != '{NOT_CONTACTED}'"
    return con.execute(sql + " order by contacted_at, domain").fetchall()


def excluded_domains(con: sqlite3.Connection) -> set[str]:
    """Domains that must never be selected as new prospects (anything past NOT_CONTACTED)."""
    return {r["domain"] for r in tracked(con)}


def _registrable(host: str) -> str:
    host = host.lower().strip(".")
    host = host[4:] if host.startswith("www.") else host
    return ".".join(host.split(".")[-2:])


def contact_match(con: sqlite3.Connection, p: dict) -> tuple[sqlite3.Row, str] | None:
    """The outreach record this prospect belongs to (status past NOT_CONTACTED), and how it matched."""
    rows = tracked(con)
    if not rows:
        return None
    domain = _norm_domain(p.get("domain") or "")
    email = (p.get("public_email") or "").lower()
    email_dom = _registrable(email.split("@", 1)[1]) if "@" in email else None
    kvk = p.get("kvk_number") or p.get("kvk_review_number")
    key = state.business_key(p.get("business_name"), p.get("city"))
    for r in rows:
        if r["domain"] == domain:
            return r, "same domain"
    for r in rows:
        if email and r["email"] and r["email"] == email:
            return r, f"same email as contacted {r['domain']}"
        if email_dom and email_dom == _registrable(r["domain"]):
            return r, f"email domain belongs to contacted {r['domain']}"
    for r in rows:
        other = con.execute("select business_key, data from prospects where domain = ?", (r["domain"],)).fetchone()
        if not other:
            continue
        if kvk:
            import json
            o = json.loads(other["data"])
            if kvk in {o.get("kvk_number"), o.get("kvk_review_number")} - {None}:
                return r, f"same KvK number as contacted {r['domain']}"
        if key and other["business_key"] == key:
            return r, f"same business name + town as contacted {r['domain']}"
    return None
