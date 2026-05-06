from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _json_dumps(value: Any, *, pretty: bool = False) -> str:
    if pretty:
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=_json_default)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=_json_default)


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _allowed_tables(raw: str) -> set[str]:
    return {item.strip() for item in str(raw or "").split(",") if item.strip()}


def _diff_table_hashes(left: dict[str, Any], right: dict[str, Any], allowed: set[str]) -> list[dict[str, Any]]:
    diffs: list[dict[str, Any]] = []
    left_hashes = left.get("table_hashes") or {}
    right_hashes = right.get("table_hashes") or {}
    for table in sorted(set(left_hashes) | set(right_hashes)):
        if table in allowed:
            continue
        l = left_hashes.get(table)
        r = right_hashes.get(table)
        if l != r:
            diffs.append(
                {
                    "table": table,
                    "left": None if l is None else {"row_count": l.get("row_count"), "sha256": l.get("sha256")},
                    "right": None if r is None else {"row_count": r.get("row_count"), "sha256": r.get("sha256")},
                }
            )
    return diffs


def _diff_business_stats(left: dict[str, Any], right: dict[str, Any]) -> list[dict[str, Any]]:
    left_stats = left.get("business_stats") or {}
    right_stats = right.get("business_stats") or {}
    diffs: list[dict[str, Any]] = []
    for key in sorted(set(left_stats) | set(right_stats)):
        if left_stats.get(key) != right_stats.get(key):
            diffs.append({"section": key, "left": left_stats.get(key), "right": right_stats.get(key)})
    return diffs


def compare(args: argparse.Namespace) -> dict[str, Any]:
    left = _load(args.left)
    right = _load(args.right)
    allowed = _allowed_tables(args.allow_table_diff)
    table_diffs = _diff_table_hashes(left, right, allowed)
    business_diffs = _diff_business_stats(left, right)
    ok = not table_diffs and not business_diffs
    report = {
        "ok": ok,
        "mode": "compare_db_fingerprints",
        "left": str(args.left),
        "right": str(args.right),
        "left_label": left.get("label"),
        "right_label": right.get("label"),
        "allowed_table_diffs": sorted(allowed),
        "summary": {
            "table_diff_count": len(table_diffs),
            "business_diff_count": len(business_diffs),
        },
        "table_diffs": table_diffs,
        "business_diffs": business_diffs,
    }
    args.report_path.parent.mkdir(parents=True, exist_ok=True)
    args.report_path.write_text(_json_dumps(report, pretty=True), encoding="utf-8")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compare two Dingchang DB business fingerprint reports.")
    parser.add_argument("--left", type=Path, required=True)
    parser.add_argument("--right", type=Path, required=True)
    parser.add_argument("--allow-table-diff", default="")
    parser.add_argument("--report-path", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    args.left = args.left.resolve()
    args.right = args.right.resolve()
    args.report_path = args.report_path.resolve()
    report = compare(args)
    print(
        _json_dumps(
            {
                "ok": report["ok"],
                "report": str(args.report_path),
                "summary": report["summary"],
            },
            pretty=True,
        )
    )
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
