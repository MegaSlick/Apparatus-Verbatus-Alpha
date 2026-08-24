"""Synthetic contract tests for the Unit 6B producer and confirmation path."""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from common.contracts.canonical import canonical_bytes, digest_bytes, digest_of
from common.contracts.errors import IncompatibleReuse, SchemaRefusal
from common.contracts.identities import physical_page_id
from common.corpus_register import append_records, members_of, register_digest
from operations.triage import instrument
from operations.triage import producer as producer_module
from operations.triage.producer import (
    CONFIRMATION_SCHEMA,
    ProducerRefusal,
    SubmittedFrame,
    append_confirmation_to_register,
    commit_confirmed_production,
    produce,
    routes_to_review,
    triage_manifest,
)


def frame(
    name: str, colour: int | None = None, *, size: tuple[int, int] = (64, 48)
) -> SubmittedFrame:
    if colour is None:
        colour = 80 + sum(name.encode("utf-8")) % 160
    image = Image.new("L", size, colour)
    encoded = BytesIO()
    image.save(encoded, format="PNG")
    return SubmittedFrame(name, encoded.getvalue())


def build_evidence(frames: list[SubmittedFrame]) -> tuple[dict, dict, list[dict]]:
    """Run the real Unit 6A instrument so a confirmation traces to genuine evidence."""
    config = instrument.load_config()
    proxies = [instrument.build_proxies_from_bytes(item.data, config) for item in frames]
    evidence, manifest = instrument.candidate_evidence(proxies, config)
    return instrument.producer_recipe(config), manifest, evidence


def confirmation(
    frames: list[SubmittedFrame], *, include_second_page: bool = True
) -> tuple[dict, dict, dict, list[dict]]:
    """Build a closed confirmation together with the real evidence it traces to."""
    recipe, manifest, evidence = build_evidence(frames)
    digests = sorted(digest_bytes(item.data) for item in frames)
    pages = [{"volume_id": "v1", "designation": "opening-31-left", "member_frame_sha256": digests}]
    if include_second_page:
        pages.append(
            {
                "volume_id": "v1",
                "designation": "opening-31-right",
                "member_frame_sha256": [digests[-1]],
            }
        )
    confirmed = {
        "schema": CONFIRMATION_SCHEMA,
        "corpus_id": "synthetic",
        "appending_run": "triage-pass-synthetic-1",
        "authority": {"kind": "fixture", "identity": "synthetic-fixture", "revision": "v1"},
        "instrument_config_sha256": manifest["instrument_config_sha256"],
        "evidence_manifest_sha256": digest_of(manifest),
        "clusters": [{"pages": pages, "evidence_pairs": [digests[:2]]}],
    }
    return confirmed, recipe, manifest, evidence


def test_producer_has_exact_coverage_and_a_full_frame_fallback_for_every_submission():
    frames = [frame("62"), frame("63"), frame("64")]
    produced = produce(frames, corpus_id="synthetic", mode="auto")
    expected = {digest_bytes(item.data) for item in frames}
    assert {row["source_frame_sha256"] for row in produced.manifest["records"]} == expected
    assert len(produced.manifest["records"]) == len(frames)
    assert all(row["confidence"] == 0 for row in produced.manifest["records"])
    assert all(row["actor"]["kind"] == "producer" for row in produced.manifest["records"])
    assert all(
        row["split"]["parts"][0]["colour_mode"] == "keep" for row in produced.manifest["records"]
    )


def test_modes_keep_rows_byte_identical_and_only_change_routing(tmp_path: Path):
    specimen = frame("63")
    manual = produce([specimen], corpus_id="synthetic", mode="manual").manifest["records"][0]
    semi = produce([specimen], corpus_id="synthetic", mode="semi").manifest["records"][0]
    auto = produce([specimen], corpus_id="synthetic", mode="auto").manifest["records"][0]
    # Mode is the declared routing fact; geometry/provenance/digest stay identical.
    for left, right in ((manual, semi), (semi, auto)):
        assert {
            key: value for key, value in left.items() if key not in {"mode", "manifest_row_sha256"}
        } == {
            key: value for key, value in right.items() if key not in {"mode", "manifest_row_sha256"}
        }
    local = tmp_path / "triage_modes.toml"
    local.write_text(
        "[manual]\nreview_at_or_below_confidence = 4\n[semi]\nreview_at_or_below_confidence = 2\n[auto]\nreview_at_or_below_confidence = 0\n",
        encoding="utf-8",
    )
    assert [routes_to_review(row, local) for row in (manual, semi, auto)] == [True, True, True]
    # Raise confidence only in this routing probe: generated producer rows are always 0.
    probe = {**auto, "confidence": 2}
    assert routes_to_review({**probe, "mode": "manual"}, local)
    assert routes_to_review({**probe, "mode": "semi"}, local)
    assert not routes_to_review({**probe, "mode": "auto"}, local)


