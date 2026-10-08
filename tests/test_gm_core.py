"""GM Core: state layer (runs, incidents, approvals, decisions, events) and the daily-run orchestration.
All tests use temporary databases, a fixed clock and a fake HTTP client; the real data/state.sqlite must stay
byte-identical and nothing may reach the network."""
from tests import _isolation  # noqa: F401  (must be first)

import datetime as dt
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from prospects import gm_run, gm_state, outreach, pipeline, state
from prospects.score import score
from tests.test_pipeline import base
from tests.test_state_isolation import fingerprint

TZ = dt.timezone(dt.timedelta(hours=2))
DAY = dt.date(2026, 10, 8)


def clock(start=dt.datetime(2026, 10, 8, 7, 0, tzinfo=TZ)):
    t = [start]

    def now():
        t[0] = t[0] + dt.timedelta(seconds=1)
        return t[0]
    return now


def ready(domain, **kw):
    kw.setdefault("public_email", f"info@{domain}")
    kw.setdefault("business_name", domain.split(".")[0].title())
    kw.setdefault("city", "Utrecht")
    return base(customer_type="homeowner", domain=domain, website=f"https://{domain}/", **kw)


def no_network(*a, **kw):
    raise AssertionError("network access attempted")


class GmCase(unittest.TestCase):
    def setUp(self):
        self.real_before = fingerprint(state.REAL_DB_PATH)
        self.tmp = tempfile.TemporaryDirectory()
        self.con = state.connect(Path(self.tmp.name) / "gm.sqlite")
        self.reports = Path(self.tmp.name) / "reports"
        self.net = mock.patch("urllib.request.urlopen", side_effect=no_network)
        self.net.start()
        self.iters = mock.patch.object(gm_state, "PIN_ITERATIONS", 1000)    # fast PBKDF2 in tests
        self.iters.start()

    def tearDown(self):
        self.iters.stop()
        self.net.stop()
        self.con.close()
        self.tmp.cleanup()
        self.assertEqual(fingerprint(state.REAL_DB_PATH), self.real_before, "real data/state.sqlite changed")

    def save(self, p):
        state.record_attempt(self.con, p["domain"], "test", 200)
        p = score(pipeline.apply_review(self.con, p))
        state.save_prospect(self.con, p)
        self.con.commit()
        return p

    def run_gm(self, **kw):
        kw.setdefault("run_date", DAY)
        kw.setdefault("offline", True)
        kw.setdefault("now", clock())
        kw.setdefault("report_dir", self.reports)
        return gm_run.run_daily(self.con, **kw)

    def approvals(self, status=None):
        return [dict(r) for r in gm_state.approvals(self.con, (status,) if status else ())]


# ============================================================ state layer

class TestSchema(GmCase):
    def test_connect_alone_does_not_create_gm_tables(self):
        names = {r[0] for r in self.con.execute("select name from sqlite_master")}
        self.assertFalse({n for n in names if n.startswith("gm_")})

    def test_ensure_schema_is_idempotent_and_versioned(self):
        gm_state.ensure_schema(self.con)
        gm_state.ensure_schema(self.con)
        self.assertEqual(self.con.execute("select value from gm_meta where key='schema_version'").fetchone()[0],
                         str(gm_state.GM_SCHEMA_VERSION))
        self.assertEqual(self.con.execute("select count(*) from gm_meta").fetchone()[0], 2)

    def test_newer_schema_version_is_refused(self):
        gm_state.ensure_schema(self.con)
        self.con.execute("update gm_meta set value = '99' where key = 'schema_version'")
        with self.assertRaises(gm_state.GmStateError):
            gm_state.ensure_schema(self.con)

    def test_existing_prospect_tables_untouched(self):
        self.save(ready("a.nl"))
        before = [tuple(r) for r in self.con.execute("select * from prospects")]
        gm_state.ensure_schema(self.con)
        self.assertEqual([tuple(r) for r in self.con.execute("select * from prospects")], before)

    def test_events_and_decisions_are_append_only(self):
        gm_state.ensure_schema(self.con)
        gm_state.log_event(self.con, "x", "y")
        gm_state.record_decision(self.con, "NL stays first market", "pipeline exists", recorded_by="GM")
        for sql in ("update gm_events set message = 'z'", "delete from gm_events",
                    "update gm_decisions set decision = 'z'", "delete from gm_decisions"):
            with self.assertRaises(sqlite3.IntegrityError, msg=sql):
                self.con.execute(sql)


class TestIncidents(GmCase):
    def setUp(self):
        super().setUp()
        gm_state.ensure_schema(self.con)

    def test_redetection_updates_one_row(self):
        gm_state.raise_incident(self.con, "x:1", "P2", "thing broke", run_id="gm-1")
        gm_state.raise_incident(self.con, "x:1", "P2", "thing broke", run_id="gm-1")   # same run: counted once
        i = gm_state.raise_incident(self.con, "x:1", "P2", "thing broke", run_id="gm-2")
        self.assertEqual(self.con.execute("select count(*) from gm_incidents").fetchone()[0], 1)
        self.assertEqual((i["occurrences"], i["first_run_id"], i["last_run_id"]), (2, "gm-1", "gm-2"))

    def test_resolve_and_reopen(self):
        i = gm_state.raise_incident(self.con, "x:1", "P2", "thing broke")
        gm_state.set_incident_status(self.con, i["id"], "RESOLVED", "fixed config", by="GM")
        with self.assertRaises(gm_state.GmStateError):
            gm_state.set_incident_status(self.con, i["id"], "RESOLVED", "again", by="GM")
        again = gm_state.raise_incident(self.con, "x:1", "P2", "thing broke")
        self.assertEqual((again["status"], again["resolved_at"]), ("OPEN", None))
        kinds = [e["kind"] for e in gm_state.events(self.con)]
        self.assertIn("incident_reopened", kinds)

    def test_note_required_and_bad_severity(self):
        i = gm_state.raise_incident(self.con, "x:1", "P3", "minor")
        with self.assertRaises(gm_state.GmStateError):
            gm_state.set_incident_status(self.con, i["id"], "RESOLVED", " ", by="GM")
        with self.assertRaises(gm_state.GmStateError):
            gm_state.raise_incident(self.con, "x:2", "P9", "nope")

    def test_resolve_unobserved_only_touches_its_prefix(self):
        gm_state.raise_incident(self.con, "health:a", "P2", "a")
        gm_state.raise_incident(self.con, "health:b", "P2", "b")
        gm_state.raise_incident(self.con, "sales:c", "P2", "c")
        gone = gm_state.resolve_unobserved(self.con, "health:", {"health:a"}, run_id=None)
        self.assertEqual(gone, ["health:b"])
        open_fps = {r["fingerprint"] for r in gm_state.open_incidents(self.con)}
        self.assertEqual(open_fps, {"health:a", "sales:c"})


