"""Acceptance coverage for the confined, podless ingest console path."""

from __future__ import annotations

import hashlib
import importlib
import inspect
import io
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

from common.contracts.canonical import canonical_bytes, digest_of
from operations.operator import cli, custody, ingest, ingest_worker
from operations.operator.errors import ERRORS, ErrorCode
from operations.operator.ingest_protocol import EXPECTED_DIGEST_FIELDS
from operations.submit import gate, inventory
from operations.triage import instrument
from operations.triage.producer import CONFIRMATION_SCHEMA

from .conftest import requires_host_boundary

ROOT = Path(__file__).resolve().parents[2]


def _distinct_png_bytes() -> bytes:
    """A valid, decodable PNG, the fixture pages' own size, matching none of them."""
    buffer = io.BytesIO()
    Image.new("L", (200, 260), color=17).save(buffer, format="PNG")
    return buffer.getvalue()


def _inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    approved = tmp_path / "approved"
    source = approved / "masters"
    output = approved / "ready"
    source.mkdir(parents=True)
    output.mkdir()
    for index in (1, 2, 3):
        fixture = ROOT / "proof" / "fixtures" / "synthetic-two-page-v0" / f"page-{index}.png"
        (source / f"page-{index}.png").write_bytes(fixture.read_bytes())
    policy = json.loads(gate.DEFAULT_POLICY_PATH.read_text(encoding="utf-8"))
    policy["storage_roots"] = [str(approved)]
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    return source, output, policy_path, approved


def _request(
    source: Path,
    output: Path,
    policy: Path,
    *,
    operation: str,
    expected_submission_manifest_sha256: str | None = None,
    expected_confirmation_sha256: str | None = None,
    expected_instrument_config_sha256: str | None = None,
    expected_data_handling_policy_sha256: str | None = None,
) -> dict[str, object]:
    return {
        "operation": operation,
        "source": str(source),
        "output_dir": str(output),
        "policy": str(policy),
        "corpus_id": "synthetic-console",
        "mode": "auto",
        "confirmation_file": None,
        "expected_submission_manifest_sha256": expected_submission_manifest_sha256,
        "expected_confirmation_sha256": expected_confirmation_sha256,
        "expected_instrument_config_sha256": expected_instrument_config_sha256,
        "expected_data_handling_policy_sha256": expected_data_handling_policy_sha256,
        "expected_output_device": None,
        "expected_output_inode": None,
    }


def _pinned_commit_request(
    source: Path, output: Path, policy: Path, preview_summary: dict[str, object]
) -> dict[str, object]:
    """A commit request pinned to exactly what a preceding preview showed."""
    request = _request(
        source,
        output,
        policy,
        operation="commit",
        expected_submission_manifest_sha256=preview_summary["submission_manifest_sha256"],
        expected_confirmation_sha256=preview_summary["confirmation_sha256"],
        expected_instrument_config_sha256=preview_summary["instrument_config_sha256"],
        expected_data_handling_policy_sha256=preview_summary["data_handling_policy_sha256"],
    )
    output_status = output.stat(follow_symlinks=False)
    request["expected_output_device"] = output_status.st_dev
    request["expected_output_inode"] = output_status.st_ino
    return request


def test_ingest_preview_builds_the_whole_submission_and_triage_plan_without_a_write(
    tmp_path: Path,
):
    source, output, policy, _approved = _inputs(tmp_path)

    prepared = ingest_worker._prepare(_request(source, output, policy, operation="preview"))
    summary = ingest_worker._summary(prepared)

    assert summary["submission_files"] == 3
    assert summary["candidate_count"] >= 1
    assert summary["confirmed_cluster_count"] == 0
    assert "submission-manifest.json" in summary["planned_files"]
    assert "triage-decision-manifest.json" in summary["planned_files"]
    assert not list(output.iterdir())


def test_ingest_commit_makes_an_immutable_ready_folder_and_keeps_source_bytes_untouched(
    tmp_path: Path,
):
    source, output, policy, _approved = _inputs(tmp_path)
    before = {path.name: path.read_bytes() for path in source.iterdir()}
    previewed = ingest_worker._prepare(_request(source, output, policy, operation="preview"))
    preview_summary = ingest_worker._summary(previewed)
    prepared = ingest_worker._prepare(
        _pinned_commit_request(source, output, policy, preview_summary)
    )

    ingest_worker._commit(prepared)

    names = {path.name for path in output.iterdir()}
    assert {
        "submission-manifest.json",
        "triage-producer-recipe.json",
        "candidate-evidence-manifest.json",
        "triage-decision-manifest.json",
        "triage-clusters.json",
        "ingest-ready.json",
    } <= names
    assert not {"corpus-register.json", "triage-confirmation.json"} & names
    assert {path.name: path.read_bytes() for path in source.iterdir()} == before
    ready = json.loads((output / "ingest-ready.json").read_text(encoding="utf-8"))
    assert ready["confirmed_cluster_count"] == 0
    assert ready["confirmation_file_retained"] is False


