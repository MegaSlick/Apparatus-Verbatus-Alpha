"""The refusals. Every contract violation raises one of these, never returns a flag.

Separated from the schemas so a stage can catch what it means to catch. The
distinction that matters is FatalAccounting versus the rest: a schema refusal is
one unit refused with a reason and the run continues around it, while a unit
belonging to no terminal set is invariant #10's fatal imbalance and stops the run.
"""


class ContractError(Exception):
    """Base for every refusal this package raises."""


class SchemaRefusal(ContractError):
    """An artifact does not match the contract it declares.

    Wrong schema label, missing required field, malformed identity, or an input
    reference whose digest does not match the bytes. The unit is refused with a
    reason; nothing is repaired silently (harvest #3: a refusal is recorded, never
    a silent omission).
    """


class ReservedKindRefusal(SchemaRefusal):
    """A producer attempted to mint a kind whose owning branch has not landed.

    Reserving a name is deliberately an active contract boundary: an unused name
    proves nothing, whereas this refusal stops a premature producer from making
    an artifact that later consumers would be tempted to reinterpret.
    """


class ReceiptVersionMismatch(SchemaRefusal):
    """A receipt label does not describe the coverage facts it carries."""


class IdentityRefusal(SchemaRefusal):
    """An identity does not verify against the bindings it claims.

    Identities here are derived, so a forged or drifted one is detectable by
    recomputation. Refusing it is what makes "act identity survives recropping"
    (ARCHITECTURE invariant 1) a checkable property rather than a convention.
    """


class ApprovalRefusal(SchemaRefusal):
    """An action that requires Tyrel's approval-record artifact does not carry one.

    GOVERNANCE: only Tyrel approves an exclusion. A claimed approval with no
    artifact is no approval, so the schema refuses rather than trusting the claim.
    """


class FatalAccounting(ContractError):
    """A unit is in no terminal set, or in more than one.

    Harvest invariant #10: total partition, proven, at every stage boundary —
    every unit is exactly one of completed / unresolved / failed, and a unit in
    none of those sets is a FATAL accounting imbalance, never a warning. This is
    deliberately not a subclass of SchemaRefusal: a stage may catch and record a
    refused unit, but nothing may catch this and carry on.
    """


class IncompatibleReuse(ContractError):
    """A run tree already holds an artifact that contradicts what we are about to write.

    Reuse is byte-for-byte or it is a refusal. Stopping before any write is what
    keeps a resumed run from half-rewriting a sealed tree.
    """
