"""Command-line entry point for offline and fake-model review."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .model import FakeModelClient, collect_model_advice
from .reviewer import MAX_BUNDLE_BYTES, review


def _read_bundle(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        size = path.stat().st_size
        if size > MAX_BUNDLE_BYTES:
            return None, f"input exceeds {MAX_BUNDLE_BYTES} byte limit; it was not truncated"
        raw = path.read_bytes()
        if len(raw) > MAX_BUNDLE_BYTES:
            return None, f"input exceeds {MAX_BUNDLE_BYTES} byte limit; it was not truncated"
        value = json.loads(raw)
        if not isinstance(value, dict):
            return None, "bundle root must be a JSON object"
        return value, None
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return None, f"cannot read input bundle: {exc}"


def _incomplete_input(error: str) -> dict[str, Any]:
    return {
        "status": "INCOMPLETE",
        "summary": {
            "finding_count": 1,
            "blocker_count": 0,
            "unreviewed_scope": ["input bundle"],
            "error": error,
        },
        "findings": [{
            "severity": "incomplete",
            "title": "Input bundle could not be reviewed",
            "rule_ref": None,
            "evidence": [],
            "correction": "Provide a complete UTF-8 JSON bundle within the documented input limit.",
        }],
        "coverage": [],
        "check_results": [{"check": "input", "status": "incomplete"}],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Review a structured routing bundle.")
    parser.add_argument("bundle", type=Path, help="path to a UTF-8 JSON review bundle")
    parser.add_argument("--model", choices=("none", "fake"), default="none")
    parser.add_argument("--ablate-code", action="store_true", help="controlled ablation: skip code equivalence")
    parser.add_argument("--checkpoint", action="store_true", help="run the frozen initial requirements/spec checkpoint")
    parser.add_argument("--output", type=Path, help="write the JSON report here (default: stdout)")
    args = parser.parse_args(argv)
    bundle, error = _read_bundle(args.bundle)
    if error:
        report = _incomplete_input(error)
    else:
        report = review(bundle, ablate_code=args.ablate_code, checkpoint=args.checkpoint)
        if args.model == "fake":
            advice = collect_model_advice(bundle, FakeModelClient())
            report["model_advice"] = advice
            if advice["status"] != "valid_advisory":
                report["summary"]["unreviewed_scope"] = sorted(set(
                    report["summary"]["unreviewed_scope"] + ["model advisory"]
                ))
                if report["status"] == "PASS":
                    report["status"] = "INCOMPLETE"
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    try:
        if args.output:
            args.output.write_text(text, encoding="utf-8")
        else:
            sys.stdout.write(text)
    except OSError as exc:
        print(f"cannot write report: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
