"""范围修正的版本留档：待审申请、监管审核落版、通知与监管上报队列处置。

企业通过 submit 提交新范围和原因，申请处于 pending 期间原范围照常执行；
监管 review 通过后才写入新范围版本并归档 scope_changes，同时把新增车辆
送入通知/上报队列、撤销移出车辆的未完成通知。重复申请沿用首次结果。
"""
from __future__ import annotations

import json
import sqlite3

from . import ApiError, j, now
from .diff import diff_vehicles, in_scope

PENDING, APPROVED, REJECTED = "pending", "approved", "rejected"


def _to_dict(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "recall_id": row["recall_id"], "base_scope_version": row["base_scope_version"],
            "scope": json.loads(row["scope_json"]), "reason": row["reason"], "status": row["status"],
            "diff": json.loads(row["diff_json"]) if row["diff_json"] else None,
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "idempotency_key": row["idempotency_key"],
            "created_by": row["created_by"], "created_at": row["created_at"],
            "reviewed_by": row["reviewed_by"], "reviewed_at": row["reviewed_at"], "review_note": row["review_note"]}


def get(conn: sqlite3.Connection, amendment_id: int) -> dict:
    row = conn.execute("SELECT * FROM scope_amendments WHERE id=?", (amendment_id,)).fetchone()
    if not row:
        raise ApiError(404, "范围修正申请不存在")
    return _to_dict(row)


def list_for_recall(conn: sqlite3.Connection, recall_id: int) -> list[dict]:
    return [_to_dict(row) for row in conn.execute(
        "SELECT * FROM scope_amendments WHERE recall_id=? ORDER BY id DESC", (recall_id,))]


def archive_version(conn: sqlite3.Connection, recall_id: int, scope_version: int, scope_json: str,
                    actor: str, reason: str = "", amendment_id: int | None = None) -> None:
    """把生效的范围版本写入留档表，保留依据（原因和来源申请）。"""
    conn.execute("""INSERT OR IGNORE INTO scope_changes(recall_id,scope_version,scope_json,reason,amendment_id,created_by,created_at)
                    VALUES(?,?,?,?,?,?,?)""",
                 (recall_id, scope_version, scope_json, reason, amendment_id, actor, now()))


def submit(conn: sqlite3.Connection, store, recall: sqlite3.Row, scope: dict, reason: str,
           actor: str, idempotency_key: str = "") -> tuple[dict, bool]:
    """登记待审修正，返回 (申请, 是否沿用首次结果)。同一召回同时只允许一笔待审。"""
    if idempotency_key:
        row = conn.execute("SELECT id FROM scope_amendments WHERE recall_id=? AND idempotency_key=?",
                           (recall["id"], idempotency_key)).fetchone()
        if row:
            return get(conn, row["id"]), True
    pending = conn.execute("SELECT * FROM scope_amendments WHERE recall_id=? AND status='pending'",
                           (recall["id"],)).fetchone()
    if pending:
        if json.loads(pending["scope_json"]) == scope and pending["reason"] == reason:
            return get(conn, pending["id"]), True
        raise ApiError(409, "已存在待审的范围修正申请，请等待监管审核")
    vehicles = conn.execute("SELECT * FROM vehicles ORDER BY id").fetchall()
    diff = diff_vehicles(json.loads(recall["scope_json"]), scope, vehicles)
    with conn:
        cur = conn.execute("""INSERT INTO scope_amendments(recall_id,base_scope_version,scope_json,reason,status,diff_json,
                              idempotency_key,created_by,created_at)
                              VALUES(?,?,?,?, 'pending',?,?,?,?)""",
                           (recall["id"], recall["scope_version"], j(scope), reason, j(diff),
                            idempotency_key or None, actor, now()))
        store.audit(actor, "amendment.submit", "amendment", cur.lastrowid,
                    {"recall_id": recall["id"], "base_scope_version": recall["scope_version"], "reason": reason,
                     "added": len(diff["added"]), "removed": len(diff["removed"])})
    return get(conn, cur.lastrowid), False


