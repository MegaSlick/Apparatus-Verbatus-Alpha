"""The door, driven over a real run tree.

Spec 03's tests 1, 2, 4 and 5 land here in the form that needs a run: an
all-refused input set exiting loud, the PDF fan-out sealing each page's rendered
bytes once, ordinals stable across reruns, and duplicate sources named. Test 7's
door half is here too — real input refused without a current approval, before a
single byte is read.

Everything drives the real `RunTree` and the real `StageContext`: no in-memory
stand-in, because what is under test is what actually lands on disk.
"""

import json

import door
import pdf_render
import pytest
from admission import RefusalReason, load_format_policy, reason_code
from door import SourceEntry, expand_sources, process_sources
from synthetic_sources import gif, jpeg, png, single_gray_page_pdf, tiff, two_page_pdf

from common.contracts.approval import build_approval_record, synthetic_fixture_ingress_record
from common.contracts.canonical import digest_bytes
from common.contracts.errors import ContractError
from common.contracts.stages import DOOR
from common.runtree.store import RunTree
from common.stage import StageContext
from operations.submit import gate

POLICY = load_format_policy()
RECIPES = {"door": "fake-door-v0", "exemplar": "fake-exemplar-v0"}
CHAIRS = ["attestator_1", "attestator_2", "attestator_3"]


def open_door(tmp_path, sources, *, run_id="r1"):
    """A run tree bound to `sources`, and a door context writing into it."""
    tree = RunTree.create(
        tmp_path / "runs",
        run_id,
        source_manifest=[
            {
                "relative_path": entry.declared_path,
                "sha256": entry.declared_sha256,
                "ordinal": entry.ordinal,
            }
            for entry in sources
        ],
        config_digest="c" * 64,
        adapter_recipes=RECIPES,
        witness_chairs=CHAIRS,
        ingress=synthetic_fixture_ingress_record(),
    )
    run = tree.read_run()
    context = StageContext(
        tree=tree,
        run=run,
        fixture={},
        scenario="test",
        stage=DOOR,
        adapter_revision=RECIPES["door"],
        args=None,
        registry=None,
    )
    return tree, context


def admissions(tree) -> dict[int, dict]:
    """Every published admission, by ordinal."""
    published = {}
    for entry in tree.build_manifest(DOOR)["artifacts"]:
        if entry["kind"] != "admission":
            continue
        record = tree.read_artifact(DOOR, "admission", entry["artifact_id"])
        published[record["payload"]["ordinal"]] = record
    return published


def reader(files: dict[str, bytes]):
    def read_bytes(relative_path: str) -> bytes:
        try:
            return files[relative_path]
        except KeyError:
            raise OSError(f"no such source: {relative_path}") from None

    return read_bytes


# --- Test 1: admission by bytes, at the door ------------------------------------


def test_a_genuine_jpeg_named_png_is_admitted_under_the_format_its_bytes_prove(tmp_path):
    data = jpeg(6, 4)
    sources = [SourceEntry(1, "scan-01.png", digest_bytes(data))]
    tree, context = open_door(tmp_path, sources)

    assert (
        process_sources(
            context, tree, sources, reader({"scan-01.png": data}), policy=POLICY, is_fixture=True
        )
        == 1
    )

    context.finish(DOOR)
    record = admissions(tree)[1]
    assert record["outcome"] == "admitted"
    assert record["payload"]["geometry"] == {"width": 6, "height": 4}
    assert tree.read_bytes(record["payload"]["stored_at"]) == data


def test_a_text_file_named_png_is_refused_and_the_reason_names_why(tmp_path):
    data = b"this is not an image, whatever it is called"
    sources = [SourceEntry(1, "scan-01.png", digest_bytes(data))]
    tree, context = open_door(tmp_path, sources)

    admitted = process_sources(
        context, tree, sources, reader({"scan-01.png": data}), policy=POLICY, is_fixture=True
    )
    context.finish(DOOR)

    assert admitted == 0
    record = admissions(tree)[1]
    assert record["outcome"] == "refused"
    assert reason_code(record["payload"]["reason"]) is RefusalReason.UNRECOGNIZED_FORMAT