PIN = "ceo-secret-1"


class TestApprovals(GmCase):
    def setUp(self):
        super().setUp()
        gm_state.ensure_schema(self.con)
        gm_state.set_ceo_pin(self.con, PIN)

    def req(self, fp="contact:a.nl", **kw):
        return gm_state.request_approval(self.con, fp, kw.pop("category", "CONTACT_PROSPECT"), "Contact A", **kw)

    def test_all_ceo_categories_exist(self):
        for c in ("SEND_EMAIL", "SEND_FOLLOW_UP", "CONTACT_PROSPECT", "CREATE_EXTERNAL_DRAFT", "PRODUCTION_DEPLOY",
                  "PRODUCTION_DB_CHANGE", "ENABLE_EXTERNAL_WORKFLOW", "SPEND_MONEY", "PAID_SERVICE",
                  "PRICING_CHANGE", "LEGAL_COMMITMENT", "DOMAIN_PURCHASE", "DESTRUCTIVE_OPERATION",
                  "CREDENTIAL_CHANGE", "IRREVERSIBLE_EXTERNAL_ACTION"):
            self.assertIn(c, gm_state.APPROVAL_CATEGORIES)

    def test_unknown_category_rejected(self):
        with self.assertRaises(gm_state.GmStateError):
            self.req(category="JUST_DO_IT")

    def test_request_is_idempotent(self):
        a = self.req(details={"v": 1})
        b = self.req(details={"v": 2})
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(json.loads(b["details"]), {"v": 2})
        self.assertEqual(len(self.approvals()), 1)

    def test_lifecycle_and_forbidden_moves(self):
        a = self.req()
        approved = gm_state.decide_approval(self.con, a["id"], "APPROVED", pin=PIN, note="go")
        self.assertEqual((approved["status"], approved["decided_by"]), ("APPROVED", "CEO"))
        done = gm_state.decide_approval(self.con, a["id"], "DONE", pin=PIN, note="sent by hand")
        self.assertEqual(done["status"], "DONE")
        for d in ("APPROVED", "REJECTED", "DONE"):
            with self.assertRaises(gm_state.GmStateError):
                gm_state.decide_approval(self.con, a["id"], d, pin=PIN)

    def test_rejected_is_final_and_never_resurrected(self):
        a = self.req()
        gm_state.decide_approval(self.con, a["id"], "REJECTED", pin=PIN, note="not a fit")
        again = self.req()
        self.assertEqual(again["status"], "REJECTED")

    def test_cannot_mark_done_without_approval(self):
        a = self.req()
        with self.assertRaises(gm_state.GmStateError):
            gm_state.decide_approval(self.con, a["id"], "DONE", pin=PIN)

    def test_withdraw_and_rerequest(self):
        self.req("contact:a.nl")
        self.req("contact:b.nl")
        gm_state.withdraw_unrequested(self.con, "contact:", {"contact:b.nl"}, reason="gone", run_id=None)
        self.assertEqual({a["fingerprint"]: a["status"] for a in self.approvals()},
                         {"contact:a.nl": "WITHDRAWN", "contact:b.nl": "PENDING"})
        self.assertEqual(self.req("contact:a.nl")["status"], "PENDING")

    def test_gm_cannot_withdraw_an_approved_request(self):
        a = self.req("contact:a.nl")
        gm_state.decide_approval(self.con, a["id"], "APPROVED", pin=PIN)
        gm_state.withdraw_unrequested(self.con, "contact:", set(), reason="gone", run_id=None)
        self.assertEqual(self.approvals()[0]["status"], "APPROVED")

    def test_pending_ceo_actions(self):
        a = self.req("contact:a.nl")
        b = self.req("contact:b.nl")
        gm_state.decide_approval(self.con, b["id"], "APPROVED", pin=PIN)
        gm_state.raise_incident(self.con, "x:ceo", "P3", "needs CEO", needs_ceo=True)
        gm_state.raise_incident(self.con, "x:p1", "P1", "serious")
        gm_state.raise_incident(self.con, "x:minor", "P3", "minor")
        p = gm_state.pending_ceo_actions(self.con)
        self.assertEqual([r["id"] for r in p["approvals_pending"]], [a["id"]])
        self.assertEqual([r["id"] for r in p["approved_not_done"]], [b["id"]])
        self.assertEqual({r["fingerprint"] for r in p["incidents"]}, {"x:ceo", "x:p1"})


# ============================================================ daily run

