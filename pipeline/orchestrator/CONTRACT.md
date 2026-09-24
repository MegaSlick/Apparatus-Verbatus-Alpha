# Orchestrator — contract

The orchestrator is not a stage. It establishes nothing, writes nothing of its own, and
holds no progress state: every fact a resume depends on is in the run tree, which is why
a run can be re-entered from any process on any machine. This file says what its driver
vocabulary means, because a word that appears in a `--flag` and nowhere in a document is
a word two branches can define differently.

## The one sequence

```
door → exemplar → ink map → designator → attestatores → perlector → recensor
     → recovery → archetypus → armarium
```

Recovery is a genuine member with no stage program of its own, not hidden work performed
before the Archetypus. That is what makes `--stage archetypus` execute the same boundary
code `--all` does at that point.

**Every round screens the whole outstanding batch before it dispatches any of it.**
`undispatchable_recovery_reason` answers, per request, why this orchestrator cannot
dispatch it: a `recovery_kind` other than `fallback-recrop` (the page-level reread belongs
to the Perlector, which has not built it), or a recrop on a real submission (the Designator
refuses `--operation recover` there by name, because a recrop's geometry still comes from a
fixture's declared rectangle). Screening the batch first is what keeps an unanswerable
request from leaving half a round behind it, and screening the route here is what stops the
Designator's refusal reaching an operator as a bare `pipeline/2_designator/run.py exited 2`.
`report_undispatchable_recoveries` then names every refused act and request before the
`ContractError` is raised — this module writes no file of its own, so the run's own output
is where it records a dispatch it would not make (principle 2); the durable evidence is the
immutable request artifact and its `recovery-requested` review, which nothing here touches.

The Recensor no longer publishes a real-ingress request at all, so
the route branch is a backstop over a tree written before that gate landed. It is still
checked: a bound nobody checks is not a bound.

**It aborts the run; it does not hold the refused acts and carry on, and that is decided
rather than omitted.** A tree that already carries such a request has no export available
to it by any route, and nothing here changes that: `recovery-requested` maps to no terminal
Armarium category (`common/contracts/outcomes.py`), so the Armarium refuses the act fatally
however this member behaves, and the Recensor holds an act with an outstanding request
without republishing (`pipeline/5_recensor/run.py`), so re-running it supersedes nothing
either. Skipping the refused acts here would move the same dead end one stage later and
lose the named cause at the boundary that knows it. Making the run exportable instead would
mean making `recovery-requested` terminal, which would also let a fixture run whose recovery
was simply never driven deliver as a partial — a half-driven run reported as a finished one.
So this stops, says why, and says plainly that the route out is a fresh run from the Door;
the three refusals an operator can meet (here, the Designator's and the Armarium's) all say
the same thing and none of them names a remedy the code does not provide.

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
`common/stage.py:230-232` aliases it as `RUN_MODES`; `pipeline/0_triage/CONTRACT.md:49-53`
records the same join.

The selections have different lifetimes. Triage persists its member as a batch property;
the driver infers one for an invocation and never writes it to the run tree, as the
byte-identity tests above require. Other contracts use a field named `mode` for unrelated
vocabularies, so the field name alone identifies no selection
(`common/contracts/approval.py:112-138`,
`pipeline/2_designator/geometry_layer.py:570`).
