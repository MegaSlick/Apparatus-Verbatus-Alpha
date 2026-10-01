import time
from collections.abc import Mapping
from copy import deepcopy

import manifest as manifest_module
import pytest
from manifest import (
    CLUSTER_SCHEMA,
    MANIFEST_SCHEMA,
    MAX_CLUSTER_MEMBERS,
    MAX_CLUSTER_RECORDS,
    MAX_MANIFEST_ROWS,
    MAX_SPLIT_PARTS,
    derivative_page_backlink,
    make_row,
    make_split,
    validate_cluster_record,
    validate_manifest,
    verify_submitted_frame,
)
from manifest import (
    make_part as contract_make_part,
)

from common.contracts.canonical import digest_bytes
from common.contracts.errors import ContractError, SchemaRefusal
from common.contracts.stages import TRIAGE_MODES

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64
ACTOR = {"kind": "model", "identity": "triage-model", "revision": "r17"}
CORPUS_ID = "montebello"


def make_part(region, crop_box, rotation_millidegrees, *, colour_mode="keep"):
    return contract_make_part(
        region,
        crop_box,
        rotation_millidegrees,
        colour_mode=colour_mode,
    )


WHOLE_FRAME = [make_part({"x": 0, "y": 0, "w": 10, "h": 8}, {"x": 1, "y": 1, "w": 8, "h": 6}, 0)]


def row(**changes):
    colour_mode = changes.pop("colour_mode", "keep")
    values = {
        "corpus_id": CORPUS_ID,
        "source_frame_sha256": DIGEST_A,
        "frame": {"width": 10, "height": 8},
        "split": make_split(deepcopy(WHOLE_FRAME)),
        "re_shoot_cluster_id": None,
        "confidence": 4,
        "mode": "semi",
        "actor": ACTOR,
        "human_override": False,
    }
    for part_record in values["split"]["parts"]:
        part_record["colour_mode"] = colour_mode
    values.update(changes)
    return make_row(**values)


def manifest(records, *, corpus_id=CORPUS_ID):
    return {"schema": MANIFEST_SCHEMA, "corpus_id": corpus_id, "records": records}


def test_every_field_round_trips_and_a_derivative_links_to_the_row():
    source = row(colour_mode="rgb", confidence=2, human_override=True)
    manifest_record = manifest([source])
    assert validate_manifest(deepcopy(manifest_record)) == manifest_record
    assert derivative_page_backlink(source, 0) == {
        "corpus_id": CORPUS_ID,
        "source_frame_sha256": DIGEST_A,
        "triage_manifest_row_sha256": source["manifest_row_sha256"],
        "triage_part_index": 0,
    }


@pytest.mark.parametrize(
    "field,value,match",
    [
        ("confidence", 5, "outside the closed ordinal"),
        ("mode", "automatic", "triage mode is not one of"),
        ("human_override", "yes", "human_override must be present and boolean"),
        (
            "actor",
            {"kind": "model", "identity": "x"},
            "closed kind/identity/revision record",
        ),
        (
            "actor",
            {"kind": "robot", "identity": "x", "revision": "r1"},
            "actor kind must be one of",
        ),
        (
            "actor",
            {"kind": "model", "identity": "   ", "revision": "r1"},
            "identity must be a non-blank resolved name",
        ),
        ("actor", {"kind": "model", "identity": "x", "revision": ""}, "resolved model revision"),
        ("actor", {"kind": "model", "identity": "x", "revision": None}, "resolved model revision"),
        (
            "actor",
            {"kind": "human", "identity": "operator", "revision": "n/a"},
            "human triage actor carries no revision",
        ),
    ],
)
def test_required_provenance_and_closed_values_are_refused(field, value, match):
    """Pinned to the reason, as the geometry-convention cases below already are.

    Each case mutates a row `make_row` already sealed, so `manifest_row_sha256` no
    longer binds the payload and `validate_row` has a second reason to refuse it;
    the specific match is what shows the named guard itself refused the row. The
    confidence and mode fields decide which frames go to human review."""
    values = row()
    values[field] = value
    with pytest.raises(ContractError, match=match):
        validate_manifest(manifest([values]))