class TestDailyRun(GmCase):
    def seed(self):
        self.save(ready("a.nl"))
        self.save(ready("b.nl", public_email="info@b.nl", kvk_number="87654321"))
        outreach.record_contact(self.con, "contacted.nl", "Contacted BV", "info@contacted.nl", "2026-10-06",
                                subject="Onderwerp", message_version="V1")
        self.con.commit()

    def test_full_run_records_all_steps_in_order(self):
        self.seed()
        res = self.run_gm()
        self.assertEqual(res["status"], "COMPLETED")
        steps = [(s["step"], s["status"]) for s in gm_state.steps(self.con, res["run_id"])]
        self.assertEqual([s for s, _ in steps], [s.name for s in gm_run.DEFAULT_STEPS])
        self.assertEqual(dict(steps)["prospect_discovery"], "SKIPPED")      # honest: not automated yet
        self.assertEqual(dict(steps)["safe_fixes"], "SKIPPED")
        self.assertEqual(res["run_id"], "gm-2026-10-08")

    def test_report_written_once_per_day(self):
        self.seed()
        res = self.run_gm()
        path = Path(res["report_path"])
        self.assertEqual(path.name, "2026-10-08.md")
        text = path.read_text(encoding="utf-8")
        for heading in ("## Executive summary", "## System health", "## Sales", "## Problems (open incidents)",
                        "## Actions taken", "## CEO approval required"):
            self.assertIn(heading, text)
        self.assertIn("revenue: UNKNOWN", text)
        self.assertIn("NOT CHECKED (offline)", text)
        self.run_gm(rerun=True)
        self.assertEqual(sorted(p.name for p in self.reports.iterdir()), ["2026-10-08.md"])

    def test_outreach_queue_prepares_approvals_for_ready_only(self):
        self.seed()
        self.run_gm()
        fps = {a["fingerprint"]: a for a in self.approvals("PENDING")}
        self.assertEqual({f for f in fps if f.startswith("contact:")}, {"contact:a.nl", "contact:b.nl"})
        self.assertNotIn("contact:contacted.nl", fps)
        self.assertEqual(fps["contact:a.nl"]["category"], "CONTACT_PROSPECT")

    def test_follow_up_without_date_raises_ceo_incident(self):
        self.seed()
        self.run_gm()
        inc = {i["fingerprint"]: i for i in gm_state.open_incidents(self.con)}
        self.assertIn("follow-up:missing-date", inc)
        self.assertEqual(inc["follow-up:missing-date"]["needs_ceo"], 1)
        self.assertIn("contacted.nl", inc["follow-up:missing-date"]["detail"])

    def test_due_follow_up_becomes_approval_request(self):
        outreach.record_contact(self.con, "c.nl", "C BV", "info@c.nl", "2026-10-01", follow_up_at="2026-10-08")
        self.con.commit()
        self.run_gm()
        fu = [a for a in self.approvals("PENDING") if a["category"] == "SEND_FOLLOW_UP"]
        self.assertEqual([a["fingerprint"] for a in fu], ["follow-up:c.nl:2026-10-08"])
        self.assertNotIn("follow-up:missing-date", {i["fingerprint"] for i in gm_state.open_incidents(self.con)})

    def test_completed_run_is_a_noop(self):
        self.seed()
        self.run_gm()
        counts = lambda: [self.con.execute(f"select count(*) from {t}").fetchone()[0]  # noqa: E731
                          for t in ("gm_runs", "gm_steps", "gm_approvals", "gm_incidents")]
        before = counts()
        occ = self.con.execute("select sum(occurrences) from gm_incidents").fetchone()[0]
        res = self.run_gm()
        self.assertTrue(res["noop"])
        self.assertEqual(counts(), before)
        self.assertEqual(self.con.execute("select sum(occurrences) from gm_incidents").fetchone()[0], occ)

    def test_rerun_does_not_duplicate_anything(self):
        self.seed()
        self.run_gm()
        approvals = sorted(a["fingerprint"] for a in self.approvals())
        incidents = sorted(i["fingerprint"] for i in gm_state.open_incidents(self.con))
        res = self.run_gm(rerun=True)
        self.assertEqual((res["status"], res["attempt"]), ("COMPLETED", 2))
        self.assertEqual(sorted(a["fingerprint"] for a in self.approvals()), approvals)
        self.assertEqual(sorted(i["fingerprint"] for i in gm_state.open_incidents(self.con)), incidents)
        self.assertEqual(self.con.execute("select count(*) from gm_runs").fetchone()[0], 1)

    def test_next_day_is_a_new_run_and_counts_occurrence(self):
        self.seed()
        self.run_gm()
        self.run_gm(run_date=DAY + dt.timedelta(days=1), now=clock(dt.datetime(2026, 10, 9, 7, tzinfo=TZ)))
        self.assertEqual(self.con.execute("select count(*) from gm_runs").fetchone()[0], 2)
        i = self.con.execute("select * from gm_incidents where fingerprint = 'follow-up:missing-date'").fetchone()
        self.assertEqual(i["occurrences"], 2)

    def test_contacted_prospect_approval_is_withdrawn(self):
        self.seed()
        self.run_gm()
        outreach.record_contact(self.con, "a.nl", "A", "info@a.nl", "2026-10-08")
        self.con.commit()
        self.run_gm(run_date=DAY + dt.timedelta(days=1), now=clock(dt.datetime(2026, 10, 9, 7, tzinfo=TZ)))
        st = {a["fingerprint"]: a["status"] for a in self.approvals()}
        self.assertEqual(st["contact:a.nl"], "WITHDRAWN")
        self.assertEqual(st["contact:b.nl"], "PENDING")

    def test_ceo_decision_survives_later_runs(self):
        self.seed()
        self.run_gm()
        a = next(a for a in self.approvals() if a["fingerprint"] == "contact:b.nl")
        gm_state.set_ceo_pin(self.con, PIN)
        gm_state.decide_approval(self.con, a["id"], "REJECTED", pin=PIN, note="skip")
        self.con.commit()
        self.run_gm(run_date=DAY + dt.timedelta(days=1), now=clock(dt.datetime(2026, 10, 9, 7, tzinfo=TZ)))
        self.assertEqual({x["fingerprint"]: x["status"] for x in self.approvals()}["contact:b.nl"], "REJECTED")

    def test_run_never_changes_prospects_or_outreach(self):
        self.seed()
        snap = lambda: ([tuple(r) for r in self.con.execute("select * from prospects order by domain")],  # noqa
                        [tuple(r) for r in self.con.execute("select * from outreach order by domain")],
                        [tuple(r) for r in self.con.execute("select * from outreach_events order by id")])
        before = snap()
        self.run_gm()
        self.run_gm(rerun=True)
        self.assertEqual(snap(), before)