# --- Test 2: nothing is silently omitted, and nothing exits quietly --------------


def test_an_unreadable_source_is_refused_by_name_and_the_rest_still_decide(tmp_path):
    """Per-file, never per-folder (harvest #2). One missing file must not take the
    readable pages with it, and it must not vanish either."""
    good = png(3, 2)
    sources = [
        SourceEntry(1, "gone.png", "0" * 64),
        SourceEntry(2, "here.png", digest_bytes(good)),
    ]
    tree, context = open_door(tmp_path, sources)

    admitted = process_sources(
        context, tree, sources, reader({"here.png": good}), policy=POLICY, is_fixture=True
    )
    context.finish(DOOR)

    assert admitted == 1
    published = admissions(tree)
    assert set(published) == {1, 2}
    assert reason_code(published[1]["payload"]["reason"]) is RefusalReason.UNREADABLE
    assert published[2]["outcome"] == "admitted"


def test_an_input_set_that_admitted_nothing_is_a_loud_failure(tmp_path):
    """Harvest #3, verbatim: never a green DONE with no output."""
    sources = [SourceEntry(1, "junk.png", "0" * 64)]
    tree, context = open_door(tmp_path, sources)
    admitted = process_sources(
        context, tree, sources, reader({"junk.png": b"junk"}), policy=POLICY, is_fixture=True
    )
    context.finish(DOOR)

    with pytest.raises(ContractError, match="admitted nothing"):
        door.require_some_admitted(admitted)

    # The refusal survives the failure. A run that died leaving no record of what
    # arrived would be exactly the silent loss the loud failure exists to prevent.
    assert (
        reason_code(admissions(tree)[1]["payload"]["reason"]) is RefusalReason.UNRECOGNIZED_FORMAT
    )


def test_the_repository_fixture_root_is_the_only_folder_called_synthetic(tmp_path):
    """Ruling 2026-08-04, item 1. A caller-owned folder is real input, whatever it
    is called — otherwise `--fixture-root` is a flag that turns the gate off."""
    assert door.declared_synthetic_fixture_root("proof") == door.DECLARED_SYNTHETIC_FIXTURE_ROOT
    pretend = tmp_path / "proof"
    pretend.mkdir()
    with pytest.raises(ContractError, match="not the declared synthetic fixture root"):
        door.declared_synthetic_fixture_root(str(pretend))


# --- Test 4: the PDF path, rendered once and sealed ------------------------------


def test_a_pdf_fans_out_into_one_ordinal_per_page(tmp_path):
    data = two_page_pdf()
    files = {"book.pdf": data}
    sources = expand_sources(
        [{"relative_path": "book.pdf", "sha256": digest_bytes(data)}], reader(files), POLICY
    )
    assert [(entry.ordinal, entry.pdf_page_index) for entry in sources] == [(1, 0), (2, 1)]

    tree, context = open_door(tmp_path, sources)
    assert (
        process_sources(context, tree, sources, reader(files), policy=POLICY, is_fixture=True) == 2
    )
    context.finish(DOOR)

    published = admissions(tree)
    assert {
        ordinal: record["payload"]["pdf_page_index"] for ordinal, record in published.items()
    } == {
        1: 0,
        2: 1,
    }
    # The sealed bytes are the rendered page, never the container, and the digest
    # of the container travels beside them as the recorded transform's source.
    for ordinal, record in published.items():
        payload = record["payload"]
        assert payload["source_sha256"] == digest_bytes(data)
        assert payload["sha256"] != digest_bytes(data)
        rendered, _ = pdf_render.render_page(data, payload["pdf_page_index"])
        assert tree.read_bytes(payload["stored_at"]) == rendered
        assert payload["stored_at"] == tree.blob_path(DOOR, digest_bytes(rendered))
        assert ordinal in (1, 2)


