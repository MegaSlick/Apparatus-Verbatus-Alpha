from __future__ import annotations

import threading
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from common.contracts.canonical import canonical_bytes, digest_of
from operations.operator import triage
from operations.operator.errors import ERRORS, ErrorCode
from operations.triage import instrument
from operations.triage.producer import (
    ProducerRefusal,
    SubmittedFrame,
    append_confirmation_to_register,
    load_confirmation,
    produce,
)

MODES = Path(__file__).resolve().parents[2] / "config" / "triage_modes.toml"


# Twelve contiguous 64-cell rows at a 40-intensity delta: each fine cell in the
# band disagrees (mean delta above the 12 tolerance) while every 12-row coarse
# prefilter cell holds exactly 6 darkened rows (mean shift 20, at its
# tolerance), so the pair is selected and genuinely measures as one 768-cell
# component -- 250 per-mille span share, 750 per-mille agreement, a real
# complementary-candidate rather than a forged verdict.
_BAND_START = {"a": None, "b": 6, "c": 30}


def _frame(name: str) -> SubmittedFrame:
    image = Image.new("L", (64, 48), 200)
    start = _BAND_START[name[0]]
    if start is not None:
        for y in range(start, start + 12):
            for x in range(64):
                image.putpixel((x, y), 160)
    encoded = BytesIO()
    image.save(encoded, format="PNG")
    return SubmittedFrame(name, encoded.getvalue())


class _Batch:
    """One console queue together with the real 6A/6B material it was built from.

    The producer half is kept because the console's confirmation draft is only
    meaningful if the producer will take it: the evidence manifest, records and
    recipe here are the same objects `produce` demands, so a test can carry a
    draft the whole way rather than asserting the console's own opinion of it.
    """

    def __init__(self, tmp_path: Path, names: tuple[str, ...] = ("a", "b")):
        self.frames = [_frame(name) for name in names]
        self.produced = produce(self.frames, corpus_id="c", mode="semi")
        config = instrument.load_config()
        proxies = [instrument.build_proxies_from_bytes(f.data, config) for f in self.frames]
        self.evidence, self.manifest = instrument.candidate_evidence(proxies, config)
        self.recipe = instrument.producer_recipe(config)
        # The frames above are constructed so the instrument's own measurement
        # lands every pair on complementary-candidate; the manifest recomputes
        # each record from the frames, so no forged verdict or measure would
        # survive the producer's revalidation.
        assert all(record["verdict"] == "complementary-candidate" for record in self.evidence)
        self.paths = {
            row["source_frame_sha256"]: str(tmp_path / f"{index}.png")
            for index, row in enumerate(self.produced.manifest["records"])
        }
        self.queue = triage.build_queue(
            self.produced.manifest,
            self.evidence,
            proxy_paths=self.paths,
            mode_declaration=triage.declare_mode("semi", batch_id="batch-1", operator="Tyrel"),
            triage_modes_path=MODES,
        )
        self.candidate = next(
            item for item in self.queue["items"] if item["kind"] == "cluster-candidate"
        )
        self.item = digest_of(self.candidate)

    def draft(self, *, pinned: bool = True) -> dict:
        draft = triage.draft_confirmation(
            self.queue,
            item_digest=self.item,
            corpus_id="c",
            appending_run="pass-1",
            authority_identity="Tyrel",
            pages=[
                {
                    "volume_id": "v1",
                    "designation": "opening-31",
                    "member_frame_sha256": sorted(self.candidate["both_digests"]),
                }
            ],
        )
        if not pinned:
            return draft
        return triage.pin_draft(draft, evidence_manifest_sha256=digest_of(self.manifest))


def _queue(tmp_path: Path) -> dict:
    return _Batch(tmp_path).queue


def test_mode_is_recorded_and_every_producer_row_is_in_the_queue(tmp_path: Path):
    batch = _Batch(tmp_path)
    queue = batch.queue
    assert queue["mode_declaration"]["mode"] == "semi"
    review_rows = [item for item in queue["items"] if item["kind"] == "review-row"]
    assert {item["source_frame_sha256"] for item in review_rows} == {
        row["source_frame_sha256"] for row in batch.produced.manifest["records"]
    }
    assert all("proxy_path" in item for item in review_rows)


def test_queue_order_is_invariant_to_evidence_and_mapping_insertion_order(tmp_path: Path):
    """No first-produced record or mapping insertion order becomes first-shown preference."""
    batch = _Batch(tmp_path, ("a", "b", "c"))
    reversed_queue = triage.build_queue(
        batch.produced.manifest,
        list(reversed(batch.evidence)),
        proxy_paths=dict(reversed(list(batch.paths.items()))),
        mode_declaration=triage.declare_mode("semi", batch_id="batch-1", operator="Tyrel"),
        triage_modes_path=MODES,
    )
    assert reversed_queue == batch.queue
    assert reversed_queue["items"] == sorted(reversed_queue["items"], key=digest_of)


def test_queue_derives_from_the_same_input_forms_it_validates(tmp_path: Path):
    """Mapping subclasses cannot validate one input and present another."""
    batch = _Batch(tmp_path)
    forged_row = {**batch.produced.manifest["records"][0], "manifest_row_sha256": "f" * 64}

    class DivergentManifest(dict):
        def __getitem__(self, key):
            if key == "records":
                return [forged_row, *super().__getitem__(key)[1:]]
            return super().__getitem__(key)

    class DivergentPaths(dict):
        def __getitem__(self, key):
            return "   "

    manifest = DivergentManifest(batch.produced.manifest)
    queue = triage.build_queue(
        manifest,
        batch.evidence,
        proxy_paths=DivergentPaths(batch.paths),
        mode_declaration=triage.declare_mode("semi", batch_id="batch-1", operator="Tyrel"),
        triage_modes_path=MODES,
    )
    review_rows = [item for item in queue["items"] if item["kind"] == "review-row"]
    assert queue["manifest_sha256"] == digest_of(batch.produced.manifest)
    assert {item["manifest_row_sha256"] for item in review_rows} == {
        row["manifest_row_sha256"] for row in batch.produced.manifest["records"]
    }


