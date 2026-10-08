# Review 3: CPU stages and the run tree (Opus reviewer, measured, 2026-10-08)

Outcome: #277 made the Recensor linear, but the largest CPU cost after the Perlector is
re-deriving every witness's presented image from the full page: 9 times per page per run
(Perlector 1, Coniector 1, Recensor 3, Archetypus 2, Armarium 2), each a full decode.
Also each act's crop is checked 7 times, the page render re-derived in 4 stages, and the
Designator decodes the full page twice per record it cuts.

Measured on a scratch copy with 3500x5000 pages (fake models, cProfile per stage), 2
pages: Coniector 17.9 s (17.3 in the denominator before any call, card waiting),
Recensor 41.6 s (current_page_testimonia 3 calls = 28.7 s), Archetypus 27.9 s, Armarium
30.9 s, Ink map 4.4 s, Designator sans Surya 4.5 s. At 200x260 the whole run took 15 s.
Scaled to 50 pages on one core: ~49 min after the Perlector, ~36 min of it witness
re-derivation. Primitives on an 18 MB page: sha256 0.05 s; dimensions() 0.64 s (full
decode); decode 0.37 s; crop_png 0.45 s vs 0.04 s from a decoded image; grayscale_rows
0.86 s; ink_map_page 1.45 s; residual_ink 1.26 s; _downscale_page 4.3 s; whole-page
crop_png 1.06 s writing a 52 MB uncompressed PNG.
#281 Surya: runner_processes = min(workers=6, cpus//8, pages) -> 4 processes on 32 vCPU,
2 on 16; capped by config not cores; sched_getaffinity ignores a cgroup CPU quota.
#283 sync: incremental within one orchestrator run; pod_run's final sync and
_hydrate_local_run build a fresh RunTreeSync, re-hash every local file and read every
volume file back.

## Per-stage loops (all single process, one core, except Surya runners)

- Door (door.py ~897 serial; _iter_admissions 1146 builds the DOOR manifest ~7 times,
  linear). Exemplar 0.3 s/page; C7 gains nothing. Ink map (run.py 56-101 manifest +
  read_artifact + verify_sealed_page_pixels hash the page twice; loop 159-228 hashes a
  third time then grayscale_rows and ink_map_page, pure-Python row loops) 2.2 s/page.
- Designator: _run_surya (surya_detection.py:177) dimensions() full decode per page;
  publish loop (314-362) re-hashes the page image per line/block record (store.py:466);
  detector loop (404-526): YOLO 1 torch thread, pages serial (detector.py:258-260);
  per record _cut_detector_region (365) dimensions + crop_png = 2 full decodes, 2-3
  input_ref hashes (377, 482-489). ~80-100 small files per real page.
- Recensor: plan_reviews twice (page_review.py 895 and 1053); each calls
  current_page_testimonia, page_coverage_findings (run.py:133: verify pixels,
  sealed_page_bytes, grayscale_rows, residual_ink), reading_regions_by_page (208,
  re-hashes the page per act), _accounting/_assessment per act; the denominator
  (stage.py:_PageReadRecords 2192) calls current_page_testimonia a third time (2243);
  write_reading_receipt rebuilds three manifests with input checks (1045, 1066).
- Archetypus: reading_acts denominator (testimonia + feed + page render stage.py:3270 +
  crop lineage 3458), current_page_testimonia again (666), per act
  verify_reading_region_lineage (449) -> dimensions (exemplar_boundary.py:501) + crop_png
  (525): two full decodes + two hashes per act.
- Armarium: denominator + testimonia once more; verify_reading_region_lineage per act at
  823 and 946 (3 per act in all); _cached_manifest (547) already once per run; _export
  29.6 of 30.9 s.
