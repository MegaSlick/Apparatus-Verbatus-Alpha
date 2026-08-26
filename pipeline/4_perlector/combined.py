"""One logical Perlector reading over every registered capture.

This small seam owns the clustered-pass shape.  It accepts one atomic
``cross-capture-autopsia.v1`` object, never a sequence of capture jobs, so its
only possible reader calls are full logical-act calls.
"""

from __future__ import annotations

import copy
from typing import Any, Callable

from common.contracts.canonical import digest_of
from common.cross_capture_autopsia import invoke_one_logical_read


def _unprimed(dossier: dict[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(dossier)
    value["testimonia"] = []
    value.pop("prior_draft", None)
    value.pop("prior_draft_view", None)
    # An unprimed instrument (lectio-nuda, lectio-prior) sees no witness-derived
    # fact, not merely no witness list: `act_attachment` is exactly the
    # witness-derived comparison/edge-delta evidence `validate_reading_payload`
    # refuses on an unprimed record, so it leaves with the testimonia it was
    # computed from.
    value.pop("act_attachment", None)
    # `build_dossier` derives `witness_covered` from the testimonia it is handed
    # and omits the key altogether when it is handed none, so the region rows of
    # a dossier that was built once *with* witnesses still say which regions a
    # witness saw. Clearing `testimonia` alone therefore leaves the unprimed
    # instrument holding witness-derived metadata about the very evidence it is
    # defined by not having seen -- the exact fact a pre-push review round
    # already removed once (the `witness_covered` note in
    # `pipeline/orchestrator/test_orchestrator_acceptance.py`'s pin comment).
    # Dropping it here reproduces `build_dossier(..., testimonia=[])` byte for
    # byte, which is what the old per-pass path actually called.
    for region in value.get("regions") or ():
        if isinstance(region, dict):
            region.pop("witness_covered", None)
    return value


def _without_prior(dossier: dict[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(dossier)
    value.pop("prior_draft", None)
    value.pop("prior_draft_view", None)
    return value


def run_logical_passes(
    reader: Any,
    *,
    autopsia: dict[str, Any],
    dossier: dict[str, Any],
    read_bytes: Callable[[str], bytes],
    protocol_config: dict[str, str | int],
    nuda_sampled: bool,
    control_sampled: bool,
    draft_fed: bool = True,
    publish_prior: Callable[[dict[str, Any], Any], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run all requested Perlector arms for one logical act.

    Every arm is assembled from the same atomic presentation.  The only
    difference is the protocol-authorized witness/prior view; image delivery
    is never capture-local and the production ``perlectio`` is one invocation.

    ``publish_prior``, if given, is called with the delivered lectio-prior
    dossier and its reader result immediately after that pass completes, and
    must return the closed ``prior_draft`` object (``{"reference", "text"}``)
    the establishing pass embeds.  This is the seam a caller uses to publish
    the immutable lectio-prior artifact and bind the establishing dossier to
    its real reference, rather than to a bare draft string with nothing to
    point at.  Without it the establishing pass carries only the text, which
    is enough to prove the mechanism in a caller that never publishes.
    """
    max_images = protocol_config.get("max_images")
    if not isinstance(max_images, int) or isinstance(max_images, bool):
        max_images = None
    output: dict[str, Any] = {}
    prior_dossier, _prior_pixels, prior = invoke_one_logical_read(
        reader,
        autopsia=autopsia,
        dossier=_unprimed(dossier),
        read_bytes=read_bytes,
        max_images=max_images,
        pass_kind="lectio-prior",
    )
    output["lectio-prior"] = {"dossier": prior_dossier, "result": prior}
    prior_draft = (
        publish_prior(prior_dossier, prior)
        if publish_prior is not None
        else {"text": prior["text"]}
    )
    if nuda_sampled:
        nuda_dossier, _nuda_pixels, nuda = invoke_one_logical_read(
            reader,
            autopsia=autopsia,
            dossier=_unprimed(dossier),
            read_bytes=read_bytes,
            max_images=max_images,
            pass_kind="lectio-nuda",
        )
        output["lectio-nuda"] = {"dossier": nuda_dossier, "result": nuda}
    if control_sampled:
        control_dossier, _control_pixels, control = invoke_one_logical_read(
            reader,
            autopsia=autopsia,
            dossier=_without_prior(dossier),
            read_bytes=read_bytes,
            max_images=max_images,
            pass_kind="primed-without-prior",
        )
        output["primed-without-prior"] = {"dossier": control_dossier, "result": control}
    establishing = copy.deepcopy(dossier)
    if draft_fed:
        establishing["prior_draft"] = prior_draft
    establishing["prior_draft_view"] = "fed" if draft_fed else "withheld"
    final_dossier, _final_pixels, final = invoke_one_logical_read(
        reader,
        autopsia=autopsia,
        dossier=establishing,
        read_bytes=read_bytes,
        max_images=max_images,
        pass_kind="perlectio",
    )
    if not draft_fed:
        # The prior remains retained evidence for self-revision and prompt
        # reproduction, but withheld means the reader cannot receive its text
        # in a side channel beside the rendered prompt. Build a separate record
        # copy after the synchronous call; never mutate the object the reader
        # was handed (a capturing implementation may retain that reference).
        retained_dossier = copy.deepcopy(final_dossier)
        retained_dossier["prior_draft"] = prior_draft
        if "dossier_digest" in retained_dossier:
            body = {
                key: value for key, value in retained_dossier.items() if key != "dossier_digest"
            }
            retained_dossier["dossier_digest"] = digest_of(body)
        final_dossier = retained_dossier
    output["perlectio"] = {"dossier": final_dossier, "result": final}
    return output
