"""The one page-witness roster reader every stage uses, its roster check, and the
check of a record reader's blank testimony.

The Attestatores, the Perlector and the Recensor each call
`declared_page_witness_chairs` on their own run authority, so what it derives
from the sealed roster, and what it refuses, is every stage's answer.
"""

import copy
from collections import OrderedDict
from concurrent.futures.process import BrokenProcessPool
from types import SimpleNamespace

import pytest

from common import imaging, native_witness, page_testimonia
from common.chairs.models import AbsentChair, ChairIdentity
from common.contracts.canonical import digest_bytes
from common.contracts.errors import FatalAccounting, SchemaRefusal
from common.contracts.stages import ATTESTATORES
from common.imaging import encode_grayscale_png_deterministic
from common.page_testimonia import (
    current_page_testimonia,
    declared_page_witness_chairs,
    require_page_roster,
    validate_page_testimonium_record,
)
from common.runtree.store import RunTree
from conftest import build_page_tree, page_context


def _identity(role: str, scope: str) -> ChairIdentity:
    return ChairIdentity(
        role=role,
        source="local-repository",
        repo=None,
        path=role,
        revision=None,
        digest_manifest="a" * 64,
        manifest=f"manifests/{role}.json",
        adapter_of=None,
        serving_recipe="fixture",
        license_note="fixture",
        witness_adapter="churro.v1",
        witness_scope=scope,
    )


def _context(scopes: dict[str, str], *, roster=None, absent=(), fixture=None):
    configured: dict[str, object] = {role: _identity(role, scope) for role, scope in scopes.items()}
    for role in absent:
        configured[role] = AbsentChair(role=role, reason="fixture absence")
    return SimpleNamespace(
        fixture={} if fixture is None else fixture,
        witness_chairs=list(scopes) + list(absent) if roster is None else roster,
        registry=SimpleNamespace(config=SimpleNamespace(chairs=configured)),
    )


# Case 0 is the scope layout shipped in config/models.toml.
DERIVED = (
    (
        _context({"attestator_1": "page", "attestator_2": "act", "attestator_3": "page"}),
        {"attestator_1", "attestator_3"},
    ),
    (_context({"attestator_1": "act", "attestator_2": "act"}), set()),
    (
        _context({"attestator_1": "page", "attestator_2": "page"}),
        {"attestator_1", "attestator_2"},
    ),
    # An explicit absence parses no scope at all, so it is never page-scoped.
    (_context({"attestator_1": "page"}, absent=("attestator_2",)), {"attestator_1"}),
    # The retired fixture key must not reach the answer.
    (
        _context(
            {"attestator_1": "page", "attestator_2": "act"},
            fixture={"page_witness_chairs": ["attestator_2"]},
        ),
        {"attestator_1"},
    ),
)


@pytest.mark.parametrize(("context", "expected"), DERIVED, ids=range(len(DERIVED)))
def test_the_page_scoped_set_is_what_the_sealed_roster_implies(context, expected):
    assert declared_page_witness_chairs(context) == expected


REFUSED = (
    _context({"attestator_1": "page"}, roster="attestator_1"),
    _context({"attestator_1": "page"}, roster=["attestator_1", "attestator_1"]),
    _context({"attestator_1": "page"}, roster=["attestator_1", 3]),
    _context({"attestator_1": "page"}, roster=["attestator_1", "attestator_9"]),
)


@pytest.mark.parametrize("context", REFUSED, ids=range(len(REFUSED)))
def test_a_roster_no_scope_can_be_derived_from_is_refused(context):
    with pytest.raises(SchemaRefusal, match="sealed witness roster"):
        declared_page_witness_chairs(context)


def _record(chair: str) -> dict:
    return {"payload": {"chair": chair}}


def test_a_page_carries_exactly_the_configured_page_witnesses():
    chairs = {"attestator_1", "attestator_3"}
    require_page_roster("pg_1", [_record("attestator_1"), _record("attestator_3")], chairs)
    with pytest.raises(FatalAccounting, match=r"no current page Testimonium .*attestator_3"):
        require_page_roster("pg_1", [_record("attestator_1")], chairs)
    with pytest.raises(FatalAccounting, match=r"did not seal as page witnesses"):
        require_page_roster(
            "pg_1",
            [_record("attestator_1"), _record("attestator_2"), _record("attestator_3")],
            chairs,
        )


