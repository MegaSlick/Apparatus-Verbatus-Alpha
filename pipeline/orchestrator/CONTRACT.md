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
     → archetypus → armarium
```

Every member is a stage program with its own completion boundary. No member re-asks a
reading: the Recensor holds what it cannot accept. The sealed recovery budget
(`config/recovery.toml`) is a forward binding: every run seals it at the Door, and
the page re-ask will spend it. No stage reads it yet, and `common/test_recovery.py`
names that.

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
| a member **held** | 3 | `manual`/`semi`: `run <id>: <mode> mode stopped at held <name>`, unless the selection ends at the Armarium: such a selection runs through every held member, as `auto` does, so the Armarium's terminal report names every hold. A held Attestatores stops every mode, including `auto`, and says so. |
| a boundary **refused** | 2 | the refusing stage's own named `ContractError`/`SchemaRefusal` on stderr, forwarded verbatim |
| the run-level **cap** breached | 4 | `run <id>: halted at the <checkpoint> checkpoint — …`, plus the offending subjects by kind |

The cap is recomputed at **every** member boundary in every mode, before any of the stops
above, and again at a stage program's own entry (`common/stage.refuse_halted_run`) so a
directly invoked stage refuses a halted run without writing. A run that is both held and
over the cap reports the cap: that is the reason that needs fixing rather than re-entry.
Re-entering a halted run is refused again at the resume preflight, from the same tally
recomputed from the same artifacts — the driver caches nothing between invocations.

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
