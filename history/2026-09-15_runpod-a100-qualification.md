# RunPod A100 qualification checkpoint — 2026-09-15

This records the live investigation through 18:48 UTC. It is evidence and a follow-up
list, not a declaration that the pipeline is proven. At this checkpoint the full
RecordGold pipeline has not run and no model-quality score exists.

## Observed behavior

The first published deployment candidate was `434d7cf45e7f76ab135d657a7849205049eaccae`
on draft [PR 118](https://github.com/MegaSlick/Apparatus-Verbatus-Alpha/pull/118).
After advertised RTX PRO 6000 availability failed at allocation, an A100 80 GB was
allocated in EU-RO-1 at $1.59/hour. Its 300 GB standard network volume retained all
four model snapshots. Resizing ephemeral disk from 60 to 200 GB reset the container;
the same volume survived, and the exact GitHub checkout and frozen environment were
reconstructed. No source-model download was repeated after that reset.

The two French validation full-page IIIF downloads matched the selected local RecordGold
source hashes. Confined ingestion completed after upgrading the host's `setpriv`:
two sources, zero grouping candidates, zero confirmed clusters, ledger digest
`3a326b1de4fad589a7ee09ea6885193e9d077ccf41173e5c9c38937f3033ff93`.
These are full-frame spreads; this run does not establish spread-splitting support.
Ground truth remains separate from inference. The DAI model was trained on RecordGold,
so eventual scores are regression evidence rather than independent generalization.

The first complete serving preflight was red:

- Churro and Designator passed actual golden-page witness reads.
- Earlier Chandra and DAI startup attempts failed because system Ninja was missing.
  Ninja was installed before the later successful Designator launch.
- Base Perlector loaded approximately 51.1 GiB of weights and served, but its response
  failed the exact smoke format. The prior raw response was discarded after hashing;
  default Qwen thinking behavior is a hypothesis, not a confirmed diagnosis.

The repair batch adds a pinned runtime preparation command for uv and Landlock-capable
setpriv plus system Ninja, selects direct-response mode in Perlector smoke and real
requests, and retains exact smoke request/raw response bytes before parsing. Exact
page-witness validation remains unchanged. The live retry will determine the outcome.

## Validation status

The local full gate was interrupted to avoid degrading the user's computer. A complete
check set on pod CPUs reached 9,630 passing tests, 12 skips and two expected failures;
three failures remained, and surviving workers stalled after one worker was killed
for exceeding the 92 GB host memory limit during overlapping model initialization.
The session interrupted pytest to recover its report. This is not a green gate.

The failures were a mode-bit permission test running as root, a security-test namespace
missing newly added launch arguments, and the HEIC admission worker killed under memory
pressure. At 18:51 UTC the focused run reproduced that memory kill without models or other
test workers running (exit -9, second cgroup OOM kill), so concurrent model loading is
not sufficient to explain it. The Linux HEIC test path is under separate investigation. The batch corrects the first two test assumptions. The three focused serving,
bootstrap and Perlector suites then passed on pod CPU. Final full-gate and GitHub review
results belong to the eventual exact merge candidate; this checkpoint is not merge-ready.

## Follow-up list

| Finding | Recorded disposition |
|---|---|
| Full-roster launch coupling | Add independent stage/model execution and retained-output resume in a future session. |
| Repeated model verification I/O | Approximately 2S+6R = 786,225,823,784 logical model-file bytes before loading; reduce redundant passes while preserving integrity. This is not a physical-network measurement. |
| Disk planning | Four snapshots occupy about 90.3 GB; five role caches about 100.9 GB because Chandra is used twice. Plan local scratch separately from durable storage. |
| Runtime preparation/template | This batch ships preparation instructions; a reusable RunPod template and complete one-command launch remain follow-ups. |
| Region and GPU availability | Network volumes constrain placement; advertised availability did not guarantee allocation. Other GPU families/tier combinations remain unmeasured. |
| Spread preparation | Public ingestion currently provides no supported geometry input for real two-page splitting; the scantailor path uses fixture XML. |
| Exact stage HTTP request retention | Stage outputs and presented pixels are retained, but exact original request bytes are currently hashed rather than stored. Smoke retention is corrected in this batch. |
| Export portability | The configured export ZIP references pixels. Retain the complete run tree, including crops, not just the ZIP. |
| Upload races and scoring integrity | Integrated fixes bind immutable upload ownership and presented-page identity and remove placeholder geometry text scores. S3 409 retry remains a fail-closed usability follow-up. |
| CLI polish | Relative cache-path handling, direct-invoke path checks, and ingestion wording need follow-up; this controlled run uses absolute volume-confined paths. |
| Validation cost | Local history scans took about 31 minutes and were repeated by push hooks. Reuse must bind exact commits/scanner/policy, never silently omit coverage. Keep memory-heavy tests separate from model initialization. |

The live Landlock failure confirms the existing
[F002 finding](2026-09-14_prelaunch-review-findings.md#f002-high-five-verbs-need-a-very-recent-undocumented-util-linuxsetpriv-feature).

## Retained evidence

The network volume holds `qualification-evidence/`, `qualification-ingest-ready/`,
`trial-source-public/pages/` and the complete `preflight/` tree. Local evidence is under
`workbench/raw/runpod-qualification-2026-09-15/` (gitignored). Verified archive hashes:

- Before resize: `19367b5613bb8fa1467406efce0519dd0cba3ea6f3248a44a265005429f1a6f9`.
- First full preflight: `c1558c1b141eab4c9ff8148026d8231798c7e5dbe534864c70d033a5a9583466`.

The user removed the elapsed-time budget during this investigation. The session disabled
the obsolete host shutdown watcher and suspended only the pod timer process, preserving
its child services. Shutdown requires completion, approaching the agreed 10% usage reserve,
or a new user decision. The next operational deadline is a process-liveness parameter;
it does not reinstate permission for automatic provider deletion.