def test_review_routing_refuses_an_incomplete_row_and_an_open_configuration(tmp_path: Path):
    config = tmp_path / "triage_modes.toml"
    config.write_text(
        "[manual]\nreview_at_or_below_confidence = 4\n"
        "[semi]\nreview_at_or_below_confidence = 4\n"
        "[auto]\nreview_at_or_below_confidence = 4\n",
        encoding="utf-8",
    )
    with pytest.raises(ProducerRefusal, match="no mode or confidence"):
        routes_to_review({"mode": "auto"}, config)
    config.write_text(
        config.read_text(encoding="utf-8") + "\n[extra]\nvalue = 1\n", encoding="utf-8"
    )
    with pytest.raises(ProducerRefusal, match="wrong closed schema"):
        routes_to_review({"mode": "auto", "confidence": 0}, config)


def test_shipped_modes_route_every_produced_row_to_review():
    """All three modes, not just the one a produced row happens to carry.

    `routes_to_review` reads the threshold for the row's own mode, so a test that
    only produces auto rows proves only the auto column of the shipped config. The
    claim this unit ships on is that nothing is calibrated yet and *every* mode
    therefore routes everything to review.
    """
    config = Path(__file__).resolve().parents[2] / "config" / "triage_modes.toml"
    for mode in ("manual", "semi", "auto"):
        rows = produce([frame("62"), frame("63")], corpus_id="synthetic", mode=mode).manifest[
            "records"
        ]
        assert rows and all(row["mode"] == mode for row in rows)
        assert all(routes_to_review(row, config) for row in rows)
    # Not vacuous through the confidence-0 floor either: the shipped thresholds send
    # the whole closed ordinal range to review in every mode.
    probe = produce([frame("62")], corpus_id="synthetic", mode="auto").manifest["records"][0]
    for mode in ("manual", "semi", "auto"):
        for confidence in range(0, 5):
            assert routes_to_review({**probe, "mode": mode, "confidence": confidence}, config)


def test_structural_candidate_cannot_create_a_link_without_confirmation():
    # The 63/65-shaped pair is a producer input only: candidate evidence lives in
    # the instrument and this confirmation-free pass has no cluster record at all.
    produced = produce([frame("63"), frame("65")], corpus_id="synthetic", mode="auto")
    assert produced.clusters == {}
    assert all(row["re_shoot_cluster_id"] is None for row in produced.manifest["records"])


def test_clean_transcription_passes_through_only_after_native_byte_binding():
    specimen = frame("clean")
    native = produce([specimen], corpus_id="synthetic", mode="auto").manifest["records"][0]
    transcribed = triage_manifest.make_row(
        **{
            **{key: value for key, value in native.items() if key != "manifest_row_sha256"},
            "actor": {
                "kind": "scantailor",
                "identity": "ScanTailor Advanced",
                "revision": "fixture-v0",
            },
        }
    )
    produced = produce(
        [specimen],
        corpus_id="synthetic",
        mode="auto",
        transcribed_rows_by_path={"clean": transcribed},
    )
    assert produced.manifest["records"] == [transcribed]


