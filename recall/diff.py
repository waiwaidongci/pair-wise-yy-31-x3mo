"""召回范围匹配与差异计算：判断车辆是否在范围内，比较两版范围的新增/移出车辆。"""
from __future__ import annotations

import sqlite3

from . import ApiError

SCOPE_KEYS = ("models", "model_years", "vin_prefixes", "countries")


def validate_scope(scope: dict) -> None:
    for key in SCOPE_KEYS:
        if not scope.get(key):
            raise ApiError(400, f"召回范围缺少 {key}")


def in_scope(vehicle: sqlite3.Row, scope: dict) -> bool:
    return (vehicle["model"] in scope.get("models", [])
            and int(vehicle["model_year"]) in scope.get("model_years", [])
            and any(vehicle["vin"].startswith(prefix.upper()) for prefix in scope.get("vin_prefixes", []))
            and (vehicle["country"] in scope.get("countries", []) or vehicle["origin_country"] in scope.get("countries", [])))


def field_changes(old_scope: dict, new_scope: dict) -> dict:
    changes = {}
    for key in SCOPE_KEYS:
        old, new = {str(v) for v in old_scope.get(key, [])}, {str(v) for v in new_scope.get(key, [])}
        if old != new:
            changes[key] = {"added": sorted(new - old), "removed": sorted(old - new)}
    return changes


def vehicle_brief(vehicle: sqlite3.Row) -> dict:
    return {"id": vehicle["id"], "vin": vehicle["vin"], "model": vehicle["model"],
            "model_year": vehicle["model_year"], "country": vehicle["country"]}


def diff_vehicles(old_scope: dict, new_scope: dict, vehicles: list[sqlite3.Row]) -> dict:
    """比较新旧两版范围，返回新增车辆、移出车辆和范围字段变化。"""
    added = [vehicle_brief(v) for v in vehicles if in_scope(v, new_scope) and not in_scope(v, old_scope)]
    removed = [vehicle_brief(v) for v in vehicles if in_scope(v, old_scope) and not in_scope(v, new_scope)]
    return {"added": added, "removed": removed, "fields": field_changes(old_scope, new_scope)}