class TestFailureHandling(GmCase):
    def steps_with(self, name, func, critical=False):
        return tuple(gm_run.Step(s.name, func, s.effect, critical or s.critical, s.always) if s.name == name else s
                     for s in gm_run.DEFAULT_STEPS)

    def test_failed_step_is_recorded_rolled_back_and_run_continues(self):
        def boom(ctx):
            gm_state.request_approval(ctx.con, "contact:partial.nl", "CONTACT_PROSPECT", "partial write")
            raise RuntimeError("source exploded")
        res = self.run_gm(steps=self.steps_with("outreach_queue", boom))
        self.assertEqual(res["status"], "PARTIAL")
        st = {s["step"]: s for s in gm_state.steps(self.con, res["run_id"])}
        self.assertEqual(st["outreach_queue"]["status"], "FAILED")
        self.assertIn("RuntimeError: source exploded", st["outreach_queue"]["error"])
        self.assertEqual(st["follow_up_queue"]["status"], "DONE")                     # later steps still ran
        self.assertEqual(st["daily_report"]["status"], "DONE")
        self.assertNotIn("contact:partial.nl", {a["fingerprint"] for a in self.approvals()})   # rolled back
        inc = {i["fingerprint"]: i for i in gm_state.open_incidents(self.con)}
        self.assertEqual(inc["run:step-failed:outreach_queue"]["severity"], "P2")
        self.assertIn("outreach_queue", Path(res["report_path"]).read_text(encoding="utf-8"))

    def test_resume_reruns_only_unfinished_steps_then_resolves_incident(self):
        calls = []
        self.run_gm(steps=self.steps_with("outreach_queue", lambda ctx: (_ for _ in ()).throw(ValueError("x"))))

        def spy(name, func):
            def wrapped(ctx):
                calls.append(name)
                return func(ctx)
            return wrapped
        fixed = tuple(gm_run.Step(s.name, spy(s.name, s.func), s.effect, s.critical, s.always)
                      for s in gm_run.DEFAULT_STEPS)
        res = self.run_gm(steps=fixed)
        self.assertEqual(res["status"], "COMPLETED")
        self.assertEqual(calls, ["outreach_queue", "approval_queue", "daily_report"])
        st = {s["step"]: s["attempts"] for s in gm_state.steps(self.con, res["run_id"])}
        self.assertEqual((st["outreach_queue"], st["system_health"]), (2, 1))
        self.assertNotIn("run:step-failed:outreach_queue", {i["fingerprint"] for i in gm_state.open_incidents(self.con)})

    def test_critical_failure_skips_remaining_steps_but_writes_report(self):
        res = self.run_gm(steps=self.steps_with("system_health",
                                                lambda ctx: (_ for _ in ()).throw(RuntimeError("db corrupt"))))
        self.assertEqual(res["status"], "FAILED")
        st = {s["step"]: s["status"] for s in gm_state.steps(self.con, res["run_id"])}
        self.assertEqual(st["system_health"], "FAILED")
        self.assertEqual(st["sales_state"], "SKIPPED")
        self.assertEqual(st["daily_report"], "DONE")
        inc = {i["fingerprint"]: i for i in gm_state.open_incidents(self.con)}
        self.assertEqual(inc["run:step-failed:system_health"]["severity"], "P1")

    def test_invalid_step_status_counts_as_failure(self):
        res = self.run_gm(steps=self.steps_with("safe_fixes", lambda ctx: gm_run.Outcome("SENT")))
        self.assertEqual(res["status"], "PARTIAL")

    def test_external_step_is_refused_before_anything_runs(self):
        steps = gm_run.DEFAULT_STEPS + (gm_run.Step("send_emails", lambda ctx: gm_run.Outcome("DONE"), "external"),)
        with self.assertRaises(gm_run.ExternalEffectRefused):
            self.run_gm(steps=steps)
        self.assertEqual(self.con.execute("select count(*) from gm_runs").fetchone()[0], 0)

    def test_concurrent_run_is_blocked(self):
        gm_state.ensure_schema(self.con)
        self.con.execute("insert into gm_runs (run_id, run_date, status, started_at, heartbeat_at) values "
                         "('gm-2026-10-08', '2026-10-08', 'RUNNING', '2026-10-08T06:59:00+02:00', "
                         "'2026-10-08T06:59:00+02:00')")
        self.con.commit()
        with self.assertRaises(gm_run.GmRunInProgress):
            self.run_gm()

    def test_stale_run_is_aborted_recorded_and_resumed(self):
        gm_state.ensure_schema(self.con)
        self.con.execute("insert into gm_runs (run_id, run_date, status, started_at, heartbeat_at) values "
                         "('gm-2026-10-08', '2026-10-08', 'RUNNING', '2026-10-08T01:00:00+02:00', "
                         "'2026-10-08T01:00:00+02:00')")
        self.con.commit()
        res = self.run_gm()
        self.assertEqual((res["status"], res["attempt"]), ("COMPLETED", 2))
        kinds = [e["kind"] for e in gm_state.events(self.con, "gm-2026-10-08")]
        self.assertIn("run_aborted", kinds)
        inc = self.con.execute("select * from gm_incidents where fingerprint = 'run:interrupted:gm-2026-10-08'"
                               ).fetchone()
        self.assertEqual(inc["status"], "RESOLVED")        # resumed run completed


class TestHealthProbes(GmCase):
    def test_offline_never_touches_the_network(self):
        res = self.run_gm(offline=True, http_get=no_network)
        self.assertEqual(res["status"], "COMPLETED")

    def test_broken_page_opens_incident_and_recovery_resolves_it(self):
        codes = {"https://orsebusiness.vercel.app/": 200, "https://orsebusiness.vercel.app/book": 200,
                 "https://orsebusiness.vercel.app/nl": 404}
        self.run_gm(offline=False, http_get=codes.__getitem__)
        inc = {i["fingerprint"]: i for i in gm_state.open_incidents(self.con)}
        nl = inc["health:http:https://orsebusiness.vercel.app/nl"]
        self.assertEqual((nl["severity"], nl["needs_ceo"]), ("P2", 0))
        codes["https://orsebusiness.vercel.app/nl"] = 200
        self.run_gm(offline=False, http_get=codes.__getitem__, run_date=DAY + dt.timedelta(days=1),
                    now=clock(dt.datetime(2026, 10, 9, 7, tzinfo=TZ)))
        self.assertNotIn("health:http:https://orsebusiness.vercel.app/nl",
                         {i["fingerprint"] for i in gm_state.open_incidents(self.con)})

    def test_network_error_is_a_finding_not_a_crash(self):
        def down(url):
            raise OSError("connection refused")
        res = self.run_gm(offline=False, http_get=down)
        self.assertEqual(res["status"], "COMPLETED")
        home = self.con.execute("select * from gm_incidents where fingerprint = "
                                "'health:http:https://orsebusiness.vercel.app/'").fetchone()
        self.assertEqual((home["severity"], home["needs_ceo"]), ("P1", 1))
        self.assertIn("connection refused", home["title"])

    def test_offline_run_does_not_resolve_http_incidents(self):
        self.run_gm(offline=False, http_get=lambda url: 500)
        self.run_gm(offline=True, rerun=True)
        self.assertTrue(any(i["fingerprint"].startswith("health:http:") for i in gm_state.open_incidents(self.con)))