@pytest.mark.parametrize(
    "case,match",
    [
        ("duplicate", "manual refusal coverage"),
        ("wrong-corpus", "manual refusal wrong-corpus"),
        ("digest", "manual refusal digest-bytes-mismatch"),
        ("dimensions", "manual refusal frame-dimensions-mismatch"),
        ("keep", "manual refusal keep-over-lossy-mode"),
    ],
)
def test_named_whole_manifest_refusals(case: str, match: str):
    first, second = frame("63"), frame("64")
    if case == "duplicate":
        with pytest.raises(ProducerRefusal, match=match):
            produce([first, SubmittedFrame("copy", first.data)], corpus_id="synthetic", mode="auto")
        return
    produced = produce([first], corpus_id="synthetic", mode="auto")
    row = dict(produced.manifest["records"][0])
    if case == "wrong-corpus":
        row["corpus_id"] = "other"
    elif case == "digest":
        row["source_frame_sha256"] = digest_bytes(second.data)
    elif case == "dimensions":
        row["frame"] = {"width": 99, "height": 99}
        row["split"]["parts"][0]["region"]["w"] = 99
        row["split"]["parts"][0]["region"]["h"] = 99
        row["split"]["parts"][0]["crop_box"]["w"] = 99
        row["split"]["parts"][0]["crop_box"]["h"] = 99
    else:
        # A paletted PNG is lossless to the encoder; force the producer check with
        # a high-precision source and its otherwise-valid synthetic row.
        image = Image.new("I;16", (12, 8), 1024)
        encoded = BytesIO()
        image.save(encoded, format="PNG")
        high = SubmittedFrame("high", encoded.getvalue())
        high_row = produce([high], corpus_id="synthetic", mode="auto").manifest["records"][0]
        high_row["split"]["parts"][0]["colour_mode"] = "keep"
        high_row = triage_manifest.make_row(
            **{key: value for key, value in high_row.items() if key != "manifest_row_sha256"}
        )
        with pytest.raises(ProducerRefusal, match=match):
            produce(
                [high],
                corpus_id="synthetic",
                mode="auto",
                transcribed_rows_by_path={"high": high_row},
            )
        return
    row = triage_manifest.make_row(
        **{key: value for key, value in row.items() if key != "manifest_row_sha256"}
    )
    with pytest.raises(ProducerRefusal, match=match):
        produce([first], corpus_id="synthetic", mode="auto", transcribed_rows_by_path={"63": row})


def test_cluster_member_and_span_refusals_are_named():
    frames = [frame(str(index)) for index in range(4)]
    wrong_member, wrong_recipe, wrong_manifest, wrong_evidence = confirmation(frames[:2])
    wrong_member["clusters"][0]["pages"][0]["member_frame_sha256"].append("f" * 64)
    with pytest.raises(ProducerRefusal, match="manual refusal cluster-member-not-submitted"):
        produce(
            frames,
            corpus_id="synthetic",
            mode="auto",
            confirmation=wrong_member,
            instrument_recipe=wrong_recipe,
            evidence_manifest=wrong_manifest,
            evidence_records=wrong_evidence,
        )
    too_wide, wide_recipe, wide_manifest, wide_evidence = confirmation(frames)
    with pytest.raises(ProducerRefusal, match="manual refusal cluster-span-over-cap"):
        produce(
            frames,
            corpus_id="synthetic",
            mode="auto",
            confirmation=too_wide,
            instrument_recipe=wide_recipe,
            evidence_manifest=wide_manifest,
            evidence_records=wide_evidence,
            max_pages_per_shard=3,
        )


def test_confirmation_refuses_a_pair_the_instrument_never_evidenced():
    frames = [frame("63"), frame("64"), frame("65", size=(80, 48))]
    confirmed, recipe, manifest, evidence = confirmation(frames)
    # Unequal dimensions make this a named instrument refusal, not an emitted
    # evidence record. Keep the genuine manifest and all genuine emitted records;
    # only the confirmation attempts to promote the refused pair.
    confirmed["clusters"][0]["evidence_pairs"] = [manifest["dimension_refused_pairs"][0]]
    with pytest.raises(ProducerRefusal, match="manual refusal evidence-not-instrumented"):
        produce(
            frames,
            corpus_id="synthetic",
            mode="auto",
            confirmation=confirmed,
            instrument_recipe=recipe,
            evidence_manifest=manifest,
            evidence_records=evidence,
        )


def test_confirmation_accounting_refuses_boolean_record_count():
    frames = [frame("63"), frame("64")]
    confirmed, recipe, manifest, evidence = confirmation(frames)
    malformed = {**manifest, "emitted_evidence_records": True}
    confirmed = {**confirmed, "evidence_manifest_sha256": digest_of(malformed)}
    with pytest.raises(ProducerRefusal, match="must be a non-negative integer"):
        produce(
            frames,
            corpus_id="synthetic",
            mode="auto",
            confirmation=confirmed,
            instrument_recipe=recipe,
            evidence_manifest=malformed,
            evidence_records=evidence,
        )


