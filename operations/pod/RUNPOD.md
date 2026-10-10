# RunPod cheat sheet

Read this before any RunPod work: starting, watching or ending a pod, volumes, prices,
stock, the API. [README.md](README.md) has the full pod reference. Account ids (volume ids,
pod ids) live in the lead's private notes, never here.

## Rules

- **The lead sets a session's permissions at its start.** A session opened with "we are
  doing live pods" may start pods inside the approved limits without asking again; any
  other session asks before anything paid. A standing approval ("up to $15 today", or a
  runbook's decisions table for the day) covers everything inside it.
- **Hourly ceiling:** `max_hourly_usd` in `config/spend.toml` ($3.00). It is a limit, not a
  target: cheapest card that fits, no idle cards, smoke on two pages first.
- **Every pod starts with its guard armed**: the container start command comes from
  `sh operations/pod/pod_start_command.sh <hours|off> <sha>`. Never create a pod without it.
- **Shutdown is verified, never assumed**: `runpodctl pod list --all` is empty,
  `runpodctl pod get <id>` fails, and the console's billing has stopped.
- **Data that never leaves**: real pages and transcriptions go only to the lead's machine
  and the lead's own pods and volumes. Never into git, never to another service.
- **Deleting a volume is the lead's action.** Give the lead the command
  (`runpodctl network-volume delete <id>`, or the console's Storage page).

## Which tool for what

| Need | Use | Notes |
|---|---|---|
| Stock, prices, vCPU per card | RunPod connector (MCP) `get-gpu-type` / `list-gpu-types` with availability, product POD, cloud SECURE | Free reads; stock and prices change by the hour |
| Create a pod | `python -m operations.pod.create_pod` (v1 GraphQL) or the connector `create-pod` (REST v2) | Always with the guard start command and an hours window, never `off`, so a stuck pod ends. Both take a minimum vCPU count (`--min-vcpu` / `minVcpuCountPerGpu`) and a CUDA constraint (`--cuda 13.0` / `allowedCudaVersions`) |
| List, inspect, delete pods | `runpodctl pod list --all`, `pod get`, `pod delete`, or the connector | Deleting is allowed without a fresh approval and verified afterwards. The connector refuses to delete pods it did not create; `runpodctl` does not |
| Network volumes | the connector or `runpodctl network-volume create/list/delete` | Bound to one datacenter; create needs a datacenter id |
| SSH, scp | `ssh -p <port> root@<ip>` from `pod get` | The `ssh.runpod.io` proxy cannot run commands or `scp` |
| Billing | connector `list-billing`, `list-pod-billing` | Settles with a delay; check again later |

Tooling on the lead's Mac: `runpodctl` 2.x (brew tap `runpod/runpodctl`; 1.x has no `pod`
command). The RunPod MCP server runs pinned at 4.0.0 through `~/bin/runpod-mcp`, which reads
the key from `~/.runpod/config.toml` so the key is never in Claude's settings. Do not run
RunPod's `npx @runpod/mcp-server add`: it writes the key into settings. Avoid RunPod's
hosted MCP (`mcp.getrunpod.io`): its create-pod sends `objectMounts: null` to v1.

## Choosing a card and a place

- Read stock first, then pick the datacenter; no region is fixed.
- **vCPU count matters as much as VRAM** for anything with CPU work: the same card type comes
  with very different vCPU counts on different hosts. Ask for a minimum (`--min-vcpu`,
  `minVcpuCountPerGpu`), search across several card types, and read the count after
  creating; `queue_runner run --dry-run` prints the day's length for the pod it runs on.
- **Pin the CUDA version to the image** (`--cuda 13.0` for the cu1300 image): a card's stock
  mixes host CUDA versions, and a container on an older host never starts but still bills.
- Read the live price and vCPU on the console's deploy page before renting. Do not copy
  prices into `config/pod_placement.toml`: its digest is sealed into run trees and serving
  qualification records, so an edit there breaks the acceptance tests.
- Image: `runpod/pytorch:1.4.0-cu1300-torch2130-ubuntu2404` (CUDA 13.0). Blackwell cards need
  it for FlashInfer; `cu1281` images fail vLLM there.

## Storage, and where results go

| Kind | Scope | Good for | Not for |
|---|---|---|---|
| Container disk | dies with the pod | code, venvs, uv cache, model store, caches | anything you want back |
| Own persistent disk (`mounts.persistent`) | dies with the pod, survives stop | the guard's records, caches, model store | results, unless copied home first |
| Network volume | one datacenter | results, weights reused in that datacenter | runs in another datacenter |
| Global volume (beta) | any datacenter | **results only**: the queue's final `sync_to` copy | repo clones, venvs, model store, locks, frequent writes |

Global volumes are object storage: no permission bits, atomic rename or locking, and
eventual consistency. A `git clone` on one restart-loops the pod. Mount it on its own path
and write to it once, at the end. It is billed per stored data plus requests.

### Attaching a global volume

Only the console attaches one: the deploy page's Persistent storage (mount path editable),
or "Configure Pod with volume" on the Storage page. The v1 GraphQL mutation accepts
`objectMounts: [{ objectStoreId, mountPath }]` without error but attaches nothing (the
reply's `objectStores` is empty); `create_pod` exits 3 on that. No API lists global volumes;
the id is on the console's Storage page. So the lead deploys the pod with the volume from the
console and the session copies results into it over SSH. CPU pods cannot mount one, so fetch
results over SSH from the pod's own disk first and treat the global copy as the safety net.

**Models download on boot.** Every bake-off arm fetches its pinned weights in its `prepare`
step, so a volume's only job is keeping results safe when the pod deletes itself.

## Traps

- A pod with only its own disk and `--no-hold` (or `end_pod = "delete"` without a verified
  copy) deletes its results with itself.
- Do not export `HF_HUB_OFFLINE=1` before starting a queue: preparation steps download, and
  the queue makes each arm's command offline itself.
- `runpodctl pod list` without `--all` hides stopped pods, which still bill for disk.
- RunPod REST v1 retires on 2026-11-15; the project's default is v2.
- RunPod's Cloudflare refuses urllib's default User-Agent (error 1010, HTTP 403); the
  project's urllib transport (`provider_runpod.py`) sends `verbatus-pod/1.0`.
  `pod_guard.sh` and `pod_delete.sh` use curl.
- Verify a queue's results at home (`queue_runner fetch` checks every digest), not on the
  pod: hashing tens of thousands of small files on a network disk is very slow.
- Over SSH, `pkill -f <pattern>` also kills the SSH shell whose command line holds the
  pattern; kill by pid.
- A short commit hash in the guard start command arms the guard and then fails the
  bootstrap: use all 40 characters.
- The guard's idle ladder only warns (`ladder_delete` is off), and reaches the phone only if
  `/workspace/private/.pod_guard/ntfy_topic` was written. The hours window given at creation
  is the real backstop.
- A create that fails after its request was sent (a transport timeout) may still have made
  the pod: run `runpodctl pod list --all` before any retry.
- A Claude Code session has no terminal to type into and each command is a fresh shell:
  secrets go in over stdin inside the same command, and anything long (`watch`, a queue)
  runs in the background.
