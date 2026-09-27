import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, GridService, Store


class GroundingLockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.s = GridService(Store(Path(self.tmp.name) / "g.db"))
        self.sub = self.s.register_asset("dispatcher", "dispatcher", "SUB", "中心站", "substation", 200, "A")
        self.line = self.s.register_asset("dispatcher", "dispatcher", "LINE", "线路", "line", 100, "A", self.sub["id"])
        self.feeder = self.s.register_asset("dispatcher", "dispatcher", "FEEDER", "支线", "line", 50, "A", self.line["id"])

    def tearDown(self): self.s.store.close(); self.tmp.cleanup()

    def plan(self, code="OUT-G"):
        outage = self.s.create_outage("dispatcher", "dispatcher", code, "线路抢修", ["A"])
        plan = self.s.create_plan("dispatcher", "dispatcher", outage["id"], [
            {"seq": 1, "action": "检查", "asset": "SUB", "required_mw": 80},
            {"seq": 2, "action": "修复", "asset": "LINE", "required_mw": 70, "depends_on": [1]},
            {"seq": 3, "action": "送电", "asset": "FEEDER", "required_mw": 40, "depends_on": [2]}])
        plan = self.s.submit_plan("dispatcher", "dispatcher", plan["id"], plan["revision"])
        plan = self.s.approve_plan("dispatcher", "dispatcher", plan["id"], plan["revision"])
        return outage, self.s.activate_plan("dispatcher", "dispatcher", plan["id"], plan["revision"])

    def lock(self, outage, asset=None, site="SITE-1", wire="JD-01", actor="dispatcher-zhang"):
        return self.s.register_grounding_lock(actor, "dispatcher", outage["id"], (asset or self.line)["id"],
                                              wire, "2026-09-27T08:00:00Z", "抢修一班", site)

    def test_register_idempotent_same_site_order(self):
        outage, _ = self.plan()
        first = self.lock(outage)
        again = self.lock(outage, wire="JD-99", site="SITE-1")
        self.assertEqual(first["id"], again["id"])
        self.assertEqual("JD-01", again["wire_no"])
        other = self.lock(outage, site="SITE-2", wire="JD-02")
        self.assertNotEqual(first["id"], other["id"])
        with self.assertRaises(ApiError):
            self.lock(outage, site="")

    def test_completed_report_pending_on_locked_line_and_downstream(self):
        outage, plan = self.plan()
        self.lock(outage)
        upstream = self.s.field_report("field", "field", plan["id"], 1, "r-1", plan["version"], "completed")
        self.assertEqual("merged", upstream["merge_status"])
        locked = self.s.field_report("field", "field", plan["id"], 2, "r-2", plan["version"], "completed")
        self.assertEqual("pending", locked["merge_status"])
        downstream = self.s.field_report("field", "field", plan["id"], 3, "r-3", plan["version"], "completed")
        self.assertEqual("pending", downstream["merge_status"])
        started = self.s.field_report("field", "field", plan["id"], 2, "r-2b", plan["version"], "started")
        self.assertEqual("merged", started["merge_status"])
        self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 1, "confirmed")
        with self.assertRaises(ApiError):
            self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 2, "confirmed")

    def test_lock_on_upstream_blocks_downstream_steps(self):
        outage, plan = self.plan()
        self.lock(outage, asset=self.sub)
        for seq in (1, 2, 3):
            report = self.s.field_report("field", "field", plan["id"], seq, f"u-{seq}", plan["version"], "completed")
            self.assertEqual("pending", report["merge_status"])

    def test_confirm_rejected_for_report_merged_before_lock(self):
        outage, plan = self.plan()
        self.s.field_report("field", "field", plan["id"], 1, "pre-1", plan["version"], "completed")
        self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 1, "confirmed")
        self.s.field_report("field", "field", plan["id"], 2, "pre-2", plan["version"], "completed")
        self.lock(outage)
        with self.assertRaises(ApiError):
            self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 2, "confirmed")

    def test_removal_requires_second_officer_wire_match_and_photo(self):
        outage, _ = self.plan()
        lock = self.lock(outage)
        with self.assertRaises(ApiError):
            self.s.remove_grounding_lock("field", "field", lock["id"], "JD-01", "photo://1")
        with self.assertRaises(ApiError):
            self.s.remove_grounding_lock("dispatcher-zhang", "dispatcher", lock["id"], "JD-01", "photo://1")
        with self.assertRaises(ApiError):
            self.s.remove_grounding_lock("dispatcher-li", "dispatcher", lock["id"], "JD-01", "")
        with self.assertRaises(ApiError):
            self.s.remove_grounding_lock("dispatcher-li", "dispatcher", lock["id"], "JD-XX", "photo://1")
        removed = self.s.remove_grounding_lock("dispatcher-li", "dispatcher", lock["id"], "JD-01", "photo://1")
        self.assertEqual("removed", removed["status"])
        self.assertEqual("dispatcher-li", removed["removed_by"])
        with self.assertRaises(ApiError):
            self.s.remove_grounding_lock("dispatcher-li", "dispatcher", lock["id"], "JD-01", "photo://2")

    def test_pending_promoted_after_removal_and_publish_restored(self):
        outage, plan = self.plan()
        self.lock(outage)
        self.s.field_report("field", "field", plan["id"], 1, "p-1", plan["version"], "completed")
        self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 1, "confirmed")
        r2 = self.s.field_report("field", "field", plan["id"], 2, "p-2", plan["version"], "completed")
        r3 = self.s.field_report("field", "field", plan["id"], 3, "p-3", plan["version"], "completed")
        self.assertEqual("pending", r2["merge_status"]); self.assertEqual("pending", r3["merge_status"])
        removed = self.s.remove_grounding_lock("dispatcher-li", "dispatcher", 1, "JD-01", "photo://1")
        self.assertEqual(sorted([r2["id"], r3["id"]]), sorted(removed["promoted_reports"]))
        self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 2, "confirmed")
        self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 3, "confirmed")
        status = self.s.publish_status("dispatcher", "dispatcher", outage["id"], plan["id"])
        self.assertEqual("restored", status["status"]["state"])
        self.assertEqual(0, status["status"]["grounding_locks_active"])

    def test_publish_restored_blocked_while_lock_active(self):
        outage, plan = self.plan()
        for seq in (1, 2, 3):
            self.s.field_report("field", "field", plan["id"], seq, f"c-{seq}", plan["version"], "completed")
            self.s.confirm_step("dispatcher", "dispatcher", plan["id"], seq, "confirmed")
        self.lock(outage)
        with self.assertRaises(ApiError):
            self.s.publish_status("dispatcher", "dispatcher", outage["id"], plan["id"])
        self.s.remove_grounding_lock("dispatcher-li", "dispatcher", 1, "JD-01", "photo://1")
        status = self.s.publish_status("dispatcher", "dispatcher", outage["id"], plan["id"])
        self.assertEqual("restored", status["status"]["state"])

    def test_grounding_view_per_outage(self):
        outage, plan = self.plan()
        self.lock(outage)
        self.s.field_report("field", "field", plan["id"], 2, "v-2", plan["version"], "completed")
        view = self.s.grounding_view(outage["id"])
        self.assertFalse(view["publish_ready"])
        self.assertEqual({"LINE", "FEEDER"}, {x["asset_code"] for x in view["locked_lines"]})
        self.assertEqual([2], [x["step_no"] for x in view["pending_steps"]])
        self.assertTrue(any("JD-01" in c for c in view["release_conditions"]))
        self.s.remove_grounding_lock("dispatcher-li", "dispatcher", 1, "JD-01", "photo://1")
        view = self.s.grounding_view(outage["id"])
        self.assertTrue(view["publish_ready"])
        self.assertEqual([], view["locked_lines"]); self.assertEqual([], view["pending_steps"])

    def test_legacy_field_reports_table_migrates(self):
        path = Path(self.tmp.name) / "legacy.db"
        conn = sqlite3.connect(path)
        conn.execute("""CREATE TABLE field_reports (
          id INTEGER PRIMARY KEY AUTOINCREMENT, client_report_id TEXT UNIQUE NOT NULL, plan_id INTEGER NOT NULL REFERENCES plans(id),
          step_no INTEGER NOT NULL, expected_plan_version INTEGER NOT NULL, status TEXT NOT NULL,
          note TEXT, merge_status TEXT NOT NULL CHECK(merge_status IN ('merged','conflict','protected')),
          conflict_reason TEXT, reported_by TEXT NOT NULL, received_at TEXT NOT NULL)""")
        conn.commit(); conn.close()
        store = Store(path)
        sql = store.conn.execute("SELECT sql FROM sqlite_master WHERE name='field_reports'").fetchone()[0]
        self.assertIn("'pending'", sql)
        store.close()


if __name__ == "__main__": unittest.main()
