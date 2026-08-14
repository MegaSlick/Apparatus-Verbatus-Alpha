# operations

**Status:** offline rehearsal; no pod is started, adopted, or billed. The one path that
leaves the computer is `verbatus upload --network-volume`, which must be named explicitly
and moves files only.

Anything with a human, a machine, or money on the other end.

| Directory | Declared responsibility |
|---|---|
| `operator/` | the plain-language, offline `verbatus` rehearsal that joins the operator's six words and read-only `status` |
| `submit/` | the future page where images are handed in |
| `pod/` | pod rental, close verification, and provider-state/billing evidence |
| `data/` | future movement of runs and exports between machines |
| `notify/` | the implemented, fileless one-way notification client |
| `review/` | immutable review-candidate manifests and local receipts |

There is no `local/`, `remote/`, or `deploy/` here. A pod runs the very same stage
directories this repository holds. Where code runs is an operational fact, not an
organising principle.

## Start here: the operator rehearsal

You do not need Terminal, SSH, Python, or an AI assistant for a normal rehearsal. On a
Mac, double-click [Verbatus.command](operator/Verbatus.command). It opens the same
plain-language flow as the `verbatus` command and asks for one word at a time.

The words are:

- `launch` — shows the fixture hourly price and every reviewed ceiling, then asks for an
  exact paid-action confirmation. It records that confirmation before its fake provider
  call.
- `boot` — checks the saved fixture setup and ends with a plainly labelled green or red
  report.
- `upload` — transfers a sealed submission record to the fixture volume. It does not
  need a pod and uses zero GPU-hours. Naming `--network-volume DATACENTER:VOLUME_ID`
  sends to a real RunPod network volume instead; see the caveat below.
- `run` — resumes the named fixture run and says which pages and acts it is working on.
- `export` — makes a local evidence bundle and prints the Armarium reconciliation table.
- `close` — asks for its own exact confirmation, then shows the captured cost through its
  cutoff and the retained volume's continuing hourly price.
- `status` — reads saved receipts and sealed submission records only. It never contacts a
  provider or creates a record.

Each paid rehearsal needs a reviewed pod-request file and reviewed spending-policy file.
Do not invent a GPU class or a ceiling: those are Tyrel's decision. If either is absent,
Verbatus says it sent no new paid provider action and tells you the safe next step.

**No verb here can start, adopt, inspect or close a real pod.** The prices, pod,
bootstrap checks and billing records are local fixtures, so the whole flow can be
practised without a credential and without a cloud charge, and the surface refuses any
pod provider that is not the in-memory fake.

**One exception, and it is off unless you name it.** `upload --network-volume` sends the
sealed submission record to a real RunPod network volume. Storage transfer needs no pod
and starts no GPU meter — that is the point of running `upload` first — but it does leave
this computer, which is why the operator has to name the volume and is told exactly what
will be contacted before a byte moves. Its credentials are read from the environment
only. **That adapter has never been run against a real endpoint**: its logic is tested
against an injected client and its network behaviour is untested.

The current tree also has base Armarium evidence but not Spec 11's product export;
`export` labels that distinction instead of presenting a substitute as the final product.

Every problem is shown in three short parts: what happened, what it means, and what to
do next. Save the receipt path Verbatus prints. Indexed receipts appear in `status`
without a new provider check; if indexing itself fails, the error names the exact saved
receipt rather than pretending nothing was written.

Governance 8 governs any future live operation in `pod/`: it needs Tyrel's explicit
permission in that session, and close must be verified against provider state and billing,
never inferred from an acknowledgement. The current directory contains the contract and
the offline rehearsal only.

`notify/notify.sh` takes its bearer topic from `NTFY_TOPIC` when the environment sets one,
and otherwise reads `private/ntfy.conf`, which is gitignored. Tyrel's ruling: an ignored file
under `private/` is an acceptable home for it. The config is parsed as data and never sourced,
and only a regular file is read — a named pipe there would block forever inside the
`SessionStart` hook, which is a session that never starts rather than a ping that never
arrives. Delivery is fixed to `https://ntfy.sh`; the client refuses an ambient
`NTFY_SERVER` override so stale process state cannot redirect the bearer topic.

**Every event exits non-zero when delivery failed**, and prints one line beginning
`notify: NOT DELIVERED`. A real delivery prints nothing at all. So the exit status is evidence
for all four events, and a session that thinks it was heard cannot wait forever on a message
that was never sent.

`start` and `milestone` used to exit 0 even after printing that line, deliberately, so a
session could not die because a ping did not land. Two independent reviewers found the same
defect in it: a caller reading the status was told the phone had a message it never received,
and `milestone` is often the only announcement a long unattended run ever makes. The reason
the exit code was 0 is provided elsewhere — the `SessionStart` hook in `.claude/settings.json`
declares `"async": true`, so it runs detached and cannot block or fail the session whatever it
returns. Keeping a caller non-blocking is the caller's job; misreporting delivery to buy it
spent the one thing this script exists to protect.

The client requires Python 3 for in-memory JSON encoding and `curl` for HTTPS delivery.
