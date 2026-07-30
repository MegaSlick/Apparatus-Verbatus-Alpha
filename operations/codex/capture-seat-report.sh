#!/bin/sh
# Capture one shell-dispatched reviewer seat's report as evidence.
#
#   sh operations/codex/capture-seat-report.sh <seat> <prompt-file> <report-path>
#
# Used by .claude/skills/reviewer-pass/SKILL.md step 3. It lived in a fenced code
# block in that file until it was extracted here: a test parsed it back out of the
# Markdown to run it, which worked but meant the executable half of a procedure was
# stored as prose. A reviewer's report is the evidence a push decision rests on, so
# the thing that writes it is code, and it is read and reviewed as code.
#
# Five behaviours here are load-bearing, and prose could enforce none of them:
#
#   1. Nothing is written until the credential scan clears the text. A reviewer's
#      report is model output about this repository and may quote a secret back.
#   2. An existing report is never overwritten — a second seat's findings must not
#      silently replace the first's.
#   3. A dangling symlink at the target is refused. `[ -e ]` follows links and reads
#      a dangling one as "nothing there", so `[ -L ]` is tested separately or the
#      write lands on whatever the link points at.
#   4. Partial output is kept when the seat exits non-zero. A crashed reviewer that
#      said something real must not lose it; the failure is reported instead.
#   5. HUP, INT and TERM abort rather than resume. A trap that tidies up and returns
#      would let the remaining steps write evidence from an interrupted call.
#
# `set -eu` is what makes each guard below fatal. Run it as a script; pasted line by
# line, a failing line simply carries on to the next one.
set -eu

if [ "$#" -ne 3 ]; then
  echo "usage: capture-seat-report.sh <seat> <prompt-file> <report-path>" >&2
  exit 2
fi

seat="$1"
prompt_path="$2"
report="$3"
report_dir=$(dirname "$report")

# Resolve the repository root rather than assuming the caller's directory. `seat.sh`
# beside this file does exactly this and every check-*.sh in .githooks/ does too; a
# reviewer noticed two files written in the same change sitting at two different
# depths. The paths below were repository-relative and worked only because the caller
# happened to be at the root. It failed loudly rather than silently, which is why this
# was a low finding and still worth taking — the mechanism to copy was eleven lines away.
root=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd -P)

[ -f "$prompt_path" ] || {
  echo "capture-seat-report: no prompt file at $prompt_path" >&2
  exit 2
}
[ -d "$report_dir" ] || {
  echo "capture-seat-report: no report directory at $report_dir" >&2
  exit 2
}

# The seat's exit status is judged last, after its output is safely on disk (4).
output=""
if output=$(sh "$root/operations/codex/seat.sh" "$seat" - <"$prompt_path" 2>&1); then
  status=0
else
  status=$?
fi

[ -n "$output" ] || {
  echo "reviewer seat $seat returned an empty report" >&2
  exit 1
}

# A seat that never started is not a review. `seat.sh` reserves exit 2 for its own
# usage and precondition failures — an unknown seat name, no `timeout` on PATH — and
# because its stderr is merged into $output above, that one-line complaint is
# non-empty, passes the scan, and used to be published as the reviewer's report. A
# review reproduced it: `reports/gpt-sol.log` containing "seat: no seat named
# audit-xyz", and then guard 2 refusing the corrected re-run because evidence already
# existed. A file that looks like a review and is not one is the exact confusion the
# reviewer pass exists to prevent, so exit 2 is reported and nothing is written.
if [ "$status" -eq 2 ]; then
  unset output
  echo "reviewer seat $seat did not run (exit 2, its usage/precondition status);" >&2
  echo "nothing was written. Fix the invocation and run it again." >&2
  exit 2
fi

# (1) Scan before writing, and drop the text from this shell if it fails.
#
# The status is captured on its own line and 1 is told apart from everything else,
# the way `commit-msg` and `applypatch-msg` do it and for the reason they cite:
# check_ingress.py answers 1 for "I recognized a credential" and 2 for "I could not
# make a trustworthy decision", and 127 means python3 was not there at all. Reporting
# any of the latter as a credential finding asserts a measurement nobody took, which
# GOVERNANCE 10 forbids — and it threw away a report that may have cost a 45-minute
# paid run. The text is still not written in either case; only the account changes.
# `|| scan_status=$?` rather than a bare pipeline followed by `$?`: under `set -e` a
# failing pipeline ends the script before the next line runs, so the status would
# never be read and the guards below would never fire. Writing it the wrong way first
# is how this comment came to exist.
scan_status=0
printf '%s\n' "$output" | python3 "$root/.githooks/check_ingress.py" --stdin-file ||
  scan_status=$?
if [ "$scan_status" -eq 1 ]; then
  unset output
  echo "reviewer seat $seat output failed credential scanning and was not written" >&2
  exit 1
fi
if [ "$scan_status" -ne 0 ]; then
  unset output
  echo "the credential scan could not run (exit $scan_status), so this report is" >&2
  echo "unverified and was not written. This is not a finding about its contents." >&2
  exit 2
fi

# (2) and (3): an existing file or any symlink, dangling or not, stops the write.
if [ -e "$report" ] || [ -L "$report" ]; then
  unset output
  echo "report target $report already exists; refusing to overwrite evidence" >&2
  exit 1
fi

temporary=$(mktemp "$report_dir/.capture-seat-report.XXXXXX") || exit 1
# (5) Each signal trap exits, so the steps below cannot run after an interruption.
trap 'rm -f "$temporary"' EXIT
trap 'rm -f "$temporary"; exit 129' HUP
trap 'rm -f "$temporary"; exit 130' INT
trap 'rm -f "$temporary"; exit 143' TERM
umask 077
printf '%s\n' "$output" >"$temporary"
unset output
# `ln` rather than `mv`, because `mv` replaces an existing target. Two reviewers
# found the same race: the existence check above and the publish below are separate
# steps, so a second seat writing the same path in between had its evidence silently
# overwritten by this one — the exact thing guard 2 in the header forbids. A hard
# link fails when the destination exists, which makes the check and the publish one
# decision instead of two. Same directory, so the link cannot cross a filesystem.
if ! ln "$temporary" "$report" 2>/dev/null; then
  echo "report target $report appeared while this report was being written;" >&2
  echo "refusing to overwrite evidence" >&2
  exit 1
fi
rm -f "$temporary"
temporary=""
trap - EXIT HUP INT TERM

if [ "$status" -ne 0 ]; then
  echo "reviewer seat $seat failed with exit $status; clean evidence remains at $report" >&2
  exit 1
fi