def test_instrument_configuration_failure_is_a_named_triage_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    batch = _Batch(tmp_path)

    def refuse_config():
        raise instrument.InstrumentRefusal("fixture configuration failure")

    monkeypatch.setattr(instrument, "load_config", refuse_config)
    with pytest.raises(triage.TriageRefusal, match="instrument-config-invalid"):
        triage.build_queue(
            batch.produced.manifest,
            batch.evidence,
            proxy_paths=batch.paths,
            mode_declaration=triage.declare_mode("semi", batch_id="batch-1", operator="Tyrel"),
            triage_modes_path=MODES,
        )


def test_stop_resume_appends_without_rewriting_and_declines_stay_visible(tmp_path: Path):
    queue = _queue(tmp_path)
    candidates = [item for item in queue["items"] if item["kind"] == "cluster-candidate"]
    state_path = tmp_path / "state.json"
    item = digest_of(candidates[0])
    first = triage.append_decision(state_path, queue, item_digest=item, decision="decline")
    assert first["decisions"][0]["decision"] == "decline"
    with pytest.raises(triage.TriageRefusal, match="decision-already-recorded"):
        triage.append_decision(state_path, queue, item_digest=item, decision="decline")
    assert triage._read_canonical(state_path, "queue-state")["decisions"] == first["decisions"]


def test_preview_digest_binds_the_confirmation_write(tmp_path: Path):
    queue = _queue(tmp_path)
    candidate = next(item for item in queue["items"] if item["kind"] == "cluster-candidate")
    draft = triage.draft_confirmation(
        queue,
        item_digest=digest_of(candidate),
        corpus_id="c",
        appending_run="pass-1",
        authority_identity="Tyrel",
        pages=[
            {"volume_id": "v", "designation": "p", "member_frame_sha256": candidate["both_digests"]}
        ],
    )
    pinned = triage.pin_draft(draft, evidence_manifest_sha256="a" * 64)
    with pytest.raises(triage.TriageRefusal, match="preview-changed"):
        triage.write_shown_confirmation(
            tmp_path / "confirmation.json", pinned, preview_sha256="b" * 64
        )
    triage.write_shown_confirmation(
        tmp_path / "confirmation.json", pinned, preview_sha256=digest_of(pinned)
    )
    assert (tmp_path / "confirmation.json").read_bytes() == canonical_bytes(pinned)


def test_preference_fields_are_refused_from_queue(tmp_path: Path):
    queue = _queue(tmp_path)
    queue["preferred"] = 1
    candidate = next(item for item in queue["items"] if item["kind"] == "cluster-candidate")
    with pytest.raises(triage.TriageRefusal, match="queue-expresses-preference"):
        triage.append_decision(
            tmp_path / "state.json", queue, item_digest=digest_of(candidate), decision="decline"
        )


def test_triage_refusal_copy_does_not_deny_a_journalled_decision():
    copy = ERRORS[ErrorCode.TRIAGE_REFUSED]
    assert "mode declaration, queue decision, or confirmation may already" in copy.what_it_means
    assert "No cluster was selected" not in copy.what_it_means


@pytest.mark.parametrize(
    ("operation", "reason"),
    [
        ("mode", "mode-not-declared"),
        ("decision", "decision-invalid"),
        ("decision-item", "queue-item-invalid"),
        ("recorded-item", "queue-item-invalid"),
        ("draft-item", "queue-item-invalid"),
    ],
)
def test_unhashable_public_inputs_are_named_refusals(tmp_path: Path, operation: str, reason: str):
    batch = _Batch(tmp_path)
    with pytest.raises(triage.TriageRefusal, match=reason):
        if operation == "mode":
            triage.declare_mode([], batch_id="batch-1", operator="Tyrel")
        elif operation == "decision":
            triage.append_decision(
                tmp_path / "state.json",
                batch.queue,
                item_digest=batch.item,
                decision=[],
            )
        elif operation == "decision-item":
            triage.append_decision(
                tmp_path / "state.json",
                batch.queue,
                item_digest=[],
                decision="decline",
            )
        elif operation == "recorded-item":
            triage.recorded_decision(
                tmp_path / "state.json",
                batch.queue,
                item_digest=[],
            )
        else:
            triage.draft_confirmation(
                batch.queue,
                item_digest=[],
                corpus_id="c",
                appending_run="pass-1",
                authority_identity="Tyrel",
                pages=[],
            )


def test_write_mode_declaration_refuses_a_field_smuggled_inside_a_non_string(tmp_path: Path):
    """A preference field hidden inside a non-string batch_id/operator must not reach disk.

    Validating a string-coerced field would check different data from the mapping
    that serialization publishes.
    """
    smuggled = {
        "schema": triage.MODE_SCHEMA,
        "batch_id": {"value": "batch-1", "preferred": True},
        "mode": "semi",
        "operator": "Tyrel",
    }
    target = tmp_path / "mode.json"
    with pytest.raises(triage.TriageRefusal, match="mode-declaration-invalid"):
        triage.write_mode_declaration(target, smuggled)
    assert not target.exists()


def test_mode_declaration_is_idempotent_but_never_rewritten(tmp_path: Path):
    target = tmp_path / "mode.json"
    first = triage.declare_mode("semi", batch_id="batch-1", operator="Tyrel")
    triage.write_mode_declaration(target, first)
    stood = target.read_bytes()
    triage.write_mode_declaration(target, first)
    assert target.read_bytes() == stood

    changed = triage.declare_mode("manual", batch_id="batch-1", operator="Tyrel")
    with pytest.raises(triage.TriageRefusal, match="mode-declaration-target-exists"):
        triage.write_mode_declaration(target, changed)
    assert target.read_bytes() == stood


