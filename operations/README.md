# operations

Anything with a human, a machine, or money on the other end.

| Directory | Responsibility |
|---|---|
| `operator/` | `verbatus`, the plain-language operator tool ([README](operator/README.md)) |
| `submit/` | manifest sealing and the data-handling gate for submitted images ([README](submit/README.md)) |
| `triage/` | the pre-Door triage instrument and producer `verbatus ingest` uses ([README](triage/README.md)) |
| `pod/` | paid pods: the pod guard, `pod_run`, bootstrap, close verification ([README](pod/README.md), [RUNPOD.md](pod/RUNPOD.md)) |
| `serving/` | the model servers a stage starts for its chairs ([README](serving/README.md)) |
| `corpus/` | RecordGold: admitting a third-party corpus and scoring pipeline output against it ([README](corpus/README.md)) |
| `replay/` | re-runs a saved run from the Perlector on, answering every model call from its recorded replies ([README](replay/README.md)) |
| `bakeoff/` | the witness bake-off bench ([README](bakeoff/README.md)) |
| `training/` | training data for the Perlector ([README](training/README.md)) |
| `notify/` | the one-way phone notification client ([README](notify/README.md)) |
| `maintenance/` | `pin_watch.py`: reports whether the configured Hugging Face and GitHub model pins and the vendored Chandra and Churro commits have moved upstream; `--notify` sends one notice when any moved or could not be checked |

There is no `local/`, `remote/` or `deploy/`: a pod runs the same stage directories this
repository holds. Where code runs is an operational fact, not an organising principle.

## Verbatus runs from a checkout, and only from a checkout

There is no wheel or packaged distribution. The pod bootstrap fetches and checks out a
pinned commit in a checkout the pod image already carries, then runs `uv sync --locked`
(`operations/pod/bootstrap.py`). What the image must carry is in `operations/pod/README.md`,
"The pod image contract", and `bootstrap.verify_image_contract` refuses by name when it does
not.

`pyproject.toml` packages `common` and `operations` only, so a wheel would carry no
`pipeline/`, `config/`, `proof/` or `gold/`, while `common/stage.py` and
`operations/submit/gate.py` resolve their defaults as siblings of those directories.
`common/checkout.py` refuses before any verb runs when they are not beside the code, and
`verbatus` reports that as `not-a-checkout`.

## Start here: the operator tool

A normal run needs no Terminal, SSH, Python or AI assistant: on a Mac, double-click
[Verbatus.command](operator/Verbatus.command). Its words, their order and their costs are in
[operator/README.md](operator/README.md).

- **Verbatus never starts, adopts or closes a pod.** A pod is started outside it, only with
  the lead's permission in that session, and its close is verified against the provider's
  own state and billing (`pod/README.md`).
- **Two words leave this computer, and only when you name a volume.** `upload
  --network-volume` sends the files a sealed submission record names, and `fetch-run` brings
  a run tree back. Their credentials come from the environment only. The only other thing
  that leaves is the `--notify` line to `https://ntfy.sh`: a run id, page ordinals and
  reasons, never register material.
- **A re-shoot is refused at the Door, whole.** Submit one capture per leaf
  (`pipeline/1_exemplar/CONTRACT.md`).
- **`export` is not the product bundle.** It copies `run.json` and `7_armarium` out of a run
  tree as a base evidence bundle and prints the reconciliation table; the Armarium builds
  and seals the product bundle.

Every problem is shown in three parts: what happened, what it means, and what to do next.
`status` shows every saved receipt again without contacting anything.
