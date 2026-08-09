"""Prompt fidelity (invariant #49): the serving path builds each seat's declared
prompt format byte-for-byte. A fine-tuned candidate misread through the wrong
prompt would be measured as a failure of the model rather than of the harness
-- so this is a registry keyed by `ChairIdentity.serving_recipe`, and an
unrecognized recipe refuses outright rather than falling back to some default
template. That refusal is the load-bearing behaviour: a silent fallback is
exactly the failure mode invariant #49 exists to prevent, and it is cheaper to
build the refusal now than to discover the fallback later on a real seat.

Real per-seat byte fidelity against an actual chat template needs the real
tokenizer/template files for whichever model finally sits in the chair, which
this offline, no-model chamber does not fetch. What this module proves is the
mechanism: a declared, tested builder per recipe, and a closed refusal for
every recipe that has none yet.

**And the prompt that was actually built is recorded on the reading.** A
builder nothing calls proves only that a builder exists; invariant #49 is
about the prompt a *reading* was produced through, so `prompt_evidence` binds
the rendered bytes, the recipe, and the resolved identity that supplied the
recipe into one record the Perlectio carries. That is what lets a later
comparison of two checkpoints establish that they were prompted the same way,
rather than assume it.
"""

from __future__ import annotations

from typing import Any, Callable, Final

from common.chairs.models import ChairIdentity
from common.contracts.canonical import digest_bytes, digest_of


def _fake_perlector_v0(chair_role: str, dossier: dict[str, Any]) -> str:
    """The declared byte template for the walking skeleton's fixture recipe."""
    lines = [
        f"role: {chair_role}",
        f"act: {dossier['act_key']}",
        f"witness_regime: {dossier['witness_regime']}",
        "testimonia:",
    ]
    for testimonium in dossier["testimonia"]:
        lines.append(
            f"  - {testimonium['witness_label']} "
            f"({testimonium['training_domain']}): {testimonium['reported']!r}"
        )
    return "\n".join(lines)


_BUILDERS: Final[dict[str, Callable[[str, dict[str, Any]], str]]] = {
    "fake-perlector-v0": _fake_perlector_v0,
}


def build_prompt(serving_recipe: str, chair_role: str, dossier: dict[str, Any]) -> str:
    """Build one chair's declared prompt, byte-exact, or refuse by name."""
    builder = _BUILDERS.get(serving_recipe)
    if builder is None:
        raise ValueError(
            f"no declared prompt builder is registered for serving recipe {serving_recipe!r}; "
            "a chair with no registered builder is never silently served a default template"
        )
    return builder(chair_role, dossier)


def prompt_evidence(chair: ChairIdentity, dossier: dict[str, Any]) -> dict[str, str]:
    """The record of the prompt one reading was actually produced through.

    Keyed on the chair's *resolved* serving recipe rather than on its role: a
    role is a chair, and a chair can be occupied by a stock model, a vendor
    model, a local checkpoint or an unmerged adapter in turn. Two occupants of
    the same role prompted through the same template because nobody noticed
    they were different models is exactly the harness failure invariant #49
    exists to make visible, so the identity digest travels beside the recipe
    and a reader comparing two Perlectiones can see whether they were prompted
    the same way.

    The rendered bytes are recorded by digest, not verbatim: they contain every
    testimonium the reader was shown, which already travels once on the
    Perlectio's own `dossier`, and a second verbatim copy is a second thing to
    drift.
    """
    rendered = build_prompt(chair.serving_recipe, chair.role, dossier)
    return {
        "serving_recipe": chair.serving_recipe,
        "chair_identity_sha256": digest_of(chair.to_record()),
        "dossier_digest": dossier["dossier_digest"],
        "rendered_sha256": digest_bytes(rendered.encode("utf-8")),
    }