# --- a record reader's blank testimony ----------------------------------------------


@pytest.fixture(scope="module")
def blank_testimony(tmp_path_factory):
    """`page-no-act`, whose record detector finds nothing on page 2: DAI's page record there."""
    root, options = build_page_tree(tmp_path_factory.mktemp("page-no-act"), "page-no-act")
    tree = RunTree(root, "r")
    [record] = [
        record
        for entry in tree.build_manifest(ATTESTATORES)["artifacts"]
        if entry["kind"] == "page-testimonium"
        and (record := tree.read_artifact(ATTESTATORES, entry["kind"], entry["artifact_id"]))[
            "payload"
        ]["chair"]
        == "attestator_2"
        and record["payload"]["page_ordinal"] == 2
    ]
    context = page_context(root, "r", "page-no-act", options)
    return context, record


def test_a_page_the_detector_found_nothing_on_is_dais_blank_testimony(blank_testimony):
    context, record = blank_testimony
    assert record["outcome"] == "genuinely-empty"
    assert record["payload"]["payload"] == "" and record["payload"]["presented"] == {}
    assert page_testimonia.is_detector_blank_testimony(context, record)
    validate_page_testimonium_record(context, record)


def _text(record):
    record["payload"]["payload"] = "x"


def _unbound(record):
    record["inputs"] = []


def _other_chair(record):
    record["payload"]["chair"] = "attestator_3"


def _not_blank_health(record):
    record["payload"]["content_health"] = {**record["payload"]["content_health"], "blank": False}


def _other_reason(record):
    record["payload"]["reason"] = "the detector was not run on this page"


FORGED = (
    (_text, "text is not empty"),
    (_unbound, "does not bind exactly its record detector's census"),
    (_other_chair, "attempted page Testimonium has no image presentation"),
    (_not_blank_health, "does not state the health and reason of blank testimony"),
    (_other_reason, "does not state the health and reason of blank testimony"),
)


@pytest.mark.parametrize(
    ("change", "refusal"), FORGED, ids=["text", "unbound", "chair", "health", "reason"]
)
def test_blank_testimony_that_is_not_the_detectors_census_is_refused(
    blank_testimony, change, refusal
):
    context, record = blank_testimony
    forged = copy.deepcopy(record)
    change(forged)
    with pytest.raises(SchemaRefusal, match=refusal):
        validate_page_testimonium_record(context, forged)


def test_blank_testimony_needs_a_census_of_no_record_below_a_stated_cap(
    blank_testimony, monkeypatch
):
    # A detector that states no cap, or a census naming records, gives no census.
    context, record = blank_testimony
    monkeypatch.setattr(page_testimonia, "empty_detector_page", lambda *_args: None)
    with pytest.raises(SchemaRefusal, match="not one of no record below a stated cap"):
        validate_page_testimonium_record(context, record)


# --- one validation per pass, shared work across pages --------------------------------


@pytest.fixture(scope="module")
def page_read(tmp_path_factory):
    """`happy`, read page by page: every page Testimonium presents adapter crops."""
    return build_page_tree(tmp_path_factory.mktemp("happy"), "happy")


@pytest.fixture
def cold_caches(monkeypatch):
    monkeypatch.setattr(page_testimonia, "_CURRENT_PAGE_TESTIMONIA", {})
    monkeypatch.setattr(native_witness, "_DERIVED_SHA256", {})
    monkeypatch.setattr(imaging, "_PAGE_SIZES", OrderedDict())
    monkeypatch.setattr(imaging, "_DECODED_PAGES", OrderedDict())


def _counting_validation(monkeypatch) -> list[str]:
    validated: list[str] = []
    real = page_testimonia.validate_page_testimonium_record

    def counting(context, record):
        validated.append(record["artifact_id"])
        real(context, record)

    monkeypatch.setattr(page_testimonia, "validate_page_testimonium_record", counting)
    return validated