def _pinned_draft(tmp_path: Path) -> dict:
    queue = _queue(tmp_path)
    candidate = next(item for item in queue["items"] if item["kind"] == "cluster-candidate")
    draft = triage.draft_confirmation(
        queue,
        item_digest=digest_of(candidate),
        corpus_id="c",
        appending_run="pass-1",
        authority_identity="Tyrel",
        pages=[
            {"volume_id": "v", "designation": "p", "member_frame_sha256": candidate["both_digests"]}
        ],
    )
    return triage.pin_draft(draft, evidence_manifest_sha256="a" * 64)


def test_write_shown_confirmation_refuses_a_smuggled_preference_field(tmp_path: Path):
    """A forbidden preference field must never reach disk, even with a matching digest.

    The caller controls both inputs, so preview agreement alone cannot establish
    that a draft is safe to publish.
    """
    pinned = _pinned_draft(tmp_path)
    smuggled = {**pinned, "preferred": True}
    target = tmp_path / "confirmation.json"
    with pytest.raises(triage.TriageRefusal, match="draft-invalid"):
        triage.write_shown_confirmation(target, smuggled, preview_sha256=digest_of(smuggled))
    assert not target.exists()


def test_write_shown_confirmation_refuses_a_malformed_schema(tmp_path: Path):
    pinned = _pinned_draft(tmp_path)
    malformed = {**pinned, "schema": "not-the-6b-schema"}
    target = tmp_path / "confirmation.json"
    with pytest.raises(triage.TriageRefusal, match="draft-invalid"):
        triage.write_shown_confirmation(target, malformed, preview_sha256=digest_of(malformed))
    assert not target.exists()


def test_a_tuple_wrapped_payload_cannot_carry_a_preference_field_onto_disk(tmp_path: Path):
    """The check must walk what the serializer writes, not what the object looks like.

    `canonical_bytes` renders a tuple as a JSON array, while
    `refuse_capture_preference` walks dicts and lists. Reparse-before-validation
    closes that shape divergence.
    """
    pinned = _pinned_draft(tmp_path)
    cluster = dict(pinned["clusters"][0])
    cluster["preferred"] = "b" * 64
    smuggled = {**pinned, "clusters": (cluster,)}
    target = tmp_path / "confirmation.json"
    with pytest.raises(triage.TriageRefusal, match="expresses-preference"):
        triage.write_shown_confirmation(target, smuggled, preview_sha256=digest_of(smuggled))
    assert not target.exists()
    assert b'"preferred"' in canonical_bytes(smuggled)


def test_duplicate_key_bytes_are_not_a_persistable_canonical_form(tmp_path: Path):
    """Reparse-and-validate is insufficient unless the emitted bytes are a fixed point.

    ``json.dumps`` asks a dict subclass for ``items()`` and will emit duplicate
    keys when that method supplies them. ``json.loads`` then collapses those keys,
    so validation sees an ordinary closed draft while the original bytes remain
    non-canonical and are refused by the producer's loader.
    """

    class DuplicateCorpusKey(dict):
        def items(self):
            return [*super().items(), ("corpus_id", self["corpus_id"])]

    hostile = DuplicateCorpusKey(_pinned_draft(tmp_path))
    data = canonical_bytes(hostile)
    target = tmp_path / "confirmation.json"
    target.write_bytes(data)
    with pytest.raises(ProducerRefusal, match="canonical JSON"):
        load_confirmation(target)
    target.unlink()

    with pytest.raises(triage.TriageRefusal, match="not-canonical"):
        triage.write_shown_confirmation(target, hostile, preview_sha256=digest_of(hostile))
    assert not target.exists()


def test_hard_rule_8_refusals_reach_the_operator_by_name(tmp_path: Path):
    """A picker refusal is the console's most consequential one; it is not "unexpected"."""
    batch = _Batch(tmp_path)
    queue = dict(batch.queue)
    queue["preferred"] = 1
    with pytest.raises(triage.TriageRefusal, match="queue-expresses-preference"):
        triage.append_decision(
            tmp_path / "state.json", queue, item_digest=batch.item, decision="decline"
        )


def test_tuple_wrapped_queue_preference_is_checked_on_the_persisted_form(tmp_path: Path):
    batch = _Batch(tmp_path)
    candidate = {**batch.candidate, "preferred": True}
    queue = {**batch.queue, "items": (candidate,)}
    with pytest.raises(triage.TriageRefusal, match="queue-expresses-preference"):
        triage.append_decision(
            tmp_path / "state.json",
            queue,
            item_digest=digest_of(candidate),
            decision="decline",
        )
    assert not (tmp_path / "state.json").exists()


def test_the_journal_refuses_a_prior_record_that_is_not_a_closed_decision(tmp_path: Path):
    """Every append republishes the whole journal, so every prior record is its bytes too.

    Container-only validation would adopt a hand-edited entry as console-authored
    history on the next append.
    """
    batch = _Batch(tmp_path)
    state_path = tmp_path / "state.json"
    forged = {
        "schema": triage.STATE_SCHEMA,
        "queue_sha256": digest_of(batch.queue),
        "decisions": [
            {
                "item_sha256": "z" * 64,
                "decision": "maybe",
                "draft_sha256": None,
                "note": "not part of the closed entry schema",
            }
        ],
    }
    state_path.write_bytes(canonical_bytes(forged))
    with pytest.raises(triage.TriageRefusal, match="queue-state-entry-invalid"):
        triage.append_decision(state_path, batch.queue, item_digest=batch.item, decision="decline")
    assert state_path.read_bytes() == canonical_bytes(forged)


