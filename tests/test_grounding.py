import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, GridService, Store


class GroundingLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.s = GridService(Store(Path(self.tmp.name) / "g.db"))
        self.sub = self.s.register_asset("dispatcher", "dispatcher", "SUB", "中心站", "substation", 200, "A")
        self.line = self.s.register_asset("dispatcher", "dispatcher", "LINE", "一号线", "line", 100, "A", self.sub["id"])
        self.feeder = self.s.register_asset("dispatcher", "dispatcher", "FEEDER", "支线", "line", 60, "A", self.line["id"])

    def tearDown(self): self.s.store.close(); self.tmp.cleanup()

    def plan(self, code="OUT-G"):
        outage = self.s.create_outage("dispatcher", "dispatcher", code, "线路抢修", ["A"])
        plan = self.s.create_plan("dispatcher", "dispatcher", outage["id"], [
            {"seq": 1, "action": "检查", "asset": "SUB", "required_mw": 80},
            {"seq": 2, "action": "抢修", "asset": "LINE", "required_mw": 70, "depends_on": [1]},
            {"seq": 3, "action": "送电", "asset": "FEEDER", "required_mw": 50, "depends_on": [2]}])
        plan = self.s.submit_plan("dispatcher", "dispatcher", plan["id"], plan["revision"])
        plan = self.s.approve_plan("dispatcher", "dispatcher", plan["id"], plan["revision"])
        return outage, self.s.activate_plan("dispatcher", "dispatcher", plan["id"], plan["revision"])

    def hang(self, order="SO-1", wire="JD-01", actor="op-1", asset="LINE"):
        return self.s.register_grounding(actor, "operator", order, wire, "检修一班", "2026-09-27T08:00:00Z", asset, None, "")

    def report(self, plan, seq, cid, status="completed"):
        return self.s.field_report("field-1", "field", plan["id"], seq, cid, plan["version"], status)

    def test_register_idempotent_and_validation(self):
        g1 = self.hang()
        g2 = self.s.register_grounding("op-2", "operator", "SO-1", "JD-99", "别的班", "2026-09-27T09:00:00Z", "SUB", None, "")
        self.assertEqual(g1["id"], g2["id"])
        self.assertEqual("JD-01", g2["ground_wire_no"])
        self.assertEqual("检修一班", g2["crew"])
        with self.assertRaises(ApiError):
            self.s.register_grounding("op-1", "operator", "SO-2", "JD-02", "检修一班", "2026-09-27T08:00:00Z", "NOPE", None, "")
        with self.assertRaises(ApiError):
            self.s.register_grounding("op-1", "operator", "", "JD-02", "检修一班", "2026-09-27T08:00:00Z", "LINE", None, "")
        with self.assertRaises(ApiError):
            self.s.register_grounding("dispatcher", "dispatcher", "SO-3", "JD-03", "检修一班", "2026-09-27T08:00:00Z", "LINE", None, "")

    def test_lock_blocks_completion_on_line_and_downstream(self):
        outage, plan = self.plan()
        self.hang()
        self.assertEqual("merged", self.report(plan, 1, "c-1")["merge_status"])
        self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 1, "confirmed")
        self.assertEqual("held", self.report(plan, 2, "c-2")["merge_status"])
        self.assertEqual("held", self.report(plan, 3, "c-3")["merge_status"])
        self.assertEqual("merged", self.report(plan, 2, "c-2s", "started")["merge_status"])
        with self.assertRaises(ApiError):
            self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 2, "confirmed")
        with self.assertRaises(ApiError):
            self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 3, "confirmed")

    def test_removal_requires_second_officer_wire_match_and_photo(self):
        outage, plan = self.plan()
        g = self.hang()
        self.report(plan, 1, "c-1"); self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 1, "confirmed")
        self.report(plan, 2, "c-2")
        with self.assertRaises(ApiError):
            self.s.remove_grounding("op-1", "operator", g["id"], "JD-01", "photo://1")
        with self.assertRaises(ApiError):
            self.s.remove_grounding("op-2", "operator", g["id"], "JD-02", "photo://1")
        with self.assertRaises(ApiError):
            self.s.remove_grounding("op-2", "operator", g["id"], "JD-01", "")
        with self.assertRaises(ApiError):
            self.s.remove_grounding("dispatcher", "dispatcher", g["id"], "JD-01", "photo://1")
        done = self.s.remove_grounding("op-2", "operator", g["id"], "JD-01", "photo://1")
        self.assertTrue(done["removed"])
        self.assertEqual("op-2", done["removed_by"])
        with self.assertRaises(ApiError):
            self.s.remove_grounding("op-3", "operator", g["id"], "JD-01", "photo://2")
        reports = {r["client_report_id"]: r for r in self.s.plan_detail(plan["id"])["field_reports"]}
        self.assertEqual("merged", reports["c-2"]["merge_status"])
        confirmed = self.s.confirm_step("dispatcher", "dispatcher", plan["id"], 2, "confirmed")
        self.assertEqual("confirmed", confirmed["status"])

    def test_overlapping_locks_keep_reports_held(self):
        outage, plan = self.plan()
        g1 = self.hang("SO-1", "JD-01", "op-1", "LINE")
        g2 = self.hang("SO-2", "JD-02", "op-1", "FEEDER")
        self.report(plan, 3, "c-3")
        self.s.remove_grounding("op-2", "operator", g1["id"], "JD-01", "photo://1")
        reports = {r["client_report_id"]: r for r in self.s.plan_detail(plan["id"])["field_reports"]}
        self.assertEqual("held", reports["c-3"]["merge_status"])
        self.s.remove_grounding("op-2", "operator", g2["id"], "JD-02", "photo://2")
        reports = {r["client_report_id"]: r for r in self.s.plan_detail(plan["id"])["field_reports"]}
        self.assertEqual("merged", reports["c-3"]["merge_status"])

    def test_publish_restored_requires_all_grounding_removed(self):
        outage, plan = self.plan()
        for seq in (1, 2, 3):
            self.report(plan, seq, f"c-{seq}")
            self.s.confirm_step("dispatcher", "dispatcher", plan["id"], seq, "confirmed")
        g = self.hang()
        with self.assertRaises(ApiError):
            self.s.publish_status("dispatcher", "dispatcher", outage["id"], plan["id"])
        self.s.remove_grounding("op-2", "operator", g["id"], "JD-01", "photo://1")
        status = self.s.publish_status("dispatcher", "dispatcher", outage["id"], plan["id"])
        self.assertEqual("restored", status["status"]["state"])

    def test_overview_lists_locked_lines_pending_steps_and_conditions(self):
        outage, plan = self.plan()
        g = self.hang()
        self.report(plan, 2, "c-2")
        entry = [o for o in self.s.grounding_overview() if o["incident_code"] == "OUT-G"][0]
        self.assertEqual(["JD-01"], [l["ground_wire_no"] for l in entry["locked_lines"]])
        self.assertEqual("LINE", entry["locked_lines"][0]["asset_code"])
        pending = {p["step_no"]: p for p in entry["pending_steps"]}
        self.assertIn(2, pending); self.assertIn(3, pending); self.assertNotIn(1, pending)
        self.assertEqual(1, pending[2]["held_reports"])
        self.assertTrue(any("JD-01" in c and "另一名值班员" in c for c in entry["release_conditions"]))
        self.s.remove_grounding("op-2", "operator", g["id"], "JD-01", "photo://1")
        self.assertEqual([], [o for o in self.s.grounding_overview() if o["incident_code"] == "OUT-G"])


if __name__ == "__main__": unittest.main()
