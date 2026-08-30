"""Structural effect seams stay concrete in production and fixture implementations.

The project does not gate on a static type checker.  A ``Protocol`` annotation
therefore documents a seam but does not stop an implementation from drifting,
and directly inheriting a Protocol can make the failure quieter by inheriting
its stub.  This matrix makes method additions executable across the reusable
implementations that production and the operator fixture actually construct.
"""

from __future__ import annotations

import inspect
from typing import Protocol

import pytest

from common.chairs.model_store import MaterializationFetcher
from common.chairs.registry import (
    HuggingFaceFetcher,
    HuggingFaceMaterializationFetcher,
    SnapshotFetcher,
)
from operations.pod.arming import ControllerArmer, FailClosedControllerArmer
from operations.pod.bootstrap import BootstrapActions, SubprocessBootstrapActions
from operations.pod.fake_provider import FakeProvider
from operations.pod.preflight import ChairCacheVerifier, SmokeReader, SystemGpuProbe
from operations.pod.provider import PodProvider
from operations.pod.provider_runpod import (
    HttpTransport as RunPodHttpTransport,
)
from operations.pod.provider_runpod import (
    RunPodProvider,
    UrllibRunPodTransport,
)
from operations.pod.transfer import TransferTarget
from operations.serving.assembly import ProfileProbe
from operations.serving.http import HttpTransport as ServingHttpTransport
from operations.serving.http import UrllibHttpTransport
from operations.serving.manager import (
    InstalledPackages,
    PackageInspector,
    ReceiptPublisher,
    StageContextReceiptPublisher,
)
from operations.serving.preflight import ServingSmokeReader
from operations.serving.process import (
    PopenServerProcess,
    ProcessLauncher,
    ServerProcess,
    SubprocessLauncher,
)
from operations.serving.residency import (
    FileResidencyLease,
    ResidencyHandle,
    ResidencyLease,
    _FileResidencyHandle,
)

from .fakes import LocalFixtureObjectStore
from .surface import (
    FixtureBootstrapActions,
    FixtureCache,
    FixtureControllerArmer,
    FixtureSmokeReader,
)
from .volume_s3 import S3VolumeTarget

SEAMS = (
    (SnapshotFetcher, HuggingFaceFetcher),
    (MaterializationFetcher, HuggingFaceMaterializationFetcher),
    (BootstrapActions, SubprocessBootstrapActions),
    (BootstrapActions, FixtureBootstrapActions),
    (ControllerArmer, FailClosedControllerArmer),
    (ControllerArmer, FixtureControllerArmer),
    (PodProvider, FakeProvider),
    (PodProvider, RunPodProvider),
    (TransferTarget, LocalFixtureObjectStore),
    (TransferTarget, S3VolumeTarget),
    (ChairCacheVerifier, FixtureCache),
    (SmokeReader, FixtureSmokeReader),
    (SmokeReader, ServingSmokeReader),
    (ProfileProbe, SystemGpuProbe),
    (RunPodHttpTransport, UrllibRunPodTransport),
    (ServingHttpTransport, UrllibHttpTransport),
    (ResidencyHandle, _FileResidencyHandle),
    (ResidencyLease, FileResidencyLease),
    (ServerProcess, PopenServerProcess),
    (ProcessLauncher, SubprocessLauncher),
    (ReceiptPublisher, StageContextReceiptPublisher),
    (PackageInspector, InstalledPackages),
)


