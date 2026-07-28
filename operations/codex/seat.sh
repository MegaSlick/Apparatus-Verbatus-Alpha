#!/bin/sh
# Run one Codex call in a named seat.
#
#   sh operations/codex/seat.sh <seat> "<prompt>"
#   sh operations/codex/seat.sh <seat> - < prompt.txt     # prompt from stdin
#   sh operations/codex/seat.sh --dry-run <seat> "<prompt>"
#   sh operations/codex/seat.sh --dry-run --seats <file> <seat> "<prompt>"
#
# The `-` form reads stdin to the end HERE and passes the text as an argument.
# It never hands an open stdin to codex, for the reason in the second failure
# mode below.
#
# Seats are declared in seats.conf beside this file. There is no way to run
# without naming one, and no flag here for model or effort: an unnamed call is
# how a session ends up spending xhigh on a mechanical task because somebody's
# desktop config said so.
#
# Every call passes --ignore-user-config, so Codex's model/effort/sandbox
# settings are not inherited from ~/.codex/config.toml. Authentication, the
# process environment, PATH and the wrapper timeout still come from the
# machine running the call; the seat line is the tracked CLI configuration,
# not a claim to capture the whole process.
#
# Two failure modes this wrapper exists to prevent, both observed:
#   * `codex exec` blocks forever when stdin never reaches EOF. It prints
#     "Reading additional input from stdin..." and waits, spending nothing and
#     looking exactly like deep reasoning. Every call here closes stdin.
#   * A call with no ceiling can sit until a session dies around it. Prompt
#     intake and Codex execution are each wrapped in a timeout, with a hard-kill
#     escalation if either ignores TERM.
#
# Timeout is the sixth field of each tracked seat. No environment variable can
# replace the roster, its ceiling, or execution with a dry run: this wrapper
# has previously inherited stale variables from another clone and silently run
# the wrong policy. Alternate seat files are accepted only with --dry-run, for
# validation tests that cannot call Codex or spend tokens.

set -eu

root=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd -P)
seats="$root/operations/codex/seats.conf"
input_timeout_s=30
dryrun=0

# The grace between TERM and KILL, named once so the number passed to `timeout`
# and the number reported afterwards cannot drift apart.
kill_grace_s=10

# A wrapper maximum on the tracked ceiling. Be exact about what this is: it is
# NOT a security boundary. Anyone who can edit a seat line can edit this
# constant on the line above it, so it stops no deliberate act. It is a typo
# guard, and typos are the realistic failure here — one extra zero turns the
# 5400s fix seat into fifteen hours of a paid API sitting on a laptop that was
# closed hours ago, and nothing else in the path would notice. Raising it is a
# tracked, reviewed edit, which is the point: a ceiling nobody has to argue for
# is not a ceiling. 7200s leaves headroom above the longest seat declared.
max_timeout_s=7200

if [ "${1:-}" = "--dry-run" ]; then
  dryrun=1
  shift
  if [ "${1:-}" = "--seats" ]; then
    [ "$#" -ge 2 ] || { echo "seat: --seats needs a file" >&2; exit 2; }
    seats=$2
    shift 2
  fi
elif [ "${1:-}" = "--seats" ]; then
  echo "seat: alternate seat files are validation-only; add --dry-run first" >&2
  exit 2
fi

usage() {
  echo "usage: seat.sh [--dry-run [--seats file]] <seat> <prompt|->" >&2
  if [ -r "$seats" ]; then
    echo "seats:" >&2
    sed -n 's/^\([a-z][a-z0-9_-]*\)  *\([^ ]*\)  *\([^ ]*\)  *\([^ ]*\)  *\([^ ]*\)  *\([^ ]*\).*/  \1  (\2, \3, \4, \6s)/p' \
      "$seats" >&2
  fi
  exit 2
}

[ "$#" -eq 2 ] || usage
seat=$1
prompt=$2

