# The pipeline

One directory per stage. Each stage is a program, `run.py`, that reads the sealed records earlier stages wrote into the run tree and writes its own
into its directory there. The door has no directory of its own: `1_exemplar/door.py`
writes its records into Exemplar's `1_exemplar/` directory. [ARCHITECTURE.md](../ARCHITECTURE.md) says what each stage is
for; each stage's `CONTRACT.md` gives its records and what a consumer may rely on.

`orchestrator/run.py` runs the stages in order, all of them or a contiguous selection
(`pipeline/orchestrator/CONTRACT.md`). Each stage refuses to start until the stage seal
of the stage it requires verifies. The order and the seal each stage requires are
defined once, in `common/contracts/stages.py` (`STAGES`, `HANDOFFS`, `SIDE_BRANCHES`).

## Stage map

| Order | Program | Stage | Reads | Writes | Requires the seal of |
|---|---|---|---|---|---|
| 1 | `1_exemplar/door.py` | door | the submitted files (a fixture, or a real submission folder with its filename ledger), an optional triage decision manifest, the configuration | `run.json` (the run authority) and one `admission` per submitted file, in `1_exemplar/` | — |
| 2 | `1_exemplar/run.py` | Exemplar | the door's admissions | one `page` per admitted page and the corpus `seal` census | door |
| 3 | `1_ink_map/run.py` | Ink map | the sealed pages | one `ink-map` per page | Exemplar |
| 4 | `2_designator/run.py` | Designator | the sealed pages | Surya's `surya-page`, `surya-line`, `surya-block`; the record detector's `detector-page`, `detector-record`, `detector-region` and crops; a provenance record for each detector | Ink map |
| 5 | `3_attestatores/run.py` | Attestatores | the sealed pages; the detector's record crops, for the record reader | one `page-testimonium` per page and witness; for Chandra, `chandra-native-attempt-intent` and `chandra-native-attempt` records of the attempts of its own retry recipe | Designator |
| 6 | `4_perlector/run.py` | Perlector | the sealed pages, the page testimonia, the Designator's records, the Ink Map's records | per page `page-feed`, `page-reading`, `page-accounting` (and `reader-sent` on a live call); per entry `act-region` and `perlectio` | Attestatores |
| 7 | `4b_coniector/run.py` | Coniector | the Perlector's readings | `reconstruction-plan`, `reconstruction-call`, `reconstruction` | Perlector |
| 8 | `5_recensor/run.py` | Recensor | the Perlector's records and everything their page accounting is measured from, the page pixels, operator review decisions | one `review` per unit, one `continuation-link` per page break a reading flags, `review-decisions`, and the partition receipt in `run-health/` | Perlector |
| 9 | `6_archetypus/run.py` | Archetypus | the Perlector's records, the Recensor's reviews and decisions | one `archetypus` per accepted reading, and `index.json` | Recensor |
| 10 | `7_armarium/run.py` | Armarium | the Archetypus's and Coniector's records, the Perlector's `page-reading`, `page-accounting`, `act-region` and `perlectio` records, the Recensor's reviews, the Ink Map's records, the sealed pages | one `manifest-entry` per counted reading and the `export` record with the product bundle | Archetypus, and the Coniector beside it |

Every stage also publishes a `decode-environment` and a `stage-seal` before its final
manifest.

The directory numbers are not the run order. `1_exemplar` and `1_ink_map` share a
number, the door has no directory of its own and writes into `1_exemplar/`, and
`4b_coniector` runs after the Perlector because it reads only the Perlector's readings.

`0_triage/` is not a stage and is never run by the orchestrator. It holds only the
CONTRACT.md of the triage decision manifest, whose schema and validator are
`common/contracts/triage.py`: the optional record of how captured frames were split,
rotated and grouped before intake. The triage producer in
`operations/triage/` writes it and the door reads it.

## Boundaries

Stages share code only through `common/` and the serving, submission and triage seams of
`operations/` (see `common/README.md`), and share data only through the records their
`CONTRACT.md` declares. No stage imports another. `test_stage_import_boundaries.py`
checks the imports it can see in the source; it cannot see a module loaded by a computed
name or through `importlib.util.spec_from_file_location`, nor tell which `run` a bare
`import run` resolves to. The numbered directory names also make a direct statement such as
`import 4_perlector` invalid Python.

The page re-ask budget is sealed from `config/recovery.toml` and spent only by the
Perlector's page re-ask (`common/page_reask.py`).
