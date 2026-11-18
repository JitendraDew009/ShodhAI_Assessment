"""Evaluation helpers for the declared condition language and safe Python subset."""

from __future__ import annotations

import ast
import itertools
import math
from typing import Any, Iterable


class UnsupportedContent(ValueError):
    """Raised when an artifact uses syntax outside the declared language."""


def condition_fields(condition: Any) -> set[str]:
    if not isinstance(condition, dict):
        raise UnsupportedContent("condition must be an object")
    if "all" in condition or "any" in condition:
        key = "all" if "all" in condition else "any"
        if set(condition) != {key}:
            raise UnsupportedContent(f"{key} condition cannot contain additional keys")
        children = condition[key]
        if not isinstance(children, list) or not children:
            raise UnsupportedContent(f"{key} must be a non-empty list")
        return set().union(*(condition_fields(child) for child in children))
    if "not" in condition:
        if set(condition) != {"not"}:
            raise UnsupportedContent("not condition cannot contain additional keys")
        return condition_fields(condition["not"])
    if set(condition) == {"field", "op", "value"} or set(condition) == {"field", "op"}:
        field, op = condition["field"], condition["op"]
        if not isinstance(field, str) or op not in {
            "eq", "ne", "gt", "gte", "lt", "lte", "in", "missing", "present"
        }:
            raise UnsupportedContent("invalid field comparison")
        if op in {"missing", "present"} and "value" in condition:
            raise UnsupportedContent(f"{op} does not accept a value")
        if op not in {"missing", "present"} and "value" not in condition:
            raise UnsupportedContent(f"{op} requires a value")
        return {field}
    raise UnsupportedContent("condition must use all, any, not, or a field comparison")


def _quantity(value: Any, unit: str | None) -> float | int:
    if isinstance(value, dict) and set(value) == {"value", "unit"}:
        value, unit = value["value"], value["unit"]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UnsupportedContent("numeric comparison value must be a number")
    if not math.isfinite(value):
        raise UnsupportedContent("numeric comparison value must be finite")
    factors = {"seconds": 1, "minutes": 60, "hours": 3600}
    if unit is None:
        return value
    if unit not in factors:
        raise UnsupportedContent(f"unsupported time unit: {unit}")
    return value * factors[unit]


def _normalize_field_value(field: str, value: Any, field_units: dict[str, str]) -> Any:
    if field in field_units and value is not None:
        return _quantity(value, field_units[field])
    return value


def evaluate_condition(
    condition: Any, ticket: dict[str, Any], field_units: dict[str, str] | None = None
) -> bool:
    field_units = field_units or {}
    if "all" in condition:
        return all(evaluate_condition(item, ticket, field_units) for item in condition["all"])
    if "any" in condition:
        return any(evaluate_condition(item, ticket, field_units) for item in condition["any"])
    if "not" in condition:
        return not evaluate_condition(condition["not"], ticket, field_units)
    field, op = condition["field"], condition["op"]
    if op == "missing":
        return field not in ticket or ticket[field] is None
    if op == "present":
        return field in ticket and ticket[field] is not None
    left = _normalize_field_value(field, ticket.get(field), field_units)
    right = condition["value"]
    if isinstance(right, dict) and "unit" in right:
        right = _quantity(right, None)
    elif field in field_units and isinstance(right, (int, float)) and not isinstance(right, bool):
        right = _quantity(right, field_units[field])
    if op == "in":
        if not isinstance(right, list):
            raise UnsupportedContent("in comparison requires a list")
        if field in field_units:
            right = [
                _quantity(item, field_units[field])
                if isinstance(item, (int, float)) and not isinstance(item, bool)
                else item
                for item in right
            ]
        return left in right
    if left is None:
        return op == "eq" and right is None or op == "ne" and right is not None
    if op == "eq":
        return left == right
    if op == "ne":
        return left != right
    if op in {"gt", "gte", "lt", "lte"} and (
        isinstance(left, bool) or isinstance(right, bool)
    ):
        raise UnsupportedContent("boolean values cannot be ordered")
    try:
        return {
            "gt": lambda: left > right,
            "gte": lambda: left >= right,
            "lt": lambda: left < right,
            "lte": lambda: left <= right,
        }[op]()
    except (TypeError, KeyError) as exc:
        raise UnsupportedContent(f"incompatible operands for {op}") from exc


