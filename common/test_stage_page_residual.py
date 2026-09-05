"""The page-residual act class, and the consumer verifier that recomputes it.

A page whose conservation reconciles more unclaimed components than the sealed
bound allows is held as **one** review item rather than as that many held acts,
and its components are then counted rather than listed. That is a real cost —
the per-component rectangles leave the artifact — so every part of the claim is
checked here against something other than the producer's word: the page
rectangle against the sealed page bytes, the identity against the reserved
``page-residual`` class, and the premise against the page's own `conservation`
record, reached through the digest-checked input hop rather than by address.

The run trees are real to the Exemplar's own seal — the Door and the Exemplar
run as programs over the synthetic fixture — and the Designator's records are
then hand-built on top. That split is deliberate. `verify_sealed_page_pixels` is
the whole reason the rectangle cannot be forged, so a stubbed page would prove
nothing; the Designator's own publication of these records is unit D's, and on
this branch its `run.py` does not yet resolve its grouping configuration at all.
Hand-building the records is therefore not a shortcut around a producer, it is
the only way to hold the *consumer* to its contract before the producer exists.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from common.chairs.registry import ChairRegistry
from common.contracts.errors import FatalAccounting
from common.contracts.identities import act_id as derive_act_id
from common.contracts.stages import DESIGNATOR, EXEMPLAR
from common.imaging import dimensions
from common.runtree.store import RunTree
from common.stage import (
    RESIDUAL_ENUMERATION_COMPLETE,
    RESIDUAL_ENUMERATION_WITHHELD,
    StageContext,
    _verify_every_conservation_residual_is_accounted,
    _verify_minted_act_rows,
    adapter_recipe_for,
    fallback_page_act_key,
    page_residual_act_key,
    run_sealed_config_digests,
)

ROOT = Path(__file__).resolve().parents[1]
ORCHESTRATOR = ROOT / "pipeline" / "orchestrator" / "run.py"
MODELS_CONFIG = ROOT / "config" / "models.toml"
RUN_ID = "page-residual-unit"
ORDINAL = 1
BOUND = 2000
MEASURED = 2500

# Sentinel default for `_Page.hold_page`'s `grouping_config_sha256` parameter: it
# means "use the digest this run actually sealed", resolved against the page's
# own context rather than fixed at import time, since the value comes from a
# real run tree built per test. `None` is not reused for this because `None`
# already means "omit the field" for the tests that exercise that refusal.
_SEALED_GROUPING_DIGEST = object()


@pytest.fixture(scope="module")
def sealed_template(tmp_path_factory):
    """One real Door-and-Exemplar run, sealed once and copied per test.

    Stopping at the Exemplar is not an economy: the Designator is the stage under
    discussion, and letting it run would mean checking these records against
    records it wrote, which is the circularity the whole file exists to avoid.
    """
    root = tmp_path_factory.mktemp("page-residual-template") / "runs"
    result = subprocess.run(
        [
            sys.executable,
            str(ORCHESTRATOR),
            "--fixture",
            "synthetic-two-page-v0",
            "--scenario",
            "happy",
            "--run-root",
            str(root),
            "--run-id",
            RUN_ID,
            "--from",
            "door",
            "--to",
            "exemplar",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return root


@pytest.fixture
def sealed(sealed_template, tmp_path):
    """A private copy of the sealed run, so each test may publish into it."""
    root = tmp_path / "runs"
    shutil.copytree(sealed_template, root)
    return RunTree(root, RUN_ID)


def _context(tree: RunTree) -> StageContext:
    run = tree.read_run()
    return StageContext(
        tree=tree,
        run=run,
        fixture={},
        scenario="happy",
        stage=DESIGNATOR,
        adapter_revision=adapter_recipe_for(run, DESIGNATOR),
        args=None,
        registry=ChairRegistry.from_toml(str(MODELS_CONFIG)),
    )


def _sealed_page(tree: RunTree, ordinal: int = ORDINAL) -> dict:
    return next(
        record
        for record in (
            tree.read_artifact(EXEMPLAR, "page", entry["artifact_id"])
            for entry in tree.build_manifest(EXEMPLAR)["artifacts"]
            if entry["kind"] == "page"
        )
        if record["payload"]["ordinal"] == ordinal and record["outcome"] == "sealed"
    )


def _page_rectangle(tree: RunTree, page: dict) -> dict:
    width, height = dimensions(tree.read_bytes(page["payload"]["image_path"]))
    return {"x": 0, "y": 0, "w": width, "h": height}


def _conservation_payload(
    *,
    ordinal: int = ORDINAL,
    enumeration: str = RESIDUAL_ENUMERATION_WITHHELD,
    count: int | None = MEASURED,
    bound: int | None = BOUND,
    components: list[dict] | None = None,
) -> dict:
    """The shape §1 of the spec gives the conservation record.

    `residual_components` is passed as ``None`` to omit the key, never to empty
    it: an empty list is the claim that this page had no unclaimed ink at all,
    which is the opposite of what a withheld record says.
    """
    payload = {
        "page_ordinal": ordinal,
        "background_source": "page-modal",
        "background_value": 255,
        "ink_measurable": True,
        # What this reconciliation ran on and under, as the producer publishes
        # it. Nothing in this verifier reads these; a double that dropped them
        # would still be testing a shape no producer emits.
        "page_width": 8,
        "page_height": 8,
        "reconciliation_thresholds": {
            "gap_tolerance_px": 3,
            "review_priority_min_dimension_px": 6,
        },
        "reason": None,
        "total_ink_pixel_count": 40,
        "claimed_pixel_count": 0,
        "residual_pixel_count": 40,
        "residual_component_count": count,
        "residual_ink_fraction_bp": 12,
        "max_residual_components": bound,
        "residual_enumeration": enumeration,
    }
    if components is not None:
        payload["residual_components"] = components
    return payload


def _row(act: str, key: str, page_id: str, outcome: str, evidence: list[dict]) -> dict:
    return {
        "act_id": act,
        "act_key": key,
        "page_id": page_id,
        "page_ordinal": ORDINAL,
        "has_continuation": False,
        "outcome": outcome,
        "evidence": evidence,
    }


class _Page:
    """One page's sealed facts plus the Designator records built over it."""

    def __init__(self, tree: RunTree):
        self.tree = tree
        self.context = _context(tree)
        self.record = _sealed_page(tree)
        self.page_id = self.record["subject_id"]
        self.rectangle = _page_rectangle(tree, self.record)
        self.conservation_ref: dict[str, str] | None = None
        self.rows: dict[str, dict] = {}

    def publish_conservation(self, payload: dict, *, outcome: str = "held") -> None:
        published = self.context.publish(
            kind="conservation",
            subject_id=self.page_id,
            outcome=outcome,
            inputs=[self.context.input_ref(self.record["payload"]["image_path"])],
            payload=payload,
        )
        self.conservation_ref = self.context.input_ref(published.relative_path)

    def hold_page(
        self,
        *,
        bounds: dict | None = None,
        act: str | None = None,
        act_key: str | None = None,
        count: int | None = MEASURED,
        bound: int | None = BOUND,
        grouping_config_sha256: str | None | object = _SEALED_GROUPING_DIGEST,
        extra: dict | None = None,
        inputs: list[dict] | None = None,
    ) -> str:
        """Mint the page-residual hold the spec's §1 describes.

        `grouping_config_sha256` defaults to the sentinel `_SEALED_GROUPING_DIGEST`,
        replaced below with the digest this run actually sealed at binding time —
        the value the consumer verifier checks the hold against. Pass `None` to
        omit the field, or a foreign digest to prove the consumer refuses one.
        """
        bounds = self.rectangle if bounds is None else bounds
        act = derive_act_id(self.page_id, "page-residual", bounds) if act is None else act
        key = page_residual_act_key(ORDINAL) if act_key is None else act_key
        if grouping_config_sha256 is _SEALED_GROUPING_DIGEST:
            grouping_config_sha256 = run_sealed_config_digests(self.context.run)[
                "designator-grouping"
            ]
        payload = {
            "act_key": key,
            "page_id": self.page_id,
            "page_ordinal": ORDINAL,
            "page_bounds": bounds,
            "residual_component_count": count,
            "max_residual_components": bound,
            "blocking_page_ordinal": ORDINAL,
            # Spelled out rather than imported from `common.stage`: this string
            # is what a consumer branches on, and the one place it is written
            # independently of the constant both producer and verifier share.
            "reason_code": "residual-components-over-page-bound",
            "reason": (
                "this page's conservation reconciled more residual components than the sealed "
                "bound allows to be minted separately, so the page is held as one review item"
            ),
            **(
                {"grouping_config_sha256": grouping_config_sha256}
                if grouping_config_sha256 is not None
                else {}
            ),
            **(extra or {}),
        }
        if inputs is None:
            inputs = [self.conservation_ref] if self.conservation_ref is not None else None
        published = self.context.publish(
            kind="hold",
            subject_id=act,
            outcome="held",
            inputs=inputs,
            payload=payload,
        )
        self.rows[act] = _row(
            act, key, self.page_id, "held", [self.context.input_ref(published.relative_path)]
        )
        return act

    def hold_component(self, bounds: dict) -> str:
        act = derive_act_id(self.page_id, "residual", bounds)
        published = self.context.publish(
            kind="hold",
            subject_id=act,
            outcome="held",
            inputs=[self.conservation_ref],
            payload={
                "act_key": f"residual:{ORDINAL}:0",
                "page_ordinal": ORDINAL,
                "residual_bounds": bounds,
                "residual_pixel_count": 4,
                "reason": "structural grouping claimed no region covering this ink",
            },
        )
        self.rows[act] = _row(
            act,
            f"residual:{ORDINAL}:0",
            self.page_id,
            "held",
            [self.context.input_ref(published.relative_path)],
        )
        return act

    def verify(self) -> None:
        """Both directions, in the order `_verify_synthetic_act_denominator` runs them."""
        self.context.finish()
        _verify_minted_act_rows(self.context, dict(self.rows))
        _verify_every_conservation_residual_is_accounted(self.context, dict(self.rows))


