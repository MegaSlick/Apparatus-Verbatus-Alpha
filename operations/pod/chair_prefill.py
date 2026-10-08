"""Fill the selected chairs' caches from the model store while the environment builds.

UV_ENVIRONMENT spends minutes downloading wheels while the disks that hold the
model store and the chair cache sit idle. `ChairCachePrefill` copies each
selected Hugging Face chair's pinned snapshot from the store into the chair cache
on a background thread meanwhile, through the same `ChairRegistry.ensure` that
PREFLIGHT would otherwise run: every copied byte is hashed against the pinned
manifest and only a verified copy is promoted. MODEL_STORE and CHAIR_CACHE wait
for it before they record completion, and PREFLIGHT adopts the registry's
verifications so it does not read those bytes again.

A chair is left for PREFLIGHT to fill, exactly as it would be without this, when
its store artifact is not present yet, when the container disk has no room for
it beside what the environment build will use, or when its fill finds no room
without evicting another cache. Any other refusal is raised by `wait`, so it
surfaces in the step that waits.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from common.chairs.errors import ChairRefusal, DiskSpaceRefusal
from common.chairs.manifests import CopyPool
from common.chairs.models import ChairIdentity
from common.chairs.registry import DIGEST_CACHE_DIRECTORY, ChairRegistry

from .bootstrap import BootstrapStep, BootstrapStepFailure, _filesystem_key, _free_bytes

# How long a waiting step gives the fill before calling it stuck: a tenth of the
# 13.6 GB/min one copy pass was measured at (review 01), so ten times the time
# the bytes should take, and never less than a quarter of an hour. A network
# volume read while the uv sync shares the disk runs far below the measured
# rate, and a fill called stuck turns MODEL_STORE red on a billing pod. Present
# caches count too: they are read once to be verified.
PREFILL_BYTES_PER_SECOND = 13.6e9 / 60 / 10
PREFILL_MINIMUM_SECONDS = 15 * 60


@dataclass(frozen=True, slots=True)
class PrefillChairs:
    """What a prefill may fill: the registry, the chairs in fill order, and those left out."""

    registry: ChairRegistry
    chairs: tuple[ChairIdentity, ...]
    deferred: tuple[dict[str, str], ...] = ()
    # The copy workers the registry's fetcher shares across the concurrent fills;
    # closed when the prefill ends.
    pool: CopyPool | None = None


@dataclass(slots=True)
class _Outcome:
    filled: list[dict[str, object]] = field(default_factory=list)
    deferred: list[dict[str, str]] = field(default_factory=list)
    failure: BaseException | None = None
    seconds: float | None = None
    copy_pool: dict[str, object] | None = None


class ChairCachePrefill:
    """One background fill of the selected chairs' caches, started at most once."""

    def __init__(
        self,
        prepare: Callable[[], PrefillChairs | None],
        *,
        free_bytes: Callable[[Path], int] = _free_bytes,
        filesystem_key: Callable[[Path], int] = _filesystem_key,
        bytes_per_second: float = PREFILL_BYTES_PER_SECOND,
        minimum_seconds: float = PREFILL_MINIMUM_SECONDS,
    ) -> None:
        # Called when the fill starts, after the checkout, so it reads the
        # pinned roster rather than whatever was on disk at container start.
        self._prepare = prepare
        self._free_bytes = free_bytes
        self._filesystem_key = filesystem_key
        self._thread: threading.Thread | None = None
        self._registry: ChairRegistry | None = None
        self._outcome = _Outcome()
        self._started_at_step: str | None = None
        self._bytes_per_second = bytes_per_second
        self._minimum_seconds = minimum_seconds
        # Bytes the chosen chairs copy or verify, and when the fill must be done by.
        self._planned_bytes = 0
        self._deadline: float | None = None

    @property
    def registry(self) -> ChairRegistry | None:
        """The registry that verified the filled chairs, once the fill has finished."""

        if self._thread is None or self._thread.is_alive():
            return None
        return self._registry

    def start(self, step: str, reserved: Mapping[int, int] | None = None) -> None:
        """Start the fill on a background thread, unless it has already started.

        `reserved` is, per filesystem (`st_dev`), the bytes other work starting now
        will still write there; a chair is filled only if it fits beside them.
        """

        if self._thread is not None:
            return
        self._started_at_step = step
        try:
            plan = self._prepare()
        except Exception as error:  # the fill is a head start; PREFLIGHT fills as before
            self._outcome.deferred.append(
                {"chair": "*", "reason": f"the prefill could not be planned: {error}"}
            )
            plan = None
        if plan is None:
            self._thread = threading.Thread(target=lambda: None, daemon=True)
            self._thread.start()
            return
        self._registry = plan.registry
        self._outcome.deferred.extend(plan.deferred)
        try:
            chairs = self._fitting(plan, dict(reserved or {}))
        except Exception as error:  # as above: nothing is filled early, nothing fails
            self._outcome.deferred.append(
                {"chair": "*", "reason": f"the cache disk could not be measured: {error}"}
            )
            chairs = []
        self._deadline = time.monotonic() + max(
            self._minimum_seconds, self._planned_bytes / self._bytes_per_second
        )
        self._thread = threading.Thread(
            target=self._fill,
            args=(plan.registry, chairs, plan.pool),
            name="chair-cache-prefill",
            daemon=True,
        )
        self._thread.start()

    def wait(self, step: BootstrapStep = BootstrapStep.MODEL_STORE) -> dict[str, object] | None:
        """Block until the fill ends; its record, or raise the refusal that stopped it.

        None when no fill was started in this process, as after a resume past it.
        A fill still running at its deadline is a named failure of `step`; the
        daemon copy threads are left behind rather than waited for.
        """

        if self._thread is None:
            return None
        remaining = None if self._deadline is None else self._deadline - time.monotonic()
        self._thread.join(timeout=None if remaining is None else max(0.0, remaining))
        if self._thread.is_alive():
            raise BootstrapStepFailure(
                step,
                f"the chair-cache prefill started at {self._started_at_step} is still "
                f"copying {self._planned_bytes} bytes after its deadline "
                f"({self._planned_bytes / self._bytes_per_second:.0f} s at a tenth of "
                "13.6 GB/min, at least "
                f"{self._minimum_seconds:.0f} s); a copy or a store read has stalled",
                "Check the model volume and the container disk (a hung network mount, a "
                "full disk), then resume this journal; the next attempt clears the "
                "abandoned candidate directories under the chair cache.",
            )
        if self._outcome.failure is not None:
            raise self._outcome.failure
        return {
            "started_at_step": self._started_at_step,
            "filled": list(self._outcome.filled),
            "deferred": list(self._outcome.deferred),
            "seconds": self._outcome.seconds,
            "copy_pool": self._outcome.copy_pool,
        }

    def _fitting(self, plan: PrefillChairs, reserved: dict[int, int]) -> list[ChairIdentity]:
        """The chairs whose copies fit beside `reserved`, in order; the rest deferred."""

        cache_root = plan.registry.cache_root
        if cache_root is None:
            self._outcome.deferred.extend(
                {"chair": chair.role, "reason": "no chair cache root is configured"}
                for chair in plan.chairs
            )
            return []
        digests_root = cache_root / DIGEST_CACHE_DIRECTORY
        try:
            key = self._filesystem_key(digests_root)
            free = self._free_bytes(digests_root)
        except OSError as error:
            self._outcome.deferred.extend(
                {"chair": chair.role, "reason": f"cache disk space unreadable: {error}"}
                for chair in plan.chairs
            )
            return []
        committed = reserved.get(key, 0)
        planned: set[str] = set()
        chosen: list[ChairIdentity] = []
        for chair in plan.chairs:
            digest = chair.digest_manifest
            try:
                size = sum(row.size for row in plan.registry.manifest(chair).rows)
            except ChairRefusal as refusal:
                self._outcome.deferred.append(
                    {"chair": chair.role, "reason": f"pinned manifest unreadable: {refusal}"}
                )
                continue
            need = 0 if digest in planned or (digests_root / digest).exists() else size
            if committed + need > free:
                self._outcome.deferred.append(
                    {
                        "chair": chair.role,
                        "reason": "no room beside the environment build; "
                        f"{need} bytes needed, {free - committed} free after it",
                    }
                )
                continue
            committed += need
            if digest not in planned:
                self._planned_bytes += size
            planned.add(digest)
            chosen.append(chair)
        return chosen

    def _fill(
        self, registry: ChairRegistry, chairs: list[ChairIdentity], pool: CopyPool | None
    ) -> None:
        """Fill each digest on its own thread, all copying through one shared pool.

        Chairs that share a digest fill in turn on one thread (the second finds
        the first's verified copy). The record and any refusal are taken in
        chair order afterwards, so neither depends on which fill finished first.
        """

        began = time.monotonic()
        by_digest: dict[str, list[ChairIdentity]] = {}
        for chair in chairs:
            by_digest.setdefault(chair.digest_manifest, []).append(chair)
        results: dict[str, object] = {}

        def fill(group: list[ChairIdentity]) -> None:
            for chair in group:
                try:
                    results[chair.role] = registry.ensure(chair, evict=False)
                except DiskSpaceRefusal as refusal:
                    results[chair.role] = refusal
                except BaseException as error:  # surfaced by `wait`, in the step that waits
                    results[chair.role] = error
                    return

        try:
            threads = [
                threading.Thread(target=fill, args=(group,), name="chair-cache-fill", daemon=True)
                for group in by_digest.values()
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        finally:
            if pool is not None:
                pool.close()
        for chair in chairs:
            result = results.get(chair.role)
            if result is None:
                continue
            if isinstance(result, DiskSpaceRefusal):
                self._outcome.deferred.append({"chair": chair.role, "reason": str(result)})
            elif isinstance(result, BaseException):
                if self._outcome.failure is None:
                    self._outcome.failure = result
            else:
                filled: dict[str, object] = {
                    "chair": chair.role,
                    "manifest_digest": result.manifest_digest,
                    "root": str(result.root),
                }
                if result.verification is not None:
                    filled["verification"] = dict(result.verification)
                self._outcome.filled.append(filled)
        if pool is not None:
            self._outcome.copy_pool = pool.workers.to_record()
        self._outcome.seconds = round(time.monotonic() - began, 1)