def test_a_non_record_in_the_journal_is_a_named_refusal_not_a_bare_attribute_error(
    tmp_path: Path,
):
    batch = _Batch(tmp_path)
    state_path = tmp_path / "state.json"
    state_path.write_bytes(
        canonical_bytes(
            {
                "schema": triage.STATE_SCHEMA,
                "queue_sha256": digest_of(batch.queue),
                "decisions": ["not-a-record"],
            }
        )
    )
    with pytest.raises(triage.TriageRefusal, match="queue-state-entry-invalid"):
        triage.append_decision(state_path, batch.queue, item_digest=batch.item, decision="decline")


def test_a_torn_journal_refuses_loudly_and_does_not_start_a_fresh_one(tmp_path: Path):
    """Resume over a damaged journal stops; it never silently begins again from empty."""
    batch = _Batch(tmp_path)
    state_path = tmp_path / "state.json"
    triage.append_decision(state_path, batch.queue, item_digest=batch.item, decision="decline")
    torn = state_path.read_bytes()[:-9]
    state_path.write_bytes(torn)
    with pytest.raises(triage.TriageRefusal, match="queue-state-unreadable"):
        triage.recorded_decision(state_path, batch.queue, item_digest=batch.item)
    with pytest.raises(triage.TriageRefusal, match="queue-state-unreadable"):
        triage.append_decision(state_path, batch.queue, item_digest=batch.item, decision="decline")
    assert state_path.read_bytes() == torn


@pytest.mark.parametrize(
    "entries",
    [
        [{"item_sha256": "f" * 64, "decision": "decline", "draft_sha256": None}],
        None,
    ],
    ids=("foreign-item", "duplicate-item"),
)
def test_a_journal_refuses_history_that_is_not_unique_and_bound_to_this_queue(
    tmp_path: Path, entries: list[dict] | None
):
    """Closed record shapes cannot make forged or duplicate history append-only."""
    batch = _Batch(tmp_path)
    if entries is None:
        entry = {"item_sha256": batch.item, "decision": "decline", "draft_sha256": None}
        entries = [entry, dict(entry)]
    forged = {
        "schema": triage.STATE_SCHEMA,
        "queue_sha256": digest_of(batch.queue),
        "decisions": entries,
    }
    state_path = tmp_path / "state.json"
    state_path.write_bytes(canonical_bytes(forged))
    with pytest.raises(triage.TriageRefusal, match="queue-state-history-invalid"):
        triage.recorded_decision(state_path, batch.queue, item_digest=batch.item)
    assert state_path.read_bytes() == canonical_bytes(forged)


def test_append_republishes_prior_history_byte_identically(tmp_path: Path):
    """A new decision changes framing only; every byte of prior history stands."""
    batch = _Batch(tmp_path, ("a", "b", "c"))
    candidates = [item for item in batch.queue["items"] if item["kind"] == "cluster-candidate"]
    state_path = tmp_path / "state.json"
    triage.append_decision(
        state_path, batch.queue, item_digest=digest_of(candidates[0]), decision="decline"
    )
    before = state_path.read_bytes()
    history_end = before.index(b'],"queue_sha256"')
    prior_history = before[:history_end]

    triage.append_decision(
        state_path, batch.queue, item_digest=digest_of(candidates[1]), decision="decline"
    )
    after = state_path.read_bytes()
    assert after.startswith(prior_history + b",")


