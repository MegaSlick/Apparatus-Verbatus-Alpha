"""The recorded relationship between a prior draft and its establishing reading."""

from typing import Any, Callable, Final

from common.contracts.errors import SchemaRefusal

# off: no blind read. fed: Pass A is fed to the establishing reading. saved: Pass A is made
# and kept as a training witness; the establishing reading never sees it.
BLIND_READ_MODES = ("off", "fed", "saved")


def refuse_removed_draft_fed(protocol: Any, subject: str) -> None:
    """Name the removed `--draft-fed` flag when an old record still carries its field."""
    if isinstance(protocol, dict) and "draft_fed" in protocol:
        raise SchemaRefusal(
            f"{subject} carries the protocol field draft_fed, which the removed --draft-fed "
            "flag sealed; it is now --blind-read (off, fed or saved), so the run predates it "
            "and must be re-read"
        )


def kind_for_view(view: str) -> str:
    if view == "fed":
        return "primed-with-prior"
    if view == "withheld":
        return "primed-draft-withheld"
    raise SchemaRefusal(f"an establishing reading has unknown prior-draft view {view!r}")


# A comparison stopped by its sealed step budget. Named so the record says the
# instrument stopped, not that the reading and the draft were found to agree.
COMPARISON_STEP_LIMIT_REASON: Final = "comparison-step-limit"
_UNMEASURED_FIELDS: Final = frozenset({"measured", "reason", "max_comparison_steps"})


def unmeasured_comparison(max_comparison_steps: int) -> dict[str, Any]:
    """The explicit non-verdict of a comparison that would pass its step budget."""
    return {
        "measured": False,
        "reason": COMPARISON_STEP_LIMIT_REASON,
        "max_comparison_steps": max_comparison_steps,
    }


def is_unmeasured_comparison(value: Any) -> bool:
    """Whether `value` is exactly the closed non-verdict `unmeasured_comparison` writes."""
    return (
        isinstance(value, dict)
        and set(value) == _UNMEASURED_FIELDS
        and value["measured"] is False
        and value["reason"] == COMPARISON_STEP_LIMIT_REASON
        and type(value["max_comparison_steps"]) is int
        and value["max_comparison_steps"] > 0
    )


def budget_stopped_comparisons(
    payload: Any, max_comparison_steps: int, subject: str
) -> tuple[bool, list[str]]:
    """Whether the sealed dissent budget stopped this reading's self-revision, and
    which chairs' dissent rows it stopped.

    Every stopped record names the budget it ran out of; one naming any other
    budget than the run sealed is refused, since the run never bound it.
    """
    if not isinstance(payload, dict):
        raise SchemaRefusal(f"{subject} has no object payload")
    recorded: list[tuple[str, Any]] = []
    revision = payload.get("self_revision")
    self_revision_stopped = is_unmeasured_comparison(revision)
    if self_revision_stopped:
        recorded.append(("self_revision", revision["max_comparison_steps"]))
    rows = payload.get("dissent")
    if not isinstance(rows, list):
        raise SchemaRefusal(f"{subject} carries no dissent record")
    chairs = []
    for row in rows:
        if isinstance(row, dict) and "max_comparison_steps" in row:
            chair = row.get("chair", row.get("letter"))
            if not isinstance(chair, str) or not chair:
                raise SchemaRefusal(f"{subject} has a stopped dissent row naming no witness")
            chairs.append(chair)
            recorded.append((f"its dissent row for {chair!r}", row["max_comparison_steps"]))
    for what, budget in recorded:
        if budget != max_comparison_steps:
            raise SchemaRefusal(
                f"{subject} records {what} stopped on a {budget!r}-step dissent budget, but "
                f"this run sealed {max_comparison_steps}"
            )
    return self_revision_stopped, sorted(chairs)


def self_revision_for_view(
    view: str, text: str, prior_text: str, measure: Callable[[str, str], list | dict]
) -> list | dict:
    """The fed draft's departures, `[]` for a withheld one, or `measure`'s non-verdict."""
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
    refuse_removed_draft_fed(protocol, subject)
    mode = protocol.get("blind_read") if isinstance(protocol, dict) else None
    if mode not in BLIND_READ_MODES or (mode == "fed") != (expected == "fed"):
        raise SchemaRefusal(f"{subject} names {kind} contrary to its prior-draft protocol")
    if expected == "withheld" and payload.get("self_revision") != []:
        raise SchemaRefusal(
            f"{subject} claims self-revisions against a draft withheld from its reader"
        )
    return expected
