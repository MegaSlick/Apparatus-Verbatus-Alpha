# Orchestrator — contract

The orchestrator is not a stage. It establishes nothing and holds no progress state
in the run tree. It can append an optional stage-timing journal outside that tree;
every fact a resume depends on is in the run tree, which is why a run can be
re-entered from any process on any machine. This file says what its driver
vocabulary means, because a word that appears in a `--flag` and nowhere in a document is
a word two branches can define differently.

On real ingress, `--canary-folder` and `--canary-manifest` are an inseparable
pair forwarded to the Door. Fixture ingress refuses the pair. The pod runner
forwards the same two paths from its volume-bound run plan.

## The one sequence

```
door → exemplar → ink map → designator → attestatores → perlector → coniector
     → recensor → archetypus → armarium
```

Every member is a stage program with its own completion boundary. The orchestrator
dispatches no re-reading: the Recensor holds what it cannot accept. The sealed
recovery budget (`config/recovery.toml`) is spent by the Perlector's page re-ask
alone, inside stage 4 (`common/page_reask.py`); the denominator plans the re-ask
again from the same sealed budget to verify it, and `common/test_recovery.py` names
those two as its only readers.

## The three selections

An invocation runs one **contiguous** subsequence, named one of three ways:

| Spelling | Selection | Vocabulary |
|---|---|---|
| `--all`, or no selector | the whole sequence | `auto` |
| `--stage <name>` | exactly that one member | `manual` |
| `--from <a> --to <b>` | inclusive, forward-only | `semi` |

`--mode` may assert the vocabulary the selection already implies; it never chooses one,
and a disagreement is refused. Non-contiguous and reverse selections are refused, because
a gap in a staged run is indistinguishable from an unrecorded skipped boundary.

Entry is gated by evidence, not by memory of the last invocation: each stage program with
a predecessor proves its stored `stage-seal` at `open_context`, and the orchestrator
proves the Armarium's own seal before it reports the run. A member that held after publishing its
evidence sealed, so re-entry past it is legal; a member that held or refused before
publishing did not, so the next entry is a named missing-seal refusal.

## Three stop reasons, told apart

| Stop | Exit | How it is said |
|---|---|---|
| a member **held** | 3 | `manual`/`semi`: `run <id>: <mode> mode stopped at held <name>`, unless the selection ends at the Armarium: such a selection runs through every held member, as `auto` does, so the Armarium's terminal report names every hold. A held Attestatores stops every mode, including `auto`, and says so. So does a held Recensor (below). |
| a boundary **refused** | 2 | the refusing stage's own named `ContractError`/`SchemaRefusal` on stderr, forwarded verbatim |
| the run-level **cap** breached | 4 | `run <id>: halted at the <checkpoint> checkpoint — …`, plus the offending subjects by kind |

The cap is recomputed at **every** member boundary in every mode, before any of the stops
above, and again at a stage program's own entry (`common/stage.refuse_halted_run`) so a
directly invoked stage refuses a halted run without writing. A run that is both held and
over the cap reports the cap: that is the reason that needs fixing rather than re-entry.
Re-entering a halted run is refused again at the resume preflight, from the same tally
recomputed from the same artifacts — the driver caches nothing between invocations.

## A held Recensor stops every mode

Before the Archetypus is invoked, in every mode, the orchestrator reads what the
Recensor's current records hold (`common/page_review.py::held_by_recensor`: each held
review's unit and codes, each held continuation link, each page the
`review-decisions` record still holds, the same total the Recensor exits held on). When anything is held, nothing is established or exported: the
Archetypus and the Armarium are not invoked. The Coniector has already run when the
selection includes it, since it reads only the Perlector's readings. The run exits 3
after `run <id>: stopped at a held recensor, before the archetypus`, listing every held
item and the way on. An unsealed Recensor is left to
the Archetypus to refuse by name.

