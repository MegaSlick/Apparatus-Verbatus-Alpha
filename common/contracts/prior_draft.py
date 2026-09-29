"""The recorded relationship between a prior draft and its establishing reading."""

from typing import Any, Callable

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
    refuse_removed_draft_fed(protocol, subject)
    mode = protocol.get("blind_read") if isinstance(protocol, dict) else None
    if mode not in BLIND_READ_MODES or (mode == "fed") != (expected == "fed"):
        raise SchemaRefusal(f"{subject} names {kind} contrary to its prior-draft protocol")
    if expected == "withheld" and payload.get("self_revision") != []:
        raise SchemaRefusal(
            f"{subject} claims self-revisions against a draft withheld from its reader"
        )
    return expected