# macOS ships no `timeout`. A direct-prompt dry run only resolves and prints
# configuration, so it needs no process ceiling. Reading stdin or making a live
# call does: resolve the command only for those two paths.
timeout_cmd=""
if [ "$prompt" = "-" ] || [ "$dryrun" -eq 0 ]; then
  if command -v timeout > /dev/null 2>&1; then
    timeout_cmd=timeout
  elif command -v gtimeout > /dev/null 2>&1; then
    timeout_cmd=gtimeout
  else
    echo "seat: no 'timeout' or 'gtimeout' on PATH — install coreutils" >&2
    echo "seat: refusing to run uncapped; an uncapped input or codex call outlives the session" >&2
    exit 2
  fi
fi

# Check the dependency before anything is created on disk. This used to sit
# after the working root was resolved, so a live call on a machine without
# `codex` first made a fresh temporary tray and then refused to run — leaving an
# empty directory behind for every attempt, indistinguishable from a tray a real
# writing seat had produced and lost.
if [ "$dryrun" -eq 0 ]; then
  command -v codex > /dev/null 2>&1 || { echo "seat: codex is not installed" >&2; exit 2; }
fi

# Drain stdin now, while this shell still owns it, so codex is always handed a
# closed one. Give the producer a short, hard ceiling as well.
if [ "$prompt" = "-" ]; then
  if prompt=$("$timeout_cmd" "--kill-after=${kill_grace_s}" "$input_timeout_s" cat); then
    :
  else
    status=$?
    case $status in
      124)
        echo "seat: prompt input did not close within ${input_timeout_s}s" >&2 ;;
      137)
        echo "seat: prompt input ignored the TERM at ${input_timeout_s}s and was hard-killed ${kill_grace_s}s later" >&2 ;;
    esac
    exit "$status"
  fi
  [ -n "$prompt" ] || { echo "seat: empty prompt on stdin" >&2; exit 2; }
fi
[ -n "$prompt" ] || { echo "seat: empty prompt" >&2; exit 2; }

[ -r "$seats" ] || { echo "seat: no $seats" >&2; exit 2; }

# Read the seat as data. First match wins; a duplicated name is a config error
# rather than a silent last-one-loads.
matches=$(awk -v want="$seat" '
  /^[[:space:]]*#/ { next }
  NF == 0          { next }
  $1 == want       { print; n++ }
  END              { exit 0 }
' "$seats")

count=$(printf '%s' "$matches" | grep -c . || true)
[ "$count" -ne 0 ] || { echo "seat: no seat named '$seat'" >&2; usage; }
[ "$count" -eq 1 ] || { echo "seat: '$seat' is declared $count times in $seats" >&2; exit 2; }

field_count=$(printf '%s\n' "$matches" | awk '{print NF}')
[ "$field_count" -eq 6 ] ||
  { echo "seat: '$seat' must have exactly six fields in $seats" >&2; exit 2; }

model=$(printf '%s\n' "$matches"  | awk '{print $2}')
effort=$(printf '%s\n' "$matches" | awk '{print $3}')
sandbox=$(printf '%s\n' "$matches"| awk '{print $4}')
workroot=$(printf '%s\n' "$matches" | awk '{print $5}')
timeout_s=$(printf '%s\n' "$matches" | awk '{print $6}')

for field in "$model" "$effort" "$sandbox" "$workroot" "$timeout_s"; do
  [ -n "$field" ] || { echo "seat: '$seat' line is incomplete in $seats" >&2; exit 2; }
done

# Zero disables GNU timeout. The ceiling therefore has to be a strictly
# positive decimal recorded in the reviewed seat line.
case $timeout_s in
  ""|*[!0-9]*)
    echo "seat: '$seat' timeout must be a positive whole number of seconds" >&2
    exit 2 ;;
esac
if ! [ "$timeout_s" -gt 0 ] 2>/dev/null; then
  echo "seat: '$seat' timeout must be greater than zero" >&2
  exit 2