@requires_host_boundary
def test_ingest_refusal_shows_the_unit_6b_reason_verbatim_and_writes_nothing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    source, output, policy, _approved = _inputs(tmp_path)
    confirmation = {
        "schema": CONFIRMATION_SCHEMA,
        "corpus_id": "another-corpus",
        "appending_run": "operator-test",
        "authority": {"kind": "human", "identity": "operator", "revision": None},
        "instrument_config_sha256": "0" * 64,
        "evidence_manifest_sha256": "0" * 64,
        "clusters": [],
    }
    confirmation_path = tmp_path / "confirmation.json"
    confirmation_path.write_bytes(canonical_bytes(confirmation))

    assert (
        cli.main(
            [
                "--workspace",
                str(ROOT),
                "--state-dir",
                str(tmp_path / "state"),
                "ingest",
                "--source",
                str(source),
                "--output-dir",
                str(output),
                "--policy",
                str(policy),
                "--corpus-id",
                "synthetic-console",
                "--confirmation-file",
                str(confirmation_path),
            ]
        )
        == 2
    )
    rendered = capsys.readouterr().out
    assert (
        "manual refusal wrong-corpus: confirmation corpus does not match producer pass" in rendered
    )
    assert (
        "What happened:" in rendered and "What it means:" in rendered and "Next step:" in rendered
    )
    assert not list(output.iterdir())