@pytest.fixture
def page(sealed) -> _Page:
    return _Page(sealed)


COMPONENT = {"x": 3, "y": 4, "w": 2, "h": 2}


# --- the page a withheld record earns -------------------------------------------


def test_a_withheld_page_held_as_one_item_verifies(page):
    """The honest shape, so every refusal below is the forgery's doing."""
    page.publish_conservation(_conservation_payload())
    act = page.hold_page()

    page.verify()

    assert act == derive_act_id(page.page_id, "page-residual", page.rectangle)
    assert page.rows[act]["act_key"] == "page-residual:1"


def test_a_withheld_record_with_no_page_residual_row_is_refused(page):
    """The whole cost of withholding is paid by the row that replaces the list.

    Without it the page's unclaimed ink has left the denominator behind a policy
    name — GOVERNANCE 2's silent loss with a reason code attached to it.
    """
    page.publish_conservation(_conservation_payload())
    page.context.finish()

    with pytest.raises(FatalAccounting, match="rather than exactly one"):
        _verify_every_conservation_residual_is_accounted(page.context, {})


def test_a_second_page_residual_row_for_one_page_is_refused(page):
    """Two review items for one withheld page double-count the same unlisted ink."""
    page.publish_conservation(_conservation_payload())
    page.hold_page()
    # Two holds over one rectangle collapse to one artifact id, so the second row
    # is minted over a rectangle that differs while still naming this page. That
    # one would refuse on its own rectangle at `_verify_minted_act_rows`; what is
    # under test here is that the *count* of holds for this page is checked at
    # all, rather than the first one found closing the record.
    page.hold_page(bounds={**page.rectangle, "w": page.rectangle["w"] - 1})

    page.context.finish()
    with pytest.raises(FatalAccounting, match="rather than exactly one"):
        _verify_every_conservation_residual_is_accounted(page.context, dict(page.rows))


