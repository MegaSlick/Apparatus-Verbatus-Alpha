"""What a preflight receipt is allowed to say about the assembly it measured.

`PreflightReport.assembly_proven` used to be the constant `False`, and every
receipt carried the note "fixture-only result; no real chair or GPU assembly is
proven".  On the fixture path that is exactly right.  On a rented card it is a
false record of a paid measurement: the receipt disowns the one measurement the
pod was rented to make.  GOVERNANCE 10 binds understatement as much as
overstatement -- claims are made only about what was actually measured, and
"nothing was measured" is itself a claim.

So the flag is derived from two facts, each recorded by the layer that produced
it and neither inferrable from a label:

* `GpuProfile.measured`, set only by `SystemGpuProbe`'s successful `nvidia-smi`
  path -- the card was read by a real driver;
* `SmokeResult.served_by`, set only by `operations.serving.preflight`'s
  `_with_service_evidence` from the receipt of the service handle it started,
  proved fixture-bound and stopped -- a chair read the golden page back through
  an engine that served it.

Nothing here starts an engine or touches a GPU: the two seams are driven with
the doubles the preflight runner takes by construction, which is the point --
a double can produce a green *page*, and must still be unable to produce a
green *assembly claim*.
"""

from __future__ import annotations

import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from common.chairs.models import ChairIdentity

from .preflight import (
    FIXTURE_ONLY_ASSEMBLY_NOTE,
    GpuProfile,
    PlacementRecipe,
    PlacementTable,
    PlacementTier,
    PreflightRunner,
    SmokeResult,
    SystemGpuProbe,
    UtilizationSample,
)

MEASURED_CARD = "NVIDIA RTX A5000"


def table() -> PlacementTable:
    return PlacementTable(
        dtype_floors={"bfloat16": (8, 0)},
        tiers=(
            PlacementTier(
                identifier="generic-24gb",
                min_vram_gib=Decimal("16"),
                max_vram_gib_exclusive=None,
                residency="single",
                detector_device="cpu",
                recipe=PlacementRecipe(
                    engine_memory_fraction=Decimal("0.58"),
                    context_cap=8192,
                    pixel_cap=1344,
                    batch_size=1,
                ),
            ),
        ),
    )


def identity(role: str) -> ChairIdentity:
    return ChairIdentity(
        role=role,
        source="huggingface",
        repo=f"vendor/{role}",
        path=None,
        revision="c" * 40,
        digest_manifest="d" * 64,
        manifest=f"manifests/{role}.json",
        adapter_of=None,
        serving_recipe=f"{role}-recipe",
        license_note="test fixture identity",
    )


class Models:
    """The one `ModelsConfig` surface `PreflightRunner` reads."""

    def __init__(self, *roles: str) -> None:
        self.chairs = {role: identity(role) for role in roles}

    def witness_floor_status(self):  # type: ignore[no-untyped-def]
        class Floor:
            meets_floor = True
            configured_count = 3
            floor = 3

        return Floor()


class Verifier:
    def verify(self, chair: ChairIdentity) -> dict[str, object]:
        return {"verified": True, "revision": chair.revision}

    def refetch_once(self, chair: ChairIdentity) -> None:
        raise AssertionError("no cache mismatch is staged in this module")


class Reader:
    """A smoke reader whose `served_by` the test sets, exactly as the two real
    implementations differ: `ServingSmokeReader` names the engine that answered,
    the operator's `FixtureSmokeReader` names nothing."""

    def __init__(self, *, served_by: str | None, valid: bool = True) -> None:
        self.served_by = served_by
        self.valid = valid

    def read(self, chair: ChairIdentity, fixture: Path, placement: PlacementTier) -> SmokeResult:
        del fixture, placement
        return SmokeResult(
            shape_valid=self.valid,
            nonempty=self.valid,
            format_valid=self.valid,
            receipt={"chair": chair.role, "page": "golden"},
            utilization=(UtilizationSample(Decimal("40"), Decimal("9")),),
            served_by=self.served_by,
        )


