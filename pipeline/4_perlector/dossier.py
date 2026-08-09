"""The dossier: what the Perlector is actually shown, built deterministically
and persisted as evidence rather than only implied by the reading it produced.

Spec_08's input contract: full-resolution crop(s) by reference, the downscaled
page render with its transform, and every Testimonium verbatim with **witness
identity and factual context only** -- model name, resolved provenance, and its
training domain stated as fact, "never pipeline-asserted worth... no
reliability scores, no error rates, no 'usually better,' no ordering by
quality" (Tyrel's ruling, 2026-07-30). No numeric trust weights, no preferred
order, no primary flag: presentation order is deterministic (sorted for
reproducible bytes) but carries no meaning, and `test_dossier_shuffle_invariance`
asserts building the same dossier from a shuffled testimonia list produces
identical bytes.

**Under the blinded regime, the dossier shows a pseudonym and nothing that
would unblind it.** A resolved model repo/revision is exactly what blinding
exists to hide, so it never appears here regardless of regime -- it already
travels on the Testimonium's own provenance, one step away, for whoever is
allowed to look. Training domain is kept under both regimes: it is a fact about
what the witness was trained on, not an identifying credential, and spec_08
asks for it explicitly.
"""

from __future__ import annotations

import tomllib
from io import BytesIO
from pathlib import Path
from typing import Any, Final

from PIL import Image
from regime import NAMED, witness_label

from common.contracts.canonical import digest_of
from common.contracts.errors import ContractError
from common.contracts.identities import artifact_id
from common.contracts.stages import EXEMPLAR, PERLECTOR
from common.stage import WITNESS_READING_OUTCOMES

DEFAULT_WITNESS_CONTEXT_PATH: Final = (
    Path(__file__).resolve().parents[2] / "config" / "witness_context.toml"
)

# A fixed factor, not configuration: the dossier's job is to hand the reader a
# genuine layout overview, not a second full-resolution copy of the page.
PAGE_RENDER_DOWNSCALE_FACTOR: Final = 2

_FORBIDDEN_KEY_FRAGMENTS: Final = ("primary", "preferred", "order", "rank", "trust", "weight")


def load_witness_context(path: Path = DEFAULT_WITNESS_CONTEXT_PATH) -> dict[str, dict[str, str]]:
    """Read the Perlector-owned factual-context declaration. Data, never code."""
    try:
        with open(path, "rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ContractError(
            f"witness context declaration at {path} could not be read: {error}"
        ) from error
    for chair, entry in raw.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("training_domain"), str):
            raise ContractError(
                f"witness context entry for {chair!r} has no training_domain string"
            )
    return raw


def _downscale_page(page_bytes: bytes, *, factor: int) -> tuple[bytes, int, int]:
    """A genuine downscale of the sealed page, deterministic for a fixed input.

    Box filtering (an honest area-average, not a single-pixel sample) so the
    render is a real layout overview rather than a decorative resize.
    """
    with Image.open(BytesIO(page_bytes)) as image:
        image.load()
        width = max(1, image.width // factor)
        height = max(1, image.height // factor)
        downscaled = image.resize((width, height), Image.BOX)
        output = BytesIO()
        downscaled.save(output, format="PNG", optimize=False, compress_level=9)
        return output.getvalue(), width, height


def build_page_render(context, *, source_page_id: str, source_page_ordinal: int) -> dict[str, Any]:
    """The downscaled page render for one act's page, with its transform recorded
    (ARCHITECTURE invariant 3: the exact image shown is reproducible from the
    Exemplar plus the recorded transforms)."""
    page = context.tree.read_artifact(
        EXEMPLAR, "page", artifact_id(EXEMPLAR, "page", source_page_id)
    )
    page_bytes = context.tree.read_bytes(page["payload"]["image_path"])
    downscaled, width, height = _downscale_page(page_bytes, factor=PAGE_RENDER_DOWNSCALE_FACTOR)
    digest, published = context.tree.put_blob(PERLECTOR, downscaled)
    return {
        "source_page_id": source_page_id,
        "source_page_ordinal": source_page_ordinal,
        "image_path": published.relative_path,
        "image_sha256": digest,
        "transform": {"operation": "downscale", "factor": PAGE_RENDER_DOWNSCALE_FACTOR},
        "width": width,
        "height": height,
    }


def _testimonium_entry(
    record: dict[str, Any],
    *,
    witness_context: dict[str, dict[str, str]],
    regime: str,
    run_id: str,
    config_digest: str,
) -> dict[str, Any]:
    chair = record["payload"]["chair"]
    context_entry = witness_context.get(chair)
    if context_entry is None:
        raise ContractError(
            f"chair {chair!r} has no declared entry in config/witness_context.toml; every "
            "configured witness must carry a factual dossier context, or none is described"
        )
    reported = (
        record["payload"].get("reported") if record["outcome"] in WITNESS_READING_OUTCOMES else None
    )
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
    witness_context: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Assemble one act's dossier. Deterministic: the same evidence in any order
    produces identical bytes, and nothing in the result may express a
    preference among the testimonia it carries.

    `page_renders` is unaffected by `testimonia` -- Lectio nuda withholds
    testimony, never sight. The Perlector sees the same crops and the same
    page context whether or not it is shown any witness.
    """
    declared_context = witness_context if witness_context is not None else load_witness_context()
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
                witness_context=declared_context,
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