def _missing_concrete_methods(protocol: type, implementation: type) -> list[str]:
    missing: list[str] = []
    # The Protocol's whole method set, not just the class body: `vars(protocol)`
    # misses anything a base Protocol declares, so the first seam that inherits
    # would let an implementation drop an inherited method and still pass here.
    # Every pair in SEAMS is flat today; this closes the gap before one is not.
    declared: dict[str, object] = {}
    for base in reversed(protocol.__mro__):
        if not getattr(base, "_is_protocol", False):
            continue
        declared.update(vars(base))
    for name, member in declared.items():
        if name.startswith("_"):
            continue
        # `inspect.isfunction` alone sees only plain `def`s, so a protocol that
        # declared a member as a `staticmethod`, `classmethod` or `property`
        # would be skipped entirely and an implementation could omit it while
        # this matrix stayed green. No seam declares one today; the walker is
        # widened before one does rather than after.
        descriptor = isinstance(member, (staticmethod, classmethod, property))
        if not descriptor and not inspect.isfunction(member):
            continue
        owner = next(
            (base for base in implementation.__mro__ if name in vars(base)),
            None,
        )
        if owner is None or getattr(owner, "_is_protocol", False):
            # A property may legitimately be answered by a slot or an annotated
            # instance attribute rather than by a class-body definition, and
            # calling that a missing member would be a false refusal.
            if isinstance(member, property) and any(
                name in getattr(base, "__slots__", ())
                or name in getattr(base, "__annotations__", {})
                for base in implementation.__mro__
            ):
                continue
            missing.append(name)
            continue
        if isinstance(member, property):
            # Reading a property off the class returns the descriptor, so the
            # `callable` check below cannot be applied here -- but presence
            # alone is not the obligation either. A protocol property is an
            # attribute a caller reads; answering it with a plain `def` gives
            # every caller a bound method where a value was promised, and
            # accepting that would let exactly the drift this matrix exists to
            # catch through under the name of a defined member.
            concrete = vars(owner)[name]
            if inspect.isfunction(concrete) or isinstance(concrete, (staticmethod, classmethod)):
                missing.append(name)
            continue
        if not callable(getattr(implementation, name, None)):
            missing.append(name)
    return sorted(missing)


class _DescriptorSeam(Protocol):
    """Not a real seam: the shapes `_missing_concrete_methods` must judge.

    No pair in `SEAMS` declares a descriptor member today, so the walker's
    handling of one is exercised nowhere else and could be wrong -- or could be
    broken later -- without a single test noticing.
    """

    @property
    def label(self) -> str:
        """An attribute a caller reads, not a method it invokes."""

    @staticmethod
    def build() -> None:
        """A staticmethod the implementation must define."""

    @classmethod
    def make(cls) -> None:
        """A classmethod the implementation must define."""

    def read(self) -> None:
        """An ordinary method, for the contrast."""


class _AnswersEveryShape:
    label = "a value"

    @staticmethod
    def build() -> None: ...

    @classmethod
    def make(cls) -> None: ...

    def read(self) -> None: ...


class _AnswersThePropertyWithAProperty(_AnswersEveryShape):
    @property
    def label(self) -> str:
        return "a value"


class _AnswersThePropertyWithASlot:
    __slots__ = ("label",)

    @staticmethod
    def build() -> None: ...

    @classmethod
    def make(cls) -> None: ...

    def read(self) -> None: ...


class _AnswersThePropertyWithAMethod(_AnswersEveryShape):
    def label(self) -> str:  # type: ignore[override]
        return "a bound method where a value was promised"


class _DefinesOnlyTheOrdinaryMethod:
    def read(self) -> None: ...


@pytest.mark.parametrize(
    ("implementation", "expected"),
    [
        (_AnswersEveryShape, []),
        (_AnswersThePropertyWithAProperty, []),
        (_AnswersThePropertyWithASlot, []),
        # The one this walker used to accept: a defined member of the wrong
        # shape, which reads as present to any check that only asks whether the
        # name resolves.
        (_AnswersThePropertyWithAMethod, ["label"]),
        (_DefinesOnlyTheOrdinaryMethod, ["build", "label", "make"]),
    ],
    ids=lambda value: getattr(value, "__name__", str(value)),
)
def test_the_seam_walker_judges_descriptor_members_by_shape(
    implementation: type, expected: list[str]
) -> None:
    assert _missing_concrete_methods(_DescriptorSeam, implementation) == expected


@pytest.mark.parametrize(
    ("protocol", "implementation"),
    SEAMS,
    ids=lambda value: value.__name__,
)
def test_reusable_structural_protocol_implementations_define_every_method(
    protocol: type, implementation: type
) -> None:
    missing = _missing_concrete_methods(protocol, implementation)
    assert not missing, (
        f"{implementation.__module__}.{implementation.__name__} inherits or omits "
        f"{protocol.__name__} methods {missing}"
    )