def test_confirmation_refuses_an_incomplete_selector_manifest_even_for_a_retained_pair():
    """6B consumes 6A's conservation record, not only its emitted-pair digest.

    Simulate a selector silently dropping one of three submission-window pairs and
    then honestly closing its shorter emitted-pair list. The confirmation names a
    different, retained pair, so checking only ``emitted_pairs_sha256`` accepts an
    incomplete pass. The recorded window denominator is the independent defense.
    """
    frames = [frame("63"), frame("64"), frame("65")]
    confirmed, recipe, manifest, evidence = confirmation(frames)
    retained = evidence[:-1]
    incomplete = {**manifest, "candidate_cost": dict(manifest["candidate_cost"])}
    incomplete["candidate_cost"]["unique_candidate_pairs"] -= 1
    incomplete["emitted_evidence_records"] = len(retained)
    incomplete["emitted_pairs_sha256"] = digest_of(
        sorted(record["both_digests"] for record in retained)
    )
    confirmed = {
        **confirmed,
        "evidence_manifest_sha256": digest_of(incomplete),
        "clusters": [{**confirmed["clusters"][0], "evidence_pairs": [retained[0]["both_digests"]]}],
    }
    with pytest.raises(ProducerRefusal, match="selector silently dropped"):
        produce(
            frames,
            corpus_id="synthetic",
            mode="auto",
            confirmation=confirmed,
            instrument_recipe=recipe,
            evidence_manifest=incomplete,
            evidence_records=retained,
        )


def test_confirmation_checks_evidence_thresholds_against_the_recorded_recipe():
    frames = [frame("63"), frame("64")]
    confirmed, recipe, manifest, evidence = confirmation(frames)
    changed = {**recipe, "comparison_recipe": dict(recipe["comparison_recipe"])}
    changed["comparison_recipe"]["mean_tolerance"] += 1
    with pytest.raises(ProducerRefusal, match="valid closed"):
        produce(
            frames,
            corpus_id="synthetic",
            mode="auto",
            confirmation=confirmed,
            instrument_recipe=changed,
            evidence_manifest=manifest,
            evidence_records=evidence,
        )


def test_confirmation_refuses_an_evidence_manifest_from_a_different_pass():
    left_frames = [frame("63"), frame("64")]
    right_frames = [frame("70"), frame("71")]
    confirmed, recipe, _own_manifest, _own_evidence = confirmation(left_frames)
    _other_recipe, _other_manifest, other_evidence = build_evidence(right_frames)
    with pytest.raises(ProducerRefusal, match="manual refusal evidence-not-instrumented"):
        produce(
            left_frames,
            corpus_id="synthetic",
            mode="auto",
            confirmation=confirmed,
            instrument_recipe=recipe,
            evidence_manifest=_other_manifest,
            evidence_records=other_evidence,
        )


def test_confirmation_requires_evidence_when_a_confirmation_is_given():
    frames = [frame("63"), frame("64")]
    confirmed, _recipe, _manifest, _evidence = confirmation(frames)
    with pytest.raises(ProducerRefusal, match="evidence-not-instrumented"):
        produce(frames, corpus_id="synthetic", mode="auto", confirmation=confirmed)


def test_confirmation_refuses_an_empty_cluster_list_and_malformed_member_types():
    frames = [frame("63"), frame("64")]
    confirmed, recipe, manifest, evidence = confirmation(frames)
    empty = {**confirmed, "clusters": []}
    with pytest.raises(ProducerRefusal, match="names no cluster"):
        produce(
            frames,
            corpus_id="synthetic",
            mode="auto",
            confirmation=empty,
            instrument_recipe=recipe,
            evidence_manifest=manifest,
            evidence_records=evidence,
        )
    malformed = json.loads(json.dumps(confirmed))
    malformed["clusters"][0]["pages"][0]["member_frame_sha256"][-1] = 7
    with pytest.raises(ProducerRefusal, match="sorted unique source-frame"):
        produce(
            frames,
            corpus_id="synthetic",
            mode="auto",
            confirmation=malformed,
            instrument_recipe=recipe,
            evidence_manifest=manifest,
            evidence_records=evidence,
        )