def test_a_pdf_page_is_rendered_at_the_door_and_never_again(tmp_path):
    """The blob is content-addressed, so a second run over identical bytes reuses it
    rather than rewriting it — and nothing downstream has an API to re-render with,
    which `test_import_boundaries.py` proves statically."""
    data = single_gray_page_pdf()
    files = {"scan.pdf": data}
    sources = expand_sources(
        [{"relative_path": "scan.pdf", "sha256": digest_bytes(data)}], reader(files), POLICY
    )
    tree, context = open_door(tmp_path, sources)
    process_sources(context, tree, sources, reader(files), policy=POLICY, is_fixture=True)
    context.finish(DOOR)
    first = {path: path.read_bytes() for path in sorted((tree.root).rglob("*")) if path.is_file()}

    tree_again, context_again = open_door(tmp_path, sources)
    process_sources(
        context_again, tree_again, sources, reader(files), policy=POLICY, is_fixture=True
    )
    context_again.finish(DOOR)
    second = {
        path: path.read_bytes() for path in sorted((tree_again.root).rglob("*")) if path.is_file()
    }
    assert first == second


def test_an_unrenderable_pdf_page_is_refused_and_still_holds_its_ordinal(tmp_path):
    data = single_gray_page_pdf(rotate=90)
    files = {"rotated.pdf": data}
    sources = expand_sources(
        [{"relative_path": "rotated.pdf", "sha256": digest_bytes(data)}], reader(files), POLICY
    )
    tree, context = open_door(tmp_path, sources)
    admitted = process_sources(
        context, tree, sources, reader(files), policy=POLICY, is_fixture=True
    )
    context.finish(DOOR)

    assert admitted == 0
    record = admissions(tree)[1]
    assert reason_code(record["payload"]["reason"]) is RefusalReason.UNSUPPORTED_VARIANT


def test_an_unopenable_pdf_occupies_exactly_one_ordinal(tmp_path):
    """One declared source refused as a whole, still accounted for by ordinal."""
    broken = b"%PDF-1.4\nnothing else at all"
    sources = expand_sources(
        [{"relative_path": "broken.pdf", "sha256": digest_bytes(broken)}],
        reader({"broken.pdf": broken}),
        POLICY,
    )
    assert len(sources) == 1 and sources[0].pdf_page_index == 0


def test_a_pdf_declared_without_a_page_index_is_refused_rather_than_guessed_at(tmp_path):
    data = single_gray_page_pdf()
    sources = [SourceEntry(1, "scan.pdf", digest_bytes(data), None)]
    tree, context = open_door(tmp_path, sources)
    process_sources(
        context, tree, sources, reader({"scan.pdf": data}), policy=POLICY, is_fixture=True
    )
    context.finish(DOOR)

    record = admissions(tree)[1]
    assert reason_code(record["payload"]["reason"]) is RefusalReason.UNSUPPORTED_VARIANT
    assert "page index" in record["payload"]["reason"]


def test_a_pdf_whose_container_digest_disagrees_with_its_declaration_is_refused(tmp_path):
    """A tampered container is untrustworthy for every page inside it, so the whole
    file's digest is checked once rather than per page."""
    data = two_page_pdf()
    sources = [
        SourceEntry(1, "book.pdf", "0" * 64, 0),
        SourceEntry(2, "book.pdf", "0" * 64, 1),
    ]
    tree, context = open_door(tmp_path, sources)
    process_sources(
        context, tree, sources, reader({"book.pdf": data}), policy=POLICY, is_fixture=True
    )
    context.finish(DOOR)

    for record in admissions(tree).values():
        assert reason_code(record["payload"]["reason"]) is RefusalReason.DIGEST_MISMATCH


# --- Test 5: ordinals and duplicates ---------------------------------------------


def test_ordinals_are_assigned_deterministically_by_path(tmp_path):
    files = {"b.png": png(2, 2), "a.pdf": two_page_pdf(), "c.tif": tiff(3, 3)}
    rows = [{"relative_path": name, "sha256": digest_bytes(data)} for name, data in files.items()]
    first = expand_sources(rows, reader(files), POLICY)
    second = expand_sources(list(reversed(rows)), reader(files), POLICY)
    assert first == second
    assert [(entry.ordinal, entry.declared_path) for entry in first] == [
        (1, "a.pdf"),
        (2, "a.pdf"),
        (3, "b.png"),
        (4, "c.tif"),
    ]


