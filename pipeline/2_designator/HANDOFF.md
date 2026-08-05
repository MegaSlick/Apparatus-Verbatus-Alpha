# Designator — handoff

The Designator turns sealed Exemplar pages into the act denominator for the
walking skeleton. It writes only ordinary `skeleton.v1` artifacts below
`2_designator/artifacts/`; each envelope has a derived artifact identity,
attempt binding where applicable, a self-hash, and digest-checked direct inputs.
The derived manifest is inventory, not a second authority.

## Scope and input boundary

Before cutting anything, the Designator reconciles `run.json`'s submitted
filename ledger with every Exemplar page outcome and the one self-hashed corpus
seal. A sealed page's Door admission and pixel blob are checked again before its
pixels are cropped. The check is deliberately before the first region write.

The current structural proposer is the declared synthetic walking skeleton. On
real, approval-gated ingress this program performs that Exemplar boundary check
and then stops with a refusal: it does not invent proposals, holds, or text in
place of the unbuilt real structural model.

## `kind="region"`

There is one append-only region record for each original proposal crop, and a
second original record for a continuation on another page. Its subject is the
stable act identity; its attempt is `crop:<ordinal>`. The payload carries:

```text
region_id, act_key, attempt_ordinal, origin
transform = {operation, source_page_ordinal, source_page_id, bounds}
image_path, image_sha256
provenance (resolved Designator chair identity, revision, and serving receipt)
```

`origin="proposal"` is the evidence the witnesses saw. A later
`origin="recovery"` record is a new crop for the same act, never a replacement
for the proposal. Its direct inputs are exactly the sealed Exemplar pixel blob
and the Recensor recovery-request it answers; a proposal crop inputs only the
sealed Exemplar pixel blob. Consumers recompute the crop from that page and
transform, so a same-sized crop from another page does not pass as this one.

## `kind="hold"` and `kind="proposal-seal"`

If an act's own page or necessary continuation was not sealed, the Designator
publishes one `held` record rather than omitting the act. Its direct input is the
relevant Exemplar page outcome and its payload names the act key, unsealed page
ordinal, and reason.

The once-only `proposal-seal` is the downstream denominator. Its self-hashed
payload contains `count`, Designator provenance, and one `expected_acts` row per
synthetic act:

```text
act_id, act_key, page_id, page_ordinal, has_continuation, outcome, evidence
```

`evidence` is the exact sorted set of region and/or hold references for that act,
and the seal's direct input set is their exact union. Consumers reject a shorter
denominator, an unaccounted Designator record, a duplicate, a claimed
continuation with no supporting proposal, or a mismatch between the row and its
evidence.

## Recovery boundary

`--operation recover --act <id> --recovery-request <id>` is the only recovery
entry point. The request must be the exact current, digest-checked Recensor
request for that act, its next ordinal, its Perlectio evidence, and the
run-bound `config/recovery.toml` policy. A command without that request does not
cut a crop. The orchestrator, not this stage, decides whether such a request is
outstanding and invokes this program.

## Consumers

Attestatores reads proposal regions only and records which pixels each witness
saw. Perlector may read recovery regions but marks them witness-uncovered unless
a Testimonium actually names them. Recensor, Archetypus, and Armarium use the
proposal seal as the conserved act denominator; none may manufacture a new act
or choose among competing crops.
