"""召回范围差异计算：比较新旧范围，算出字段与车辆两个维度的变化。"""
from __future__ import annotations

SCOPE_KEYS = ("models", "model_years", "vin_prefixes", "countries")


def validate_scope(scope: dict) -> None:
    """范围必须包含车型、年款、VIN 前缀和国家四类条件，否则抛 ValueError。"""
    for key in SCOPE_KEYS:
        if not scope.get(key):
            raise ValueError(f"召回范围缺少 {key}")


def in_scope(vehicle, scope: dict) -> bool:
    """车辆是否落在范围内：车型、年款、VIN 前缀、所在国或原产国同时匹配。"""
    return (vehicle["model"] in scope.get("models", [])
            and int(vehicle["model_year"]) in scope.get("model_years", [])
            and any(vehicle["vin"].startswith(prefix.upper()) for prefix in scope.get("vin_prefixes", []))
            and (vehicle["country"] in scope.get("countries", []) or vehicle["origin_country"] in scope.get("countries", [])))


def field_diff(old_scope: dict, new_scope: dict) -> dict:
    """四类范围条件各自的新增项和移除项。"""
    return {key: {"added": sorted(set(new_scope.get(key, [])) - set(old_scope.get(key, [])), key=str),
                  "removed": sorted(set(old_scope.get(key, [])) - set(new_scope.get(key, [])), key=str)}
            for key in SCOPE_KEYS}


def vehicle_diff(vehicles, old_scope: dict, new_scope: dict) -> tuple[list, list]:
    """返回 (新增车辆, 移出车辆)：命中新范围但未命中旧范围为新增，反之为移出。"""
    added = [v for v in vehicles if in_scope(v, new_scope) and not in_scope(v, old_scope)]
    removed = [v for v in vehicles if in_scope(v, old_scope) and not in_scope(v, new_scope)]
    return added, removed


def scope_diff(old_scope: dict, new_scope: dict, vehicles) -> dict:
    """完整差异：字段变化加上按 VIN 列出的新增/移出车辆。"""
    added, removed = vehicle_diff(vehicles, old_scope, new_scope)
    return {"fields": field_diff(old_scope, new_scope),
            "added_vins": sorted(v["vin"] for v in added),
            "removed_vins": sorted(v["vin"] for v in removed)}
