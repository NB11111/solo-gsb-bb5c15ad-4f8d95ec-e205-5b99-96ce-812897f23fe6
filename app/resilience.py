"""单温区故障容灾评估。

基于同一输入快照,把每个运输段的每个可用温区分别模拟为不可用(单点故障),
复用完整装箱、锁温与热暴露规则重新规划;基准方案本身不被改动。每个故障
场景返回:是否仍可运输、相对基准的额外箱数、由已装载变为未装载的批次与
件数、首个冲突原因。场景按 新增未装载件数降序、额外箱数降序、段序号升序、
温区序升序 稳定排序。
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from .planner import plan


def _fingerprint(payload: dict[str, Any]) -> str:
    """输入快照指纹(规范化 JSON 的 SHA-256 前 12 位),用于把评估结果绑定到输入。"""
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def _loaded_units_by_batch(result: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """按批号汇总一次规划结果中的已装件数:{批号: {"name": 名称, "units": 件数}}。"""
    out: dict[int, dict[str, Any]] = {}
    for box in result["boxes"]:
        for it in box["items"]:
            entry = out.setdefault(it["batch_no"], {"name": it["name"], "units": 0})
            entry["units"] += it["units"]
    return out


def _newly_unloaded(
    baseline_loaded: dict[int, dict[str, Any]],
    scenario_loaded: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    """由已装载变为未装载的批次与件数(基准已装 > 场景已装的部分),按批号排序。"""
    rows: list[dict[str, Any]] = []
    for no in sorted(baseline_loaded):
        base_units = baseline_loaded[no]["units"]
        scen_units = scenario_loaded.get(no, {}).get("units", 0)
        if scen_units < base_units:
            rows.append(
                {
                    "batch_no": no,
                    "name": baseline_loaded[no]["name"],
                    "units": base_units - scen_units,
                    "baseline_loaded_units": base_units,
                    "scenario_loaded_units": scen_units,
                }
            )
    return rows


def _risk_level(newly_unloaded_units: int, extra_boxes: int) -> str:
    """风险等级:有批次由已装变为未装 = 高;仅箱数增加 = 中;其余 = 低。"""
    if newly_unloaded_units > 0:
        return "high"
    if extra_boxes > 0:
        return "medium"
    return "low"


def _scenario_entry(
    seg: dict[str, Any],
    zone_idx: int,
    zone: dict[str, Any],
    *,
    still_transportable: bool,
    boxes_used: int,
    extra_boxes: int,
    loaded_units: int,
    unloaded_units: int,
    newly: list[dict[str, Any]],
    reason: str | None,
) -> dict[str, Any]:
    newly_units = sum(n["units"] for n in newly)
    return {
        "segment_no": seg["segment_no"],
        "segment_name": seg["name"],
        "zone_index": zone_idx + 1,
        "zone_name": zone["name"],
        "zone": zone,
        "still_transportable": still_transportable,
        "boxes_used": boxes_used,
        "extra_boxes": extra_boxes,
        "loaded_units": loaded_units,
        "unloaded_units": unloaded_units,
        "newly_unloaded_units": newly_units,
        "newly_unloaded": newly,
        "first_conflict_reason": reason,
        "risk_level": _risk_level(newly_units, extra_boxes),
    }


def assess(payload: dict[str, Any]) -> dict[str, Any]:
    """对输入快照逐温区模拟故障并重新规划;输入不合法时抛 ValueError。"""
    # 基准方案:评估全程不改动其结果,场景差异均相对它计算
    baseline = plan(payload)
    bsum = baseline["summary"]
    base_loaded = _loaded_units_by_batch(baseline)
    segment_mode = bsum["segment_mode"]
    total_units = bsum["total_cargo_units"]

    scenarios: list[dict[str, Any]] = []
    for seg in baseline["segments"]:
        seg_idx = seg["segment_no"] - 1
        for zone_idx, zone in enumerate(seg["zones"]):
            scenario_payload = copy.deepcopy(payload)
            if segment_mode:
                zone_list = scenario_payload["segments"][seg_idx]["zones"]
            else:
                zone_list = scenario_payload["zones"]
            zone_list.pop(zone_idx)

            if not zone_list:
                # 该段温区全部失效:任何货物都无法通过此段,无需重新规划
                if segment_mode:
                    reason = (
                        f"温区「{zone['name']}」不可用后,运输段「{seg['name']}」"
                        f"(第 {seg['segment_no']} 段)不再有任何可用温区,"
                        f"全部货物均无法通过该段,全程温度无法保证"
                    )
                else:
                    reason = (
                        f"温区「{zone['name']}」不可用后,全程不再有任何可用温区,"
                        f"全部货物均无法装载"
                    )
                scenarios.append(
                    _scenario_entry(
                        seg,
                        zone_idx,
                        zone,
                        still_transportable=False,
                        boxes_used=0,
                        extra_boxes=0,
                        loaded_units=0,
                        unloaded_units=total_units,
                        newly=_newly_unloaded(base_loaded, {}),
                        reason=reason,
                    )
                )
                continue

            try:
                result = plan(scenario_payload)
            except ValueError as exc:
                # 兜底:场景输入无法规划时视为整体不可运输
                scenarios.append(
                    _scenario_entry(
                        seg,
                        zone_idx,
                        zone,
                        still_transportable=False,
                        boxes_used=0,
                        extra_boxes=0,
                        loaded_units=0,
                        unloaded_units=total_units,
                        newly=_newly_unloaded(base_loaded, {}),
                        reason=str(exc),
                    )
                )
                continue

            ssum = result["summary"]
            scenarios.append(
                _scenario_entry(
                    seg,
                    zone_idx,
                    zone,
                    still_transportable=ssum["all_transportable"],
                    boxes_used=ssum["boxes_used"],
                    extra_boxes=max(0, ssum["boxes_used"] - bsum["boxes_used"]),
                    loaded_units=ssum["loaded_units"],
                    unloaded_units=ssum["unloaded_units"],
                    newly=_newly_unloaded(
                        base_loaded, _loaded_units_by_batch(result)
                    ),
                    reason=(
                        result["unloaded"][0]["reason"] if result["unloaded"] else None
                    ),
                )
            )

    # 稳定排序:新增未装件数 ↓、额外箱数 ↓、段序号 ↑、温区序 ↑
    scenarios.sort(
        key=lambda s: (
            -s["newly_unloaded_units"],
            -s["extra_boxes"],
            s["segment_no"],
            s["zone_index"],
        )
    )

    return {
        "input_fingerprint": _fingerprint(payload),
        "baseline": {
            "boxes_used": bsum["boxes_used"],
            "loaded_units": bsum["loaded_units"],
            "unloaded_units": bsum["unloaded_units"],
            "total_cargo_units": bsum["total_cargo_units"],
            "all_transportable": bsum["all_transportable"],
            "transport_hours": bsum["transport_hours"],
            "segment_mode": bsum["segment_mode"],
        },
        "scenarios": scenarios,
    }
