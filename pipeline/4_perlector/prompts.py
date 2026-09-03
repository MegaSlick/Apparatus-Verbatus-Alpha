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


def _neutral_dossier_lines(
    chair_role: str, dossier: dict[str, Any], protocol_config: dict[str, str]
) -> list[str]:
    """The structure every registered Perlector template shares: testimonia
    presented as labelled clues, the sealed neutral fragment around a fed
    prior draft, and the role and act key that close the shared shape. A
    builder differs from this only in what, if anything, it appends after it
    -- never in how this part is rendered, so two chairs read through the
    same shared prefix stay comparable (invariant #49).
    """
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
    return lines


def _fake_perlector_v0(
    chair_role: str, dossier: dict[str, Any], protocol_config: dict[str, str]
) -> str:
    """The declared byte template for the walking skeleton's fixture recipe."""
    return "\n".join(_neutral_dossier_lines(chair_role, dossier, protocol_config))


# The one pinned transcription instruction for `unproven-real-perlector`
# (`config/models-real.toml`), the first recipe this module renders for a
# real serving engine rather than a fixture. Per GOVERNANCE 3 ("the Perlector
# never picks") and GOVERNANCE 10 ("the instrument may not constrain what it
# measures"): it names no witness preference, sets no severity or confidence
# floor, and says nothing about which way to argue. It asks for the ink
# transcribed as written, unmodernized, through to the end -- nothing else.
# Code, covered by `builder_sha256` like every builder in this module;
# reworded only as a reviewed two-file change with `config/README.md`'s R5a
# register, the same rule already governing the Pass-B fragment.
TRANSCRIPTION_INSTRUCTION: Final = (
    "Transcribe the ink exactly as it is written on the page. Do not modernize spelling, "
    "expand abbreviations, or correct the scribe. Read through to the end of the act."
)


def _unproven_real_perlector_v0(
    chair_role: str, dossier: dict[str, Any], protocol_config: dict[str, str]
) -> str:
    """`unproven-real-perlector`'s declared template (`config/models-real.toml:18`).

    The same neutral structure `_fake_perlector_v0` renders, plus the one
    pinned transcription instruction above, appended last so it is never the
    reader's first framing and never sits ahead of the neutral Pass-B fragment
    (GOVERNANCE 3's "no picker" holds over prompt bytes, not only the
    dossier).

    Reads only fields a delivered dossier and its retained twin carry
    identically: `witness_regime`; each `testimonia[*]` row's
    `witness_label`/`training_domain`/`reported`/`model_name`/
    `resolved_provenance`; `prior_draft`/`prior_draft_view`; `act_key`. Never
    `dossier_digest`, `cross_capture_autopsia`, or `logical_act_id` -- fields
    sealed onto the retained dossier that a delivered one does not carry
    identically, so a builder that read them would render bytes the
    prompt-fidelity test (`test_prompt_fidelity.py`) could never reproduce
    from a delivered dossier and a retained one alike.
    """
    return "\n".join(
        [
            *_neutral_dossier_lines(chair_role, dossier, protocol_config),
            TRANSCRIPTION_INSTRUCTION,
        ]
    )


_BUILDERS: Final[dict[str, Callable[[str, dict[str, Any], dict[str, str]], str]]] = {
    "fake-perlector-v0": _fake_perlector_v0,
    "unproven-real-perlector": _unproven_real_perlector_v0,
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