def test_a_physical_page_designation_may_appear_in_only_one_confirmation_cluster():
    frames = [frame(str(index)) for index in range(4)]
    confirmed, recipe, manifest, evidence = confirmation(frames)
    digests = sorted(digest_bytes(item.data) for item in frames)
    confirmed["clusters"] = [
        {
            "pages": [
                {
                    "volume_id": "v1",
                    "designation": "shared-page",
                    "member_frame_sha256": digests[:2],
                },
                {
                    "volume_id": "v1",
                    "designation": "left-neighbour",
                    "member_frame_sha256": [digests[0]],
                },
            ],
            "evidence_pairs": [digests[:2]],
        },
        {
            "pages": [
                {
                    "volume_id": "v1",
                    "designation": "shared-page",
                    "member_frame_sha256": digests[2:],
                },
                {
                    "volume_id": "v1",
                    "designation": "right-neighbour",
                    "member_frame_sha256": [digests[3]],
                },
            ],
            "evidence_pairs": [digests[2:]],
        },
    ]
    with pytest.raises(ProducerRefusal, match="physical page cannot be assigned twice"):
        produce(
            frames,
            corpus_id="synthetic",
            mode="auto",
            confirmation=confirmed,
            instrument_recipe=recipe,
            evidence_manifest=manifest,
            evidence_records=evidence,
        )


def test_confirmation_writes_memberships_then_stable_cluster_without_a_preference(tmp_path: Path):
    frames = [frame("63"), frame("64"), frame("65")]
    confirmed, recipe, manifest, evidence = confirmation(frames)
    produced = produce(
        frames,
        corpus_id="synthetic",
        mode="auto",
        confirmation=confirmed,
        instrument_recipe=recipe,
        evidence_manifest=manifest,
        evidence_records=evidence,
    )
    assert len(produced.clusters) == 1
    cluster_id = next(iter(produced.clusters))
    register = tmp_path / "register.json"
    first_digest = append_confirmation_to_register(
        confirmed,
        produced,
        instrument_recipe=recipe,
        evidence_manifest=manifest,
        evidence_records=evidence,
        register_path=register,
    )
    register_bytes = register.read_bytes()
    left = confirmed["clusters"][0]["pages"][0]
    right = confirmed["clusters"][0]["pages"][1]
    left_id = next(item for item in produced.clusters.values())
    assert set(
        members_of(
            register_bytes, physical_page_id("synthetic", left["volume_id"], left["designation"])
        )
    ) == set(left["member_frame_sha256"])
    assert set(
        members_of(
            register_bytes, physical_page_id("synthetic", right["volume_id"], right["designation"])
        )
    ) == set(right["member_frame_sha256"])
    assert left_id["cluster_id"] == cluster_id
    # A fourth capture grows the membership chain but derives the same page-based id.
    fourth = frame("66")
    extended = frames + [fourth]
    confirmed2, recipe2, manifest2, evidence2 = confirmation(extended)
    confirmed2["appending_run"] = "triage-pass-synthetic-2"
    # The helper chooses the lexically last digest for the right page. Digest order
    # can change when a frame is added, but membership may only grow; carry the
    # earlier right-page capture into this genuinely extending confirmation.
    confirmed2["clusters"][0]["pages"][1]["member_frame_sha256"] = sorted(
        set(
            confirmed2["clusters"][0]["pages"][1]["member_frame_sha256"]
            + right["member_frame_sha256"]
        )
    )
    produced2 = produce(
        extended,
        corpus_id="synthetic",
        mode="auto",
        confirmation=confirmed2,
        instrument_recipe=recipe2,
        evidence_manifest=manifest2,
        evidence_records=evidence2,
    )
    assert set(produced2.clusters) == {cluster_id}
    append_confirmation_to_register(
        confirmed2,
        produced2,
        instrument_recipe=recipe2,
        evidence_manifest=manifest2,
        evidence_records=evidence2,
        register_path=register,
        expected_register_digest=first_digest,
    )


def test_no_designation_refuses_before_any_confirmation_output_is_written(tmp_path: Path):
    frames = [frame("63"), frame("64")]
    confirmed, recipe, manifest, evidence = confirmation(frames, include_second_page=False)
    confirmed["clusters"][0]["pages"] = []
    register = tmp_path / "register.json"
    with pytest.raises(ProducerRefusal, match="no physical-page designation"):
        commit_confirmed_production(
            frames,
            corpus_id="synthetic",
            mode="auto",
            confirmation=confirmed,
            instrument_recipe=recipe,
            evidence_manifest=manifest,
            evidence_records=evidence,
            register_path=register,
            manifest_path=tmp_path / "manifest.json",
            clusters_path=tmp_path / "clusters.json",
            authority_path=tmp_path / "authority.json",
        )
    assert not register.exists()
    assert sorted(tmp_path.iterdir()) == []