fi
if [ "$timeout_s" -gt "$max_timeout_s" ]; then
  echo "seat: '$seat' asks for ${timeout_s}s, above this wrapper's ${max_timeout_s}s maximum" >&2
  echo "seat: raise max_timeout_s in seat.sh if the seat really needs longer — deliberately," >&2
  echo "seat: in a tracked edit, rather than by one extra digit in a seat line." >&2
  exit 2
fi

# No model allowlist, and that is a decision rather than an oversight. A list of
# permitted model names goes stale at the next release, and this repository has
# already been bitten by exactly that shape of guard in the push gate: a list
# that has to be edited to keep working eventually gets edited to stop working.
# What does not go stale is the model name's SHAPE. The value below is handed
# straight to `-m`, so what has to be refused is a value that stops being a
# model name and becomes something else — an option, an argument break, a path.
# Whether the name exists is Codex's question, and it answers it loudly.
case $model in
  -*)
    echo "seat: '$seat' model '$model' begins with a dash and would parse as an option" >&2
    exit 2 ;;
  ""|*[!A-Za-z0-9._-]*)
    echo "seat: '$seat' model '$model' is not a plain model name ([A-Za-z0-9._-])" >&2
    exit 2 ;;
esac

# The effort levels this project permits in tracked seats.
#
# `ultra` was kept out because it couples maximum reasoning to automatic
# delegation, and a model can narrate delegation it did not perform — so the
# seat's own report cannot establish what happened. The exclusion carried its
# own release condition: "until the mechanism leaves external evidence."
#
# Tyrel admitted it on 2026-07-28 under that condition, met this way: an ultra
# seat runs against a working copy taken from a frozen snapshot, and every
# change it makes is read from the diff between the two rather than from
# anything it says about itself.
#
# Be exact about what that buys. The snapshot verifies the WORK PRODUCT — which
# files changed, and how. It does not verify the delegation claim, and nothing
# here does. A seat may still report subagents it never ran. That is tolerable
# only because the diff is what gets reviewed and the narration is not evidence
# of anything. An ultra seat without a snapshot to diff against is the case the
# original exclusion was written for, and it is still wrong.
case $effort in
  none|minimal|low|medium|high|xhigh|max|ultra) : ;;
  *) echo "seat: '$effort' is not an allowed seat effort (none minimal low medium high xhigh max ultra)" >&2
     exit 2 ;;
esac

case $sandbox in
  read-only|workspace-write) : ;;
  danger-full-access)
     echo "seat: danger-full-access is not available through a seat" >&2; exit 2 ;;
  *) echo "seat: '$sandbox' is not a sandbox mode" >&2; exit 2 ;;
esac

if [ "$workroot" = TMPTRAY ]; then
  # $TMPDIR carries a trailing slash on macOS, which mktemp preserves verbatim
  # and every later path comparison then has to normalise around. Strip it once
  # here instead.
  tmpbase=${TMPDIR:-/tmp}
  while : ; do
    case $tmpbase in
      */) tmpbase=${tmpbase%/} ;;
      *)  break ;;
    esac
  done
  [ -n "$tmpbase" ] || tmpbase=/
  [ -d "$tmpbase" ] || { echo "seat: temporary base '$tmpbase' does not exist" >&2; exit 2; }
  tmpbase=$(CDPATH='' cd -- "$tmpbase" && pwd -P)
  if [ "$dryrun" -eq 1 ]; then
    # A dry run resolves configuration without creating the disposable tray it
    # promises not to run in. The marker is never passed to Codex.
    workdir="$tmpbase/verbatus-tray-DRYRUN"
  else
    workdir=$(mktemp -d "$tmpbase/verbatus-tray-XXXXXX")
    workdir=$(CDPATH='' cd -- "$workdir" && pwd -P)
  fi
