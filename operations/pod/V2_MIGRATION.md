# RunPod REST v1 → v2: the migration, recorded page by page before any code

Tyrel ruled on 2026-08-11 (`workbench/standing/TYREL_RULINGS_2026-08-10_SPEND.md`):
*"V2 should be what we use."* `provider_runpod.py` still speaks v1, and RunPod's
own documentation banner now retires v1 on **2026-11-15**. This file is the
record the ruling and the README's v1/v2 paragraph asked for: every endpoint,
field, status code and lifecycle word the adapter uses today, mapped to its v2
counterpart with the page that says so, and the ones with no counterpart named.
**No v2 code exists in this tree.** The plan at the end is what the next unit
executes; this unit read documentation only and called nothing.

Every page below was read online on **2026-09-02**. A citation is the page's
URL under `https://docs.runpod.io/` unless another host is named. Where a page
was summarized rather than quoted, the entry says so. Nothing here is an
observed response: it is what the vendor publishes, and the first authorised
live run is what confirms the provider answers that way (deferral 04-6).

## 1. The two routes, and the one that stays GraphQL

| Fact | v1 (the adapter today) | v2 | Source |
|---|---|---|---|
| Base URL | `https://rest.runpod.io/v1` | `https://api.runpod.io/v2` | `api-reference-v2/migrate-from-v1` |
| Credential | `Authorization: Bearer <key>` | unchanged: "Continue using `Authorization: Bearer RUNPOD_API_KEY` header" | `api-reference-v2/migrate-from-v1` |
| Status of the route | "REST API v1 is deprecated and will be retired on November 15, 2026. Migrate your integrations to REST API v2 before that date." | `info.version` `2.0.0`, description "Runpod public REST API — v2", **no beta wording** in the schema; the v2 overview page carries no beta, GA or maintenance wording either | `api-reference/overview`; `https://api.runpod.io/v2/openapi.json`; `api-reference-v2/overview` |
| Schema | none published as a file in the pages read | `https://api.runpod.io/v2/openapi.json` | `api-reference-v2/overview` |
| Path parameter | `{podId}` | `{id}` on every resource | `api-reference-v2/migrate-from-v1` |
| Error body | `{"message": "..."}` | RFC 9457: required `title`, `status`, `detail`, optional `errors[]`; e.g. `{"title":"Not Found","status":404,"detail":"The requested Pod does not exist."}` | `api-reference-v2/migrate-from-v1` |
| Account balance | none | **none** — `GET /v2/billing` and `GET /v2/billing/pods` are historical spend; neither reports a balance, credits, or current spend per hour | `api-reference-v2/billing/get-aggregated-billing-history`, `.../get-pod-billing-history` |
| GraphQL (`myself { clientBalance currentSpendPerHr }`) | the balance observer this unit built | "The GraphQL API is deprecated and will be retired in early 2027. For new integrations, use REST API v2." | `sdks/graphql/configurations` |

So the version move does **not** touch the balance observer, and the observer
itself now has a sunset: by early 2027 either v2 publishes a balance or the
floor gate loses its live source. That is a standing question for the vendor,
recorded here so it is not rediscovered in 2027.

## 2. Every v1 call the adapter makes, mapped

Grepped from `provider_runpod.py` as it stands: six endpoints, twelve request
fields, fourteen response fields, four billing fields, three lifecycle words,
and the status codes each verb accepts.

### 2.1 Endpoints