def test_a_human_actor_is_recorded_without_inventing_a_revision():
    # The resolved revision belongs to a *model*. A person has none, and the schema
    # says so with null rather than accepting a placeholder string.
    human = row(actor={"kind": "human", "identity": "operator", "revision": None})
    assert validate_manifest(manifest([human]))


def test_offline_producer_actor_requires_a_resolved_identity_and_revision():
    produced = row(actor={"kind": "producer", "identity": "triage-instrument", "revision": "v1"})
    assert validate_manifest(manifest([produced]))
    for actor in (
        {"kind": "producer", "identity": "", "revision": "v1"},
        {"kind": "producer", "identity": "triage-instrument", "revision": ""},
        {"kind": "producer", "identity": "triage-instrument", "revision": None},
    ):
        with pytest.raises(ContractError):
            validate_manifest(manifest([row(actor=actor)]))


def test_a_non_utf8_actor_identity_stays_inside_the_typed_refusal_algebra():
    # `canonical_bytes` turns the lone surrogate into TypeError, so this proves the
    # crossing into `SchemaRefusal`, not a Unicode-specific branch of `_row_digest`.
    malformed = row()
    malformed["actor"] = {"kind": "producer", "identity": "\ud800", "revision": "v1"}
    with pytest.raises(SchemaRefusal, match="cannot be canonically serialized"):
        validate_manifest(manifest([malformed]))


def test_missing_mode_actor_or_override_is_refused():
    for field in ("mode", "actor", "human_override"):
        values = row()
        del values[field]
        with pytest.raises(ContractError, match="closed decision-manifest"):
            validate_manifest(manifest([values]))


def test_missing_per_part_decisions_are_refused_instead_of_defaulted():
    for field in ("crop_box", "rotation", "colour_mode"):
        values = row()
        del values["split"]["parts"][0][field]
        with pytest.raises(ContractError, match="closed region/crop_box/rotation/colour_mode"):
            validate_manifest(manifest([values]))


def test_no_winner_field_can_be_added_to_a_cluster_row():
    values = row(re_shoot_cluster_id="opening-35")
    values["winner"] = True
    with pytest.raises(ContractError, match="no winner field"):
        validate_manifest(manifest([values]))

    record = cluster([DIGEST_A, DIGEST_B])
    record["winner"] = DIGEST_A
    with pytest.raises(ContractError, match="closed corpus-scoped schema"):
        validate_manifest(manifest([]), {"opening-35": record})


def test_a_malformed_row_raises_the_typed_schema_refusal_caught_by_recorders():
    # `errors.py` exists so a stage can catch what it means to catch, and the
    # pipeline's live recorders catch `SchemaRefusal`. A refusal raised as the
    # bare base class sails past every one of them.
    with pytest.raises(SchemaRefusal):
        validate_manifest(manifest([{"nonsense": 1}]))


def test_a_cyclic_row_value_is_a_typed_refusal_not_a_recursion_crash():
    cycle = {}
    cycle["identity"] = cycle
    with pytest.raises(SchemaRefusal, match="cannot be canonically serialized"):
        row(actor={"kind": "model", "identity": cycle, "revision": "r17"})


def test_split_must_partition_its_frame_and_cluster_must_contain_the_frame():
    bad = row()
    bad["split"] = make_split(
        [make_part({"x": 0, "y": 0, "w": 9, "h": 8}, {"x": 0, "y": 0, "w": 9, "h": 8}, 0)]
    )
    with pytest.raises(ContractError, match="does not partition"):
        validate_manifest(manifest([bad]))
    named = row(re_shoot_cluster_id="opening-35")
    with pytest.raises(ContractError, match="does not contain"):
        validate_manifest(manifest([named]), {})


def test_overlapping_split_parts_do_not_partition_the_frame():
    # Total area alone accepts this: the overlap at x=4 and the gap at x=9 are
    # both one column, so 5*8 + 5*8 still equals the frame's 80 pixels.
    overlap = row()
    overlap["split"] = make_split(
        [
            make_part({"x": 0, "y": 0, "w": 5, "h": 8}, {"x": 0, "y": 0, "w": 5, "h": 8}, 0),
            make_part({"x": 4, "y": 0, "w": 5, "h": 8}, {"x": 0, "y": 0, "w": 5, "h": 8}, 0),
        ]
    )
    with pytest.raises(ContractError, match="overlap"):
        validate_manifest(manifest([overlap]))


