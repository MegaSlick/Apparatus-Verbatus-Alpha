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
"""

from __future__ import annotations

from typing import Any, Callable, Final


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