The way on is a person's: record operator review decisions in the run, then resume it
from the Recensor (`--from recensor --to armarium`), which applies them
(`pipeline/5_recensor/CONTRACT.md`, "Operator review decisions"), or, for a page
`re-ask`, from the Perlector (`--from perlector --to armarium`), which reads the page
again as an operator re-read (`pipeline/4_perlector/CONTRACT.md`, "An operator
re-read") before the Recensor reviews it. The run continues
past the Recensor once nothing is held, or once an `advance` record
(`operations/operator/advance.py`) binds the Recensor's current seal
(`common.stage.boundary_advanced`); the export then names every hold. A Recensor pass
that applies new decisions re-seals, so an advance given before it passes nothing.
The Recensor is in `common.stage.ALWAYS_HELD_BOUNDARIES` for this reason, beside the
Attestatores and the Armarium, and `advance` accepts it in every mode.

**More than 1 in 50 of a run's pages held means the run has a systemic problem.** At
that stop the orchestrator counts the run's held pages
(`common/page_review.py::held_pages_after_review`: a page with any held unit, or still
held by the `review-decisions` record, of the distinct pages the Recensor reviewed).
When their share is more than the run's sealed `[review] max_held_page_share`
(`config/review.toml`, read by `common/review_policy.py` and checked against the run's
`review` seal), the report opens with one line, `run <id>: systemic: <held> of <pages>
page(s) are held after the recensor, more than the sealed limit of <share> ...`,
naming the held pages. The run stops all the same: the alarm adds a reason, never a
pass.

A person's recorded `advance` of the Recensor's seal may still pass a systemic run: it
is an explicit choice, and the run is not trapped. The alarm never goes silent:

- the orchestrator measures the share again at the advance check and prints the same
  `systemic:` line before the run continues;
- the Armarium measures it the same way (`common.page_review.held_share`) and records it
  in the aggregate basis as `systemic_review` `{held_pages, pages,
  max_held_page_share}`, so the aggregate carries the line as a `systemic: ...` reason,
  the run stays partial, the terminal report names it, and the package verifier
  recomputes it (`pipeline/7_armarium/CONTRACT.md`);
- `verbatus run` notifies the line as a `decision` through `operations/notify` when
  notifications are on, at the stop and on a held export, and `verbatus export` names it
  in a partial export's notification;
- the invocation's stop record (`--stop-record`, `orchestrator-stop.v2`: run id, exit code, `exported` and `systemic`) names the line
  as `systemic` (null when none was printed), and `pod_run --notify` sends it from the
  pod as the same `decision` (`operations/pod/notify_hooks.py::notify_systemic`). The
  record's directory must exist and be writable before any stage runs. Once the
  selection starts, the record is written on every return. A refusal raised inside the
  selection is recorded as `EXIT_FATAL` with `exported` false and any alarm already
  printed, and is then raised unchanged. A record that cannot be written ends the
  invocation with `EXIT_FATAL`, and the refusal names the exit, export and alarm it would
  have held, because a caller cannot tell a missing record from a run with no alarm.

## Mode is an invocation choice, never durable bytes

No selection reaches a manifest, a seal, an artifact, a receipt, or any other file. `invoke`
builds each stage's argv explicitly and forwards no selector. This is checked, not asserted:
`test_all_and_manual_stages_write_the_identical_happy_run_tree` and
`test_all_and_a_split_semi_range_write_the_identical_happy_run_tree` drive the identical
run three ways and compare every byte, so a mode that leaked into the tree would differ
between the three and fail.

**Scan triage and the driver share one mode vocabulary.** Triage chooses `manual`, `semi`,
or `auto` per batch through confidence-threshold settings (`config/triage_modes.toml`).
`common.contracts.stages.TRIAGE_MODES` declares that triple once, and
`common.stage.RUN_MODES` aliases it; `pipeline/0_triage/CONTRACT.md` ("Modes and refusals")
records the same join.

The selections have different lifetimes. Triage persists its member as a batch property;
the driver infers one for an invocation and never writes it to the run tree, as the
byte-identity tests above require. Other contracts use a field named `mode` for unrelated
vocabularies, so the field name alone identifies no selection
(the ingress record in `common/contracts/approval.py`, `parse_ingress_record`, and the
crop-policy `mode` of `pipeline/2_designator/geometry_layer.py`, `yolo_obb`).

## Transcript lines and the timing journal

Every stage runs unbuffered (`python -I -u`), and the orchestrator flushes each line
it prints, so a transcript shows lines as they happen. Around each stage it prints
one line when the stage starts and one when it ends, with its exit and duration and
each chair it launched (launch and ready moments, from the stage's launch audits,
found by the empty `launch-audit-<digest>` note the stage leaves beside its engine
logs, so no other blob is read);
around a volume sync, one line when it starts and one with the files copied and its
duration.

With `--stage-sync-root`, each stage's run tree is copied to the volume after it
ends. The file list is frozen at the stage boundary, on the main thread
(`RunTreeSync.plan`); the copy then runs on a thread while the next stage starts, so
that stage's cold start overlaps the copy. Each sync is joined before the next one
starts and before the invocation returns, whatever way it ends: a stage that fails
still waits for the sync running beside it, and a failed sync stops the run when the
stage beside it ends, before any further stage. The volume therefore holds a stage's
files by the end of the stage after it, not before that stage starts: a pod lost
while that stage runs may lack part of the stage before it on the volume.
Each sync is journaled on its own line, `stage` `volume sync after <stage>`, `kind`
`volume-sync`, with its duration, `files_copied`, and `exit_code` 0, or `None` with
`failure` naming why.

The optional stage-timing journal (`--stage-timing-journal`, `stage-timing-journal.v4`)
gets one line per stage invocation, written even when the stage fails. Its readers
check only the schema and the run, so optional fields are added without a new
version. `gpu_utilization` holds summary statistics over every read and at most 120
samples, every `sample_stride`th read; the card is read every 15 s, and not at all
on a host with no `nvidia-smi` on PATH (`None` plus that reason). `serving_spans`
lists each chair the invocation launched with its `started_at`, `ready_at` and
`ready_seconds` (`None` when the audits could not be read); the serving manager
records no stop moment, so the entry's `finished_at` bounds it. A chair taken over
from the stage before keeps its launch and ready moments and adds `adopted_at`, the
moment this invocation took it over; the stage's end line says `adopted` for it, with
no model load, where a launched chair's load would be said.

When a selection runs the Coniector right after the Perlector, the Perlector is
invoked with `--hand-off-to-coniector`, a scheduling hint that is not sealed: a
Perlector whose chair the reconstructor's row shares leaves it serving for the
Coniector to take over (`operations/serving/README.md`, "A shared service").
