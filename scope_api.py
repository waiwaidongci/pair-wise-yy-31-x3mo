"""范围修正接口处理：把 HTTP 请求映射到 AmendmentService。

POST /api/recalls/{id}/amendments  企业提交待审修正（/api/recalls/{id}/scope 为兼容别名）
GET  /api/recalls/{id}/amendments  查看待审范围、差异和处置结果
POST /api/amendments/{id}/review   监管审核（approve / reject）
"""
from __future__ import annotations


def dispatch(amendments, method: str, parts: list[str], body: dict, actor: str | None, role: str | None):
    """处理范围修正相关接口，返回响应内容；路径不匹配时返回 None。"""
    if len(parts) == 4 and parts[:2] == ["api", "recalls"] and parts[3] in {"amendments", "scope"}:
        if method == "POST":
            return amendments.submit(actor, role, int(parts[2]), body.get("scope", {}), body.get("reason", ""),
                                     int(body.get("expected_version", -1)), body.get("request_key", ""))
        if method == "GET" and parts[3] == "amendments":
            return {"amendments": amendments.list_for_recall(actor, role, int(parts[2]))}
    if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "amendments"] and parts[3] == "review":
        return amendments.review(actor, role, int(parts[2]), body.get("decision", ""), body.get("note", ""))
    return None