def test_a_duplicate_source_file_is_refused_by_name_and_names_the_first(tmp_path):
    data = png(3, 3)
    sources = [
        SourceEntry(1, "scan-01.png", digest_bytes(data)),
        SourceEntry(2, "scan-01-copy.png", digest_bytes(data)),
    ]
    tree, context = open_door(tmp_path, sources)
    admitted = process_sources(
        context,
        tree,
        sources,
        reader({"scan-01.png": data, "scan-01-copy.png": data}),
        policy=POLICY,
        is_fixture=True,
    )
    context.finish(DOOR)

    assert admitted == 1
    published = admissions(tree)
    assert published[1]["outcome"] == "admitted"
    assert reason_code(published[2]["payload"]["reason"]) is RefusalReason.DUPLICATE
    assert "source-1" in published[2]["payload"]["reason"]


def test_two_byte_identical_pdf_pages_are_both_admitted(tmp_path):
    """Two blank pages in one scanned book are a real, common case. Refusing the
    second as a duplicate would lose a page that genuinely exists — and the blob
    store already dedupes their bytes on disk without calling either one a refusal."""
    data = pdf_with_two_identical_pages()
    files = {"blank.pdf": data}
    sources = expand_sources(
        [{"relative_path": "blank.pdf", "sha256": digest_bytes(data)}], reader(files), POLICY
    )
    tree, context = open_door(tmp_path, sources)
    admitted = process_sources(
        context, tree, sources, reader(files), policy=POLICY, is_fixture=True
    )
    context.finish(DOOR)

    assert admitted == 2
    published = admissions(tree)
    assert {record["payload"]["sha256"] for record in published.values()} == {
        published[1]["payload"]["sha256"]
    }, "the two pages really are byte-identical, which is the point of this case"
    assert all(record["outcome"] == "admitted" for record in published.values())


def pdf_with_two_identical_pages() -> bytes:
    from synthetic_sources import gray_image, image_page_pdf

    return image_page_pdf([gray_image(4, 3, 255), gray_image(4, 3, 255)])


# --- Test 7, door half: real input is gated before a byte is read -----------------


def test_real_input_with_no_approval_refuses_before_any_source_is_opened(tmp_path):
    opened: list[str] = []

    def watched(relative_path: str) -> bytes:
        opened.append(relative_path)
        return png()

    sources = [SourceEntry(1, "real.png", None)]
    tree, context = open_door(tmp_path, sources)
    with pytest.raises(gate.GateRefusal, match="none was supplied"):
        process_sources(
            context,
            tree,
            sources,
            watched,
            policy=POLICY,
            is_fixture=False,
            approval=None,
            data_policy=gate.load_policy(),
        )
    assert opened == [], "the gate must refuse before a single source is read"
    assert admissions(tree) == {}


def test_fixture_input_needs_no_approval_at_all(tmp_path):
    data = png(2, 2)
    sources = [SourceEntry(1, "page-1.png", digest_bytes(data))]
    tree, context = open_door(tmp_path, sources)
    assert (
        process_sources(
            context, tree, sources, reader({"page-1.png": data}), policy=POLICY, is_fixture=True
        )
        == 1
    )


