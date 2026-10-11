# Orchestrator: contract

The orchestrator is not a stage. It establishes nothing and holds no progress state in the
run tree; every fact a resume depends on is in the run tree, so a run can be re-entered from
any process on any machine. It may append a stage-timing journal outside the tree. This file
defines its driver vocabulary.

On real ingress, `--canary-folder` and `--canary-manifest` are an inseparable pair forwarded
to the Door; fixture ingress refuses them.

## The one sequence

```
door → exemplar → ink map → designator → attestatores → perlector → coniector
     → recensor → archetypus → armarium
```

Every member is a stage program with its own completion boundary. The orchestrator
dispatches no re-reading: the Recensor holds what it cannot accept. The sealed recovery
budget (`config/recovery.toml`) is spent only by the Perlector's page re-ask inside stage 4
(`common/page_reask.py`).

## The three selections

An invocation runs one **contiguous** subsequence:

| Spelling | Selection | Vocabulary |
|---|---|---|
| `--all`, or no selector | the whole sequence | `auto` |
| `--stage <name>` | exactly that member | `manual` |
| `--from <a> --to <b>` | inclusive, forward-only | `semi` |

`--mode` may assert the vocabulary the selection implies, never choose one; a disagreement is
refused. Non-contiguous and reverse selections are refused, because a gap is
indistinguishable from an unrecorded skipped boundary.

Entry is gated by evidence: each stage with a predecessor proves its stored `stage-seal` on
opening, and the orchestrator proves the Armarium's seal before reporting the run. A member
that held after publishing its evidence sealed, so re-entry past it is legal; one that held
or refused before publishing did not, and the next entry is a named missing-seal refusal.

## Three stop reasons

| Stop | Exit | How it is said |
|---|---|---|
| a member **held** | 3 | `manual`/`semi`: `run <id>: <mode> mode stopped at held <name>`, unless the selection ends at the Armarium, which runs through every hold as `auto` does so the terminal report names them all. A held Attestatores and a held Recensor stop every mode |
| a boundary **refused** | 2 | the refusing stage's `ContractError`/`SchemaRefusal` on stderr, forwarded verbatim |
| the run-level **cap** breached | 4 | `run <id>: halted at the <checkpoint> checkpoint — …`, with the offending subjects by kind |

The hard-failure cap is recomputed from disk at every member boundary in every mode, before
any other stop, and again at each stage's own entry (`common/stage.refuse_halted_run`), so a
directly invoked stage refuses a halted run without writing. A run both held and over the cap
reports the cap. Re-entering a halted run is refused at the resume preflight; the driver
caches nothing between invocations.

## A held Recensor stops every mode

Before the Archetypus is invoked, in every mode, the orchestrator reads what the Recensor's
current records hold (`common/page_review.py::held_by_recensor`: held reviews, held
continuation links, and pages the `review-decisions` record still holds). When anything is
held, the Archetypus and Armarium are not invoked (the Coniector has already run when the
selection includes it). The run exits 3 after `run <id>: stopped at a held recensor, before
the archetypus`, listing every held item and the way on. An unsealed Recensor is left to the
Archetypus to refuse.

The way on is a person's: record operator review decisions, then resume from the Recensor
(`--from recensor --to armarium`; `pipeline/5_recensor/CONTRACT.md`, "Operator review
decisions"), or for a page `re-ask` from the Perlector (`--from perlector --to armarium`;
`pipeline/4_perlector/CONTRACT.md`, "An operator re-read"). The run continues past the
Recensor once nothing is held, or once an `advance` record (`operations/operator/advance.py`)
binds the Recensor's current seal (`common.stage.boundary_advanced`); the export then names
every hold. A Recensor pass that applies new decisions re-seals, so an earlier advance passes
nothing. The Recensor is in `common.stage.ALWAYS_HELD_BOUNDARIES` with the Attestatores and
the Armarium, and `advance` accepts it in every mode.