def route_from_rules(
    rules: list[dict[str, Any]], ticket: dict[str, Any], field_units: dict[str, str] | None = None
) -> str | None:
    for rule in rules:
        if evaluate_condition(rule["when"], ticket, field_units):
            return rule["route"]
    return None


def _py_expr(node: ast.AST, ticket: dict[str, Any]) -> Any:
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, int, float, bool, type(None))):
        return node.value
    if isinstance(node, ast.Name) and node.id == "ticket":
        return ticket
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if (
            isinstance(node.func.value, ast.Name) and node.func.value.id == "ticket"
            and node.func.attr == "get" and len(node.args) in (1, 2) and not node.keywords
            and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)
        ):
            return ticket.get(node.args[0].value, _py_expr(node.args[1], ticket) if len(node.args) == 2 else None)
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "ticket":
        key = _py_expr(node.slice, ticket)
        if not isinstance(key, str):
            raise UnsupportedContent("ticket subscript key must be a string")
        return ticket.get(key)
    if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
        if isinstance(node.op, ast.And):
            for value in node.values:
                if not _py_expr(value, ticket):
                    return False
            return True
        for value in node.values:
            if _py_expr(value, ticket):
                return True
        return False
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _py_expr(node.operand, ticket)
    if isinstance(node, ast.Compare):
        left = _py_expr(node.left, ticket)
        for op, comparator in zip(node.ops, node.comparators):
            right = _py_expr(comparator, ticket)
            if isinstance(op, (ast.Is, ast.Eq)):
                passed = left == right
            elif isinstance(op, (ast.IsNot, ast.NotEq)):
                passed = left != right
            elif isinstance(op, ast.Gt):
                passed = left is not None and left > right
            elif isinstance(op, ast.GtE):
                passed = left is not None and left >= right
            elif isinstance(op, ast.Lt):
                passed = left is not None and left < right
            elif isinstance(op, ast.LtE):
                passed = left is not None and left <= right
            elif isinstance(op, ast.In):
                passed = left in right
            elif isinstance(op, ast.NotIn):
                passed = left not in right
            else:
                raise UnsupportedContent("unsupported Python comparison")
            if not passed:
                return False
            left = right
        return True
    if isinstance(node, ast.List):
        return [_py_expr(item, ticket) for item in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(_py_expr(item, ticket) for item in node.elts)
    raise UnsupportedContent(f"unsupported Python expression: {type(node).__name__}")


def _py_block(statements: list[ast.stmt], ticket: dict[str, Any]) -> str | None:
    for statement in statements:
        if isinstance(statement, ast.If):
            block = statement.body if _py_expr(statement.test, ticket) else statement.orelse
            result = _py_block(block, ticket)
            if result is not None:
                return result
        elif isinstance(statement, ast.Return):
            value = _py_expr(statement.value, ticket) if statement.value else None
            if not isinstance(value, str):
                raise UnsupportedContent("route function must return string destinations")
            return value
        else:
            raise UnsupportedContent(f"unsupported Python statement: {type(statement).__name__}")
    return None


def _validate_py_expression(node: ast.AST) -> None:
    if isinstance(node, ast.Constant):
        if not isinstance(node.value, (str, int, float, bool, type(None))):
            raise UnsupportedContent("unsupported Python constant")
    elif isinstance(node, ast.Name):
        raise UnsupportedContent("a ticket field must be read with get or a string subscript")
    elif isinstance(node, ast.Call):
        if not (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "ticket"
            and node.func.attr == "get"
            and len(node.args) in (1, 2)
            and not node.keywords
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            raise UnsupportedContent("only ticket.get with a literal field is supported")
        for argument in node.args:
            _validate_py_expression(argument)
    elif isinstance(node, ast.Subscript):
        if not (
            isinstance(node.value, ast.Name) and node.value.id == "ticket"
            and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str)
        ):
            raise UnsupportedContent("ticket subscripts require a literal string field")
    elif isinstance(node, ast.BoolOp):
        if not isinstance(node.op, (ast.And, ast.Or)):
            raise UnsupportedContent("unsupported Boolean operator")
        for value in node.values:
            _validate_py_expression(value)
    elif isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, ast.Not):
            raise UnsupportedContent("only unary not is supported")
        _validate_py_expression(node.operand)
    elif isinstance(node, ast.Compare):
        _validate_py_expression(node.left)
        for operation, comparator in zip(node.ops, node.comparators):
            if not isinstance(operation, (
                ast.Eq, ast.NotEq, ast.Is, ast.IsNot, ast.Gt, ast.GtE,
                ast.Lt, ast.LtE, ast.In, ast.NotIn,
            )):
                raise UnsupportedContent("unsupported Python comparison")
            _validate_py_expression(comparator)
    elif isinstance(node, (ast.List, ast.Tuple)):
        for element in node.elts:
            _validate_py_expression(element)
    else:
        raise UnsupportedContent(f"unsupported Python expression: {type(node).__name__}")


