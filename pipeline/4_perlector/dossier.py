"""The preference screen over a witness payload: no key may name a preference.

Presentation order is deterministic, because the bytes must reproduce, but it
carries no meaning: no numeric trust weight, no preferred order, no primary
flag. `assert_no_order_bearing_field` refuses any key that would say otherwise.
"""

from __future__ import annotations

from typing import Any, Final

from common.contracts.errors import ContractError

# Fragments, not exact names, because the field that reintroduces a preference
# will be called `trust_score` or `witness_priority` rather than `trust`. The
# list is deliberately longer than the words this build could plausibly emit:
# it is a tripwire for a later edit, and a tripwire that only names today's
# spellings catches nothing. Every fragment is checked against the whole
# dossier, so a new field whose name trips one is a conversation at review
# rather than a silent landing.
_FORBIDDEN_KEY_FRAGMENTS: Final = (
    "primary",
    "prefer",
    "order",
    "rank",
    "trust",
    "weight",
    "score",
    "reliab",
    "select",
    "winner",
    "chosen",
    "priority",
    "better",
    "best",
    "picker",
    "consensus",
    "majority",
    "vote",
    "quorum",
)


# One walk position, as a crumb and its parent. `None` is the root's parent.
# Rendered by `_walk_position`, and only when a refusal has to name a place.
_Trail = tuple[str, Any] | None

# A refusal an operator cannot read has not named anything. The position of a
# field buried a million levels down inside a Testimonium is its top, its
# bottom, and how far apart they are, not a path of several million characters.
# No dossier this build produces comes near this bound.
_MAX_RENDERED_CRUMBS: Final = 32


def _walk_position(trail: _Trail) -> str:
    """Render a sweep position. Called on the refusal path, never on the clean one."""
    crumbs: list[str] = []
    while trail is not None:
        crumb, trail = trail
        crumbs.append(crumb)
    crumbs.reverse()
    if len(crumbs) > 2 * _MAX_RENDERED_CRUMBS:
        head = "".join(crumbs[:_MAX_RENDERED_CRUMBS])
        tail = "".join(crumbs[-_MAX_RENDERED_CRUMBS:])
        return f"{head}...({len(crumbs) - 2 * _MAX_RENDERED_CRUMBS} more levels)...{tail}"
    return "".join(crumbs)


def assert_no_order_bearing_field(value: Any, path: str = "$") -> None:
    """A durable sweep: no key anywhere in a witness payload may name a preference.

    No production path calls it; `test_page_feed.py` runs it over the page feed.

    Iterative, not recursive: a payload can carry model-authored JSON whose depth
    this build does not choose, and a recursive walk over a deep one would raise
    `RecursionError` -- a crash naming nothing.

    The path is assembled only when a field is refused, so a deep payload costs
    this walk its own list rather than the square of its depth in string bytes.
    A cycle is refused rather than looped on: the recursive form ended a cycle
    by exhausting itself, and a walk with no stack to exhaust would hang.
    """
    pending: list[tuple[str, Any, _Trail]] = [("value", value, (path, None))]
    open_path: set[int] = set()
    while pending:
        kind, current, trail = pending.pop()
        if kind == "exit":
            open_path.discard(current)
            continue
        if kind == "key":
            lowered = current.lower()
            if any(fragment in lowered for fragment in _FORBIDDEN_KEY_FRAGMENTS):
                raise ContractError(
                    f"{_walk_position(trail)}.{current} names a preference among witnesses. "
                    "An order-bearing or trust-bearing dossier field would make the reader a "
                    "picker. Remove the field and rebuild the dossier from unranked testimony."
                )
            continue
        if isinstance(current, (dict, list, tuple)):
            marker = id(current)
            if marker in open_path:
                raise ContractError(
                    f"{_walk_position(trail)} contains itself, so no sweep of this dossier "
                    "can terminate and none of it can be sealed. Rebuild the dossier from "
                    "values that are not their own ancestors."
                )
            open_path.add(marker)
            pending.append(("exit", marker, None))
        if isinstance(current, dict):
            # A key is checked where the recursive sweep checked it: after the
            # preceding sibling's whole subtree and before its own value's, so a
            # dossier with two offenders is still named by the first one.
            tasks: list[tuple[str, Any, _Trail]] = []
            for key, item in current.items():
                tasks.append(("key", key, trail))
                tasks.append(("value", item, (f".{key}", trail)))
            pending.extend(reversed(tasks))
        elif isinstance(current, (list, tuple)):
            # A tuple serializes exactly like a list through `canonical_bytes`,
            # so a field wrapped in one must be screened the same way or it
            # reaches the sealed dossier unexamined.
            for index in range(len(current) - 1, -1, -1):
                pending.append(("value", current[index], (f"[{index}]", trail)))