def test_a_hold_naming_both_a_residual_and_a_page_rectangle_is_refused(page):
    """One hold accounts for one component or for a page held in place of many.

    Carrying both rectangles is not a richer record: it is two incompatible
    claims, and a router that picked either one would be choosing which of them
    the run means.
    """
    page.publish_conservation(_conservation_payload())
    act = page.hold_page(extra={"residual_bounds": COMPONENT})

    page.context.finish()
    with pytest.raises(FatalAccounting, match="names both a residual rectangle and a page"):
        _verify_minted_act_rows(page.context, {act: page.rows[act]})


def test_a_hold_naming_no_grouping_digest_is_refused(page):
    """The bound is a grouping-policy parameter; a hold owes the policy's name.

    Without it, a Designator free to invent its `max_residual_components` could
    hold any page it likes and this verifier would agree with it.
    """
    page.publish_conservation(_conservation_payload())
    act = page.hold_page(grouping_config_sha256=None)

    page.context.finish()
    with pytest.raises(FatalAccounting, match="sealed grouping"):
        _verify_minted_act_rows(page.context, {act: page.rows[act]})


def test_a_hold_naming_a_foreign_grouping_digest_is_refused(page):
    """A bound judged against a policy this run never sealed is not this run's bound."""
    page.publish_conservation(_conservation_payload())
    act = page.hold_page(grouping_config_sha256="0" * 64)

    page.context.finish()
    with pytest.raises(FatalAccounting, match="this run sealed at binding time"):
        _verify_minted_act_rows(page.context, {act: page.rows[act]})


