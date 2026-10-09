# RunPod cheat sheet

Read this before any RunPod work: starting, watching or ending a pod, volumes, prices,
stock, the API. Facts here were verified on the date shown; re-check anything with a
price or stock before acting. Account ids (volume ids, pod ids) live in the lead's private
notes, never here.

## Rules that never move

- **The lead approves every paid thing**: every pod, every card switch, every new paid run.
  A standing approval given in the session ("up to $15 today") covers everything inside it.
  No spend at all unless the session was told to spend.
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
| Create a pod with a network volume or own disk | Connector `create-pod` (REST v2) or the project's pod tool | Always with the guard start command as `args`. `minVcpuCountPerGpu` is only on the connector/REST route. |
| Create a pod with a **global volume** | The project's pod tool, v1 GraphQL route (`objectMounts`) | REST v2, runpodctl and the MCP server cannot attach one. See below. |
| List / inspect / delete pods | Connector `list-pods`, `get-pod`, `delete-pod`; or `runpodctl pod …` | Shutdown is the safe direction and is allowed without a fresh approval. |
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
  in the console on 2026-10-08. Ask for a minimum (`minVcpuCountPerGpu` on REST v2) or read
  the count before deploying; `queue_runner run --dry-run` prints the day's length for the
  pod it runs on.
- Prices seen 2026-10-08 evening, Secure on-demand: A40 $0.59/h, RTX 3090 $0.50/h (community
  stock mostly), RTX 4090 $0.89/h, RTX PRO 4500 $0.72/h, RTX PRO 6000 $2.49/h. Read again
  before renting; the console's deploy page shows the live price and vCPU.
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
the deploy page under Persistent storage, where the mount path is editable.

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
