# Archetypus — handoff

The Archetypus is the first and only current stage that calls one machine reading
established. It writes a once-only `kind="archetypus"` record under
`6_archetypus/artifacts/` for an act whose current Recensor review is exactly
`accepted`. A held act deliberately has no Archetypus record; that absence is part
of the terminal accounting, not a gap to fill.

## Input boundary

The stage derives the current review by unique attempt ordinal. For an accepted
review it takes the digest-checked `perlectio_ref` carried by that review and
resolves that exact Perlectio. It does not independently select whatever
Perlectio is now latest. The Perlectio must be a completed-class reading with
valid serving provenance; a held Designator act may not be resurrected by an
accepted later review.

## `kind="archetypus"`

The artifact subject is the stable act identity. Its payload is separately
self-hashed and contains:

```text
act_id, act_key, page_id, status = "established", text
regions, provenance, dissent_ref
perlectio_ref, recensor_ref, self_hash
```

`text`, `regions`, and provenance are exact copies of the one reviewed
Perlectio; `dissent_ref` names that Perlectio artifact rather than making a
second mutable dissent copy. `perlectio_ref` and `recensor_ref` are typed,
digest-checked references and both are direct inputs, together with the exact
crop blobs named by the reading. The normal envelope also carries its own
self-hash and derived identity.

There is no alternate text, no witness text field, and no branch that chooses
among readings. A later run cannot write a second different record under the
same once-only identity.

## Consumer obligations

Armarium requires exactly one Archetypus record for an accepted act, rather than
selecting one. Before export it verifies the nested self-hash, both parent
references and direct-input chains, and exact equality of text, regions,
provenance, and dissent reference with the reviewed Perlectio. It then links each
region back to the original Exemplar filename ledger.
