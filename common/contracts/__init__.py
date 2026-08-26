"""The one executable authority for `skeleton.v1`.

Each stage's HANDOFF.md describes what that stage owns and links here; none of them
carries a competing copy of the schema, because two copies of a contract is one
contract and one thing that goes stale. The canonical DATA_CONTRACT.md is reserved
until specs 01-03 have stabilized and can be written from observed behaviour rather
than ahead of it (master plan ledger).

`skeleton.v1` is disposable on purpose. It exists to prove wiring and bookkeeping
before any model, GPU, or real page exists, and it proves nothing whatever about
reading ink.
"""

from .approval import (
    APPROVER,
    REAL_INGRESS,
    SYNTHETIC_FIXTURE_INGRESS,
    ApprovalRecordBinding,
    ApprovalRecordReference,
    build_approval_record,
    parse_ingress_record,
    real_ingress_record,
    synthetic_fixture_ingress_record,
    validate_approval_record,
)
from .canonical import (
    SCHEMA_LABEL,
    canonical_bytes,
    canonical_text,
    digest_bytes,
    digest_of,
    self_hash,
    verify_self_hash,
)
from .envelope import (
    build_envelope,
    validate_envelope,
    validate_input_refs,
    verify_input_bytes,
)
from .errors import (
    ApprovalRefusal,
    ContractError,
    FatalAccounting,
    IdentityRefusal,
    IncompatibleReuse,
    SchemaRefusal,
)
from .identities import (
    act_id,
    artifact_id,
    attempt_id,
    is_well_formed,
    page_id,
    region_id,
    validate_run_id,
    verify,
)
from .outcomes import (
    ArmariumCategory,
    OutcomeClass,
    check_algebra_is_total,
    classify,
    require_approval,
    run_aggregate,
    terminal_category,
    witness_coverage,
)
from .serving import SERVING_CONFIG_INPUTS_FIELDS, SERVING_CONFIG_INPUTS_SCHEMA
from .stages import HANDOFFS, STAGES, stage_directory

__all__ = [
    "APPROVER",
    "ArmariumCategory",
    "ApprovalRecordBinding",
    "ApprovalRecordReference",
    "ApprovalRefusal",
    "ContractError",
    "FatalAccounting",
    "HANDOFFS",
    "IdentityRefusal",
    "IncompatibleReuse",
    "OutcomeClass",
    "REAL_INGRESS",
    "SCHEMA_LABEL",
    "SERVING_CONFIG_INPUTS_FIELDS",
    "SERVING_CONFIG_INPUTS_SCHEMA",
    "STAGES",
    "SchemaRefusal",
    "SYNTHETIC_FIXTURE_INGRESS",
    "act_id",
    "artifact_id",
    "attempt_id",
    "build_approval_record",
    "build_envelope",
    "canonical_bytes",
    "canonical_text",
    "check_algebra_is_total",
    "classify",
    "digest_bytes",
    "digest_of",
    "is_well_formed",
    "page_id",
    "parse_ingress_record",
    "real_ingress_record",
    "region_id",
    "require_approval",
    "run_aggregate",
    "self_hash",
    "stage_directory",
    "terminal_category",
    "synthetic_fixture_ingress_record",
    "validate_approval_record",
    "validate_envelope",
    "validate_input_refs",
    "validate_run_id",
    "verify",
    "verify_input_bytes",
    "verify_self_hash",
    "witness_coverage",
]
