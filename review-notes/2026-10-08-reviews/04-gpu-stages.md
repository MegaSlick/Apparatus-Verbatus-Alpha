# Review 4: GPU stage request concurrency (Opus reviewer, read-only, 2026-10-08)

Bottom line: all three GPU stages publish in page order through common/in_order_window.py
and that is sound. Four things keep the card from running wider:
1. Head-of-line blocking in the window (biggest, cheapest).
2. Perlector max_tokens never sized to the page.
3. The cheap witness card reads one page at a time (max_num_seqs = 1 at 24 GB).
4. Chandra is forced to width 1 for a reason the code does not support.
Item I (Coniector window) is correct but saves ~1 min on 6 pages; most of the Coniector's
12.3 min is the weight copy and cold start.

## How requests go out today

- Attestatores: one witness server at a time in sorted chair order
  (3_attestatores/run.py:2516-2580); width = profile.max_num_seqs except Chandra forced
  to 1 (:2573-2578); DAI reads a page's records serially inside one job (:2087-2121).
  Recipes 1/2/4 for 24/48/80+ GB; 24 GB tier batch_size = 1 (pod_placement.toml,
  enforced serving/preflight.py:256). ~20 s per page reading average; ~8 min of the 14
  were likely three cold starts.
- Perlector: in_order_window in three phases (first readings, re-asks, re-reads;
  4_perlector/page_run.py:1271-1305); width = min(--perlector-concurrency,
  row.max_num_seqs) (run.py:220-232); row now 4 / 8192. Feeds built before the chair
  starts (page_run.py:1265); chair starts lazily (:436-437). ~6 min per request; the
  12,288-token run-away took ~10-14 min alone.
- Coniector: plain serial loop (4b_coniector/run.py:404-421); chair starts lazily in
  _ask (:251-252); row max_num_seqs 2 / 4096 (recipes:454-455). ~20 s per call.

nvidia-smi util is kernel time, not batch occupancy; wider windows raise tokens/s and
close gaps but do not show as higher util the same way.

## Findings, ranked

- F1 head-of-line blocking (~5-8 min on 10-07; more per run-away per chunk):
  in_order_window.py:40-43 counts an answered-but-unpublished job as a slot. Keep two
  bounds: in-flight <= width; buffer of answered-unpublished (e.g. 4*width). Wait with
  FIRST_COMPLETED, draw lazily on the main thread (publish_sent order preserved,
  live_calls.py:166-200), publish strictly from the head. Interrupt branch (:54-64)
  already records every arrived reply. Tests: test_live_perlector.py:869 pins the old
  behaviour (becomes "never more than width in flight"); add slow-head test;
  test_attestatores_live_pass.py:1397.
- F2 Perlector max_tokens from reserve (~6-8 min per run-away): request_capacity.py:758
  returns min(cap, room); reserve (:660-689) used only for admission; comment :580-586
  says deliberate. Send min(cap, room, ceil(reserve*headroom)) with optional floor;
  seal headroom in [perlector_generation] (decoding.v8; decoding.py:25,108-150); record
  in answer_reserve (:744-749); same for reask path. A reply at the bound is finish
  length -> truncated -> held unread; nothing repaired. Headroom choice trades held
  pages for time: lead should see it; derive from 10-07 completion_tokens /
  answer_reserve.tokens. Tests: test_page_request_capacity.py, test_page_feed.py,
  test_truncation.py, test_serving_catalogue_capacity.py.
- F3 cheap witness card fully serial (~15-25 min per 50-page chunk, unmeasured):
  recipes:112,201,290 max_num_seqs = 1 at 24 GB; placement batch_size = 1. DAI ~0.47
  GiB KV per seq at 8192, Churro 1.1 GiB (file note line 29); Chandra unchecked. Raise
  to e.g. 4 for DAI and Churro after one cheap-pod KV check. Throughput only.
- F4 Chandra width 1 not required by invariants (~1-3 min on 6 pages; 10-15 per 50 at
  48 GB+): stated reason (run.py:2573-2575) is arrival order of intent/attempt records,
  but publishes are immutable path-addressed files (store.py:458-468, durability.py:
  82-105), manifests sorted (store.py:895), subject ids per page (chandra_native.py:
  98-99). Consequences: up to W orphan intents on a crash (each stops resume until an
  operator decides, chandra_native.py:987-990); batch composition changes retry-trigger
  rate; check whether vendor chandra/model/vllm.py@d4f7467 itself sends concurrently.
  Tests: test_attestatores_live_pass.py (1397 + Chandra resume/orphan tests),
  test_chandra_adapter.py.
- F5 Coniector window, item I (~1 min on 6; 8-12 per 50): split _publish_call into
  draw (main: page_id, prompt, shown_keys, sealed_generations, adopt/refuse :319-342,
  admission), call (worker: send_chat_request only, failures returned as values like
  page_run.py:447-459), finish (main: payload incl. chair.maker() which writes the
  fixture receipt :155-160, publish, read back, derive, publish reconstruction). Start
  the chair on the main thread at the first sending job (today lazy in _ask :251-252).
  Width row.max_num_seqs live else 1; raise reconstructor row to 4 / 8192. Gap: no
  "sent" marker, so an interrupted pass leaves up to W unbound reply blobs; add a sent
  marker + unbound-reply refusal like live_calls.py:166-248, or refuse resume while
  unbound blobs exist. Tests: test_coniector.py (live row max_num_seqs 1 at :623); add
  width-2 order test.
- F6 DAI records serial within a page: flatten (page, record) calls into one window;
  seal the page Testimonium when its last record finishes. Tests: test_detector_records.py.
- F7 width from server capacity: simplest raise the 80+ GB cap (e.g. 8) and let the KV
  pool decide; better a token-budget window (sum of in-flight prompt+reserve within the
  engine's KV token count read at startup; unverified that vLLM 0.30.0 exposes it).
  Width is not sealed; reader-sent records carry concurrency.
- F8 Perlector phase barrier (re-asks wait for all first readings, page_run.py:
  1271-1294) and lazy chair start after feed build (:1265, :436-437): start the chair in
  a background thread while feeds build (up to 375 s overlap).

## Repeated work per request

- Witnesses read+hash the page twice per witness per page (run.py:197; chandra.py:213,
  witness_adapters.py:142). DAI repeats full-page read, sha256 and full decode per
  record (imaging.py:694-712). Perlector reads the page in _prepare (page_run.py:340)
  and publish_act_records (:980), decodes the whole page once per act crop (:987-990).
  Pages are uncompressed PNG (imaging.py:356-364), tens of MB. Fix: per-stage cache of
  verified page bytes keyed by (path, sha256) matching sealed_page_bytes' one-checked-
  read rule (exemplar_boundary.py:143-146); one decode per page shared by crops.
- Every publish re-hashes every input (store.py:1369-1373); input_ref/artifact_ref
  re-read records just written (stage.py:795-802); engine_call_inputs re-hashes reply
  and call record per page (live_calls.py:92-101). Small, main-thread.
- Overlay drawn twice (page_feed.py:760-766, page_path.py:465-466) but off today.
- Not repeated: feeds built once; no manifest re-verify inside loops.

Recommended: F1 first (small, local tests, helps all three stages); F5 on top; bring F2
headroom and F3 24 GB widths to the lead with the 10-07 completion-token ratios.
