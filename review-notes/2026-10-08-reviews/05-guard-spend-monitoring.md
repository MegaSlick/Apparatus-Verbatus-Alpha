# Review 5: pod guard, spend ceilings, liveness and progress (Opus reviewer, 2026-10-08)

Bottom line: every hand-route pod has three deletion triggers that cannot be switched off
(soft-maximum deadline 2 h, 30-minute idle delete, backstop at the 3 h hard maximum).
"No deadline" is refused in at least eight places. The keep-alive that says "still
working" does not run during the bootstrap or the final volume sync, and judges progress
from block-buffered transcript bytes. Smallest fix: pod_budget = "off"|"on" in a v5
spend.toml, accepted by the start command and the guard, and the 15/30/60/120 min ladder
replacing the 30-minute delete. shellcheck clean; baseline suites pass.

## Wiring today

- pod_start_command.sh: <hours> required (26-29); 0 accepted (w<1 -> w=1 at 60, so a
  one-second deadline); hard max from VERBATUS_HARD_MAX_SECONDS else spend.toml (31-43);
  window cut to cap-120-start (60); deadline file written if absent (63); guard fetched
  once with no retry and run with idle 30 hard-coded (64); backstop every 300 s deletes
  at deadline+3600 or cap (66-69).
- pod_guard.sh: max_hours required (92-93); deadline always created (126-130);
  sane_deadline rejects > 7 days (124); each tick heartbeat, re-read deadline (bad value
  only logged 306-307), delete at deadline whatever the run does (310), idle accrual
  capped at keep-alive age (322), delete at idle_limit (323).
- pod_run --no-hold: pre-checks 1429-1476; release after final report sets the deadline
  to now whatever the outcome (2298-2312, 1479-1535), except a failed final sync.
  Bootstrap needs VERBATUS_HARD_DEADLINE (bootstrap_main.py 939-951).
- spend.py: states unconfigured|configured only (431-436); configured requires every
  ceiling (186-187, 147-150), positive and ordered (219-242); budget_environment()
  sealed by launch.py:1550.
- Refusers of "no deadline": start command 26-69; guard 92,124,126-130,310; spend.py
  186-242,431-436; finish_estimate.py Budget.from_policy 296-304, sealed_budget 330-348,
  PodDeadline 543-570, DeadlineWatch._tick 718-725; bootstrap_main._hard_deadline
  939-951; pod_run _pod_budget 1572-1595, _deadline_watch 1598-1660; displays
  operator/spend.py 61-65, operator/watch.py 332-363, config/README.md 150-169; lease
  and timer route (launch.py 1544-1551, supervise.py 1059) - leave bounded.

## How pods were plausibly deleted by mistake

1. --no-hold moves the deadline to now on every outcome (pod_run.py 2307); on a pod with
   its own disk every result is lost (observed 2026-10-07, README 734-739).
2. No keep-alive during bootstrap (run_bootstrap at 1964 before tracking at 2063-2065):
   model fetch, verify_store and engine starts judged by counters only; CPU threshold
   half a core; volume reads may not show in /proc/net/dev.
3. No keep-alive during the final sync (2166): a new RunTreeSync re-digests everything
   (sync.py 54-124), slow low-CPU work on FUSE.
4. Progress judged from transcript size / run-tree mtime (RunProgress 1361-1391,
   RUN_STALL_SECONDS 15 min): stages run under -I with no -u, so stdout is block-
   buffered (8 KB) and grows in bursts; the per-stage sync (run.py:1028) writes nothing
   the mark sees; after 15 quiet min the keep-alive stops and the guard deletes ~45 min
   after the last visible progress.
5. 30-minute idle fixed; SSH debugging after a refusal uses no CPU.
6. Deadline typos ignored silently; hours=0 accepted.
7. Restarted pod keeps the old deadline (63) and the cap fixed at print time (48-49).
8. Holding a finished run touches no keep-alive -> idle delete 30 min later.

## Design: budgets off by default

spend.toml schema pod-spend.v5: pod_budget = "off"|"on" (off = no guard deadline, no
backstop, no ladder delete unless ladder_delete), ladder_delete = "on"|"off"; soft/hard
maximums required only when on, allowed but inert when off. v4 retired
(RETIRED_SPEND_SCHEMAS, spend.py 26-35); 14 "pod-spend.v4" literals in tests -> shared
constant. SpendPolicy gains pod_budget, ladder_delete; budget_environment() returns
VERBATUS_POD_BUDGET=off when off. max_hourly_usd, metered cost, balance floor,
hard_lifetime keep governing the lease route and paid-action gate (they never delete).
Env override VERBATUS_POD_BUDGET.