@pytest.mark.parametrize(
    "forged",
    [
        {"reason_code": "structure-pass-held"},
        {"reason_code": None},
        {"blocking_page_ordinal": ORDINAL + 1},
    ],
)
def test_a_hold_wearing_another_cause_or_blaming_another_page_is_refused(page, forged):
    """The closed hold vocabulary is what a consumer branches on without prose.

    This is the one hold whose evidence a reviewer cannot open and count, so a
    page-residual hold arriving under another cause's code -- or naming a page
    other than the one it holds -- would be routed downstream as that other
    thing while carrying this one's claim.
    """
    page.publish_conservation(_conservation_payload())
    act = page.hold_page(extra=forged)

    page.context.finish()
    with pytest.raises(FatalAccounting, match="records its cause as"):
        _verify_minted_act_rows(page.context, {act: page.rows[act]})


def test_a_run_that_sealed_no_grouping_digest_is_refused_by_its_own_name(page, monkeypatch):
    """A binding gap and drift are two faults, and an operator does two things.

    `require_sealed_config` separates them for that reason: a run that never
    sealed the policy has to be created again on a build that does, while a hold
    naming a foreign digest means the policy file moved under a run that did.
    One message printing `None` as the sealed digest sends both to the same
    place.
    """
    import common.stage as stage_module

    page.publish_conservation(_conservation_payload())
    act = page.hold_page()
    monkeypatch.setattr(stage_module, "run_sealed_config_digests", lambda run: {})

    page.context.finish()
    with pytest.raises(FatalAccounting, match="sealed no designator-grouping digest at all"):
        _verify_minted_act_rows(page.context, {act: page.rows[act]})


def test_a_hold_naming_a_boolean_bound_is_refused_against_its_records_integer(page):
    """`True == 1` in Python, and a policy is an integer or it is not one.

    Defence in depth, and exercised as such: the row-side verifier types the
    hold's own bound first and refuses this hold for that, so this direction is
    driven on its own here. It is the comparison that binds the review item's
    policy to the reconciliation's, and a bare `!=` let a hold naming `True`
    agree with a record naming `1` -- the case `_is_count` exists for.
    """
    page.publish_conservation(_conservation_payload(bound=1, count=2))
    act = page.hold_page(bound=True, count=2)

    page.context.finish()
    with pytest.raises(FatalAccounting, match="does not name an integer residual component"):
        _verify_minted_act_rows(page.context, {act: page.rows[act]})
    with pytest.raises(FatalAccounting, match="must name one policy, as one integer"):
        _verify_every_conservation_residual_is_accounted(page.context, dict(page.rows))


def test_a_bound_the_reconciliation_did_not_exceed_is_refused(page):
    """The premise is the record's own count against the hold's own bound."""
    page.publish_conservation(_conservation_payload(count=BOUND))
    act = page.hold_page(count=BOUND)

    page.context.finish()
    with pytest.raises(FatalAccounting, match="which does not exceed it"):
        _verify_minted_act_rows(page.context, {act: page.rows[act]})


def test_a_hold_count_the_reconciliation_never_measured_is_refused(page):
    """The count on the review item is the count the reconciliation took."""
    page.publish_conservation(_conservation_payload())
    act = page.hold_page(count=MEASURED + 1)

    page.context.finish()
    with pytest.raises(FatalAccounting, match="never a second figure beside it"):
        _verify_minted_act_rows(page.context, {act: page.rows[act]})


def test_a_hold_judged_against_a_bound_the_record_did_not_apply_is_refused(page):
    """A laxer bound on the review item describes a decision the run never took."""
    page.publish_conservation(_conservation_payload())
    page.hold_page(bound=BOUND - 1, count=MEASURED)

    page.context.finish()
    with pytest.raises(FatalAccounting, match="must name one policy"):
        _verify_every_conservation_residual_is_accounted(page.context, dict(page.rows))


