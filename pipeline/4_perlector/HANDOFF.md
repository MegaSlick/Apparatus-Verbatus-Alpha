# Perlector — handoff

The Perlector writes one append-only `kind="perlectio"` record for each reading
attempt under `4_perlector/artifacts/`. This walking-skeleton writer takes its
text from the declared synthetic fixture solely to exercise the evidence shape;
it does not claim a real model reading. Its artifacts are `skeleton.v1` envelopes
with derived identities, attempt bindings, self-hashes, and checked direct inputs.

## Input boundary

For every proposed act, the stage reads every Designator region currently in the
act's history, recomputes its crop from the sealed Exemplar page and transform,
checks the crop bytes, and validates the region's own resolved Designator
identity/revision and serving receipt. It reads and validates every Testimonium
for the act, then derives one current record per chair by unique attempt ordinal;
all superseded records remain immutable history. It does not select a preferred
witness or use witness agreement to choose its text.

Recovery regions are readable evidence for a later attempt. They remain marked
`witness_covered=false` unless a completed Testimonium actually names that exact
region; a recrop never rewrites what a witness saw.

## `kind="perlectio"`

The subject is the stable act identity and the attempt is `perlegere:<ordinal>`.
A successful or attempted reading payload contains:

```text
act_key, attempt_ordinal, text
basis = {
  regions = [{region_id, image_path, image_sha256, transform,
              verified_dimensions, source_page_ordinal, source_page_id,
              structure_provenance, witness_covered}, ...],
  testimonia = [{chair, artifact_id, outcome, reference}, ...]
}
dissent, provenance
```

The direct input set is exactly the crop blobs read plus the digest-checked
Testimonium artifacts listed in `basis.testimonia`. `dissent` is computed after
the reading is fixed; it says where the fixture reading differs from a witness
that reported, not which witness is right. Provenance carries the resolved
Perlector identity, revision, adapter recipe, serving receipt, and witness regime.

A held act or unavailable reader receives an explicit non-completed Perlectio
with its reason, not a fabricated text. The deterministic fixture also exercises
`truncated`; Recensor treats every non-completed outcome as a visible hold.

## Consumer obligations

Recensor derives the latest reading by unique attempt ordinal, refuses a tie, and
writes the exact Perlectio reference it reviewed into every review/request. The
ordinal is not taken on the payload's word: every consumer names the operation it
is collapsing attempts of (`perlegere`, `recense`, `read:<chair>`) and the sealed
`attempt_id` is recomputed from (subject, operation, ordinal), because the envelope
binds `artifact_id` to that token without ever re-deriving the token itself.
Ordinals must also be the contiguous run 1..N -- attempts are append-only and never
reused, so a gap is an attempt that is no longer here, and a manufactured far
ordinal cannot leapfrog the attempt that happened.
Archetypus and Armarium follow that reference rather than independently looking up
whatever reading now sorts latest. This prevents an unreviewed recovery reading
from becoming established text.
