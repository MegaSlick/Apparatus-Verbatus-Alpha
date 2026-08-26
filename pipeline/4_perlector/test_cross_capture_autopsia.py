"""Unit 19B's atomic all-capture Perlector boundary."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import logical_reading  # noqa: E402
import run as perlector_run  # noqa: E402
from combined import run_logical_passes  # noqa: E402

from common.contracts.canonical import digest_bytes, digest_of  # noqa: E402
from common.contracts.errors import ContractError, SchemaRefusal  # noqa: E402
from common.cross_capture_autopsia import (  # noqa: E402
    OVER_CAPACITY,
    assemble_reader_input,
    atomic_delivered_pixels,
    build_autopsia,
    build_autopsia_from_run,
    cross_capture_audit_scope,
    dissent_shell,
    invoke_one_logical_read,
    over_capacity_reason,
    validate_autopsia,
)
from common.physical_act_partition import source_ledger_from_run  # noqa: E402

A, B = "a" * 64, "b" * 64
REF = {"relative_path": "blobs/x", "sha256": "c" * 64}
# The stand-in transport below reads a path back as its own bytes, so a view
# reference states the digest of exactly that: an image reference is a claim
# about bytes, and `atomic_delivered_pixels` checks the claim before a reader
# is handed anything (`test_a_view_image_that_no_longer_matches_its_digest_is_refused`).
READ_BYTES = str.encode


def blob(path):
    return {"relative_path": path, "sha256": digest_bytes(READ_BYTES(path))}


def view(capture, suffix):
    return {
        "view_id": f"view-{suffix}",
        "physical_page_id": "ppg_fixture",
        "source_sha256": capture,
        "page_ids": [f"pg_{suffix}"],
        "local_act_ids": [f"act_{suffix}"],
        "region_refs": [blob(f"blobs/crop-{suffix}")],
        "page_render_refs": [blob(f"blobs/page-{suffix}")],
        "alignment_ref": f"alignment-{suffix}",
        "visibility_evidence_refs": [blob(f"blobs/visibility-{suffix}")],
    }


def autopsia(views=None):
    return build_autopsia(
        logical_act_id="pac_fixture",
        partition_ref=REF,
        required_capture_sha256s=[A, B],
        views=views or [view(A, "a"), view(B, "b")],
    )


def test_complete_capture_set_is_canonical_and_shuffle_invariant():
    assert autopsia() == autopsia([view(B, "b"), view(A, "a")])
    assert validate_autopsia(autopsia())["member_conservation"]["required_count"] == 2


def test_every_capture_pixel_arrives_in_one_atomic_delivery():
    delivered = atomic_delivered_pixels(autopsia(), read_bytes=READ_BYTES, max_images=6)
    assert delivered == {
        "region_images": [b"blobs/crop-a", b"blobs/crop-b"],
        "page_render_images": [b"blobs/page-a", b"blobs/page-b"],
    }


class RecordingReader:
    def __init__(self):
        self.calls = []

    def read(self, dossier, *, pass_kind, delivered_pixels):
        self.calls.append((dossier, pass_kind, delivered_pixels))
        return {"text": "joint ink", "stop_reason": None}


def test_one_logical_read_receives_all_pixels_testimony_and_prior_in_one_call():
    reader = RecordingReader()
    dossier = {
        "logical_act_id": "pac_fixture",
        "testimonia": [{"capture": A}, {"capture": B}],
        "prior_draft": {"text": "prior"},
    }
    delivered, pixels, result = invoke_one_logical_read(
        reader,
        autopsia=autopsia(),
        dossier=dossier,
        read_bytes=READ_BYTES,
        max_images=6,
        pass_kind="perlectio",
    )
    assert result["text"] == "joint ink"
    assert len(reader.calls) == 1
    seen, kind, received = reader.calls[0]
    assert kind == "perlectio"
    assert seen == delivered
    assert received == pixels
    assert seen["testimonia"] == dossier["testimonia"]
    assert seen["prior_draft"] == dossier["prior_draft"]
    assert seen["cross_capture_autopsia"] == autopsia()
    assert received["region_images"] == [b"blobs/crop-a", b"blobs/crop-b"]


def test_the_reader_receives_a_dossier_digest_sealing_the_delivered_fields():
    reader = RecordingReader()
    body = {"testimonia": []}
    delivered, _pixels, _result = invoke_one_logical_read(
        reader,
        autopsia=autopsia(),
        dossier={**body, "dossier_digest": digest_of(body)},
        read_bytes=READ_BYTES,
        max_images=6,
        pass_kind="perlectio",
    )
    sealed = {key: value for key, value in delivered.items() if key != "dossier_digest"}
    assert delivered["dossier_digest"] == digest_of(sealed)
    assert reader.calls[0][0] == delivered


def test_every_instrument_arm_reuses_the_same_complete_presentation_not_one_capture():
    dossier = {"testimonia": [], "prior_draft": {"text": "prior"}}
    for pass_kind in ("lectio-prior", "lectio-nuda", "primed-without-prior", "perlectio"):
        delivered, pixels = assemble_reader_input(
            autopsia=autopsia(),
            dossier=dossier,
            read_bytes=READ_BYTES,
            max_images=6,
        )
        assert delivered["cross_capture_autopsia"]["required_capture_sha256s"] == [A, B]
        assert len(pixels["region_images"]) == 2, pass_kind


def test_clustered_logical_passes_make_one_establishing_call_and_no_capture_local_calls():
    reader = RecordingReader()
    output = run_logical_passes(
        reader,
        autopsia=autopsia(),
        dossier={"testimonia": [{"capture": A}, {"capture": B}]},
        read_bytes=READ_BYTES,
        protocol_config={"max_images": 6},
        nuda_sampled=True,
        control_sampled=True,
    )
    assert set(output) == {"lectio-prior", "lectio-nuda", "primed-without-prior", "perlectio"}
    assert [call[1] for call in reader.calls] == [
        "lectio-prior",
        "lectio-nuda",
        "primed-without-prior",
        "perlectio",
    ]
    assert all(
        call[0]["cross_capture_autopsia"]["required_capture_sha256s"] == [A, B]
        and len(call[2]["region_images"]) == 2
        for call in reader.calls
    )
    assert output["lectio-nuda"]["dossier"]["testimonia"] == []
    assert output["primed-without-prior"]["dossier"]["testimonia"] == [
        {"capture": A},
        {"capture": B},
    ]
    assert output["perlectio"]["dossier"]["prior_draft"] == {"text": "joint ink"}


def test_a_withheld_prior_is_retained_after_but_not_delivered_to_the_reader():
    reader = RecordingReader()
    body = {"testimonia": []}
    output = run_logical_passes(
        reader,
        autopsia=autopsia(),
        dossier={**body, "dossier_digest": digest_of(body)},
        read_bytes=READ_BYTES,
        protocol_config={"max_images": 6},
        nuda_sampled=False,
        control_sampled=False,
        draft_fed=False,
    )
    reader_dossier = next(call[0] for call in reader.calls if call[1] == "perlectio")
    assert reader_dossier["prior_draft_view"] == "withheld"
    assert "prior_draft" not in reader_dossier
    reader_body = {key: value for key, value in reader_dossier.items() if key != "dossier_digest"}
    assert reader_dossier["dossier_digest"] == digest_of(reader_body)

    retained = output["perlectio"]["dossier"]
    assert retained["prior_draft_view"] == "withheld"
    assert retained["prior_draft"] == {"text": "joint ink"}


def test_sealed_capacity_holds_a_cluster_before_any_logical_reader_call():
    reader = RecordingReader()
    with pytest.raises(SchemaRefusal, match=OVER_CAPACITY):
        run_logical_passes(
            reader,
            autopsia=autopsia(),
            dossier={"testimonia": []},
            read_bytes=lambda _: pytest.fail("no capture may be read before the capacity hold"),
            protocol_config={"max_images": 3},
            nuda_sampled=False,
            control_sampled=False,
        )
    assert reader.calls == []


def test_unmeasured_or_insufficient_capacity_holds_before_any_read():
    for capacity in (None, 3):
        with pytest.raises(SchemaRefusal, match=OVER_CAPACITY):
            atomic_delivered_pixels(
                autopsia(), read_bytes=lambda _: pytest.fail("read"), max_images=capacity
            )


def test_capture_drop_or_preference_field_is_refused():
    with pytest.raises(SchemaRefusal, match="required and delivered"):
        autopsia([view(A, "a")])
    bad = view(A, "a")
    bad["selected_view"] = True
    with pytest.raises(SchemaRefusal, match="preference|closed schema"):
        autopsia([bad, view(B, "b")])


@pytest.mark.parametrize(
    ("required", "views", "match"),
    [
        (None, [view(A, "a"), view(B, "b")], "required_capture_sha256s"),
        ([A, B], None, "views"),
    ],
)
def test_malformed_collection_boundaries_are_named_schema_refusals(required, views, match):
    with pytest.raises(SchemaRefusal, match=match):
        build_autopsia(
            logical_act_id="pac_fixture",
            partition_ref=REF,
            required_capture_sha256s=required,
            views=views,
        )


def test_source_ledger_is_derived_from_manifest_not_proposals():
    assert source_ledger_from_run(
        {"source_manifest": [{"sha256": A}, {"sha256": A}, {"sha256": B}]}
    ) == {A, B}
    with pytest.raises(SchemaRefusal, match="cluster-member-absent"):
        build_autopsia_from_run(
            run={"source_manifest": [{"sha256": A}]},
            logical_act_id="pac_fixture",
            partition_ref=REF,
            required_capture_sha256s=[A, B],
            views=[view(A, "a"), view(B, "b")],
        )


def test_dissent_shell_is_post_read_and_pair_complete():
    shell = dissent_shell(
        perlectio_ref=REF,
        autopsia=autopsia(),
        reader_invocation_ref=REF,
        response_observation_digest="a" * 64,
    )
    assert shell["logical_act_id"] == "pac_fixture"
    assert shell["capture_pairs"] == [[A, B]]
    assert shell["reader_invocation_ref"] == REF
    assert shell["response_observation_digest"] == "a" * 64


def test_cross_capture_audit_has_the_complete_page_set_and_no_representative_page():
    assert cross_capture_audit_scope(autopsia()) == {"page_ids": ["pg_a", "pg_b"]}


def test_a_view_image_that_no_longer_matches_its_digest_is_refused():
    """The per-pass reader dossier this transport replaced verified every
    delivered crop against its sealed digest (`dossier.py::_delivered_images`).
    A transport that handed a reader whatever bytes now sit at the path would
    have dropped that guard silently on the way through 19B."""
    reader = RecordingReader()
    with pytest.raises(SchemaRefusal, match="no longer matches its sealed digest"):
        atomic_delivered_pixels(
            autopsia(), read_bytes=lambda path: b"tampered " + READ_BYTES(path), max_images=6
        )
    with pytest.raises(SchemaRefusal, match="no longer matches its sealed digest"):
        invoke_one_logical_read(
            reader,
            autopsia=autopsia(),
            dossier={"testimonia": []},
            read_bytes=lambda path: b"tampered " + READ_BYTES(path),
            max_images=6,
            pass_kind="perlectio",
        )
    assert reader.calls == []


def test_an_unreadable_view_image_is_a_named_refusal_not_a_bare_os_error():
    def missing(_path):
        raise OSError("no such blob")

    with pytest.raises(SchemaRefusal, match="could not be read"):
        atomic_delivered_pixels(autopsia(), read_bytes=missing, max_images=6)


def test_the_unprimed_arms_carry_no_witness_derived_region_coverage():
    """`build_dossier` omits `witness_covered` entirely when it is handed no
    testimonia, so the old per-pass path's lectio-prior/nuda dossiers had no
    such key. The combined path builds one dossier *with* witnesses and strips
    it per arm, and a strip that stopped at `testimonia` would leave the
    unprimed instrument holding a witness-derived fact about every region."""
    reader = RecordingReader()
    output = run_logical_passes(
        reader,
        autopsia=autopsia(),
        dossier={
            "testimonia": [{"capture": A}],
            "act_attachment": {"comparison_views": {}},
            "regions": [
                {"region_id": "rgn_a", "image_sha256": "d" * 64, "witness_covered": True},
                {"region_id": "rgn_b", "image_sha256": "e" * 64, "witness_covered": False},
            ],
        },
        read_bytes=READ_BYTES,
        protocol_config={"max_images": 6},
        nuda_sampled=True,
        control_sampled=True,
    )
    for arm in ("lectio-prior", "lectio-nuda"):
        seen = output[arm]["dossier"]
        assert seen["testimonia"] == []
        assert "act_attachment" not in seen
        assert all("witness_covered" not in region for region in seen["regions"]), arm
    # The primed arms are witness-bearing by design and keep every fact.
    for arm in ("primed-without-prior", "perlectio"):
        assert all("witness_covered" in region for region in output[arm]["dossier"]["regions"]), arm


def test_over_capacity_is_answerable_before_it_is_a_refusal():
    """A producer routes the named finding to a not-run Perlectio for that act
    (consult §3.1); it can only do so if asking is not itself the refusal."""
    assert over_capacity_reason(autopsia(), 4) is None
    assert OVER_CAPACITY in over_capacity_reason(autopsia(), 3)
    assert OVER_CAPACITY in over_capacity_reason(autopsia(), None)


def test_the_final_published_dossier_is_swept_for_late_preference_fields():
    """The combined transport adds fields after ``build_dossier``'s sweep.

    The final reseal is therefore the production guard over what publication
    actually retains, rather than only what the earlier constructor saw.
    """
    late_preference = {
        "logical_act_id": "pac_fixture",
        "cross_capture_autopsia": autopsia(),
        "winner_capture": A,
        "dossier_digest": "0" * 64,
    }
    with pytest.raises(ContractError, match="order-bearing or trust-bearing field"):
        perlector_run._reseal_dossier(late_preference)


def test_a_late_preference_field_is_refused_before_the_reader_is_called():
    reader = RecordingReader()
    with pytest.raises(SchemaRefusal, match="forbidden preference field"):
        invoke_one_logical_read(
            reader,
            autopsia=autopsia(),
            dossier={"testimonia": [], "winner_capture": A},
            read_bytes=READ_BYTES,
            max_images=6,
            pass_kind="perlectio",
        )
    assert reader.calls == []


def _matching_cross_capture_dossier():
    record = autopsia()
    body = {
        "logical_act_id": record["logical_act_id"],
        "cross_capture_autopsia": record,
        "regions": [
            {"image_path": ref["relative_path"], "image_sha256": ref["sha256"]}
            for item in record["views"]
            for ref in item["region_refs"]
        ],
        "page_renders": [
            {"image_path": ref["relative_path"], "image_sha256": ref["sha256"]}
            for item in record["views"]
            for ref in item["page_render_refs"]
        ],
    }
    return body


def test_published_cross_capture_evidence_must_match_identity_images_and_partition_input():
    dossier = _matching_cross_capture_dossier()
    perlector_run._validate_cross_capture_dossier(dossier, inputs=[REF])

    wrong_identity = dict(dossier, logical_act_id="pac_other")
    with pytest.raises(SchemaRefusal, match="logical act identity disagrees"):
        perlector_run._validate_cross_capture_dossier(wrong_identity, inputs=[REF])

    missing_region = dict(dossier, regions=dossier["regions"][:-1])
    with pytest.raises(SchemaRefusal, match="regions differ"):
        perlector_run._validate_cross_capture_dossier(missing_region, inputs=[REF])

    with pytest.raises(SchemaRefusal, match="partition.*absent"):
        perlector_run._validate_cross_capture_dossier(dossier, inputs=[])


class _Tree:
    def __init__(self):
        self.blobs = 0

    def read_artifact(self, *_args):
        return {"payload": {"source_sha256": A}}

    def build_manifest(self, *_args):
        return {"artifacts": [{"kind": "page", "artifact_id": "page-a"}]}

    def put_blob(self, *_args):
        self.blobs += 1
        raise AssertionError("a partition this loop cannot read must not be published")


class _Context:
    def __init__(self):
        self.tree = _Tree()
        self.run = {
            "register_digest": "0" * 64,
            "source_manifest": [{"sha256": A}],
        }

    def artifact_ref(self, *_args):
        return {"relative_path": "seal", "sha256": "1" * 64}


EXPECTED = [
    {
        "act_id": "act_1",
        "act_key": "k1",
        "page_id": "pg_1",
        "page_ordinal": 1,
        "outcome": "expected",
        "evidence": [{"relative_path": "2_designator/blobs/p"}],
    }
]


def _partition(monkeypatch, groups, findings=()):
    monkeypatch.setattr(logical_reading, "read_snapshot", lambda *_a: b"")
    monkeypatch.setattr(logical_reading, "_verified_source_ledger", lambda *_a: {A})
    monkeypatch.setattr(
        logical_reading,
        "build_physical_act_partition",
        lambda **_kw: {"findings": list(findings), "logical_acts": groups},
    )
    return logical_reading.build_run_partition(_Context(), EXPECTED)


def _singleton_group(act_id):
    return {
        "logical_act_id": act_id,
        "identity_scope": "image-local-singleton",
        "physical_act_id": None,
        "physical_page_components": [],
        "member_local_acts": [{"act_id": act_id}],
        "capture_presentations": [],
    }


def test_a_partition_with_any_finding_stops_before_the_first_perlectio(monkeypatch):
    """The read loop publishes each act as it reads it, so by the time it
    reaches an act the partition could not resolve, earlier acts already have
    Perlectiones. Consult §2.1 stops the run *before* Perlector publication."""
    with pytest.raises(SchemaRefusal, match="not total"):
        _partition(
            monkeypatch,
            [_singleton_group("act_1")],
            findings=[{"code": "unresolved-physical-act", "act_id": "act_2"}],
        )


def test_a_clustered_logical_act_is_refused_rather_than_read_once_per_member(monkeypatch):
    """`run.py` walks local acts and presents one local act's own regions, so a
    multi-member logical act would publish one capture-local Perlectio per
    member (forbidden shapes §7.9, §7.15). Unreachable while every act is an
    image-local singleton; refused rather than left silent for the first run
    whose register actually clusters."""
    clustered = _singleton_group("pac_1")
    clustered["identity_scope"] = "physical-act"
    clustered["physical_act_id"] = "pac_1"
    clustered["member_local_acts"] = [{"act_id": "act_1"}, {"act_id": "act_2"}]
    with pytest.raises(SchemaRefusal, match="clustered logical act"):
        _partition(monkeypatch, [clustered])
    lone_member = dict(clustered, member_local_acts=[{"act_id": "act_1"}])
    with pytest.raises(SchemaRefusal, match="clustered logical act"):
        _partition(monkeypatch, [lone_member])


def test_excluding_a_held_act_from_the_partition_denominator_is_reported(monkeypatch, capsys):
    """The forced narrower count is a named run finding, never a quiet census."""
    seen = {}

    def unresolved_partition(**kwargs):
        seen.update(kwargs)
        return {
            "findings": [{"code": "unresolved-physical-act", "act_id": "act_1"}],
            "logical_acts": [],
        }

    monkeypatch.setattr(logical_reading, "read_snapshot", lambda *_a: b"")
    monkeypatch.setattr(logical_reading, "_verified_source_ledger", lambda *_a: {A})
    monkeypatch.setattr(logical_reading, "build_physical_act_partition", unresolved_partition)
    held = {
        "act_id": "act_held",
        "act_key": "held",
        "page_id": "pg_held",
        "page_ordinal": 2,
        "outcome": "held",
        "evidence": [{"relative_path": "2_designator/artifacts/hold.json"}],
    }
    with pytest.raises(SchemaRefusal, match="not total"):
        logical_reading.build_run_partition(_Context(), [*EXPECTED, held])

    assert [row["act_id"] for row in seen["local_acts"]] == ["act_1"]
    assert (
        "physical-act partition excludes 1 held act(s) from local_expected_count: act_held"
        in capsys.readouterr().err
    )


def test_the_production_source_ledger_contains_only_exemplar_verified_captures():
    context = _Context()
    context.run["source_manifest"].append({"sha256": B})
    assert logical_reading._verified_source_ledger(context) == {A}


def test_two_rendered_pages_from_one_capture_form_one_view_without_losing_evidence():
    context = _Context()
    bases = [
        {
            "source_page_id": page_id,
            "image_path": f"crop-{page_id}",
            "image_sha256": digest_bytes(f"crop-{page_id}".encode()),
        }
        for page_id in ("pg_1", "pg_2")
    ]
    renders = [
        {
            "source_page_id": page_id,
            "image_path": f"render-{page_id}",
            "image_sha256": digest_bytes(f"render-{page_id}".encode()),
        }
        for page_id in ("pg_1", "pg_2")
    ]
    record = logical_reading.act_autopsia(
        context,
        logical_act_id="act_1",
        partition_ref=REF,
        act={"act_id": "act_1"},
        bases=bases,
        page_renders=renders,
    )
    assert record["required_capture_sha256s"] == [A]
    (one_view,) = record["views"]
    assert one_view["page_ids"] == ["pg_1", "pg_2"]
    assert len(one_view["region_refs"]) == 2
    assert len(one_view["page_render_refs"]) == 2


def test_instrument_sampling_is_keyed_only_by_logical_act(monkeypatch):
    seen = []

    def nuda_sampled(subject, **_kwargs):
        seen.append(("nuda", subject))
        return True

    def control_sampled(subject, **_kwargs):
        seen.append(("control", subject))
        return False

    monkeypatch.setattr(perlector_run.nuda, "is_nuda_sampled", nuda_sampled)
    monkeypatch.setattr(perlector_run.protocol, "is_control_sampled", control_sampled)

    class SamplingContext:
        tree = type("Tree", (), {"run_id": "r"})()
        run = {
            "corpus_frame_membership": {
                "frame_digest": A,
                "page_digest": B,
                "seed": "s",
            }
        }
        nuda_per_mille = 1
        perlector_instrument_per_mille = 1

    assert perlector_run._logical_sampling_decisions(SamplingContext(), "pac_shared") == (
        True,
        False,
    )
    assert seen == [("nuda", "pac_shared"), ("control", "pac_shared")]