class TestFollowUpTracking(GmCase):
    """Follow-up policy as approved by the CEO: dates set via outreach.update_details, CEO-approved manual sends,
    replies / opt-outs cancel pending follow-ups, unknown recipients are escalated."""

    def contacted(self, domain, email):
        outreach.record_contact(self.con, domain, domain.split(".")[0].title(), email, "2026-10-06")
        self.con.commit()

    def next_day_run(self, day=13):
        return self.run_gm(run_date=dt.date(2026, 10, day), now=clock(dt.datetime(2026, 10, day, 7, tzinfo=TZ)))

    def test_setting_dates_resolves_missing_date_incident(self):
        self.contacted("a.nl", "info@a.nl")
        self.run_gm()
        self.assertIn("follow-up:missing-date", {i["fingerprint"] for i in gm_state.open_incidents(self.con)})
        outreach.update_details(self.con, "a.nl", follow_up_at="2026-10-13", note="policy")
        self.con.commit()
        self.run_gm(rerun=True)
        self.assertNotIn("follow-up:missing-date", {i["fingerprint"] for i in gm_state.open_incidents(self.con)})
        self.assertEqual(self.approvals("PENDING"), [])           # not due yet on 2026-10-08

    def test_due_follow_up_with_unknown_recipient_is_escalated(self):
        self.contacted("vt.nl", None)
        outreach.update_details(self.con, "vt.nl", follow_up_at="2026-10-13", note="policy")
        self.con.commit()
        self.next_day_run()
        inc = {i["fingerprint"]: i for i in gm_state.open_incidents(self.con)}
        nr = inc["follow-up:no-recipient:vt.nl"]
        self.assertEqual((nr["severity"], nr["needs_ceo"]), ("P2", 1))
        self.assertIn("RECIPIENT UNKNOWN", nr["detail"])
        self.assertEqual([a for a in self.approvals() if a["category"] == "SEND_FOLLOW_UP"], [])   # nothing approvable

    def test_unknown_or_invalid_recipient_never_gets_an_approval_on_reruns(self):
        self.contacted("vt.nl", None)
        self.contacted("bad.nl", "info@bad.nl")
        self.con.execute("update outreach set email = 'UNKNOWN' where domain = 'bad.nl'")      # not a valid address
        for d in ("vt.nl", "bad.nl"):
            outreach.update_details(self.con, d, follow_up_at="2026-10-13", note="policy")
        self.con.commit()
        for day in (13, 14, 15):
            res = self.next_day_run(day)
        self.assertEqual([a for a in self.approvals() if a["category"] == "SEND_FOLLOW_UP"], [])
        step = {s["step"]: s for s in gm_state.steps(self.con, res["run_id"])}["follow_up_queue"]
        self.assertEqual(sorted(json.loads(step["result"])["due_without_recipient"]), ["bad.nl", "vt.nl"])
        self.assertEqual(len([i for i in gm_state.open_incidents(self.con)
                              if i["fingerprint"].startswith("follow-up:no-recipient:")]), 2)

    def test_confirmed_recipient_resolves_the_escalation(self):
        self.contacted("vt.nl", None)
        outreach.update_details(self.con, "vt.nl", follow_up_at="2026-10-13", note="policy")
        self.con.commit()
        self.next_day_run()
        outreach.update_details(self.con, "vt.nl", email="info@vt.nl", note="confirmed from Sent folder")
        self.con.commit()
        self.next_day_run(day=14)
        self.assertNotIn("follow-up:no-recipient:vt.nl", {i["fingerprint"] for i in gm_state.open_incidents(self.con)})
        fu = [a for a in self.approvals("PENDING") if a["category"] == "SEND_FOLLOW_UP"]
        self.assertEqual(json.loads(fu[0]["details"])["email"], "info@vt.nl")

    def test_known_recipient_gets_no_escalation(self):
        self.contacted("a.nl", "info@a.nl")
        outreach.update_details(self.con, "a.nl", follow_up_at="2026-10-13", note="policy")
        self.con.commit()
        self.next_day_run()
        self.assertFalse([i for i in gm_state.open_incidents(self.con)
                          if i["fingerprint"].startswith("follow-up:no-recipient:")])

    def test_reply_cancels_pending_follow_up(self):
        self.contacted("a.nl", "info@a.nl")
        outreach.update_details(self.con, "a.nl", follow_up_at="2026-10-13", note="policy")
        self.con.commit()
        self.next_day_run()
        outreach.set_status(self.con, "a.nl", "REPLIED", reply_type="question", note="replied by email")
        self.con.commit()
        self.next_day_run(day=14)
        st = {a["fingerprint"]: a["status"] for a in self.approvals()}
        self.assertEqual(st["follow-up:a.nl:2026-10-13"], "WITHDRAWN")

    def test_opt_out_cancels_pending_follow_up_and_locks_record(self):
        self.contacted("a.nl", "info@a.nl")
        outreach.update_details(self.con, "a.nl", follow_up_at="2026-10-13", note="policy")
        self.con.commit()
        self.next_day_run()
        outreach.set_status(self.con, "a.nl", "DO_NOT_CONTACT", reply_type="unsubscribe", note="replied 'stop'")
        self.con.commit()
        self.next_day_run(day=14)
        self.assertEqual({a["fingerprint"]: a["status"] for a in self.approvals()}["follow-up:a.nl:2026-10-13"],
                         "WITHDRAWN")
        with self.assertRaises(outreach.OutreachError):
            outreach.update_details(self.con, "a.nl", follow_up_at="2026-10-22", note="no")

    def test_second_follow_up_after_rescheduling(self):
        self.contacted("a.nl", "info@a.nl")
        outreach.update_details(self.con, "a.nl", follow_up_at="2026-10-13", note="policy")
        self.con.commit()
        self.next_day_run()
        outreach.set_status(self.con, "a.nl", "FOLLOW_UP", follow_up_at="2026-10-22", note="follow-up 1 sent")
        self.con.commit()
        self.next_day_run(day=14)
        st = {a["fingerprint"]: a["status"] for a in self.approvals()}
        self.assertEqual(st["follow-up:a.nl:2026-10-13"], "WITHDRAWN")     # done; no longer due
        self.next_day_run(day=22)
        st = {a["fingerprint"]: a["status"] for a in self.approvals()}
        self.assertEqual(st["follow-up:a.nl:2026-10-22"], "PENDING")

    # --- approved follow-up, then reply / STOP before the CEO sends it

    def approve_due_follow_up(self, domain="a.nl"):
        gm_state.ensure_schema(self.con)
        gm_state.set_ceo_pin(self.con, "123456")
        self.contacted(domain, f"info@{domain}")
        outreach.update_details(self.con, domain, follow_up_at="2026-10-13", note="policy")
        self.con.commit()
        self.next_day_run()
        a = [a for a in self.approvals("PENDING") if a["category"] == "SEND_FOLLOW_UP"][0]
        gm_state.decide_approval(self.con, a["id"], "APPROVED", pin="123456", note="go")
        self.con.commit()
        return a["id"]

    def assert_do_not_send(self, aid):
        self.next_day_run(day=14)
        inc = {i["fingerprint"]: i for i in gm_state.open_incidents(self.con)}
        p1 = inc[f"follow-up:do-not-send:{aid}"]
        self.assertEqual((p1["severity"], p1["needs_ceo"]), ("P1", 1))
        self.assertIn("DO NOT SEND", p1["title"])
        approved = gm_state.pending_ceo_actions(self.con)["approved_not_done"]
        self.assertTrue(gm_state.follow_up_send_blocked(self.con, approved[0]))
        report = (self.reports / "2026-10-14.md").read_text(encoding="utf-8")
        self.assertIn("DO NOT SEND", report)

    def test_reply_after_approval_blocks_the_approved_follow_up(self):
        aid = self.approve_due_follow_up()
        outreach.set_status(self.con, "a.nl", "REPLIED", reply_type="question", note="replied before we sent")
        self.con.commit()
        self.assert_do_not_send(aid)

    def test_stop_after_approval_blocks_the_approved_follow_up(self):
        aid = self.approve_due_follow_up()
        outreach.set_status(self.con, "a.nl", "DO_NOT_CONTACT", reply_type="unsubscribe", note="stop")
        self.con.commit()
        self.assert_do_not_send(aid)

    def test_approved_follow_up_without_reply_is_not_blocked_and_block_clears_when_closed(self):
        aid = self.approve_due_follow_up()
        self.assertIsNone(gm_state.follow_up_send_blocked(self.con, self.approvals("APPROVED")[0]))
        outreach.set_status(self.con, "a.nl", "REPLIED", reply_type="question", note="reply")
        self.con.commit()
        self.next_day_run(day=14)
        gm_state.decide_approval(self.con, aid, "DONE", pin="123456", note="not sent: reply received")
        self.con.commit()
        self.next_day_run(day=15)
        self.assertNotIn(f"follow-up:do-not-send:{aid}", {i["fingerprint"] for i in gm_state.open_incidents(self.con)})

    # --- maximum 2 follow-ups

    def test_maximum_two_follow_ups(self):
        self.contacted("a.nl", "info@a.nl")
        outreach.update_details(self.con, "a.nl", follow_up_at="2026-10-13", note="policy")
        self.assertEqual(outreach.follow_ups_sent(self.con, "a.nl"), 0)
        r = outreach.set_status(self.con, "a.nl", "FOLLOW_UP", follow_up_at="2026-10-20", note="follow-up 1 sent")
        self.assertEqual((outreach.follow_ups_sent(self.con, "a.nl"), r["follow_up_at"]), (1, "2026-10-20"))
        outreach.update_details(self.con, "a.nl", follow_up_at="2026-10-21", note="moved")   # not counted as a send
        self.assertEqual(outreach.follow_ups_sent(self.con, "a.nl"), 1)
        with self.assertRaises(outreach.OutreachError):                              # #2 is the last: no next date
            outreach.set_status(self.con, "a.nl", "FOLLOW_UP", follow_up_at="2026-10-28", note="x")
        r = outreach.set_status(self.con, "a.nl", "FOLLOW_UP", note="follow-up 2 sent")
        self.assertEqual((outreach.follow_ups_sent(self.con, "a.nl"), r["follow_up_at"]), (2, None))
        with self.assertRaises(outreach.OutreachError):
            outreach.set_status(self.con, "a.nl", "FOLLOW_UP", note="follow-up 3")
        with self.assertRaises(outreach.OutreachError):
            outreach.update_details(self.con, "a.nl", follow_up_at="2026-10-28", note="third")
        self.con.execute("update outreach set follow_up_at = '2026-10-12' where domain = 'a.nl'")   # forced past date
        self.con.commit()
        res = self.next_day_run()
        self.assertEqual([a for a in self.approvals() if a["category"] == "SEND_FOLLOW_UP"], [])
        step = json.loads({s["step"]: s for s in gm_state.steps(self.con, res["run_id"])}["follow_up_queue"]["result"])
        self.assertNotIn("a.nl", step["due"] + step["missing_follow_up_date"])

    def test_gm_creates_at_most_two_follow_up_approvals(self):
        gm_state.ensure_schema(self.con)
        gm_state.set_ceo_pin(self.con, "123456")
        self.contacted("a.nl", "info@a.nl")
        outreach.update_details(self.con, "a.nl", follow_up_at="2026-10-13", note="policy")
        self.con.commit()
        for day, nxt in ((13, "2026-10-20"), (20, None)):
            self.next_day_run(day)
            a = [a for a in self.approvals("PENDING") if a["category"] == "SEND_FOLLOW_UP"][0]
            gm_state.decide_approval(self.con, a["id"], "APPROVED", pin="123456", note="ok")
            gm_state.decide_approval(self.con, a["id"], "DONE", pin="123456", note="sent")
            outreach.set_status(self.con, "a.nl", "FOLLOW_UP", follow_up_at=nxt, note="sent")
            self.con.commit()
        for day in (21, 27, 30):
            self.next_day_run(day)
        self.assertEqual(len([a for a in self.approvals() if a["category"] == "SEND_FOLLOW_UP"]), 2)