def test_confirmed_production_publishes_register_and_all_bound_documents(tmp_path: Path):
    frames = [frame("63"), frame("64")]
    confirmed, recipe, manifest, evidence = confirmation(frames)
    produced, successor = commit_confirmed_production(
        frames,
        corpus_id="synthetic",
        mode="auto",
        confirmation=confirmed,
        instrument_recipe=recipe,
        evidence_manifest=manifest,
        evidence_records=evidence,
        register_path=tmp_path / "register.json",
        manifest_path=tmp_path / "triage-manifest.json",
        clusters_path=tmp_path / "triage-clusters.json",
        authority_path=tmp_path / "triage-confirmation.json",
    )
    assert len(successor) == 64
    assert (tmp_path / "register.json").exists()
    assert (tmp_path / "triage-manifest.json").read_bytes() == canonical_bytes(produced.manifest)
    assert (tmp_path / "triage-clusters.json").read_bytes() == canonical_bytes(produced.clusters)
    # The authority that made a corpus-lifetime cluster is retained beside it: the
    # register's appending_run alone cannot say who claimed the write, or against
    # which instrument configuration and evidence manifest they claimed it.
    published = json.loads((tmp_path / "triage-confirmation.json").read_text(encoding="utf-8"))
    assert published == confirmed
    assert published["authority"] == confirmed["authority"]


def test_confirmation_authority_is_retained_before_a_register_append_is_attempted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    frames = [frame("63"), frame("64")]
    confirmed, recipe, manifest, evidence = confirmation(frames)
    authority_path = tmp_path / "authority.json"

    def refused_append(*args, **kwargs):
        assert authority_path.read_bytes() == canonical_bytes(confirmed)
        raise IncompatibleReuse("synthetic concurrent register append")

    monkeypatch.setattr(producer_module, "append_confirmation_to_register", refused_append)
    with pytest.raises(IncompatibleReuse, match="synthetic concurrent"):
        commit_confirmed_production(
            frames,
            corpus_id="synthetic",
            mode="auto",
            confirmation=confirmed,
            instrument_recipe=recipe,
            evidence_manifest=manifest,
            evidence_records=evidence,
            register_path=tmp_path / "register.json",
            manifest_path=tmp_path / "manifest.json",
            clusters_path=tmp_path / "clusters.json",
            authority_path=authority_path,
        )
    assert authority_path.read_bytes() == canonical_bytes(confirmed)
    assert not (tmp_path / "manifest.json").exists()
    assert not (tmp_path / "clusters.json").exists()


def test_authority_path_refuses_different_confirmation_bytes_without_overwrite(tmp_path: Path):
    frames = [frame("63"), frame("64")]
    confirmed, recipe, manifest, evidence = confirmation(frames)
    register_path = tmp_path / "register.json"
    manifest_path = tmp_path / "manifest.json"
    clusters_path = tmp_path / "clusters.json"
    authority_path = tmp_path / "authority.json"
    produced, head = commit_confirmed_production(
        frames,
        corpus_id="synthetic",
        mode="auto",
        confirmation=confirmed,
        instrument_recipe=recipe,
        evidence_manifest=manifest,
        evidence_records=evidence,
        register_path=register_path,
        manifest_path=manifest_path,
        clusters_path=clusters_path,
        authority_path=authority_path,
    )
    original_authority = authority_path.read_bytes()
    changed = {**confirmed, "appending_run": "another-confirmation-act"}
    with pytest.raises(ProducerRefusal, match="different immutable evidence"):
        commit_confirmed_production(
            frames,
            corpus_id="synthetic",
            mode="auto",
            confirmation=changed,
            instrument_recipe=recipe,
            evidence_manifest=manifest,
            evidence_records=evidence,
            register_path=register_path,
            expected_register_digest=head,
            manifest_path=manifest_path,
            clusters_path=clusters_path,
            authority_path=authority_path,
        )
    assert authority_path.read_bytes() == original_authority
    assert register_digest(register_path.read_bytes()) == head
    assert manifest_path.read_bytes() == canonical_bytes(produced.manifest)
    assert clusters_path.read_bytes() == canonical_bytes(produced.clusters)


