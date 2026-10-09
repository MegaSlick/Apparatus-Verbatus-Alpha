# RunPod cheat sheet

Read this before any RunPod work: starting, watching or ending a pod, volumes, prices,
stock, the API. Facts here were verified on the date shown; re-check anything with a
price or stock before acting. Account ids (volume ids, pod ids) live in the lead's private
notes, never here.

## Rules that never move

- **The lead sets a session's permissions at its start, and only the lead changes them.**
  A session the lead opens with "we are doing live pods" may start pods inside the
  approved limits without asking again; any other session asks before anything paid.
  A standing approval covers everything inside it, whether given in the session ("up to
  $15 today") or written by the lead into a runbook's decisions table for that day.
- **Hourly ceiling $3.00** (`config/spend.toml`, lead 2026-10-09): approved, not a target.
  Do not be wasteful: cheapest card that fits, no idle cards, smoke on two pages first.
- **Every pod starts with its guard armed**: the container start command comes from
  `sh operations/pod/pod_start_command.sh <hours|off> <sha>`. Never create a pod without it.
- **Shutdown is verified, never assumed**: `runpodctl pod list --all` empty, `pod get <id>`
  fails, and the console's billing has stopped.
- **Data that never leaves**: real pages and transcriptions go only to this Mac and the
  lead's own pods and volumes. Never into git, never to another service.
- **Deleting a volume is the lead's click.** Sessions that carry the desktop app's browser or
  computer-use tools refuse to delete data even when asked; do not argue, hand the lead the
  one-line command: `runpodctl network-volume delete <id>` (or the console's Storage page).

## Which tool for what (2026-10-08)

| Need | Use | Notes |
|---|---|---|
| Stock, prices, vCPU per card | RunPod connector (MCP) `get-gpu-type` / `list-gpu-types` with availability, product POD, cloud SECURE | Free reads. Stock and prices move by the hour. |
| Create a pod | `python -m operations.pod.create_pod` (v1 GraphQL; the only route that takes a global volume) or the connector `create-pod` (REST v2) | Always with the guard start command. Both routes take a minimum vCPU count (`--min-vcpu` / `minVcpuCountPerGpu`) and a CUDA constraint (`--cuda 13.0` / `allowedCudaVersions`). Always with an hours window, never `off`, so a stuck pod ends. |
| List / inspect / delete pods | `runpodctl pod list --all`, `pod get`, `pod delete`; or the connector | Shutdown is the safe direction: allowed without a fresh approval, and verified afterwards (listing empty, billing stopped). The connector refuses to delete pods it did not create; `runpodctl` does not. |
| Network volumes | Connector or `runpodctl network-volume create/list/delete` | Datacenter-bound; create needs a datacenter id. |
| SSH / scp to a pod | `ssh -p <port> root@<ip>` from `pod get` | The `ssh.runpod.io` proxy cannot run commands or `scp`. |
| Billing | Connector `list-billing`, `list-pod-billing` | Settles with a delay; check again later. |

Tooling on this Mac: `runpodctl` 2.14 (brew tap `runpod/runpodctl`; v1.x has no `pod`
command). The RunPod MCP server runs pinned at 4.0.0 through `~/bin/runpod-mcp`, which reads
the key from `~/.runpod/config.toml` so the key is never in Claude's settings. Do not run
RunPod's `npx @runpod/mcp-server add`: same server, key written into settings. RunPod's
*hosted* MCP (`mcp.getrunpod.io`) had a create-pod bug in 2026-08 (sends `objectMounts: null`
to v1; runpod/runpod-plugins-official#56): avoid it.

## Choosing a card and a place

- Read stock first, then pick the datacenter: no region is fixed. The old EU-RO-1 volume is
  gone (2026-10-08).
- **vCPU count matters as much as VRAM** for anything with CPU arms: a 4090 came with 8 vCPU
  in the console on 2026-10-08. Ask for a minimum (`--min-vcpu` on the pod tool,
  `minVcpuCountPerGpu` on REST v2; both validated 2026-10-08) and read the count after
  creating; `queue_runner run --dry-run` prints the day's length for the pod it runs on.
- **Pin the CUDA version to the image** (`--cuda 13.0` for the cu1300 image): a card's stock
  mixes host CUDA versions, and a container on an older host never starts but bills.
- Prices seen 2026-10-08 evening, Secure on-demand: A40 $0.59/h, RTX 3090 $0.50/h (community
  stock mostly), RTX 4090 $0.89/h, RTX PRO 4500 $0.72/h, RTX PRO 6000 $2.49/h. Read again
  before renting; the console's deploy page shows the live price and vCPU. Do not copy
  prices into `config/pod_placement.toml`: its digest is sealed into run trees and serving
  qualification records, so an edit there breaks the acceptance tests.
- Images: `runpod/pytorch:1.4.0-cu1300-torch2130-ubuntu2404` (CUDA 13.0; Blackwell cards need
  it for FlashInfer). `cu1281` images fail vLLM on Blackwell.

## Storage, and where results go

| Kind | Scope | Good for | Not for |
|---|---|---|---|
| Container disk | dies with the pod | code, venvs, uv cache, model store, caches | anything you want back |
| Own persistent disk (`mounts.persistent`) | dies with the pod, survives stop | the guard's records, caches, model store | results, unless copied home first |
| Network volume | one datacenter | results, weights to reuse in that datacenter | runs in another datacenter |
| **Global volume** (beta) | any datacenter | **results only**: the queue's final `sync_to` copy | repo clones, venvs, model store, locks, frequent writes |

Global volumes are object storage: no permission bits, no atomic rename, no locking,
eventual consistency. A `git clone` on one restart-loops the pod. Mount it on its own path
and write to it once, at the end. Billing is per stored data plus requests; an empty one
costs nothing.

**Attaching a global volume (verified 2026-10-08 against the live schema, not yet with a
real deploy):** only the v1 GraphQL mutation takes it:

```
podFindAndDeployOnDemand(input: { …, objectMounts: [{ objectStoreId: "<global volume id>", mountPath: "/workspace/global" }] })
```

A running pod reports it as `objectStores { objectStoreId mountPath }`. There is no API to
list global volumes; the id is on the console's Storage page. The console attaches it on
the deploy page under Persistent storage, where the mount path is editable. CPU pods
cannot mount one (GPU pods only), so fetching from a global volume later needs a GPU pod:
fetch results over SSH from the pod's own disk first, and treat the global copy as the
safety net.

**Models download on boot.** Every bake-off arm fetches its own pinned weights in its
`prepare` step (datacenter speed: minutes). Nothing has to be on a volume beforehand, so a
volume's only job is keeping results safe when the pod deletes itself.

## Traps seen so far

- A pod with only its own disk and `--no-hold` (or `end_pod = "delete"` without a verified
  copy) deletes its results with itself: lost once on 2026-10-07.
- Do not export `HF_HUB_OFFLINE=1` before starting a queue: preparation steps download; the
  queue makes each arm's command offline itself.
- `runpodctl pod list` without `--all` hides stopped pods, which still bill for disk.
- RunPod REST v1 retires on 2026-11-15; the project's default is v2. The global-volume route
  is the only reason to touch v1 GraphQL.
- A short commit hash in the guard start command arms the guard and then fails the bootstrap:
  use the full 40 characters.
- The guard's idle ladder only warns (`ladder_delete` is off in `config/spend.toml`), and
  only reaches the phone if `/workspace/private/.pod_guard/ntfy_topic` was written. The hours
  window given at creation is the real backstop.
- A create that fails after its request was sent (a transport timeout) may still have made
  the pod: `runpodctl pod list --all` before any retry.
- A Claude Code session has no terminal to type into and each command is a fresh shell:
  secrets go in over stdin inside the same command, and anything long (`watch`, a queue)
  runs in the background.
