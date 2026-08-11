"""The dossier: what the Perlector is actually shown, built deterministically
and persisted as evidence rather than only implied by the reading it produced.

Spec_08's input contract: full-resolution crop(s) by reference, the downscaled
page render with its transform, and every Testimonium verbatim with **witness
identity and factual context only** -- model name, resolved provenance, and its
training domain stated as fact, "never pipeline-asserted worth... no
reliability scores, no error rates, no 'usually better,' no ordering by
quality" (Tyrel's ruling, 2026-07-30). No numeric trust weights, no preferred
order, no primary flag: presentation order is deterministic, because the bytes
must reproduce, but it carries no meaning and no reader may take one from it.
"""

from __future__ import annotations

import copy
import tomllib
from io import BytesIO
from pathlib import Path
from typing import Any, Final

from PIL import Image
from regime import NAMED, REGIMES, witness_label

from common.chairs.models import ChairIdentity
from common.contracts.canonical import digest_of
from common.contracts.errors import ContractError, SchemaRefusal
from common.contracts.identities import artifact_id
from common.contracts.stages import EXEMPLAR, PERLECTOR
from common.imaging import crop_png, dimensions
from common.stage import WITNESS_READING_OUTCOMES

# A fixed bound, not configuration: the dossier's job is to hand the reader a
# genuine layout overview, not a second full-resolution copy of the page. A
# *bound* rather than a divisor because a divisor is not a bound -- halving a
# 6000-pixel archival scan still hands the reader a 3000-pixel image, which is
# not page context, it is the page again at some cost. Capping the long edge
# gives every page the same kind of overview whatever it was scanned at.
PAGE_CONTEXT_MAX_EDGE: Final = 1024

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
)


def load_witness_context(path: Path) -> dict[str, dict[str, str]]:
    """Read the Perlector-owned factual-context declaration. Data, never code.

    The path is always supplied, never defaulted: the run seals the digest of
    one declaration file into `config_digest`, and a module-local default would
    be a second answer to "which file" that could quietly build a dossier from
    bytes the run was not sealed under.
    """
    try:
        with open(path, "rb") as handle:
            raw = tomllib.load(handle)
    # `UnicodeDecodeError` beside the others because `tomllib` decodes the bytes
    # itself: a declaration file that is not valid UTF-8 raises it rather than
    # `TOMLDecodeError`, and it escaped this handler as a raw traceback where
    # every other malformed-file case is a named refusal. `common/stage.py`
    # already catches it for the same reason. Found by CodeRabbit.
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ContractError(
            f"witness context declaration at {path} could not be read: {error}"
        ) from error
    for chair, entry in raw.items():
        if (
            not isinstance(chair, str)
            or not chair
            or not isinstance(entry, dict)
            or set(entry) != {"training_domain"}
            or not isinstance(entry.get("training_domain"), str)
            or not entry["training_domain"].strip()
        ):
            raise ContractError(
                f"witness context entry for {chair!r} is not the closed, non-blank "
                "training_domain record"
            )
    return raw


