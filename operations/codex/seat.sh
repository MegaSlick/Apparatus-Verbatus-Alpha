#!/bin/sh
# Run one Codex call in a named seat.
#
#   sh operations/codex/seat.sh <seat> "<prompt>"
#   sh operations/codex/seat.sh <seat> - < prompt.txt     # prompt from stdin
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
# Every call passes --ignore-user-config, so nothing is inherited from
# ~/.codex/config.toml. The seat line is the whole configuration, and it is a
# tracked file a reviewer can read.
#
# Two failure modes this wrapper exists to prevent, both observed:
#   * `codex exec` blocks forever when stdin never reaches EOF. It prints
#     "Reading additional input from stdin..." and waits, spending nothing and
#     looking exactly like deep reasoning. Every call here closes stdin.
#   * A call with no ceiling can sit until a session dies around it. Every call
#     is wrapped in timeout.
#
# Environment:
#   CODEX_SEAT_TIMEOUT   seconds before the call is killed (default 600)
#   CODEX_SEAT_DRYRUN=1  print the resolved command and exit 0, run nothing.
#                        This is what the tests assert against, so they cost
#                        no tokens.
#   CODEX_SEATS_FILE     an alternate seat file. For the tests, which need to
#                        feed this script malformed seats without editing the
#                        real one. Not a way to escape the roster — anyone who
#                        can set it can edit seats.conf. The guard here is
#                        discipline, as everywhere else in this repository.

set -eu

root=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)
seats=${CODEX_SEATS_FILE:-"$root/operations/codex/seats.conf"}
timeout_s=${CODEX_SEAT_TIMEOUT:-600}

usage() {
  echo "usage: seat.sh <seat> <prompt|->" >&2
  if [ -r "$seats" ]; then
    echo "seats:" >&2
    sed -n 's/^\([a-z][a-z0-9_-]*\)  *\([^ ]*\)  *\([^ ]*\)  *\([^ ]*\).*/  \1  (\2, \3, \4)/p' \
      "$seats" >&2
  fi
  exit 2
}

[ "$#" -eq 2 ] || usage
seat=$1
prompt=$2

# Drain stdin now, while this shell still owns it, so codex is always handed a
# closed one.
if [ "$prompt" = "-" ]; then
  prompt=$(cat)
  [ -n "$prompt" ] || { echo "seat: empty prompt on stdin" >&2; exit 2; }
fi

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

model=$(printf '%s\n' "$matches"  | awk '{print $2}')
effort=$(printf '%s\n' "$matches" | awk '{print $3}')
sandbox=$(printf '%s\n' "$matches"| awk '{print $4}')
workroot=$(printf '%s\n' "$matches" | awk '{print $5}')

for field in "$model" "$effort" "$sandbox" "$workroot"; do
  [ -n "$field" ] || { echo "seat: '$seat' line is incomplete in $seats" >&2; exit 2; }
done

# The efforts the API actually accepts, checked here because the CLI does not:
# it forwarded `ultra` to Luna, whose own catalog does not list it, and a
# wrong value that does not error is a seat running at a depth nobody chose.
case $effort in
  none|minimal|low|medium|high|xhigh|max) : ;;
  *) echo "seat: '$effort' is not an effort the API accepts (none minimal low medium high xhigh max)" >&2
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
  workdir=$(mktemp -d "$tmpbase/verbatus-tray-XXXXXX")
else
  workdir="$root/$workroot"
  [ -d "$workdir" ] || { echo "seat: workroot '$workroot' does not exist" >&2; exit 2; }
  workdir=$(CDPATH='' cd -- "$workdir" && pwd)
fi

# Measured, not assumed. `-C` does not bound a workspace-write sandbox: the
# boundary resolves to an ancestor of it — the enclosing git repository when
# there is one. A seat rooted at `autoclave/` would therefore be free to write
# anywhere in the tree, which is the reverse of the quarantine.
#
# The boundary that IS enforced is the outside one: a seat rooted outside the
# repository was refused an absolute-path write into it by the OS sandbox. So a
# writing seat runs outside the tree, and the session carries its output in
# after reading it — no byte enters that a reviewed session did not place.
if [ "$sandbox" = workspace-write ]; then
  case $workdir in
    "$root" | "$root"/*)
      echo "seat: a workspace-write seat may not run inside the repository." >&2
      echo "seat: -C does not bound the sandbox — the enclosing git repo does," >&2
      echo "seat: so this seat could write the whole tree. Use workroot TMPTRAY." >&2
      exit 2 ;;
  esac
fi

set -- codex exec \
  --ignore-user-config \
  -m "$model" \
  -c "model_reasoning_effort=$effort" \
  -s "$sandbox" \
  -C "$workdir" \
  --skip-git-repo-check \
  "$prompt"

# Say what is about to run. A seat that cannot be read back from the transcript
# is a seat nobody can check afterwards.
echo "seat: $seat -> $model, effort $effort, sandbox $sandbox, root $workroot, timeout ${timeout_s}s" >&2

# And say where it runs, resolved. TMPTRAY names a directory this script has
# just created under a random name, so without this line a writing seat's
# drafts exist somewhere nobody can name — findable only by globbing the
# temporary directory and guessing which tray was whose. A draft that cannot be
# located has been lost, whatever the exit status said.
echo "seat: workdir $workdir" >&2

if [ "${CODEX_SEAT_DRYRUN:-}" = 1 ]; then
  for a in "$@"; do printf '%s\n' "$a"; done
  exit 0
fi

command -v codex > /dev/null 2>&1 || { echo "seat: codex is not installed" >&2; exit 2; }

# macOS ships no `timeout`. This machine has GNU coreutils, a pod or a fresh
# Mac may not, and without the check every seat call would die at 127 with a
# message blaming codex.
if ! command -v timeout > /dev/null 2>&1; then
  echo "seat: no 'timeout' on PATH — install coreutils (brew install coreutils)" >&2
  echo "seat: refusing to run uncapped; an uncapped codex call outlives the session" >&2
  exit 2
fi

# </dev/null is the whole reason this wrapper exists; see the header.
# The if/else is not decoration: under `set -e` a bare call would abort the
# script before the status could be read, and the timeout case would be
# reported as a clean exit.
if timeout "$timeout_s" "$@" < /dev/null; then
  status=0
else
  status=$?
fi

if [ "$status" -eq 124 ]; then
  echo "seat: '$seat' hit the ${timeout_s}s ceiling and was killed" >&2
fi
exit "$status"
