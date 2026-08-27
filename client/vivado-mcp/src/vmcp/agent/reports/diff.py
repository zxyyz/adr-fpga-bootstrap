"""Small semantic diffs over normalized report JSON."""

from __future__ import annotations

from typing import Any

from ...common.errors import BadRequest


def report_diff(
    before: dict[str, Any], after: dict[str, Any], kind: str
) -> dict[str, Any]:
    if kind == "utilization":
        return _utilization(before, after)
    if kind == "timing":
        return _timing(before, after)
    raise BadRequest("report_diff kind must be utilization or timing")


def _utilization(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    old = {row["resource"]: row for row in before.get("resources", [])}
    new = {row["resource"]: row for row in after.get("resources", [])}
    changes = []
    for resource in sorted(old.keys() | new.keys()):
        left, right = old.get(resource, {}), new.get(resource, {})
        old_used, new_used = left.get("used", 0), right.get("used", 0)
        changes.append(
            {
                "resource": resource,
                "before_used": old_used,
                "after_used": new_used,
                "delta_used": new_used - old_used,
                "before_pct": left.get("pct"),
                "after_pct": right.get("pct"),
                "delta_pct": _delta(left.get("pct"), right.get("pct")),
            }
        )
    return {"kind": "utilization", "changes": changes}


def _timing(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    metrics = (
        "wns_ns",
        "tns_ns",
        "setup_failing_endpoints",
        "whs_ns",
        "ths_ns",
        "hold_failing_endpoints",
        "wpws_ns",
        "tpws_ns",
    )
    old, new = before.get("summary", {}), after.get("summary", {})
    summary = {
        name: {
            "before": old.get(name),
            "after": new.get(name),
            "delta": _delta(old.get(name), new.get(name)),
        }
        for name in metrics
        if name in old or name in new
    }
    old_domains = {row["clock"]: row for row in before.get("domains", [])}
    new_domains = {row["clock"]: row for row in after.get("domains", [])}
    domains = []
    for clock in sorted(old_domains.keys() | new_domains.keys()):
        left, right = old_domains.get(clock, {}), new_domains.get(clock, {})
        domains.append(
            {
                "clock": clock,
                "wns_delta_ns": _delta(left.get("wns_ns"), right.get("wns_ns")),
                "tns_delta_ns": _delta(left.get("tns_ns"), right.get("tns_ns")),
                "failing_endpoints_delta": _delta(
                    left.get("setup_failing_endpoints"),
                    right.get("setup_failing_endpoints"),
                ),
            }
        )
    return {"kind": "timing", "summary": summary, "domains": domains}


def _delta(before: Any, after: Any) -> int | float | None:
    if not isinstance(before, (int, float)) or not isinstance(after, (int, float)):
        return None
    return round(after - before, 6)