@pytest.mark.parametrize(
    "target,field,value,match",
    [
        ("region", "x", -1, "outside its frame"),
        ("region", "w", 0, "degenerate"),
        ("crop_box", "y", -1, "outside its own split part"),
        ("crop_box", "h", 0, "degenerate"),
    ],
)
def test_negative_coordinates_and_zero_area_parts_are_refused(target, field, value, match):
    bad = row()
    bad["split"]["parts"][0][target][field] = value
    with pytest.raises(ContractError, match=match):
        validate_manifest(manifest([bad]))


def test_a_shared_edge_is_disjoint_and_an_exact_one_part_split_is_not_degenerate():
    one = row()
    assert validate_manifest(manifest([one]))
    touching = row(
        split=make_split(
            [
                make_part(
                    {"x": 0, "y": 0, "w": 5, "h": 8},
                    {"x": 0, "y": 0, "w": 5, "h": 8},
                    0,
                ),
                make_part(
                    {"x": 5, "y": 0, "w": 5, "h": 8},
                    {"x": 0, "y": 0, "w": 5, "h": 8},
                    0,
                ),
            ]
        )
    )
    assert validate_manifest(manifest([touching]))


def test_a_degenerate_one_part_split_cannot_hide_a_gap():
    partial = row()
    partial["split"] = make_split(
        [
            make_part(
                {"x": 0, "y": 0, "w": 10, "h": 7},
                {"x": 0, "y": 0, "w": 10, "h": 7},
                0,
            )
        ]
    )
    with pytest.raises(ContractError, match="does not partition"):
        validate_manifest(manifest([partial]))


def test_each_split_part_carries_its_own_crop_and_deskew():
    # The unit's own structural case: two surfaces at different angles, which one
    # frame-level rotation cannot express.
    taped = row(
        split=make_split(
            [
                make_part({"x": 0, "y": 0, "w": 5, "h": 8}, {"x": 0, "y": 1, "w": 5, "h": 6}, 900),
                make_part(
                    {"x": 5, "y": 0, "w": 5, "h": 8}, {"x": 1, "y": 0, "w": 4, "h": 8}, -2500
                ),
            ]
        )
    )
    assert validate_manifest(manifest([taped]))
    assert [part["rotation"]["rotation_millidegrees"] for part in taped["split"]["parts"]] == [
        900,
        -2500,
    ]


def test_a_crop_box_outside_its_own_split_part_is_refused():
    straddling = row()
    straddling["split"] = make_split(
        [
            make_part({"x": 0, "y": 0, "w": 5, "h": 8}, {"x": 4, "y": 0, "w": 4, "h": 8}, 0),
            make_part({"x": 5, "y": 0, "w": 5, "h": 8}, {"x": 0, "y": 0, "w": 5, "h": 8}, 0),
        ]
    )
    with pytest.raises(ContractError, match="outside its own split part"):
        validate_manifest(manifest([straddling]))


def test_coordinate_spaces_and_crop_then_rotate_conventions_are_closed_in_the_schema():
    explicit = row(
        split=make_split(
            [
                make_part(
                    {"x": 0, "y": 0, "w": 5, "h": 8},
                    {"x": 0, "y": 0, "w": 5, "h": 8},
                    1250,
                ),
                make_part(
                    {"x": 5, "y": 0, "w": 5, "h": 8},
                    {"x": 1, "y": 1, "w": 4, "h": 6},
                    -300,
                ),
            ]
        )
    )
    assert validate_manifest(manifest([explicit]))
    second = explicit["split"]["parts"][1]
    assert second["region"]["space"] == "frame"
    assert second["crop_box"] == {"space": "part", "x": 1, "y": 1, "w": 4, "h": 6}
    assert second["rotation"] == {
        "rotation_millidegrees": -300,
        "direction": "clockwise",
        "origin": "crop-centre",
        "canvas": "expand",
    }


