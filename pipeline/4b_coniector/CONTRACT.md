# Coniector — contract

The Coniector runs directly after the Perlector, before the Recensor, on a side branch:
it reads the Perlector's sealed readings and only the Armarium reads what it
writes. Its seal's predecessor is the Perlector (`common/contracts/stages.py`,
`HANDOFFS`); the Armarium verifies its seal beside the Archetypus's
(`SIDE_BRANCHES`).

It proposes, beneath each diplomatic reading, a labelled and unconfirmed
reconstruction from text alone. It never sees a page image, never changes the
diplomatic reading and never decides where an act ends: every outcome it writes
flows onward (`common/contracts/outcomes.py`, `TERMINAL_CATEGORY`), and no
reconstruction is counted in any denominator.

## Inputs

- The sealed switches and bounds of `config/reconstruction.toml`
  (`--reconstruction-config`, sealed as `reconstruction`): `mode` (`on` or
  `off`, default `on`), `pages_are_consecutive` (default `false`) and the
  departure bounds `common.reconstruction.load_reconstruction_policy` reads.
- The sealed decoding policy: the `reconstructor` chair's sampling row and
  `[reconstructor_generation] answer_max_tokens`.
- `common.stage.reading_acts`: every entry of each read page answer (classes
  `reading` and `reading-unplaced`), each read through its `perlectio.v3`. Its diplomatic text is the Perlectio's text with its doubt
  marks rendered back (`render_doubt_marks`).
- The `reconstructor` chair from the run's roster: the Perlector's model at the
  Perlector's revision, asked text only. A serving row that is not live reads the
  synthetic fixture's declared replies; on a real submission whose plan asks any
  call, such a row is refused before anything is published.

## Records

Shapes and derivations are `common/reconstruction_records.py`, the one module
both this stage and the Armarium derive them with.

`reconstruction-plan` (subject `coniector`, outcome `planned`), schema
`coniector-plan.v2`: `{mode, pages_are_consecutive, policy_sha256, calls}`. `calls` is
`common.reconstruction.reconstruction_plan` over the entries: one call per page,
`{page_ordinal, subjects, chains, context}`. Chains (an act crossing an agreed
page break) and context (the neighbouring pages' edge acts) exist only when the
run is sealed `pages_are_consecutive`. With `mode = "off"` `calls` is empty and
no model is loaded. An act a page's re-ask recovered (`reading_attempt` 2) is a
diplomatic reading like any other and may be a subject, but a page's edges are
its current whole-page reading's (its first reading's, or an operator re-read's
that superseded it), as the Recensor's page breaks are: a recovered act is never
a chain piece or another page's context, and one carrying a continuation flag,
which a re-ask may not set, is refused.

After an operator re-read (`pipeline/4_perlector/CONTRACT.md`, "An operator
re-read") the readings differ from those the sealed plan was made over. The pass
then publishes the new plan as the plan's next generation, with `supersedes`
naming the last one (attempt `replan:<n>`; the first plan has no attempt and no
`supersedes`), and each call or reconstruction that differs from every one
sealed for its page or subject as that subject's next generation (attempts
`recall:<n>`, `remake:<n>`). The current plan is the last of the chain; a page's
current call and a subject's current reconstruction are the ones the current
plan and readings give. Every earlier record stays as made, superseded. The
reconstruction stays tied to the model's reading it was made from: a reading a
person later corrects is shown above it as "model reading (original)"
(`pipeline/7_armarium/CONTRACT.md`).

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
record), `call-failed` (with the retained bytes) or, in a replay run
(`pipeline/4_perlector/CONTRACT.md`, "A replay"), `not-replayed`: its source run never
sent this call in these bytes, so no recorded reply answers it, and it names no
capacity, failure or receipt. In a replay every other call is answered with the
source's retained reply through the same client.

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

## Calls

A live pass with a call that has no sealed record starts its chair on a background
thread once the plan is published, while the calls are drawn, and waits for it
before the first call is sent; a failed start stops the pass on the main thread. A
pass whose every call is already sealed loads no model. A pass whose calls all turn
out not to be sent (over capacity) has started a chair it does not use: it waits
for that start and stops the chair before its seal; a pass that stops while the chair is still loading does not wait
for the load, whose thread stops the chair once it returns. When the
Perlector left its chair serving for this stage and the reconstructor's row shares
that service, starting the chair takes the running service over instead of loading
the model again; its receipt names the reconstructor and keeps the service's start
moment, and its launch audit says `launch_purpose = "adopted"` and what it was
taken over from. A take-over any check refuses stops that service and starts the
chair as usual, the reason in the launch audit (`operations/serving/README.md`, "A
shared service"). A pass that sends nothing stops a service left for it before its
seal. Calls are
sent through `common.in_order_window`: up to the launched row's `max_num_seqs` (the row's
own, or the run's `--capacity-plan` width) in flight at once on a live row, one at a time otherwise. Only the request itself
leaves the main thread; each call's record and its reconstructions are published
on the main thread in plan order, whatever order the replies arrive in, so the
records are the bytes a serial pass publishes.

## Resume

A page whose `reconstruction-call` is sealed is not asked again: its record is
adopted when it was asked from this plan call, this prompt, this serving mode
and this chair, and refused by name otherwise, and its reconstructions are
derived again from its reply. That includes a call that failed or was refused:
its record is sealed evidence, and asking again would publish different bytes
under the same identity, so reconstructing that page again takes a new run,
unless an operator re-read changed the readings and the plan with them. A
live call interrupted before any reply was retained is asked again. One whose
reply or call record was retained but is named by no record stops the resumed
pass before it sends anything: asking again would ask that page twice, so it is
reconstructed in a new run, and the retained bytes stay in the run tree as the
interrupted pass's evidence (`common.retained_replies`).

A chain's pieces are never subjects of their own: they are asked as one join.
When the Coniector reads them as not one act, the join is `does-not-continue`
and the pieces carry no reconstruction.

## What the Armarium checks

`common.reconstruction_records.verified_reconstructions` recomputes everything:
the plan from the sealed switches and the Perlector's current readings (so a
Coniector sealed over readings that differ from the Perlector's sealed readings is
refused); each
call's prompt; its maker against the run's roster and its receipt; a call not
asked against the evidence its reason leaves (the roster's absent chair, the
capacity record that did not fit, the failure's retained bytes); each reply
against the fixture's declaration (a scenario that declares none for a page is
answered with every subject and chain and no finding or departure) or the
retained engine bytes, and a live
call's retained record against this prompt's request, rendered again with the
chair's sealed sampling row and the receipt's seed; each parse; and every
reconstruction from its reply. A record the recomputation does not give, or one
it gives that is missing, is `FatalAccounting`; once a plan supersedes another,
a call or reconstruction made from a superseded call is kept and not shown.
