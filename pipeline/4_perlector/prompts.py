"""Prompt fidelity -- spec 08's invariant #49, whose text is carried here in
full because the numbered spec list lives outside the repository: "the serving
path builds each seat's declared prompt format byte-for-byte (a fine-tuned
candidate misread through the wrong prompt would be measured as a failure of
the model rather than of the harness). Tested per seat."

The serving path builds each chair's declared prompt format byte-for-byte. A
fine-tuned candidate misread through the wrong prompt would be measured as a
failure of the model rather than of the harness --
so this is a registry keyed by `ChairIdentity.serving_recipe`, and a recipe with
no registered builder refuses outright. The silent fallback to some other
chair's template is the failure invariant #49 exists to prevent.

Real byte fidelity against an actual chat template needs the tokenizer and
template files for whichever model finally sits in the chair, which this
offline chamber does not fetch. What is proved here is the mechanism: a
declared, tested builder per recipe, and a closed refusal for every recipe that
has none yet.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Final

from common.chairs.models import ChairIdentity
from common.contracts.canonical import canonical_text, digest_bytes, digest_of

# The whole module's bytes, read once at import. A deployment without source
# files still needs them for this line — but it fails loudly when the module
# loads, before any act is read, rather than per prompt mid-run as
# `inspect.getsource` would.
_MODULE_SOURCE_DIGEST: Final[str] = digest_bytes(Path(__file__).resolve().read_bytes())
_DEFAULT_PROTOCOL: Final = {
    "page_shared_prefix_policy": "page-shared-prefix-first.v1",
    "pass_b_fragment": "",
}


def _fake_perlector_v0(
    chair_role: str, dossier: dict[str, Any], protocol_config: dict[str, str]
) -> str:
    """The declared byte template for the walking skeleton's fixture recipe."""
    lines = [
        f"page_shared_prefix_policy: {protocol_config['page_shared_prefix_policy']}",
        f"witness_regime: {dossier['witness_regime']}",
        "testimonia:",
    ]
    for testimonium in dossier["testimonia"]:
        lines.append(
            f"  - {testimonium['witness_label']} "
            f"({testimonium['training_domain']}): {testimonium['reported']!r}; "
            f"model={testimonium['model_name']!r}; "
            f"provenance={canonical_text(testimonium['resolved_provenance'])}"
        )
    if dossier.get("prior_draft_view") == "fed":
        lines.extend(
            [
                "prior_draft:",
                dossier["prior_draft"]["text"],
                protocol_config["pass_b_fragment"],
            ]
        )
    lines.extend((f"role: {chair_role}", f"act: {dossier['act_key']}"))
    return "\n".join(lines)


_BUILDERS: Final[dict[str, Callable[[str, dict[str, Any], dict[str, str]], str]]] = {
    "fake-perlector-v0": _fake_perlector_v0,
}


def _builder_for(serving_recipe: str) -> Callable[[str, dict[str, Any], dict[str, str]], str]:
    builder = _BUILDERS.get(serving_recipe)
    if builder is None:
        raise ValueError(
            f"no declared prompt builder is registered for serving recipe {serving_recipe!r}; "
            "a chair with no registered builder is never silently served a default template"
        )
    return builder


def build_prompt(
    serving_recipe: str,
    chair_role: str,
    dossier: dict[str, Any],
    protocol_config: dict[str, str] | None = None,
) -> str:
    """Build one chair's declared prompt, byte-exact, or refuse by name."""
    return _builder_for(serving_recipe)(chair_role, dossier, protocol_config or _DEFAULT_PROTOCOL)


def prompt_evidence(
    chair: ChairIdentity,
    dossier: dict[str, Any],
    protocol_config: dict[str, str] | None = None,
    protocol_sha256: str = "unsealed-test",
) -> dict[str, str]:
    """The record of the prompt one reading was actually produced through.

    A builder nothing calls proves only that a builder exists; invariant #49 is
    about the prompt a *reading* came out of, which is why this record is
    carried on the Perlectio rather than merely tested.

    Keyed on the chair's *resolved* recipe, never on its role: a role can be
    occupied by a stock model, a vendor model, a local checkpoint or an unmerged
    adapter in turn, and two occupants prompted through one template because
    nobody noticed they were different models is the harness failure #49 makes
    visible. The identity digest travels beside the recipe so two Perlectiones
    can be compared for whether they were prompted alike rather than assumed to
    have been.

    The rendered bytes are recorded by digest, not verbatim: they contain every
    testimonium the reader was shown, which already travels once on the
    Perlectio's own `dossier`, and a second copy is a second thing to drift.

    D-7: the recipe name pins *which* template a reading was produced through
    only by convention -- nothing bound the builder's own bytes into the
    record, so reproducing `rendered_sha256` later needs the builder at the
    exact revision that ran, and the record itself could not say whether that
    revision had moved. `builder_sha256` closes that: a digest of this whole
    module's source — not one function's, because a builder renders through
    helpers, and an edited helper changes the rendered bytes just as surely as
    an edited builder — so any edit to the prompt-building code changes this
    record's own claim about itself rather than silently invalidating an old
    one nothing can detect.
    """
    builder = _builder_for(chair.serving_recipe)
    protocol_config = protocol_config or _DEFAULT_PROTOCOL
    rendered = builder(chair.role, dossier, protocol_config)
    return {
        "serving_recipe": chair.serving_recipe,
        "chair_identity_sha256": digest_of(chair.to_record()),
        "dossier_digest": dossier["dossier_digest"],
        "rendered_sha256": digest_bytes(rendered.encode("utf-8")),
        "builder_sha256": _MODULE_SOURCE_DIGEST,
        "protocol_sha256": protocol_sha256,
        "page_shared_prefix_policy": protocol_config["page_shared_prefix_policy"],
    }