def test_concurrent_appends_cannot_replace_each_others_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The journal lock covers read through replace, so the second writer sees the first."""
    batch = _Batch(tmp_path, ("a", "b", "c"))
    candidates = [item for item in batch.queue["items"] if item["kind"] == "cluster-candidate"]
    state_path = tmp_path / "state.json"
    first_inside_write = threading.Event()
    release_first = threading.Event()
    second_finished = threading.Event()
    errors: list[BaseException] = []
    original_atomic = triage._atomic_bytes

    def held_first_write(path: Path, data: bytes) -> None:
        if not first_inside_write.is_set():
            first_inside_write.set()
            assert release_first.wait(5), "test did not release the first journal writer"
        original_atomic(path, data)

    def append(item: dict, *, finished: threading.Event | None = None) -> None:
        try:
            triage.append_decision(
                state_path, batch.queue, item_digest=digest_of(item), decision="decline"
            )
        except BaseException as error:
            errors.append(error)
        finally:
            if finished is not None:
                finished.set()

    monkeypatch.setattr(triage, "_atomic_bytes", held_first_write)
    first = threading.Thread(target=append, args=(candidates[0],), daemon=True)
    first.start()
    assert first_inside_write.wait(2), "first writer never reached journal publication"
    second = threading.Thread(
        target=append, args=(candidates[1],), kwargs={"finished": second_finished}, daemon=True
    )
    second.start()
    assert not second_finished.wait(0.2), "second writer passed the held journal lock"
    release_first.set()
    first.join(5)
    second.join(5)

    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    state = triage._read_canonical(state_path, "queue-state")
    assert {entry["item_sha256"] for entry in state["decisions"]} == {
        digest_of(candidates[0]),
        digest_of(candidates[1]),
    }


def test_unhashable_decision_value_is_a_named_refusal(tmp_path: Path):
    batch = _Batch(tmp_path)
    forged = {
        "schema": triage.STATE_SCHEMA,
        "queue_sha256": digest_of(batch.queue),
        "decisions": [
            {"item_sha256": batch.item, "decision": [], "draft_sha256": None},
        ],
    }
    state_path = tmp_path / "state.json"
    state_path.write_bytes(canonical_bytes(forged))
    with pytest.raises(triage.TriageRefusal, match="queue-state-entry-invalid"):
        triage.recorded_decision(state_path, batch.queue, item_digest=batch.item)


def test_float_in_a_read_document_is_a_named_noncanonical_refusal(tmp_path: Path):
    path = tmp_path / "state.json"
    path.write_bytes(b'{"value":1.5}')
    with pytest.raises(triage.TriageRefusal, match="queue-state-not-canonical"):
        triage._read_canonical(path, "queue-state")


def test_preview_refusal_happens_before_acceptance_is_journalled(tmp_path: Path):
    """Every predictable refusal must precede the irreversible half of accept."""
    batch = _Batch(tmp_path)
    state_path = tmp_path / "state.json"
    confirmation_path = tmp_path / "confirmation.json"
    with pytest.raises(triage.TriageRefusal, match="preview-changed"):
        triage.accept_candidate(
            state_path,
            batch.queue,
            item_digest=batch.item,
            draft=batch.draft(),
            confirmation_path=confirmation_path,
            preview_sha256="f" * 64,
        )
    assert not state_path.exists()
    assert not confirmation_path.exists()


def test_acceptance_refuses_one_path_for_state_and_confirmation(tmp_path: Path):
    """A path alias is refused before nested POSIX locks can self-deadlock."""
    batch = _Batch(tmp_path)
    shared = tmp_path / "shared.json"
    draft = batch.draft()
    with pytest.raises(triage.TriageRefusal, match="acceptance-paths-alias"):
        triage.accept_candidate(
            shared,
            batch.queue,
            item_digest=batch.item,
            draft=draft,
            confirmation_path=shared,
            preview_sha256=digest_of(draft),
        )
    assert not shared.exists()


def test_acceptance_refuses_case_variant_state_and_confirmation_paths(tmp_path: Path):
    """APFS is case-insensitive by default; two spellings must not alias silently.

    Neither `Path.resolve()` (which does not correct case) nor `samefile()`
    (which needs both paths to already exist) can see this collision before
    either file exists, so an operator naming the state journal `STATE.JSON`
    and the confirmation `state.json` would otherwise journal the acceptance
    to one directory entry and then have the confirmation's unconditional
    `os.replace` silently clobber it -- destroying the just-written journal
    with no refusal.
    """
    batch = _Batch(tmp_path)
    draft = batch.draft()
    state_path = tmp_path / "STATE.JSON"
    confirmation_path = tmp_path / "state.json"
    with pytest.raises(triage.TriageRefusal, match="acceptance-paths-alias"):
        triage.accept_candidate(
            state_path,
            batch.queue,
            item_digest=batch.item,
            draft=draft,
            confirmation_path=confirmation_path,
            preview_sha256=digest_of(draft),
        )
    assert not state_path.exists()
    assert not confirmation_path.exists()


def test_uncreatable_write_and_lock_resources_are_named_refusals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    batch = _Batch(tmp_path)
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("occupied", encoding="utf-8")
    with pytest.raises(triage.TriageRefusal, match="queue-state-lock-failed"):
        triage.append_decision(
            blocked_parent / "state.json",
            batch.queue,
            item_digest=batch.item,
            decision="decline",
        )
    with pytest.raises(triage.TriageRefusal, match="write-failed"):
        triage._atomic_bytes(blocked_parent / "record.json", b"{}")

    def refuse_temporary(*_args, **_kwargs):
        raise OSError("fixture temporary-file refusal")

    monkeypatch.setattr(triage.tempfile, "mkstemp", refuse_temporary)
    with pytest.raises(triage.TriageRefusal, match="temporary file could not be created"):
        triage._atomic_bytes(tmp_path / "record.json", b"{}")


@pytest.mark.parametrize("routed", [True, False], ids=("review-row", "candidate"))
def test_queue_items_require_usable_proxy_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, routed: bool
):
    batch = _Batch(tmp_path)
    paths = dict(batch.paths)
    paths[batch.candidate["both_digests"][0]] = "   "
    monkeypatch.setattr(triage, "routes_to_review", lambda _row, _path: routed)
    with pytest.raises(triage.TriageRefusal, match="proxy-path-missing"):
        triage.build_queue(
            batch.produced.manifest,
            batch.evidence,
            proxy_paths=paths,
            mode_declaration=triage.declare_mode("semi", batch_id="batch-1", operator="Tyrel"),
            triage_modes_path=MODES,
        )


@pytest.mark.parametrize("pair_edit", ["unsorted", "duplicate", "non-member"])
def test_confirmation_writer_refuses_producer_invalid_evidence_pairs(
    tmp_path: Path, pair_edit: str
):
    batch = _Batch(tmp_path)
    draft = batch.draft()
    pair = draft["clusters"][0]["evidence_pairs"][0]
    if pair_edit == "unsorted":
        edited_pair = list(reversed(pair))
    elif pair_edit == "duplicate":
        edited_pair = [pair[0], pair[0]]
    else:
        edited_pair = [pair[0], "f" * 64]
    edited = {
        **draft,
        "clusters": [{**draft["clusters"][0], "evidence_pairs": [edited_pair]}],
    }
    target = tmp_path / "confirmation.json"
    with pytest.raises(triage.TriageRefusal, match="draft-invalid"):
        triage.write_shown_confirmation(target, edited, preview_sha256=digest_of(edited))
    assert not target.exists()


def test_confirmation_writer_refuses_overlapping_clusters(tmp_path: Path):
    batch = _Batch(tmp_path)
    draft = batch.draft()
    edited = {**draft, "clusters": [draft["clusters"][0], draft["clusters"][0]]}
    target = tmp_path / "confirmation.json"
    with pytest.raises(triage.TriageRefusal, match="clusters overlap"):
        triage.write_shown_confirmation(target, edited, preview_sha256=digest_of(edited))
    assert not target.exists()


def test_occupied_confirmation_target_is_refused_before_journalling(tmp_path: Path):
    batch = _Batch(tmp_path)
    target = tmp_path / "confirmation.json"
    other = {**batch.draft(), "appending_run": "another-pass"}
    triage.write_shown_confirmation(target, other, preview_sha256=digest_of(other))
    stood = target.read_bytes()

    state_path = tmp_path / "state.json"
    draft = batch.draft()
    with pytest.raises(triage.TriageRefusal, match="confirmation-target-exists"):
        triage.accept_candidate(
            state_path,
            batch.queue,
            item_digest=batch.item,
            draft=draft,
            confirmation_path=target,
            preview_sha256=digest_of(draft),
        )
    assert not state_path.exists()
    assert target.read_bytes() == stood


def test_loader_refused_target_is_a_named_refusal_before_journalling(tmp_path: Path):
    batch = _Batch(tmp_path)
    target = tmp_path / "confirmation.json"
    target.write_bytes(canonical_bytes({**batch.draft(), "preferred": True}))
    stood = target.read_bytes()

    state_path = tmp_path / "state.json"
    draft = batch.draft()
    with pytest.raises(triage.TriageRefusal, match="confirmation-target-invalid"):
        triage.accept_candidate(
            state_path,
            batch.queue,
            item_digest=batch.item,
            draft=draft,
            confirmation_path=target,
            preview_sha256=digest_of(draft),
        )
    assert not state_path.exists()
    assert target.read_bytes() == stood


@pytest.mark.parametrize("edit", ["wrong-corpus", "foreign-members", "unsorted-members"])
def test_producer_refused_candidate_bindings_are_refused_before_journalling(
    tmp_path: Path, edit: str
):
    """The queue already holds enough evidence to reject these producer failures."""
    batch = _Batch(tmp_path)
    draft = batch.draft()
    if edit == "wrong-corpus":
        edited = {**draft, "corpus_id": "another-corpus"}
    else:
        members = (
            ["e" * 64, "f" * 64]
            if edit == "foreign-members"
            else [*reversed(batch.candidate["both_digests"])]
        )
        page = {**draft["clusters"][0]["pages"][0], "member_frame_sha256": members}
        edited = {
            **draft,
            "clusters": [{**draft["clusters"][0], "pages": [page]}],
        }
    with pytest.raises(ProducerRefusal):
        produce(
            batch.frames,
            corpus_id="c",
            mode="semi",
            confirmation=edited,
            instrument_recipe=batch.recipe,
            evidence_manifest=batch.manifest,
            evidence_records=batch.evidence,
        )

    state_path = tmp_path / "state.json"
    confirmation_path = tmp_path / "confirmation.json"
    with pytest.raises(triage.TriageRefusal, match="draft-(?:invalid|does-not-match-candidate)"):
        triage.accept_candidate(
            state_path,
            batch.queue,
            item_digest=batch.item,
            draft=edited,
            confirmation_path=confirmation_path,
            preview_sha256=digest_of(edited),
        )
    assert not state_path.exists()
    assert not confirmation_path.exists()


def test_an_interrupted_acceptance_is_completed_by_its_replay(tmp_path: Path):
    """An acceptance is two files; a crash between them must not strand the first.

    Journal-first ordering keeps an invalid draft from the confirmation file, but a
    replay must complete the second write without treating the first as a new decision.
    """
    batch = _Batch(tmp_path)
    state_path = tmp_path / "state.json"
    confirmation = tmp_path / "confirmation.json"
    draft = batch.draft()
    journalled = triage.append_decision(
        state_path, batch.queue, item_digest=batch.item, decision="accept", draft=draft
    )
    assert not confirmation.exists()
    state = triage.accept_candidate(
        state_path,
        batch.queue,
        item_digest=batch.item,
        draft=draft,
        confirmation_path=confirmation,
        preview_sha256=digest_of(draft),
    )
    assert confirmation.read_bytes() == canonical_bytes(draft)
    assert state["decisions"] == journalled["decisions"]
    assert triage._read_canonical(state_path, "queue-state") == journalled
    triage.accept_candidate(
        state_path,
        batch.queue,
        item_digest=batch.item,
        draft=draft,
        confirmation_path=confirmation,
        preview_sha256=digest_of(draft),
    )
    assert triage._read_canonical(state_path, "queue-state") == journalled


def test_a_decided_row_still_refuses_a_different_draft_or_a_reversal(tmp_path: Path):
    """Resuming an interrupted acceptance is not licence to redecide the row."""
    batch = _Batch(tmp_path)
    state_path = tmp_path / "state.json"
    draft = batch.draft()
    triage.append_decision(
        state_path, batch.queue, item_digest=batch.item, decision="accept", draft=draft
    )
    other = {**draft, "appending_run": "pass-2"}
    with pytest.raises(triage.TriageRefusal, match="decision-already-recorded"):
        triage.accept_candidate(
            state_path,
            batch.queue,
            item_digest=batch.item,
            draft=other,
            confirmation_path=tmp_path / "other.json",
            preview_sha256=digest_of(other),
        )
    assert not (tmp_path / "other.json").exists()

    declined_state = tmp_path / "declined.json"
    triage.append_decision(declined_state, batch.queue, item_digest=batch.item, decision="decline")
    declined_draft = batch.draft()
    with pytest.raises(triage.TriageRefusal, match="decision-already-recorded"):
        triage.accept_candidate(
            declined_state,
            batch.queue,
            item_digest=batch.item,
            draft=declined_draft,
            confirmation_path=tmp_path / "after-decline.json",
            preview_sha256=digest_of(declined_draft),
        )
    assert not (tmp_path / "after-decline.json").exists()


def test_an_unpinned_draft_never_becomes_a_journalled_acceptance(tmp_path: Path):
    """The placeholder pin must be refused at both irreversible write seams.

    Otherwise an acceptance can become bound to a confirmation the producer cannot
    commit, with no lawful path to decide the row again.
    """
    batch = _Batch(tmp_path)
    unpinned = batch.draft(pinned=False)
    assert unpinned["evidence_manifest_sha256"] == triage.UNPINNED_EVIDENCE_MANIFEST
    with pytest.raises(triage.TriageRefusal, match="draft-unpinned"):
        triage.write_shown_confirmation(
            tmp_path / "confirmation.json", unpinned, preview_sha256=digest_of(unpinned)
        )
    with pytest.raises(triage.TriageRefusal, match="draft-unpinned"):
        triage.append_decision(
            tmp_path / "state.json",
            batch.queue,
            item_digest=batch.item,
            decision="accept",
            draft=unpinned,
        )
    assert not (tmp_path / "confirmation.json").exists()
    assert not (tmp_path / "state.json").exists()


def test_a_console_acceptance_is_committable_and_edits_to_it_are_refused(tmp_path: Path):
    """The console's draft and the 6B producer's validation actually meet.

    Both directions, against the real instrument and producer rather than the
    console's own opinion of them: the confirmation this console writes is
    accepted by `produce` and appends to the register, and a draft edited to
    disagree with the evidence it names is refused by the producer.
    """
    batch = _Batch(tmp_path)
    confirmation_path = tmp_path / "confirmation.json"
    draft = batch.draft()
    triage.accept_candidate(
        batch_state := tmp_path / "state.json",
        batch.queue,
        item_digest=batch.item,
        draft=draft,
        confirmation_path=confirmation_path,
        preview_sha256=digest_of(draft),
    )
    assert (
        triage._read_canonical(batch_state, "queue-state")["decisions"][0]["decision"] == "accept"
    )
    written = load_confirmation(confirmation_path)
    assert confirmation_path.read_bytes() == canonical_bytes(written) == canonical_bytes(draft)
    committed = produce(
        batch.frames,
        corpus_id="c",
        mode="semi",
        confirmation=written,
        instrument_recipe=batch.recipe,
        evidence_manifest=batch.manifest,
        evidence_records=batch.evidence,
    )
    assert len(committed.clusters) == 1
    append_confirmation_to_register(
        written,
        committed,
        instrument_recipe=batch.recipe,
        evidence_manifest=batch.manifest,
        evidence_records=batch.evidence,
        register_path=tmp_path / "register.json",
    )

    def _edited(**cluster_changes) -> dict:
        cluster = {**written["clusters"][0], **cluster_changes}
        return {**written, "clusters": [cluster]}

    refused = [
        _edited(evidence_pairs=[["e" * 64, "f" * 64]]),
        {**written, "evidence_manifest_sha256": "a" * 64},
        _edited(
            pages=[{**written["clusters"][0]["pages"][0], "member_frame_sha256": ["e" * 64] * 2}]
        ),
    ]
    for edited in refused:
        with pytest.raises(ProducerRefusal):
            produce(
                batch.frames,
                corpus_id="c",
                mode="semi",
                confirmation=edited,
                instrument_recipe=batch.recipe,
                evidence_manifest=batch.manifest,
                evidence_records=batch.evidence,
            )


def test_a_recorded_decision_makes_its_directory_entry_durable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A decision reported as recorded must survive a power cut, not only a process exit.

    Without the parent-directory fsync the rename can be lost after this call has
    returned success, so resume reads a journal that never heard of a decision the
    operator was told was made. The same write also has to leave no hidden
    temporary behind, because this directory is the operator's durable state.
    """
    batch = _Batch(tmp_path)
    synced: list[tuple[Path, bool]] = []
    original = triage.sync_directory
    monkeypatch.setattr(
        triage,
        "sync_directory",
        lambda path, *, strict=False: (
            synced.append((Path(path), strict)),
            original(path, strict=strict),
        )[-1],
    )
    state_path = tmp_path / "records" / "state.json"
    triage.append_decision(state_path, batch.queue, item_digest=batch.item, decision="decline")
    assert (state_path.parent, True) in synced
    assert sorted(entry.name for entry in state_path.parent.iterdir()) == [
        ".state.json.lock",
        "state.json",
    ]


