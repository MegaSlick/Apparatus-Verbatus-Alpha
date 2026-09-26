import ast

import pytest


@pytest.fixture
def exercised_reasons(request) -> set[str]:
    return asserted_reasons(request.path.read_text(encoding="utf-8"))


def _parameter_names(node: ast.expr) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [name.strip() for name in node.value.split(",")]
    if isinstance(node, (ast.Tuple, ast.List)):
        return [elt.value for elt in node.elts if isinstance(elt, ast.Constant)]
    return []


def asserted_reasons(source: str) -> set[str]:
    """Every refusal reason a test source asserts, read from its syntax tree.

    A reason counts when it is the anchored `match="^reason:"` of a `pytest.raises`,
    the last column of a `parametrize` table whose last parameter is `reason`, or
    the second argument of an `_only_reason` call. Comments never count.
    """
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "_only_reason":
            if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                found.add(node.args[1].value)
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr == "raises":
            for keyword in node.keywords:
                value = keyword.value
                if keyword.arg == "match" and isinstance(value, ast.Constant):
                    text = value.value
                    if isinstance(text, str) and text.startswith("^") and text.endswith(":"):
                        found.add(text[1:-1])
        if func.attr == "parametrize" and len(node.args) > 1:
            names, cases = node.args[0], node.args[1]
            if _parameter_names(names)[-1:] == ["reason"] and isinstance(
                cases, (ast.List, ast.Tuple)
            ):
                for case in cases.elts:
                    if isinstance(case, (ast.Tuple, ast.List)) and isinstance(
                        case.elts[-1], ast.Constant
                    ):
                        found.add(case.elts[-1].value)
    return found