def test_confirmed_commit_refuses_aliased_destinations_before_any_write(tmp_path: Path):
    frames = [frame("63"), frame("64")]
    confirmed, recipe, evidence_manifest, evidence = confirmation(frames)
    shared = tmp_path / "shared.json"
    with pytest.raises(ProducerRefusal, match="authority and manifest"):
        commit_confirmed_production(
            frames,
            corpus_id="synthetic",
            mode="auto",
            confirmation=confirmed,
            instrument_recipe=recipe,
            evidence_manifest=evidence_manifest,
            evidence_records=evidence,
            register_path=tmp_path / "register.json",
            manifest_path=shared,
            clusters_path=tmp_path / "clusters.json",
            authority_path=shared,
        )
    assert sorted(tmp_path.iterdir()) == []


def test_subset_confirmation_cannot_regress_a_grown_membership_or_door_cluster(tmp_path: Path):
    frames = [frame("63"), frame("64"), frame("65")]
    confirmed, recipe, manifest, evidence = confirmation(frames)
    register_path = tmp_path / "register.json"
    manifest_path = tmp_path / "manifest.json"
    clusters_path = tmp_path / "clusters.json"
    produced, head = commit_confirmed_production(
        frames,
        corpus_id="synthetic",
        mode="auto",
        confirmation=confirmed,
        instrument_recipe=recipe,
        evidence_manifest=manifest,
        evidence_records=evidence,
        register_path=register_path,
        manifest_path=manifest_path,
        clusters_path=clusters_path,
        authority_path=tmp_path / "authority-1.json",
    )
    prior_register = register_path.read_bytes()
    prior_manifest = manifest_path.read_bytes()
    prior_clusters = clusters_path.read_bytes()

    subset_frames = frames[:2]
    subset, subset_recipe, subset_manifest, subset_evidence = confirmation(subset_frames)
    subset["appending_run"] = "stale-subset"
    with pytest.raises(ProducerRefusal, match="stale-confirmation-membership"):
        commit_confirmed_production(
            subset_frames,
            corpus_id="synthetic",
            mode="auto",
            confirmation=subset,
            instrument_recipe=subset_recipe,
            evidence_manifest=subset_manifest,
            evidence_records=subset_evidence,
            register_path=register_path,
            expected_register_digest=head,
            manifest_path=manifest_path,
            clusters_path=clusters_path,
            authority_path=tmp_path / "authority-stale.json",
        )
    assert register_path.read_bytes() == prior_register
    assert manifest_path.read_bytes() == prior_manifest == canonical_bytes(produced.manifest)
    assert clusters_path.read_bytes() == prior_clusters == canonical_bytes(produced.clusters)


def test_crash_between_register_and_door_documents_converges_on_retry(tmp_path: Path):
    """A retry that observes the true post-crash register head republishes, not refuses."""
    frames = [frame("63"), frame("64")]
    confirmed, recipe, manifest, evidence = confirmation(frames)
    register_path = tmp_path / "register.json"
    manifest_path = tmp_path / "triage-manifest.json"
    clusters_path = tmp_path / "triage-clusters.json"
    authority_path = tmp_path / "triage-confirmation.json"
    produced, successor = commit_confirmed_production(
        frames,
        corpus_id="synthetic",
        mode="auto",
        confirmation=confirmed,
        instrument_recipe=recipe,
        evidence_manifest=manifest,
        evidence_records=evidence,
        register_path=register_path,
        manifest_path=manifest_path,
        clusters_path=clusters_path,
        authority_path=authority_path,
    )
    # Simulate a crash after the register append but before the Door documents were
    # durably published: the register already advanced, the documents never landed.
    manifest_path.unlink()
    clusters_path.unlink()
    authority_path.unlink()
    retried, retried_successor = commit_confirmed_production(
        frames,
        corpus_id="synthetic",
        mode="auto",
        confirmation=confirmed,
        instrument_recipe=recipe,
        evidence_manifest=manifest,
        evidence_records=evidence,
        register_path=register_path,
        expected_register_digest=successor,
        manifest_path=manifest_path,
        clusters_path=clusters_path,
        authority_path=authority_path,
    )
    assert retried_successor == successor
    assert manifest_path.read_bytes() == canonical_bytes(produced.manifest)
    assert clusters_path.read_bytes() == canonical_bytes(produced.clusters)
    assert authority_path.read_bytes() == canonical_bytes(confirmed)
    # A stale caller that never re-reads the register head still gets the ordinary
    # concurrent-write refusal rather than a silent duplicate or a false convergence.
    manifest_path.unlink()
    clusters_path.unlink()
    authority_path.unlink()
    with pytest.raises(IncompatibleReuse, match="the corpus register changed"):
        commit_confirmed_production(
            frames,
            corpus_id="synthetic",
            mode="auto",
            confirmation=confirmed,
            instrument_recipe=recipe,
            evidence_manifest=manifest,
            evidence_records=evidence,
            register_path=register_path,
            manifest_path=manifest_path,
            clusters_path=clusters_path,
            authority_path=authority_path,
        )


