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
door → exemplar → ink map → designator → attestatores → perlector → recensor
     → archetypus → coniector → armarium
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
review's unit and codes, each held continuation link, the same total the Recensor
exits held on). When anything is held, nothing is established or exported: the
Archetypus and the Armarium are not invoked, the Coniector still runs when the
selection includes it (it reads only the Perlector's readings, so what is left needs no
model), and the run exits 3 after `run <id>: stopped at a held recensor, before the
archetypus`, listing every held item and the way on. An unsealed Recensor is left to
the Archetypus to refuse by name.

The way on is a person's: record operator review decisions in the run, then resume it
from the Recensor (`--from recensor --to armarium`), which applies them
(`pipeline/5_recensor/CONTRACT.md`, "Operator review decisions"). The run continues
past the Recensor once nothing is held, or once an `advance` record
(`operations/operator/advance.py`) binds the Recensor's current seal
(`common.stage.boundary_advanced`); the export then names every hold. A Recensor pass
that applies new decisions re-seals, so an advance given before it passes nothing.
The Recensor is in `common.stage.ALWAYS_HELD_BOUNDARIES` for this reason, beside the
Attestatores and the Armarium, and `advance` accepts it in every mode.

**More held than a few pages is a problem with the run.** At that stop the orchestrator
counts the run's held pages (`common/page_review.py::held_pages_after_review`: a page
with any held unit, or still held by the `review-decisions` record, of the pages the
Recensor reviewed, which are all the run's sealed pages). When their share is more
than the run's sealed `[review] max_held_page_share` (`config/review.toml`, 1/50 by the
lead's ruling, read by `common/review_policy.py` and checked against the run's
`review` seal), the report opens with one line, `run <id>: systemic: <held> of <pages>
page(s) are held after the recensor, more than the sealed limit of <share> ...`,
naming the held pages. The run stops all the same: the alarm adds a reason, never a
pass. `verbatus run` notifies that line as a `decision` through `operations/notify`
when notifications are on. A run sealed before the policy existed says the check was
not made.

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