def test_a_pass_validates_the_page_witnesses_once_and_hands_out_copies(
    page_read, cold_caches, monkeypatch
):
    root, options = page_read
    validated = _counting_validation(monkeypatch)

    first = current_page_testimonia(page_context(root, "r", "happy", options))
    assert validated, "nothing was validated"
    expected = copy.deepcopy(first)
    for records in first.values():
        records.clear()
    second = current_page_testimonia(page_context(root, "r", "happy", options))

    assert second == expected, "a caller's change reached the next caller"
    assert len(validated) == sum(len(records) for records in expected.values())


def test_the_attestatores_are_validated_afresh_on_every_ask(page_read, cold_caches, monkeypatch):
    root, options = page_read
    validated = _counting_validation(monkeypatch)
    context = page_context(root, "r", "happy", options, stage=ATTESTATORES)

    current_page_testimonia(context)
    once = len(validated)
    current_page_testimonia(context)

    assert len(validated) == 2 * once


def test_worker_processes_warm_exactly_what_validation_derives_itself(
    page_read, cold_caches, monkeypatch
):
    root, options = page_read
    serial = current_page_testimonia(page_context(root, "r", "happy", options))
    derived_here = dict(native_witness._DERIVED_SHA256)
    assert derived_here, "the fixture presents no adapter crop"

    monkeypatch.setattr(page_testimonia, "_CURRENT_PAGE_TESTIMONIA", {})
    monkeypatch.setattr(native_witness, "_DERIVED_SHA256", {})
    monkeypatch.setattr(imaging, "_PAGE_SIZES", OrderedDict())
    monkeypatch.setattr(page_testimonia, "POOL_MIN_PAGE_BYTES", 0)
    monkeypatch.setattr(page_testimonia, "pool_workers", lambda tasks, bytes_per_task: 2)

    def no_derivation_here(*_args):
        raise AssertionError("the parent re-derived a crop the workers had derived")

    monkeypatch.setattr(native_witness, "_derive_adapter_crop", no_derivation_here)
    pooled = current_page_testimonia(page_context(root, "r", "happy", options))

    assert pooled == serial
    assert native_witness._DERIVED_SHA256 == derived_here


def test_a_held_derivation_is_the_derivation_and_a_refusal_is_never_held(cold_caches):
    page = encode_grayscale_png_deterministic(
        12, 10, [bytearray((row * 12 + column) % 256 for column in range(12)) for row in range(10)]
    )
    transform = {"operation": "crop", "bounds": {"x": 1, "y": 2, "w": 6, "h": 5}}
    expected = digest_bytes(native_witness._derive_adapter_crop(page, transform))

    assert native_witness.derived_presentation_sha256(page, transform) == expected
    assert native_witness.derived_presentation_sha256(page, dict(transform)) == expected
    assert len(native_witness._DERIVED_SHA256) == 1

    outside = {**transform, "bounds": {"x": 8, "y": 2, "w": 6, "h": 5}}
    for _ in range(2):
        with pytest.raises(ValueError, match="fall outside"):
            native_witness.derived_presentation_sha256(page, outside)
    assert len(native_witness._DERIVED_SHA256) == 1


def test_a_broken_worker_pool_leaves_validation_to_derive_every_page_itself(
    page_read, cold_caches, monkeypatch
):
    root, options = page_read
    serial = current_page_testimonia(page_context(root, "r", "happy", options))

    monkeypatch.setattr(page_testimonia, "_CURRENT_PAGE_TESTIMONIA", {})
    monkeypatch.setattr(native_witness, "_DERIVED_SHA256", {})
    monkeypatch.setattr(imaging, "_PAGE_SIZES", OrderedDict())
    monkeypatch.setattr(page_testimonia, "POOL_MIN_PAGE_BYTES", 0)
    monkeypatch.setattr(page_testimonia, "pool_workers", lambda tasks, bytes_per_task: 2)

    class BrokenPool:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def map(self, *_args):
            raise BrokenProcessPool("a worker was killed")

    monkeypatch.setattr(page_testimonia, "ProcessPoolExecutor", BrokenPool)
    recovered = current_page_testimonia(page_context(root, "r", "happy", options))

    assert recovered == serial
    assert native_witness._DERIVED_SHA256, "validation derived no crop itself"