def test_a_held_page_over_a_record_still_reporting_proposed_is_refused(page):
    """The record standing behind a held page must itself say `held`.

    A record whose payload matches the hold exactly, down to the bound and
    count, still tells a reader the reconciliation *proposed* its residuals if
    its own `outcome` disagrees — the reverse of what a page-residual hold
    means.
    """
    page.publish_conservation(_conservation_payload(), outcome="proposed")
    act = page.hold_page()

    page.context.finish()
    with pytest.raises(FatalAccounting, match="rather than 'held'"):
        _verify_minted_act_rows(page.context, {act: page.rows[act]})


def test_a_withheld_hold_over_an_enumerated_record_is_refused(page):
    """A page whose components were listed owes one held act each, not one page."""
    page.publish_conservation(
        _conservation_payload(
            enumeration=RESIDUAL_ENUMERATION_COMPLETE,
            components=[{"bounds": COMPONENT, "pixel_count": 4, "review_priority": "high"}],
        ),
        outcome="proposed",
    )
    act = page.hold_page()

    page.context.finish()
    with pytest.raises(FatalAccounting, match="rather than 'withheld-page-held'"):
        _verify_minted_act_rows(page.context, {act: page.rows[act]})


def test_a_withheld_record_that_still_carries_a_components_key_is_refused(page):
    """Absence is the contract, because an empty list reads as "no unclaimed ink"."""
    page.publish_conservation(_conservation_payload(components=[]))
    act = page.hold_page()

    page.context.finish()
    with pytest.raises(FatalAccounting, match="still carries a residual_components key"):
        _verify_minted_act_rows(page.context, {act: page.rows[act]})


def test_an_unknown_residual_enumeration_is_refused(page):
    """The pair is closed, so a third spelling is a refusal and never a default."""
    page.publish_conservation(_conservation_payload(enumeration="partial"))
    page.context.finish()

    with pytest.raises(FatalAccounting, match="outside the closed set"):
        _verify_every_conservation_residual_is_accounted(page.context, {})


def test_a_record_naming_no_enumeration_at_all_is_refused(page):
    """The field is always published, so its absence is an unknown value.

    Reading a missing field as "complete" would let a record that lost it pass as
    fully enumerated with nothing listed — the exact confusion the closed pair
    exists to prevent, arriving by omission instead of by a new spelling.
    """
    payload = _conservation_payload()
    del payload["residual_enumeration"]
    page.publish_conservation(payload)
    page.context.finish()

    with pytest.raises(FatalAccounting, match="outside the closed set"):
        _verify_every_conservation_residual_is_accounted(page.context, {})


def test_a_page_residual_hold_outside_the_denominator_is_refused(page):
    """A held page the seal never counted is evidence beside the denominator."""
    page.publish_conservation(_conservation_payload())
    act = page.hold_page()
    page.context.finish()

    with pytest.raises(FatalAccounting, match="does not account for"):
        _verify_every_conservation_residual_is_accounted(page.context, {})
    assert act in page.rows


# --- the rectangle is recomputed, never read ------------------------------------


def test_a_forged_page_rectangle_is_refused_against_the_sealed_page_bytes(page):
    """A self-consistent identity over the wrong rectangle is still the wrong page.

    The hold below is internally perfect: the act id derives from exactly the
    rectangle the record names. Only a second reading of the sealed page bytes
    can tell that the rectangle is not the page.
    """
    forged = {"x": 0, "y": 0, "w": page.rectangle["w"], "h": page.rectangle["h"] + 40}
    page.publish_conservation(_conservation_payload())
    act = page.hold_page(bounds=forged)

    page.context.finish()
    with pytest.raises(FatalAccounting, match="not the complete sealed page rectangle"):
        _verify_minted_act_rows(page.context, {act: page.rows[act]})


def test_an_identity_that_does_not_bind_the_page_residual_class_is_refused(page):
    """The class is half the binding, and it is the half that carries disposition.

    A page-fallback identity over the page rectangle is `proposed` and reaches
    the witnesses; a page-residual identity over the same rectangle is terminal.
    Minting one and presenting it as the other is refused on the identity itself.
    """
    page.publish_conservation(_conservation_payload())
    act = page.hold_page(act=derive_act_id(page.page_id, "page-fallback", page.rectangle))

    page.context.finish()
    with pytest.raises(FatalAccounting, match="reserved page-residual class"):
        _verify_minted_act_rows(page.context, {act: page.rows[act]})


def test_a_hold_wearing_the_page_fallback_key_is_refused(page):
    """The two page-wide labels are derived, so neither may wear the other's."""
    page.publish_conservation(_conservation_payload())
    act = page.hold_page(act_key=fallback_page_act_key(ORDINAL))

    page.context.finish()
    with pytest.raises(FatalAccounting, match="derived page-residual key"):
        _verify_minted_act_rows(page.context, {act: page.rows[act]})