def _validate_py_block(statements: list[ast.stmt]) -> None:
    for statement in statements:
        if isinstance(statement, ast.If):
            _validate_py_expression(statement.test)
            _validate_py_block(statement.body)
            _validate_py_block(statement.orelse)
        elif isinstance(statement, ast.Return):
            if statement.value is not None:
                _validate_py_expression(statement.value)
        else:
            raise UnsupportedContent(f"unsupported Python statement: {type(statement).__name__}")


def route_from_python(source: str, ticket: dict[str, Any]) -> str | None:
    try:
        module = ast.parse(source)
    except SyntaxError as exc:
        raise UnsupportedContent(f"Python syntax error: {exc.msg}") from exc
    functions = [
        node for node in module.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    if len(module.body) != 1 or len(functions) != 1 or isinstance(functions[0], ast.AsyncFunctionDef):
        raise UnsupportedContent("snippet must contain only one synchronous function")
    function = functions[0]
    if function.name != "route" or [arg.arg for arg in function.args.args] != ["ticket"]:
        raise UnsupportedContent("snippet must define route(ticket)")
    if (
        function.args.vararg or function.args.kwarg or function.args.kwonlyargs
        or function.args.posonlyargs or function.args.defaults or function.args.kw_defaults
        or function.decorator_list or function.returns
    ):
        raise UnsupportedContent("function arguments and decorators are unsupported")
    _validate_py_block(function.body)
    return _py_block(function.body, ticket)


def _numeric_probes(threshold: float) -> set[float]:
    return {threshold}


def ticket_domain(
    rule_sets: Iterable[list[dict[str, Any]]],
    workflows: Iterable[dict[str, Any]],
    snippets: Iterable[str],
    field_units: dict[str, str],
    *,
    limit: int = 10000,
) -> tuple[list[dict[str, Any]], bool]:
    fields: set[str] = set()
    numeric_values: dict[str, set[float]] = {}
    categorical_values: dict[str, set[Any]] = {}

    def add_condition(condition: Any) -> None:
        for field in condition_fields(condition):
            fields.add(field)
        if "all" in condition or "any" in condition:
            for child in condition.get("all", condition.get("any", [])):
                add_condition(child)
        elif "not" in condition:
            add_condition(condition["not"])
        elif condition["op"] not in {"missing", "present"}:
            field, value = condition["field"], condition["value"]
            values = [value] if condition["op"] != "in" else value
            if isinstance(value, dict) and set(value) == {"value", "unit"}:
                factors = {"seconds": 1, "minutes": 60, "hours": 3600}
                if value["unit"] not in factors:
                    raise UnsupportedContent(f"unsupported time unit: {value['unit']}")
                base_value = _quantity(value, None)
                target_unit = field_units.get(field)
                values = [base_value / factors[target_unit]] if target_unit else [base_value]
            for item in values:
                if isinstance(item, dict):
                    raise UnsupportedContent("comparison values in lists must be scalar")
                if isinstance(item, (int, float)) and not isinstance(item, bool):
                    numeric_values.setdefault(field, set()).update(_numeric_probes(item))
                else:
                    categorical_values.setdefault(field, set()).add(item)

    try:
        for rules in rule_sets:
            for rule in rules:
                add_condition(rule["when"])
        for workflow in workflows:
            for node in workflow["nodes"]:
                if node["type"] == "decision":
                    add_condition(node["when"])
        for source in snippets:
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "ticket"
                    and node.func.attr == "get" and node.args
                    and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)
                ):
                    fields.add(node.args[0].value)
                elif isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "ticket":
                    if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                        fields.add(node.slice.value)
            for node in ast.walk(tree):
                if isinstance(node, ast.Compare):
                    left = node.left
                    field = None
                    if isinstance(left, ast.Call) and isinstance(left.func, ast.Attribute) and left.func.attr == "get":
                        if isinstance(left.func.value, ast.Name) and left.func.value.id == "ticket" and left.args and isinstance(left.args[0], ast.Constant):
                            field = left.args[0].value
                    elif isinstance(left, ast.Subscript) and isinstance(left.value, ast.Name) and left.value.id == "ticket":
                        if isinstance(left.slice, ast.Constant):
                            field = left.slice.value
                    for comparator in node.comparators:
                        if field is not None and isinstance(comparator, (ast.List, ast.Tuple)):
                            for item in comparator.elts:
                                if isinstance(item, ast.Constant):
                                    categorical_values.setdefault(field, set()).add(item.value)
                        if isinstance(comparator, ast.Constant) and isinstance(comparator.value, (str, int, float)):
                            if isinstance(comparator.value, (int, float)) and not isinstance(comparator.value, bool):
                                if field is not None:
                                    numeric_values.setdefault(field, set()).update(_numeric_probes(comparator.value))
                            else:
                                if field is not None:
                                    categorical_values.setdefault(field, set()).add(comparator.value)
    except (KeyError, TypeError, SyntaxError, UnsupportedContent):
        raise

    dimensions: list[tuple[str, list[Any]]] = []
    for field in sorted(fields):
        boundaries = sorted(numeric_values.get(field, set()))
        if boundaries:
            if any(value is not None for value in categorical_values.get(field, set())):
                raise UnsupportedContent(f"field {field} has mixed numeric and categorical comparisons")
            values: set[Any] = set(boundaries)
            values.update(math.nextafter(value, -math.inf) for value in boundaries)
            values.update(math.nextafter(value, math.inf) for value in boundaries)
            values.add(boundaries[0] - max(1.0, abs(boundaries[0]) + 1.0))
            values.add(boundaries[-1] + max(1.0, abs(boundaries[-1]) + 1.0))
            values.update(
                left + (right - left) / 2
                for left, right in zip(boundaries, boundaries[1:])
            )
            values.add(None)
        else:
            values = set(categorical_values.get(field, set()))
            other = "__other__"
            while other in values:
                other += "_"
            values.update({None, other})
        if field == "urgent":
            values.update({True, False})
        dimensions.append((field, list(values)))
    size = math.prod(len(values) for _, values in dimensions) if dimensions else 1
    if size > limit:
        return [], False
    tickets = []
    for values in itertools.product(*(values for _, values in dimensions)):
        tickets.append(dict(zip((field for field, _ in dimensions), values)))
    return tickets, True


def ordered_rules_semantic_key(rules: list[dict[str, Any]]) -> tuple[Any, ...]:
    """Kept for diagnostics; correctness is established by routed ticket comparisons."""
    return tuple((rule.get("when"), rule.get("route")) for rule in rules)
