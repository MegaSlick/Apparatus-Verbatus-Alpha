# Orchestrator — handoff

The orchestrator is not a stage. It establishes nothing, writes nothing of its own, and
holds no progress state: every fact a resume depends on is in the run tree, which is why
a run can be re-entered from any process on any machine. This file says what its driver
vocabulary means, because a word that appears in a `--flag` and nowhere in a document is
a word two branches can define differently.

## The one sequence

```
door → exemplar → designator → attestatores → perlector → recensor
     → recovery → archetypus → armarium
```

Recovery is a genuine member with no stage program of its own, not hidden work performed
before the Archetypus. That is what makes `--stage archetypus` execute the same boundary
code `--all` does at that point.

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
a predecessor proves its stored `stage-seal` at `open_context`, the recovery member proves
the Recensor boundary before dispatching a recrop, and the orchestrator proves the
Armarium's own seal before it reports the run. A member that held after publishing its
evidence sealed, so re-entry past it is legal; a member that held or refused before
publishing did not, so the next entry is a named missing-seal refusal.

## Three stop reasons, told apart

| Stop | Exit | How it is said |
|---|---|---|
| a member **held** | 3 | `manual`/`semi`: `run <id>: <mode> mode stopped at held <name>`. A held Attestatores stops every mode, including `auto`, and says so. The Armarium is excluded: it is always last, so its hold falls through to the run's terminal report rather than losing it. |
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
`common/contracts/stages.py:113-115` declares that triple once as `TRIAGE_MODES`, and
`common/stage.py:230-232` aliases it as `RUN_MODES`; `pipeline/0_triage/HANDOFF.md:49-53`
records the same join.

The selections have different lifetimes. Triage persists its member as a batch property;
the driver infers one for an invocation and never writes it to the run tree, as the
byte-identity tests above require. Other contracts use a field named `mode` for unrelated
vocabularies, so the field name alone identifies no selection
(`common/contracts/approval.py:112-138`,
`pipeline/2_designator/geometry_layer.py:570`).
