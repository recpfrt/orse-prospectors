"""`python run.py gm ...` — GM Core commands (local state only; nothing here contacts anyone or deploys anything).

CEO-only commands (approve / reject / done / ceo-pin / audit, and `decide --by CEO`) refuse to run without an
interactive terminal and ask for the CEO PIN, which the state layer verifies. The GM (Claude Code) runs commands
without a terminal and does not know the PIN, so it can prepare and observe decisions but never make them.
"""

import datetime as dt
import getpass
import json
import sys

from . import gm_run, gm_state


def _open(args):
    import run                      # reuse the CLI's connection handling (--db / $ORSE_PROSPECTS_DB / default)
    con = run._con(args)
    gm_state.ensure_schema(con)
    return con


def cmd_run(args):
    con = _open(args)
    run_date = dt.date.fromisoformat(args.date) if args.date else None
    try:
        res = gm_run.run_daily(con, run_date=run_date, rerun=args.rerun, offline=args.offline)
    except gm_run.GmRunInProgress as e:
        sys.exit(f"[gm] {e}")
    print(json.dumps(res, indent=2))
    if res["status"] in ("FAILED", "PARTIAL"):
        sys.exit(1)


def cmd_status(args):
    con = _open(args)
    runs = gm_state.recent_runs(con, args.limit)
    if not runs:
        print("[gm] no runs yet")
    for r in runs:
        print(f"{r['run_id']}  {r['status']:<9} attempt {r['attempt']}  started {r['started_at']}  "
              f"finished {r['finished_at'] or '-'}  report {r['report_path'] or '-'}")
    if runs:
        print(f"\nsteps of {runs[0]['run_id']}:")
        for s in gm_state.steps(con, runs[0]["run_id"]):
            print(f"  {s['seq']:>2} {s['step']:<20} {s['status']:<8} attempts {s['attempts']}"
                  + (f"  ERROR {s['error']}" if s["error"] else ""))
    p = gm_state.pending_ceo_actions(con)
    print(f"\npending CEO actions: {len(p['approvals_pending'])} approvals, {len(p['approved_not_done'])} approved "
          f"not done, {len(p['incidents'])} incidents")


def cmd_actions(args):
    con = _open(args)
    p = gm_state.pending_ceo_actions(con)
    print("CEO ACTION REQUIRED\n")
    for a in p["approvals_pending"]:
        print(f"#{a['id']:<4} {a['category']:<18} {a['title']}\n      risk: {a['risk'] or '-'}"
              f" | recommendation: {a['recommendation'] or '-'}")
    for a in p["approved_not_done"]:
        stop = gm_state.follow_up_send_blocked(con, a)
        print(f"#{a['id']:<4} APPROVED, not done: {a['title']}" + (f"\n      DO NOT SEND: {stop}" if stop else ""))
    for i in p["incidents"]:
        print(f"incident #{i['id']} [{i['severity']}] {i['title']}" + (f"\n      {i['detail']}" if i["detail"] else ""))
    if not any(p.values()):
        print("nothing")


def cmd_approvals(args):
    con = _open(args)
    rows = gm_state.approvals(con, () if args.all else ("PENDING",))
    for a in rows:
        print(f"#{a['id']:<4} {a['status']:<9} {a['category']:<18} {a['title']}"
              + (f"  [{a['decided_by']} {a['decided_at']}: {a['decision_note'] or ''}]" if a["decided_at"] else ""))
        stop = gm_state.follow_up_send_blocked(con, a)
        if stop:
            print(f"      DO NOT SEND: {stop}")
        if args.details and a["details"]:
            print("      " + a["details"])
    print(f"[gm] {len(rows)} approval requests")