Start command <hours|off> <sha>: off + budget off -> created-<pod>, no deadline file,
no backstop, guard fetch retried (~10 tries, 30 s), `sh pod_guard.sh off`. Numeric hours
with budget off -> arm that deadline, backstop at deadline+grace, no hard_max read.
Budget on -> today's behaviour; off refused. Always refuse hours <= 0.

Guard `pod_guard.sh <max_hours|off>`: off creates no deadline; a later valid deadline
file is honoured; ignored values also notify. Ladder replaces the delete at 323:
POD_GUARD_WARN_SECONDS 900 (one notice, alert-<pod>), URGENT 1800 (repeat every
POD_GUARD_URGENT_REPEAT 600 s with Priority: urgent), BACKUP 3600 (copy paths from
backup-<pod> to $V/runs-guard-backup/<run>-<epoch>/, diff -rq, notify), DELETE 7200
(shut_down only if POD_GUARD_DELETE=on and the backup verified or nothing to back up).
Progress resets step and sends "resumed"; keep-alive still resets. Positional
idle_minutes still accepted, mapped to WARN.

pod_run with budget off: _pod_budget says "budget off"; PodDeadline may be None;
DeadlineWatch writes deadline null, at_risk false, no notice. bootstrap_main
_hard_deadline accepts VERBATUS_HARD_DEADLINE=none for a caller that will not hold.
--no-hold stays explicit; add refusal when /workspace/private is not a network mount.
Remove pod_run's own stall notice (2102-2112); the guard owns ladder notices.

Tests: test_pod_start_command.py (env drops VERBATUS_POD_BUDGET; 35-57, 60-84 set on;
new off/refusal/zero tests), test_pod_guard.py (fixture ladder vars WARN 1 URGENT 2
BACKUP 3 DELETE 4, POD_GUARD_DELETE=on; new off-mode, later deadline, progress file,
resumed, failed backup blocks delete, urgent repeat tests; backstop tests 669-741 stay
under on), spend tests (v4->v5, off inert, on without maximums refused, v4 retired),
test_finish_estimate.py:67 becomes "budget off", test_spend_surface "budget off",
test_pod_run.py stall tests 4210-4245 assert the progress file.

## Design: stall ladder and rate checks

New operations/pod/progress_watch.py driven from pod_run's liveness tick (2085),
sharing one RunTreeProgress sample with DeadlineWatch. Expected rate: Perlector
throughput.planned_seconds_per_page / concurrency; Surya seconds_per_page / workers
(recipes 524/538/552, serving/config.py 266); otherwise own pace after 5 pages and 10
min; later, medians from earlier run reports (stage_rates with pages_done/pages_total
in the pod_run report, not the v4 journal whose audit rejects other schemas, pod_run
1259-1264). Checks: rolling 10-min page rate below half expected, or no new page within
max(10 min, 3x expected) (before the first page: startup_timeout + 3E or 30 min).
Non-page stages keep transcript/run-tree change made reliable by -u. Warn-only: GPU
util < 50% for 10 min while memory.used shows a model; CPU busy cores <
max(1.5, 25% cores) for 10 min in CPU stages; disk MB/s from /proc/<pid>/io rchar/wchar
over the process tree. Writes <report stem>-progress.json (pod-run-progress.v1; add to
models.run_report_paths 40-51 and operator/watch.py:73 which unpacks six paths),
.pod_guard/progress-<pod> one atomic line "<now> <last_ok> <ok|slow|stalled|
bootstrapping> <check> <detail>" (fresh within 300 s), .pod_guard/backup-<pod>, and
stage_rates in the final report. Minimum this session: PR 1 budget off + ladder on
counters; PR 2 stage lines with -u, progress file with page-rate checks, stage_rates,
guard reads progress file, bootstrap ticker thread.

## Smaller fixes

1. -u on the stage command (run.py:395) and orchestrator command (pod_run.py:463);
   flush=True on orchestrator prints.
2. Stage lines in invoke (run.py 474-503) and around stage_sync (1027-1033).
3. Refuse hours <= 0 (start command 29).
4. Final sync without ticks (pod_run.py:2166): reuse the orchestrator's verified set or
   tick during it.
5. Notify on ignored deadline values (guard 306-307, backstop 67).
6. GPU sampler (run.py 145-258): nvidia-smi every 5 s for every stage incl. CPU stages
   and cards with none; up to 720 samples per journal line. which() once, 15-30 s,
   summary stats + downsample.
7. Retry the guard fetch (start command 64).
8. run_tree_mark walks the whole tree every 15 s (1328-1358).
9. Start command writes the deadline with plain echo > (63); use temp-and-move.

Decisions for the lead: ladder_delete default on (recommended) or off; delete waits for a
verified backup (recommended yes).