@pytest.mark.parametrize(
    "path,value,match",
    [
        (("operation_order",), "region-rotate-crop", "operation_order"),
        (("parts", 0, "region", "space"), "part", "frame coordinates"),
        (("parts", 0, "crop_box", "space"), "frame", "part coordinates"),
        (("parts", 0, "rotation", "direction"), "counterclockwise", "clockwise"),
        (("parts", 0, "rotation", "origin"), "frame-origin", "crop-centre"),
        (("parts", 0, "rotation", "canvas"), "clip", "expand"),
    ],
)
def test_no_geometry_convention_can_be_changed_without_a_refusal(path, value, match):
    bad = row()
    target = bad["split"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ContractError, match=match):
        validate_manifest(manifest([bad]))


def test_colour_mode_is_per_split_page_not_ambiguous_at_frame_level():
    mixed = row(
        split=make_split(
            [
                make_part(
                    {"x": 0, "y": 0, "w": 5, "h": 8},
                    {"x": 0, "y": 0, "w": 5, "h": 8},
                    0,
                    colour_mode="rgb",
                ),
                make_part(
                    {"x": 5, "y": 0, "w": 5, "h": 8},
                    {"x": 0, "y": 0, "w": 5, "h": 8},
                    0,
                    colour_mode="bitonal",
                ),
            ]
        )
    )
    assert validate_manifest(manifest([mixed]))
    bad = row()
    bad["split"]["parts"][0]["colour_mode"] = "sepia"
    with pytest.raises(ContractError, match="part colour_mode"):
        validate_manifest(manifest([bad]))


def test_a_derivative_backlink_must_name_the_exact_part_of_its_row():
    split = row(
        split=make_split(
            [
                make_part(
                    {"x": 0, "y": 0, "w": 5, "h": 8},
                    {"x": 0, "y": 0, "w": 5, "h": 8},
                    0,
                ),
                make_part(
                    {"x": 5, "y": 0, "w": 5, "h": 8},
                    {"x": 0, "y": 0, "w": 5, "h": 8},
                    0,
                ),
            ]
        )
    )
    assert derivative_page_backlink(split, 0)["triage_part_index"] == 0
    assert derivative_page_backlink(split, 1)["triage_part_index"] == 1
    for invalid in (-1, 2, True):
        with pytest.raises(ContractError, match="does not name a split part"):
            derivative_page_backlink(split, invalid)


def test_partition_validation_does_not_scale_with_the_pixels_of_a_master():
    # An archival master is tens of megapixels and a parish set runs past 2,000
    # frames. A partition check that enumerates pixels costs seconds and gigabytes
    # per row; this frame has 400 million of them and must still validate at once.
    started = time.monotonic()
    huge = row(
        frame={"width": 20_000, "height": 20_000},
        split=make_split(
            [
                make_part(
                    {"x": 0, "y": 0, "w": 10_000, "h": 20_000},
                    {"x": 5, "y": 5, "w": 9_000, "h": 19_000},
                    0,
                ),
                make_part(
                    {"x": 10_000, "y": 0, "w": 10_000, "h": 20_000},
                    {"x": 5, "y": 5, "w": 9_000, "h": 19_000},
                    0,
                ),
            ]
        ),
    )
    assert validate_manifest(manifest([huge]))
    assert time.monotonic() - started < 10


def cluster(members, split_count=1, cluster_id="opening-35"):
    return {
        "schema": CLUSTER_SCHEMA,
        "corpus_id": "montebello",
        "cluster_id": cluster_id,
        "member_frame_sha256": members,
        "split_count": split_count,
    }


def test_cluster_is_corpus_scoped_and_refuses_incompatible_split_counts_across_runs():
    record = cluster([DIGEST_A, DIGEST_B])
    first_run = row(re_shoot_cluster_id="opening-35")
    second_run = row(source_frame_sha256=DIGEST_B, re_shoot_cluster_id="opening-35")
    validate_manifest(manifest([first_run]), {"opening-35": record})
    validate_manifest(manifest([second_run]), {"opening-35": record})
    split = row(
        source_frame_sha256=DIGEST_B,
        re_shoot_cluster_id="opening-35",
        split=make_split(
            [
                make_part({"x": 0, "y": 0, "w": 5, "h": 8}, {"x": 0, "y": 0, "w": 5, "h": 8}, 0),
                make_part({"x": 5, "y": 0, "w": 5, "h": 8}, {"x": 0, "y": 0, "w": 5, "h": 8}, 0),
            ]
        ),
    )
    with pytest.raises(ContractError, match="incompatible split counts"):
        validate_manifest(manifest([split]), {"opening-35": record})