def test_retraction_and_confirmation_append_never_silently_resurrect_stale_membership(
    tmp_path: Path,
):
    """A retained historical membership is not the current register head.

    Retraction races are serialized by the register digest. The exact old
    confirmation cannot resurrect the act that was withdrawn, while a fresh
    confirmation act (a new appending-run identity) can append the same members
    again and republishes a manifest that agrees with the replayed register.
    """
    frames = [frame("63"), frame("64")]
    confirmed, recipe, manifest, evidence = confirmation(frames)
    register_path = tmp_path / "register.json"
    manifest_path = tmp_path / "triage-manifest.json"
    clusters_path = tmp_path / "triage-clusters.json"
    authority_path = tmp_path / "triage-confirmation.json"
    arguments = {
        "frames": frames,
        "corpus_id": "synthetic",
        "mode": "auto",
        "instrument_recipe": recipe,
        "evidence_manifest": manifest,
        "evidence_records": evidence,
        "register_path": register_path,
        "manifest_path": manifest_path,
        "clusters_path": clusters_path,
        "authority_path": authority_path,
    }
    produced, first_head = commit_confirmed_production(confirmation=confirmed, **arguments)
    page_id = physical_page_id("synthetic", "v1", "opening-31-left")
    register = json.loads(register_path.read_text(encoding="utf-8"))
    link = next(
        record
        for record in register["records"]
        if record["kind"] == "membership" and record["physical_page_id"] == page_id
    )
    retraction = {
        "kind": "retraction",
        "retracts": f"membership:{digest_of(link)}",
        "reason": "the confirmation joined two different blank forms",
        "appending_run": "triage-correction-1",
    }
    corrected_head = append_records(register_path, [retraction], expected_digest=first_head)
    assert members_of(register_path.read_bytes(), page_id) == []

    # A writer that raced the retraction observed the old head and is refused before
    # any Door document can be republished.
    with pytest.raises(IncompatibleReuse, match="the corpus register changed"):
        commit_confirmed_production(
            confirmation=confirmed,
            expected_register_digest=first_head,
            **arguments,
        )
    assert register_digest(register_path.read_bytes()) == corrected_head
    assert manifest_path.read_bytes() == canonical_bytes(produced.manifest)

    # Even after re-reading, replaying the *same* operator act cannot resurrect a
    # withdrawn immutable membership record.
    with pytest.raises(SchemaRefusal, match="repeats immutable record"):
        commit_confirmed_production(
            confirmation=confirmed,
            expected_register_digest=corrected_head,
            **arguments,
        )
    assert members_of(register_path.read_bytes(), page_id) == []

    reconfirmed = {**confirmed, "appending_run": "triage-pass-synthetic-2"}
    reconfirmed_authority = tmp_path / "triage-confirmation-2.json"
    republished, final_head = commit_confirmed_production(
        confirmation=reconfirmed,
        expected_register_digest=corrected_head,
        **{**arguments, "authority_path": reconfirmed_authority},
    )
    assert final_head != corrected_head
    assert set(members_of(register_path.read_bytes(), page_id)) == set(
        confirmed["clusters"][0]["pages"][0]["member_frame_sha256"]
    )
    assert manifest_path.read_bytes() == canonical_bytes(republished.manifest)
    assert clusters_path.read_bytes() == canonical_bytes(republished.clusters)
    assert authority_path.read_bytes() == canonical_bytes(confirmed)
    assert reconfirmed_authority.read_bytes() == canonical_bytes(reconfirmed)
