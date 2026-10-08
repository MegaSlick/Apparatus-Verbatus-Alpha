"""Pages measured in worker processes give what measuring them here gives, in page order."""

import hashlib

import pytest

from common import ink_map_measure
from common.background import DEFAULT_INK_MAP_CONFIG_PATH, load_background_config
from common.contracts.errors import FatalAccounting
from common.imaging import encode_grayscale_png
from common.residual_ink import load_coverage_audit_config
from common.runtree.store import RunTree


def _page_with_ink(seed: int) -> bytes:
    rows = [bytearray([230] * 120) for _ in range(90)]
    for y in range(20 + seed, 60):
        rows[y][30 + seed : 80] = bytes([60] * (50 - seed))
    rows[2][5:25] = bytes([90] * 20)
    return encode_grayscale_png(120, 90, rows)


def _sealed_tree(tmp_path, images: list[bytes]):
    run = tmp_path / "r"
    pages = []
    for ordinal, data in enumerate(images, start=1):
        digest = hashlib.sha256(data).hexdigest()
        path = f"1_exemplar/blobs/sha256/{digest}"
        (run / path).parent.mkdir(parents=True, exist_ok=True)
        (run / path).write_bytes(data)
        page = {
            "subject_id": f"page-{ordinal}",
            "payload": {"ordinal": ordinal, "image_path": path, "source_sha256": digest},
        }
        pages.append((ordinal, page, f"1_exemplar/artifacts/page/{ordinal}.json"))
    return RunTree(tmp_path, "r"), pages


def _configs():
    return (
        load_background_config(DEFAULT_INK_MAP_CONFIG_PATH),
        load_coverage_audit_config(DEFAULT_INK_MAP_CONFIG_PATH),
    )


@pytest.fixture
def pooled(monkeypatch):
    monkeypatch.setattr(ink_map_measure, "POOL_MIN_PAGE_BYTES", 0)
    monkeypatch.setattr(ink_map_measure, "pool_workers", lambda tasks, bytes_per_task: 2)


def test_worker_processes_measure_what_this_process_measures_in_page_order(tmp_path, monkeypatch):
    tree, pages = _sealed_tree(tmp_path, [_page_with_ink(seed) for seed in range(4)])
    here = list(ink_map_measure.measure_pages(tree, pages, *_configs()))

    monkeypatch.setattr(ink_map_measure, "POOL_MIN_PAGE_BYTES", 0)
    monkeypatch.setattr(ink_map_measure, "pool_workers", lambda tasks, bytes_per_task: 2)
    monkeypatch.setattr(
        ink_map_measure,
        "measure_page_bytes",
        lambda *_args: pytest.fail("a pooled page was measured in this process"),
    )
    workers = list(ink_map_measure.measure_pages(tree, pages, *_configs()))

    assert [row[0] for row in workers] == [1, 2, 3, 4]
    assert workers == here
    assert all(kind == ink_map_measure.MEASURED for *_, (kind, _) in here)


def test_a_worker_refusal_stops_the_pages_at_its_own(tmp_path, pooled):
    tree, pages = _sealed_tree(tmp_path, [_page_with_ink(0), b"not an image", _page_with_ink(1)])
    handed_out = []

    with pytest.raises(FatalAccounting, match="cannot measure sealed Exemplar page 2"):
        for ordinal, *_ in ink_map_measure.measure_pages(tree, pages, *_configs()):
            handed_out.append(ordinal)

    assert handed_out == [1]


def test_a_worker_checks_the_bytes_it_reads_against_the_sealed_digest(tmp_path, pooled):
    tree, pages = _sealed_tree(tmp_path, [_page_with_ink(0), _page_with_ink(1)])
    (tree.root / pages[1][1]["payload"]["image_path"]).write_bytes(_page_with_ink(2))

    with pytest.raises(FatalAccounting, match="page 2"):
        list(ink_map_measure.measure_pages(tree, pages, *_configs()))