@requires_host_boundary
def test_ingest_commit_failure_shows_the_workers_own_reason_not_a_raw_json_dict(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    """An uncertain commit may follow writes, so its reason must remain actionable.

    The refusal is named, not merely counted. Every custody failure also exits 2
    and also renders "What happened:" and "What it means:" without JSON, so the
    original assertions were satisfied by a console that refused to open at all
    and never reached the unwritable folder this test sets up. That is not a
    hypothetical: on a host whose `setpriv` predates Landlock this test passed
    while measuring nothing, which is what `requires_host_boundary` above now
    prevents. Asserting *which* refusal arrived keeps it honest on any host.
    """
    source, output, policy, _approved = _inputs(tmp_path)
    output.chmod(0o500)
    try:
        exit_code = cli.main(
            [
                "--workspace",
                str(ROOT),
                "--state-dir",
                str(tmp_path / "state"),
                "ingest",
                "--source",
                str(source),
                "--output-dir",
                str(output),
                "--policy",
                str(policy),
                "--corpus-id",
                "synthetic-console",
            ]
        )
    finally:
        output.chmod(0o700)
    assert exit_code == 2
    rendered = capsys.readouterr().out
    assert "What happened:" in rendered and "What it means:" in rendered
    assert '"status"' not in rendered
    assert '"reason"' not in rendered
    # The ingest worker's own refusal, not the console failing to open. Without
    # this the console never had to reach the folder at all.
    assert ERRORS[ErrorCode.INGEST_UNRESOLVED].what_happened in rendered
    assert ERRORS[ErrorCode.CONSOLE_CUSTODY_REFUSED].what_happened not in rendered


@requires_host_boundary
def test_ingest_does_not_resolve_a_source_symlink_past_the_data_gate(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    source, output, policy, approved = _inputs(tmp_path)
    linked = approved / "linked-masters"
    linked.symlink_to(source, target_is_directory=True)

    assert (
        cli.main(
            [
                "--workspace",
                str(ROOT),
                "--state-dir",
                str(tmp_path / "state"),
                "ingest",
                "--source",
                str(linked),
                "--output-dir",
                str(output),
                "--policy",
                str(policy),
                "--corpus-id",
                "synthetic-console",
            ]
        )
        == 2
    )
    assert "the submitted folder is a symlink" in capsys.readouterr().out
    assert not list(output.iterdir())


def test_ingest_refuses_an_empty_confirmation_before_the_commit_child_can_write(
    tmp_path: Path,
):
    source, output, policy, _approved = _inputs(tmp_path)
    first = ingest_worker._prepare(_request(source, output, policy, operation="preview"))
    confirmation = {
        "schema": CONFIRMATION_SCHEMA,
        "corpus_id": "synthetic-console",
        "appending_run": "operator-test",
        "authority": {"kind": "human", "identity": "operator", "revision": None},
        "instrument_config_sha256": first.recipe["instrument_config_sha256"],
        "evidence_manifest_sha256": digest_of(first.evidence_manifest),
        "clusters": [],
    }
    confirmation_path = tmp_path / "empty-confirmation.json"
    confirmation_path.write_bytes(canonical_bytes(confirmation))
    request = _request(source, output, policy, operation="preview")
    request["confirmation_file"] = str(confirmation_path)

    with pytest.raises(Exception, match="confirmation names no cluster"):
        ingest_worker._prepare(request)

    assert not list(output.iterdir())


def test_ingest_accepts_a_valid_confirmation_file_and_retains_its_authority(
    tmp_path: Path,
):
    source, output, policy, _approved = _inputs(tmp_path)
    first = ingest_worker._prepare(_request(source, output, policy, operation="preview"))
    pair = first.evidence[0]["both_digests"]
    confirmation = {
        "schema": CONFIRMATION_SCHEMA,
        "corpus_id": "synthetic-console",
        "appending_run": "operator-confirmation-test",
        "authority": {"kind": "human", "identity": "operator", "revision": None},
        "instrument_config_sha256": first.recipe["instrument_config_sha256"],
        "evidence_manifest_sha256": digest_of(first.evidence_manifest),
        "clusters": [
            {
                "pages": [
                    {
                        "volume_id": "volume-1",
                        "designation": "opening-1",
                        "member_frame_sha256": list(pair),
                    }
                ],
                "evidence_pairs": [list(pair)],
            }
        ],
    }
    confirmation_path = tmp_path / "confirmation.json"
    confirmation_path.write_bytes(canonical_bytes(confirmation))
    preview_request = _request(source, output, policy, operation="preview")
    preview_request["confirmation_file"] = str(confirmation_path)
    previewed = ingest_worker._prepare(preview_request)
    preview_summary = ingest_worker._summary(previewed)
    request = _pinned_commit_request(source, output, policy, preview_summary)
    request["confirmation_file"] = str(confirmation_path)

    prepared = ingest_worker._prepare(request)
    ingest_worker._commit(prepared)

    assert (
        json.loads((output / "triage-confirmation.json").read_text(encoding="utf-8"))
        == confirmation
    )
    assert (output / "corpus-register.json").is_file()
    ready = json.loads((output / "ingest-ready.json").read_text(encoding="utf-8"))
    assert ready["confirmed_cluster_count"] == 1
    assert ready["confirmation_file_retained"] is True

    # A digest binds preview to commit, but visible designations and member prefixes
    # are what let the operator approve the confirmation's actual membership.
    [membership] = preview_summary["confirmed_clusters"]
    assert membership.startswith("volume-1 opening-1: ")
    assert pair[0][:12] in membership and pair[1][:12] in membership
    printed: list[str] = []
    ingest._print_preview(preview_summary, printed.append)
    assert "volume-1 opening-1" in printed[0]
    assert pair[0][:12] in printed[0] and pair[1][:12] in printed[0]


def test_ingest_does_not_publish_ready_when_the_register_digest_is_not_verified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source, output, policy, _approved = _inputs(tmp_path)
    first = ingest_worker._prepare(_request(source, output, policy, operation="preview"))
    pair = first.evidence[0]["both_digests"]
    confirmation = {
        "schema": CONFIRMATION_SCHEMA,
        "corpus_id": "synthetic-console",
        "appending_run": "operator-register-digest-test",
        "authority": {"kind": "human", "identity": "operator", "revision": None},
        "instrument_config_sha256": first.recipe["instrument_config_sha256"],
        "evidence_manifest_sha256": digest_of(first.evidence_manifest),
        "clusters": [
            {
                "pages": [
                    {
                        "volume_id": "volume-1",
                        "designation": "opening-1",
                        "member_frame_sha256": list(pair),
                    }
                ],
                "evidence_pairs": [list(pair)],
            }
        ],
    }
    confirmation_path = tmp_path / "confirmation.json"
    confirmation_path.write_bytes(canonical_bytes(confirmation))
    preview_request = _request(source, output, policy, operation="preview")
    preview_request["confirmation_file"] = str(confirmation_path)
    previewed = ingest_worker._prepare(preview_request)
    commit_request = _pinned_commit_request(
        source, output, policy, ingest_worker._summary(previewed)
    )
    commit_request["confirmation_file"] = str(confirmation_path)
    prepared = ingest_worker._prepare(commit_request)
    real_commit = ingest_worker.producer.commit_confirmed_production

    def return_unmatched_digest(*args, **kwargs):  # type: ignore[no-untyped-def]
        produced, _digest = real_commit(*args, **kwargs)
        return produced, "0" * 64

    monkeypatch.setattr(
        ingest_worker.producer, "commit_confirmed_production", return_unmatched_digest
    )

    with pytest.raises(ValueError, match="does not match the verified chain digest"):
        ingest_worker._commit(prepared)

    assert not (output / "ingest-ready.json").exists()


def test_ingest_commit_refuses_when_the_submitted_folder_changed_after_the_preview(
    tmp_path: Path,
):
    """Preview and commit are two independent worker launches with no shared state.

    Without a digest pin, a source file changing in that window would be committed
    silently different from what the operator was just shown and approved on screen —
    the exact TOCTOU a shown-before-written promise has to close.
    """
    source, output, policy, _approved = _inputs(tmp_path)
    previewed = ingest_worker._prepare(_request(source, output, policy, operation="preview"))
    preview_summary = ingest_worker._summary(previewed)

    (source / "page-1.png").write_bytes(_distinct_png_bytes())

    commit_request = _pinned_commit_request(source, output, policy, preview_summary)
    with pytest.raises(ValueError, match="changed after the ingest preview was shown"):
        ingest_worker._prepare(commit_request)
    assert not list(output.iterdir())


def test_ingest_commit_refuses_when_the_output_directory_inode_changed_after_preview(
    tmp_path: Path,
):
    """The selected destination is an inode, not only a reusable path spelling."""
    source, output, policy, approved = _inputs(tmp_path)
    previewed = ingest_worker._prepare(_request(source, output, policy, operation="preview"))
    pinned = _pinned_commit_request(source, output, policy, ingest_worker._summary(previewed))
    original = approved / "original-ready"
    output.rename(original)
    output.mkdir()

    with pytest.raises(ValueError, match="output folder changed after the preview"):
        ingest_worker._prepare(pinned)

    assert not list(output.iterdir())
    assert not list(original.iterdir())


def test_ingest_commit_refuses_when_the_confirmation_file_changed_after_the_preview(
    tmp_path: Path,
):
    """A confirmation swapped in after the shown preview must not commit silently.

    Both the previewed and swapped confirmations trace to the same evidence manifest
    (so the producer's own evidence-binding check cannot catch the swap on its own);
    only the pinned confirmation digest distinguishes them.
    """
    source, output, policy, _approved = _inputs(tmp_path)
    first = ingest_worker._prepare(_request(source, output, policy, operation="preview"))
    pair = first.evidence[0]["both_digests"]
    shared_fields = {
        "schema": CONFIRMATION_SCHEMA,
        "corpus_id": "synthetic-console",
        "authority": {"kind": "human", "identity": "operator", "revision": None},
        "instrument_config_sha256": first.recipe["instrument_config_sha256"],
        "evidence_manifest_sha256": digest_of(first.evidence_manifest),
        "clusters": [
            {
                "pages": [
                    {
                        "volume_id": "volume-1",
                        "designation": "opening-1",
                        "member_frame_sha256": list(pair),
                    }
                ],
                "evidence_pairs": [list(pair)],
            }
        ],
    }
    previewed_confirmation = {**shared_fields, "appending_run": "operator-preview"}
    confirmation_path = tmp_path / "confirmation.json"
    confirmation_path.write_bytes(canonical_bytes(previewed_confirmation))

    preview_request = _request(source, output, policy, operation="preview")
    preview_request["confirmation_file"] = str(confirmation_path)
    previewed = ingest_worker._prepare(preview_request)
    preview_summary = ingest_worker._summary(previewed)

    swapped_confirmation = {**shared_fields, "appending_run": "swapped-after-preview"}
    confirmation_path.write_bytes(canonical_bytes(swapped_confirmation))

    commit_request = _pinned_commit_request(source, output, policy, preview_summary)
    commit_request["confirmation_file"] = str(confirmation_path)
    with pytest.raises(ValueError, match="confirmation file changed after the ingest preview"):
        ingest_worker._prepare(commit_request)
    assert not list(output.iterdir())


@requires_host_boundary
def test_scripted_ingest_walk_uses_the_confined_console_and_prints_door_summary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    source, output, policy, _approved = _inputs(tmp_path)

    assert (
        cli.main(
            [
                "--workspace",
                str(ROOT),
                "--state-dir",
                str(tmp_path / "state"),
                "ingest",
                "--source",
                str(source),
                "--output-dir",
                str(output),
                "--policy",
                str(policy),
                "--corpus-id",
                "synthetic-console",
            ]
        )
        == 0
    )
    rendered = capsys.readouterr().out
    assert "Ingest preview — no files have been written." in rendered
    assert "Ready-to-submit folder:" in rendered
    assert "What the Door will see:" in rendered
    assert "No pod was started, confirmed, or billed." in rendered
    assert (output / "ingest-ready.json").is_file()


def test_each_ingest_relevant_verb_has_a_double_click_console_surface(
    monkeypatch: pytest.MonkeyPatch,
):
    """The console owns submit, gate, producer, and confirmation as one UI verb."""

    parser = cli.build_parser()
    selected = parser.parse_args(
        [
            "ingest",
            "--source",
            "/approved/masters",
            "--output-dir",
            "/approved/ready",
            "--corpus-id",
            "corpus",
        ]
    )
    assert selected.verb == "ingest"

    answers = iter(("ingest", "/approved/masters", "/approved/ready", "", "corpus", "auto", ""))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    assert cli._interactive_arguments() == [
        "ingest",
        "--source",
        "/approved/masters",
        "--output-dir",
        "/approved/ready",
        "--corpus-id",
        "corpus",
        "--mode",
        "auto",
    ]
    wrapper = ROOT / "operations" / "operator" / "Verbatus.command"
    assert "operations.operator.entry" in wrapper.read_text(encoding="utf-8")

    answers = iter(
        (
            "ingest",
            "/approved/masters",
            "/approved/ready",
            "/reviewed/data-policy.json",
            "corpus",
            "auto",
            "",
        )
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    selected_with_policy = cli._interactive_arguments()
    assert selected_with_policy[-2:] == ["--policy", "/reviewed/data-policy.json"]


def test_ingest_presenter_has_no_image_or_producer_writer_and_uses_the_custody_seam():
    """The presenter names neither producer nor submit, and launches through custody.

    The two negatives read the source text on purpose. A module-namespace or
    resolved-import check sees only what the module imported at import time, and
    would pass a function-local `import operations.triage` inside a branch no
    test happens to take — which is exactly how the producer would come back
    into the presenter. Reading the text catches it wherever it is written.

    The writable targets are *not* asserted here as text. They are pinned as
    behaviour by the custody-launch coverage in
    `test_the_double_click_route_reaches_the_one_confined_ingest_implementation`,
    which records every launch and asserts `[None, output.resolve()]` — the same
    property, checked by observing it rather than by matching a keyword
    argument's spelling.
    """
    source = inspect.getsource(ingest)
    assert "operations.triage" not in source
    assert "operations.submit" not in source
    assert ingest.run_confined is custody.run_confined


@requires_host_boundary
def test_ingest_refuses_an_output_folder_inside_the_submitted_folder(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    """The Door inventories the source, so ingest output inside it is inadmissible."""
    source, _output, policy, _approved = _inputs(tmp_path)
    inside = source / "ready"
    inside.mkdir()
    marker = inside / "already-here.txt"
    marker.write_text("must survive the refusal", encoding="utf-8")
    (source / "undecodable.bin").write_bytes(b"not an image")

    assert (
        cli.main(
            [
                "--workspace",
                str(ROOT),
                "--state-dir",
                str(tmp_path / "state"),
                "ingest",
                "--source",
                str(source),
                "--output-dir",
                str(inside),
                "--policy",
                str(policy),
                "--corpus-id",
                "synthetic-console",
            ]
        )
        == 2
    )
    rendered = capsys.readouterr().out
    assert "cannot live inside the submitted folder" in rendered
    assert "output folder is not empty" not in rendered
    assert "could not be decoded" not in rendered
    assert marker.read_text(encoding="utf-8") == "must survive the refusal"


@requires_host_boundary
def test_a_case_variant_spelling_cannot_place_the_output_inside_the_submission(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    """Case variants on default APFS must not bypass identity containment."""
    source, _output, policy, approved = _inputs(tmp_path)
    variant = approved / "Masters"
    if not variant.is_dir():
        pytest.skip("case-sensitive filesystem: the variant spelling is a different folder")
    inside = source / "ready"
    inside.mkdir()

    assert (
        cli.main(
            [
                "--workspace",
                str(ROOT),
                "--state-dir",
                str(tmp_path / "state"),
                "ingest",
                "--source",
                str(source),
                "--output-dir",
                str(variant / "ready"),
                "--policy",
                str(policy),
                "--corpus-id",
                "synthetic-console",
            ]
        )
        == 2
    )
    rendered = capsys.readouterr().out
    assert "cannot live inside the submitted folder" in rendered
    assert list(inside.iterdir()) == []


def test_ingest_commit_refuses_when_the_instrument_settings_changed_after_the_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Instrument bytes determine proxies, verdicts, recipes, and evidence names."""
    source, output, policy, _approved = _inputs(tmp_path)
    previewed = ingest_worker._prepare(_request(source, output, policy, operation="preview"))
    preview_summary = ingest_worker._summary(previewed)
    commit_request = _pinned_commit_request(source, output, policy, preview_summary)

    # Bytes the loader accepts and that change no threshold: the point is that the
    # sealed instrument digest moved, not that a verdict did.
    edited = tmp_path / "instrument.toml"
    edited.write_text(
        instrument.DEFAULT_CONFIG_PATH.read_text(encoding="utf-8") + "\n", encoding="utf-8"
    )
    # `load_config`'s default path is bound at definition, so replace the call the
    # worker actually makes: this is the second launch reading different bytes.
    real_load_config = instrument.load_config
    monkeypatch.setattr(instrument, "load_config", lambda path=edited: real_load_config(path))

    with pytest.raises(
        ValueError, match="triage instrument configuration changed after the ingest preview"
    ):
        ingest_worker._prepare(commit_request)
    assert not list(output.iterdir())


def test_ingest_commit_refuses_when_the_data_handling_policy_changed_after_the_preview(
    tmp_path: Path,
):
    """The gate authority shown by preview is a mutable input too.

    The path itself travels unchanged in the request, but both confined children
    read its bytes independently. Even a semantically identical rewrite is a new
    policy document, so commit must not silently proceed under a policy other than
    the exact one whose storage check the preview reported.
    """
    source, output, policy, _approved = _inputs(tmp_path)
    previewed = ingest_worker._prepare(_request(source, output, policy, operation="preview"))
    preview_summary = ingest_worker._summary(previewed)
    commit_request = _pinned_commit_request(source, output, policy, preview_summary)

    policy.write_text(policy.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="data-handling policy changed after the ingest preview"):
        ingest_worker._prepare(commit_request)
    assert not list(output.iterdir())


@requires_host_boundary
def test_ingest_names_every_undecodable_file_by_position_and_digest(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    """Refusals must identify bad frames without leaking sealed filenames."""
    source, output, policy, _approved = _inputs(tmp_path)
    stray = source / ".DS_Store"
    stray.write_bytes(b"\x00\x01Bud1 not an image")
    stray_digest = hashlib.sha256(stray.read_bytes()).hexdigest()

    assert (
        cli.main(
            [
                "--workspace",
                str(ROOT),
                "--state-dir",
                str(tmp_path / "state"),
                "ingest",
                "--source",
                str(source),
                "--output-dir",
                str(output),
                "--policy",
                str(policy),
                "--corpus-id",
                "synthetic-console",
            ]
        )
        == 2
    )
    rendered = capsys.readouterr().out
    assert "1 of 4 submitted file(s) could not be decoded" in rendered
    # `.DS_Store` sorts before `page-1.png`, so it is file 1 in the ledger's order.
    assert f"file 1 (digest {stray_digest[:12]})" in rendered
    # The path itself never reaches the terminal.
    assert ".DS_Store" not in rendered
    assert not list(output.iterdir())


def test_ingest_refuses_a_submitted_file_larger_than_the_retained_byte_ceiling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """An unbounded read of a real master folder ends as an unreadable failure.

    The producer's API takes whole frames, so the bytes are held either way; what
    the bound changes is that an oversized submission becomes a named refusal
    instead of an OOM kill of the confined child, which returns no JSON and
    reaches the operator with an empty detail (GOVERNANCE 2). The ceiling is
    `inventory.MAX_SUBMITTED_BYTES` — this repository's own declared limit on
    retained submitted bytes — not a second hand-kept copy of the Door's.
    """
    source, output, policy, _approved = _inputs(tmp_path)
    monkeypatch.setattr(inventory, "MAX_SUBMITTED_BYTES", 64)

    with pytest.raises(ValueError, match="larger than the 64-byte ceiling"):
        ingest_worker._prepare(_request(source, output, policy, operation="preview"))
    assert not list(output.iterdir())


def test_ingest_refuses_a_frame_count_that_would_amplify_proxy_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source, output, policy, _approved = _inputs(tmp_path)
    monkeypatch.setattr(ingest_worker, "MAX_INGEST_FRAMES", 2)

    with pytest.raises(ValueError, match="more than 2 image masters"):
        ingest_worker._prepare(_request(source, output, policy, operation="preview"))

    assert not list(output.iterdir())


def test_ingest_refuses_candidate_amplification_before_full_comparisons(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source, output, policy, _approved = _inputs(tmp_path)
    monkeypatch.setattr(ingest_worker, "MAX_INGEST_CANDIDATE_PAIRS", 2)

    with pytest.raises(instrument.InstrumentRefusal, match="above the 2-pair ceiling"):
        ingest_worker._prepare(_request(source, output, policy, operation="preview"))

    assert not list(output.iterdir())


def test_ingest_refuses_a_corpus_id_that_amplifies_every_produced_row(tmp_path: Path):
    source, output, policy, _approved = _inputs(tmp_path)
    request = _request(source, output, policy, operation="preview")
    request["corpus_id"] = "x" * (ingest_worker.MAX_CORPUS_ID_CHARACTERS + 1)

    with pytest.raises(ValueError, match="corpus id is longer"):
        ingest_worker._request(request)


def test_the_console_shows_the_ledger_digest_the_run_tree_will_carry(tmp_path: Path):
    """The displayed ledger identity must be the self-hash every page carries."""
    source, output, policy, _approved = _inputs(tmp_path)
    previewed = ingest_worker._prepare(_request(source, output, policy, operation="preview"))
    summary = ingest_worker._summary(previewed)
    prepared = ingest_worker._prepare(_pinned_commit_request(source, output, policy, summary))
    ingest_worker._commit(prepared)

    manifest = json.loads((output / "submission-manifest.json").read_text(encoding="utf-8"))
    assert summary["submission_ledger_self_hash"] == manifest["self_hash"]
    assert summary["submission_manifest_sha256"] == digest_of(manifest)
    assert summary["submission_manifest_sha256"] != manifest["self_hash"]
    ready = json.loads((output / "ingest-ready.json").read_text(encoding="utf-8"))
    assert ready["submission_ledger_self_hash"] == manifest["self_hash"]
    assert ready["data_handling_policy_sha256"] == gate.load_policy_binding(policy).config_sha256


@requires_host_boundary
def test_the_ready_folder_is_admitted_by_the_real_door_exactly_as_the_console_claimed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    """Reconcile the console's admission claim against the real Door output.

    Dynamic import is required because the Door's package segment starts with a
    digit; this cross-boundary test reads it without moving the claim from this suite.
    """
    door = importlib.import_module("pipeline.1_exemplar.door")
    from common.runtree.store import RunTree

    assert Path(door.__file__).resolve() == ROOT / "pipeline" / "1_exemplar" / "door.py"

    source, output, policy, approved = _inputs(tmp_path)
    assert (
        cli.main(
            [
                "--workspace",
                str(ROOT),
                "--state-dir",
                str(tmp_path / "state"),
                "ingest",
                "--source",
                str(source),
                "--output-dir",
                str(output),
                "--policy",
                str(policy),
                "--corpus-id",
                "synthetic-console",
            ]
        )
        == 0
    )
    rendered = capsys.readouterr().out

    run_root = approved / "runs"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "door.py",
            "--run-root",
            str(run_root),
            "--run-id",
            "from-the-console",
            "--submission-folder",
            str(source),
            "--submission-manifest",
            str(output / "submission-manifest.json"),
            "--data-gate-policy",
            str(policy),
            "--triage-decision-manifest",
            str(output / "triage-decision-manifest.json"),
            "--triage-clusters",
            str(output / "triage-clusters.json"),
            "--triage-producer-recipe",
            str(output / "triage-producer-recipe.json"),
        ],
    )
    assert door.main() == 0

    run = RunTree(run_root, "from-the-console").read_run()
    door_manifest = RunTree(run_root, "from-the-console").build_manifest(door.DOOR)
    admissions = [entry for entry in door_manifest["artifacts"] if entry["kind"] == "admission"]
    ledger = json.loads((output / "submission-manifest.json").read_text(encoding="utf-8"))
    assert len(run["source_manifest"]) == len(ledger["files"]) == 3
    assert len(admissions) == 3
    assert {entry["outcome"] for entry in admissions} == {"admitted"}
    assert f"{len(ledger['files'])} submitted file(s)" in rendered
    # The digest the console printed is the one every admitted page carries.
    assert {row["ledger_sha256"] for row in run["source_manifest"]} == {ledger["self_hash"]}
    assert f"ledger self-hash {ledger['self_hash']}" in rendered


@requires_host_boundary
def test_a_failed_preview_never_tells_the_operator_records_may_have_been_written(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    """A no-write preview cannot use commit's possibly-partial recovery text."""
    source, output, policy, _approved = _inputs(tmp_path)

    monkeypatch.setattr(
        ingest,
        "_decode_response",
        lambda _value: {"status": "wrong-shape"},
    )
    assert (
        cli.main(
            [
                "--workspace",
                str(ROOT),
                "--state-dir",
                str(tmp_path / "state"),
                "ingest",
                "--source",
                str(source),
                "--output-dir",
                str(output),
                "--policy",
                str(policy),
                "--corpus-id",
                "synthetic-console",
            ]
        )
        == 2
    )
    rendered = capsys.readouterr().out
    assert "the preview runs with no write rights at all" in rendered
    assert "immutable ingest records may have been written" not in rendered
    assert not list(output.iterdir())


def test_a_replayed_commit_can_only_ever_write_what_its_pin_already_described(
    tmp_path: Path,
):
    """The pin is an equality test: it refuses, and it never authorises.

    Replaying a commit request with an old pin is the third failure mode the pin
    has to survive. It cannot launder a different write: another output inode is
    refused even when every input digest still matches, and the folder that already
    holds the first result meets the empty-folder requirement before anything is
    written, so no record is ever replaced.
    """
    source, first_output, policy, approved = _inputs(tmp_path)
    previewed = ingest_worker._prepare(_request(source, first_output, policy, operation="preview"))
    summary = ingest_worker._summary(previewed)
    pinned = _pinned_commit_request(source, first_output, policy, summary)
    ingest_worker._commit(ingest_worker._prepare(pinned))
    written = {path.name: path.read_bytes() for path in first_output.iterdir()}

    second_output = approved / "replayed"
    second_output.mkdir()
    replayed = {**pinned, "output_dir": str(second_output)}
    with pytest.raises(ValueError, match="output folder changed after the preview"):
        ingest_worker._prepare(replayed)
    assert not list(second_output.iterdir())

    with pytest.raises(ValueError, match="output folder is not empty"):
        ingest_worker._prepare(pinned)
    assert {path.name: path.read_bytes() for path in first_output.iterdir()} == written


def test_a_preview_request_may_not_carry_any_expected_digest(tmp_path: Path):
    """A preview establishes the pin; a preview that arrives holding one is not one."""
    source, output, policy, _approved = _inputs(tmp_path)
    for field in EXPECTED_DIGEST_FIELDS:
        request = _request(source, output, policy, operation="preview")
        request[field] = "0" * 64
        with pytest.raises(ValueError, match="may not already carry an expected digest"):
            ingest_worker._request(request)


def test_prepare_refuses_a_proxy_whose_computed_digest_does_not_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source, output, policy, _approved = _inputs(tmp_path)
    real_build = instrument.build_proxies_from_bytes

    def corrupt_proxy(data, config):  # type: ignore[no-untyped-def]
        proxy = real_build(data, config)
        return replace(proxy, signature_png=proxy.signature_png + b"changed")

    monkeypatch.setattr(instrument, "build_proxies_from_bytes", corrupt_proxy)

    with pytest.raises(instrument.InstrumentRefusal, match="does not match the digest"):
        ingest_worker._prepare(_request(source, output, policy, operation="preview"))

    assert not list(output.iterdir())


def test_parent_refuses_a_malformed_or_amplified_worker_summary(tmp_path: Path):
    source, output, policy, _approved = _inputs(tmp_path)
    prepared = ingest_worker._prepare(_request(source, output, policy, operation="preview"))
    summary = ingest_worker._summary(prepared)
    summary["candidates"] = [
        "x" * (ingest.MAX_PREVIEW_LINE_CHARACTERS + 1) for _candidate in summary["candidates"]
    ]

    with pytest.raises(ingest.OperatorError, match="could not show"):
        ingest._summary(
            {"status": "preview", "summary": summary},
            ingest.ErrorCode.INGEST_PREVIEW_UNRESOLVED,
        )


def test_the_double_click_route_reaches_the_one_confined_ingest_implementation(
    monkeypatch: pytest.MonkeyPatch,
):
    """The interactive path must not be a second, weaker copy of the verb path.

    Both routes exist only as argv: `_interactive_arguments` asks a person for the
    same facts a flag would carry and hands the result to the same `parse_args`,
    so there is exactly one dispatch and exactly one call site for the confined
    flow. That is asserted here rather than assumed, because "a person can drive
    it" is this unit's exit and a divergent second implementation is precisely how
    the interactive route would come to hold weaker guarantees than the verb one.
    """
    source = inspect.getsource(cli)
    assert source.count("ingest_in_custody(") == 1
    # The interactive branch supplies flags to the parser and nothing else: it
    # never reaches the worker, the custody seam, or a path of its own.
    interactive = inspect.getsource(cli._interactive_arguments)
    assert "ingest_in_custody" not in interactive
    assert "run_confined" not in interactive

    answers = iter(("ingest", "/approved/masters", "/approved/ready", "", "corpus", "semi", ""))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    parsed = cli.build_parser().parse_args(cli._interactive_arguments())
    assert (parsed.verb, parsed.mode, parsed.policy, parsed.confirmation_file) == (
        "ingest",
        "semi",
        None,
        None,
    )


@requires_host_boundary
@pytest.mark.parametrize("with_confirmation", (False, True), ids=("no-confirmation", "confirmed"))
def test_the_committed_folder_holds_exactly_the_files_the_preview_listed(
    tmp_path: Path,
    with_confirmation: bool,
    monkeypatch: pytest.MonkeyPatch,
):
    """Preview and commit file sets must match, including the persistent lock.

    The console route uses the real custody launcher so no in-process stub can hide
    a file created by the confined worker.
    """
    source, output, policy, _approved = _inputs(tmp_path)
    confirmation_path: Path | None = None
    if with_confirmation:
        first = ingest_worker._prepare(_request(source, output, policy, operation="preview"))
        pair = first.evidence[0]["both_digests"]
        confirmation_path = tmp_path / "confirmation.json"
        confirmation_path.write_bytes(
            canonical_bytes(
                {
                    "schema": CONFIRMATION_SCHEMA,
                    "corpus_id": "synthetic-console",
                    "appending_run": "operator-plan-test",
                    "authority": {"kind": "human", "identity": "operator", "revision": None},
                    "instrument_config_sha256": first.recipe["instrument_config_sha256"],
                    "evidence_manifest_sha256": digest_of(first.evidence_manifest),
                    "clusters": [
                        {
                            "pages": [
                                {
                                    "volume_id": "volume-1",
                                    "designation": "opening-1",
                                    "member_frame_sha256": list(pair),
                                }
                            ],
                            "evidence_pairs": [list(pair)],
                        }
                    ],
                }
            )
        )

    planned: list[str] = []
    real_print_preview = ingest._print_preview

    def observe_preview(summary, printer):  # type: ignore[no-untyped-def]
        planned.extend(summary["planned_files"])
        real_print_preview(summary, printer)

    launches: list[Path | None] = []
    real_run_confined = ingest.run_confined

    def observed_confined_launch(  # type: ignore[no-untyped-def]
        command, *, writable, cwd, input_text
    ):
        launches.append(writable)
        return real_run_confined(
            command,
            writable=writable,
            cwd=cwd,
            input_text=input_text,
        )

    monkeypatch.setattr(ingest, "_print_preview", observe_preview)
    monkeypatch.setattr(ingest, "run_confined", observed_confined_launch)
    arguments = [
        "--workspace",
        str(ROOT),
        "--state-dir",
        str(tmp_path / "state"),
        "ingest",
        "--source",
        str(source),
        "--output-dir",
        str(output),
        "--policy",
        str(policy),
        "--corpus-id",
        "synthetic-console",
    ]
    if confirmation_path is not None:
        arguments.extend(("--confirmation-file", str(confirmation_path)))

    assert cli.main(arguments) == 0

    assert launches == [None, output.resolve()]
    assert sorted(planned) == sorted(path.name for path in output.iterdir())
    assert len(planned) == len(set(planned))
    # Three candidate-evidence records make the exact branch totals 15 and 18.
    assert len(planned) == (18 if with_confirmation else 15)