def _ceo_terminal(action: str) -> None:
    """CEO-only commands need a human at an interactive terminal (stdin and stdout both a TTY)."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        sys.exit(f"[gm] refused: '{action}' is a CEO action and must be run by the CEO in their own interactive "
                 f"terminal (e.g. PowerShell or Windows Terminal), not by the GM or a script")


def _ceo_pin(action: str) -> str:
    _ceo_terminal(action)
    return getpass.getpass("CEO PIN: ")


def _decide(decision, command):
    def handler(args):
        con = _open(args)
        row = con.execute("select * from gm_approvals where id = ?", (args.id,)).fetchone()
        if row is None:
            sys.exit(f"[gm] no approval {args.id}")
        _ceo_terminal(command)
        print(f"#{row['id']} {row['category']} — {row['title']}\n  status: {row['status']}\n  risk: {row['risk']}"
              f"\n  recommendation: {row['recommendation']}\n  details: {row['details']}\n")
        pin = getpass.getpass(f"CEO PIN to mark #{row['id']} {decision}: ")
        try:
            row = gm_state.decide_approval(con, args.id, decision, pin=pin, note=args.note)
        except (gm_state.GmStateError, gm_state.CeoAuthError) as e:
            sys.exit(f"[gm] {e}")
        con.commit()
        print(f"[gm] approval #{row['id']} is now {row['status']} (recorded only — nothing was executed)")
    return handler


def cmd_ceo_pin(args):
    con = _open(args)
    _ceo_terminal("ceo-pin")
    old = getpass.getpass("Current CEO PIN: ") if gm_state.ceo_pin_configured(con) else None
    new = getpass.getpass(f"New CEO PIN (at least {gm_state.PIN_MIN_LENGTH} characters): ")
    if getpass.getpass("Repeat new CEO PIN: ") != new:
        sys.exit("[gm] PINs do not match; nothing changed")
    try:
        gm_state.set_ceo_pin(con, new, old)
    except (gm_state.GmStateError, gm_state.CeoAuthError) as e:
        sys.exit(f"[gm] {e}")
    con.commit()
    print("[gm] CEO PIN saved (only a salted hash is stored). Do not share it with the GM.")


def cmd_audit(args):
    con = _open(args)
    pin = _ceo_pin("audit")
    try:
        problems = gm_state.audit_decisions(con, pin)
    except gm_state.CeoAuthError as e:
        sys.exit(f"[gm] {e}")
    for p in problems:
        print("PROBLEM:", p)
    print(f"[gm] audit: {'all CEO decisions verified' if not problems else str(len(problems)) + ' problem(s)'}")
    if problems:
        sys.exit(1)


def cmd_incidents(args):
    con = _open(args)
    rows = gm_state.open_incidents(con, include_resolved=args.all)
    for i in rows:
        print(f"#{i['id']:<4} {i['severity']} {i['status']:<12} {i['title']}  ({i['occurrences']}x, last "
              f"{i['last_seen']}){'  CEO' if i['needs_ceo'] else ''}")
    print(f"[gm] {len(rows)} incidents")


def _incident(status):
    def handler(args):
        con = _open(args)
        try:
            row = gm_state.set_incident_status(con, args.id, status, args.note, by=args.by)
        except gm_state.GmStateError as e:
            sys.exit(f"[gm] {e}")
        con.commit()
        print(f"[gm] incident #{row['id']} is now {row['status']}")
    return handler


def cmd_request(args):
    con = _open(args)
    try:
        row = gm_state.request_approval(con, f"manual:{args.category}:{args.title.strip().lower()}", args.category,
                                        args.title, details=args.details, risk=args.risk,
                                        recommendation=args.recommendation)
    except gm_state.GmStateError as e:
        sys.exit(f"[gm] {e}")
    con.commit()
    print(f"[gm] approval request #{row['id']} {row['status']}: {row['category']} — {row['title']}")


def cmd_decide(args):
    con = _open(args)
    pin = _ceo_pin("decide --by CEO") if args.by.strip().upper() == "CEO" else None
    try:
        did = gm_state.record_decision(con, args.decision, args.reason, recorded_by=args.by, evidence=args.evidence,
                                       alternatives=args.alternatives, expected_outcome=args.expected,
                                       review_condition=args.review, pin=pin)
    except gm_state.CeoAuthError as e:
        sys.exit(f"[gm] {e}")
    con.commit()
    print(f"[gm] decision #{did} recorded")


def cmd_decisions(args):
    con = _open(args)
    for d in gm_state.decisions(con):
        print(f"#{d['id']} {d['decided_at']} ({d['recorded_by']}) {d['decision']}\n    reason: {d['reason']}"
              + "".join(f"\n    {k}: {d[k]}" for k in ("evidence", "alternatives", "expected_outcome",
                                                          "review_condition") if d[k]))


def cmd_events(args):
    con = _open(args)
    for e in gm_state.events(con, args.run, args.limit):
        print(f"{e['at']} {e['run_id'] or '-':<14} {e['kind']:<20} {e['message']}")


def register(sub):
    gm = sub.add_parser("gm", help="GM Core: daily run, CEO approvals, incidents, decisions (local state only)")
    g = gm.add_subparsers(dest="gm_cmd", required=True)
    r = g.add_parser("run", help="run (or resume) today's GM operating loop")
    r.add_argument("--date", help="run date YYYY-MM-DD (default: today)")
    r.add_argument("--rerun", action="store_true", help="run all steps again even if the run completed")
    r.add_argument("--offline", action="store_true", help="skip production HTTP health probes")
    r.set_defaults(func=cmd_run)
    s = g.add_parser("status", help="recent runs, steps of the latest run, pending CEO actions")
    s.add_argument("--limit", type=int, default=5)
    s.set_defaults(func=cmd_status)
    g.add_parser("actions", help="everything waiting on the CEO").set_defaults(func=cmd_actions)
    a = g.add_parser("approvals", help="CEO approval requests")
    a.add_argument("--all", action="store_true")
    a.add_argument("--details", action="store_true")
    a.set_defaults(func=cmd_approvals)
    for name, decision in (("approve", "APPROVED"), ("reject", "REJECTED"), ("done", "DONE")):
        d = g.add_parser(name, help=f"CEO only (terminal + PIN): mark an approval request {decision} "
                                    f"(records only, executes nothing)")
        d.add_argument("id", type=int)
        d.add_argument("--note")
        d.set_defaults(func=_decide(decision, name))
    g.add_parser("ceo-pin", help="CEO only (terminal): set or change the CEO PIN").set_defaults(func=cmd_ceo_pin)
    g.add_parser("audit", help="CEO only (terminal + PIN): verify every recorded CEO decision").set_defaults(
        func=cmd_audit)
    q = g.add_parser("request", help="GM: file an approval request for an action needing the CEO")
    q.add_argument("--category", required=True, choices=sorted(gm_state.APPROVAL_CATEGORIES))
    q.add_argument("--title", required=True)
    q.add_argument("--details")
    q.add_argument("--risk")
    q.add_argument("--recommendation")
    q.set_defaults(func=cmd_request)
    i = g.add_parser("incidents", help="open incidents")
    i.add_argument("--all", action="store_true", help="include resolved")
    i.set_defaults(func=cmd_incidents)
    for name, status in (("ack", "ACKNOWLEDGED"), ("resolve", "RESOLVED")):
        x = g.add_parser(name, help=f"mark an incident {status}")
        x.add_argument("id", type=int)
        x.add_argument("--note", required=True)
        x.add_argument("--by", default="GM")
        x.set_defaults(func=_incident(status))
    dc = g.add_parser("decide", help="record a decision in the append-only decision log")
    dc.add_argument("--decision", required=True)
    dc.add_argument("--reason", required=True)
    dc.add_argument("--evidence")
    dc.add_argument("--alternatives")
    dc.add_argument("--expected")
    dc.add_argument("--review")
    dc.add_argument("--by", default="GM", help="GM (default) or CEO (CEO: terminal + PIN)")
    dc.set_defaults(func=cmd_decide)
    g.add_parser("decisions", help="the decision log").set_defaults(func=cmd_decisions)
    ev = g.add_parser("events", help="the GM audit log")
    ev.add_argument("--run")
    ev.add_argument("--limit", type=int, default=50)
    ev.set_defaults(func=cmd_events)
