"""Deterministic cross-artifact workflow review."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .dsl import (
    UnsupportedContent,
    condition_fields,
    evaluate_condition,
    route_from_python,
    route_from_rules,
    ticket_domain,
)

MAX_BUNDLE_BYTES = 1_000_000
MAX_DOMAIN_TICKETS = 10_000


class WorkflowDefect(ValueError):
    """A decision branch is absent or points to a nonexistent node."""

    def __init__(self, message: str, rule_ref: str | None = None):
        super().__init__(message)
        self.rule_ref = rule_ref


def _evidence(artifact: dict[str, Any], location: str, excerpt: Any) -> dict[str, Any]:
    return {
        "artifact": artifact.get("name", "unknown"),
        "version": artifact.get("version", "unknown"),
        "location": location,
        "excerpt": excerpt,
    }


def _finding(
    severity: str,
    title: str,
    rule_ref: str | None,
    evidence: list[dict[str, Any]],
    correction: str,
    *,
    counterexample: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = {
        "severity": severity,
        "title": title,
        "rule_ref": rule_ref,
        "evidence": evidence,
        "correction": correction,
    }
    if counterexample is not None:
        result["counterexample"] = counterexample
    return result


def _artifact(bundle: dict[str, Any], key: str) -> dict[str, Any]:
    value = bundle.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    if not isinstance(value.get("version"), str) or not isinstance(value.get("name"), str):
        raise ValueError(f"{key} must include string name and version")
    allowed = {
        "source": {"name", "version", "requirements"},
        "specification": {"name", "version", "rules"},
        "workflow": {"name", "version", "start", "nodes"},
        "snippet": {"name", "version", "source"},
    }[key]
    if set(value) - allowed:
        raise ValueError(f"{key} contains unsupported fields: {sorted(set(value) - allowed)}")
    return value


def _validate_rules(rules: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(rules, list):
        raise UnsupportedContent(f"{label} rules must be a list")
    identifiers = []
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise UnsupportedContent(f"{label} rule {index} must be an object")
        if not all(isinstance(rule.get(key), str) and rule[key] for key in ("id", "anchor", "source_text", "route")):
            raise UnsupportedContent(f"{label} rule {index} requires id, anchor, source_text and route")
        if not isinstance(rule.get("when"), dict):
            raise UnsupportedContent(f"{label} rule {index} requires a condition")
        condition_fields(rule["when"])
        identifiers.append(rule["id"])
        if set(rule) - {"id", "anchor", "source_text", "route", "when", "precedence"}:
            raise UnsupportedContent(f"{label} rule {index} contains unsupported fields")
    if len(set(identifiers)) != len(identifiers):
        raise UnsupportedContent(f"{label} rule identifiers must be unique")
    declared = [rule.get("precedence") for rule in rules]
    if any(value is not None for value in declared):
        if any(not isinstance(value, int) or isinstance(value, bool) for value in declared):
            raise UnsupportedContent(f"{label} precedence must be an integer on every rule")
        if len(set(declared)) != len(declared):
            raise UnsupportedContent(f"{label} precedence values must be unique")
        rules = sorted(rules, key=lambda rule: rule["precedence"])
    return rules


def _effective_requirements(bundle: dict[str, Any], source: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    rules = [dict(rule) for rule in _validate_rules(source.get("requirements"), "source")]
    findings: list[dict[str, Any]] = []
    revision = bundle.get("review_revision")
    if not isinstance(revision, str) or not revision:
        raise UnsupportedContent("review_revision must be a non-empty string")
    records = bundle.get("approval_records", [])
    if not isinstance(records, list):
        raise UnsupportedContent("approval_records must be a list")
    applicable: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            raise UnsupportedContent("approval record must be an object")
        if set(record) - {"change_id", "revision", "sequence", "target_rule", "status", "replacement"}:
            raise UnsupportedContent("approval record contains unsupported fields")
        if "replacement" in record and (
            not isinstance(record["replacement"], dict)
            or set(record["replacement"]) - {"when", "route"}
        ):
            raise UnsupportedContent("approval replacement contains unsupported fields")
        if record.get("status") not in {"approved", "unapproved"}:
            raise UnsupportedContent("approval status must be approved or unapproved")
        if (
            not isinstance(record.get("change_id"), str)
            or not isinstance(record.get("target_rule"), str)
            or not isinstance(record.get("sequence"), int)
            or isinstance(record.get("sequence"), bool)
            or not isinstance(record.get("revision"), str)
        ):
            raise UnsupportedContent("approval record requires change_id, revision, target_rule and integer sequence")
        if record.get("revision") == revision:
            applicable.append(record)
    approval_artifact = {"name": "approval_records", "version": str(revision)}

    identity_claims: dict[str, set[str]] = {}
    for record in applicable:
        change_id = record.get("change_id")
        if isinstance(change_id, str):
            identity_claims.setdefault(change_id, set()).add(
                json.dumps(record, sort_keys=True, separators=(",", ":"))
            )
    conflict = any(len(claims) > 1 for claims in identity_claims.values())
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in applicable:
        rule_ref = record.get("target_rule")
        if not isinstance(rule_ref, str):
            continue
        grouped.setdefault((rule_ref, str(record.get("sequence", ""))), []).append(record)
    for (rule_ref, _), group in grouped.items():
        payloads = {
            json.dumps(
                {"status": item.get("status"), "replacement": item.get("replacement")},
                sort_keys=True,
                separators=(",", ":"),
            )
            for item in group
        }
        if len(payloads) > 1:
            conflict = True
            findings.append(_finding(
                "blocker",
                "Conflicting approval records apply to the same rule and sequence",
                rule_ref,
                [
                    _evidence(approval_artifact, f"approval_records[{index}]", record)
                    for index, record in enumerate(applicable)
                    if record.get("target_rule") == rule_ref
                ],
                "Resolve the approval records and publish one authoritative record for this revision.",
            ))
    if conflict and not findings:
        findings.append(_finding(
            "blocker",
            "Contradictory records claim the same change identifier",
            None,
            [
                _evidence(approval_artifact, f"approval_records[{index}]", record)
                for index, record in enumerate(applicable)
            ],
            "Resolve the contradictory records; prose approval claims are not authoritative.",
        ))
    if conflict:
        return rules, findings, True

    index_by_id = {rule.get("id"): index for index, rule in enumerate(rules)}
    for record in sorted(applicable, key=lambda item: (item.get("sequence", 0), item.get("change_id", ""))):
        if record.get("status") != "approved":
            continue
        target = record.get("target_rule")
        replacement = record.get("replacement")
        if target not in index_by_id:
            findings.append(_finding(
                "incomplete",
                "Approved change targets an unknown source requirement",
                target if isinstance(target, str) else None,
                [_evidence(approval_artifact, "approval_records", record)],
                "Correct the target reference or add the anchored source requirement.",
            ))
            continue
        if not isinstance(replacement, dict):
            findings.append(_finding(
                "incomplete",
                "Approved change has no structured replacement",
                target,
                [_evidence(approval_artifact, "approval_records", record)],
                "Provide a supported structured replacement condition and route.",
            ))
            continue
        allowed = {"when", "route"}
        if set(replacement) - allowed or not replacement:
            findings.append(_finding(
                "incomplete",
                "Approved change uses an unsupported replacement shape",
                target,
                [_evidence(approval_artifact, "approval_records", record)],
                "Use only a replacement when condition and/or route.",
            ))
            continue
        updated = dict(rules[index_by_id[target]])
        updated.update(replacement)
        try:
            condition_fields(updated.get("when"))
            if not isinstance(updated.get("route"), str):
                raise UnsupportedContent("replacement route must be a string")
        except UnsupportedContent as exc:
            findings.append(_finding(
                "incomplete",
                "Approved change cannot be interpreted",
                target,
                [_evidence(source, "approval_records", record)],
                f"Replace the unsupported change with a structured condition and route ({exc}).",
            ))
            continue
        rules[index_by_id[target]] = updated
    return rules, findings, conflict


def _workflow_router(workflow: dict[str, Any]):
    nodes = workflow.get("nodes")
    if not isinstance(nodes, list):
        raise UnsupportedContent("workflow nodes must be a list")
    by_id = {}
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get("id"), str):
            raise UnsupportedContent("each workflow node requires a string id")
        allowed = (
            {"id", "type", "rule_ref", "when", "then", "else"}
            if node.get("type") == "decision"
            else {"id", "type", "rule_ref", "route"}
        )
        if set(node) - allowed:
            raise UnsupportedContent(f"workflow node {node['id']} contains unsupported fields")
        if node["id"] in by_id:
            raise UnsupportedContent(f"duplicate workflow node id: {node['id']}")
        by_id[node["id"]] = node
    start = workflow.get("start")
    if start not in by_id:
        raise UnsupportedContent("workflow start must identify an existing node")
    for node in nodes:
        if node.get("type") == "decision":
            condition_fields(node.get("when"))
            for edge in ("then", "else"):
                if edge not in node:
                    raise WorkflowDefect(
                        f"decision node {node['id']} is missing its {edge} branch",
                        node.get("rule_ref"),
                    )
                if node.get(edge) not in by_id:
                    raise WorkflowDefect(
                        f"decision node {node['id']} has nonexistent {edge} destination",
                        node.get("rule_ref"),
                    )
        elif node.get("type") == "route":
            if not isinstance(node.get("route"), str):
                raise UnsupportedContent(f"route node {node['id']} requires a string route")
        else:
            raise UnsupportedContent(f"unsupported workflow node type at {node['id']}")

    def route(ticket: dict[str, Any], field_units: dict[str, str]) -> str | None:
        seen: set[str] = set()
        current = start
        while True:
            if current in seen:
                raise UnsupportedContent("workflow contains a reachable cycle")
            seen.add(current)
            node = by_id[current]
            if node["type"] == "route":
                return node["route"]
            edge = "then" if evaluate_condition(node["when"], ticket, field_units) else "else"
            current = node[edge]

    return route, set(by_id)


def _validate_snippet_meta(snippet: dict[str, Any]) -> str:
    if not isinstance(snippet, dict) or not isinstance(snippet.get("source"), str):
        raise UnsupportedContent("linked snippet requires source text")
    return snippet["source"]


def review(bundle: dict[str, Any], *, ablate_code: bool = False, checkpoint: bool = False) -> dict[str, Any]:
    """Review one bundle. No artifact source is executed and no result is cached."""
    if not isinstance(bundle, dict):
        return _result(
            "INCOMPLETE",
            [_finding("incomplete", "Review bundle must be a JSON object", None, [], "Provide an object-shaped review bundle.")],
            [], [{"check": "input_contract", "status": "incomplete"}], "",
            ["input bundle"],
        )
    digest = hashlib.sha256(
        json.dumps(bundle, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    findings: list[dict[str, Any]] = []
    check_results: list[dict[str, Any]] = []
    unreviewed: list[str] = []
    allowed_bundle_fields = {
        "review_revision", "field_units", "source", "specification",
        "workflow", "snippet", "approval_records",
    }
    if set(bundle) - allowed_bundle_fields:
        return _result(
            "INCOMPLETE",
            [_finding(
                "incomplete", "Review bundle contains unsupported top-level fields", None,
                [], f"Remove or explicitly support fields: {sorted(set(bundle) - allowed_bundle_fields)}.",
            )],
            [], [{"check": "input_contract", "status": "incomplete"}], digest,
            ["unrecognized bundle fields"],
        )
    try:
        source = _artifact(bundle, "source")
        spec = _artifact(bundle, "specification")
        workflow = _artifact(bundle, "workflow")
        snippet = _artifact(bundle, "snippet")
    except ValueError as exc:
        return _result(
            "INCOMPLETE",
            [_finding("incomplete", str(exc), None, [], "Supply complete versioned artifacts.")],
            [], [{"check": "input_contract", "status": "incomplete"}], digest,
            ["versioned artifacts"],
        )

    approval_conflict = False
    try:
        source_rules, approval_findings, approval_conflict = _effective_requirements(bundle, source)
        findings.extend(approval_findings)
        if approval_conflict:
            unreviewed.append("effective requirements (conflicting approval records)")
        if not isinstance(bundle.get("field_units", {}), dict):
            raise UnsupportedContent("field_units must be an object")
        field_units = bundle.get("field_units", {})
        spec_rules = _validate_rules(spec.get("rules"), "specification")
        if any(unit not in {"seconds", "minutes", "hours"} for unit in field_units.values()):
            raise UnsupportedContent("field_units values must be seconds, minutes, or hours")
    except (UnsupportedContent, TypeError, KeyError) as exc:
        findings.append(_finding(
            "incomplete", "Effective requirements or specification are unsupported", None,
            [_evidence(source, "requirements", source.get("requirements")), _evidence(spec, "rules", spec.get("rules"))],
            f"Express the content in the declared structured language ({exc}).",
        ))
        unreviewed.append("requirements and specification semantics")
        source_rules = []
        spec_rules = []

    workflow_route = None
    workflow_ids: set[str] = set()
    if checkpoint:
        unreviewed.append("workflow paths")
        workflow_check = "not_checked"
    else:
        workflow_check = "incomplete"
        try:
            workflow_route, workflow_ids = _workflow_router(workflow)
            workflow_check = "complete"
            reachable = {workflow["start"]}
            pending = [workflow["start"]]
            nodes = {node["id"]: node for node in workflow["nodes"]}
            while pending:
                node = nodes[pending.pop()]
                for edge in ("then", "else") if node["type"] == "decision" else ():
                    if node[edge] not in reachable:
                        reachable.add(node[edge])
                        pending.append(node[edge])
            unreachable = workflow_ids - reachable
            if unreachable:
                findings.append(_finding(
                    "incomplete", "Workflow contains unreachable nodes", None,
                    [_evidence(workflow, "nodes", sorted(unreachable))],
                    "Remove unreachable nodes or connect them to a reachable path.",
                ))
                unreviewed.append("unreachable workflow nodes")
                workflow_check = "incomplete"
        except WorkflowDefect as exc:
            source_ids = {rule.get("id") for rule in source_rules}
            findings.append(_finding(
                "blocker", "Workflow has a missing or invalid required branch",
                exc.rule_ref if exc.rule_ref in source_ids else None,
                [_evidence(workflow, "nodes", workflow.get("nodes"))],
                f"Restore both true and false destinations and connect them to existing nodes ({exc}).",
            ))
            unreviewed.append("workflow paths")
            workflow_check = "failed"
        except (UnsupportedContent, TypeError, KeyError) as exc:
            findings.append(_finding(
                "incomplete", "Workflow graph cannot be evaluated", None,
                [_evidence(workflow, "nodes", workflow.get("nodes"))],
                f"Repair the graph using supported decision and route nodes ({exc}).",
            ))
            unreviewed.append("workflow paths")
            workflow_check = "incomplete"
    check_results.append({"check": "workflow_graph_validation", "status": workflow_check})

    python_check = "not_checked" if checkpoint else "incomplete"
    try:
        code = _validate_snippet_meta(snippet)
        if checkpoint:
            unreviewed.append("linked Python snippet (outside initial checkpoint)")
        else:
            route_from_python(code, {})
            python_check = "complete"
        if ablate_code:
            unreviewed.append("linked Python snippet (disabled by controlled ablation)")
    except (UnsupportedContent, TypeError, KeyError) as exc:
        code = None
        findings.append(_finding(
            "incomplete", "Linked Python snippet is outside the safe supported subset", None,
            [_evidence(snippet, "source", snippet.get("source"))],
            f"Rewrite the snippet using only route(ticket), conditionals and string returns ({exc}).",
        ))
        unreviewed.append("linked Python snippet")
        python_check = "incomplete"
    check_results.append({"check": "python_parse_and_subset_validation", "status": python_check})
    check_results.append({"check": "python_execution", "status": "not_performed"})

    if not checkpoint and (not source_rules or not spec_rules):
        unreviewed.append("cross-artifact behavioral equivalence")
    if source_rules and spec_rules:
        try:
            all_rules = [source_rules, spec_rules]
            workflows = [workflow] if workflow_route else []
            snippets = [code] if code and not (ablate_code or checkpoint) else []
            tickets, exhaustive = ticket_domain(
                all_rules, workflows, snippets, field_units, limit=MAX_DOMAIN_TICKETS
            )
            if not exhaustive:
                unreviewed.append(f"ticket domain exceeds {MAX_DOMAIN_TICKETS} combinations")
                findings.append(_finding(
                    "incomplete", "The supported ticket domain exceeds the exhaustive check limit", None,
                    [_evidence(source, "requirements", len(source_rules))],
                    "Reduce the declared domain or use a reviewer configured for a larger bounded domain.",
                ))
            for artifact_name, rules, artifact in (
                ("specification", spec_rules, spec),
            ):
                mismatch = _first_mismatch(source_rules, rules, tickets, field_units)
                if mismatch:
                    ticket, expected, actual, rule_ref = mismatch
                    findings.append(_finding(
                        "blocker", "Specification behavior disagrees with effective requirements",
                        rule_ref,
                        [_evidence(source, f"requirements/{rule_ref}", _rule_by_id(source_rules, rule_ref)),
                         _evidence(artifact, "rules", artifact.get("rules"))],
                        "Align the specification condition, action and precedence with the effective requirement.",
                        counterexample={"ticket": ticket, "expected_route": expected, "actual_route": actual},
                    ))
            if not checkpoint and workflow_route:
                mismatch = _first_router_mismatch(
                    source_rules, lambda ticket: workflow_route(ticket, field_units), tickets, field_units
                )
                if mismatch:
                    ticket, expected, actual, rule_ref = mismatch
                    findings.append(_finding(
                        "blocker", "Workflow path disagrees with effective requirements", rule_ref,
                        [_evidence(source, f"requirements/{rule_ref}", _rule_by_id(source_rules, rule_ref)),
                         _evidence(workflow, "nodes", workflow.get("nodes"))],
                        "Correct the decision condition or branch destination while preserving requirement precedence.",
                        counterexample={"ticket": ticket, "expected_route": expected, "actual_route": actual},
                    ))
            if not checkpoint and not ablate_code and code:
                mismatch = _first_router_mismatch(
                    source_rules, lambda ticket: route_from_python(code, ticket), tickets, field_units
                )
                if mismatch:
                    ticket, expected, actual, rule_ref = mismatch
                    findings.append(_finding(
                        "blocker", "Linked Python behavior disagrees with effective requirements", rule_ref,
                        [_evidence(source, f"requirements/{rule_ref}", _rule_by_id(source_rules, rule_ref)),
                         _evidence(snippet, "source", code)],
                        "Update the restricted route function to implement the effective conditions in order.",
                        counterexample={"ticket": ticket, "expected_route": expected, "actual_route": actual},
                    ))
            check_results.append({
                "check": "bounded_exhaustive_behavioral_equivalence",
                "status": "complete" if exhaustive else "incomplete",
                "tickets_checked": len(tickets),
                "domain_limit": MAX_DOMAIN_TICKETS,
                "approach": "exhaustive over generated boundary, categorical, missing and other-value representatives",
            })
        except (UnsupportedContent, TypeError, KeyError, ValueError) as exc:
            findings.append(_finding(
                "incomplete", "Behavioral comparison could not be completed", None,
                [_evidence(source, "requirements", source.get("requirements"))],
                f"Resolve unsupported or ambiguous behavior ({exc}).",
            ))
            unreviewed.append("behavioral equivalence")

    if approval_conflict:
        check_results.append({"check": "approval_authority", "status": "conflict"})
    elif not any(item.get("check") == "approval_authority" for item in check_results):
        check_results.append({"check": "approval_authority", "status": "complete"})
    has_blocker = any(item["severity"] == "blocker" for item in findings)
    has_incomplete = bool(unreviewed) or any(item["severity"] == "incomplete" for item in findings)
    status = "CHANGES_REQUIRED" if has_blocker else "INCOMPLETE" if has_incomplete else "PASS"
    coverage = [
        {
            "rule_ref": rule.get("id"),
            "source_location": rule.get("anchor"),
            "effective_condition": rule.get("when"),
            "route": rule.get("route"),
            "reviewed": not has_incomplete,
        }
        for rule in source_rules
    ]
    return _result(status, findings, coverage, check_results, digest, unreviewed)


def _rule_by_id(rules: list[dict[str, Any]], rule_ref: str | None) -> Any:
    return next((rule for rule in rules if rule.get("id") == rule_ref), None)


def _first_mismatch(
    expected_rules: list[dict[str, Any]],
    actual_rules: list[dict[str, Any]],
    tickets: list[dict[str, Any]],
    field_units: dict[str, str],
):
    for ticket in tickets:
        expected = route_from_rules(expected_rules, ticket, field_units)
        actual = route_from_rules(actual_rules, ticket, field_units)
        if expected != actual:
            return ticket, expected, actual, _winning_rule(expected_rules, ticket, field_units)
    return None


def _first_router_mismatch(expected_rules, actual_router, tickets, field_units):
    for ticket in tickets:
        expected = route_from_rules(expected_rules, ticket, field_units)
        actual = actual_router(ticket)
        if expected != actual:
            return ticket, expected, actual, _winning_rule(expected_rules, ticket, field_units)
    return None


def _winning_rule(rules, ticket, field_units):
    for rule in rules:
        if evaluate_condition(rule["when"], ticket, field_units):
            return rule.get("id")
    return None


def _result(status, findings, coverage, check_results, digest, unreviewed=None):
    return {
        "status": status,
        "summary": {
            "finding_count": len(findings),
            "blocker_count": sum(item["severity"] == "blocker" for item in findings),
            "unreviewed_scope": sorted(set(unreviewed or [])),
            "bundle_sha256": digest,
        },
        "findings": findings,
        "coverage": coverage,
        "check_results": check_results,
    }


def report_json(bundle: dict[str, Any], **options: Any) -> str:
    return json.dumps(review(bundle, **options), indent=2, sort_keys=True)