def test_the_premise_is_followed_through_exactly_one_input(page):
    """Not by address: the conservation record is reached through the hop that
    checks its bytes, so a hold with no input has nothing to be checked against."""
    page.publish_conservation(_conservation_payload())
    act = page.hold_page(inputs=[])

    page.context.finish()
    with pytest.raises(FatalAccounting, match="exactly one conservation artifact"):
        _verify_minted_act_rows(page.context, {act: page.rows[act]})


# --- what must not have moved ----------------------------------------------------


def test_an_enumerated_page_still_accounts_for_every_component_it_lists(page):
    """The complete path, byte for byte: each listed residual owes a held act."""
    page.publish_conservation(
        _conservation_payload(
            enumeration=RESIDUAL_ENUMERATION_COMPLETE,
            count=1,
            components=[{"bounds": COMPONENT, "pixel_count": 4, "review_priority": "high"}],
        ),
        outcome="proposed",
    )
    page.hold_component(COMPONENT)

    page.verify()


def test_an_enumerated_component_with_no_held_act_is_still_refused(page):
    """The residual path's own refusal, unchanged by the withheld branch beside it."""
    page.publish_conservation(
        _conservation_payload(
            enumeration=RESIDUAL_ENUMERATION_COMPLETE,
            count=1,
            components=[{"bounds": COMPONENT, "pixel_count": 4, "review_priority": "high"}],
        ),
        outcome="proposed",
    )
    page.context.finish()

    with pytest.raises(FatalAccounting, match="accounts for no held act for"):
        _verify_every_conservation_residual_is_accounted(page.context, {})


def test_an_enumerated_record_with_no_component_list_is_still_refused(page):
    """`complete` and no list is the older gap, and it still fires by its own name."""
    payload = _conservation_payload(enumeration=RESIDUAL_ENUMERATION_COMPLETE, count=0)
    page.publish_conservation(payload, outcome="proposed")
    page.context.finish()

    with pytest.raises(FatalAccounting, match="carries no residual-component list"):
        _verify_every_conservation_residual_is_accounted(page.context, {})


def test_an_enumerated_records_count_must_match_its_own_listed_components(page):
    """The count a reviewer is shown must be the list they can already count.

    An enumerated record whose `residual_component_count` disagrees with
    `len(residual_components)` is the same untruth `_verify_page_residual_premise`
    already refuses on the withheld side; here the evidence to recompute it is
    free, so nothing may leave it unrefused.
    """
    payload = _conservation_payload(
        enumeration=RESIDUAL_ENUMERATION_COMPLETE,
        count=99,
        components=[{"bounds": COMPONENT, "pixel_count": 4, "review_priority": "high"}],
    )
    page.publish_conservation(payload, outcome="proposed")
    page.hold_component(COMPONENT)
    page.context.finish()

    with pytest.raises(FatalAccounting, match="carries 1 entries"):
        _verify_every_conservation_residual_is_accounted(page.context, dict(page.rows))


def test_a_component_hold_still_traces_to_the_reconciliation_that_found_it(page):
    """The residual path is routed by the rectangle its hold names, and a hold
    naming a component rectangle still reaches the conservation record."""
    page.publish_conservation(
        _conservation_payload(
            enumeration=RESIDUAL_ENUMERATION_COMPLETE,
            count=1,
            components=[{"bounds": COMPONENT, "pixel_count": 4, "review_priority": "high"}],
        ),
        outcome="proposed",
    )
    act = page.hold_component({"x": 30, "y": 30, "w": 2, "h": 2})

    page.context.finish()
    with pytest.raises(FatalAccounting, match="does not carry at those bounds"):
        _verify_minted_act_rows(page.context, {act: page.rows[act]})


def test_a_proposed_extra_row_still_routes_to_the_page_fallback_verifier(page):
    """Routing by outcome first: `proposed` is the page-fallback path and nothing
    added here may divert it. A page-residual act is never proposed."""
    page.publish_conservation(_conservation_payload())
    act = derive_act_id(page.page_id, "page-fallback", page.rectangle)
    row = _row(act, fallback_page_act_key(ORDINAL), page.page_id, "proposed", [])

    page.context.finish()
    with pytest.raises(FatalAccounting, match="published no page-fallback record for it"):
        _verify_minted_act_rows(page.context, {act: row})
