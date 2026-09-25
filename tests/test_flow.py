import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scope_api
from app import ApiError, RecallService, Store
from scope_amendments import AmendmentError


class RecallFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.s = RecallService(Store(Path(self.tmp.name) / "r.db"))
        self.dealer_cn = self.s.register_dealer("reg", "regulator", "D-CN", "中国中心", "CN")
        self.dealer_sg = self.s.register_dealer("reg", "regulator", "D-SG", "新加坡中心", "SG")

    def tearDown(self): self.s.store.close(); self.tmp.cleanup()

    def make_recall(self):
        r = self.s.create_recall("maker", "manufacturer", "RC-1", "制动检查", {"models": ["X"], "model_years": [2018], "vin_prefixes": ["LX"], "countries": ["CN"]}, {"version": 1, "description": "更换软管"})
        r = self.s.submit_recall("maker", "manufacturer", r["id"], r["revision"])
        return self.s.review_recall("reg", "regulator", r["id"], "publish", r["revision"], "同意发布")

    def notifications(self, recall_id):
        rows = self.s.store.conn.execute("SELECT vehicle_id, scope_version, status FROM notifications WHERE recall_id=? ORDER BY vehicle_id, scope_version", (recall_id,)).fetchall()
        result = {}
        for row in rows: result.setdefault(row["vehicle_id"], []).append((row["scope_version"], row["status"]))
        return result

    def test_publish_cross_border_repair_and_unfinished(self):
        recall = self.make_recall()
        vehicle = self.s.register_vehicle("maker", "manufacturer", "LX00001", "X", 2018, "CN", "张三")
        self.s.register_vehicle("maker", "manufacturer", "LX00002", "X", 2018, "CN", "李四")
        self.s.transfer_vehicle("dealer", "dealer", vehicle["vin"], "SG", "Wang")
        self.s.add_parts("maker", "manufacturer", recall["id"], self.dealer_sg["id"], 1, 2)
        report = self.s.report_repair("dealer", "dealer", recall["id"], vehicle["vin"], self.dealer_sg["id"], 1, "abc123", True, "BP-9", "repair-1")
        self.assertEqual("reported", report["status"])
        confirmed = self.s.review_repair("reg", "regulator", report["id"], "confirm", "证据一致")
        self.assertEqual("confirmed", confirmed["status"])
        before = self.s.unfinished("reg", "regulator", recall["id"])
        self.assertEqual(1, before["unfinished_count"])
        same_scope = {"models": ["X"], "model_years": [2018], "vin_prefixes": ["LX"], "countries": ["CN"]}
        amendment = self.s.amendments.submit("maker", "manufacturer", recall["id"], same_scope, "范围确认不变", recall["revision"], "req-1")
        approved = self.s.amendments.review("reg", "regulator", amendment["id"], "approve", "同意")
        self.assertEqual(2, approved["target_scope_version"])
        detail = self.s.recall_detail(recall["id"])
        self.assertEqual(2, detail["scope_version"])
        self.assertEqual(2, len(detail["reports"]))
        self.assertEqual("confirmed", detail["repairs"][0]["status"])

    def test_shortage_wrong_remedy_permissions_and_duplicate(self):
        recall = self.make_recall()
        v = self.s.register_vehicle("maker", "manufacturer", "LX10000", "X", 2018, "CN", "赵六")
        with self.assertRaises(ApiError):
            self.s.review_recall("dealer", "dealer", recall["id"], "publish", recall["revision"])
        self.s.add_parts("maker", "manufacturer", recall["id"], self.dealer_cn["id"], 1, 1)
        with self.assertRaises(ApiError) as ctx:
            self.s.report_repair("dealer", "dealer", recall["id"], v["vin"], self.dealer_cn["id"], 2, "bad", True, idempotency_key="wrong")
        self.assertIn("版本", ctx.exception.message)
        first = self.s.report_repair("dealer", "dealer", recall["id"], v["vin"], self.dealer_cn["id"], 1, "ok", False, idempotency_key="idem")
        duplicate = self.s.report_repair("dealer", "dealer", recall["id"], v["vin"], self.dealer_cn["id"], 1, "ok", False, idempotency_key="idem")
        self.assertEqual(first["id"], duplicate["id"])
        with self.assertRaises(ApiError):
            self.s.report_repair("dealer", "dealer", recall["id"], v["vin"], self.dealer_cn["id"], 1, "ok", True, idempotency_key="other")
        flagged = self.s.review_repair("reg", "regulator", first["id"], "confirm")
        self.assertEqual("flagged", flagged["status"])

    def test_scope_amendment_pending_then_approval(self):
        keep = self.s.register_vehicle("maker", "manufacturer", "LX00001", "X", 2018, "CN", "张三")
        drop = self.s.register_vehicle("maker", "manufacturer", "LX00002", "X", 2018, "CN", "李四")
        recall = self.make_recall()  # 发布时为 keep/drop 各生成一条待办通知
        add = self.s.register_vehicle("maker", "manufacturer", "LX00003", "X", 2019, "CN", "王五")
        self.s.add_parts("maker", "manufacturer", recall["id"], self.dealer_cn["id"], 1, 1)
        report = self.s.report_repair("dealer", "dealer", recall["id"], drop["vin"], self.dealer_cn["id"], 1, "ok", True, idempotency_key="r1")
        self.s.review_repair("reg", "regulator", report["id"], "confirm")
        new_scope = {"models": ["X"], "model_years": [2019], "vin_prefixes": ["LX"], "countries": ["CN"]}
        amendment = self.s.amendments.submit("maker", "manufacturer", recall["id"], new_scope, "年款范围更正为2019", recall["revision"], "req-1")
        self.assertEqual("pending", amendment["status"])
        self.assertEqual(["LX00003"], amendment["diff"]["added_vins"])
        self.assertEqual(["LX00001", "LX00002"], amendment["diff"]["removed_vins"])
        self.assertEqual({"added": [2019], "removed": [2018]}, amendment["diff"]["fields"]["model_years"])
        again = self.s.amendments.submit("maker", "manufacturer", recall["id"], new_scope, "年款范围更正为2019", recall["revision"], "req-1")
        self.assertEqual(amendment["id"], again["id"])  # 重复申请沿用首次结果
        with self.assertRaises(AmendmentError):  # 已有待审修正时不同请求键的申请冲突
            self.s.amendments.submit("maker", "manufacturer", recall["id"], new_scope, "另一条", recall["revision"], "req-2")
        with self.assertRaises(ApiError):  # 通过前原范围照常执行：新范围车辆不能维修
            self.s.report_repair("dealer", "dealer", recall["id"], add["vin"], self.dealer_cn["id"], 1, "ok", True, idempotency_key="r2")
        before = self.s.unfinished("reg", "regulator", recall["id"])
        self.assertEqual(["LX00001"], [v["vin"] for v in before["vehicles"]])
        approved = self.s.amendments.review("reg", "regulator", amendment["id"], "approve", "同意更正")
        self.assertEqual("approved", approved["status"])
        disp = approved["disposition"]
        self.assertEqual(2, disp["scope_version"])
        self.assertEqual(["LX00003"], disp["added_vins"])
        self.assertEqual(["LX00001", "LX00002"], disp["removed_vins"])
        self.assertEqual(1, disp["notifications_created"])
        self.assertEqual(2, disp["notifications_revoked"])
        self.assertEqual(1, disp["confirmed_repairs_retained"])
        detail = self.s.recall_detail(recall["id"])
        self.assertEqual(2, detail["scope_version"])
        self.assertEqual(2, len(detail["reports"]))
        self.assertEqual(1, len(detail["scope_history"]))
        self.assertEqual("年款范围更正为2019", detail["scope_history"][0]["reason"])
        self.assertEqual(amendment["id"], detail["scope_history"][0]["amendment_id"])
        self.assertEqual("confirmed", detail["repairs"][0]["status"])  # 确认过的维修保留
        notif = self.notifications(recall["id"])
        self.assertEqual([(1, "revoked")], notif[keep["id"]])
        self.assertEqual([(1, "revoked")], notif[drop["id"]])
        self.assertEqual([(2, "queued")], notif[add["id"]])
        after = self.s.unfinished("reg", "regulator", recall["id"])  # 移出车辆不再算未完成
        self.assertEqual(["LX00003"], [v["vin"] for v in after["vehicles"]])
        self.s.add_parts("maker", "manufacturer", recall["id"], self.dealer_cn["id"], 1, 1)
        ok = self.s.report_repair("dealer", "dealer", recall["id"], add["vin"], self.dealer_cn["id"], 1, "ok", True, idempotency_key="r3")
        self.assertEqual("reported", ok["status"])
        with self.assertRaises(ApiError):  # 移出车辆不能再按该召回维修
            self.s.report_repair("dealer", "dealer", recall["id"], keep["vin"], self.dealer_cn["id"], 1, "ok", True, idempotency_key="r4")

    def test_amendment_reject_conflict_and_permissions(self):
        self.s.register_vehicle("maker", "manufacturer", "LX00001", "X", 2018, "CN", "张三")
        recall = self.make_recall()
        new_scope = {"models": ["X"], "model_years": [2018, 2019], "vin_prefixes": ["LX"], "countries": ["CN"]}
        with self.assertRaises(AmendmentError):
            self.s.amendments.submit("maker", "manufacturer", recall["id"], new_scope, "", recall["revision"], "req-1")
        with self.assertRaises(AmendmentError):
            self.s.amendments.submit("maker", "manufacturer", recall["id"], new_scope, "原因", recall["revision"], "")
        with self.assertRaises(AmendmentError):  # 版本号不符
            self.s.amendments.submit("maker", "manufacturer", recall["id"], new_scope, "原因", recall["revision"] + 1, "req-1")
        with self.assertRaises(AmendmentError):  # 非本机构企业
            self.s.amendments.submit("other", "manufacturer", recall["id"], new_scope, "原因", recall["revision"], "req-1")
        amendment = self.s.amendments.submit("maker", "manufacturer", recall["id"], new_scope, "扩大年款", recall["revision"], "req-1")
        with self.assertRaises(AmendmentError):  # 监管以外角色不能审核
            self.s.amendments.review("maker", "manufacturer", amendment["id"], "approve")
        rejected = self.s.amendments.review("reg", "regulator", amendment["id"], "reject", "依据不足")
        self.assertEqual("rejected", rejected["status"])
        self.assertIsNone(rejected["disposition"])
        with self.assertRaises(AmendmentError):  # 已处理的申请不能重复审核
            self.s.amendments.review("reg", "regulator", amendment["id"], "approve")
        detail = self.s.recall_detail(recall["id"])  # 驳回后原范围不变
        self.assertEqual(1, detail["scope_version"])
        self.assertEqual([2018], detail["scope"]["model_years"])
        again = self.s.amendments.submit("maker", "manufacturer", recall["id"], new_scope, "扩大年款", recall["revision"], "req-1")
        self.assertEqual("rejected", again["status"])  # 同一请求键沿用首次结果
        pending = self.s.amendments.submit("maker", "manufacturer", recall["id"], new_scope, "补充依据后重报", recall["revision"], "req-2")
        self.assertEqual("pending", pending["status"])

    def test_amendment_api_dispatch(self):
        recall = self.make_recall()
        scope = {"models": ["X"], "model_years": [2018, 2019], "vin_prefixes": ["LX"], "countries": ["CN"]}
        out = scope_api.dispatch(self.s.amendments, "POST", ["api", "recalls", str(recall["id"]), "scope"],
                                 {"scope": scope, "reason": "兼容旧接口", "expected_version": recall["revision"], "request_key": "k1"}, "maker", "manufacturer")
        self.assertEqual("pending", out["status"])
        listed = scope_api.dispatch(self.s.amendments, "GET", ["api", "recalls", str(recall["id"]), "amendments"], {}, "reg", "regulator")
        self.assertEqual(1, len(listed["amendments"]))
        reviewed = scope_api.dispatch(self.s.amendments, "POST", ["api", "amendments", str(out["id"]), "review"], {"decision": "approve"}, "reg", "regulator")
        self.assertEqual("approved", reviewed["status"])
        self.assertIsNone(scope_api.dispatch(self.s.amendments, "GET", ["api", "health"], {}, None, None))


if __name__ == "__main__": unittest.main()
