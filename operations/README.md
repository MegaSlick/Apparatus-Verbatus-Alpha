# operations

Anything with a human, a machine, or money on the other end.

| Directory | Responsibility |
|---|---|
| `operator/` | `verbatus`, the plain-language operator tool: prepare, upload, run, fetch, export, review, decide, advance and back up a run, one word at a time |
| `submit/` | manifest sealing and the data-handling gate for submitted images |
| `triage/` | the pre-Door triage instrument and producer `verbatus ingest` uses |
| `pod/` | pod rental, close verification, and provider-state/billing evidence |
| `serving/` | the model servers a stage starts for its chairs |
| `corpus/` | RecordGold: admitting a third-party expert-annotated corpus and scoring pipeline output against it ([README](corpus/README.md)) |
| `maintenance/` | `pin_watch.py`: reports whether the configured Hugging Face and GitHub model pins and the vendored Chandra and Churro commits have moved upstream; `--notify` sends one notice when any moved or could not be checked |
| `notify/` | the one-way phone notification client ([README](notify/README.md)) |
| `bakeoff/` | the Phase W witness bake-off bench: native witness runs, raw-output cache, scores ([README](bakeoff/README.md)) |

The table is not exhaustive; a directory that needs explaining carries its own README.

There is no `local/`, `remote/`, or `deploy/` here. A pod runs the very same stage
directories this repository holds. Where code runs is an operational fact, not an
organising principle.

## Verbatus runs from a checkout, and only from a checkout

There is no wheel installation and no packaged distribution of this system. The pod
bootstrap **fetches and checks out** a pinned commit in a checkout the pod image already
carries, then runs `uv sync --locked` (`operations/pod/bootstrap.py`); nothing anywhere
builds or installs a wheel.

A fetch, unlike a clone, needs a checkout, an `origin` remote and credentials reachable
from the bootstrap's own explicit environment. What the image has to carry is written
down in `operations/pod/README.md` under "The pod image contract", and
`bootstrap.verify_image_contract` refuses by name, before the fetch, when it does not.

`pyproject.toml` discovers `common` and `operations` only, so a built wheel carries no
`pipeline/`, `config/`, `proof/` or `gold/`, while `common/stage.py` and
`operations/submit/gate.py` resolve their defaults as siblings of those packages. Private
material is never packaged to cover that: `common/checkout.py` refuses before any verb
runs when the checkout-relative directories are not beside the code, and `verbatus`
renders that as `not-a-checkout`.

## Start here: the operator tool

You do not need Terminal, SSH, Python, or an AI assistant for a normal run. On a Mac,
double-click [Verbatus.command](operator/Verbatus.command); it opens the same
plain-language flow as the `verbatus` command and asks for one word at a time. Its words,
their order and what each costs are in [operator/README.md](operator/README.md).

- **Verbatus never starts, adopts or closes a pod.** A pod is started outside it, only
  with the project lead's permission in that session, and its close is verified against
  the provider's own state and billing (`pod/README.md`).
- **Two words leave this computer, and only when you name a volume.**
  `upload --network-volume` sends the files a sealed submission record names to a RunPod
  network volume, and `fetch-run` brings a pod-written run tree back from one. Their
  credentials are read from the environment only. The one other thing that leaves is
  the `--notify` line, sent to `https://ntfy.sh`: a run id, page ordinals and reasons,
  never register material.
- **A re-shoot is refused at the Door, whole.** A triage manifest that names a re-shoot
  cluster (two captures of one leaf) makes the Door refuse the entire submission, since
  no later stage links two captures of one leaf. Submit one capture per leaf
  (`pipeline/1_exemplar/CONTRACT.md`).
- **`export` is not the product bundle.** It copies `run.json` and the `7_armarium`
  directory out of a run tree as a base evidence bundle and prints the reconciliation
  table. The product bundle is built and sealed by the Armarium (`pipeline/7_armarium`).

Every problem is shown in three short parts: what happened, what it means, and what to do
next. Save the receipt path Verbatus prints; `status` shows every indexed receipt again
without contacting anything.

## Notifications

`notify/notify.sh` posts one line to the project lead's phone through `https://ntfy.sh`,
and exits non-zero, printing `notify: NOT DELIVERED`, whenever delivery failed.
`verbatus --notify` uses it for run, export and held-run notices. How events, the topic
and failures work is in [notify/README.md](notify/README.md).