def test_the_double_click_triage_route_shows_the_queue_and_records_no_decision(monkeypatch):
    """Display-only is the design here, so it is held rather than left to drift.

    Acceptance is pinned to `--preview-sha256`, the digest of the draft the
    operator was shown. A blind prompt chain cannot honestly produce that: it
    would ask someone to confirm a digest they never saw, which is the single
    thing that confirmation exists to prevent. Decline is withheld alongside it
    rather than shipping half a decision surface where a queue item can be
    dismissed before its evidence is on screen.

    If a later change does build a real interactive review surface, this test is
    the thing it has to come and change on purpose — which is the point.
    """
    from operations.operator import cli

    answers = iter(
        (
            "triage",
            "/approved/manifest.json",
            "/approved/evidence.json",
            "/approved/proxies.json",
            "semi",
            "batch-1",
            "Tyrel",
            "/approved/mode.json",
        )
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    arguments = cli._interactive_arguments()

    assert arguments[0] == "triage"
    for flag in ("--queue-state", "--accept", "--decline", "--draft", "--preview-sha256"):
        assert flag not in arguments, (
            f"the double-click triage route offered {flag}; a decision recorded there "
            "would be made without the queue and its digests on screen"
        )


def test_the_triage_verb_refuses_accept_and_decline_as_one_operator_act(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    from operations.operator import cli

    mode_record = tmp_path / "mode.json"
    result = cli.main(
        [
            "--workspace",
            str(MODES.parents[1]),
            "--state-dir",
            str(tmp_path / "receipts"),
            "triage",
            "--manifest",
            str(tmp_path / "missing-manifest.json"),
            "--evidence",
            str(tmp_path / "missing-evidence.json"),
            "--proxy-paths",
            str(tmp_path / "missing-proxies.json"),
            "--mode",
            "semi",
            "--batch-id",
            "batch-1",
            "--operator",
            "Tyrel",
            "--mode-record",
            str(mode_record),
            "--decline",
            "a" * 64,
            "--accept",
            "b" * 64,
        ]
    )
    assert result == 2
    assert "not allowed with argument" in capsys.readouterr().out
    assert not mode_record.exists()


@pytest.mark.parametrize(
    ("flags", "refusal"),
    (
        (("--accept", "a" * 64), "acceptance-incomplete"),
        (("--decline", "a" * 64), "queue-state-required"),
    ),
)
def test_an_incomplete_triage_decision_writes_no_mode_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], flags: tuple[str, ...], refusal: str
):
    """A command that refuses must not have claimed the batch's mode on its way.

    The mode declaration was written before these flag sets were checked, and
    `write_mode_declaration` refuses to rewrite a batch's declared mode once it
    exists. So an operator whose first attempt was incomplete had the record
    already standing, and correcting the invocation to a different `--mode` was
    then refused by the abandoned attempt rather than accepted. The paths below
    do not exist on purpose: the refusal has to arrive before anything is read
    or written at all.
    """
    from operations.operator import cli

    mode_record = tmp_path / "mode.json"
    result = cli.main(
        [
            "--workspace",
            str(MODES.parents[1]),
            "--state-dir",
            str(tmp_path / "receipts"),
            "triage",
            "--manifest",
            str(tmp_path / "missing-manifest.json"),
            "--evidence",
            str(tmp_path / "missing-evidence.json"),
            "--proxy-paths",
            str(tmp_path / "missing-proxies.json"),
            "--mode",
            "semi",
            "--batch-id",
            "batch-1",
            "--operator",
            "Tyrel",
            "--mode-record",
            str(mode_record),
            *flags,
        ]
    )

    assert result == 2
    assert refusal in capsys.readouterr().out
    assert not mode_record.exists()
    assert not mode_record.parent.joinpath(".mode.json.lock").exists()