- Store: build_manifest (837) openat chain per path component (1096), inputs hashed once
  per build (#282, 860-878); write_manifest full rebuild; 3-4 builds per stage (linear).
  read_artifact_snapshot (762) / read_artifact_reference (796) re-hash all inputs on
  every read; input_ref (stage.py:789) re-reads the whole file.

## Findings (minutes per 50-page chunk, one core)

1. Witness presented images re-derived 9x per page (~36 min, ~16 removable without
   changing checks): page_testimonia.py:287 current_page_testimonia ->
   validate_presented_page (62) -> native_witness.py:534
   validate_presented_page_binding; adapter crop_png (583) full decode,
   resize_png_lanczos, _replay_colour_mode. (a) cache per process keyed by root, run id
   and the Attestatores manifest snapshot (same argument as _PASS_MANIFESTS,
   stage.py:207: Attestatores is sealed before these stages open); (b) decode each page
   once per pass, memo derived sha256 per (page sha256, transform digest); (c) validate
   per page in a ProcessPoolExecutor, raise the first refusal in page order. Tests:
   common/test_page_testimonia.py, 3_attestatores/test_page_witness_roster.py,
   test_native_testimonium_contract.py, 5_recensor/test_page_review.py,
   test_partition_receipt.py, 6_archetypus/test_page_establish.py,
   7_armarium/test_page_export.py, orchestrator/test_orchestrator_acceptance.py.
2. Full-page decode per crop and size check (~10-15 min): imaging.dimensions (811)
   decodes to report size; crop_png -> _crop_decoded_page (942) decodes per crop;
   callers exemplar_boundary.py:501, 525, designator run.py:370, 407,
   surya_detection.py:186, 317, page_render._downscale_page (51-52, 4.3 s per page per
   stage, 4 stages). verify_reading_region_lineage 7x per act. Per-process LRU(2) of
   decoded pages keyed by sha256; crop_from_decoded sharing _encode_crop_deterministic;
   dimensions from the decoded image; keep the decode-failure refusal with one full
   load() per page. Byte-identical. Tests: test_imaging_determinism.py,
   test_imaging_bounds.py, test_imaging_review_regressions.py, test_page_render.py,
   test_exemplar_boundary.py, 2_designator/test_secondary_proposer.py,
   test_geometry_layer.py.
3. Coniector denominator before any call (~7.5 min GPU-pod time): same root cause;
   plus item I.
4. Recensor plans twice (~2-3 min): compute findings and testimonia once per context
   and pass to both passes; the receipt still re-measures reviews on disk.
5. Designator non-Surya (~3-5 min): covered by 2 plus publish_artifact re-hashing the
   page per Surya line/block (~3 s/page); a per-StageContext verified-input set keyed by
   (path, sha256, st_ino, st_size, st_mtime_ns) used by publish (relaxes re-check at
   every use mid-stage; the seal still re-checks every input, stage.py:977-981: say so
   in the PR). YOLO: process pool of single-thread workers each loading the model once
   (same bytes); overlap Surya subprocesses with detection.
6. C7 Ink map process pool (~1.7 min on 32 cores): workers compute (ordinal, measured |
   refusal) from path + sealed digest, each reading and hashing its own bytes; parent
   publishes in page order with first-failure semantics; workers = cpu_count capped by
   pages and memory (~100+ MB per decoded page as row lists). Exemplar no gain; Door
   likely gain on the real route. Tests: 1_ink_map/test_ink_map_stage.py,
   test_ink_calibration.py, 1_exemplar tests.
7. Hard-failure checkpoint after every stage (~1-2 min): orchestrator run.py:1040 ->
   tally_hard_failures rebuilds five manifests with input checks; verify_inputs=False
   for already-sealed stages is an integrity choice; flag it.

## #281 Surya sizing

surya_detector.py:524-528 runner_processes = max(1, min(workers, cpus // threads,
pages)); real rows threads = 8, workers = 6 (recipes 513-553). Drop or default the
workers cap to cpus // threads; read /sys/fs/cgroup/cpu.max; measure threads 4 vs 8 on
a pod before changing threads (sealed into run facts and the receipt endpoint
subprocess://cpu/threads-N; workers is not sealed, so changing it keeps every byte).
Static contiguous slices (page_slices 508). Tests: test_surya_detector.py
(test_runner_processes_are_bounded_by_workers_cpus_and_pages,
test_several_processes_give_the_bytes_one_process_gives).

## Run tree sync

Fixture 2 pages: 141 files, 80 MB (mostly Attestatores presented images 18.7 + 13.1 MB
per page). Real: ~120-150 files per page -> ~6-7k files, 1.5+ GB per 50-page chunk.
Within one orchestrator run only new files are hashed (sync.py:76-79) but each new file:
hash source (83), temp write + fsync (101), hard link (103), fsync directory per file
(104), read target back over the volume to hash (105), hash source again (112-114);
_directory() stats every ancestor each sync (24-36, 63); files copied one at a time.
pod_run final sync (pod_run.py:2166) and _hydrate_local_run (1087) build a new
RunTreeSync: everything re-hashed and every target read back. Make it incremental:
persist the ledger to <local>/.verbatus-sync-ledger.json (prefix already skipped at 69)
with (path, size, mtime_ns, sha256) written after each verified file; copy in a
ThreadPoolExecutor (16-32 threads); fsync each directory once after its files are
linked; cache created directories; optionally take source digests from stage seals.
Tests: operations/pod/test_run_tree_sync.py, test_pod_run.py,
orchestrator/test_volume_hosted_run_tree.py.

Scratch tooling (not in the repo): scratchpad/tools/{profwrap,bigfix,bench}.py and
profiles under scratchpad/profbig and scratchpad/prof.