def _downscale_page(page_bytes: bytes, *, maximum_edge: int) -> tuple[bytes, dict[str, Any]]:
    """A genuine downscale of the sealed page, deterministic for a fixed input.

    The sealed page goes through `common.imaging.crop_png` first, at its own
    full bounds, so this render inherits the one decode-and-display policy the
    rest of the pipeline uses -- including the high-precision handling that
    stops a 16-bit scan rendering as near-white. Pillow then does the resize
    with a named resampler.

    The transform record names the source size, the target size and the
    resampler rather than only a factor, because ARCHITECTURE invariant 3 asks
    that the exact image shown be reproducible from the Exemplar plus the
    recorded transforms -- and "downscaled by 2" is only reproducible by
    someone who also has this function. A page already inside the bound is
    recorded as `identity` rather than silently resampled to itself.
    """
    width, height = dimensions(page_bytes)
    display = crop_png(page_bytes, {"x": 0, "y": 0, "w": width, "h": height})
    with Image.open(BytesIO(display)) as image:
        image.load()
        if max(image.width, image.height) <= maximum_edge:
            rendered = image.copy()
            resampler = "identity"
        else:
            scale = maximum_edge / max(image.width, image.height)
            rendered = image.resize(
                (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
                resample=Image.Resampling.LANCZOS,
            )
            resampler = "pillow-lanczos"
        output = BytesIO()
        rendered.save(output, format="PNG", optimize=False, compress_level=9)
        return output.getvalue(), {
            "operation": "downscale-for-page-context",
            "source_dimensions": {"w": width, "h": height},
            "target_dimensions": {"w": rendered.width, "h": rendered.height},
            "maximum_edge": maximum_edge,
            "resampler": resampler,
        }


def build_page_render(context, *, source_page_id: str, source_page_ordinal: int) -> dict[str, Any]:
    """The downscaled page render for one act's page, with its transform recorded
    (ARCHITECTURE invariant 3: the exact image shown is reproducible from the
    Exemplar plus the recorded transforms)."""
    page = context.tree.read_artifact(
        EXEMPLAR, "page", artifact_id(EXEMPLAR, "page", source_page_id)
    )
    source_path = page["payload"].get("image_path")
    if not isinstance(source_path, str) or not source_path:
        raise SchemaRefusal("a sealed Exemplar page carries no image to render page context from")
    page_bytes = context.tree.read_bytes(source_path)
    try:
        downscaled, transform = _downscale_page(page_bytes, maximum_edge=PAGE_CONTEXT_MAX_EDGE)
    except (OSError, ValueError) as error:
        raise SchemaRefusal(
            "a sealed Exemplar page could not be rendered as Perlector page context"
        ) from error
    digest, published = context.tree.put_blob(PERLECTOR, downscaled)
    return {
        "source_page_id": source_page_id,
        "source_page_ordinal": source_page_ordinal,
        # The sealed page this render was derived from, named so the derivation
        # can be checked rather than believed.
        "source": context.input_ref(source_path),
        "image_path": published.relative_path,
        "image_sha256": digest,
        "transform": transform,
    }


def _testimonium_entry(
    record: dict[str, Any],
    *,
    witness_context: dict[str, dict[str, str]],
    witness_context_path: Path | str,
    regime: str,
    run_id: str,
    config_digest: str,
) -> dict[str, Any]:
    chair = record["payload"]["chair"]
    context_entry = witness_context.get(chair)
    if context_entry is None:
        raise ContractError(
            f"chair {chair!r} has no declared entry in {witness_context_path}; every "
            "configured witness must carry a factual dossier context, or none is described"
        )
    reported = (
        record["payload"].get("reported") if record["outcome"] in WITNESS_READING_OUTCOMES else None
    )
    provenance = record["payload"].get("provenance")
    if not isinstance(provenance, dict):
        raise SchemaRefusal(f"Testimonium from {chair!r} has no resolved provenance")
    identity = provenance.get("resolved_identity")
    if identity is not None and not isinstance(identity, dict):
        raise SchemaRefusal(f"Testimonium from {chair!r} has malformed resolved identity")
    model_name = None
    if identity is not None:
        # `ChairIdentity.source_reference` is the one declared rule for which
        # field names a chair's source, and this display name follows it rather
        # than re-deriving the huggingface/local split. Safe to reconstruct:
        # `validate_serving_provenance` (in `testimonia_of`, before any record
        # reaches this module) already required a configured chair's
        # `resolved_identity` to satisfy `ChairIdentity(**record)`.
        try:
            model_name = ChairIdentity(**identity).source_reference
        except TypeError as error:
            raise SchemaRefusal(
                f"Testimonium from {chair!r} has malformed resolved identity"
            ) from error
        if not model_name:
            raise SchemaRefusal(f"Testimonium from {chair!r} has no resolved model name")
    # Under `blinded`, the training-domain fact is withheld along with the
    # chair's real name. A domain description ("a reader fine-tuned on French
    # parish records") can identify a witness as surely as its name would --
    # the whole point of blinding is that the reader cannot learn which
    # witness is which, and a factual sentence that only one configured chair
    # could truthfully carry is exactly that leak. `None` here, not an
    # anonymized paraphrase: inventing a vaguer-but-still-informative
    # description would only move the leak, not close it.
    training_domain = context_entry["training_domain"] if regime == NAMED else None
    return {
        "witness_label": witness_label(
            chair, regime=regime, run_id=run_id, config_digest=config_digest
        ),
        # Named dossiers carry the model fact and the exact provenance that was
        # validated at the input boundary.  Both are withheld together when
        # blinded: either one would reverse the pseudonym inside the dossier.
        "model_name": model_name if regime == NAMED else None,
        "resolved_provenance": copy.deepcopy(provenance) if regime == NAMED else None,
        "training_domain": training_domain,
        "outcome": record["outcome"],
        "reported": reported,
    }


def build_dossier(
    context,
    *,
    act_id: str,
    act_key: str,
    regions: list[dict[str, Any]],
    testimonia: list[dict[str, Any]],
    witnessed_region_ids: set[str],
    regime: str,
    page_renders: list[dict[str, Any]],
    witness_context: dict[str, dict[str, str]],
) -> dict[str, Any]:
    """Assemble one act's dossier. Deterministic: the same evidence in any order
    produces identical bytes, and nothing in the result may express a
    preference among the testimonia it carries.

    `page_renders` is unaffected by `testimonia` -- Lectio nuda withholds
    testimony, never sight. The Perlector sees the same crops and the same
    page context whether or not it is shown any witness.
    """
    if regime not in REGIMES:
        raise SchemaRefusal(f"witness regime {regime!r} is not one of {sorted(REGIMES)}")
    region_rows = sorted(
        (
            {
                "region_id": region["region_id"],
                "image_path": region["image_path"],
                "image_sha256": region["image_sha256"],
                "witness_covered": region["region_id"] in witnessed_region_ids,
            }
            for region in regions
        ),
        key=lambda row: row["region_id"],
    )
    page_render_rows = sorted(page_renders, key=lambda row: row["source_page_id"])
    testimonia_rows = sorted(
        (
            _testimonium_entry(
                record,
                witness_context=witness_context,
                witness_context_path=context.witness_context_config_path,
                regime=regime,
                run_id=context.tree.run_id,
                config_digest=context.config_digest,
            )
            for record in testimonia
        ),
        key=lambda row: row["witness_label"],
    )
    dossier = {
        "act_id": act_id,
        "act_key": act_key,
        "witness_regime": regime,
        "regions": region_rows,
        "page_renders": page_render_rows,
        "testimonia": testimonia_rows,
    }
    # Swept before the digest is taken: a preference-bearing field sealed into
    # the digest is already in the record by the time anyone could object. This
    # is the guard standing over GOVERNANCE 3, so it runs on the production path
    # and not only in the tests.
    assert_no_order_bearing_field(dossier)
    dossier["dossier_digest"] = digest_of(dossier)
    return dossier


def assert_no_order_bearing_field(value: Any, path: str = "$") -> None:
    """A durable sweep: no key anywhere in a dossier may name a preference.

    Cheap enough to run on every dossier this build produces, so a future edit
    that reintroduces a trust/order/preferred field is caught immediately
    rather than argued about at review.
    """
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = key.lower()
            if any(fragment in lowered for fragment in _FORBIDDEN_KEY_FRAGMENTS):
                raise ContractError(
                    f"{path}.{key} names a preference among witnesses; GOVERNANCE 3 forbids "
                    "any order-bearing or trust-bearing field in a dossier"
                )
            assert_no_order_bearing_field(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            assert_no_order_bearing_field(item, f"{path}[{index}]")