def measured_profile() -> GpuProfile:
    """What `SystemGpuProbe` returns when `nvidia-smi` actually answered."""

    return GpuProfile(
        name=MEASURED_CARD,
        cuda_version="13.0",
        driver_version="580.65",
        compute_capability=(8, 6),
        vram_gib=Decimal("24"),
        disk_gib=Decimal("400"),
        dtype="bfloat16",
        measured=True,
    )


def synthetic_profile() -> GpuProfile:
    """A hand-built planning profile -- the operator rehearsal's shape."""

    return GpuProfile(
        name="synthetic",
        cuda_version="13.0",
        driver_version="580.65",
        compute_capability=(8, 6),
        vram_gib=Decimal("24"),
        disk_gib=Decimal("400"),
        dtype="bfloat16",
    )


@pytest.fixture
def fixture_page(tmp_path: Path) -> Path:
    page = tmp_path / "golden.png"
    page.write_bytes(b"golden page bytes")
    return page


def runner(fixture: Path, *, roles: tuple[str, ...], reader: Reader) -> PreflightRunner:
    return PreflightRunner(Models(*roles), table(), Verifier(), reader, fixture)


def test_a_real_card_and_a_served_chair_prove_the_assembly(fixture_page: Path) -> None:
    """Both halves present: the receipt says so, and says of what."""

    report = runner(
        fixture_page, roles=("attestator_3",), reader=Reader(served_by="vllm 0.27.1")
    ).run(measured_profile())

    assert report.color == "green"
    assert report.assembly_proven is True
    record = report.to_record()
    assert record["assembly_proven"] is True
    # The note names the chairs and the card, so a reader of the receipt alone
    # can tell what was proven from what merely ran.
    assert record["assembly_note"] == (
        f"real assembly measured on {MEASURED_CARD}: attestator_3 via vllm 0.27.1 "
        "smoke-read the golden page through a served engine"
    )
    assert record["assembly_note"] != FIXTURE_ONLY_ASSEMBLY_NOTE
    smoke = report.smoke_receipts[0]
    assert smoke["chair"] == "attestator_3"
    assert smoke["served_engine"] == "vllm 0.27.1"


def test_the_old_constant_would_have_misreported_this_paid_measurement(
    fixture_page: Path,
) -> None:
    """The counterfactual, stated as an assertion rather than a comment.

    `assembly_proven = False` was unconditional, and `to_record` derived the
    note from it.  Re-deriving both here shows the record a rented card would
    have published under that code: `False`, and "fixture-only" about a real
    `nvidia-smi` read and a real served engine.  This test fails against the
    constant and passes against the derivation, which is what makes the change
    a behaviour change rather than a rewording.
    """

    report = runner(
        fixture_page,
        roles=("attestator_3", "designator_structure"),
        reader=Reader(served_by="vllm 0.27.1"),
    ).run(measured_profile())

    old_flag = False
    old_note = FIXTURE_ONLY_ASSEMBLY_NOTE
    assert report.assembly_proven != old_flag
    assert report.to_record()["assembly_note"] != old_note
    # Every chair that read through the engine is named, in a stable order.
    assert report.to_record()["assembly_note"] == (
        f"real assembly measured on {MEASURED_CARD}: attestator_3 via vllm 0.27.1, "
        "designator_structure via vllm 0.27.1 smoke-read the golden page through a "
        "served engine"
    )


def test_the_fixture_path_keeps_the_old_flag_and_the_old_note(fixture_page: Path) -> None:
    """A synthetic profile and a reader that served nothing prove nothing.

    Both halves are absent here, which is the operator rehearsal's own shape,
    and the sentence it publishes is byte-for-byte the one it published before.
    """

    report = runner(fixture_page, roles=("attestator_3",), reader=Reader(served_by=None)).run(
        synthetic_profile()
    )

    assert report.color == "green"
    assert report.assembly_proven is False
    record = report.to_record()
    assert record["assembly_proven"] is False
    assert record["assembly_note"] == FIXTURE_ONLY_ASSEMBLY_NOTE
    assert report.smoke_receipts[0]["served_engine"] is None


