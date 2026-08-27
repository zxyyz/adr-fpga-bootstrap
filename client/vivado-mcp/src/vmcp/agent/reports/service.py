"""Resolve report sources, cache normalized JSON and generate missing reports."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...common.errors import BadRequest, NotFound
from ...common.jsonio import read_json, write_json
from ...common.models import ToolKind
from ...common.tcl import TCL_DECODE_PROC, b64encode_utf8
from .. import paths
from ..jobs.supervisor import JobSupervisor
from ..registry import SessionRegistry
from ..workspace import WorkspaceManager
from .diff import report_diff
from .parsers import (
    metadata,
    parse_power,
    parse_rule_report,
    parse_timing,
    parse_timing_paths,
    parse_utilization,
)

_JOB_ID = re.compile(r"j_[0-9a-f]{8}\Z")
_KINDS = frozenset(
    {
        "utilization",
        "timing",
        "timing_paths",
        "clocks",
        "drc",
        "methodology",
        "cdc",
        "power",
    }
)


@dataclass(frozen=True, slots=True)
class _Source:
    display: str
    dcp: Path
    reports: list[Path]
    label: str
    settings_sh: str


class ReportService:
    """One serialized service; Vivado report sessions are intentionally reused."""

    def __init__(
        self,
        jobs: JobSupervisor,
        registry: SessionRegistry,
        workspaces: WorkspaceManager,
    ) -> None:
        self.jobs = jobs
        self.registry = registry
        self.workspaces = workspaces
        self._lock = asyncio.Lock()
        self._session_id: str | None = None
        self._session_key: tuple[str, str] | None = None
        self._open_hash: str | None = None

    async def get(self, params: dict[str, Any]) -> dict[str, Any]:
        kind = str(params.get("kind", ""))
        if kind not in _KINDS:
            raise BadRequest(f"unknown report kind {kind!r}")
        args = self._normalize_args(kind, params.get("args") or {})
        async with self._lock:
            source = self._resolve_source(params)
            design_hash = _sha256(source.dcp)
            key = self._cache_key(design_hash, kind, args)
            cache_file = paths.REPORT_CACHE / f"{key}.json"
            if cache_file.is_file():
                result = read_json(cache_file)
                return self._decorate(result, source.display, design_hash, key, True)

            report = self._existing_report(source.reports, kind, args)
            if report is None:
                report = await self._generate(source, design_hash, key, kind, args)
            text = report.read_text(encoding="utf-8", errors="replace")
            result = self._parse(kind, text, args)
            result.update(
                {
                    "schema_version": 1,
                    "report_type": kind,
                    "parser": "vivado-text-v2",
                }
            )
            write_json(cache_file, result)
            return self._decorate(result, source.display, design_hash, key, False)

    async def compare(self, params: dict[str, Any]) -> dict[str, Any]:
        kind = str(params.get("kind", ""))
        if kind not in {"utilization", "timing"}:
            raise BadRequest("report_diff kind must be utilization or timing")
        base = {
            key: value
            for key, value in params.items()
            if key not in {"a", "b", "kind", "args"}
        }
        before = await self.get(
            {**base, "source": params.get("a"), "kind": kind, "args": {}}
        )
        after = await self.get(
            {**base, "source": params.get("b"), "kind": kind, "args": {}}
        )
        result = report_diff(before, after, kind)
        result.update(
            {
                "a": params.get("a"),
                "b": params.get("b"),
                "a_design_hash": before["design_hash"],
                "b_design_hash": after["design_hash"],
            }
        )
        return result

    def _resolve_source(self, params: dict[str, Any]) -> _Source:
        value = str(params.get("source") or "")
        if not value:
            raise BadRequest("report source is required")
        if _JOB_ID.fullmatch(value):
            data = self.jobs.report_inputs(value)
            spec = data["spec"]
            artifacts = [Path(item["path"]) for item in data["artifacts"]]
            dcp = _select_dcp(artifacts)
            if dcp is None:
                raise NotFound(f"job {value} has no DCP artifact")
            reports = [item for item in artifacts if item.suffix.lower() == ".rpt"]
            report_dir = spec.get("report_dir")
            if report_dir:
                reports.extend(Path(report_dir).glob("*.rpt"))
            return _Source(
                display=value,
                dcp=dcp,
                reports=_unique_existing(reports),
                label=str(spec["label"]),
                settings_sh=str(spec["settings_sh"]),
            )

        workspace = params.get("workspace")
        if not workspace:
            raise BadRequest("a workspace is required when report source is a DCP path")
        mapper = self.workspaces.mapper(
            str(workspace), str(params.get("build", "build"))
        )
        dcp = mapper.to_remote(value)
        if dcp.suffix.lower() != ".dcp" or not dcp.is_file():
            raise NotFound(f"report source is not an existing DCP: {value!r}")
        return _Source(
            display=value,
            dcp=dcp,
            reports=[],
            label=str(params.get("label") or "vivado"),
            settings_sh=str(params.get("settings_sh") or ""),
        )

    @staticmethod
    def _normalize_args(kind: str, raw: dict[str, Any]) -> dict[str, Any]:
        args: dict[str, Any] = {}
        if kind == "utilization":
            args["hierarchical"] = bool(raw.get("hierarchical", False))
            cells = raw.get("cells") or []
            if not isinstance(cells, list) or any(
                not isinstance(x, str) for x in cells
            ):
                raise BadRequest("cells must be a list of strings")
            args["cells"] = cells[:100]
        elif kind in {"timing", "timing_paths"}:
            count_name = "max_paths" if kind == "timing" else "nworst"
            maximum = 20 if kind == "timing" else 100
            args[count_name] = max(1, min(int(raw.get(count_name, 5)), maximum))
            if kind == "timing_paths":
                delay_type = str(raw.get("delay_type", "max"))
                if delay_type not in {"max", "min"}:
                    raise BadRequest("delay_type must be max or min")
                args["delay_type"] = delay_type
                detail = str(raw.get("detail", "summary"))
                if detail not in {"summary", "full"}:
                    raise BadRequest("detail must be summary or full")
                if detail == "full" and args["nworst"] > 1:
                    raise BadRequest(
                        "detail=full supports nworst=1 to bound the response"
                    )
                args["detail"] = detail
                for name in ("from_endpoint", "to_endpoint", "through"):
                    value = raw.get(name)
                    if value is not None:
                        args[name] = str(value)
        return args

    @staticmethod
    def _cache_key(design_hash: str, kind: str, args: dict[str, Any]) -> str:
        payload = json.dumps(
            {"schema": 2, "design": design_hash, "kind": kind, "args": args},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _existing_report(
        reports: list[Path], kind: str, args: dict[str, Any]
    ) -> Path | None:
        names = [(path, path.name.lower()) for path in reports]
        if kind == "utilization":
            hierarchical = args.get("hierarchical")
            cells = args.get("cells")
            if cells:
                return None
            if hierarchical:
                return next(
                    (
                        path
                        for path, name in names
                        if "utilization_hierarchical" in name
                    ),
                    None,
                )
            candidates = [
                path
                for path, name in names
                if "utilization" in name and "hierarchical" not in name
            ]
            return _best_named(candidates, ("utilization.rpt", "placed", "synth"))
        if kind in {"timing", "clocks"}:
            candidates = [path for path, name in names if "timing_summary" in name]
            return _best_named(candidates, ("timing_summary.rpt", "routed", "synth"))
        if kind == "timing_paths":
            custom = any(
                args.get(name) for name in ("from_endpoint", "to_endpoint", "through")
            )
            if custom:
                return None
            candidates = [
                path
                for path, name in names
                if "timing_summary" in name or "timing_paths" in name
            ]
            return _best_named(candidates, ("timing_summary.rpt", "routed"))
        candidates = [
            path
            for path, name in names
            if kind in name and not (kind == "drc" and "methodology" in name)
        ]
        return _best_named(candidates, (f"{kind}.rpt", "routed", "placed", "synth"))

    async def _generate(
        self,
        source: _Source,
        design_hash: str,
        key: str,
        kind: str,
        args: dict[str, Any],
    ) -> Path:
        report = paths.REPORT_CACHE / f"{key}.rpt"
        session = await self._report_session(source)
        lines = list(TCL_DECODE_PROC)
        if self._open_hash != design_hash:
            lines += [
                "catch {close_design}",
                f"set vmcp_dcp [vmcp_decode {{{b64encode_utf8(str(source.dcp))}}}]",
                "open_checkpoint $vmcp_dcp",
            ]
        lines += self._report_command(kind, args, report)
        result = await session.eval("\n".join(lines), timeout=600.0)
        if result.rc != 0 or not report.is_file():
            detail = result.result or result.errorinfo or result.log[-2000:]
            raise BadRequest(f"Vivado could not generate {kind} report: {detail}")
        self._open_hash = design_hash
        return report

    async def _report_session(self, source: _Source):
        key = (source.label, source.settings_sh)
        if self._session_id is not None and self._session_key == key:
            try:
                return self.registry.get(self._session_id)
            except NotFound:
                self._session_id = None
                self._open_hash = None
        if self._session_id is not None:
            try:
                await self.registry.close(self._session_id)
            except NotFound:
                pass
        info = await self.registry.open(
            kind=str(ToolKind.VIVADO),
            label=source.label,
            settings_sh=source.settings_sh,
            cwd=str(paths.HOME),
            boot_timeout=180.0,
        )
        self._session_id = info.session_id
        self._session_key = key
        self._open_hash = None
        return self.registry.get(info.session_id)

    @staticmethod
    def _report_command(kind: str, args: dict[str, Any], report: Path) -> list[str]:
        lines = [f"set vmcp_report [vmcp_decode {{{b64encode_utf8(str(report))}}}]"]
        command = {
            "timing": "report_timing_summary",
            "clocks": "report_timing_summary",
            "drc": "report_drc",
            "methodology": "report_methodology",
            "cdc": "report_cdc",
            "power": "report_power",
        }.get(kind)
        if kind == "utilization":
            parts = ["report_utilization"]
            if args.get("hierarchical"):
                parts.append("-hierarchical")
            cells = args.get("cells") or []
            if cells:
                encoded = b64encode_utf8(" ".join(cells))
                lines += [
                    f"set vmcp_cells_pattern [vmcp_decode {{{encoded}}}]",
                    "set vmcp_cells [get_cells -quiet -hierarchical $vmcp_cells_pattern]",
                    'if {![llength $vmcp_cells]} {error "cells selector matched nothing"}',
                ]
                parts += ["-cells", "$vmcp_cells"]
            command = " ".join(parts)
        elif kind == "timing_paths":
            parts = [
                "report_timing",
                "-nworst",
                str(args["nworst"]),
                "-delay_type",
                str(args["delay_type"]),
            ]
            for option, name in (
                ("-from", "from_endpoint"),
                ("-to", "to_endpoint"),
                ("-through", "through"),
            ):
                if name in args:
                    variable = f"vmcp_{name}"
                    lines += [
                        f"set {variable}_pattern [vmcp_decode {{{b64encode_utf8(args[name])}}}]",
                        f"set {variable} [concat [get_pins -quiet -hierarchical ${variable}_pattern] [get_ports -quiet ${variable}_pattern] [get_cells -quiet -hierarchical ${variable}_pattern]]",
                        f'if {{![llength ${variable}]}} {{error "{name} matched nothing"}}',
                    ]
                    parts += [option, f"${variable}"]
            command = " ".join(parts)
        assert command is not None
        lines.append(f"{command} -file $vmcp_report")
        return lines

    @staticmethod
    def _parse(kind: str, text: str, args: dict[str, Any]) -> dict[str, Any]:
        if kind == "utilization":
            return parse_utilization(text, hierarchical=args.get("hierarchical", False))
        if kind == "timing":
            return parse_timing(text, max_paths=args["max_paths"])
        if kind == "clocks":
            timing = parse_timing(text, max_paths=0)
            return {
                "metadata": timing["metadata"],
                "clocks": timing["clocks"],
                "domains": timing["domains"],
            }
        if kind == "timing_paths":
            paths_result = parse_timing_paths(text)
            paths_result = [
                item
                for item in paths_result
                if item["delay_type"] == args["delay_type"]
            ]
            paths_result = paths_result[: args["nworst"]]
            if args["detail"] != "full":
                paths_result = [
                    {key: value for key, value in item.items() if key != "raw"}
                    for item in paths_result
                ]
            return {"metadata": metadata(text), "paths": paths_result}
        if kind in {"drc", "methodology", "cdc"}:
            return parse_rule_report(text)
        if kind == "power":
            return parse_power(text)
        raise AssertionError(kind)

    @staticmethod
    def _decorate(
        result: dict[str, Any],
        source: str,
        design_hash: str,
        key: str,
        hit: bool,
    ) -> dict[str, Any]:
        copied = dict(result)
        copied.update(
            {
                "source": source,
                "design_hash": design_hash,
                "cache": {"hit": hit, "key": key},
            }
        )
        return copied


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _select_dcp(paths_list: list[Path]) -> Path | None:
    candidates = [
        path for path in paths_list if path.suffix.lower() == ".dcp" and path.is_file()
    ]
    if not candidates:
        return None

    def score(path: Path) -> tuple[int, float]:
        name = path.name.lower()
        quality = (
            5
            if "rout" in name
            else (
                4
                if "plac" in name
                else 3 if "opt" in name else 2 if "synth" in name else 1
            )
        )
        return quality, path.stat().st_mtime

    return max(candidates, key=score)


def _unique_existing(items: list[Path]) -> list[Path]:
    return list(dict.fromkeys(path for path in items if path.is_file()))


def _best_named(candidates: list[Path], preferences: tuple[str, ...]) -> Path | None:
    if not candidates:
        return None

    def score(path: Path) -> tuple[int, float]:
        name = path.name.lower()
        rank = max(
            (
                len(preferences) - index
                for index, value in enumerate(preferences)
                if value in name
            ),
            default=0,
        )
        return rank, path.stat().st_mtime

    return max(candidates, key=score)
