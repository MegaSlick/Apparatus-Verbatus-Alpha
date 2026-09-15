# First live RunPod deployment trial

The first trial exercised GitHub commit
`3a7dd80df3637aeddd1867935af080ffbde311f4` on 2026-09-15. It reached model
materialization and stopped before inference. It produced no witness or Perlector
outputs and establishes no transcription-quality result.

## Environment and observed results

- RunPod Secure Cloud, EU-RO-1, one RTX PRO 6000 Blackwell Server Edition with
  97,887 MiB reported GPU memory; quoted GPU price USD 2.09/hour.
- A 300 GB standard network volume mounted at `/workspace/private` retained
  the source images, downloaded models and evidence after pod deletion.
- The official image was pinned to
  `runpod/pytorch@sha256:0a360022e8de4375af99430f84e8b38951acc397252163a37ceac7204d01be35`.
  It contained uv 0.9.0. Installing the repository-required uv 0.12.1 allowed
  `uv sync --locked --group pod` to finish.
- The resulting environment reported torch 2.13.0+cu130, vLLM 0.27.1,
  transformers 5.14.1 and NVIDIA driver 595.91.07. A small bf16 GPU matrix
  multiplication succeeded. Model serving was not tested by that probe.
- All four pinned real model snapshots materialized. Their canonical manifests
  were fetched independently through S3 and their hashes checked locally; the
  [manifest-pin record](2026-09-15_runpod-model-manifest-pins.md) identifies them.
- Two French RecordGold validation images were uploaded, downloaded and checked
  by SHA-256, then checked through the pod's mounted filesystem. Both images and
  the model-store record were fetched again after deleting the pod.

The pod existed for about 18 minutes. Provider deletion returned HTTP 204;
subsequent exact-pod GET returned 404 and an independent pod listing showed it
absent. Account current spend then fell to USD 0.029/hour, the retained volume.
The account balance decreased by about USD 0.65 across this interval. The
pod-specific billing query had no detailed entry yet; detailed charges were not
available from that query, and its empty response does not establish zero cost.

## Failures the trial exposed

1. The image contract resolved `.venv/bin/python` through its normal uv symlink
   to the system binary and rejected a valid virtual environment. Copying the
   interpreter into the venv was a temporary runtime workaround, with no pipeline
   source edit. The source correction validates the invoked launcher and active
   virtual-environment prefix.
2. After downloading the models, CHAIR_CACHE refused the all-zero manifest
   fingerprint in the real roster. The measured metadata now supplies the pins;
   this does not mark any serving profile proven.
3. Normal serving correctly refuses unproven profiles, but the shipped preflight
   had no first-time qualification route. A GPU must be able to measure a profile
   before a reviewed configuration can claim that measurement occurred.
4. A subsequent GPU-free test of the repository upload command transferred an
   image but failed verification. RunPod returned empty custom metadata from
   both HeadObject and GetObject despite the uploader supplying a checksum tag.
   Independent GET hashing matched the source. Verification must work from the
   returned bytes rather than require that custom metadata survive.
5. The operator uploader's image prefix and missing uploaded submission manifest
   did not supply the layout Boot B expects. A successful image PUT alone does
   not establish a ready pipeline submission.

The repository's local ingest/triage command completed for the same two images,
producing a sealed submission, review proxies and triage records with no grouping
candidates. A nested macOS Seatbelt launch failed inside the coding-tool sandbox;
running the same confined command outside that outer sandbox succeeded.

## Limits and retained evidence

Provisioning used temporary operator glue, a fresh authenticated GitHub clone and
independent host/pod shutdown timers. This was not a demonstration of a shipped
one-command launcher or RunPod template. Pipeline source came from the stated
GitHub commit; local ground-truth text was not supplied to inference.

Raw logs, manifests, upload receipts and provider observations are retained in the
session's private workbench. The evidence archive fetched through S3 has SHA-256
`7465a36865fe64281dd570c4c7ccc8cb9e959b2b137ef1612194433b16dff835`.
The network filesystem's `df` output reported cluster-wide capacity, not the
purchased 300 GB allowance; capacity planning used the purchased amount.

The next trial must test actual model qualification and native pipeline outputs.
Smaller-card compatibility, interrupted inference recovery, bulk throughput and
quality scores remain unmeasured. Compute was released while preparing the fixes.