| Verb | v1 call | v2 call | Source |
|---|---|---|---|
| `create` | `POST /pods` → `200` or `201` accepted | `POST /v2/pods` → **`201 Created`** only. "Provisioning is asynchronous: the pod starts in `PROVISIONING`, transitions through `STARTING`, and reaches `RUNNING` once its container is healthy. Poll `getPod` (or watch the pod's `status`) to observe readiness rather than assuming the pod is running when this call returns." | `api-reference-v2/pods/create-a-pod` |
| `create` (recovery) / `verify_absent` | `GET /pods?includeMachine=true&includeNetworkVolume=true` → bare array | `GET /v2/pods` → envelope `{"pods": [...]}`; only query parameter `includeClusterPods` (default false); **no pagination parameters documented**; `env` is returned per pod | `api-reference-v2/pods/list-pods`; `api-reference-v2/migrate-from-v1` ("v1 returned bare arrays; v2 wraps them") |
| `adopt` | `GET /pods/{podId}?includeMachine=true&includeNetworkVolume=true` → `200`/`404` | `GET /v2/pods/{id}` — **no query parameters**; gpu, mounts and env are always in the body | `api-reference-v2/pods/get-a-pod` |
| `status` | `GET /pods/{podId}` → `200`/`404` | `GET /v2/pods/{id}` → `200`, `401`, `403`, `404`, `429`; 404 body `{"title":"Not Found","status":404,"detail":"pod not found"}` | `api-reference-v2/pods/get-a-pod` |
| `terminate` | `DELETE /pods/{podId}` → `204` documented; adapter also tolerates `200`, `202`, `404` | `DELETE /v2/pods/{id}` → `204` "Deleted. Response has no body."; `401`, `403`, `404`, **`409`** ("pod belongs to cluster; cannot terminate via pod endpoints"), `429`. Equivalent: `POST /v2/pods/{id}/action` with `{"action":"terminate"}` → `204`. Network volumes are "only detached — the volume itself is not deleted"; host-local storage is "destroyed with it". No idempotency wording. | `api-reference-v2/pods/terminate-a-pod`; `api-reference-v2/pods/trigger-a-pod-state-transition` |
| `capture_cost` | `GET /billing/pods?podId&startTime&endTime&bucketSize=hour&grouping=podId` → bare array | `GET /v2/billing/pods?podId&startTime&endTime&bucketSize` → envelope (§2.4); **`grouping` has no v2 counterpart** (records carry `podId` when filtered); `lastN` is an alternative to the window, mutually exclusive with it; `bucketSize` still accepts `hour` (default `day`) | `api-reference-v2/billing/get-pod-billing-history`; `api-reference-v2/migrate-from-v1` |

### 2.2 The create body (`_create_payload`)

The v2 body nests what v1 flattened, and two fields the runtime treats as
load-bearing have **no v2 counterpart at all**. That finding is the reason the
README refused to migrate on an assumption, and it is now a documented fact
rather than a caution.

| v1 field the adapter sends | v2 | Source |
|---|---|---|
| `name` | `name` (required, min length 1) | `api-reference-v2/pods/create-a-pod` |
| `imageName` | `image` (required unless `templateId`) | same |
| `gpuTypeIds: [one id]` | `gpu.id` (string, e.g. `"NVIDIA GeForce RTX 4090"`) | same |
| `gpuCount: 1` | `gpu.count` (default 1) | same |
| `cloudType: "SECURE"` | `cloud: "SECURE"` (default; `COMMUNITY` the other value) | same |
| `computeType: "GPU"` | none; "Exactly one of `gpu` or `cpu` must be set" carries the meaning | same |
| `networkVolumeId` + `volumeMountPath` | `mounts.network[0].volumeId` + `mounts.network[0].path` ("max 1 item currently") | same |
| `env` | `env` | same |
| `templateId` | `templateId` ("body fields override template") | same |
| **`interruptible: false`** | **none.** The create input's full property list is `name, cloud, cpu, dataCenterIds, globalNetworking, gpu, mounts, startJupyter, startSsh, templateId, image, args, disk, env, ports, registry`; `interruptible`, `spot` and `bidPerGpu` do not appear, and the Pod response carries no `interruptible` either | `https://api.runpod.io/v2/openapi.json` |
| **`dockerStartCmd: [argv]`** | **none.** No `command`, `startCommand`, `dockerStartCmd`, `dockerEntrypoint` or `entrypoint`; the only related field is `args` (string): "Arguments passed to container entrypoint" | `https://api.runpod.io/v2/openapi.json`; `api-reference-v2/pods/create-a-pod` |

