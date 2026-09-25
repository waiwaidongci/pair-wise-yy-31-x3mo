"""HTTP 接口处理：路由分发、请求解析与错误响应。"""
from __future__ import annotations

import json
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

from . import ApiError

INDEX_HTML = Path(__file__).resolve().parent.parent / "static" / "index.html"


class Handler(BaseHTTPRequestHandler):
    service: "object"  # RecallService，由 app.run 注入

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, status: int, body: object) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        size = int(self.headers.get("Content-Length", "0"))
        try:
            return json.loads(self.rfile.read(size)) if size else {}
        except json.JSONDecodeError as exc:
            raise ApiError(400, "JSON 请求体无效") from exc

    def _parts(self) -> list[str]:
        return [p for p in urlparse(self.path).path.strip("/").split("/") if p]

    def do_GET(self) -> None:
        try:
            p = self._parts()
            if p in (["health"], ["api", "health"]):
                out = {"status": "ok"}
            elif p == ["api", "state"]:
                out = self.service.state()
            elif len(p) == 3 and p[:2] == ["api", "recalls"]:
                out = self.service.recall_detail(int(p[2]))
            elif len(p) == 4 and p[:2] == ["api", "recalls"] and p[3] == "unfinished":
                out = self.service.unfinished(self.headers.get("X-Actor"), self.headers.get("X-Role"), int(p[2]))
            elif len(p) == 4 and p[:2] == ["api", "recalls"] and p[3] == "amendments":
                out = {"amendments": self.service.list_amendments(int(p[2]))}
            elif not p:
                page = INDEX_HTML.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)
                return
            else:
                raise ApiError(404, "接口不存在")
            self._send(200, out)
        except ApiError as exc:
            self._send(exc.status, {"error": exc.message})
        except Exception as exc:
            self._send(500, {"error": str(exc)})

    def do_POST(self) -> None:
        try:
            p, body = self._parts(), self._body()
            actor, role = self.headers.get("X-Actor"), self.headers.get("X-Role")
            if p == ["api", "dealers"]:
                out = self.service.register_dealer(actor, role, body.get("code", ""), body.get("name", ""), body.get("country", ""))
            elif p == ["api", "vehicles"]:
                out = self.service.register_vehicle(actor, role, body.get("vin", ""), body.get("model", ""), int(body.get("model_year", 0)), body.get("country", ""), body.get("owner_name", ""))
            elif len(p) == 4 and p[:2] == ["api", "vehicles"] and p[3] == "transfer":
                out = self.service.transfer_vehicle(actor, role, p[2], body.get("country", ""), body.get("owner_name", ""))
            elif p == ["api", "recalls"]:
                out = self.service.create_recall(actor, role, body.get("campaign_code", ""), body.get("title", ""), body.get("scope", {}), body.get("remedy", {}))
            elif len(p) == 4 and p[:2] == ["api", "recalls"] and p[3] == "submit":
                out = self.service.submit_recall(actor, role, int(p[2]), int(body.get("expected_version", -1)))
            elif len(p) == 4 and p[:2] == ["api", "recalls"] and p[3] == "review":
                out = self.service.review_recall(actor, role, int(p[2]), body.get("decision", ""), int(body.get("expected_version", -1)), body.get("note", ""))
            elif len(p) == 4 and p[:2] == ["api", "recalls"] and p[3] == "amendments":
                out = self.service.submit_amendment(actor, role, int(p[2]), body.get("scope", {}), body.get("reason", ""),
                                                    int(body.get("expected_version", -1)), body.get("idempotency_key", ""))
            elif len(p) == 6 and p[:2] == ["api", "recalls"] and p[3] == "amendments" and p[5] == "review":
                out = self.service.review_amendment(actor, role, int(p[2]), int(p[4]), body.get("decision", ""), body.get("note", ""))
            elif len(p) == 4 and p[:2] == ["api", "recalls"] and p[3] == "parts":
                out = self.service.add_parts(actor, role, int(p[2]), int(body.get("dealer_id", 0)), int(body.get("remedy_version", 0)), int(body.get("quantity", 0)))
            elif p == ["api", "repairs"]:
                out = self.service.report_repair(actor, role, int(body.get("recall_id", 0)), body.get("vin", ""), int(body.get("dealer_id", 0)), int(body.get("remedy_version", 0)), body.get("evidence_hash", ""), bool(body.get("evidence_consistent", True)), body.get("border_permit", ""), body.get("idempotency_key", ""))
            elif len(p) == 4 and p[:2] == ["api", "repairs"] and p[3] == "review":
                out = self.service.review_repair(actor, role, int(p[2]), body.get("decision", ""), body.get("note", ""))
            else:
                raise ApiError(404, "接口不存在")
            self._send(200, out)
        except ApiError as exc:
            self._send(exc.status, {"error": exc.message})
        except (ValueError, TypeError, sqlite3.IntegrityError) as exc:
            self._send(400, {"error": str(exc)})
        except Exception as exc:
            self._send(500, {"error": str(exc)})
