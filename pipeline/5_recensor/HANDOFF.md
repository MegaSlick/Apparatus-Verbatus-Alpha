# Recensor — handoff

The Recensor establishes no text. It writes append-only review history under
`5_recensor/artifacts/`, using `skeleton.v1` envelopes with a derived attempt
identity, self-hash, and digest-checked parents. The stage first validates every
act's witness denominator, so a duplicate or unsealed witness record is refused
before it writes a review for an earlier act.

## Input boundary and current state

For each Designator expected act, Recensor reads all Testimonia by chair and
derives a current outcome only from the unique greatest `attempt_ordinal`. A
missing configured chair, an unsealed extra chair, or a duplicate ordinal is a
fatal accounting error; it is never resolved by sort order. Completed coverage is
`read` plus `genuinely-empty`, while failed and not-run outcomes remain visible
shortfalls.

It reads the current Perlectio in the same unique-ordinal manner, verifies its
direct evidence, and verifies that a claimed continuation has all of its original
proposal regions. A non-completed reading, short continuation, exhausted recovery
budget, or declared hold is recorded as held-for-review rather than accepted.

## `kind="review"`

Every review payload has `act_key`, `attempt_ordinal`, coverage, the recovery
counts/bounds, a reason where applicable, and `perlectio_ref`. The Perlectio
reference is both a payload fact and a direct input: it names exactly the reading
the review assessed. Ordinary terminal records use `accepted` or
`held-for-review`; held Designator acts instead directly input their hold evidence.

An accepted review is not a new reading and does not select among witnesses. It
only records that this precise Perlectio and the conserved geometry/coverage
reconciled.

## `kind="recovery-request"`

When the bounded policy permits a recrop, Recensor appends a
`recovery-requested` request. Its direct input is the exact Perlectio, and its
payload carries the act key, ordinal, coverage, budget used/allowed, the complete
resolved recovery policy, and `perlectio_ref`. It appends a matching
`recovery-requested` review whose direct inputs are that same Perlectio and exact
request, with `recovery_request_ref` and the same policy in its payload.

`config/recovery.toml` is read through `common.recovery`, is included in the
run configuration digest, and records its file digest, absolute cap, and resolved
allowed budget. The orchestrator reads the latest review, rechecks its request and
policy bindings, then invokes the Designator with the request id. Neither a bare
CLI command nor an unbound request may cause a recrop.

## Consumer obligations

Archetypus establishes text only for a current `accepted` review and follows its
exact `perlectio_ref`; it does not reselect a newer reading. Armarium derives the
terminal category from this review history and keeps all holds visible, so a
partial result cannot present as complete.