Fields v1 accepted that the adapter never sent (`containerDiskInGb`,
`volumeInGb`, `ports`, `dataCenterIds`, `dockerEntrypoint`) map to `disk`,
`mounts.persistent` (itself marked "Deprecated: prefer NetworkMount for any
data you cannot recreate"), `ports`, `dataCenterIds`, and nothing,
respectively (`api-reference-v2/pods/create-a-pod`).

### 2.3 The pod object (`_record`, `_runtime_contract`, `status`, `_find_by_launch_token`)

| v1 field the adapter reads | v2 | Source |
|---|---|---|
| `id` | `id` | `api-reference-v2/pods/get-a-pod` |
| `name` | `name` | same |
| `desiredStatus` ∈ {`RUNNING`, `EXITED`, `TERMINATED`} | `status` ∈ {**`PROVISIONING`, `STARTING`,** `RUNNING`, `EXITED`, **`ERROR`,** `TERMINATED`}; plus `actions[]`, the valid transitions from that state | same; `api-reference-v2/pods/create-a-pod` |
| `costPerHr` — v1 page: "Cost in Runpod credits per hour" | `cost` — "Current cost in USD per hour", "0.0 when EXITED/TERMINATED" | `api-reference/pods/GET/pods/podId`; `openapi.json`; `create-a-pod` |
| `lastStartedAt` (the close window's anchor; deferral 04-7) | **`createdAt`** (ISO 8601) and **`startedAt`** (ISO 8601 or null) — the two instants 04-7 exists to separate | `api-reference-v2/pods/create-a-pod` |
| `networkVolume.id` / `networkVolumeId` | `mounts.network[].volumeId` | same |
| `volumeMountPath` | `mounts.network[].path` | same |
| `machine.gpuTypeId` | `gpu.id` (omitted for CPU pods) | `api-reference-v2/pods/get-a-pod` |
| `interruptible` (refused when missing or true) | **none** | `openapi.json` |
| `dockerStartCmd` (refused when missing) | **none**; `args` (string) is the nearest field | `openapi.json` |
| `image` | `image` | `get-a-pod` |
| `templateId` | `template` (string or null) | same |
| `env` (launch-token correlation; billing-cutoff margin) | `env` | same; `list-pods` |

### 2.4 Billing records (`capture_cost`)

| v1 field the adapter reads | v2 | Source |
|---|---|---|
| bare array of records | `{"records": [...], "metadata": {...}}` | `api-reference-v2/billing/get-pod-billing-history` |
| `podId` (attribution) | `records[].podId` | same |
| `time` (bucket start) | `records[].startTime` **and** `records[].endTime` (RFC 3339) | same |
| `amount` | `records[].totalAmount`, with `gpuAmount`, `cpuAmount`, `diskAmount` — the "per-pod cost breakdown" the ruling names | same; ruling of 2026-08-11 |
| `timeBilledMs` (validated ≥ 0) | **none** | `timeBilledMs`, `gpuTypeId` and `diskSpaceBilledGb` do not appear in the v2 record schema on `get-pod-billing-history` or `openapi.json` as read on 2026-09-02 |
| (nothing: v1 returns no envelope, so the resolved window is unknowable — the reason `PENDING_RECONCILIATION` is never emitted) | `metadata.query` — "Resolved query window and granularity (routes without a filter)."; `metadata.recordCount`, `metadata.uniquePodCount`, `metadata.totals`. Window snapping: `startTime` is "Snapped down to the start of its bucketSize bucket", `endTime` "Snapped up to the end of the bucketSize bucket" | same |
| currency | "Total pod cost in USD for the bucket." (`records[].totalAmount`) | same, read 2026-09-02 |

`metadata.query`'s resolved window is what deferral 04-9 was waiting for: a
verifier can compare the window it asked for against the one the provider says
it resolved, instead of trusting its own echo. Whether the buckets *fill* that
window is still an observation for the first live run.

### 2.5 Status codes and lifecycle words the runtime reacts to

| Where | v1 behaviour | v2 fact | Consequence |
|---|---|---|---|
| `create` | `200`/`201` | `201`; `400` — "The body matches the contract but was rejected — either it breaks a cross-field rule, or this GPU and data center combination could not be placed."; `422` — "The body does not match the contract. `errors` lists each violation."; **`402` "Insufficient account balance"**; `403` "no access to requested pool"; `429` with `Retry-After` | `402` is a provider-side floor beneath ours; the adapter should name it, never retry it. `400` must not be read as malformed — that is `422`'s meaning, not `400`'s (`api-reference-v2/pods/create-a-pod`) |
| `_record` | refuses any `desiredStatus` outside the three words | a just-created v2 pod is `PROVISIONING`, then `STARTING` | `_record` as written would refuse every fresh v2 create; `adopt` (requires `RUNNING`) is unchanged in meaning |
| `supervise.py` every tick | closes on any word other than `RUNNING` | a pod is legitimately `PROVISIONING`/`STARTING` before it runs | the supervisor would close a pod that is still starting; the migration must teach it the two pre-running words and `ERROR` explicitly, with a bounded provisioning wait rather than an open-ended one |
| `terminate` | tolerates `200`/`202`/`204`/`404` | `204`; `409` for cluster members | `409` is a refusal to name, not to tolerate |
| pod-side timer env | `RUNPOD_POD_ID`, `RUNPOD_API_KEY` read from the pod's environment | **not checked** in this pass: the v2 pages read do not describe the environment a pod receives | the next unit reads the pod-environment page before relying on either name under v2 |

## 3. What v2 offers that v1 did not

| Capability | v2 | Source | Bearing here |
|---|---|---|---|
| A provider-side TTL / `maxRuntime` / auto-terminate belt (deferral 04-4's missing belt; checklist row) | **Not found in the docs read on 2026-09-02.** Neither the create body nor `PATCH /v2/pods/{id}` (`name, image, args, disk, ports, env, registry, locked, globalNetworking, mounts, templateId`) carries `ttl`, `maxRuntime`, `terminateAfter`, `autoTerminate`, `timeout`, `idleTimeout`, `expiresAt` or `lifetime` | `openapi.json`; `api-reference-v2/pods/update-a-pod` | 04-4 still has no provider-side belt; the two controllers remain the only ones |
| `createdAt` and `startedAt` on the pod | present | `create-a-pod` | closes the *anchor* half of 04-7 once the verifier anchors on `createdAt` |
| Billing envelope with resolved window and per-pod breakdown | present | `get-pod-billing-history` | the *proof* half of 04-7 and the window half of 04-9 |
| A GPU catalogue with prices, independent of creating anything | `GET /v2/catalog/gpus` → per type `id`, `name`, `memory`, `price.secure` / `price.community` / `price.serverless` (USD per single GPU), `maxCount`, and `availability` with `include=AVAILABILITY&product=POD` | `api-reference-v2/catalog/list-gpu-types` | a read-only cross-check of `config/pod_placement.toml`'s reviewed price sheet and of the exact `gpu.id` strings that have never been sent to the API (deferral 04-6); the sheet stays the sealed authority |
| `402` on create when the balance is insufficient | present | `create-a-pod` | a second belt under the floor gate, named, not relied on |
| Explicit `actions[]` on the pod | present | `get-a-pod` | a supervisor can read whether `terminate` is a valid transition before requesting it |
| Pod log streaming | `GET /v2/pods/{id}/logs` | `migrate-from-v1` | not needed by this runtime; the journal on the volume is the record |
| Atomic state transitions | `POST /v2/pods/{id}/action` with `start`, `stop`, `restart`, `terminate`; `stop` "releasing GPU/CPU compute while keeping its disk. The pod moves to `EXITED`" | `trigger-a-pod-state-transition` | `terminate` only; the README's rule stands — a stopped pod bills its disk at double rate |

## 4. What has no v2 counterpart, and what each costs the runtime

1. **`interruptible=false` cannot be requested or verified.** Today
   `_create_payload` sends it and `_runtime_contract` refuses a pod that does
   not report it false. The v2 documentation read does not say whether v2 pods
   are on-demand only, spot only, or chosen elsewhere. The next unit must find
   a v2 page that states the rental type, or record documented absence and
   have the first live boot read the pod's type in the console. **This is a
   stop-and-say item under CLAUDE.md rule 9 if no page settles it:** Spec 04's
   "a spot reclaim mid-run is a silent-loss machine" is a goal, and shipping a
   v2 create that cannot prove on-demand would satisfy the ruling by breaking
   the goal.
2. **`dockerStartCmd` has no v2 field.** The runtime's whole arming argument
   rests on the pod timer being the container's primary process, checked
   before create (`models._assert_pod_timer_is_primary_process`) and again
   against the effective response (`_runtime_contract`). Under v2 the primary
   process comes from the image's `ENTRYPOINT` plus a single `args` string. The
   candidate design: the pinned image's entrypoint is the interpreter, `args`
   is the timer argv rendered as one string, and the contract check compares
   the response's `args` against that rendering. Whether `args` is split by
   the provider on whitespace, and whether it is echoed back verbatim, is
   documented nowhere read; that is an observation for Boot A under v2. The
   v2 template page (`api-reference-v2/templates/create-a-template`) was not
   read in this pass and may carry a start command; the next unit reads it
   first.
3. **`timeBilledMs` is gone.** `capture_cost`'s non-negative check on it goes;
   `records[].startTime`/`endTime` carry the same information as a window.
4. **`grouping=podId` is gone.** Filtering by `podId` is what attribution
   needs; the records still name `podId`.
5. **`includeMachine` / `includeNetworkVolume` are gone**, and with them the
   README's caution that a list omits those objects unless asked: the v2 pod
   object always carries `gpu` and `mounts`.
6. **`lastStartedAt` is gone**, replaced by the pair 04-7 wanted.

## 5. The plan the next unit executes

In order, each step offline against the fake transport, none touching a
provider:

1. **Read the three pages this pass did not**: the v2 template create page
   (for a start-command field), the pod-environment page (for `RUNPOD_POD_ID`
   and the key's env name under v2), and whatever v2 page names the rental
   type. Record each with its date in `provider_runpod.py`'s docstring, the
   way the four v1 pages are recorded today. If no page settles item 4.1,
   stop and put the conflict to Tyrel with this file as the evidence.
2. **Build `RunPodV2Provider` beside the v1 class, not over it**, behind the
   same seven-verb seam and the same `HttpTransport`, with `RUNPOD_REST_ROOT`
   becoming the v1 constant and a `RUNPOD_V2_ROOT` beside it. The v1 class
   stays until the first live run under v2 is green, then is deleted in its
   own commit; two adapters in one file is a state to pass through, not to
   keep.
3. **Teach the lifecycle vocabulary**: `_POD_STATES` gains `PROVISIONING`,
   `STARTING`, `ERROR`; `_record` accepts a fresh pod in either pre-running
   word; `adopt` still requires `RUNNING`; `supervise.py` treats the two
   pre-running words as "wait, bounded by the arming timeout" and `ERROR`
   as "close now, naming it". The armer's poll bound already exists for the
   wait.
4. **Re-anchor the close window on `createdAt`** and make the verifier
   compare `metadata.query`'s resolved window against the requested one;
   emit `PENDING_RECONCILIATION` when `recordCount` is zero inside a resolved
   window that ends at the cutoff, since v2 now distinguishes "nothing
   posted" from "window unresolved". The documented field is "Resolved query
   window and granularity (routes without a filter)." — before building the
   `PENDING_RECONCILIATION` comparison, confirm whether `metadata.query` is
   still present when the request filters by `podId` (the route Boot A and
   `capture_cost` actually use), since the parenthetical reads as if it may
   be reported only on the unfiltered route. Rewrite 04-7 and 04-9 to say
   exactly which half each closes and which half still waits on a live
   observation.
5. **Name `402` and `409`** as refusals in `create` and `terminate`, and stop
   treating `400` on create as proof the request was malformed.
6. **Add a read-only catalogue cross-check** (`GET /v2/catalog/gpus`) to the
   first-boot checklist, run before Boot A under v2, so the exact `gpu.id`
   strings and the reviewed prices are confirmed by a free call before the
   first paid one.
7. **Leave the balance observer where it is** (GraphQL), and add a row to the
   README's checklist: confirm before early 2027 whether v2 has grown a
   balance field, or the floor gate loses its live source.
8. **Rewrite the v1 shapes in `test_provider_runpod.py`** as v2 shapes in a
   second file, `test_provider_runpod_v2.py`, so the documented-shape tests
   for both routes coexist until v1 is deleted.

What this plan does not do: it does not migrate the pod-timer's environment
contract, the S3 volume view, or anything in `operations/operator/volume_s3.py`,
none of which is a REST v1 call.
