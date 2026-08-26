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
    value = _without_prior(dossier)
    value["testimonia"] = []
    # Unprimed arms may receive neither testimony nor facts derived from it.
    value.pop("act_attachment", None)
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
    """Use one atomic presentation for every requested arm of one logical act.

    ``publish_prior`` must return the closed prior reference and text before
    the establishing call; without a publisher, only the text is retained.
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
        # Retain the prior only on a separate post-call copy because a reader may
        # keep the exact dossier object it was handed.
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
