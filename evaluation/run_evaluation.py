"""Run the frozen labels against checkpoint, final and controlled ablation."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "evaluation"))

from generate_cases import cases
from workflow_reviewer.model import FakeModelClient, collect_model_advice
from workflow_reviewer.reviewer import review


def _evaluate(mode: str, bundles: dict, labels: dict) -> dict:
    rows = []
    start = time.perf_counter()
    model_calls = 0
    for case_id, bundle in bundles.items():
        label = labels[case_id]
        options = {
            "checkpoint": mode == "checkpoint",
            "ablate_code": mode == "ablation",
        }
        report = review(bundle, **options)
        if label["model"] == "timeout":
            client = FakeModelClient(fail_with=TimeoutError("synthetic timeout"))
            advice = collect_model_advice(bundle, client, max_attempts=2)
            model_calls += client.calls
            report["model_advice"] = advice
            if report["status"] == "PASS":
                report["status"] = "INCOMPLETE"
                report["summary"]["unreviewed_scope"].append("model advisory")
        expected = label["expected"]
        rows.append({
            "case_id": case_id,
            "split": label["split"],
            "expected": expected,
            "actual": report["status"],
            "correct": expected == report["status"],
            "blockers": report["summary"]["blocker_count"],
            "finding_count": report["summary"]["finding_count"],
            "well_supported_blockers": sum(
                finding["severity"] == "blocker"
                and bool(finding.get("evidence"))
                and all(
                    all(key in evidence and evidence[key] not in (None, "") for key in ("artifact", "version", "location"))
                    for evidence in finding.get("evidence", [])
                )
                and bool(finding.get("correction"))
                for finding in report["findings"]
            ),
            "behavioral_counterexamples": sum(
                finding["severity"] == "blocker"
                and "counterexample" in finding
                and {"ticket", "expected_route", "actual_route"} <= finding["counterexample"].keys()
                for finding in report["findings"]
            ),
            "report": report,
        })
    elapsed = time.perf_counter() - start
    confusion = {
        "false_passes_on_defective": sum(
            row["expected"] == "CHANGES_REQUIRED" and row["actual"] == "PASS" for row in rows
        ),
        "false_blocks_on_valid": sum(
            row["expected"] == "PASS" and row["actual"] == "CHANGES_REQUIRED" for row in rows
        ),
        "incomplete_by_expected_type": {
            kind: sum(row["expected"] == kind and row["actual"] == "INCOMPLETE" for row in rows)
            for kind in ("PASS", "CHANGES_REQUIRED", "INCOMPLETE")
        },
        "unsupported_blocker_findings": sum(
            finding["severity"] == "blocker" and (
                not finding.get("evidence") or not finding.get("correction")
                or any(
                    not all(key in evidence and evidence[key] not in (None, "") for key in ("artifact", "version", "location"))
                    for evidence in finding.get("evidence", [])
                )
            )
            for row in rows for finding in row["report"]["findings"]
        ),
        "blocker_finding_count": sum(
            finding["severity"] == "blocker"
            for row in rows for finding in row["report"]["findings"]
        ),
        "supported_blocker_findings": sum(row["well_supported_blockers"] for row in rows),
        "behavioral_counterexamples": sum(row["behavioral_counterexamples"] for row in rows),
    }
    return {
        "mode": mode,
        "case_count": len(rows),
        "correct_count": sum(row["correct"] for row in rows),
        "accuracy": sum(row["correct"] for row in rows) / len(rows) if rows else 0,
        "counts_by_expected": {
            kind: sum(row["expected"] == kind for row in rows)
            for kind in ("PASS", "CHANGES_REQUIRED", "INCOMPLETE")
        },
        "counts_by_actual": {
            kind: sum(row["actual"] == kind for row in rows)
            for kind in ("PASS", "CHANGES_REQUIRED", "INCOMPLETE")
        },
        "counts_by_split": {
            split: {
                "case_count": sum(row["split"] == split for row in rows),
                "correct_count": sum(row["split"] == split and row["correct"] for row in rows),
                "accuracy": (
                    sum(row["split"] == split and row["correct"] for row in rows)
                    / sum(row["split"] == split for row in rows)
                    if any(row["split"] == split for row in rows) else 0
                ),
                "false_passes_on_defective": sum(
                    row["split"] == split and row["expected"] == "CHANGES_REQUIRED" and row["actual"] == "PASS"
                    for row in rows
                ),
                "false_blocks_on_valid": sum(
                    row["split"] == split and row["expected"] == "PASS" and row["actual"] == "CHANGES_REQUIRED"
                    for row in rows
                ),
            }
            for split in ("development", "held_out")
        },
        "confusion_and_quality": confusion,
        "runtime_seconds": round(elapsed, 6),
        "model_calls": model_calls,
        "rows": rows,
    }


def main() -> None:
    bundles = cases()
    labels = json.loads((ROOT / "evaluation" / "labels.json").read_text(encoding="utf-8"))
    if set(bundles) != set(labels):
        raise SystemExit("bundle and expected-label ids do not match")
    (ROOT / "evaluation" / "bundles.json").write_text(
        json.dumps(bundles, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (ROOT / "examples").mkdir(parents=True, exist_ok=True)
    (ROOT / "examples" / "valid_bundle.json").write_text(
        json.dumps(bundles["valid_baseline"], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    results = {
        mode: _evaluate(mode, bundles, labels)
        for mode in ("checkpoint", "final", "ablation")
    }
    report = {
        "evaluation": "synthetic, deterministic, CPU-only; expected labels are not passed to reviewer",
        "generation": {"seed": 2026, "generator": "evaluation/generate_cases.py"},
        "held_out_case_count": sum(item["split"] == "held_out" for item in labels.values()),
        "model_results": {
            "fake_client": {
                "cases_per_mode": sum(item["model"] != "none" for item in labels.values()),
                "calls_by_mode": {
                    mode: result["model_calls"] for mode, result in results.items()
                },
                "outcome": "bounded synthetic timeout; no semantic model performance measured",
            },
            "live_model": {"cases": 0, "calls": 0, "status": "not configured or measured"},
        },
        "results": results,
    }
    (ROOT / "reports").mkdir(parents=True, exist_ok=True)
    (ROOT / "reports" / "evaluation.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    metrics = {key: {field: value for field, value in result.items() if field != "rows"} for key, result in results.items()}
    (ROOT / "reports" / "metrics.json").write_text(
        json.dumps(metrics, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(metrics, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