elif [ "$workroot" = WORKCOPY ]; then
  # A durable working copy outside the repository — the only home a writing seat
  # can have. Before this existed, a workroot had to be inside the repository
  # and a workspace-write seat had to be outside it, so the only writable seat
  # possible was a temporary tray whose output vanished with the run.
  #
  # The path is NOT written here. It differs on every machine, clone and pod, and
  # a tracked absolute path is wrong everywhere except the laptop it was typed
  # on. The tracked line names the concept; the machine says where. Same reason
  # the notification topic lives in a gitignored file and not in a script.
  copyconf="$root/private/workcopy.conf"
  [ -f "$copyconf" ] && [ -r "$copyconf" ] || {
    echo "seat: '$seat' wants a WORKCOPY but $copyconf does not exist." >&2
    echo "seat: create it with a single line: WORKCOPY_PATH=/absolute/path" >&2
    echo "seat: it is gitignored, because a path is machine-local." >&2
    exit 2
  }
  # Read as data. A config that executes is a config that can do anything.
  workdir=$(sed -n 's/^WORKCOPY_PATH=//p' "$copyconf" | tail -n 1 | tr -d '"' | tr -d "'")
  [ -n "$workdir" ] || { echo "seat: $copyconf sets no WORKCOPY_PATH" >&2; exit 2; }
  case $workdir in
    /*) : ;;
    *) echo "seat: WORKCOPY_PATH must be absolute; got '$workdir'" >&2; exit 2 ;;
  esac
  [ -d "$workdir" ] || { echo "seat: WORKCOPY_PATH '$workdir' is not a directory" >&2; exit 2; }
  workdir=$(CDPATH='' cd -- "$workdir" && pwd -P)
  # The whole point is that it is not the repository. A working copy that
  # resolves back inside would let a writing seat edit the live tree.
  case $workdir in
    "$root" | "$root"/*)
      echo "seat: WORKCOPY_PATH resolves inside the repository; it must be a" >&2
      echo "seat: separate working copy, or a writing seat edits the live tree." >&2
      exit 2 ;;
  esac
  # A working copy that can push is a working copy that can push to main. Refuse
  # it here rather than trusting a prompt to say "do not push".
  if git -C "$workdir" remote 2>/dev/null | grep -q .; then
    echo "seat: WORKCOPY_PATH '$workdir' still has a git remote." >&2
    echo "seat: remove it, so no push is possible by construction." >&2
    exit 2
  fi
else
  case $workroot in
    /*|../*|*/../*|*/..)
      echo "seat: workroot '$workroot' must stay inside the repository" >&2
      echo "seat: (use WORKCOPY for a declared working copy outside it)" >&2
      exit 2 ;;
  esac
  workdir="$root/$workroot"
  [ -d "$workdir" ] || { echo "seat: workroot '$workroot' does not exist" >&2; exit 2; }
  workdir=$(CDPATH='' cd -- "$workdir" && pwd -P)
  case $workdir in
    "$root" | "$root"/*) : ;;
    *)
      echo "seat: workroot '$workroot' resolves outside the repository" >&2
      exit 2 ;;
  esac
fi

# Sandbox probes have contradicted one another about whether `-C` inside a Git
# repository grants writes to the whole repository or only the named folder.
# The exact in-repository case is unresolved. Keep the conservative rejection:
# an uncertain boundary must not be described as proven confinement.
#
# TMPTRAY was refused writes into the repository in the probes that exercised
# that direction, but it is temporary and can expose the wider system temp
# area. It is retained pending Tyrel's decision on writing seats, not declared
# safe. The session must preserve and inspect any draft before it enters here.
if [ "$sandbox" = workspace-write ]; then
  case $workdir in
    "$root" | "$root"/*)
      echo "seat: a workspace-write seat may not run inside the repository." >&2
      echo "seat: the in-repository sandbox boundary is unresolved." >&2
      echo "seat: keep writing seats outside until Tyrel chooses their future." >&2
      exit 2 ;;
  esac
fi

# A seat blind to its own deadline cannot triage against it. It plans work it
# will not be allowed to finish, and because the report is written last, the
# kill does not shorten the answer — it deletes it. A run can do an hour of real
# work, change files, and produce nothing anybody can read. This wrapper already
# holds the number, so it states it rather than trusting each prompt's author to
# remember; the ceiling and the announcement can then never drift apart.
deadline_target_s=$(( timeout_s * 9 / 10 ))
if [ "$timeout_s" -ge 120 ]; then
  deadline_human="${timeout_s} seconds (about $(( timeout_s / 60 )) minutes)"
  target_human="${deadline_target_s} seconds (about $(( deadline_target_s / 60 )) minutes)"
else
  deadline_human="${timeout_s} seconds"
  target_human="${deadline_target_s} seconds"
fi

prompt="Your time budget for this run.

You will be killed without warning after ${deadline_human}. Aim to be finished
and reported by ${target_human}, keeping the remainder as margin.

Triage the highest-value work first, and do not begin anything you cannot finish
inside that budget. Reserve the closing minutes for your report. If you run
short, stop working and write the report anyway, naming plainly what you left
half-done and where you left it — an unfinished run that reports honestly is
worth more than a complete one nobody can read.

--- your task follows ---

$prompt"

# End option parsing before the prompt: instructions such as `resume` or
# `--help` are data, never Codex subcommands or flags.
set -- codex exec \
  --ephemeral \
  --ignore-user-config \
  --strict-config \
  -m "$model" \
  -c "model_reasoning_effort=$effort" \
  -s "$sandbox" \
  -C "$workdir" \
  --skip-git-repo-check \
  -- \
  "$prompt"

# Say what is about to run. A seat that cannot be read back from the transcript
# is a seat nobody can check afterwards.
echo "seat: $seat -> $model, effort $effort, sandbox $sandbox, root $workroot, timeout ${timeout_s}s (+${kill_grace_s}s grace before hard kill)" >&2

# And say where it runs, resolved. TMPTRAY names a directory this script has
# just created under a random name, so without this line a writing seat's
# drafts exist somewhere nobody can name — findable only by globbing the
# temporary directory and guessing which tray was whose. A draft that cannot be
# located has been lost, whatever the exit status said.
echo "seat: workdir $workdir" >&2

if [ "$dryrun" -eq 1 ]; then
  for a in "$@"; do printf '%s\n' "$a"; done
  exit 0
fi

# </dev/null is the whole reason this wrapper exists; see the header.
# The if/else is not decoration: under `set -e` a bare call would abort the
# script before the status could be read, and the timeout case would be
# reported as a clean exit.
if "$timeout_cmd" "--kill-after=${kill_grace_s}" "$timeout_s" "$@" < /dev/null; then
  status=0
else
  status=$?
fi

# A run that was cut off must not read as a run that finished badly. `timeout`
# reports 124 only when the process took the TERM; a process that ignores TERM
# is escalated to KILL after the grace and comes back as 128+9 = 137, which used
# to fall through here with no explanation at all. That is the worse of the two
# cases to leave silent, because the seat's report is written last: a 137 seat
# did real work for the whole ceiling and produced nothing anybody can read.
#
# 137 is not exclusively ours — an out-of-memory kill lands on the same number,
# and nothing here can tell the two apart. So say both, and claim neither.
case $status in
  124)
    echo "seat: '$seat' hit the ${timeout_s}s ceiling and was killed" >&2
    echo "seat: cut off, not finished — anything it had not written is gone" >&2 ;;
  137)
    echo "seat: '$seat' was killed by SIGKILL after roughly ${timeout_s}s" >&2
    echo "seat: that is how this wrapper's hard kill ends when a run ignores the TERM at its" >&2
    echo "seat: ${timeout_s}s ceiling; an out-of-memory kill also arrives as 137 and cannot be" >&2
    echo "seat: told apart from here. Either way the run was cut off, not completed." >&2 ;;
esac
exit "$status"