def test_an_unloadable_batch_writes_no_mode_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    """The same claim, one step further out: a batch that will not load.

    `load_queue` refuses an unreadable or non-canonical manifest, evidence map
    or proxy map. With the declaration persisted ahead of that load, a plain
    typo in a path left the batch's mode declared by a command that showed the
    operator nothing — and since the mode is not rewritten, the corrected
    invocation could no longer choose a different one.
    """
    from operations.operator import cli

    mode_record = tmp_path / "mode.json"
    for name in ("manifest", "evidence", "proxies"):
        (tmp_path / f"{name}.json").write_bytes(b"{ not canonical json")

    result = cli.main(
        [
            "--workspace",
            str(MODES.parents[1]),
            "--state-dir",
            str(tmp_path / "receipts"),
            "triage",
            "--manifest",
            str(tmp_path / "manifest.json"),
            "--evidence",
            str(tmp_path / "evidence.json"),
            "--proxy-paths",
            str(tmp_path / "proxies.json"),
            "--mode",
            "semi",
            "--batch-id",
            "batch-1",
            "--operator",
            "Tyrel",
            "--mode-record",
            str(mode_record),
        ]
    )

    assert result == 2
    assert "manifest-unreadable" in capsys.readouterr().out
    assert not mode_record.exists()


