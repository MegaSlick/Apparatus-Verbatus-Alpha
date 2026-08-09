"""Deterministic sampling and mechanical held-out use checks.

This module holds opaque IDs and digests only.  It never reads images or
transcriptions, so an attempt to repurpose evaluation material is refused before
such bytes reach a tuning/calibration caller that uses this boundary.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Callable, Iterable, Mapping

from .encoding import canonical_json_bytes, is_sha256, sha256_bytes
from .errors import HoldoutRefusal
from .models import MaterialClass, ReferenceStatus


class MaterialUse(StrEnum):
    """Uses that may never share Spec 05 evaluation material."""

    EVALUATION = "evaluation"
    PROMPT_TUNING = "prompt_tuning"
    PADDING_CALIBRATION = "padding_calibration"
    ADJUSTMENT = "adjustment"
    BENCH = "bench"
    DRESS_REHEARSAL = "dress_rehearsal"


PROHIBITED_EVALUATION_OVERLAP: frozenset[MaterialUse] = frozenset(
    {
        MaterialUse.PROMPT_TUNING,
        MaterialUse.PADDING_CALIBRATION,
        MaterialUse.ADJUSTMENT,
        MaterialUse.BENCH,
        MaterialUse.DRESS_REHEARSAL,
    }
)

# This literal is fixed before the sampling frame exists.  It is not a secret and
# it is not generated from candidate or reference content.
SPEC05_SELECTION_SEED = "verbatus/spec05/selection-v1"


@dataclass(frozen=True, slots=True)
class FrameMember:
    """An opaque sampling-frame row; no raw image or text belongs here."""

    opaque_act_id: str
    page_sha256: str
    crop_sha256: str
    stratum: str
    material_class: MaterialClass
    reference_status: ReferenceStatus | None = None
    private_evidence_sha256s: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.opaque_act_id, str) or not self.opaque_act_id:
            raise HoldoutRefusal("sampling-frame opaque_act_id must be non-empty")
        if not is_sha256(self.page_sha256) or not is_sha256(self.crop_sha256):
            raise HoldoutRefusal("sampling-frame page and crop digests must be SHA-256 values")
        if not isinstance(self.stratum, str) or not self.stratum:
            raise HoldoutRefusal("sampling-frame stratum must be non-empty")
        if not isinstance(self.material_class, MaterialClass):
            raise HoldoutRefusal("sampling-frame material_class must be a MaterialClass")
        if self.reference_status is not None and not isinstance(
            self.reference_status, ReferenceStatus
        ):
            raise HoldoutRefusal("sampling-frame reference_status must be a ReferenceStatus")
        if any(not is_sha256(value) for value in self.private_evidence_sha256s):
            raise HoldoutRefusal("sampling-frame private evidence must be SHA-256 values")
        if len(set(self.private_evidence_sha256s)) != len(self.private_evidence_sha256s):
            raise HoldoutRefusal("sampling-frame private evidence digests must be distinct")

    def material_digests(self) -> frozenset[str]:
        """All selected material locks out later use, including private text evidence."""

        return frozenset({self.page_sha256, self.crop_sha256, *self.private_evidence_sha256s})

    def binding(
        self,
    ) -> tuple[str, str, str, MaterialClass, ReferenceStatus, tuple[str, ...]]:
        """The exact material classification and evidence set a run must reproduce."""

        return (
            self.opaque_act_id,
            self.page_sha256,
            self.crop_sha256,
            self.material_class,
            self.reference_status,
            tuple(sorted(self.private_evidence_sha256s)),
        )

    def selection_binding(self) -> tuple[str, str, str, str, MaterialClass]:
        """The pre-reference sampling identity that deterministic selection preserves."""

        return (
            self.opaque_act_id,
            self.page_sha256,
            self.crop_sha256,
            self.stratum,
            self.material_class,
        )

    def record(self) -> dict[str, object]:
        return {
            "opaque_act_id": self.opaque_act_id,
            "page_sha256": self.page_sha256,
            "crop_sha256": self.crop_sha256,
            "stratum": self.stratum,
            "material_class": self.material_class.value,
            "reference_status": self.reference_status.value if self.reference_status else None,
            "private_evidence_sha256s": sorted(self.private_evidence_sha256s),
        }


@dataclass(frozen=True, slots=True)
class SamplingPlan:
    """A sealed choice of seed and stratum quotas made before material is shown."""

    seed: str
    quota_by_stratum: Mapping[str, int]

    def __post_init__(self) -> None:
        if self.seed != SPEC05_SELECTION_SEED:
            raise HoldoutRefusal(
                "sampling seed differs from the predeclared Spec 05 selection seed"
            )
        quotas = dict(self.quota_by_stratum)
        if not quotas:
            raise HoldoutRefusal("sampling plan requires an explicit quota for every stratum")
        for stratum, quota in quotas.items():
            if not isinstance(stratum, str) or not stratum:
                raise HoldoutRefusal("sampling stratum must be non-empty")
            if not isinstance(quota, int) or isinstance(quota, bool) or quota < 1:
                raise HoldoutRefusal("sampling quotas must be positive integers")
        object.__setattr__(self, "quota_by_stratum", MappingProxyType(quotas))

    def record(self) -> dict[str, object]:
        return {
            "algorithm": "sha256(seed UTF-8 || NUL || opaque_act_id UTF-8), ascending within stratum",
            "seed": self.seed,
            "quota_by_stratum": dict(sorted(self.quota_by_stratum.items())),
        }


@dataclass(frozen=True, slots=True)
class EvaluationManifest:
    """A self-verifying sealed draw, including the full opaque sampling frame.

    Retaining the complete frame lets the object recompute its own deterministic
    draw.  A caller cannot provide selected members plus an arbitrary claimed
    frame digest and call that a manifest.
    """

    protocol_sha256: str
    sampling_plan: SamplingPlan
    frame_members: tuple[FrameMember, ...]
    members: tuple[FrameMember, ...]

    def __post_init__(self) -> None:
        if not is_sha256(self.protocol_sha256):
            raise HoldoutRefusal("evaluation manifest requires a protocol SHA-256 value")
        if not self.frame_members:
            raise HoldoutRefusal("evaluation manifest has no complete sampling frame")
        if not self.members:
            raise HoldoutRefusal("evaluation manifest may not be empty")
        frame_ids = [member.opaque_act_id for member in self.frame_members]
        if len(set(frame_ids)) != len(frame_ids):
            raise HoldoutRefusal("sampling frame repeats an opaque act ID")
        selected_ids = [member.opaque_act_id for member in self.members]
        if len(set(selected_ids)) != len(selected_ids):
            raise HoldoutRefusal("evaluation manifest repeats an opaque act ID")
        expected = _selected_members(self.frame_members, self.sampling_plan)
        if tuple(member.selection_binding() for member in self.members) != tuple(
            member.selection_binding() for member in expected
        ):
            raise HoldoutRefusal(
                "evaluation manifest members differ from its sealed deterministic selection"
            )
        if any(not member.private_evidence_sha256s for member in self.members):
            raise HoldoutRefusal(
                "every selected evaluation member must bind its private reference and Testimonium evidence"
            )
        if any(member.reference_status is None for member in self.members):
            raise HoldoutRefusal(
                "every selected evaluation member must bind a checked, blank, or unresolved reference status"
            )

    @property
    def frame_sha256(self) -> str:
        return frame_digest(self.frame_members)

    @property
    def selected_sha256(self) -> str:
        return sha256_bytes(canonical_json_bytes([member.record() for member in self.members]))

    @property
    def manifest_sha256(self) -> str:
        """Digest of the exact private manifest metadata bound to run approval."""

        return sha256_bytes(canonical_json_bytes(self.record()))

    @property
    def material_digests(self) -> frozenset[str]:
        return frozenset().union(*(member.material_digests() for member in self.members))

    def record(self) -> dict[str, object]:
        return {
            "schema": "spec05-evaluation-manifest.v1",
            "protocol_sha256": self.protocol_sha256,
            "frame_sha256": self.frame_sha256,
            "sampling_plan": self.sampling_plan.record(),
            "selected_sha256": self.selected_sha256,
            "frame_member_count": len(self.frame_members),
            "member_count": len(self.members),
            "members": [member.record() for member in self.members],
        }

    def require_scoreable_acts(
        self,
        acts: Iterable[tuple[str, str, str, MaterialClass, ReferenceStatus, tuple[str, ...]]],
        *,
        excluded_opaque_act_ids: Iterable[str],
    ) -> None:
        """Bind the scoreable subset after every selected exclusion is accounted for."""

        observed = tuple(acts)
        excluded = frozenset(excluded_opaque_act_ids)
        selected_ids = {member.opaque_act_id for member in self.members}
        if not excluded <= selected_ids:
            raise HoldoutRefusal("sample accounting excludes an act outside the sealed selection")
        expected = {
            member.binding() for member in self.members if member.opaque_act_id not in excluded
        }
        if len(observed) != len(set(observed)):
            raise HoldoutRefusal("run repeats an act/material-class/evidence binding")
        if set(observed) != expected:
            raise HoldoutRefusal(
                "run acts are not exactly the material, classification, and evidence in the sealed manifest"
            )


@dataclass(frozen=True, slots=True)
class ReferenceExclusion:
    """Private accounting for a selected reference that cannot enter CER/WER."""

    opaque_act_id: str
    status: ReferenceStatus
    reason_evidence_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.opaque_act_id, str) or not self.opaque_act_id:
            raise HoldoutRefusal("reference exclusion needs an opaque act ID")
        if self.status not in (
            ReferenceStatus.NO_READABLE_TEXT,
            ReferenceStatus.UNRESOLVED_GAP,
        ):
            raise HoldoutRefusal("reference exclusions are only blank or unresolved selected acts")
        if not is_sha256(self.reason_evidence_sha256):
            raise HoldoutRefusal("reference exclusion needs a reason-evidence SHA-256")

    def record(self) -> dict[str, str]:
        return {
            "opaque_act_id": self.opaque_act_id,
            "status": self.status.value,
            "reason_evidence_sha256": self.reason_evidence_sha256,
        }


@dataclass(frozen=True, slots=True)
class PrivateSampleAccounting:
    """Complete private accounting of scoreable and excluded selected acts.

    This is evidence, not a selector: it cannot replace an excluded act, alter the
    deterministic draw, or make an excluded act scoreable.
    """

    manifest_sha256: str
    scoreable_opaque_act_ids: tuple[str, ...]
    exclusions: tuple[ReferenceExclusion, ...] = ()

    def __post_init__(self) -> None:
        if not is_sha256(self.manifest_sha256):
            raise HoldoutRefusal("private sample accounting needs a manifest SHA-256")
        if len(set(self.scoreable_opaque_act_ids)) != len(self.scoreable_opaque_act_ids):
            raise HoldoutRefusal("private sample accounting repeats a scoreable act")
        if any(
            not isinstance(opaque_act_id, str) or not opaque_act_id
            for opaque_act_id in self.scoreable_opaque_act_ids
        ):
            raise HoldoutRefusal("private sample accounting has an invalid scoreable act ID")
        exclusion_ids = [item.opaque_act_id for item in self.exclusions]
        if len(set(exclusion_ids)) != len(exclusion_ids):
            raise HoldoutRefusal("private sample accounting repeats an excluded act")
        if set(self.scoreable_opaque_act_ids) & set(exclusion_ids):
            raise HoldoutRefusal("a selected act cannot be both scoreable and excluded")

    @classmethod
    def all_scoreable(cls, manifest: EvaluationManifest) -> "PrivateSampleAccounting":
        """Construct the only implicit accounting: every selected act is scoreable."""

        return cls(
            manifest_sha256=manifest.manifest_sha256,
            scoreable_opaque_act_ids=tuple(member.opaque_act_id for member in manifest.members),
        )

    @property
    def excluded_opaque_act_ids(self) -> frozenset[str]:
        return frozenset(item.opaque_act_id for item in self.exclusions)

    @property
    def digest(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.record()))

    def record(self) -> dict[str, object]:
        return {
            "schema": "spec05-private-sample-accounting.v1",
            "manifest_sha256": self.manifest_sha256,
            "scoreable_opaque_act_ids": list(self.scoreable_opaque_act_ids),
            "exclusions": [item.record() for item in self.exclusions],
        }

    def require_complete_for(self, manifest: EvaluationManifest) -> None:
        """Refuse a selected act that is neither scored nor privately accounted."""

        if self.manifest_sha256 != manifest.manifest_sha256:
            raise HoldoutRefusal("private sample accounting names a different manifest")
        selected_ids = {member.opaque_act_id for member in manifest.members}
        accounted_ids = set(self.scoreable_opaque_act_ids) | set(self.excluded_opaque_act_ids)
        if accounted_ids != selected_ids:
            raise HoldoutRefusal(
                "private sample accounting does not account for every deterministically selected act"
            )
        by_id = {member.opaque_act_id: member for member in manifest.members}
        if any(
            by_id[opaque_act_id].reference_status is not ReferenceStatus.CHECKED
            for opaque_act_id in self.scoreable_opaque_act_ids
        ):
            raise HoldoutRefusal(
                "private sample accounting calls a blank or unresolved selected reference scoreable"
            )
        for exclusion in self.exclusions:
            if by_id[exclusion.opaque_act_id].reference_status is not exclusion.status:
                raise HoldoutRefusal(
                    "private sample accounting exclusion status differs from the sealed selected reference"
                )


def _payload_digest(payload: bytes | str) -> str:
    """Hash protected material at ingress, whichever of the two forms it arrives in.

    An unpaired surrogate is a ``str`` Python will not encode, so it refuses by
    name here rather than raising UnicodeEncodeError out of a guard whose whole
    job is to refuse cleanly.
    """

    if isinstance(payload, bytes):
        return sha256_bytes(payload)
    if not isinstance(payload, str):
        raise HoldoutRefusal("protected material payloads must be bytes or Unicode text")
    try:
        return sha256_bytes(payload.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise HoldoutRefusal("protected material text contains an unpaired surrogate") from error


def frame_digest(members: Iterable[FrameMember]) -> str:
    """Hash a full frame after deterministic ordering, before selection."""

    rows = sorted((member.record() for member in members), key=lambda row: row["opaque_act_id"])
    return sha256_bytes(canonical_json_bytes(rows))


def _draw_key(seed: str, opaque_act_id: str) -> bytes:
    return hashlib.sha256(seed.encode("utf-8") + b"\0" + opaque_act_id.encode("utf-8")).digest()


def select_manifest(
    frame: Iterable[FrameMember],
    *,
    protocol_sha256: str,
    plan: SamplingPlan,
    selected_private_evidence_by_act: Mapping[str, Iterable[str]] | None = None,
    selected_reference_status_by_act: Mapping[str, ReferenceStatus] | None = None,
) -> EvaluationManifest:
    """Select quotas, then bind selected private evidence without changing the draw."""

    members = tuple(frame)
    if not members:
        raise HoldoutRefusal("cannot sample an empty frame")
    opaque_ids = [member.opaque_act_id for member in members]
    if len(set(opaque_ids)) != len(opaque_ids):
        raise HoldoutRefusal("sampling frame repeats an opaque act ID")
    selected = _selected_members(members, plan)
    if selected_private_evidence_by_act is not None:
        evidence = {
            opaque_act_id: tuple(sorted(values))
            for opaque_act_id, values in selected_private_evidence_by_act.items()
        }
        selected_ids = {member.opaque_act_id for member in selected}
        if set(evidence) != selected_ids:
            raise HoldoutRefusal(
                "private evidence bindings must name exactly the deterministic selected acts"
            )
        selected = tuple(
            replace(member, private_evidence_sha256s=evidence[member.opaque_act_id])
            for member in selected
        )
    if selected_reference_status_by_act is not None:
        statuses = dict(selected_reference_status_by_act)
        selected_ids = {member.opaque_act_id for member in selected}
        if set(statuses) != selected_ids or any(
            not isinstance(status, ReferenceStatus) for status in statuses.values()
        ):
            raise HoldoutRefusal(
                "reference-status bindings must name exactly the deterministic selected acts"
            )
        selected = tuple(
            replace(member, reference_status=statuses[member.opaque_act_id]) for member in selected
        )
    return EvaluationManifest(
        protocol_sha256=protocol_sha256,
        sampling_plan=plan,
        frame_members=members,
        members=selected,
    )


def _selected_members(frame: Iterable[FrameMember], plan: SamplingPlan) -> tuple[FrameMember, ...]:
    """Apply the fixed deterministic draw and reject a malformed frame."""

    by_stratum: dict[str, list[FrameMember]] = {}
    for member in frame:
        by_stratum.setdefault(member.stratum, []).append(member)
    if set(by_stratum) != set(plan.quota_by_stratum):
        raise HoldoutRefusal("sampling frame strata do not exactly match the sealed quota plan")
    selected: list[FrameMember] = []
    for stratum, quota in sorted(plan.quota_by_stratum.items()):
        ordered = sorted(
            by_stratum[stratum], key=lambda member: _draw_key(plan.seed, member.opaque_act_id)
        )
        if len(ordered) < quota:
            raise HoldoutRefusal(
                f"sampling frame has {len(ordered)} members for {stratum!r}, below sealed quota {quota}"
            )
        selected.extend(ordered[:quota])
    return tuple(sorted(selected, key=lambda member: member.opaque_act_id))


class HeldOutUseGuard:
    """The single refusal point for evaluation-material reuse in this framework."""

    def __init__(self, manifest: EvaluationManifest) -> None:
        self._manifest = manifest
        self._capability = object()

    def bind_selected_payload(
        self, opaque_act_id: str, payload: bytes | str
    ) -> "BoundEvaluationMaterial":
        """Bind a known selected payload to its act before it may be transformed.

        The factory checks a raw or closed-normalization digest now.  Afterwards the
        lineage, not a newly computed content hash, follows transformations.
        """

        if not isinstance(opaque_act_id, str) or not opaque_act_id:
            raise HoldoutRefusal("held-out payload binding needs an opaque act ID")
        digest = _payload_digest(payload)
        member = next(
            (item for item in self._manifest.members if item.opaque_act_id == opaque_act_id),
            None,
        )
        if member is None or digest not in member.material_digests():
            raise HoldoutRefusal("payload does not belong to the named selected evaluation act")
        return BoundEvaluationMaterial(payload, opaque_act_id, self._capability)

    def material_for_use(
        self, material: "BoundEvaluationMaterial", *, use: MaterialUse
    ) -> bytes | str:
        """Return a lineage-bound payload only for an allowed use."""

        if not isinstance(material, BoundEvaluationMaterial):
            raise HoldoutRefusal("protected material ingress requires a bound evaluation payload")
        if material._capability is not self._capability:
            raise HoldoutRefusal("protected material belongs to a different held-out guard")
        if material.opaque_act_id not in {
            member.opaque_act_id for member in self._manifest.members
        }:
            raise HoldoutRefusal("protected material names no selected evaluation act")
        if not isinstance(use, MaterialUse):
            raise HoldoutRefusal("material use must be one declared MaterialUse value")
        if use in PROHIBITED_EVALUATION_OVERLAP:
            raise HoldoutRefusal(
                f"{use.value} cannot receive a lineage-bound selected evaluation payload"
            )
        return material._payload

    def require_allowed(self, material_digests: Iterable[str], *, use: MaterialUse) -> None:
        """Fail before a forbidden caller can use declared protected digests.

        New callers handling raw content should prefer require_allowed_payloads so the
        digest declaration cannot drift from the bytes at their ingress.
        """

        if not isinstance(use, MaterialUse):
            raise HoldoutRefusal("material use must be one declared MaterialUse value")
        proposed = frozenset(material_digests)
        if any(not is_sha256(value) for value in proposed):
            raise HoldoutRefusal("material-use declaration contains a non-SHA-256 digest")
        if use in PROHIBITED_EVALUATION_OVERLAP:
            # Both spellings refuse. Naming the overlap when there is one is what
            # tells a caller it held evaluation material rather than merely
            # unlineaged bytes.
            overlap = proposed & self._manifest.material_digests
            if overlap:
                raise HoldoutRefusal(
                    f"{use.value} overlaps the held-out evaluation material "
                    f"({len(overlap)} digest(s))"
                )
            raise HoldoutRefusal(
                f"{use.value} rejects unlineaged material digests; a later stage needs its own "
                "separately approved material provenance"
            )

    def require_allowed_payloads(
        self, payloads: Iterable[bytes | str], *, use: MaterialUse
    ) -> None:
        """Hash raw text/bytes at ingress before applying the held-out-use refusal."""

        self.require_allowed([_payload_digest(payload) for payload in payloads], use=use)


class BoundEvaluationMaterial:
    """A selected payload whose act lineage survives an in-framework transformation."""

    __slots__ = ("_payload", "_capability", "opaque_act_id")

    def __init__(self, payload: bytes | str, opaque_act_id: str, capability: object) -> None:
        self._payload = payload
        self.opaque_act_id = opaque_act_id
        self._capability = capability

    def transform(
        self, transformer: Callable[[bytes | str], bytes | str]
    ) -> "BoundEvaluationMaterial":
        """Derive a payload without ever dropping its selected-act lineage."""

        result = transformer(self._payload)
        if not isinstance(result, (bytes, str)):
            raise HoldoutRefusal("a protected-material transformation must return bytes or text")
        return BoundEvaluationMaterial(result, self.opaque_act_id, self._capability)