def test_a_cluster_record_carries_no_run_or_shard_scoped_field():
    # A cluster record survives being read from two different runs because its
    # closed field set holds nothing run- or shard-scoped; a field added to that
    # set must be named here.
    assert manifest_module._CLUSTER_FIELDS == {
        "schema",
        "corpus_id",
        "cluster_id",
        "member_frame_sha256",
        "split_count",
    }
    record = cluster([DIGEST_A, DIGEST_B])
    validate_manifest(manifest([]), {"opening-35": record})
    strayed = dict(record, run_id="run-1")
    with pytest.raises(ContractError, match="closed corpus-scoped schema"):
        validate_manifest(manifest([]), {"opening-35": strayed})


def test_cluster_members_may_span_corpus_frame_shards():
    # A corpus frame is capped at 1,000 pages and parish sets run past 2,000, so a
    # re-shoot cluster's members routinely land in different shards and therefore
    # different manifests. Each shard's manifest validates against the whole
    # cluster while holding only its own member.
    record = cluster([DIGEST_A, DIGEST_B, DIGEST_C])
    for member in (DIGEST_A, DIGEST_B, DIGEST_C):
        shard = row(source_frame_sha256=member, re_shoot_cluster_id="opening-35")
        validate_manifest(manifest([shard]), {"opening-35": record})


def test_a_manifest_naming_a_cluster_is_refused_without_the_cluster_records():
    # Skipping the reference check because a caller passed no context would let a
    # dangling cluster id through under a successful validation.
    named = row(re_shoot_cluster_id="opening-35")
    with pytest.raises(ContractError, match="cannot be validated without"):
        validate_manifest(manifest([named]))
    unclustered = row()
    assert validate_manifest(manifest([unclustered]))


def test_a_cluster_record_filed_under_another_cluster_id_is_refused():
    misfiled = cluster([DIGEST_A, DIGEST_B], cluster_id="opening-36")
    named = row(re_shoot_cluster_id="opening-35")
    with pytest.raises(ContractError, match="filed under a different cluster id"):
        validate_manifest(manifest([named]), {"opening-35": misfiled})


def test_malformed_cluster_inputs_stay_inside_the_schema_refusal_algebra():
    malformed_member = cluster([DIGEST_A, []])
    with pytest.raises(SchemaRefusal, match="frame source digests"):
        validate_manifest(manifest([]), {"opening-35": malformed_member})
    with pytest.raises(SchemaRefusal, match="supplied as a mapping"):
        validate_manifest(manifest([]), [malformed_member])


def test_manifest_rows_and_cluster_records_cannot_cross_corpus_scope():
    source = row()
    with pytest.raises(ContractError, match="row from a different corpus"):
        validate_manifest(manifest([source], corpus_id="another-corpus"))

    without_corpus = manifest([source])
    del without_corpus["corpus_id"]
    with pytest.raises(ContractError, match="schema/corpus_id/records"):
        validate_manifest(without_corpus)

    named = row(re_shoot_cluster_id="opening-35")
    wrong_corpus = cluster([DIGEST_A, DIGEST_B])
    wrong_corpus["corpus_id"] = "another-corpus"
    with pytest.raises(ContractError, match="different corpus"):
        validate_manifest(manifest([named]), {"opening-35": wrong_corpus})


def test_digest_must_match_submitted_bytes():
    data = b"frame bytes"
    bound = row(source_frame_sha256=digest_bytes(data))
    verify_submitted_frame(bound, data)
    with pytest.raises(ContractError, match="does not match submitted bytes"):
        verify_submitted_frame(bound, b"other frame")


class _DigestSwitchingRow(Mapping):
    """Expose one digest while copied and another if the source is read again."""

    def __init__(self, source, replacement_digest):
        self.source = source
        self.replacement_digest = replacement_digest
        self.digest_reads = 0

    def __getitem__(self, key):
        if key == "source_frame_sha256":
            self.digest_reads += 1
            if self.digest_reads > 1:
                return self.replacement_digest
        return self.source[key]

    def __iter__(self):
        return iter(self.source)

    def __len__(self):
        return len(self.source)