def test_a_real_run_seals_the_approval_into_every_admission_it_publishes(tmp_path, monkeypatch):
    """The CLI end to end, on synthetic bytes standing in for real input. No real
    image has been touched; what is proven is that the gate is what would let one in."""
    policy_path = tmp_path / "policy.json"
    policy = json.loads(gate.DEFAULT_POLICY_PATH.read_text(encoding="utf-8"))
    submission = tmp_path / "approved" / "batch"
    submission.mkdir(parents=True)
    (submission / "page-1.png").write_bytes(png(4, 3))
    (submission / "page-2.jpg").write_bytes(jpeg(5, 4))
    (submission / "refused.gif").write_bytes(gif())
    policy["storage_roots"] = [str(tmp_path / "approved")]
    policy_path.write_text(json.dumps(policy), encoding="utf-8")

    approval_path = tmp_path / "approval.json"
    approval_path.write_text(
        json.dumps(
            build_approval_record(
                subject_ids=["data-handling-policy"],
                action="data-gate",
                reason="synthetic proving run for spec 03",
                target_version_hash=gate.policy_hash(policy),
                timestamp="2026-08-04T12:00:00Z",
            )
        ),
        encoding="utf-8",
    )

    run_root = tmp_path / "approved" / "runs"
    monkeypatch.setattr(
        "sys.argv",
        [
            "door.py",
            "--run-root",
            str(run_root),
            "--run-id",
            "real-1",
            "--submission-folder",
            str(submission),
            "--approval-record",
            str(approval_path),
            "--data-gate-policy",
            str(policy_path),
        ],
    )
    assert door.main() == 0

    tree = RunTree(run_root, "real-1")
    run = tree.read_run()
    assert run["ingress"]["mode"] == "approval-gated-real"
    assert run["ingress"]["data_gate_policy_hash"] == gate.policy_hash(policy)

    published = admissions(tree)
    assert len(published) == 3
    assert {record["outcome"] for record in published.values()} == {"admitted", "refused"}
    for record in published.values():
        assert (
            record["payload"]["data_gate_approval_ref"] == run["ingress"]["data_gate_approval_ref"]
        )
    stored = tree.read_approval_record(gate.read_external_approval(approval_path, policy)[1])
    assert stored["action"] == "data-gate"


def test_a_real_submission_outside_the_approved_storage_roots_is_refused(tmp_path, monkeypatch):
    policy_path = tmp_path / "policy.json"
    policy = json.loads(gate.DEFAULT_POLICY_PATH.read_text(encoding="utf-8"))
    approved = tmp_path / "approved"
    approved.mkdir()
    policy["storage_roots"] = [str(approved)]
    policy_path.write_text(json.dumps(policy), encoding="utf-8")

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "page-1.png").write_bytes(png())
    approval_path = tmp_path / "approval.json"
    approval_path.write_text(
        json.dumps(
            build_approval_record(
                subject_ids=["data-handling-policy"],
                action="data-gate",
                reason="synthetic proving run",
                target_version_hash=gate.policy_hash(policy),
                timestamp="2026-08-04T12:00:00Z",
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "door.py",
            "--run-root",
            str(approved / "runs"),
            "--run-id",
            "real-2",
            "--submission-folder",
            str(elsewhere),
            "--approval-record",
            str(approval_path),
            "--data-gate-policy",
            str(policy_path),
        ],
    )
    with pytest.raises(gate.GateRefusal, match="outside every approved storage root"):
        door.main()
    assert not (approved / "runs").exists()


def test_an_approval_record_offered_without_a_real_folder_is_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "sys.argv",
        [
            "door.py",
            "--run-root",
            str(tmp_path / "runs"),
            "--run-id",
            "r1",
            "--approval-record",
            str(tmp_path / "approval.json"),
        ],
    )
    with pytest.raises(ContractError, match="meaningful only with a real submission folder"):
        door.main()


def test_the_page_fan_out_really_does_read_every_declared_file():
    """Why the gate has to be enforced *before* `expand_sources`, not only inside
    the admission loop after it: counting a PDF's pages means opening the file, and
    a submission with no approval must not have its files opened at all."""
    opened: list[str] = []
    files = {"a.png": png(2, 2), "b.pdf": single_gray_page_pdf()}

    def watched(relative_path: str) -> bytes:
        opened.append(relative_path)
        return files[relative_path]

    rows = [{"relative_path": name, "sha256": digest_bytes(data)} for name, data in files.items()]
    expand_sources(rows, watched, POLICY)
    assert sorted(opened) == ["a.png", "b.pdf"]
