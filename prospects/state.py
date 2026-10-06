"""Local pipeline state (SQLite, data/state.sqlite). Never synced anywhere.

domains     every domain ever attempted: revisit interval, 429 cool-down, permanent skip
prospects   latest enrichment + scoring result per domain (JSON) and its queue
kvk_reviews manual KvK verification results entered by the CEO
"""

import datetime as dt
import json
import re
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "state.sqlite"
REVISIT_DAYS = 30
COOLDOWN_429_DAYS = 7
MAX_429 = 2          # after this many 429 responses, skip the domain permanently

SCHEMA = """
create table if not exists domains (
  domain text primary key,
  source text,
  first_seen text not null,
  last_attempt text,
  last_status integer,
  count_429 integer not null default 0,
  skip_reason text
);
create table if not exists prospects (
  domain text primary key,
  queue text not null,
  business_key text,
  public_email text,
  data text not null,
  updated_at text not null
);
create index if not exists prospects_queue on prospects(queue);
create index if not exists prospects_email on prospects(public_email);
create index if not exists prospects_business on prospects(business_key);
create table if not exists kvk_reviews (
  domain text primary key,
  result text not null check (result in ('bv', 'not_bv')),
  kvk_number text,
  note text,
  reviewed_at text not null
);
"""


def now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def business_key(name: str | None, city: str | None) -> str | None:
    """Normalized business identity for cross-source duplicate detection."""
    if not name or not city:
        return None
    n = re.sub(r"\b(b\.?\s?v\.?|v\.?o\.?f\.?|installatiebedrijf|installatietechniek|installaties)\b", " ", name.lower())
    n = re.sub(r"[^a-z0-9]", "", n)
    return f"{n}|{re.sub(r'[^a-z]', '', city.lower())}" if n else None


def is_due(con: sqlite3.Connection, domain: str, today: dt.datetime | None = None) -> bool:
    today = today or dt.datetime.now()
    row = con.execute("select * from domains where domain = ?", (domain,)).fetchone()
    if row is None:
        return True
    if row["skip_reason"]:
        return False
    last = dt.datetime.fromisoformat(row["last_attempt"]) if row["last_attempt"] else None
    if last is None:
        return True
    wait = COOLDOWN_429_DAYS if row["last_status"] == 429 else REVISIT_DAYS
    return today - last >= dt.timedelta(days=wait)


def record_attempt(con: sqlite3.Connection, domain: str, source: str, status: int | None) -> None:
    row = con.execute("select count_429 from domains where domain = ?", (domain,)).fetchone()
    count_429 = (row["count_429"] if row else 0) + (1 if status == 429 else 0)
    skip = f"rate-limited {count_429}x (HTTP 429); skipped permanently" if count_429 >= MAX_429 else None
    con.execute(
        """insert into domains (domain, source, first_seen, last_attempt, last_status, count_429, skip_reason)
           values (?, ?, ?, ?, ?, ?, ?)
           on conflict(domain) do update set last_attempt = excluded.last_attempt,
             last_status = excluded.last_status, count_429 = excluded.count_429,
             skip_reason = coalesce(excluded.skip_reason, domains.skip_reason)""",
        (domain, source, now(), now(), status, count_429, skip),
    )


def find_duplicate(con: sqlite3.Connection, p: dict) -> str | None:
    """An EARLIER-seen domain with the same public email, KvK number, or business name + town.
    Only earlier domains count, so the first domain of a business keeps its place in the queues."""
    seen = con.execute("select first_seen from domains where domain = ?", (p["domain"],)).fetchone()
    first_seen = seen["first_seen"] if seen else now()
    earlier = ("join domains d on d.domain = prospects.domain "
               "where prospects.domain != ? and (d.first_seen < ? or (d.first_seen = ? and prospects.domain < ?))")
    args = (p["domain"], first_seen, first_seen, p["domain"])
    email = (p.get("public_email") or "").lower()
    if email:
        row = con.execute(f"select prospects.domain from prospects {earlier} and lower(public_email) = ?",
                          args + (email,)).fetchone()
        if row:
            return f"same public email as {row['domain']}"
    kvk = p.get("kvk_number")
    if kvk:
        row = con.execute(f"select prospects.domain from prospects {earlier} "
                          "and json_extract(data, '$.kvk_number') = ?", args + (kvk,)).fetchone()
        if row:
            return f"same KvK number as {row['domain']} (likely one company behind several websites)"
    key = business_key(p.get("business_name"), p.get("city"))
    if key:
        row = con.execute(f"select prospects.domain from prospects {earlier} and business_key = ?",
                          args + (key,)).fetchone()
        if row:
            return f"same business name + town as {row['domain']}"
    return None


def save_prospect(con: sqlite3.Connection, p: dict) -> None:
    con.execute(
        """insert into prospects (domain, queue, business_key, public_email, data, updated_at)
           values (?, ?, ?, ?, ?, ?)
           on conflict(domain) do update set queue = excluded.queue, business_key = excluded.business_key,
             public_email = excluded.public_email, data = excluded.data, updated_at = excluded.updated_at""",
        (p["domain"], p["queue"], business_key(p.get("business_name"), p.get("city")),
         p.get("public_email"), json.dumps(p, ensure_ascii=False), now()),
    )


def load_prospects(con: sqlite3.Connection, queue: str | None = None) -> list[dict]:
    sql, args = "select data from prospects", ()
    if queue:
        sql, args = sql + " where queue = ?", (queue,)
    return [json.loads(r["data"]) for r in con.execute(sql, args)]


def record_kvk_review(con: sqlite3.Connection, domain: str, result: str, kvk_number: str | None, note: str | None) -> None:
    con.execute(
        """insert into kvk_reviews (domain, result, kvk_number, note, reviewed_at) values (?, ?, ?, ?, ?)
           on conflict(domain) do update set result = excluded.result, kvk_number = excluded.kvk_number,
             note = excluded.note, reviewed_at = excluded.reviewed_at""",
        (domain, result, kvk_number, note, now()),
    )


def kvk_review(con: sqlite3.Connection, domain: str) -> sqlite3.Row | None:
    return con.execute("select * from kvk_reviews where domain = ?", (domain,)).fetchone()