**The systemic alarm.** At that stop the orchestrator counts held pages
(`common/page_review.py::held_pages_after_review`). When their share exceeds the sealed
`[review] max_held_page_share` (`config/review.toml`, `common/review_policy.py`), the report
opens with `run <id>: systemic: <held> of <pages> page(s) are held after the recensor, more
than the sealed limit of <share> ...`. The run stops all the same: the alarm adds a reason,
never a pass. An `advance` may still pass a systemic run, but the alarm never goes silent:

- the orchestrator prints the same `systemic:` line again at the advance check;
- the Armarium records `systemic_review` `{held_pages, pages, max_held_page_share}` in the
  aggregate basis, so the run stays partial and the verifier recomputes it;
- `verbatus run` and `verbatus export` notify it as a `decision` when notifications are on;
- the stop record (`--stop-record`, `orchestrator-stop.v2`: run id, exit code, `exported`,
  `systemic`) names it, and `pod_run --notify` sends it from the pod
  (`operations/pod/notify_hooks.py::notify_systemic`).

The stop record's directory must exist and be writable before any stage runs; once the
selection starts the record is written on every return. A refusal inside the selection is
recorded as `EXIT_FATAL` and then raised unchanged. A record that cannot be written ends the
invocation with `EXIT_FATAL`, naming what it would have held, because a caller cannot tell a
missing record from a run with no alarm.

## Mode is an invocation choice, never durable bytes

No selection reaches a manifest, seal, artifact, receipt or any other file; `invoke` builds
each stage's argv explicitly and forwards no selector.
`test_all_and_manual_stages_write_the_identical_happy_run_tree` and
`test_all_and_a_split_semi_range_write_the_identical_happy_run_tree` drive the same run three
ways and compare every byte.

Scan triage uses the same `manual`/`semi`/`auto` vocabulary per batch
(`config/triage_modes.toml`; `common.contracts.stages.TRIAGE_MODES`, aliased as
`common.stage.RUN_MODES`; `pipeline/0_triage/CONTRACT.md`, "Modes and refusals"). Triage
persists its mode as a batch property; the driver never writes one. Other contracts use a field
named `mode` for unrelated things (`common/contracts/approval.py`'s ingress record, the
Designator's crop-policy `mode`), so the field name alone identifies no selection.

## Transcript, volume sync and timing journal

Every stage runs unbuffered (`python -I -u`) and the orchestrator flushes each line, so a
transcript shows lines as they happen. Around each stage it prints a start line and an end
line with its exit, duration and each chair it launched (launch and ready moments from the
stage's launch audits, found via the empty `launch-audit-<digest>` note beside its engine
logs).

With `--stage-sync-root`, each stage's run tree is copied to the volume after it ends. The
file list is frozen at the stage boundary (`RunTreeSync.plan`) and the copy runs on a thread
while the next stage starts. Each sync is joined before the next one starts and before the
invocation returns; a failed sync stops the run when the stage beside it ends. So the volume
holds a stage's files by the end of the following stage: a pod lost during that stage may lack
part of the previous one. Each sync is journaled (`kind` `volume-sync`, duration,
`files_copied`, `exit_code` 0, or `None` with `failure`).

`--stage-timing-journal` (`stage-timing-journal.v4`) gets one line per stage invocation, even
when the stage fails; readers check only the schema and run, so optional fields can be added.
`gpu_utilization` summarises reads every 15 s (at most 120 samples; `None` with a reason when
there is no `nvidia-smi`). `serving_spans` lists each launched chair's `started_at`,
`ready_at` and `ready_seconds`; a chair taken over from the previous stage keeps its launch
moments and adds `adopted_at`.

When a selection runs the Coniector right after the Perlector, the Perlector gets
`--hand-off-to-coniector`, an unsealed scheduling hint: a Perlector whose chair the
reconstructor's row shares leaves it serving for the Coniector to take over
(`operations/serving/README.md`, "A shared service").