class TestContactInvariant(GmCase):
    def test_held_ready_prospect_raises_p1(self):
        self.save(ready("held.nl"))
        state.add_hold(self.con, "held.nl", "CEO decision")          # hold added without re-scoring: stale READY
        self.con.commit()
        self.run_gm()
        inc = {i["fingerprint"]: i for i in gm_state.open_incidents(self.con)}
        self.assertEqual(inc["contact-check:hold:held.nl"]["severity"], "P1")

    def test_clean_state_has_no_violations(self):
        self.save(ready("a.nl"))
        res = self.run_gm()
        step = {s["step"]: s for s in gm_state.steps(self.con, res["run_id"])}["contact_checks"]
        self.assertEqual(json.loads(step["result"])["violations"], [])


class TestCli(GmCase):
    def cli(self, *argv):
        import run
        with mock.patch("sys.argv", ["run.py", "--db", str(Path(self.tmp.name) / "gm.sqlite"), *argv]), \
                mock.patch.object(gm_run, "REPORT_DIR", self.reports):
            try:
                run.main()
            finally:
                run.close_connections()

    def db_row(self, sql, *params):
        con = sqlite3.connect(Path(self.tmp.name) / "gm.sqlite")
        try:
            return con.execute(sql, params).fetchone()
        finally:
            con.close()

    def as_ceo_terminal(self, *pins):
        """Simulates the CEO at an interactive terminal typing the given PIN(s)."""
        answers = iter(pins)
        return (mock.patch("sys.stdin.isatty", return_value=True), mock.patch("sys.stdout.isatty", return_value=True),
                mock.patch("getpass.getpass", side_effect=lambda prompt="": next(answers)))

    def test_run_status_and_ceo_decision_via_cli(self):
        self.save(ready("a.nl"))
        self.cli("gm", "run", "--offline", "--date", "2026-10-08")
        self.cli("gm", "status")
        a = self.approvals("PENDING")[0]
        t1, t2, t3 = self.as_ceo_terminal(PIN, PIN)
        with t1, t2, t3:
            self.cli("gm", "ceo-pin")
        t1, t2, t3 = self.as_ceo_terminal(PIN)
        with t1, t2, t3:
            self.cli("gm", "approve", str(a["id"]), "--note", "ok")
        self.assertEqual(self.db_row("select status, decided_by from gm_approvals where id = ?", a["id"]),
                         ("APPROVED", "CEO"))

    def test_gm_without_terminal_cannot_approve_reject_done_or_set_pin(self):
        self.save(ready("a.nl"))
        self.cli("gm", "run", "--offline", "--date", "2026-10-08")
        a = self.approvals("PENDING")[0]
        with mock.patch("getpass.getpass", side_effect=AssertionError("must not even prompt")):
            for argv in (("approve", str(a["id"])), ("reject", str(a["id"])), ("done", str(a["id"])),
                         ("ceo-pin",), ("audit",), ("decide", "--decision", "x", "--reason", "y", "--by", "CEO")):
                with self.assertRaises(SystemExit) as cm:
                    self.cli("gm", *argv)
                self.assertIn("CEO action", str(cm.exception.code))
        self.assertEqual(self.db_row("select status from gm_approvals where id = ?", a["id"]), ("PENDING",))
        self.assertIsNone(self.db_row("select value from gm_meta where key = 'ceo_pin'"))

    def test_wrong_pin_at_terminal_changes_nothing(self):
        self.save(ready("a.nl"))
        self.cli("gm", "run", "--offline", "--date", "2026-10-08")
        a = self.approvals("PENDING")[0]
        t1, t2, t3 = self.as_ceo_terminal(PIN, PIN)
        with t1, t2, t3:
            self.cli("gm", "ceo-pin")
        t1, t2, t3 = self.as_ceo_terminal("guess-123")
        with t1, t2, t3, self.assertRaises(SystemExit):
            self.cli("gm", "approve", str(a["id"]))
        self.assertEqual(self.db_row("select status from gm_approvals where id = ?", a["id"]), ("PENDING",))

    def test_manual_request_and_decision_log(self):
        self.cli("gm", "request", "--category", "PRODUCTION_DEPLOY", "--title", "Deploy /nl landing page",
                 "--risk", "production change")
        self.cli("gm", "request", "--category", "PRODUCTION_DEPLOY", "--title", "Deploy /nl landing page")
        self.cli("gm", "decide", "--decision", "NL remains first market", "--reason", "pipeline exists")
        con = sqlite3.connect(Path(self.tmp.name) / "gm.sqlite")
        self.assertEqual(con.execute("select count(*) from gm_approvals").fetchone()[0], 1)
        self.assertEqual(con.execute("select count(*) from gm_decisions").fetchone()[0], 1)
        con.close()