def test_the_triage_verb_accepts_and_resumes_from_the_command_line(tmp_path: Path):
    """The operator's actual path through the console, including the resume.

    The library-level tests bind the write rules; this binds the seam that calls
    them, which is where the acceptance's two writes are sequenced.
    """
    from operations.operator import cli

    batch = _Batch(tmp_path)
    files = {
        "manifest": tmp_path / "manifest.json",
        "evidence": tmp_path / "evidence.json",
        "proxies": tmp_path / "proxies.json",
    }
    files["manifest"].write_bytes(canonical_bytes(batch.produced.manifest))
    files["evidence"].write_bytes(canonical_bytes({"records": batch.evidence}))
    files["proxies"].write_bytes(canonical_bytes(batch.paths))
    draft = batch.draft()
    draft_path = tmp_path / "draft.json"
    draft_path.write_bytes(canonical_bytes(draft))
    state_path = tmp_path / "state.json"
    confirmation = tmp_path / "confirmation.json"
    common = [
        "--workspace",
        str(MODES.parents[1]),
        "--state-dir",
        str(tmp_path / "receipts"),
        "triage",
        "--manifest",
        str(files["manifest"]),
        "--evidence",
        str(files["evidence"]),
        "--proxy-paths",
        str(files["proxies"]),
        "--mode",
        "semi",
        "--batch-id",
        "batch-1",
        "--operator",
        "Tyrel",
        "--mode-record",
        str(tmp_path / "mode.json"),
        "--queue-state",
        str(state_path),
    ]
    assert cli.main(common) == 0
    assert triage._read_canonical(tmp_path / "mode.json", "mode")["mode"] == "semi"

    accept = [
        *common,
        "--accept",
        batch.item,
        "--draft",
        str(draft_path),
        "--confirmation-out",
        str(confirmation),
        "--preview-sha256",
        digest_of(draft),
    ]
    assert cli.main(accept) == 0
    assert confirmation.read_bytes() == canonical_bytes(draft)
    journal = triage._read_canonical(state_path, "queue-state")

    confirmation.unlink()
    assert cli.main(accept) == 0
    assert confirmation.read_bytes() == canonical_bytes(draft)
    assert triage._read_canonical(state_path, "queue-state") == journal

    other = {**draft, "appending_run": "pass-2"}
    other_path = tmp_path / "other.json"
    other_path.write_bytes(canonical_bytes(other))
    changed = [
        *common,
        "--accept",
        batch.item,
        "--draft",
        str(other_path),
        "--confirmation-out",
        str(tmp_path / "other-confirmation.json"),
        "--preview-sha256",
        digest_of(other),
    ]
    assert cli.main(changed) == 2
    assert not (tmp_path / "other-confirmation.json").exists()
