"""Freeze implementation, labels, generation settings and generated bundle hashes."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evaluation"))
from generate_cases import cases


def main() -> None:
    paths = [
        *(ROOT / "src" / "workflow_reviewer").glob("*.py"),
        *(ROOT / "schemas").glob("*.schema.json"),
        ROOT / "tests" / "test_reviewer.py",
        ROOT / "pyproject.toml",
        ROOT / "evaluation" / "generate_cases.py",
        ROOT / "evaluation" / "run_evaluation.py",
        ROOT / "evaluation" / "freeze_checkpoint.py",
        ROOT / "evaluation" / "labels.json",
        ROOT / "evaluation" / "generation_config.json",
    ]
    file_hashes = {
        path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(paths)
    }
    labels = json.loads((ROOT / "evaluation" / "labels.json").read_text(encoding="utf-8"))
    generated = json.dumps(cases(), sort_keys=True, separators=(",", ":")).encode("utf-8")
    manifest = {
        "checkpoint": "initial working requirements-versus-specification mode; graph/code left unreviewed",
        "frozen_before_final_evaluation": True,
        "case_count": len(labels),
        "development_case_count": sum(item["split"] == "development" for item in labels.values()),
        "held_out_case_ids": sorted(key for key, item in labels.items() if item["split"] == "held_out"),
        "expected_labels_not_in_reviewer_inputs": True,
        "generated_bundle_set_sha256": hashlib.sha256(generated).hexdigest(),
        "frozen_file_sha256": file_hashes,
    }
    destination = ROOT / "evaluation" / "frozen_checkpoint.json"
    destination.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(destination.relative_to(ROOT))
    print("held-out cases:", len(manifest["held_out_case_ids"]))
    print("frozen files:", len(file_hashes))


if __name__ == "__main__":
    main()
