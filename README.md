# AI-assisted workflow reviewer

This is a CPU-only Python CLI for evidence-backed comparison of anchored policy requirements, a structured specification, a versioned decision graph, and a linked Python route function. The deterministic reviewer decides `PASS`, `CHANGES_REQUIRED`, or `INCOMPLETE`; an optional model-client interface is advisory only.

## Recruiter report: answer and result

**What it answers:** Does a workflow implementation match the approved policy requirements across its specification, decision graph, and linked Python route?

**What it produces:** An evidence-backed JSON report containing `PASS`, `CHANGES_REQUIRED`, or `INCOMPLETE`, plus source-rule citations, findings, coverage, check results, and reproducible bundle hashes. It does not execute supplied Python.

| Frozen evaluation result | Result |
|---|---:|
| Overall accuracy | **21/21 cases (100%)** |
| Valid cases | **9/9 correctly classified as PASS** |
| Defective cases | **9/9 correctly classified as CHANGES_REQUIRED** |
| Incomplete/unsupported cases | **3/3 correctly classified as INCOMPLETE** |
| Held-out cases | **4/4 (100%)** |
| False blocks on valid cases | **0** |
| False passes on defective cases | **0** |
| Unsupported blocker findings | **0** |

The 21-case frozen suite is the project’s reproducible result. Seventeen cases are development cases and four are held out. It is deterministic, CPU-only, and uses synthetic data; no live LLM or private data was used. The fake model client was used only to test bounded failure and response-handling paths.

- **Machine-readable report:** [metrics.json](./reports/metrics.json)
- **Detailed evaluation output:** [evaluation.json](./reports/evaluation.json)
- **Technical findings and limitations:** [engineering_note.md](./engineering_note.md)
- **CLI and implementation:** [cli.py](./src/workflow_reviewer/cli.py), [reviewer.py](./src/workflow_reviewer/reviewer.py)
- **Tests:** [test_reviewer.py](./tests/test_reviewer.py)

Run the complete reproducible check:

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v; if ($?) { python evaluation\run_evaluation.py }
```

## Run it

Requires Python 3.11–3.13. The application and tests use the standard library only; there are no model, GPU, network, or runtime-package requirements.

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v; if ($?) { python evaluation\run_evaluation.py }
```

That single CPU-only command runs the offline tests and frozen evaluation.

For the CLI from a source checkout, set the module path first:

```powershell
$env:PYTHONPATH = "src"
python -m workflow_reviewer.cli examples\valid_bundle.json --model fake
```

The evaluation command regenerates `evaluation\bundles.json` deterministically from the generator and writes `reports\evaluation.json` and `reports\metrics.json`. Expected labels are in the separate `evaluation\labels.json`; they are not added to bundles or model prompts. The same 21 cases are run in checkpoint, final, and ablation modes. Seventeen development and four held-out cases are identified in the frozen labels and checkpoint manifest. `--checkpoint` omits graph and code semantic checks; `--ablate-code` skips only code-behavior equivalence and reports that omission.

`evaluation\freeze_checkpoint.py` records SHA-256 hashes for the implementation, tests, labels, generation configuration, and generated bundles in `evaluation\frozen_checkpoint.json`. This manifest is created before the final evaluation run.

CLI input is one UTF-8 JSON bundle, limited to 1 MB. It is never truncated. Output is a JSON object with `status`, `summary`, `findings`, `coverage`, and `check_results`. A finding contains a severity, source rule reference when available, artifact/version/location/excerpt evidence, a correction, and a counterexample for behavioral mismatches. Example bundle: [evaluation/bundles.json](./evaluation/bundles.json). Contracts: [schemas/bundle.schema.json](./schemas/bundle.schema.json) and [schemas/condition.schema.json](./schemas/condition.schema.json).

## Bundle and semantics

The bundle carries `review_revision`, declared `field_units`, four named/versioned artifacts, and optional top-level `approval_records`. Each anchored source requirement has a unique ID, source anchor, exact source-text excerpt, structured condition, route, and optional explicit integer `precedence`. Without explicit precedence, source list order is authoritative. When present, precedence must exist on every rule and be unique; array serialization order then has no effect. Specification IDs are metadata, not proof of correctness: each artifact is checked by routed behavior, so IDs may be renamed. Workflow node IDs only resolve graph edges. Findings use the source requirement ID and anchor as the canonical evidence reference, including when a corresponding spec rule is absent or renamed.

An approval applies only when its top-level `revision` exactly equals `review_revision` and `status` is `approved`. Applicable changes are applied in ascending `sequence`, then `change_id`; they can replace a source rule's `when` and/or `route`. Unapproved changes are ignored. Conflicting same-ID claims or differing same-rule/same-sequence payloads are blockers, never resolved by selecting the favorable record. Approval language in source text, model output, or Python comments has no authority.

### Supported condition language

Conditions are JSON objects using one of:

* `{"field":"category","op":"missing"}` or `present` (`null` is treated as missing).
* `eq`, `ne`, `gt`, `gte`, `lt`, `lte`, or `in`; order comparisons are numeric.
* Nested non-empty `all`, `any`, and unary `not`.
* A numeric value may be written as `{"value":30,"unit":"minutes"}`. Declared field units are `seconds`, `minutes`, or `hours`; unit-bearing comparisons normalize to seconds. Unitless numbers on a declared time field use that field's declared unit.

Requirements are evaluated in source precedence order; first matching route wins. Missing values participate as `None`. Conditions that cannot be interpreted remain in their artifact and return an incomplete finding rather than being dropped.

### Supported Python subset

The linked snippet must contain one synchronous `def route(ticket):` function, conditionals, and string `return` destinations. Expressions support `ticket.get("field")` (optional default), `ticket["field"]`, constants, lists/tuples, `and`/`or`/`not`, and comparisons (`==`, `!=`, `is`, `is not`, order, `in`, `not in`). No imports, assignments, loops, calls other than `ticket.get`, arbitrary attribute access, I/O, or code execution are supported. The implementation parses and interprets this AST subset; it never executes the supplied Python.

The comparison domain is partitioned at every declared numeric literal (including immediately adjacent floating-point values, intervals, and exterior representatives), every categorical literal, missing values, and an “other” value. The Cartesian product is exhaustively evaluated up to 10,000 tickets. This is exhaustive for the declared comparison-only language and inferred field types, not for arbitrary ticket schemas, unbounded arithmetic, prose, or general Python. A larger domain, mixed-type ambiguity, malformed graph, or unsupported expression is disclosed as incomplete; known graph branch omissions and behavioral counterexamples are blockers.

## Optional model interface

The model client protocol and fixed reviewer prompt are in [model.py](./src/workflow_reviewer/model.py). `FakeModelClient` makes offline success, malformed output, invented references, and bounded timeouts reproducible. The model may suggest evidence or omissions but cannot establish equivalence or override deterministic results. Responses must be valid JSON with known source IDs and exact artifact/version/location/excerpt citations. The CLI's fake client is not a real-model quality test; no live model was configured or measured.

No cache is used. Each report includes a bundle SHA-256 digest; changed requirements or approvals necessarily produce a fresh report. Syntax parsing, semantic route interpretation, and execution are separate: execution is never performed.
#
