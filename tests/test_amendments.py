import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, RecallService, Store

OLD_SCOPE = {"models": ["X"], "model_years": [2018], "vin_prefixes": ["LX", "LY"], "countries": ["CN"]}
NEW_SCOPE = {"models": ["X"], "model_years": [2018, 2019], "vin_prefixes": ["LX"], "countries": ["CN"]}


class AmendmentFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = RecallService(Store(Path(self.tmp.name) / "r.db"))
        self.dealer = self.s.register_dealer("reg", "regulator", "D-CN", "中国中心", "CN")
        recall = self.s.create_recall("maker", "manufacturer", "RC-1", "制动检查", OLD_SCOPE, {"version": 1, "description": "更换软管"})
        # stay: 新旧范围都在; added: 仅新范围; removed: 仅旧范围; fixed: 仅旧范围且已确认维修
        self.stay = self.s.register_vehicle("maker", "manufacturer", "LXSTAY1", "X", 2018, "CN", "甲")
        self.added = self.s.register_vehicle("maker", "manufacturer", "LXADD01", "X", 2019, "CN", "乙")
        self.removed = self.s.register_vehicle("maker", "manufacturer", "LYREM01", "X", 2018, "CN", "丙")
        self.fixed = self.s.register_vehicle("maker", "manufacturer", "LYFIX01", "X", 2018, "CN", "丁")
        recall = self.s.submit_recall("maker", "manufacturer", recall["id"], recall["revision"])
        self.recall = self.s.review_recall("reg", "regulator", recall["id"], "publish", recall["revision"], "同意发布")
        self.s.add_parts("maker", "manufacturer", self.recall["id"], self.dealer["id"], 1, 4)
        report = self.s.report_repair("dealer", "dealer", self.recall["id"], self.fixed["vin"], self.dealer["id"], 1, "ev1", True, idempotency_key="rp-1")
        self.s.review_repair("reg", "regulator", report["id"], "confirm", "证据一致")

    def tearDown(self):
        self.s.store.close()
        self.tmp.cleanup()

    def notifications(self, vehicle_id):
        return [dict(r) for r in self.s.conn.execute(
            "SELECT * FROM notifications WHERE recall_id=? AND vehicle_id=? ORDER BY id", (self.recall["id"], vehicle_id))]

    def submit(self, key="am-1"):
        return self.s.submit_amendment("maker", "manufacturer", self.recall["id"], NEW_SCOPE, "补充2019年款并收缩VIN前缀", self.recall["revision"], key)

    def approve(self, amendment):
        return self.s.review_amendment("reg", "regulator", self.recall["id"], amendment["id"], "approve", "依据充分")

    def test_pending_keeps_old_scope_and_duplicate_reuses_first(self):
        amendment = self.submit()
        self.assertEqual("pending", amendment["status"])
        self.assertFalse(amendment["reused"])
        self.assertEqual(1, amendment["base_scope_version"])
        self.assertEqual([self.added["vin"]], [v["vin"] for v in amendment["diff"]["added"]])
        self.assertEqual({self.removed["vin"], self.fixed["vin"]}, {v["vin"] for v in amendment["diff"]["removed"]})
        # 监管通过前原范围照常执行
        detail = self.s.recall_detail(self.recall["id"])
        self.assertEqual(1, detail["scope_version"])
        self.assertEqual(OLD_SCOPE, detail["scope"])
        pending = self.s.unfinished("reg", "regulator", self.recall["id"])
        self.assertEqual({self.stay["vin"], self.removed["vin"]}, {v["vin"] for v in pending["vehicles"]})
        # 重复申请沿用首次结果：同幂等键、同内容各一次
        again = self.submit()
        self.assertTrue(again["reused"])
        self.assertEqual(amendment["id"], again["id"])
        same_content = self.s.submit_amendment("maker", "manufacturer", self.recall["id"], NEW_SCOPE, "补充2019年款并收缩VIN前缀", self.recall["revision"])
        self.assertTrue(same_content["reused"])
        self.assertEqual(amendment["id"], same_content["id"])
        self.assertEqual(1, len(self.s.list_amendments(self.recall["id"])))
        # 待审期间不允许另一笔不同内容的申请
        other_scope = dict(NEW_SCOPE, countries=["CN", "SG"])
        with self.assertRaises(ApiError) as ctx:
            self.s.submit_amendment("maker", "manufacturer", self.recall["id"], other_scope, "另一份修正", self.recall["revision"], "am-2")
        self.assertIn("待审", ctx.exception.message)

    def test_approve_disposes_notifications_reports_and_unfinished(self):
        amendment = self.submit()
        reviewed = self.approve(amendment)
        self.assertEqual("approved", reviewed["status"])
        result = reviewed["result"]
        self.assertEqual(2, result["scope_version"])
        self.assertEqual(1, result["added_count"])
        self.assertEqual(2, result["removed_count"])
        self.assertEqual(1, result["notifications_created"])
        self.assertEqual(2, result["notifications_cancelled"])
        # 新增车辆进入通知队列，移出车辆的未完成通知撤销
        self.assertEqual([("queued", 2)], [(n["status"], n["scope_version"]) for n in self.notifications(self.added["id"])])
        self.assertEqual(["cancelled"], [n["status"] for n in self.notifications(self.removed["id"])])
        self.assertEqual(["cancelled"], [n["status"] for n in self.notifications(self.fixed["id"])])
        self.assertEqual([("queued", 1)], [(n["status"], n["scope_version"]) for n in self.notifications(self.stay["id"])])
        # 新版本上报进入队列，且带依据
        detail = self.s.recall_detail(self.recall["id"])
        self.assertEqual(2, detail["scope_version"])
        self.assertEqual(NEW_SCOPE, detail["scope"])
        self.assertEqual(2, len(detail["reports"]))
        payload = json.loads(detail["reports"][1]["payload_json"])
        self.assertEqual("queued", detail["reports"][1]["status"])
        self.assertEqual("补充2019年款并收缩VIN前缀", payload["reason"])
        self.assertEqual(amendment["id"], payload["amendment_id"])
        # 确认过的维修保留但不再算未完成；移出车辆也不再算
        self.assertEqual("confirmed", detail["repairs"][0]["status"])
        after = self.s.unfinished("reg", "regulator", self.recall["id"])
        self.assertEqual({self.stay["vin"], self.added["vin"]}, {v["vin"] for v in after["vehicles"]})
        # 版本留档：v1 首次发布、v2 修正落版均归档且带原因
        rows = self.s.conn.execute("SELECT scope_version,reason,amendment_id FROM scope_changes WHERE recall_id=? ORDER BY scope_version", (self.recall["id"],)).fetchall()
        self.assertEqual([(1, "首次发布", None), (2, "补充2019年款并收缩VIN前缀", amendment["id"])],
                         [(r["scope_version"], r["reason"], r["amendment_id"]) for r in rows])
        # 已审核申请不能重复审核
        with self.assertRaises(ApiError):
            self.approve(amendment)

    def test_reject_keeps_original_scope(self):
        amendment = self.submit()
        reviewed = self.s.review_amendment("reg", "regulator", self.recall["id"], amendment["id"], "reject", "依据不足")
        self.assertEqual("rejected", reviewed["status"])
        self.assertIsNone(reviewed["result"])
        detail = self.s.recall_detail(self.recall["id"])
        self.assertEqual(1, detail["scope_version"])
        self.assertEqual(OLD_SCOPE, detail["scope"])
        self.assertEqual(1, len(detail["reports"]))
        self.assertEqual(["queued"], [n["status"] for n in self.notifications(self.removed["id"])])
        # 退回后可再次申请
        again = self.s.submit_amendment("maker", "manufacturer", self.recall["id"], NEW_SCOPE, "补充材料后重新提交", detail["revision"], "am-2")
        self.assertEqual("pending", again["status"])

    def test_submit_validation_and_permissions(self):
        with self.assertRaises(ApiError):
            self.s.submit_amendment("dealer", "dealer", self.recall["id"], NEW_SCOPE, "越权", self.recall["revision"])
        with self.assertRaises(ApiError):
            self.s.submit_amendment("other-maker", "manufacturer", self.recall["id"], NEW_SCOPE, "非本机构", self.recall["revision"])
        with self.assertRaises(ApiError) as ctx:
            self.s.submit_amendment("maker", "manufacturer", self.recall["id"], NEW_SCOPE, "", self.recall["revision"])
        self.assertIn("原因", ctx.exception.message)
        with self.assertRaises(ApiError) as ctx:
            self.s.submit_amendment("maker", "manufacturer", self.recall["id"], NEW_SCOPE, "版本不对", self.recall["revision"] + 9)
        self.assertIn("刷新版本", ctx.exception.message)
        with self.assertRaises(ApiError):
            self.s.submit_amendment("maker", "manufacturer", self.recall["id"], {"models": ["X"]}, "范围缺字段", self.recall["revision"])
        with self.assertRaises(ApiError):
            self.s.review_amendment("maker", "manufacturer", self.recall["id"], 1, "approve")


if __name__ == "__main__":
    unittest.main()
