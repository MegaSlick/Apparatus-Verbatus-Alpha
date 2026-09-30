# Coniector — contract

The Coniector runs after the Archetypus and before the Armarium, on a side branch:
it reads the Perlector's sealed readings and only the Armarium reads what it
writes. Its seal's predecessor is the Perlector (`common/contracts/stages.py`,
`HANDOFFS`); the Armarium verifies its seal beside the Archetypus's
(`SIDE_BRANCHES`). It runs after recovery so it reads the Perlector's final
readings.

It proposes, beneath each diplomatic reading, a labelled and unconfirmed
reconstruction from text alone. It never sees a page image, never changes the
diplomatic reading and never decides where an act ends: every outcome it writes
flows onward (`common/contracts/outcomes.py`, `TERMINAL_CATEGORY`), and no
reconstruction is counted in any denominator.

## Inputs

- The sealed switches and bounds of `config/reconstruction.toml`
  (`--reconstruction-config`, sealed as `reconstruction`): `mode` (`on` or
  `off`, default `off`), `pages_are_consecutive` (default `false`) and the
  departure bounds `common.reconstruction.load_reconstruction_policy` reads.
- The sealed decoding policy: the `reconstructor` chair's sampling row and
  `[reconstructor_generation] answer_max_tokens`.
- On a page-read run, `common.stage.reading_acts`: every entry of each read page
  answer (classes `reading` and `reading-unplaced`), each read through its
  `perlectio.v2`. Its diplomatic text is the Perlectio's text with its doubt
  marks rendered back (`render_doubt_marks`).
- The `reconstructor` chair from the run's roster: the Perlector's model at the
  Perlector's revision, asked text only.

An act-read run has no page answers to reconstruct over; its plan says so and
asks nothing.

## Records

Shapes and derivations are `common/reconstruction_records.py`, the one module
both this stage and the Armarium derive them with.

`reconstruction-plan` (subject `coniector`, outcome `planned`), schema
`coniector-plan.v1`: `{mode, pages_are_consecutive, policy_sha256,
reading_unit, not_applicable, calls}`. `calls` is
`common.reconstruction.reconstruction_plan` over the entries: one call per page,
`{page_ordinal, subjects, chains, context}`. Chains (an act crossing an agreed
page break) and context (the neighbouring pages' edge acts) exist only when the
run is sealed `pages_are_consecutive`. With `mode = "off"` `calls` is empty and
no model is loaded.

`reconstruction-call` (subject the page's `page_id`), schema `coniector-call.v1`,
one per planned call: the plan call; `prompt_version` and `prompt_sha256` of the
text `common/reconstruction_prompt.py` builds; `shown`, every entry key the
prompt showed; `serving_mode`; the admitted `capacity`; the live
`engine_call` or the call `failure`; `reply_text`, the reply exactly as given;
`finish_reason`, `stop_reason`; `parse_state` (`parsed`, `malformed`,
`answer-invalid`, `reply-cut-off` or `not-asked`) and `problems`; `maker`, the
chair and the receipt of what served it. Outcome `answered` when parsed, else
`not-answered`. A reply cut at the output cap is never read. A call not asked
names one reason: `chair-absent`, `request-over-capacity` (with its capacity
record) or `call-failed` (with the retained bytes).

`reconstruction` (subject the act's `act_id`, or a join's first piece), schema
`coniector-reconstruction.v1`, one per subject act and one per chain: `{unit
(act|join), act_ids, act_keys, page_ordinal, call_ref, label, made, maker,
diplomatic_raw_sha256, diplomatic_clean_sha256s, reconstruction_raw,
reconstruction_text, reconstruction_uncertainty, continues, departures,
findings, not_made}`. Outcome `made` or `not-made`.

- A departure is `{diplomatic, reconstruction, reason?}`; applied, it records
  its span in raw and clean offsets and whether its replacement occurs in any
  text the call showed (`replacement_in_context`). No basis citation is asked
  for or required.
- A finding is `{code, reason?}`, code one of `cut-at-page-break`, `incomplete`,
  `out-of-sequence`, `inconsistent`, `other`. Findings are flags: they are
  exported and change nothing, and no finding ever holds the diplomatic.
- Not made: the call's reason for every act and join of a call with no usable
  answer (`reply-malformed`, `reply-answer-invalid`, `reply-cut-off`, or the
  reason it was not asked); otherwise the act's own
  (`common.reconstruction.NOT_MADE_CODES`), or `does-not-continue` for a join
  whose pieces the Coniector reads as not one act. One failing departure leaves
  its whole act, or join, not made; the rest of the page stands.

## Resume

A page whose `reconstruction-call` is sealed is not asked again: its record is
adopted when it was asked from this plan call and this prompt, and refused by
name otherwise, and its reconstructions are derived again from its reply. A
live call interrupted before its record was published is asked again; its
retained reply stays in the run tree.

## What the Armarium checks

`common.reconstruction_records.verified_reconstructions` recomputes everything:
the plan from the sealed switches and the Perlector's readings; each call's
prompt; each reply against the fixture's declaration or the retained engine
bytes (and the call record against the chair's sealed sampling row); each parse;
and every reconstruction from its reply. A record the recomputation does not
give, or one it gives that is missing, is `FatalAccounting`.
