"""One sealed page's ink map measurement, and every page's, shared across CPUs.

The Ink map measures every sealed page of a shard, and each page is a full
decode and a pure-Python pass over its rows. Pages are independent, so they are
measured in worker processes when there are enough bytes to repay starting
them; each worker reads and checks its page's bytes itself. Results come back
in page order and a refusal is raised when its page is reached, so the stage
publishes exactly the pages it would have published measuring one at a time.
"""

from __future__ import annotations

import multiprocessing
from collections.abc import Callable, Iterator
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, Final

from common.background import BackgroundInferenceRefusal, resolve_background_policy
from common.contracts.errors import FatalAccounting
from common.cpus import pool_workers
from common.exemplar_boundary import sealed_page_bytes
from common.imaging import UnsettledReadingPolicy, grayscale_rows
from common.residual_ink import ink_map_page, resolve_coverage_audit_policy
from common.runtree.store import RunTree

MEASURED: Final = "measured"
NOT_MEASURABLE: Final = "not-measurable"
_REFUSED: Final = "refused"

# Page bytes below which measuring every page in this process costs less than
# starting worker processes to share them.
POOL_MIN_PAGE_BYTES: Final = 4 << 20
# What one worker holds while it measures a page: the page, its rows of grey
# and the measure's own working copies.
BYTES_PER_PAGE_WORKER: Final = 150 << 20


def measured_page_bytes(tree, ordinal: int, page: dict) -> bytes:
    """The page pixels the Ink map measures, digested as the bytes it measures."""
    return sealed_page_bytes(
        tree, page, what=f"the ink map (page {ordinal})", refusal=FatalAccounting
    )


def measure_page_bytes(
    ordinal: int, image_bytes: bytes, background_config: dict, coverage_config: dict
) -> tuple[str, Any]:
    """`(MEASURED, the ink map of the page)`, or `(NOT_MEASURABLE, why)` when its paper
    cannot be inferred. Bytes with no settled grey reading, or that do not decode,
    are refused by name."""
    try:
        # The bytes match the digest the Exemplar sealed; that does not prove
        # the decoder can read them. Earlier pages may already be published, so
        # either refusal says the map is incomplete and names its own cause.
        width, height, rows = grayscale_rows(image_bytes)
    except UnsettledReadingPolicy as error:
        raise FatalAccounting(
            f"the ink map cannot measure sealed Exemplar page {ordinal}: the pipeline has "
            f"no settled policy for reading its pixels as grey ({error}); the bytes are "
            "intact, no boundary was sealed, and the records already published for "
            "earlier pages of this run are an incomplete map"
        ) from error
    except ValueError as error:
        raise FatalAccounting(
            f"the ink map cannot measure sealed Exemplar page {ordinal}: its own "
            f"digest-verified pixels do not decode ({error}); no boundary was "
            "sealed, and the records already published for earlier pages of this "
            "run are an incomplete map"
        ) from error
    policy = resolve_background_policy(background_config, width, height)
    audit_policy = resolve_coverage_audit_policy(coverage_config, width, height)
    try:
        return MEASURED, ink_map_page(
            width, height, rows, background_policy=policy, coverage_policy=audit_policy
        )
    except BackgroundInferenceRefusal as error:
        return NOT_MEASURABLE, str(error)


def _measure_in_worker(
    root: str,
    run_id: str,
    ordinal: int,
    page: dict,
    background_config: dict,
    coverage_config: dict,
) -> tuple[str, Any]:
    try:
        image_bytes = measured_page_bytes(RunTree(Path(root), run_id), ordinal, page)
        return measure_page_bytes(ordinal, image_bytes, background_config, coverage_config)
    except FatalAccounting as error:
        return _REFUSED, str(error)


def measure_pages(
    tree,
    pages: list[tuple[int, dict, str]],
    background_config: dict,
    coverage_config: dict,
    *,
    read_page: Callable[[Any, int, dict], bytes] = measured_page_bytes,
) -> Iterator[tuple[int, dict, str, tuple[str, Any]]]:
    """Each `(ordinal, page, page_path)` of `pages` with its measurement, in their order.

    A page's refusal is raised when its turn comes, so every earlier page has
    been handed out and no later one is. `read_page` reads a page measured in
    this process; a worker reads its page through its own `RunTree`.
    """
    workers = pool_workers(len(pages), bytes_per_task=BYTES_PER_PAGE_WORKER)
    if workers < 2 or _page_bytes(tree, pages) < POOL_MIN_PAGE_BYTES:
        for ordinal, page, page_path in pages:
            image_bytes = read_page(tree, ordinal, page)
            yield (
                ordinal,
                page,
                page_path,
                measure_page_bytes(ordinal, image_bytes, background_config, coverage_config),
            )
        return
    root, run_id = str(Path(tree.root).parent), tree.run_id
    pool = ProcessPoolExecutor(workers, mp_context=multiprocessing.get_context("spawn"))
    try:
        results = pool.map(
            _measure_in_worker,
            [root] * len(pages),
            [run_id] * len(pages),
            [ordinal for ordinal, _, _ in pages],
            [page for _, page, _ in pages],
            [background_config] * len(pages),
            [coverage_config] * len(pages),
        )
        for (ordinal, page, page_path), (kind, value) in zip(pages, results, strict=True):
            if kind == _REFUSED:
                raise FatalAccounting(value)
            yield ordinal, page, page_path, (kind, value)
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


def _page_bytes(tree, pages: list[tuple[int, dict, str]]) -> int:
    """The pages' bytes on disk, or 0 for a tree with no directory of its own."""
    if getattr(tree, "root", None) is None or getattr(tree, "run_id", None) is None:
        return 0
    total = 0
    for _, page, _ in pages:
        try:
            total += tree.resolve(page["payload"]["image_path"]).stat().st_size
        except (OSError, KeyError, TypeError, ValueError):
            continue  # measured here, where its refusal is named
    return total