class TestCeoBoundary(GmCase):
    def setUp(self):
        super().setUp()
        gm_state.ensure_schema(self.con)
        self.a = gm_state.request_approval(self.con, "contact:a.nl", "CONTACT_PROSPECT", "Contact A")

    def test_no_pin_configured_means_no_decision_possible(self):
        with self.assertRaises(gm_state.CeoAuthError):
            gm_state.decide_approval(self.con, self.a["id"], "APPROVED", pin="anything")
        self.assertEqual(gm_state.approvals(self.con)[0]["status"], "PENDING")

    def test_wrong_or_missing_pin_is_refused(self):
        gm_state.set_ceo_pin(self.con, PIN)
        for pin in (None, "", "wrong-pin"):
            with self.assertRaises(gm_state.CeoAuthError):
                gm_state.decide_approval(self.con, self.a["id"], "APPROVED", pin=pin)
        self.assertEqual(gm_state.approvals(self.con)[0]["status"], "PENDING")

    def test_pin_is_never_stored_in_plain_text(self):
        gm_state.set_ceo_pin(self.con, PIN)
        dump = "\n".join(self.con.iterdump())
        self.assertNotIn(PIN, dump)
        self.assertTrue(gm_state.verify_ceo_pin(self.con, PIN))

    def test_pin_rules(self):
        with self.assertRaises(gm_state.GmStateError):
            gm_state.set_ceo_pin(self.con, "12345")                          # too short
        gm_state.set_ceo_pin(self.con, PIN)
        with self.assertRaises(gm_state.CeoAuthError):
            gm_state.set_ceo_pin(self.con, "new-pin-99")                     # change needs the current PIN
        with self.assertRaises(gm_state.CeoAuthError):
            gm_state.set_ceo_pin(self.con, "new-pin-99", "wrong-pin")
        gm_state.set_ceo_pin(self.con, "new-pin-99", PIN)
        self.assertTrue(gm_state.verify_ceo_pin(self.con, "new-pin-99"))
        self.assertFalse(gm_state.verify_ceo_pin(self.con, PIN))

    def test_genuine_decisions_pass_audit_also_after_pin_change(self):
        gm_state.set_ceo_pin(self.con, PIN)
        gm_state.decide_approval(self.con, self.a["id"], "APPROVED", pin=PIN, note="go")
        gm_state.decide_approval(self.con, self.a["id"], "DONE", pin=PIN, note="sent")
        self.assertEqual(gm_state.audit_decisions(self.con, PIN), [])
        gm_state.set_ceo_pin(self.con, "new-pin-99", PIN)
        self.assertEqual(gm_state.audit_decisions(self.con, "new-pin-99"), [])
        with self.assertRaises(gm_state.CeoAuthError):
            gm_state.audit_decisions(self.con, PIN)

    def test_direct_database_edit_is_detected(self):
        gm_state.set_ceo_pin(self.con, PIN)
        self.con.execute("update gm_approvals set status = 'APPROVED', decided_by = 'CEO' where id = ?",
                         (self.a["id"],))
        self.assertEqual([r["id"] for r in gm_state.unproven_decisions(self.con)], [self.a["id"]])
        self.assertTrue(gm_state.audit_decisions(self.con, PIN))
        # a forged proof string does not help either
        self.con.execute("update gm_approvals set decision_proof = 'f00d' where id = ?", (self.a["id"],))
        self.assertTrue(any("without a valid CEO decision" in p for p in gm_state.audit_decisions(self.con, PIN)))

    def test_gm_run_flags_unproven_decision_and_missing_pin(self):
        res = self.run_gm()
        inc = {i["fingerprint"]: i for i in gm_state.open_incidents(self.con)}
        self.assertEqual((inc["ceo:pin-missing"]["severity"], inc["ceo:pin-missing"]["needs_ceo"]), ("P2", 1))
        self.con.execute("update gm_approvals set status = 'APPROVED' where id = ?", (self.a["id"],))
        self.con.commit()
        self.run_gm(rerun=True)
        inc = {i["fingerprint"]: i for i in gm_state.open_incidents(self.con)}
        self.assertEqual(inc[f"ceo:unproven-decision:{self.a['id']}"]["severity"], "P1")
        self.assertIn("treat as NOT approved", inc[f"ceo:unproven-decision:{self.a['id']}"]["detail"])
        self.assertEqual(res["status"], "COMPLETED")

    def test_pin_set_resolves_missing_pin_incident(self):
        self.run_gm()
        gm_state.set_ceo_pin(self.con, PIN)
        self.con.commit()
        self.run_gm(rerun=True)
        self.assertNotIn("ceo:pin-missing", {i["fingerprint"] for i in gm_state.open_incidents(self.con)})

    def test_ceo_decision_log_entry_needs_pin(self):
        gm_state.set_ceo_pin(self.con, PIN)
        with self.assertRaises(gm_state.CeoAuthError):
            gm_state.record_decision(self.con, "x", "y", recorded_by="CEO")
        gm_state.record_decision(self.con, "x", "y", recorded_by="CEO", pin=PIN)
        gm_state.record_decision(self.con, "observation", "evidence", recorded_by="GM")
        self.assertEqual(len(gm_state.decisions(self.con)), 2)