def test_a_served_chair_on_a_profile_nobody_measured_proves_nothing(fixture_page: Path) -> None:
    """Half of the claim is not the claim: a typed VRAM number is not a card."""

    report = runner(
        fixture_page, roles=("attestator_3",), reader=Reader(served_by="vllm 0.27.1")
    ).run(synthetic_profile())

    assert report.assembly_proven is False
    assert report.to_record()["assembly_note"] == FIXTURE_ONLY_ASSEMBLY_NOTE


def test_a_measured_card_with_no_served_chair_says_so_rather_than_fixture_only(
    fixture_page: Path,
) -> None:
    """The third case the old note could not express.

    The card is real and the assembly is not proven.  Calling that "fixture-only"
    would misdescribe a measurement that was paid for, so the note names what
    happened instead.
    """

    report = runner(fixture_page, roles=("attestator_3",), reader=Reader(served_by=None)).run(
        measured_profile()
    )

    assert report.assembly_proven is False
    assert report.to_record()["assembly_note"] == (
        f"no assembly proven: {MEASURED_CARD} was measured by a real driver read, but no "
        "chair smoke-read the golden page through a served engine"
    )


def test_an_invalid_page_read_through_a_real_engine_proves_no_assembly(
    fixture_page: Path,
) -> None:
    """ "Smoke-read its witness" means the page came back, not that a process started."""

    report = runner(
        fixture_page,
        roles=("attestator_3",),
        reader=Reader(served_by="vllm 0.27.1", valid=False),
    ).run(measured_profile())

    assert report.color == "red"
    assert [issue.code for issue in report.issues] == ["smoke-output-invalid"]
    assert report.assembly_proven is False
    # The engine that served the invalid read is still recorded: the read
    # happened, and only the claim about it is withheld (GOVERNANCE 2).
    assert report.smoke_receipts[0]["served_engine"] == "vllm 0.27.1"


def test_a_smoke_adapter_cannot_write_the_runtime_owned_served_engine_field(
    fixture_page: Path,
) -> None:
    """The field the assembly claim is published under is the runtime's to write.

    Same posture as `repaired_once` on a cache receipt: an adapter that could
    pre-populate it could assert its own proof.
    """

    class Forger(Reader):
        def read(
            self, chair: ChairIdentity, fixture: Path, placement: PlacementTier
        ) -> SmokeResult:
            result = super().read(chair, fixture, placement)
            result.receipt["served_engine"] = "vllm 0.27.1"
            return result

    report = runner(fixture_page, roles=("attestator_3",), reader=Forger(served_by=None)).run(
        measured_profile()
    )

    assert report.color == "red"
    codes = [issue.code for issue in report.issues]
    assert "smoke-receipt-invalid" in codes
    assert report.assembly_proven is False
    assert report.smoke_receipts == ()


def test_a_synthetic_profile_cannot_declare_itself_measured() -> None:
    """`measured` defaults to False, so a fixture is fixture by construction."""

    assert synthetic_profile().measured is False
    assert measured_profile().measured is True


def test_only_a_real_driver_read_sets_measured() -> None:
    """The other half of the claim, at the seam that produces it.

    `SystemGpuProbe` is the only writer of `measured`; both of its outcomes are
    checked here so the flag cannot quietly become "the probe object was
    constructed".
    """

    class Disk:
        free = 400 * 1024**3

    def answering(argv: list[str]) -> subprocess.CompletedProcess[str]:
        if len(argv) == 1:
            return subprocess.CompletedProcess(argv, 0, "CUDA Version: 13.0\n", "")
        return subprocess.CompletedProcess(argv, 0, f"{MEASURED_CARD}, 580.65, 24564, 8.6\n", "")

    def absent(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, "", "nvidia-smi: command not found")

    probe = SystemGpuProbe(disk_path="/", runner=answering, disk_usage=lambda path: Disk())
    assert probe.profile("bfloat16").measured is True
    unmeasured = SystemGpuProbe(disk_path="/", runner=absent, disk_usage=lambda path: Disk())
    assert unmeasured.profile("bfloat16").measured is False


def test_served_by_must_be_a_real_name_or_nothing() -> None:
    with pytest.raises(ValueError, match="served_by"):
        SmokeResult(True, True, True, {}, (), served_by="   ")