def test_submitted_frame_uses_the_digest_from_the_validated_snapshot():
    original = b"original frame"
    replacement = b"replacement frame"
    switching = _DigestSwitchingRow(
        row(source_frame_sha256=digest_bytes(original)), digest_bytes(replacement)
    )
    with pytest.raises(ContractError, match="does not match submitted bytes"):
        verify_submitted_frame(switching, replacement)
    assert switching.digest_reads == 1


def test_submitted_frame_refuses_a_non_bytes_boundary_value():
    with pytest.raises(SchemaRefusal, match="must be bytes"):
        verify_submitted_frame(row(), "not bytes")


def test_a_manifest_with_two_rows_for_one_frame_is_refused():
    """One frame may not carry two geometries inside one shard."""
    first = row()
    second = row(
        split=make_split(
            [
                make_part({"x": 0, "y": 0, "w": 5, "h": 8}, {"x": 0, "y": 0, "w": 5, "h": 8}, 0),
                make_part({"x": 5, "y": 0, "w": 5, "h": 8}, {"x": 0, "y": 0, "w": 5, "h": 8}, 0),
            ]
        )
    )
    with pytest.raises(SchemaRefusal, match="more than one row for a submitted frame"):
        validate_manifest(manifest([first, second]))


def test_a_cluster_member_whose_own_row_names_no_cluster_is_refused():
    """Membership holds both ways, or the member drops out of every cluster check."""
    records = {"opening-35": cluster([DIGEST_A, DIGEST_B])}
    with pytest.raises(SchemaRefusal, match="own triage row does not name it"):
        validate_manifest(manifest([row()]), records)


def test_a_frame_listed_in_two_cluster_records_is_refused():
    records = {
        "opening-35": cluster([DIGEST_A, DIGEST_B]),
        "opening-36": cluster([DIGEST_A, DIGEST_C], cluster_id="opening-36"),
    }
    with pytest.raises(SchemaRefusal, match="member of more than one re-shoot cluster"):
        validate_manifest(manifest([]), records)


def test_contract_counts_are_bounded_before_their_work_can_amplify():
    too_many_parts = [
        make_part({"x": index, "y": 0, "w": 1, "h": 1}, {"x": 0, "y": 0, "w": 1, "h": 1}, 0)
        for index in range(MAX_SPLIT_PARTS + 1)
    ]
    # "before its row is serialized" specifically: `make_row` guards the count
    # before it derives the digest and `_validate_split` guards it again afterwards;
    # the early guard is the one that keeps quadratic work off untrusted input.
    with pytest.raises(
        SchemaRefusal, match=f"{MAX_SPLIT_PARTS}-part limit before its row is serialized"
    ):
        row(
            frame={"width": MAX_SPLIT_PARTS + 1, "height": 1},
            split=make_split(too_many_parts),
        )

    one = row()
    with pytest.raises(SchemaRefusal, match=f"{MAX_MANIFEST_ROWS}-row shard limit"):
        validate_manifest(manifest([one] * (MAX_MANIFEST_ROWS + 1)))

    members = [f"{index:064x}" for index in range(MAX_CLUSTER_MEMBERS + 1)]
    with pytest.raises(SchemaRefusal, match=f"{MAX_CLUSTER_MEMBERS}-member limit"):
        validate_cluster_record(cluster(members))

    records = {
        f"cluster-{index}": cluster([DIGEST_A, DIGEST_B], cluster_id=f"cluster-{index}")
        for index in range(MAX_CLUSTER_RECORDS + 1)
    }
    with pytest.raises(SchemaRefusal, match=f"{MAX_CLUSTER_RECORDS}-record limit"):
        validate_manifest(manifest([]), records)


def test_the_mode_triple_is_the_shared_vocabulary_not_a_private_one():
    assert TRIAGE_MODES == ("manual", "semi", "auto")
    for mode in TRIAGE_MODES:
        assert validate_manifest(manifest([row(mode=mode)]))