class TestTimezone(GmCase):
    def test_default_operating_timezone_is_amsterdam(self):
        self.assertEqual(gm_state.GM_TIMEZONE, "Europe/Amsterdam")
        summer = dt.datetime(2026, 7, 1, 10, 0, tzinfo=dt.timezone.utc)
        winter = dt.datetime(2026, 12, 1, 10, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(gm_state.timestamp(summer), "2026-07-01T12:00:00+02:00")
        self.assertEqual(gm_state.timestamp(winter), "2026-12-01T11:00:00+01:00")

    def test_run_date_follows_amsterdam_not_the_machine(self):
        # 22:30 UTC on 7 Oct = 00:30 on 8 Oct in Amsterdam (CEST)
        late = clock(dt.datetime(2026, 10, 7, 22, 30, tzinfo=dt.timezone.utc))
        res = gm_run.run_daily(self.con, offline=True, now=late, report_dir=self.reports)
        self.assertEqual(res["run_id"], "gm-2026-10-08")
        started = self.con.execute("select started_at from gm_runs").fetchone()[0]
        self.assertTrue(started.startswith("2026-10-08T00:30") and started.endswith("+02:00"), started)

    def test_market_timezone_is_configurable(self):
        late = clock(dt.datetime(2026, 10, 7, 22, 30, tzinfo=dt.timezone.utc))
        res = gm_run.run_daily(self.con, offline=True, now=late, report_dir=self.reports, tz="Europe/London")
        self.assertEqual(res["run_id"], "gm-2026-10-07")                  # 23:30 in London


if __name__ == "__main__":
    unittest.main()
