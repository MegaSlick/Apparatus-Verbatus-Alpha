"""The recorded relationship between a prior draft and its establishing reading."""

from typing import Any, Callable

from common.contracts.errors import SchemaRefusal


def kind_for_view(view: str) -> str:
    if view == "fed":
        return "primed-with-prior"
    if view == "withheld":
        return "primed-draft-withheld"
    raise SchemaRefusal(f"an establishing reading has unknown prior-draft view {view!r}")


def self_revision_for_view(
    view: str, text: str, prior_text: str, measure: Callable[[str, str], list]
) -> list:
    kind_for_view(view)
    return measure(text, prior_text) if view == "fed" else []


def validate_establishing_view(payload: dict, dossier: Any, subject: str) -> str:
    """Bind kind, shown view, protocol, and the withheld revision claim."""
    kind = payload.get("lectio_kind")
    if kind not in ("primed-with-prior", "primed-draft-withheld"):
        raise SchemaRefusal(f"{subject} names non-establishing lectio kind {kind!r}")
    expected = "fed" if kind == "primed-with-prior" else "withheld"
    if not isinstance(dossier, dict) or dossier.get("prior_draft_view") != expected:
        raise SchemaRefusal(f"{subject} names {kind} without a {expected} prior-draft view")
    protocol = payload.get("protocol")
    if not isinstance(protocol, dict) or protocol.get("draft_fed") is not (expected == "fed"):
        raise SchemaRefusal(f"{subject} names {kind} contrary to its prior-draft protocol")
    if expected == "withheld" and payload.get("self_revision") != []:
        raise SchemaRefusal(
            f"{subject} claims self-revisions against a draft withheld from its reader"
        )
    return expected