def review(conn: sqlite3.Connection, store, recall: sqlite3.Row, amendment_id: int,
           decision: str, actor: str, note: str = "") -> dict:
    """监管审核：reject 只标记申请；approve 落版新范围并处置通知/上报队列。"""
    if decision not in {"approve", "reject"}:
        raise ApiError(400, "决定只能是 approve 或 reject")
    row = conn.execute("SELECT * FROM scope_amendments WHERE id=?", (amendment_id,)).fetchone()
    if not row or row["recall_id"] != recall["id"]:
        raise ApiError(404, "范围修正申请不存在")
    if row["status"] != PENDING:
        raise ApiError(409, "该范围修正申请已审核")
    if decision == "reject":
        with conn:
            conn.execute("UPDATE scope_amendments SET status='rejected',reviewed_by=?,reviewed_at=?,review_note=? WHERE id=?",
                         (actor, now(), note, amendment_id))
            store.audit(actor, "amendment.reject", "amendment", amendment_id,
                        {"recall_id": recall["id"], "note": note})
        return get(conn, amendment_id)
    return _approve(conn, store, recall, row, actor, note)


def _approve(conn: sqlite3.Connection, store, recall: sqlite3.Row, amendment: sqlite3.Row,
             actor: str, note: str) -> dict:
    old_scope, new_scope = json.loads(recall["scope_json"]), json.loads(amendment["scope_json"])
    new_version = int(recall["scope_version"]) + 1
    vehicles = conn.execute("SELECT * FROM vehicles ORDER BY id").fetchall()
    diff = diff_vehicles(old_scope, new_scope, vehicles)
    with conn:
        conn.execute("UPDATE recalls SET scope_json=?,scope_version=?,revision=revision+1,updated_at=? WHERE id=?",
                     (amendment["scope_json"], new_version, now(), recall["id"]))
        archive_version(conn, recall["id"], new_version, amendment["scope_json"], actor,
                        amendment["reason"], amendment["id"])
        # 移出车辆的未完成通知撤销：凡不在新范围内，queued 通知一律取消
        cancelled = 0
        for vehicle in vehicles:
            if not in_scope(vehicle, new_scope):
                cur = conn.execute("UPDATE notifications SET status='cancelled' WHERE recall_id=? AND vehicle_id=? AND status='queued'",
                                   (recall["id"], vehicle["id"]))
                cancelled += cur.rowcount
        # 新增车辆进入通知队列：在新范围内且没有未完成通知的，按新版本补入
        created = 0
        for vehicle in vehicles:
            if in_scope(vehicle, new_scope):
                open_notice = conn.execute("SELECT 1 FROM notifications WHERE recall_id=? AND vehicle_id=? AND status='queued'",
                                           (recall["id"], vehicle["id"])).fetchone()
                if not open_notice:
                    conn.execute("""INSERT OR IGNORE INTO notifications(recall_id,vehicle_id,scope_version,channel,status,created_at)
                                    VALUES(?,?,?, 'owner-notice','queued',?)""",
                                 (recall["id"], vehicle["id"], new_version, now()))
                    created += 1
        affected = sum(1 for v in vehicles if in_scope(v, new_scope))
        payload = {"campaign_code": recall["campaign_code"], "scope_version": new_version, "scope": new_scope,
                   "remedy_version": recall["remedy_version"], "affected_count": affected,
                   "amendment_id": amendment["id"], "reason": amendment["reason"],
                   "added_count": len(diff["added"]), "removed_count": len(diff["removed"])}
        cur = conn.execute("""INSERT OR IGNORE INTO regulatory_reports(recall_id,scope_version,payload_json,status,created_at)
                              VALUES(?,?,?, 'queued',?)""",
                           (recall["id"], new_version, j(payload), now()))
        result = {"scope_version": new_version, "added_count": len(diff["added"]), "removed_count": len(diff["removed"]),
                  "notifications_created": created, "notifications_cancelled": cancelled,
                  "report_id": cur.lastrowid, "affected_count": affected}
        conn.execute("UPDATE scope_amendments SET status='approved',result_json=?,reviewed_by=?,reviewed_at=?,review_note=? WHERE id=?",
                     (j(result), actor, now(), note, amendment["id"]))
        store.audit(actor, "amendment.approve", "amendment", amendment["id"],
                    {"recall_id": recall["id"], "note": note, **result})
    return get(conn, amendment["id"])
