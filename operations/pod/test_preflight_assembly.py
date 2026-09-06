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

"Set only by" is enforced rather than documented. Both were plain constructor
arguments, and `PreflightRunner` takes both values from its caller -- the
profile is passed to `run`, the reader to the constructor -- so a caller who
wanted the claim could simply write it down. Each fact now travels with an
opaque token minted inside those two runtime paths and nowhere else, so the
drills below reach the claim the way the runtime does: the profile comes out of
`SystemGpuProbe` driven by a fake `nvidia-smi`, and the served smoke result out
of `_with_service_evidence` holding a fake started handle.

Nothing here starts an engine or touches a GPU: the two seams are driven with
the doubles the preflight runner takes by construction, which is the point --
a double can produce a green *page*, and must still be unable to produce a
green *assembly claim*.
"""

from __future__ import annotations

import json
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from common.chairs.models import ChairIdentity
from operations.serving.preflight import _with_service_evidence

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


class ServedHandle:
    """The attribute surface `_with_service_evidence` reads off a started handle.

    A stand-in for `ServingManager`'s `ServiceHandle`: the engine name in the
    claim comes from a *published service receipt*, and this carries one. What
    it stands in for is the lifecycle -- start, fixture-bound proof, stop --
    which no test here runs. What it cannot stand in for is the mint: the token
    is created inside `_with_service_evidence`, which is the runtime's own code,
    reached here rather than imitated.
    """

    class Receipt:
        class Details:
            engine = "vllm"
            engine_version = "0.27.1"

        details = Details()

        def to_record(self) -> dict[str, object]:
            return {"engine": "vllm", "engine_version": "0.27.1"}

    receipt = Receipt()
    receipt_reference = {"path": "service-receipt.json"}
    launch_audit: dict[str, object] = {"argv": ("python", "-m", "vllm.entrypoints")}
    audit_reference = {"path": "launch-audit.json"}
    evidence_reference = {"path": "serving-evidence.json"}
    last_fixture_response_sha256 = "a" * 64
    last_fixture_output_sha256 = "b" * 64
    requests_completed = 1
    fixture_requests_completed = 1


class Reader:
    """A smoke reader that either served the read or did not, as the two real
    implementations differ: `ServingSmokeReader` names the engine that answered,
    the operator's `FixtureSmokeReader` names nothing.

    The served variant does not *write* `served_by`; it cannot. It hands its
    result to `operations.serving.preflight._with_service_evidence` -- the one
    function that may name an engine -- holding a receipt-bearing handle, which
    is the seam the serving runtime itself passes through.
    """

    def __init__(self, *, served: bool, valid: bool = True) -> None:
        self.served = served
        self.valid = valid

    def read(self, chair: ChairIdentity, fixture: Path, placement: PlacementTier) -> SmokeResult:
        del fixture, placement
        result = SmokeResult(
            shape_valid=self.valid,
            nonempty=self.valid,
            format_valid=self.valid,
            receipt={"chair": chair.role, "page": "golden"},
            utilization=(UtilizationSample(Decimal("40"), Decimal("9")),),
        )
        if not self.served:
            return result
        return _with_service_evidence(result, ServedHandle(), "c" * 64)  # type: ignore[arg-type]


class Disk:
    free = 400 * 1024**3


def answering_nvidia_smi(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """One card, four parseable fields, and a CUDA banner."""

    if len(argv) == 1:
        return subprocess.CompletedProcess(argv, 0, "CUDA Version: 13.0\n", "")
    return subprocess.CompletedProcess(argv, 0, f"{MEASURED_CARD}, 580.65, 24564, 8.6\n", "")


def measured_profile() -> GpuProfile:
    """What `SystemGpuProbe` returns when `nvidia-smi` actually answered.

    Driven through the probe rather than written down: `GpuProfile(measured=True)`
    is refused at construction now, because a profile that could declare itself
    measured could publish a paid measurement nobody made.
    """

    probe = SystemGpuProbe(disk_path="/", runner=answering_nvidia_smi, disk_usage=lambda _p: Disk())
    return probe.profile("bfloat16")


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

    report = runner(fixture_page, roles=("attestator_3",), reader=Reader(served=True)).run(
        measured_profile()
    )

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
        reader=Reader(served=True),
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

    report = runner(fixture_page, roles=("attestator_3",), reader=Reader(served=False)).run(
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

    report = runner(fixture_page, roles=("attestator_3",), reader=Reader(served=True)).run(
        synthetic_profile()
    )

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

    report = runner(fixture_page, roles=("attestator_3",), reader=Reader(served=False)).run(
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
        reader=Reader(served=True, valid=False),
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

    report = runner(fixture_page, roles=("attestator_3",), reader=Forger(served=False)).run(
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

    def absent(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, "", "nvidia-smi: command not found")

    probe = SystemGpuProbe(
        disk_path="/", runner=answering_nvidia_smi, disk_usage=lambda _path: Disk()
    )
    assert probe.profile("bfloat16").measured is True
    unmeasured = SystemGpuProbe(disk_path="/", runner=absent, disk_usage=lambda _path: Disk())
    assert unmeasured.profile("bfloat16").measured is False


def test_served_by_must_be_a_real_name_or_nothing() -> None:
    with pytest.raises(ValueError, match="served_by"):
        SmokeResult(True, True, True, {}, (), served_by="   ")


def test_a_caller_built_profile_cannot_claim_a_measured_card(fixture_page: Path) -> None:
    """The hole this guard closes, stated as the caller would have exploited it.

    `PreflightRunner.run` takes the profile from its caller, and `measured` was
    an ordinary constructor argument -- so anything that could call `run` could
    hand it a profile declaring a card nobody read, and the receipt would have
    published "real assembly measured on <whatever the caller typed>". The
    profile is now refused at construction, before it can reach a runner at all.
    """

    with pytest.raises(ValueError, match="cannot declare itself measured"):
        GpuProfile(
            name="a card nobody read",
            cuda_version="13.0",
            driver_version="580.65",
            compute_capability=(8, 6),
            vram_gib=Decimal("24"),
            disk_gib=Decimal("400"),
            dtype="bfloat16",
            measured=True,
        )
    # Nor by supplying something *shaped* like the token: it is checked by type,
    # and no value a caller can name is that type.
    for forged in ("nvidia-smi read by SystemGpuProbe", True, object(), {"origin": "probe"}):
        with pytest.raises(ValueError, match="minted by the probe"):
            GpuProfile(
                name="a card nobody read",
                cuda_version="13.0",
                driver_version="580.65",
                compute_capability=(8, 6),
                vram_gib=Decimal("24"),
                disk_gib=Decimal("400"),
                dtype="bfloat16",
                measured=True,
                provenance=forged,
            )
    # And the runtime's own profile still proves what it measured, through the
    # same probe seam the pod runs -- the guard refuses forgery, not measurement.
    report = runner(fixture_page, roles=("attestator_3",), reader=Reader(served=True)).run(
        measured_profile()
    )
    assert report.assembly_proven is True


def test_a_caller_built_smoke_result_cannot_name_its_own_engine(fixture_page: Path) -> None:
    """The serving half of the same hole.

    A smoke reader is supplied to `PreflightRunner`'s constructor, so a reader
    that could set `served_by` could assert that an engine served a page it
    invented. `_bound_receipt` already refused a reader that wrote the
    runtime-owned `served_engine` *receipt* field; this refuses the reader that
    writes the field that receipt is derived from.
    """

    with pytest.raises(ValueError, match="cannot name its own engine"):
        SmokeResult(True, True, True, {}, (), served_by="vllm 0.27.1")
    with pytest.raises(ValueError, match="minted by the serving runtime"):
        SmokeResult(True, True, True, {}, (), served_by="vllm 0.27.1", provenance="vllm 0.27.1")

    # A reader that returns an unserved result on a genuinely measured card
    # proves the card and not the assembly -- the honest third case, and the
    # only outcome left to a reader that cannot mint provenance.
    report = runner(fixture_page, roles=("attestator_3",), reader=Reader(served=False)).run(
        measured_profile()
    )
    assert report.assembly_proven is False
    assert report.smoke_receipts[0]["served_engine"] is None


def test_the_token_never_reaches_a_receipt_or_a_record(fixture_page: Path) -> None:
    """It is provenance, not evidence: nothing serialises it.

    A token that appeared in a published record could be read back out of an
    artifact and, in a future reader, handed to a constructor. It appears in no
    receipt, no record, and no JSON -- and the record is JSON-serialisable,
    which a token in it would break.
    """

    report = runner(fixture_page, roles=("attestator_3",), reader=Reader(served=True)).run(
        measured_profile()
    )

    record = report.to_record()
    text = json.dumps(record)
    assert "provenance" not in text
    assert "runtime provenance" not in text
    assert report.profile.provenance is not None, "the profile still carries it in memory"
