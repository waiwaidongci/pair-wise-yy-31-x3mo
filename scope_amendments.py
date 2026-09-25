"""范围修正版本留档：待审修正的登记、审核、归档与处置。

企业提交的新范围先登记为待审修正（含原因、版本号和差异），监管通过前
原范围照常执行；通过后新范围才写入召回并归档到 scope_changes，同时完成
通知补发/撤销和监管上报等处置。重复申请（同一请求键）沿用首次登记的结果。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import scope_diff


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _j(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class AmendmentError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


class AmendmentService:
    """召回范围的待审修正（每个召回同一时间只允许一条待审）。"""

    def __init__(self, store):
        self.store, self.conn = store, store.conn

    @staticmethod
    def _check_role(actor: str | None, role: str | None, allowed: set[str]) -> str:
        if not actor: raise AmendmentError(401, "缺少身份")
        if role not in allowed: raise AmendmentError(403, "角色无权执行此操作")
        return actor

    def _row(self, table: str, identity: object, column: str = "id"):
        row = self.conn.execute(f"SELECT * FROM {table} WHERE {column}=?", (identity,)).fetchone()
        if not row: raise AmendmentError(404, "对象不存在")
        return row

    @staticmethod
    def _dict(row) -> dict:
        return {"id": row["id"], "recall_id": row["recall_id"],
                "base_scope_version": row["base_scope_version"], "target_scope_version": row["target_scope_version"],
                "scope": json.loads(row["scope_json"]), "reason": row["reason"], "diff": json.loads(row["diff_json"]),
                "status": row["status"], "request_key": row["request_key"],
                "submitted_by": row["submitted_by"], "submitted_at": row["submitted_at"],
                "reviewed_by": row["reviewed_by"], "reviewed_at": row["reviewed_at"], "review_note": row["review_note"],
                "disposition": json.loads(row["disposition_json"]) if row["disposition_json"] else None}

    def for_recall(self, recall_id: int) -> list[dict]:
        return [self._dict(row) for row in self.conn.execute("SELECT * FROM scope_amendments WHERE recall_id=? ORDER BY id DESC", (recall_id,))]

    def list_for_recall(self, actor: str | None, role: str | None, recall_id: int) -> list[dict]:
        self._check_role(actor, role, {"manufacturer", "regulator"})
        self._row("recalls", recall_id)
        return self.for_recall(recall_id)

    def list_all(self) -> list[dict]:
        return [self._dict(row) for row in self.conn.execute("SELECT * FROM scope_amendments ORDER BY id DESC")]

    def submit(self, actor: str | None, role: str | None, recall_id: int, scope: dict, reason: str, expected_version: int, request_key: str) -> dict:
        actor = self._check_role(actor, role, {"manufacturer"})
        try: scope_diff.validate_scope(scope)
        except ValueError as exc: raise AmendmentError(400, str(exc)) from exc
        reason, request_key = (reason or "").strip(), (request_key or "").strip()
        if not reason: raise AmendmentError(400, "修正原因不能为空")
        if not request_key: raise AmendmentError(400, "请求键不能为空")
        recall = self._row("recalls", recall_id)
        if recall["manufacturer"] != actor: raise AmendmentError(403, "只能调整本机构的召回范围")
        if recall["state"] != "published": raise AmendmentError(409, "只有已发布召回可以调整范围")
        if int(expected_version) != int(recall["revision"]): raise AmendmentError(409, "召回已被修改，请刷新版本")
        existing = self.conn.execute("SELECT * FROM scope_amendments WHERE recall_id=? AND request_key=?", (recall_id, request_key)).fetchone()
        if existing: return self._dict(existing)  # 重复申请沿用首次结果
        pending = self.conn.execute("SELECT id FROM scope_amendments WHERE recall_id=? AND status='pending'", (recall_id,)).fetchone()
        if pending: raise AmendmentError(409, "已有待审核的范围修正，请等待监管处理")
        current_scope = json.loads(recall["scope_json"])
        vehicles = self.conn.execute("SELECT * FROM vehicles ORDER BY id").fetchall()
        diff = scope_diff.scope_diff(current_scope, scope, vehicles)
        target_version = int(recall["scope_version"]) + 1
        with self.conn:
            cur = self.conn.execute("""INSERT INTO scope_amendments(recall_id,base_scope_version,target_scope_version,scope_json,reason,diff_json,status,request_key,submitted_by,submitted_at)
                                     VALUES(?,?,?,?,?,?, 'pending',?,?,?)""",
                                    (recall_id, int(recall["scope_version"]), target_version, _j(scope), reason, _j(diff), request_key, actor, _now()))
            self.store.audit(actor, "recall.amendment.submit", "amendment", cur.lastrowid,
                             {"recall_id": recall_id, "target_scope_version": target_version, "reason": reason,
                              "added_vins": diff["added_vins"], "removed_vins": diff["removed_vins"]})
        return self._dict(self._row("scope_amendments", cur.lastrowid))

    def review(self, actor: str | None, role: str | None, amendment_id: int, decision: str, note: str = "") -> dict:
        actor = self._check_role(actor, role, {"regulator"})
        if decision not in {"approve", "reject"}: raise AmendmentError(400, "决定只能是 approve 或 reject")
        amendment = self._row("scope_amendments", amendment_id)
        if amendment["status"] != "pending": raise AmendmentError(409, "修正申请已处理")
        if decision == "reject":
            with self.conn:
                self.conn.execute("UPDATE scope_amendments SET status='rejected',reviewed_by=?,reviewed_at=?,review_note=? WHERE id=?",
                                  (actor, _now(), note, amendment_id))
                self.store.audit(actor, "recall.amendment.reject", "amendment", amendment_id, {"recall_id": amendment["recall_id"], "note": note})
            return self._dict(self._row("scope_amendments", amendment_id))
        return self._approve(actor, amendment, note)

    def _approve(self, actor: str, amendment, note: str) -> dict:
        recall = self._row("recalls", amendment["recall_id"])
        if int(recall["scope_version"]) != int(amendment["base_scope_version"]):
            raise AmendmentError(409, "召回范围已变化，请企业重新提交修正")
        old_scope, new_scope = json.loads(recall["scope_json"]), json.loads(amendment["scope_json"])
        new_version = int(amendment["target_scope_version"])
        vehicles = self.conn.execute("SELECT * FROM vehicles ORDER BY id").fetchall()
        added, removed = scope_diff.vehicle_diff(vehicles, old_scope, new_scope)
        removed_ids = [v["id"] for v in removed]
        retained = 0  # 移出车辆上已确认的维修：保留记录，只是不再计入未完成
        if removed_ids:
            marks = ",".join("?" * len(removed_ids))
            retained = self.conn.execute(f"SELECT COUNT(*) AS c FROM repairs WHERE recall_id=? AND status='confirmed' AND vehicle_id IN ({marks})",
                                         (amendment["recall_id"], *removed_ids)).fetchone()["c"]
        created = revoked = 0
        stamp = _now()
        with self.conn:
            self.conn.execute("UPDATE recalls SET scope_json=?,scope_version=?,revision=revision+1,updated_at=? WHERE id=?",
                              (_j(new_scope), new_version, stamp, amendment["recall_id"]))
            self.conn.execute("INSERT INTO scope_changes(recall_id,scope_version,scope_json,reason,amendment_id,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                              (amendment["recall_id"], new_version, _j(new_scope), amendment["reason"], amendment["id"], amendment["submitted_by"], stamp))
            for vehicle in added:  # 新增车辆进入通知队列
                cur = self.conn.execute("INSERT OR IGNORE INTO notifications(recall_id,vehicle_id,scope_version,channel,status,created_at) VALUES(?,?,?, 'owner-notice','queued',?)",
                                        (amendment["recall_id"], vehicle["id"], new_version, stamp))
                created += cur.rowcount
            for vehicle_id in removed_ids:  # 移出车辆的未完成通知撤销
                cur = self.conn.execute("UPDATE notifications SET status='revoked' WHERE recall_id=? AND vehicle_id=? AND status='queued'",
                                        (amendment["recall_id"], vehicle_id))
                revoked += cur.rowcount
            payload = {"campaign_code": recall["campaign_code"], "scope_version": new_version, "scope": new_scope,
                       "remedy_version": recall["remedy_version"], "affected_count": sum(1 for v in vehicles if scope_diff.in_scope(v, new_scope)),
                       "amendment_id": amendment["id"], "reason": amendment["reason"],
                       "added_vins": sorted(v["vin"] for v in added), "removed_vins": sorted(v["vin"] for v in removed)}
            cur = self.conn.execute("INSERT INTO regulatory_reports(recall_id,scope_version,payload_json,status,created_at) VALUES(?,?,?, 'queued',?)",
                                    (amendment["recall_id"], new_version, _j(payload), stamp))
            disposition = {"scope_version": new_version, "added_vins": sorted(v["vin"] for v in added),
                           "removed_vins": sorted(v["vin"] for v in removed), "notifications_created": created,
                           "notifications_revoked": revoked, "confirmed_repairs_retained": retained, "report_id": cur.lastrowid}
            self.conn.execute("UPDATE scope_amendments SET status='approved',reviewed_by=?,reviewed_at=?,review_note=?,disposition_json=? WHERE id=?",
                              (actor, stamp, note, _j(disposition), amendment["id"]))
            self.store.audit(actor, "recall.amendment.approve", "amendment", amendment["id"],
                             {"recall_id": amendment["recall_id"], "scope_version": new_version,
                              "notifications_created": created, "notifications_revoked": revoked, "confirmed_repairs_retained": retained})
        return self._dict(self._row("scope_amendments", amendment["id"]))
