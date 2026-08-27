"""Version-tolerant parsers for the stable text tables in Vivado reports."""

from __future__ import annotations

import re
from typing import Any

_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)"


def _number(value: str) -> int | float | None:
    text = value.strip().replace(",", "")
    if text in {"", "-", "--", "---", "NA", "N/A", "n/a"}:
        return None
    try:
        result = float(text.rstrip("%"))
    except ValueError:
        return None
    return int(result) if result.is_integer() and "." not in text else result


def metadata(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    aliases = {
        "Tool Version": "tool_version",
        "Design": "design",
        "Device": "device",
        "Design State": "design_state",
        "Command": "command",
    }
    for line in text.splitlines()[:40]:
        match = re.match(r"^\|\s*([^:|]+?)\s*:\s*(.*?)\s*\|?\s*$", line)
        if match and match.group(1).strip() in aliases:
            result[aliases[match.group(1).strip()]] = match.group(2).strip()
    return result


def _pipe_rows(text: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or not stripped.endswith("|"):
            continue
        cells = [cell.strip() for cell in stripped[1:-1].split("|")]
        if cells and not all(set(cell) <= {"-", "+", "="} for cell in cells):
            rows.append(cells)
    return rows


_RESOURCE_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("LUT", ("Slice LUTs", "CLB LUTs")),
    ("LUTRAM", ("LUT as Memory",)),
    ("FF", ("Slice Registers", "CLB Registers", "Register as Flip Flop")),
    ("BRAM", ("Block RAM Tile", "Block RAM Tiles")),
    ("URAM", ("URAM", "UltraRAM")),
    ("DSP", ("DSPs", "DSP Blocks")),
    ("IO", ("Bonded IOB", "Bonded IOBs")),
    ("BUFG", ("BUFGCTRL", "BUFGCE", "Global Clock Buffer")),
)


def parse_utilization(text: str, *, hierarchical: bool = False) -> dict[str, Any]:
    rows = _pipe_rows(text)
    resources = []
    for canonical, aliases in _RESOURCE_ALIASES:
        match = next(
            (
                row
                for row in rows
                if row and row[0].strip().rstrip("*").strip() in aliases
            ),
            None,
        )
        if match is None or len(match) < 3:
            continue
        used = _number(match[1])
        available = _number(match[-2])
        pct = _number(match[-1])
        if not used:
            continue
        resources.append(
            {
                "resource": canonical,
                "used": used,
                "available": available,
                "pct": pct,
            }
        )

    result: dict[str, Any] = {
        "metadata": metadata(text),
        "resources": resources,
        "summary": {
            item["resource"]: {
                "used": item["used"],
                "available": item["available"],
                "pct": item["pct"],
            }
            for item in resources
        },
    }
    if hierarchical:
        result["hierarchy"] = _parse_hierarchy(rows)
    return result


def _parse_hierarchy(rows: list[list[str]]) -> list[dict[str, Any]]:
    header_index = next(
        (
            index
            for index, row in enumerate(rows)
            if row and row[0] in {"Instance", "Name"} and len(row) >= 4
        ),
        None,
    )
    if header_index is None:
        return []
    headers = [
        re.sub(r"\W+", "_", value.lower()).strip("_") for value in rows[header_index]
    ]
    result = []
    for row in rows[header_index + 1 :]:
        if len(row) != len(headers) or row[0] in {"Instance", "Name"}:
            continue
        values: dict[str, Any] = {headers[0]: row[0]}
        nonzero = False
        for name, value in zip(headers[1:], row[1:]):
            parsed = _number(value)
            values[name] = parsed if parsed is not None else value
            nonzero = nonzero or bool(parsed)
        if nonzero:
            result.append(values)
    return result[:500]


_SUMMARY_FIELDS = (
    "wns_ns",
    "tns_ns",
    "setup_failing_endpoints",
    "setup_total_endpoints",
    "whs_ns",
    "ths_ns",
    "hold_failing_endpoints",
    "hold_total_endpoints",
    "wpws_ns",
    "tpws_ns",
    "pulse_width_failing_endpoints",
    "pulse_width_total_endpoints",
)


def parse_timing(text: str, *, max_paths: int = 5) -> dict[str, Any]:
    lines = text.splitlines()
    summary = _timing_summary(lines)
    clocks = _timing_clocks(lines, summary)
    domains = _timing_domains(lines)
    paths = _timing_paths(lines)[: max(0, max_paths)]
    return {
        "metadata": metadata(text),
        "summary": summary,
        "clocks": clocks,
        "domains": domains,
        "paths": [
            {key: value for key, value in path.items() if key != "raw"}
            for path in paths
        ],
    }


def parse_timing_paths(text: str) -> list[dict[str, Any]]:
    return _timing_paths(text.splitlines())


def _timing_summary(lines: list[str]) -> dict[str, Any]:
    start = next(
        (i for i, line in enumerate(lines) if "Design Timing Summary" in line), 0
    )
    for line in lines[start : start + 30]:
        values = re.findall(_NUMBER, line)
        if len(values) == len(_SUMMARY_FIELDS):
            return {
                name: _number(value)
                for name, value in zip(_SUMMARY_FIELDS, values, strict=True)
            }
    return {}


def _timing_clocks(lines: list[str], summary: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    pattern = re.compile(rf"^\s*(\S+)\s+\{{[^}}]+\}}\s+({_NUMBER})\s+({_NUMBER})\s*$")
    for line in lines:
        match = pattern.match(line)
        if not match:
            continue
        period = float(match.group(2))
        wns = summary.get("wns_ns")
        achieved = None
        if isinstance(wns, (int, float)) and period - wns > 0:
            achieved = round(1000.0 / (period - wns), 3)
        result.append(
            {
                "clock": match.group(1),
                "period_ns": period,
                "target_mhz": float(match.group(3)),
                "achieved_mhz": achieved,
            }
        )
    return result


def _timing_domains(lines: list[str]) -> list[dict[str, Any]]:
    start = next(
        (i for i, line in enumerate(lines) if "Intra Clock Table" in line), None
    )
    if start is None:
        return []
    result = []
    for line in lines[start + 1 :]:
        if "Inter Clock Table" in line:
            break
        parts = line.split()
        if len(parts) != 13 or _number(parts[1]) is None:
            continue
        values = [_number(value) for value in parts[1:]]
        row: dict[str, Any] = {"clock": parts[0]}
        row.update(dict(zip(_SUMMARY_FIELDS, values, strict=True)))
        result.append(row)
    return result


def _timing_paths(
    lines: list[str],
) -> list[dict[str, Any]]:  # pylint: disable=too-many-locals
    starts = [i for i, line in enumerate(lines) if re.match(r"^Slack \(", line)]
    result = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        block = lines[start:end]
        previous = "\n".join(lines[max(0, start - 12) : start])
        delay_type = "min" if "Min Delay Paths" in previous else "max"

        def field(name: str, block_lines: list[str] = block) -> str | None:
            prefix = f"{name}:"
            for item in block_lines:
                if item.strip().startswith(prefix):
                    return item.split(":", 1)[1].strip()
            return None

        slack_match = re.search(rf"Slack \(([^)]+)\)\s*:\s*({_NUMBER})ns", block[0])
        delay_match = re.search(
            rf"({_NUMBER})ns\s+\(logic\s+({_NUMBER})ns\s+\(({_NUMBER})%\)\s+"
            rf"route\s+({_NUMBER})ns\s+\(({_NUMBER})%\)\)",
            field("Data Path Delay") or "",
        )
        logic_match = re.match(r"(\d+)", field("Logic Levels") or "")
        skew_match = re.match(rf"({_NUMBER})ns", field("Clock Path Skew") or "")
        if slack_match is None:
            continue
        result.append(
            {
                "delay_type": delay_type,
                "status": slack_match.group(1).lower(),
                "slack_ns": float(slack_match.group(2)),
                "source": field("Source"),
                "destination": field("Destination"),
                "path_group": field("Path Group"),
                "path_type": field("Path Type"),
                "requirement_ns": _leading_number(field("Requirement")),
                "data_path_delay_ns": (
                    float(delay_match.group(1)) if delay_match else None
                ),
                "logic_delay_pct": float(delay_match.group(3)) if delay_match else None,
                "net_delay_pct": float(delay_match.group(5)) if delay_match else None,
                "logic_levels": int(logic_match.group(1)) if logic_match else None,
                "skew_ns": float(skew_match.group(1)) if skew_match else None,
                "raw": "\n".join(block).strip()[:12000],
            }
        )
    return result


def _leading_number(value: str | None) -> float | None:
    match = re.match(rf"({_NUMBER})", value or "")
    return float(match.group(1)) if match else None


_SEVERITIES = {"Advisory", "Info", "Warning", "Critical Warning", "Error", "Fatal"}


def parse_rule_report(text: str) -> dict[str, Any]:
    rows = _pipe_rows(text)
    detail_examples = _rule_examples(text.splitlines())
    rules = []
    for row in rows:
        if len(row) < 4 or row[1] not in _SEVERITIES:
            continue
        count = _number(row[-1])
        if not isinstance(count, (int, float)):
            continue
        rule = row[0]
        rules.append(
            {
                "rule": rule,
                "severity": row[1].lower().replace(" ", "_"),
                "count": int(count),
                "description": row[2],
                "one_example": detail_examples.get(rule),
            }
        )
    return {
        "metadata": metadata(text),
        "rules": rules,
        "count": sum(item["count"] for item in rules),
    }


def _rule_examples(lines: list[str]) -> dict[str, str]:
    result = {}
    header = re.compile(r"^([A-Za-z][A-Za-z0-9_-]+)#\d+\s+(.+)$")
    for index, line in enumerate(lines):
        match = header.match(line.strip())
        if not match or match.group(1) in result:
            continue
        example = []
        for detail in lines[index + 1 : index + 6]:
            stripped = detail.strip()
            if stripped and not stripped.startswith("Related violations:"):
                example.append(stripped)
        result[match.group(1)] = " ".join(example)[:1000]
    return result


def parse_power(text: str) -> dict[str, Any]:
    rows = _pipe_rows(text)
    summary_names = {
        "Total On-Chip Power (W)": "total_on_chip_w",
        "Dynamic (W)": "dynamic_w",
        "Device Static (W)": "device_static_w",
        "Junction Temperature (C)": "junction_temp_c",
        "Max Ambient (C)": "max_ambient_c",
        "Confidence Level": "confidence",
    }
    summary: dict[str, Any] = {}
    components = []
    for row in rows:
        if len(row) == 2 and row[0] in summary_names:
            summary[summary_names[row[0]]] = _number(row[1]) or row[1]
        elif len(row) == 5 and _number(row[1]) is not None and row[0] != "On-Chip":
            components.append(
                {
                    "component": row[0].strip(),
                    "power_w": _number(row[1]),
                    "used": _number(row[2]),
                    "available": _number(row[3]),
                    "utilization_pct": _number(row[4]),
                }
            )
    return {"metadata": metadata(text), "summary": summary, "components": components}
