"""车辆召回业务包：差异计算、版本留档与接口处理。"""
from __future__ import annotations

import json
from datetime import datetime, timezone


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message); self.status, self.message = status, message


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def j(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
