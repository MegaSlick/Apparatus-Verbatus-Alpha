#!/bin/sh
# The autoclave — a sealed chamber a writing agent works inside.
#
# The shape, in one paragraph. The host repository is mounted read-only at
# /src. The container clones it to /work and cuts a branch. The agent works
# there with a full shell and runs its own tests. When it is done the branch
# comes back as a git bundle through /out, the one writable host path, and the
# session reads the diff before anything enters the real repository. No *git*
# credentials go in, so nothing can be pushed from inside; one vendor's model
# credential does, when the chamber is created for that vendor.
#
# Usage:
#   autoclave.sh doctor                  what is installed, running, and built
#   autoclave.sh build                   build the image
#   autoclave.sh login <claude|codex> [browser|device]
#                                        sign a vendor in, once, into its volume
#   autoclave.sh new <task> [<base>] [<claude|codex>]
#                                        start a chamber on a branch from <base>,
#                                        holding one vendor's credential or none
#   autoclave.sh dispatch <task> <claude|codex> <brief-file> <model> [effort]
#                                        run an agent inside, against a written brief
#   autoclave.sh shell <task>            open a shell in a running chamber
#   autoclave.sh exec <task> <cmd>...    run one command in a chamber
#   autoclave.sh collect <task>          bring the branch back as agent/<task>
#   autoclave.sh report <task>           print the report the agent left, if any
#   autoclave.sh list                    every chamber, running or stopped
#   autoclave.sh rm <task> [force]       destroy a chamber (never its output);
#                                        `force` destroys uncollected work with it
#
# POSIX sh: this runs on the host, and the host's shell is not guaranteed.
set -eu
GIT_NO_REPLACE_OBJECTS=1
export GIT_NO_REPLACE_OBJECTS

IMAGE_NAME="verbatus-autoclave"
IMAGE_TAG="dev"
IMAGE="${IMAGE_NAME}:${IMAGE_TAG}"

# Container names are prefixed so `list` and `rm` can never touch a container
# belonging to something else on this machine.
PREFIX="verbatus-ac"

# Agent-controlled bytes cross into the host only through bounded helper calls.
# Four MiB leaves ample room for prompts and reports; a collection bundle may
# carry repository history, so its ceiling is deliberately larger but finite.
MAX_BRIEF_BYTES=4194304
MAX_REPORT_BYTES=4194304
MAX_COLLECTION_BUNDLE_BYTES=536870912

# The repository root, resolved from this script rather than the caller's cwd,
# so every command works from anywhere.
REPO_ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)

# The one thing in `/out` that a POSIX shell cannot do for itself: open a path once,
# refusing to follow a symlink at the end of it. See the file's own header.
SAFE_FILE="${REPO_ROOT}/operations/autoclave/safe_file.py"
FINGERPRINT="${REPO_ROOT}/operations/autoclave/fingerprint.py"

# Output lives in the gitignored workbench: it is a note, not a document, and it
# must never appear in `git status`. One drawer per task, kept after the chamber
# is destroyed — the bundle is the evidence that a dispatch happened.
OUT_ROOT="${REPO_ROOT}/workbench/autoclave"

# Credentials live in named volumes, one per vendor, and never in the image, the
# repository or a bind mount from the host.
#
# Why not reuse the host's own sign-in. Claude Code keeps its credentials in the
# macOS Keychain, which a Linux container cannot read, and lifting the token out
# of the Keychain to inject it would mean handling Tyrel's credential in plain
# text. Codex keeps a real file, but bind-mounting it would put his live host
# credential inside a chamber that has network egress.
#
# So the chamber gets its own sign-in, done once by him through `login`, held in
# a volume that no agent can reach except as the file its own CLI reads. The
# secret never passes through a session, a script, a commit or a transcript.
AUTH_VOL_CLAUDE="verbatus-ac-auth-claude"
AUTH_VOL_CODEX="verbatus-ac-auth-codex"
AUTH_DIR_CLAUDE="/home/agent/.claude"
AUTH_DIR_CODEX="/home/agent/.codex"
CLAUDE_LOCK="${AUTH_DIR_CLAUDE}/.credentials.autoclave.lock"

# One task is one lifecycle.  A launcher command may read a chamber state and then
# act on it; another command for that same task cannot be allowed to change that
# state between those two operations.  `mkdir` is the portable atomic primitive
# available to POSIX sh. The directory is resolved beside git's common directory,
# so linked worktrees share it while the chamber cannot reach it.
LIFECYCLE_LOCK=""
LIFECYCLE_SNAPSHOT=""
LIFECYCLE_INCOMING=""

lifecycle_cleanup() {
    trap - 0 1 2 15
    [ -z "$LIFECYCLE_INCOMING" ] ||
        git -C "$REPO_ROOT" update-ref --no-deref -d "$LIFECYCLE_INCOMING" >/dev/null 2>&1 || :
    [ -z "$LIFECYCLE_SNAPSHOT" ] || rm -f "$LIFECYCLE_SNAPSHOT" || :
    [ -z "$LIFECYCLE_LOCK" ] || rmdir "$LIFECYCLE_LOCK" 2>/dev/null || :
    LIFECYCLE_INCOMING=""
    LIFECYCLE_SNAPSHOT=""
    LIFECYCLE_LOCK=""
}

acquire_lifecycle_lock() {
    lifecycle_task="$1"
    lifecycle_common=$(git -C "$REPO_ROOT" rev-parse --path-format=absolute --git-common-dir) ||
        die "could not locate this repository's common git directory"
    lifecycle_root=$(dirname "$lifecycle_common")
    lifecycle_dir="${lifecycle_root}/workbench/autoclave/.locks"
    mkdir -p "$lifecycle_dir" ||
        die "could not create the task lock directory ${lifecycle_dir}"
    # Cleanup removes `LIFECYCLE_LOCK`, so assign it only after this process has
    # created the directory; a refused acquire must not own the existing lock.
    lifecycle_candidate="${lifecycle_dir}/${lifecycle_task}.lock"
    mkdir "$lifecycle_candidate" 2>/dev/null ||
        die "task '${lifecycle_task}' is already active: ${lifecycle_candidate}. If no launcher is running, remove the stale lock with: rmdir ${lifecycle_candidate}"
    LIFECYCLE_LOCK="$lifecycle_candidate"
    trap 'lifecycle_cleanup' 0
    trap 'lifecycle_cleanup; exit 1' 1 2 15
}

collected_marker_of() {
    printf '%s/.collected/%s/%s' "$OUT_ROOT" "$1" "$2"
}

die() { printf 'autoclave: %s\n' "$*" >&2; exit 1; }
note() { printf '%s\n' "$*"; }

need_docker() {
    command -v docker >/dev/null 2>&1 || die \
        "docker CLI not found. Install with: brew install colima docker"
    docker info >/dev/null 2>&1 || die \
        "docker CLI found but no engine is responding. Start one with: colima start"
}

# A task name becomes a container name, a directory and a git branch, so it is
# constrained once here rather than sanitised differently in three places.
check_task() {
    [ -n "${1:-}" ] || die "a task name is required"
    case "$1" in
        *[!a-z0-9-]*) die "task name '$1' — lowercase letters, digits and hyphens only" ;;
        -*|*-) die "task name '$1' must not start or end with a hyphen" ;;
    esac
}

container_of() { printf '%s-%s' "$PREFIX" "$1"; }
outdir_of() { printf '%s/%s' "$OUT_ROOT" "$1"; }

exists() { docker container inspect "$(container_of "$1")" >/dev/null 2>&1; }

running() {
    [ "$(docker container inspect -f '{{.State.Running}}' \
        "$(container_of "$1")" 2>/dev/null)" = "true" ]
}

cmd_doctor() {
    note "repository:  ${REPO_ROOT}"
    note "output root: ${OUT_ROOT}"
    printf 'colima:      '
    if command -v colima >/dev/null 2>&1; then colima version 2>&1 | head -1; else echo "not installed"; fi
    printf 'docker CLI:  '
    # `|| echo` because `set -e` applies here too: a docker CLI that is installed but
    # cannot report a version killed `doctor` at this line, on exactly the broken
    # machine the command exists to describe. It answers about the rest instead.
    if command -v docker >/dev/null 2>&1; then
        docker --version 2>/dev/null || echo "installed, but it will not report a version"
    else
        echo "not installed"
    fi
    printf 'engine:      '
    if docker info >/dev/null 2>&1; then echo "responding"; else echo "not responding"; fi
    printf 'image:       '
    if docker image inspect "$IMAGE" >/dev/null 2>&1; then
        # `image ls` rather than `inspect .Size`: the two disagree — inspect
        # reported 480 MB for an image `ls` calls 1.88 GB — and the number an
        # operator can check against `docker image ls` is the one to print.
        docker image ls "$IMAGE" --format '{{.Size}} (built {{.CreatedSince}})'
    else
        echo "not built — run: $0 build"
    fi
    # **A volume is where a sign-in lands, not evidence that one finished.** This used
    # to print "signed in" on the strength of `has_volume` alone — a claim about a
    # vendor's authentication state that nothing here had measured (GOVERNANCE 10), and
    # wrong in the case that matters: `login` creates the volume before the sign-in
    # runs, so an abandoned or failed attempt left one behind and `doctor` called it a
    # sign-in from then on. It now asks the vendor CLI, in a throwaway container, and
    # reads only the exit status; the CLI's output is discarded, so nothing the volume
    # holds can reach this script's output.
    #
    # `doctor` is the first thing anyone runs, so it still answers on a machine with
    # nothing installed: with no engine or no image there is no way to ask, and it says
    # "unknown" rather than guessing in either direction.
    for pair in "claude:${AUTH_VOL_CLAUDE}" "codex:${AUTH_VOL_CODEX}"; do
        vendor=${pair%%:*}; volume=${pair#*:}
        printf 'auth %-7s ' "$vendor"
        if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
            echo "unknown — no engine is responding, so nothing here can ask"
        elif ! has_volume "$volume"; then
            echo "not signed in — run: $0 login ${vendor}"
        elif ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
            echo "unknown — a volume exists, but asking needs the image: $0 build"
        else
            asked=0; authenticated "$vendor" "$volume" || asked=$?
            case "$asked" in
                0)
                    if [ "$vendor" = claude ]; then
                        echo "signed in (${volume}); access is refreshed before chamber auth checks"
                    else
                        echo "signed in (${volume})"
                    fi ;;
                1) echo "not signed in — the volume exists but ${vendor} says no; run: $0 login ${vendor}" ;;
                *) echo "unknown — the volume exists but ${vendor} could not be asked" ;;
            esac
        fi
    done
}

cmd_build() {
    need_docker
    docker buildx version >/dev/null 2>&1 ||
        die "Docker Buildx is required — install it with: brew install docker-buildx"
    harness_fingerprint=$(python3 "$FINGERPRINT") ||
        die "cannot fingerprint the chamber image inputs"
    # Context is the repository root so the Dockerfile can reach
    # requirements-dev.txt; .dockerignore there denies everything else.
    # The uid and gid are the caller's, so bundles land owned by the host user.
    docker buildx build --load \
        --file "${REPO_ROOT}/operations/autoclave/Dockerfile" \
        --build-arg "UID=$(id -u)" \
        --build-arg "GID=$(id -g)" \
        --build-arg "HARNESS_FINGERPRINT=${harness_fingerprint}" \
        --tag "$IMAGE" \
        "${REPO_ROOT}"
    note ""
    note "built ${IMAGE}"
}

has_volume() { docker volume inspect "$1" >/dev/null 2>&1; }

auth_dir() {
    case "$1" in
        claude) printf '%s' "$AUTH_DIR_CLAUDE" ;;
        codex)  printf '%s' "$AUTH_DIR_CODEX" ;;
        *) return 1 ;;
    esac
}

# The command that asks a vendor whether it is signed in, checked against `--help`
# on both CLIs rather than guessed: `claude auth status` exits 0 when signed in and
# 1 when not; `codex login status` does the same. Both print, and neither is allowed
# to print here — the caller discards their output and keeps only the status.
auth_check() {
    case "$1" in
        claude) printf '%s' "claude auth status" ;;
        codex)  printf '%s' "codex login status" ;;
        *) return 1 ;;
    esac
}

# Ask the vendor, in a throwaway container holding nothing but the volume. Verified
# inside a chamber: the Claude sign-in that `auth status` reads lives at
# `.credentials.json` *inside* the mounted directory, so a container that has the
# volume and nothing else answers correctly. The CLI's own output goes nowhere, so no
# credential byte can reach a log or a transcript.
#
# **Three answers, not two.** 0 signed in, 1 the vendor says no, 2 the question could
# not be asked. Collapsing the last two into "no" is what `.githooks/commit-msg` calls
# a check that cannot run reading as a failure — harmless where the consequence is a
# refusal, and not harmless in `login`, where the consequence is deleting the volume
# the sign-in just landed in. A transient engine error during a one-second verification
# run must not cost a completed sign-in.
#
# The vendor's status is carried out on stdout as a sentinel rather than as this
# container's exit status, because `docker run` reports its own failures with exit
# codes a CLI could also produce. No sentinel means nothing ran.
authenticated() {
    vendor_asked="$1"
    volume_asked="$2"
    # `login` has already inspected an immutable ID before it opens its booth. Keep
    # every container in that one operation on the exact same image: a tag can move
    # between the interactive CLI and the status check. Other callers deliberately
    # keep the current tagged image behaviour.
    image_asked="${3:-$IMAGE}"
    mount_asked=$(auth_dir "$vendor_asked") || return 2
    check_asked=$(auth_check "$vendor_asked") || return 2
    has_volume "$volume_asked" || return 1
    # `CLAUDE_CONFIG_DIR` belongs here too, and its absence was the sharpest gap in the
    # first cut of this fix: `cmd_login` calls this function *to verify the sign-in that
    # just completed*, and `require_signed_in` calls it on every `new`. Without the
    # variable this throwaway container holds the credential with no configuration beside
    # it — the exact condition the header above names as fatal. If `claude auth status`
    # ever refreshes a near-expiry token, the check verifying a sign-in would be the thing
    # that destroyed it, and the symptom would once again look like an expiry.
    answer=$(docker run --rm --volume "${volume_asked}:${mount_asked}" \
        --env "CLAUDE_CONFIG_DIR=${mount_asked}" "$image_asked" \
        sh -c "${check_asked} >/dev/null 2>&1; printf 'ac-auth:%s' \$?" 2>/dev/null) || return 2
    case "$answer" in
        ac-auth:0) return 0 ;;
        ac-auth:*) return 1 ;;
        *) return 2 ;;
    esac
}

# Claude's print-mode dispatch does not renew an expired access token itself.  Refresh
# before asking its CLI for status. The CLI reports an expired credential as logged in,
# so the helper must succeed; status then remains a second check of the resulting layout.
refresh_claude_volume() {
    volume_asked="$1"
    # Same reason `authenticated` takes one: inside a single `new`, a rebuild that moves
    # the tag would otherwise refresh and verify the credential with one CLI build and
    # then run the chamber on a different one.
    image_asked="${2:-$IMAGE}"
    has_volume "$volume_asked" || return 1
    docker run --rm --volume "${volume_asked}:${AUTH_DIR_CLAUDE}" \
        --env "CLAUDE_CONFIG_DIR=${AUTH_DIR_CLAUDE}" "$image_asked" \
        python3 /opt/autoclave/refresh_claude_token.py
}

refresh_claude_chamber() {
    task_asked="$1"
    docker exec -e CLAUDE_CONFIG_DIR="$AUTH_DIR_CLAUDE" \
        "$(container_of "$task_asked")" \
        python3 /opt/autoclave/refresh_claude_token.py
}

cmd_login() {
    vendor="${1:-}"
    # `--device` asks the vendor for a code to type on another device instead of
    # standing up a callback server on localhost *inside the container*. A callback at
    # `http://localhost:1455` resolves to the container and is unreachable from a
    # phone, so a flow that uses one has to be finished at this keyboard.
    #
    # **Which of the two flows Claude uses was asserted here and never measured.**
    # This comment said Claude's ordinary flow used that localhost callback, and a
    # dated record was written correcting an earlier commit on the strength of it.
    # Measured on 2026-08-03 against the CLI in the image: `claude auth login` prints
    # an authorize URL whose `redirect_uri` is
    # `https://platform.claude.com/oauth/code/callback`, stands up no local server, and
    # then waits at `Paste code here if prompted >`. So it can be completed from a
    # phone after all, and the earlier commit this project corrected was right.
    # `--device` remains Codex's alone because it is Codex's flag, not because Claude
    # needs a keyboard.
    mode="${2:-browser}"
    # **Arguments are judged before infrastructure is touched**, the same ordering
    # `dispatch` and `new` already keep. Asking Docker first meant `login gemini`
    # answered "docker CLI not found" instead of naming the two vendors that exist —
    # a true statement about the wrong thing, and one that sends the reader off to
    # install something they did not need. Two tests assert the vendor error and fail
    # anywhere Docker is absent, which is every chamber and CI.
    case "$vendor" in
        # `claude auth login`, not the `/login` slash command: the slash form is a
        # prompt typed into an interactive session, so it needs a terminal, and the
        # branch below exists precisely for when there is not one. `claude auth --help`
        # names `login`, `logout` and `status` as real subcommands.
        claude) volume="$AUTH_VOL_CLAUDE"; mount="$AUTH_DIR_CLAUDE"; tool="claude auth login" ;;
        codex)  volume="$AUTH_VOL_CODEX";  mount="$AUTH_DIR_CODEX";  tool="codex login" ;;
        *) die "login takes 'claude' or 'codex'" ;;
    esac
    case "$mode" in
        browser) : ;;
        device)
            [ "$vendor" = codex ] ||
                die "only codex has a --device-auth flag. '$0 login claude' does not need one: it prints a URL and waits for a pasted code, so it can be finished from a phone"
            tool="codex login --device-auth" ;;
        *) die "login mode is 'browser' or 'device'" ;;
    esac
    need_docker
    image_id=$(docker image inspect --format '{{.Id}}' "$IMAGE" 2>/dev/null) ||
        die "image not built — run: $0 build"
    [ -n "$image_id" ] || die "image '$IMAGE' has no immutable image ID"

    # **Whether this call created the volume decides whether this call may take it
    # away.** An empty volume that was already here is somebody else's business.
    #
    # It is removed through a trap rather than on the way out, because the way this
    # actually happens is a sign-in abandoned halfway — Ctrl-C at the vendor's prompt,
    # or a closed terminal — and neither of those reaches a line further down. An empty
    # volume left standing is what made `doctor` report a sign-in that never happened
    # and what let `new <task> <vendor>` believe a credential existed.
    created_now=no
    if ! has_volume "$volume"; then
        docker volume create "$volume" >/dev/null
        created_now=yes
        discard_empty_volume() {
            leaving="$1"
            trap - 0 1 2 15
            docker volume rm "$volume" >/dev/null 2>&1 || true
            exit "$leaving"
        }
        trap 'discard_empty_volume $?' 0
        trap 'discard_empty_volume 1' 1 2 15
    fi

    note "Signing '${vendor}' into ${volume}."
    note "This is interactive and it is yours to complete — the sign-in happens"
    note "between you and the vendor, and nothing about it is read or stored by"
    note "this script. Follow whatever the CLI asks, then exit."
    note ""
    # --rm: the container is a booth for the sign-in. What persists is the volume.
    #
    # A tty is requested only when one exists. `docker run --tty` without one fails
    # outright, and the sign-in is exactly the command somebody may drive from a
    # script, a pipe, or an agent session that has no terminal.
    # The CLI's own exit status is kept rather than thrown away with `|| true`. It was
    # discarded so the report below could always run; the report has to say what
    # happened either way, and a vendor CLI that failed is the clearest evidence there
    # is of what happened.
    #
    # Claude receives CLAUDE_CONFIG_DIR so every configuration file lands in the
    # mounted volume and survives this temporary login container.
    case "$vendor" in
        claude)
            login_cmd="flock -w 60 ${CLAUDE_LOCK} $tool"
            login_env="--env CLAUDE_CONFIG_DIR=${mount}" ;;
        *)
            login_cmd="$tool"
            login_env="" ;;
    esac

    login_status=0
    if [ -t 0 ]; then
        # shellcheck disable=SC2086
        docker run --rm --interactive --tty \
            --volume "${volume}:${mount}" \
            $login_env \
            "$image_id" \
            sh -c "$login_cmd" || login_status=$?
    else
        note "(no terminal here — running without one; follow the URL or code below)"
        note ""
        # shellcheck disable=SC2086
        docker run --rm --interactive \
            --volume "${volume}:${mount}" \
            $login_env \
            "$image_id" \
            sh -c "$login_cmd" || login_status=$?
    fi

    note ""
    # **Whether files appeared is not whether a sign-in worked.** The old test was
    # `ls -A` on the mount, and both CLIs write configuration into that directory
    # before, during and after any sign-in — so it reported success for an attempt
    # that was cancelled at the vendor's own page. Ask the vendor instead.
    asked=0; authenticated "$vendor" "$volume" "$image_id" || asked=$?
    if [ "$asked" -eq 2 ]; then
        # The question could not be put. Whatever the volume now holds is not this
        # script's to throw away on the strength of an answer nobody got.
        [ "$created_now" = yes ] && trap - 0 1 2 15
        die "${vendor}'s sign-in could not be verified — the check itself did not run. ${volume} is left as it is; try '$0 doctor' once the engine and image are healthy"
    fi
    if [ "$login_status" -ne 0 ] || [ "$asked" -ne 0 ]; then
        note "${vendor} does not report itself signed in — the sign-in did not complete."
        note "Nothing is broken; run this again when you are ready."
        die "${vendor} sign-in did not complete (its CLI exited ${login_status})"
    fi
    if [ "$created_now" = yes ]; then
        trap - 0 1 2 15
    fi
    note "${vendor} reports itself signed in. Chambers created from here will use ${volume}."
}

# Two arms below, one per vendor, and neither naming the other's constant: that
# shape is what `test_a_chamber_holds_one_vendor_credential_or_none` reads, and it
# is the shape of the defect it exists to catch. The tri-state handling that would
# otherwise be duplicated into both lives here instead.
# The refresh helper's exit code is the difference between "wait and retry" and "sign in
# again", and telling an operator to sign in rotates a refresh token that is usually still
# valid. Exit 1 is retryable and leaves the stored credential intact — the endpoint
# failed, refused, another writer held the lock, or a concurrent write was restored. Exit
# 2 is local: the credential could not be read or republished, and needs a human.
claude_refresh_failure() {
    case "$1" in
        2) die "Claude's stored credential could not be read or republished, so this machine's credential state needs checking before anything else — see the message above; '$0 doctor' reports the volumes" ;;
        *) die "Claude could not refresh its access token, and the stored credential is unchanged — retry, and only run '$0 login claude' if the retry reports the refresh token itself is rejected" ;;
    esac
}

require_signed_in() {
    # `new` resolves an immutable image ID and starts the chamber from it, so the
    # credential must be refreshed and verified against that exact image rather than a
    # tag that a concurrent rebuild can move in between.
    image_asked="${3:-$IMAGE}"
    if [ "$1" = claude ]; then
        has_volume "$2" || die "'claude' is not signed in — run: $0 login claude"
        refreshed=0; refresh_claude_volume "$2" "$image_asked" || refreshed=$?
        [ "$refreshed" -eq 0 ] || claude_refresh_failure "$refreshed"
    fi
    asked=0; authenticated "$1" "$2" "$image_asked" || asked=$?
    [ "$asked" -eq 1 ] && die "'$1' is not signed in — run: $0 login $1"
    [ "$asked" -eq 0 ] ||
        die "'$1' could not be asked whether it is signed in, so nothing here can say the chamber will hold a working credential. Check '$0 doctor'"
}

cmd_new() {
    task="${1:-}"; check_task "$task"
    base="${2:-HEAD}"
    # **A chamber gets one vendor's credential, or none.** It used to get every
    # sign-in that existed, read-write, whichever vendor was later dispatched — so a
    # Codex agent held the Claude credential and the reverse, with open egress. The
    # vendor has to be named here rather than at `dispatch`, because mounts are fixed
    # when the container is created and cannot be added to a running one.
    #
    # None is the default on purpose: a chamber for running the suite or reading the
    # tree needs no credential at all, and that is most of them.
    vendor="${3:-none}"
    case "$vendor" in
        claude|codex|none) : ;;
        *) die "new takes 'claude', 'codex' or nothing as its vendor" ;;
    esac
    # **Above the lock, because the lock resolves the common git directory too.** With
    # this below `acquire_lifecycle_lock`, the case this guard exists for -- a worktree
    # whose main checkout is gone -- died inside the lock with "could not locate this
    # repository's common git directory" and never reached the explanation or the path.
    # The same ordering mistake this guard was written to fix, one level up.
    # **A linked worktree cannot be the source of a chamber, and it has to be refused
    # here rather than discovered later.** In a worktree, `.git` is a *file* holding
    # `gitdir: /the/main/checkout/.git/worktrees/<name>` — an absolute host path that is
    # outside the `/src` bind. The clone inside the container therefore fails with
    # "fatal: not a git repository" naming a directory the container cannot see.
    #
    # That failure lands *after* `docker run`, which is the real damage: the chamber is
    # already up, `rm` refuses it as uncollected, and the operator is left forcing away a
    # container that never held a clone. Checked before anything is created, the same
    # mistake costs one sentence.
    #
    # `.claude/worktrees/` is this project's own convention for isolated checkouts
    # (`.gitignore` says so), so sessions land here by following the rules, not by
    # misusing them. Mounting the main `.git` as a second volume would lift the
    # restriction; it is not done because a chamber pinned to a commit gets everything it
    # needs from the main checkout, and a second writable-looking git path is a boundary
    # question that should be decided deliberately rather than added in passing.
    # **A submodule root carries a `.git` file too**, so the wording covers both rather
    # than asserting "worktree" about something that is not one. The mechanism is
    # identical either way — the real git directory is elsewhere on the host and is not
    # mounted — so one refusal serves both and neither gets told a wrong story.
    #
    # The path is printed only when git actually answered. A worktree whose main checkout
    # has been deleted is a common way to arrive here, and `--git-common-dir` then fails;
    # unchecked, the refusal read "the clone would look for  inside the container", with
    # nothing between the words and no directory for the operator to go and find.
    if [ -f "${REPO_ROOT}/.git" ]; then
        main_checkout=$(git -C "$REPO_ROOT" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) ||
            main_checkout=""
        [ -n "$main_checkout" ] || main_checkout=$(sed -n 's/^gitdir: //p' "${REPO_ROOT}/.git" 2>/dev/null)
        if [ -n "$main_checkout" ]; then
            die "${REPO_ROOT}/.git is a file, not a directory — this is a linked worktree or a submodule, and a chamber cannot be cloned from one. The clone would look for ${main_checkout} inside the container, which is not mounted. Run ${0} from the checkout that owns that git directory."
        fi
        die "${REPO_ROOT}/.git is a file, not a directory — this is a linked worktree or a submodule, and a chamber cannot be cloned from one. Git could not say where its real git directory is, which usually means the checkout that owns it has been deleted. Run ${0} from a full checkout."
    fi

    acquire_lifecycle_lock "$task"

    # Resolve the base to a commit on the host, so the chamber is pinned to an exact
    # tree rather than to whatever a name meant at clone time. It is git-only, so it
    # belongs above the Docker check for the same reason `login`'s vendor check does:
    # a mistyped base should say the base is wrong, not that Docker is missing.
    base_sha=$(git -C "$REPO_ROOT" rev-parse --verify "${base}^{commit}") \
        || die "cannot resolve base '$base'"

    # A drawer that already holds a report belongs to a dispatch that already
    # happened, and `rm` deliberately keeps it — the drawer is the only surviving
    # evidence that a chamber ran. Reusing the task name therefore overwrites that
    # evidence the moment the new seat finishes, silently and with no way back:
    # `workbench/` is gitignored, so there is no history to recover it from.
    #
    # That is not hypothetical. On 2026-08-04 a session dispatched a four-seat
    # review into `rev-terra`, `rev-sonnet`, `rev-opus` and `rev-sol` — names used
    # for a different review the night before — and destroyed GPT-5.6 Terra's seat
    # from the System 02 review. The other three were rescued only because their
    # seats had not finished writing yet. Recorded in
    # `workbench/archive/2026-08-03_reviews-preserved/LOST.md`.
    #
    # CLAUDE.md hard rule 7: nothing is lost silently. So this refuses rather than
    # warns — a warning printed at the top of a dispatch that then runs for two
    # hours is a warning nobody reads.
    #
    # **Before `need_docker`, and that ordering is the point.** Its own test asserts
    # this refusal happens "before the engine is touched", and until 2026-08-05 it did
    # not: the check sat a hundred lines below, after the engine and image were
    # required. On a machine with the image built that is invisible — the image check
    # passes silently and this one fires — so the test passed locally and failed in CI,
    # where no image exists and `new` died at "image not built" instead. A refusal that
    # only works on a machine that has already done the setup is not the refusal the
    # test claims. Reading a path needs no engine, so it is checked first.
    if [ -f "$(outdir_of "$task")/report.md" ]; then
        die "chamber '${task}' would reuse an output drawer that already holds a report:
    $(outdir_of "$task")/report.md
    written $(date -r "$(outdir_of "$task")/report.md" '+%Y-%m-%d %H:%M' 2>/dev/null || echo 'at an unknown time')

  That report is the only surviving evidence of an earlier dispatch, and creating
  this chamber would overwrite it with no way back — workbench/ is gitignored.

  Pick a different task name, or move the old report aside first."
    fi

    # **Every prerequisite that only reads is checked before anything is written.**
    # These four used to sit below the snapshot, which is the first thing `new` writes
    # to the host repository, so a `new` that failed for a missing engine, an unbuilt
    # image or a name already in use left `refs/heads/autoclave/snapshot-<task>` and its
    # commit standing — and `rm` refuses a task with no container, so the one command
    # that deletes that ref could not be reached to do it.
    need_docker
    image_id=$(docker image inspect --format '{{.Id}}' "$IMAGE" 2>/dev/null) ||
        die "image not built — run: $0 build"
    [ -n "$image_id" ] || die "image '$IMAGE' has no immutable image ID"
    exists "$task" && die "chamber '$task' already exists — use 'rm $task' first"

    # Every input that defines the image is content-addressed together. Comparing the
    # label rather than mtimes works after a fresh checkout and catches a locally edited
    # Dockerfile, requirements file, brief, refresh helper, build-context policy, or the
    # fingerprint implementation itself. Refuse before creating a snapshot or container.
    expected_fingerprint=$(python3 "$FINGERPRINT") ||
        die "cannot fingerprint the chamber image inputs"
    image_fingerprint=$(docker image inspect --format \
        '{{index .Config.Labels "verbatus.harness-fingerprint"}}' "$image_id" 2>/dev/null) ||
        image_fingerprint=""
    [ "$image_fingerprint" = "$expected_fingerprint" ] ||
        die "the chamber image does not match this checkout's repository harness inputs. Rebuild: $0 build"

    # **A named vendor whose sign-in is not real is refused here.** It used to be
    # tolerated: the mount was skipped and the chamber was still labelled for that
    # vendor, so `new x claude` before `login claude` built a chamber that *said* it
    # held the Claude credential and did not. Signing in afterwards made the label true
    # and the mount no more real — mounts are fixed when the container is created — and
    # the only symptom was an authentication failure from inside the CLI at dispatch,
    # two layers down from the mistake. The vendor is asked rather than the volume
    # counted, for the reason `doctor` gives.
    #
    # Read-write, because both CLIs refresh their own tokens and a read-only mount
    # would turn a one-time sign-in into a recurring one. `none` is still the default
    # and still needs nothing: a chamber for running the suite or reading the tree
    # wants no credential at all, and that is most of them.
    auth_mounts=""
    case "$vendor" in
        claude)
            require_signed_in claude "$AUTH_VOL_CLAUDE" "$image_id"
            auth_mounts="--volume ${AUTH_VOL_CLAUDE}:${AUTH_DIR_CLAUDE}" ;;
        codex)
            require_signed_in codex "$AUTH_VOL_CODEX" "$image_id"
            auth_mounts="--volume ${AUTH_VOL_CODEX}:${AUTH_DIR_CODEX}" ;;
    esac

    # **The chamber gets what was on this machine when it was created, not what was
    # last committed.** A clone carries commits only, so uncommitted work was invisible
    # inside — an agent asked to check a change could not see the change, and this
    # session hit that twice in one day before understanding why.
    #
    # Not a live mount: a snapshot, frozen at `new` (Tyrel, 2026-08-02). The agent must
    # see one unchanging tree, and a bind mount of a directory being edited underneath
    # it is a different and much worse thing.
    #
    # Built through a temporary index so the real one is never touched — no staging is
    # disturbed, nothing is added to any commit the operator is preparing. Only when
    # the base is the default: an explicitly named base means that exact commit and
    # nothing else.
    #
    # **Built with plumbing rather than `git add -A`.** Pointing the bulk command at a
    # temporary index does leave the real index byte-identical, which is what the rule
    # is protecting — but CLAUDE.md's Branches section says "Never `git add -A`" with
    # no exception, and a line that reads as a rule violation to every future reader is
    # worth six lines to remove. `git diff --name-only` against the base gives every
    # tracked path that moved, staged or not; `ls-files --others --exclude-standard`
    # gives the untracked ones that are not ignored, so `workbench/`, `private/` and
    # `scriptorium/` stay out exactly as they do everywhere else. NUL-delimited
    # throughout, because a path may contain a newline and `-z` is the only spelling
    # that survives one.
    snapshot_ref=""
    if [ "$base" = "HEAD" ]; then
        snap_index=$(mktemp) || die "cannot create a temporary index"
        snap_paths=$(mktemp) || die "cannot create a temporary path list"
        if GIT_INDEX_FILE="$snap_index" git -C "$REPO_ROOT" read-tree "$base_sha" 2>/dev/null &&
           git -C "$REPO_ROOT" diff --name-only -z "$base_sha" >"$snap_paths" &&
           GIT_INDEX_FILE="$snap_index" git -C "$REPO_ROOT" \
               update-index --add --remove -z --stdin <"$snap_paths" 2>/dev/null &&
           git -C "$REPO_ROOT" ls-files --others --exclude-standard -z >"$snap_paths" &&
           GIT_INDEX_FILE="$snap_index" git -C "$REPO_ROOT" \
               update-index --add --remove -z --stdin <"$snap_paths" 2>/dev/null; then
            snap_tree=$(GIT_INDEX_FILE="$snap_index" git -C "$REPO_ROOT" write-tree 2>/dev/null)
        else
            snap_tree=""
        fi
        rm -f "$snap_index" "$snap_paths"
        base_tree=$(git -C "$REPO_ROOT" rev-parse "${base_sha}^{tree}")
        if [ -n "$snap_tree" ] && [ "$snap_tree" != "$base_tree" ]; then
            # Two `-m` arguments, because the second is the attribution trailer.
            # `commit-tree` runs no hooks at all, so `commit-msg` never sees this
            # message and cannot ask it for the `Co-Authored-By` line every other
            # commit in this repository carries. The agent's branch is built on top of
            # this commit and comes back with it, so an unattributed snapshot is an
            # unattributed commit in the collected history. No model wrote this tree —
            # it is the operator's own working tree, frozen by this script — and the
            # trailer says exactly that rather than naming one.
            snap=$(git -C "$REPO_ROOT" commit-tree "$snap_tree" -p "$base_sha" \
                -m "autoclave snapshot for ${task}: the working tree at chamber creation" \
                -m "Co-Authored-By: autoclave <autoclave@localhost>") \
                || die "could not record the working-tree snapshot"
            # A ref, because `git clone` fetches refs and not dangling commits. The
            # empty old-value is a compare-and-swap: create it, never overwrite it. A
            # ref already standing here means an earlier `new` for this task left one
            # behind, and silently pointing it somewhere else would lose whatever it
            # held.
            snapshot_ref="refs/heads/autoclave/snapshot-${task}"
            git -C "$REPO_ROOT" update-ref "$snapshot_ref" "$snap" "" \
                || die "${snapshot_ref} already exists. Read it, then remove it with: git -C ${REPO_ROOT} update-ref -d ${snapshot_ref}"
            # Counted from the same two commands the snapshot was built from, so the
            # number describes exactly what was captured. `status --porcelain -z`
            # would have been simpler and wrong: it emits a rename as *two*
            # NUL-terminated fields, so counting NUL bytes counted every rename twice.
            #
            # **Counted before `base_sha` moves onto the snapshot**, which is the whole
            # reason these two lines are in this order. Diffing the working tree against
            # the snapshot compares it with itself, so the count was always `0` — and
            # it printed `0 path(s)` directly underneath a line saying the tree was not
            # clean, which is the shape of a number nobody trusts again.
            dirty=$(( $(git -C "$REPO_ROOT" diff --name-only -z "$base_sha" |
                            tr -cd '\000' | wc -c) +
                      $(git -C "$REPO_ROOT" ls-files --others --exclude-standard -z |
                            tr -cd '\000' | wc -c) ))
            base_sha="$snap"
            note "working tree is not clean — the chamber gets a snapshot of it"
            note "  ${dirty} path(s) beyond ${base}, captured now and frozen"
        fi
    fi

    outdir=$(outdir_of "$task")

    mkdir -p "$outdir"

    # --network none would be the honest default, but the agent CLIs need to
    # reach their model provider. Egress is therefore open and that is a stated
    # limit, not an oversight: see the README. What is *not* open is any route
    # to write the host — /src is read-only. It is readable, though, except for the
    # three masked drawers below. The vendor's auth volume, when there is one, was
    # settled above.
    #
    # `/src` is read-only, which stops an agent *changing* this machine and does
    # nothing about it *reading* this machine. Egress is open, so a readable secret
    # is a sendable one. Empty read-only tmpfs mounts cover the three drawers that
    # hold things a chamber has no business reading:
    #
    #   private/     the notification bearer topic
    #   workbench/   every handoff, note, ledger and reviewer transcript — and the
    #                guard's own `SECRET_DRAWERS` names it a place secrets may live,
    #                so masking `private/` alone left half the rule unenforced
    #   scriptorium/ run trees
    #
    # The directories still exist and are empty, so anything walking the tree finds
    # what it expects. None of the three is in the objects `git clone` reads — all
    # are gitignored — so `/work` is unaffected. `workbench/autoclave/<task>` reaches
    # the agent separately as `/out`, which is a different mount and still writable.
    #
    # The mountpoints are created first. Docker builds a missing mountpoint inside
    # the bind, and `/src` is read-only, so a clone without these directories failed
    # at `docker run` with "read-only file system" — which is every fresh clone,
    # since all three are gitignored. Verified: exit 125 before this line existed.
    mkdir -p "${REPO_ROOT}/private" "${REPO_ROOT}/workbench" "${REPO_ROOT}/scriptorium"

    # **A chamber could not read the spec it was being asked to build from.** The
    # specs live in `workbench/design/`, inside the drawer masked immediately above,
    # and they are gitignored — so they were absent from `/src` twice over and a brief
    # had to carry a whole spec as prose. That is why `workbench/design` reaches the
    # chamber here as its own mount rather than by unmasking the drawer: the masking
    # rule's stated subject is handoffs, notes, ledgers and reviewer transcripts, and
    # none of those moves. The specs are the one thing in there an agent is *supposed*
    # to build from.
    #
    # A frozen copy, for the same reason `/src` is a snapshot: a session that repairs
    # a spec while a chamber is reading it would otherwise change the tree underneath
    # the agent. Copied at `new`, removed by `rm`.
    #
    # **Writable, and that is a correction.** It was read-only for one sitting, on the
    # reasoning that an agent "has no business editing the specs" — which is tidiness,
    # not containment, and the two are not the same rule. `/src` is read-only because it
    # is really this machine's tree; writing it would change the host with no diff to
    # review. This is a per-chamber copy that `rm` deletes, so nothing it holds is
    # anybody's original.
    # Locking it cost a real dispatch (Tyrel, 2026-08-03): a builder told by its spec to
    # record resolved model revisions in the bench memo could not, because this mount
    # refused a write nothing needed refused. A chamber has free rein over what is its
    # own; the boundary is the host, not the agent's judgement.
    #
    # A failure here leaves no chamber, so `rm` — which requires one — cannot clean up
    # after it. The snapshot ref already exists by this point, and an orphaned ref makes
    # the *next* `new` for this task fail at `update-ref`. So staging unwinds the ref the
    # same way the `docker run` failure below does. Found by CodeRabbit on pull request 16.
    specs_stage="${REPO_ROOT}/workbench/autoclave/.specs/${task}"
    stage_failed() {
        rm -rf "$specs_stage"
        [ -n "$snapshot_ref" ] &&
            git -C "$REPO_ROOT" update-ref -d "$snapshot_ref" 2>/dev/null
        die "$1${snapshot_ref:+ — the snapshot ref was removed}"
    }
    rm -rf "$specs_stage"
    mkdir -p "$specs_stage" || stage_failed "could not create the spec staging directory"
    if [ -d "${REPO_ROOT}/workbench/design" ]; then
        cp -R "${REPO_ROOT}/workbench/design/." "${specs_stage}/" ||
            stage_failed "could not stage workbench/design for the chamber"
    fi

    # **The window onto the old pipeline is off, and reaching for it is now deliberate.**
    # It was mounted by default from `operations/autoclave/window.conf` between
    # 2026-08-04 and 2026-08-20, because a chamber that could not see the old code had
    # once decided to refuse PDFs outright while the old pipeline had accepted them at
    # stage one all along.
    #
    # **Tyrel ruled on 2026-08-20 that the reference is no longer needed** — the rebuild
    # is planned from documents now, not from reading the old tree — so `window.conf`,
    # the per-directory `/window` mounts and the `/stage` audit-notes mount are gone. A
    # chamber gets `/work`, `/src`, `/out` and `/specs`, and nothing of the old system.
    #
    # `AUTOCLAVE_WINDOW` survives as the single opt-in below, and it is the only way in.
    # CLAUDE.md's Quarantine section and `cleanroom/README.md` still govern what may
    # cross if a session ever sets it; what changed is that no session gets the window
    # by accident, and reinstating it is an environment variable rather than an edit.
    window_mount=""
    if [ -n "${AUTOCLAVE_WINDOW:-}" ]; then
        case "$AUTOCLAVE_WINDOW" in
            /*) : ;;
            *) die "AUTOCLAVE_WINDOW must be an absolute path, not '${AUTOCLAVE_WINDOW}'" ;;
        esac
        case "$AUTOCLAVE_WINDOW" in
            *[[:space:]]*) die "AUTOCLAVE_WINDOW must not contain whitespace — this script splits the flag list on it" ;;
        esac
        # A colon is the separator in Docker's short `--volume src:dst:opts` form, so a
        # path containing one is read as three fields the operator never wrote. Refusing
        # it here says which path is wrong; letting it through produces an
        # invalid-volume-specification error naming a mount nobody asked for.
        case "$AUTOCLAVE_WINDOW" in
            *:*) die "AUTOCLAVE_WINDOW must not contain ':' — Docker reads it as a volume-spec separator" ;;
        esac
        [ -d "$AUTOCLAVE_WINDOW" ] ||
            die "AUTOCLAVE_WINDOW names '${AUTOCLAVE_WINDOW}', which is not a directory"
        window_mount="${window_mount} --volume ${AUTOCLAVE_WINDOW}:/window:ro"
        note "window open: ${AUTOCLAVE_WINDOW} is readable at /window. Reference — what you take from it is cited, never carried across silently."
    fi
    #
    # Word splitting on $auth_mounts is the point: it is a flag list this script
    # built from two fixed constants, never from user input.
    # shellcheck disable=SC2086
    docker run --detach \
        --name "$(container_of "$task")" \
        --label "verbatus.autoclave=1" \
        --label "verbatus.task=${task}" \
        --label "verbatus.vendor=${vendor}" \
        --label "verbatus.base=${base_sha}" \
        --volume "${REPO_ROOT}:/src:ro" \
        --tmpfs /src/private:ro,size=4k \
        --tmpfs /src/workbench:ro,size=4k \
        --tmpfs /src/scriptorium:ro,size=4k \
        --volume "${outdir}:/out" \
        --volume "${specs_stage}:/specs" \
        $auth_mounts $window_mount \
        --env "CLAUDE_CONFIG_DIR=${AUTH_DIR_CLAUDE}" \
        --workdir /work \
        "$image_id" \
        sleep infinity >/dev/null || {
        # The one failure past the snapshot that can leave a ref standing. `rm`
        # refuses a task with no container, so nothing else would ever delete it.
        [ -n "$snapshot_ref" ] &&
            git -C "$REPO_ROOT" update-ref -d "$snapshot_ref" 2>/dev/null
        rm -rf "$specs_stage"
        die "docker run failed — no chamber was created${snapshot_ref:+, and the snapshot ref was removed}"
    }

    # Clone from the read-only mount. `--no-local` is deliberate: a local clone
    # hardlinks into /src/.git, and hardlinks into a read-only mount are exactly
    # the kind of shared state this arrangement exists to avoid.
    #
    # **The script crosses as stdin, under a quoted here-document, and the two values
    # it needs cross as environment variables.** It used to be one double-quoted
    # argument with the base commit interpolated into it, which meant the *host* shell
    # expanded the whole block first — every `$(…)` and every backtick in it, comments
    # included. A backtick in one of these comments ran on the host once already and
    # the fix was a note telling the next editor not to write one. `<<'AUTOCLAVE_SETUP'`
    # is quoted, so nothing in here is expanded here; `sh -s` reads it from stdin,
    # which is what `docker exec -i` attaches.
    #
    # The `if !` matters and is not a style choice. A here-document's body begins at
    # the next newline whatever else is unfinished on the line, so writing this as
    # `docker exec … <<'X' ||` and a `die` on the following line puts that `die` inside
    # the script sent to the container and hands the `||` to whatever follows the
    # terminator — a failed setup would then run the line that says the chamber is up.
    if ! docker exec -e AC_BASE_SHA="$base_sha" -e AC_TASK="$task" \
        --interactive "$(container_of "$task")" sh -s <<'AUTOCLAVE_SETUP'
        set -eu
        # Nothing in this block is expanded on the host, and the line above proves it:
        # $(printf host-expansion-canary) and `printf host-expansion-canary` reach the
        # container as the characters written here. A test reads the recorded script
        # back and refuses it if either has been evaluated — pinning the values alone
        # would pass for an unquoted delimiter with the two variables escaped, which is
        # exactly the "fix" that reopens this.
        git clone --no-local --quiet /src /work
        cd /work
        git checkout --quiet "$AC_BASE_SHA"
        git switch --quiet -c "agent/$AC_TASK"
        cp /opt/autoclave/CLAUDE.md /work/AUTOCLAVE.md
        # /work/AGENTS.md is the one Codex actually reads. A copy at the filesystem
        # root is not enough: probed on 2026-08-02, a Codex seat reported that no
        # instruction file had been loaded automatically at all, and that /AGENTS.md
        # existed but was not in its context. Discovery starts at the working
        # directory, which is /work. Every Codex agent dispatched into a chamber
        # before this — including that day's audit and review seats — ran with no
        # standing limits beyond whatever its prompt carried.
        cp /opt/autoclave/CLAUDE.md /work/AGENTS.md
        # The brief is scaffolding, not the agent's work, and it must not read as
        # either. Untracked at the repository root it is 'stray documentation' to
        # `.githooks/doc-allowlist.sh`, so `check-static.sh` failed inside every
        # chamber — on a file the agent did not write and cannot correct. An agent
        # told to run the gate before handing work back either reports itself blocked
        # or deletes its own brief to make the gate pass. Excluded in the clone rather
        # than added to the tracked `.gitignore`, because this file exists only here.
        printf '/AUTOCLAVE.md\n/AGENTS.md\n' >> /work/.git/info/exclude
        git config user.name  'autoclave'
        git config user.email 'autoclave@localhost'
        # **The hooks are switched on in here.** `core.hooksPath` is local to a clone
        # and never travels, so a fresh chamber had every git-hook rule off — silently,
        # which violates the repository branch and attribution rules.
        # The one that matters most here is `commit-msg`: without it nothing asked a
        # chamber commit for the `Co-Authored-By` line naming the model that wrote it,
        # and an agent could hand back forty unattributed commits before anyone looked.
        # Set directly rather than through `install.sh`, which also chmods and creates
        # drawers: the hooks are tracked executable, so the setting is the whole of what
        # a clone needs.
        git config core.hooksPath .githooks
AUTOCLAVE_SETUP
    then
        die "chamber started but setup failed — inspect with: docker logs $(container_of "$task")"
    fi

    # **The subagent spawn-depth cap is lifted in here**, in its own single-quoted
    # command so the JSON's quotes are not fighting the interpolation above.
    #
    # The clone carries this repository's agent settings, and the cap among them
    # exists so fan-out on Tyrel's machine stays visible to the session running it.
    # That reason does not exist in a chamber: nothing an agent spawns can reach the
    # host, and the container is already the bound. Inheriting it meant a chamber
    # agent asked to orchestrate was limited by a number chosen for somewhere else.
    #
    # `settings.local.json` takes precedence and is gitignored, so the tracked tree
    # stays clean and `collect` still sees an untouched checkout.
    docker exec -e AC_VENDOR="$vendor" "$(container_of "$task")" sh -c '
        # Fail chamber creation if either the local settings or trust update fails.
        set -eu

        printf "%s\n" \
            "{" \
            "  \"env\": {" \
            "    \"CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH\": \"64\"" \
            "  }" \
            "}" > /work/.claude/settings.local.json

        [ "$AC_VENDOR" = claude ] || exit 0

        # **The workspace is trusted, because the chamber *is* the trust boundary.**
        # Without this Claude Code prints "this workspace has not been trusted" and
        # silently ignores every `permissions.allow` entry in the project settings —
        # so a chamber discarded the very settings it was given, including the cap
        # lifted immediately above. There is nobody in here to accept a trust dialog,
        # and the file it wants did not exist at all; only a backup of it did. Codex
        # has carried its own `trust_level = "trusted"` since sign-in, so this is the
        # Claude half of a thing already true for the other vendor.
        #
        # No apostrophe may appear anywhere in this block. It is one long single-quoted
        # argument, so a word like "the sign-in-s own config" ends the string in the
        # middle of a comment and silently swallows every function defined after it —
        # which is exactly what it did, once, while this fix was being written.
        # The trust flag goes where `CLAUDE_CONFIG_DIR` makes the CLI actually look —
        # inside the mounted directory. Written to the path beside it, as it was, the
        # flag is set in a file nothing reads and the trust dialog blocks a detached
        # chamber with no one there to answer it.
        # **Published by temp-then-rename, because this file is now shared and live.**
        # `/home/agent/.claude` is the credential volume every Claude chamber mounts, and
        # `CLAUDE_CONFIG_DIR` has just made `.claude.json` the configuration the CLI
        # refreshes against rather than a throwaway. A plain read-modify-write here races
        # two ways: a refresh from a concurrent chamber landing between the read and the
        # write is discarded, and a reader can see a truncated file. The first cut of
        # this fix broke it on the one file it had just made load-bearing.
        #
        # No apostrophe may appear in this block, as the warning above it says. The first
        # cut of this comment used one and silently swallowed every function defined after
        # it — the same failure that warning was written about, in the block it guards.
        # Guarded on the directory existing rather than creating it, the same way the
        # save below is. A chamber built without a vendor — the default, and most of
        # them — has no mounted directory here, and there is no trust flag worth writing
        # into a path the CLI will never read.
        #
        # **Locked, and written only when the flag is actually missing.** Temp-then-rename
        # stopped the torn read but not a lost update. The login booth, refresh helper,
        # and this trust initializer join the same volume lock. Status and model runs do
        # not: locking a whole inference run serialized every Claude chamber. The CLI is
        # therefore an external writer; refresh detects a change before publication, but
        # no lock invented here can make a vendor process participate in that protocol.
        #
        # The wait is bounded and loud rather than indefinite. A chamber creation that
        # hangs forever on a lock somebody wedged is worse than one that says why it
        # stopped, and there is nobody in here to notice the difference.
        python3 - <<"PY"
import fcntl, json, os, pathlib, tempfile, time
d = pathlib.Path("/home/agent/.claude")
if d.is_dir():
    p = d / ".claude.json"
    with open(d / ".credentials.autoclave.lock", "a+") as guard:
        deadline = time.monotonic() + 30
        while True:
            try:
                fcntl.flock(guard.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("another chamber held the shared config lock for 30s")
                time.sleep(0.05)
        contents = p.read_text() if p.exists() else ""
        config = json.loads(contents) if contents.strip() else {}
        work = config.setdefault("projects", {}).setdefault("/work", {})
        if work.get("hasTrustDialogAccepted") is not True:
            work["hasTrustDialogAccepted"] = True
            # A container pid is not unique across chambers, and rename is atomic only
            # within one filesystem; use a unique temp in the destination directory.
            fd, tmp = tempfile.mkstemp(dir=str(d), prefix=".claude.json.tmp.")
            try:
                with os.fdopen(fd, "w") as handle:
                    handle.write(json.dumps(config, indent=2))
                os.replace(tmp, p)
            except BaseException:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
PY
    ' || die "chamber started but the agent configuration could not be written"

    note "chamber '${task}' is up"
    note "  base:   ${base_sha}"
    note "  branch: agent/${task}"
    note "  output: ${outdir}"
    note ""
    note "  shell:   $0 shell ${task}"
    note "  collect: $0 collect ${task}"
}

# `shell` and `exec` deliberately stay outside the lifecycle lock, on the same side as
# `report`. `dispatch` holds that lock for the entire agent run, and a Claude dispatch
# buffers all its output until it exits, so taking the lock here meant that for the hours
# an agent was working the only two commands that could show what it was doing answered
# "task is already active" -- and told the operator to remove the lock, which is the one
# action that breaks the serialisation the lock exists to provide. Neither command
# mutates lifecycle state: they attach to a container that is already running.
cmd_shell() {
    task="${1:-}"; check_task "$task"
    need_docker
    running "$task" || die "chamber '$task' is not running"
    docker exec -it "$(container_of "$task")" /bin/bash
}

cmd_exec() {
    task="${1:-}"; check_task "$task"; shift
    [ "$#" -gt 0 ] || die "a command is required"
    need_docker
    running "$task" || die "chamber '$task' is not running"
    docker exec "$(container_of "$task")" "$@"
}

# **The model that writes a chamber commit is the author of it.** `new` sets a
# placeholder identity — `git config user.name 'autoclave'`, `user.email
# 'autoclave@localhost'` — because the chamber is created before any model is
# chosen, and something has to be there for the clone to commit at all. Left alone,
# that placeholder is what `commit-msg` records as the author of every line the
# agent writes. Tyrel ruled on 2026-08-11: "Generally I don't think autoclave
# should ever be an author on any code it should be the models who wrote it." So
# the identity is restamped here, at dispatch — the first moment the model is
# known — immediately before its CLI runs, in both vendor arms below.
#
# The address is chosen by the validated `$vendor` argument, never inferred from
# the model's name — a name pattern misattributes the first model whose spelling
# crosses vendors. `.githooks/commit-msg` documents these two exact no-reply
# addresses for the `Co-Authored-By` trailer, and the author line has to agree.
stamp_author() {
    stamp_task="$1"
    stamp_vendor="$2"
    stamp_model="$3"
    case "$stamp_vendor" in
        claude) stamp_addr='noreply@anthropic.com' ;;
        codex) stamp_addr='noreply@openai.com' ;;
        *) die "cannot stamp an author for unknown vendor '$stamp_vendor'" ;;
    esac
    docker exec "$(container_of "$stamp_task")" sh -c \
        "set -eu
        cd /work
        git config user.name '$stamp_model'
        git config user.email '$stamp_addr'" \
        || die "could not stamp '$stamp_model' as the commit author in chamber '$stamp_task'"
}

cmd_dispatch() {
    task="${1:-}"; check_task "$task"
    # **`AUTOCLAVE_WINDOW` is a `new`-time variable, and setting it here is refused rather
    # than ignored.** Mounts are fixed when the container is created, so the variable does
    # nothing at this point — and a no-op is the wrong answer to an operator who has just
    # said, in the only way the launcher offers, that this agent needs the old pipeline.
    #
    # The failure it prevents is quiet and expensive: `rebuilder.md` tells an agent to read
    # `/window`, the agent finds no such path, and an agent that has been told the reference
    # exists reasons around its absence far more often than it stops and reports it. The
    # chamber then returns work that reads as informed by the old system and is not.
    #
    # Named for the phase rather than the variable, because the operator's intent was right
    # and only the timing was wrong: the chamber has to be made again, not patched.
    if [ -n "${AUTOCLAVE_WINDOW:-}" ]; then
        die "AUTOCLAVE_WINDOW is set, but a window can only be opened when the chamber is created — this chamber's mounts are already fixed. Destroy it and run 'new' with AUTOCLAVE_WINDOW set, or unset it to dispatch without a window."
    fi
    vendor="${2:-}"
    brief="${3:-}"
    # **The model is required, and that is the point.** An omitted model runs whatever
    # the vendor's CLI happens to default to — here, `codex doctor` reports that is
    # `gpt-5.6-sol`, the most expensive seat OpenAI sells. Every chamber would have
    # silently been a flagship chamber, and the whole tier structure in
    # `.claude/agents/README.md` would have described a choice nothing ever made.
    # Naming it is how a bounded unit gets a bounded seat.
    model="${4:-}"
    # Effort defaults to medium, which is Tyrel's ruling (2026-08-01) and is enforced
    # here rather than left to whoever writes the dispatch line.
    effort="${5:-medium}"

    # **Every argument is judged before anything is touched.** A typo in a model name
    # should cost a line of output, not a container's startup and a confusing failure
    # from a vendor CLI two layers down. The same ordering is already pinned for `new`.
    # Checked left to right, in the order the arguments are written, so the first
    # complaint names the first thing wrong rather than the first thing this function
    # happened to look at.
    [ -n "$brief" ] ||
        die "usage: $0 dispatch <task> <claude|codex> <brief-file> <model> [effort]"
    case "$vendor" in
        claude|codex) : ;;
        *) die "dispatch takes 'claude' or 'codex'" ;;
    esac
    [ -f "$brief" ] || die "no brief at '$brief'"
    [ -n "$model" ] || die "dispatch needs a model — see .claude/agents/README.md for which"
    # It reaches the container as an environment variable and never as interpolated
    # text, but a value beginning with `-` would still arrive as a flag.
    case "$model" in
        -*|*[!A-Za-z0-9._-]*) die "'$model' is not a plain model name" ;;
    esac
    # **Effort is judged against the vendor and the model, not against a vocabulary.**
    # `none minimal low medium high xhigh max` is the union of what the two vendors
    # accept, and no vendor accepts all seven. `.claude/agents/README.md` records
    # the corresponding dispatch table: `minimal` is rejected by
    # every Codex model; `gpt-5.3-codex-spark` also rejects `none` and `max`, leaving it
    # `low`–`xhigh`; Claude accepts `low`, `medium`, `high`, `xhigh`, `max` and nothing
    # else. Checking the union let a value that file records as unreachable through the
    # validation whose entire purpose is that a bad argument costs a line of output —
    # and it failed instead inside a vendor CLI, after a chamber and a model process
    # had been touched, in a message naming neither the argument nor this script.
    #
    # An unmeasured Codex model gets the general Codex range rather than a refusal.
    # That is the honest answer for a model this table has not measured, and it is why
    # the model name is not checked against a list of its own: a launcher that refuses
    # every seat added after it was written is a launcher somebody edits under time
    # pressure to get a dispatch out.
    # **`ultra` is Sol's and Terra's alone.** Codex's own model catalog on this machine
    # (`models_cache.json`, under its config directory — named without a path here
    # because the credential guard in `operations/test_autoclave.py` refuses that
    # directory's spelling anywhere in this file, and rightly so) lists `ultra` under
    # `supported_reasoning_levels` for `gpt-5.6-sol` and `gpt-5.6-terra` and nothing
    # else; its
    # description is "Maximum reasoning with automatic task delegation", one step above
    # `max`. Measured against a live dispatch on 2026-08-03: Terra at `ultra` ran and
    # echoed `reasoning effort: ultra` back.
    #
    # Refusing `ultra` for Claude is the load-bearing half. `claude --effort ultra` does
    # not fail — it prints "Unknown --effort value 'ultra' — ignoring it and using the
    # default effort" and carries on at the *default*, which is not even `max`. A Claude
    # chamber buffers all output until it exits, so that warning would surface hours
    # later beside work that had silently run shallow.
    #
    # **Claude's own orchestrating level is `ultracode`, and it is a real effort value.**
    # An earlier draft of this comment said it was only a keyword to write into a brief.
    # It is not: measured against the CLI in the image on 2026-08-03, `--effort ultracode`
    # is accepted **silently**, while `--effort ultra` and `--effort banana` both draw the
    # unknown-value warning. The CLI distinguishes it from a typo. It is absent from
    # `--help`, which lists only low/medium/high/xhigh/max — which is exactly why it has
    # to be recorded somewhere that is checked, and why this list is that place.
    case "$vendor" in
        claude) reachable="low medium high xhigh max ultracode" ;;
        codex)
            case "$model" in
                gpt-5.3-codex-spark) reachable="low medium high xhigh" ;;
                gpt-5.6-sol|gpt-5.6-terra) reachable="none low medium high xhigh max ultra" ;;
                *) reachable="none low medium high xhigh max" ;;
            esac ;;
    esac
    case " ${reachable} " in
        *" ${effort} "*) : ;;
        *) die "'$effort' is not an allowed effort for ${vendor} '${model}' — it takes: ${reachable}" ;;
    esac

    # The shared lifecycle lock protects chamber state, not argument validation.
    # In particular, a bad model or effort must name that bad argument even when
    # another dispatch is active for this task.
    acquire_lifecycle_lock "$task"

    need_docker
    running "$task" || die "chamber '$task' is not running — start it with: $0 new $task"

    # A chamber holds one vendor's credential, chosen when it was created, because a
    # mount cannot be added to a running container. Refusing here is what makes that
    # choice mean something: without it a chamber built for one vendor would be
    # dispatched to the other and fail deep inside the CLI with an auth error.
    chamber_vendor=$(docker inspect --format '{{index .Config.Labels "verbatus.vendor"}}' \
        "$(container_of "$task")" 2>/dev/null) || chamber_vendor=""
    [ "$chamber_vendor" = "$vendor" ] || die \
        "chamber '$task' holds no ${vendor} credential (it was created for '${chamber_vendor:-none}'). Create one with: $0 new <task> <base> ${vendor}"

    # **The label says which credential was mounted; this asks whether it still
    # works.** `new` checked the sign-in when the chamber was built, and a chamber can
    # outlive it — a token expires, a vendor forces a re-auth, an earlier agent
    # rewrote the configuration directory it shares with every later chamber of the
    # same vendor. Asking inside the chamber, through the mount the CLI will actually
    # read, is the only test that covers all three. It costs one `docker exec` against
    # a container that is already running. Claude is refreshed first because its print
    # mode does not do that itself and auth status alone accepts expired credentials.
    if [ "$vendor" = claude ]; then
        # Same three-way split as `require_signed_in`: a transient endpoint failure
        # during dispatch must not read as a lost sign-in.
        refreshed=0; refresh_claude_chamber "$task" || refreshed=$?
        [ "$refreshed" -eq 0 ] || claude_refresh_failure "$refreshed"
    fi
    check=$(auth_check "$vendor")
    docker exec "$(container_of "$task")" sh -c "$check" >/dev/null 2>&1 || die \
        "'$vendor' is not signed in inside chamber '$task' — sign in with '$0 login $vendor', then rebuild the chamber ('$0 rm $task' and '$0 new $task <base> $vendor'), because a mount cannot be added to a running container"

    # The brief travels as a file through the scratch drawer, never as a shell
    # argument. A brief is prose written by a session; interpolating it into a
    # command line makes its punctuation executable.
    #
    # `/out` is a host directory the running agent can write, and an agent that
    # replaces `brief.md` with a symlink turns a `cp` into a write through it, onto any
    # file this user can write. Testing the slot and then copying into it does not fix
    # that: the agent is running while both happen and can swap the path in between,
    # so the test passes and the thing it tested is not what was written to. The helper
    # opens the destination once with `O_NOFOLLOW` and writes through that descriptor,
    # which closes the link and the race together, and refuses a directory or a FIFO on
    # the way. The `[ -L ]` test stays in front of it because it says what is wrong in
    # a sentence, where the helper says only that it refused.
    brief_in="$(outdir_of "$task")/brief.md"
    [ -L "$brief_in" ] && die "brief slot ${brief_in} is a symlink — refusing to write through it"
    python3 "$SAFE_FILE" write "$brief" "$brief_in" "$MAX_BRIEF_BYTES" ||
        die "brief copy from ${brief} to ${brief_in} was refused — the source or slot is not a safe regular file reachable without an agent-controlled symlink"

    note "dispatching ${vendor} into '${task}'"
    note "  brief:  $(outdir_of "$task")/brief.md"
    note "  report: $(outdir_of "$task")/report.md"
    note "  model:  ${model}, effort ${effort}"
    note ""

    # Model and effort reach the container as environment variables, for the same
    # reason the brief travels as a file: a value interpolated into a quoted command
    # line brings its punctuation with it.
    #
    # **The brief is each CLI's standard input, not an argument.** It used to be
    # `"$(cat /out/brief.md)"` — quoted, so its punctuation was inert, which is what
    # made the arrangement safe and is all the old claim ever established. Three things
    # were still true of it: a brief over `MAX_ARG_STRLEN` (128 KiB on Linux) fails the
    # exec outright, command substitution strips every trailing newline, and the whole
    # brief is visible in the container's process list to anything else running there.
    # Both CLIs document the file form and both were run to confirm it: `claude -p`
    # with no prompt argument reads stdin ("Input must be provided either through stdin
    # or as a prompt argument" is its own error when neither is given), and `codex
    # exec`'s help says a prompt of `-` means the instructions are read from stdin.
    # A regular file also gives an immediate EOF, which is what `< /dev/null` was for:
    # `codex exec` waits forever on an open stdin with nothing attached.
    #
    # The two vendors spell effort differently and neither spelling is guessable.
    # `claude` takes `--effort <level>`. `codex exec` has no effort flag at all — it
    # is a config override, `-c model_reasoning_effort=<level>`. Verified against
    # `--help` on both CLIs rather than assumed.
    dispatch_status=0

    case "$vendor" in
        claude)
            # --dangerously-skip-permissions is correct *here* and nowhere else:
            # the container is the boundary, so there is no host left to protect
            # by prompting, and a prompt inside a detached container is a hang.
            # This flag is the reason the chamber exists.
            #
            # **`--append-system-prompt-file` is how the chamber's limits reach a
            # Claude agent, and it is the only channel `/work/CLAUDE.md` cannot
            # outrank.** The in-tree copies of the brief are files the agent has
            # to choose to read, and `/work/CLAUDE.md` — this repository's own rules,
            # written for the host — loads automatically beside them. A seat that read
            # both applied the host's rules to itself and refused to orchestrate.
            # The system prompt is not a file it can rank; it is what it is.
            # Codex has no equivalent flag, and does not need one: it reads
            # `/work/AGENTS.md`, which is the same bytes, and it was probed doing so.
            # `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0` because the default is a real
            # kill and it silently cost a lane. In `-p` mode the CLI waits a bounded
            # time for its own background tasks and then **terminates them and exits**.
            # An orchestrating seat that fans out — which is the whole reason to run at
            # `ultracode` — reaches that ceiling as a matter of course: a round-two lane
            # spawned a 28-agent join audit, hit the default at 600 seconds, killed its
            # own workflow and returned having committed nothing. The dispatch exited 0
            # and `collect` reported NO COMMITS, so the failure looked like a model that
            # did no work rather than a harness that stopped it.
            #
            # This is the case `.claude/agents/README.md` singles out under "no agent
            # gets a time limit": a real kill is not a deadline, and a killed seat loses
            # its report entirely rather than handing back a shorter one. The rule there
            # is to tell a seat a limit that genuinely exists — but this one buys
            # nothing, so it is removed instead. Zero means wait indefinitely; the
            # chamber is already the bound on how long anything may run.
            #
            # Codex needs no counterpart: it streams and has no such ceiling.
            # **`CLAUDE_CONFIG_DIR` is what actually fixes the two-file split**, and it
            # replaces the copy-in/copy-out above rather than joining it. The CLI keeps
            # `.claude.json`, `backups/`, `projects/` and `sessions/` inside this
            # directory instead of beside it — probed on 2026-08-10 against CLI 2.1.226
            # in this image, not taken from documentation. Pointed at the mounted volume,
            # every file a refresh touches is persistent by construction, which is why
            # Codex never had this bug: its whole configuration was already inside its
            # own mount.
            #
            # Before this, the refresh had **never once succeeded** in the credential
            # volume: `.credentials.json` still carried the mtime of the last manual
            # sign-in, and `backups/.claude.json.backup.*` held 50-byte stubs — the CLI
            # backing up the empty configuration it had just been handed. Seven sign-ins
            # in a week, each dying at its first refresh, roughly eight hours in, while
            # the refresh token itself stayed valid for weeks.
            #
            stamp_author "$task" "$vendor" "$model"
            docker exec -e AC_MODEL="$model" -e AC_EFFORT="$effort" \
                -e CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0 \
                -e CLAUDE_CONFIG_DIR="$AUTH_DIR_CLAUDE" \
                "$(container_of "$task")" sh -c '
                cd /work
                claude --dangerously-skip-permissions \
                    --append-system-prompt-file /opt/autoclave/CLAUDE.md \
                    --model "$AC_MODEL" \
                    --effort "$AC_EFFORT" \
                    -p < /out/brief.md
            ' || dispatch_status=$?
            ;;
        codex)
            # stdin is a finite regular file, which is what the old `< /dev/null`
            # protected against: `codex exec` waits forever on an open stdin when
            # nothing is attached, and inside a detached container that is a dispatch
            # that never returns and never says why. A file gives the prompt and then
            # an immediate EOF.
            #
            # `--dangerously-bypass-approvals-and-sandbox` is the exact counterpart of
            # the Claude flag above, and its own help text says where it belongs:
            # "intended solely for running in environments that are externally
            # sandboxed". A chamber is that environment. Without it Codex tries to
            # build its own `bwrap` sandbox inside the container, which needs
            # unprivileged user namespaces Docker does not grant, and **every**
            # command the agent runs fails before it starts — including reading a
            # file. The first real dispatch produced exactly that and reported itself
            # blocked rather than pretending; the alternative fix, granting the
            # container the privileges bwrap wants, would weaken the one boundary
            # this whole arrangement rests on to restore a second one inside it.
            #
            # `--` before the prompt so a brief beginning with a dash is read as the
            # prompt rather than as an unknown option, and `-` as the prompt because
            # that is how `codex exec` spells "read the instructions from stdin".
            stamp_author "$task" "$vendor" "$model"
            docker exec -e AC_MODEL="$model" -e AC_EFFORT="$effort" \
                "$(container_of "$task")" sh -c '
                cd /work
                codex exec --skip-git-repo-check \
                    --dangerously-bypass-approvals-and-sandbox \
                    -m "$AC_MODEL" \
                    -c "model_reasoning_effort=$AC_EFFORT" \
                    -- - < /out/brief.md
            ' || dispatch_status=$? ;;
    esac

    note ""
    note "dispatch returned. Nothing has been collected and nothing merged."
    [ "$dispatch_status" -eq 0 ] || note "  the CLI exited ${dispatch_status} — read the output above before trusting anything below"
    note "  what it wrote:  $0 collect ${task}"
    note "  what it said:   $0 report ${task}"
    return "$dispatch_status"
}

cmd_collect() {
    task="${1:-}"; check_task "$task"
    acquire_lifecycle_lock "$task"
    need_docker
    running "$task" || die "chamber '$task' is not running"

    branch="agent/${task}"
    bundle_in="/out/${task}.bundle"
    bundle_out="$(outdir_of "$task")/${task}.bundle"

    chamber_base=$(docker inspect --format '{{index .Config.Labels "verbatus.base"}}' \
        "$(container_of "$task")" 2>/dev/null) || chamber_base=""
    [ -n "$chamber_base" ] || die "chamber '$task' records no base, so nothing here can say what it added"

    # **Work that was never added counts as uncommitted.** A bundle carries commits, so
    # anything still loose in the chamber's tree is left behind — and `rm` then destroys
    # the only copy of it. `git diff` and `git diff --cached` between them see modified
    # files and staged files and say nothing whatsoever about an untracked one, which is
    # the shape most of a chamber's output takes: a new module, a new test, a report
    # written into the tree. So the refusal read as a guarantee of a clean tree and was
    # not one. `status --porcelain` sees all three in one listing, and that listing is
    # what the refusal prints, so the operator is told which paths are at risk rather
    # than that something somewhere is dirty.
    #
    # The two brief copies the launcher writes into the clone are in
    # `.git/info/exclude`, so they are not untracked work and do not trip this.
    docker exec "$(container_of "$task")" sh -c "
        set -eu
        cd /work
        loose=\$(git status --porcelain)
        if [ -n \"\$loose\" ]; then
            echo 'autoclave: uncommitted or untracked work in the chamber' >&2
            printf '%s\n' \"\$loose\" >&2
            exit 1
        fi
        python3 /src/operations/autoclave/safe_file.py bundle ${bundle_in} '${branch}'
    " || die "nothing collected from '$task' — the tree is not clean, or the bundle could not be written; its own message is above. A branch carrying no commits is not this failure: that collects, and says so."

    # **This slot is the agent's too, and `[ -f ]` is not enough.** Three reviews
    # called it harmless on the grounds that `git fetch` only reads. It does only
    # read — and git will read a *gitfile*: a one-line `gitdir: /some/other/repo/.git`
    # is a perfectly ordinary regular file, and `git fetch` on it fetches from that
    # repository. An agent that leaves one here turns `collect` into "import any
    # repository on this machine, and report it as the work I did". Reproduced: a
    # private repository's branch arrived on `agent/<task>` and `collect` said
    # `1 commit(s)`.
    #
    # Verifying the slot and then fetching the same path is still two reads of a live
    # agent-controlled name. The agent can replace a verified bundle between them.
    # `safe_file.py` opens that name once without following a symlink; its bytes go
    # into a host-owned file under git's common directory. Verification and fetch use
    # that one snapshot only. A linked worktree has `.git` as a file, hence
    # `--git-common-dir` rather than spelling `${REPO_ROOT}/.git`.
    #
    # The snapshot is created beside where `retain` will move it, not under git's common
    # directory. Both locations are host-owned and outside the chamber's `/out` mount, so
    # the security property is the same — but in a linked worktree the common directory
    # and `${OUT_ROOT}` can sit on different filesystems, and `retain`'s `os.replace`
    # then raises `EXDEV` *after* the local branch has already been updated. Collection
    # would fail without retaining the recovery bundle: the one artefact that exists so a
    # later `rm` cannot destroy unrecoverable work. `.collected` is never mounted.
    [ -L "$bundle_out" ] && die "bundle slot ${bundle_out} is a symlink — refusing to read through it"
    [ -f "$bundle_out" ] || die "bundle did not appear at ${bundle_out}"
    snapshot_dir="${OUT_ROOT}/.collected"
    mkdir -p "$snapshot_dir" ||
        die "could not create the host-owned snapshot directory ${snapshot_dir}; nothing was fetched"
    bundle_snapshot=$(mktemp "${snapshot_dir}/autoclave-bundle.${task}.XXXXXX") ||
        die "could not create a host-owned bundle snapshot; nothing was fetched"
    LIFECYCLE_SNAPSHOT="$bundle_snapshot"
    if ! python3 "$SAFE_FILE" read "$bundle_out" \
        "$MAX_COLLECTION_BUNDLE_BYTES" > "$bundle_snapshot"; then
        die "could not safely snapshot bundle slot ${bundle_out}; nothing was fetched"
    fi
    if ! git -C "$REPO_ROOT" bundle verify "$bundle_snapshot" >/dev/null 2>&1; then
        die "what is at ${bundle_out} is not a git bundle. Nothing was fetched. Look at it before anything else — the chamber writes that path and this is what it left there"
    fi

    nonce=${bundle_snapshot##*.}
    incoming="refs/autoclave/incoming/${task}-${nonce}"
    if git -C "$REPO_ROOT" show-ref --verify --quiet "$incoming" ||
       git -C "$REPO_ROOT" symbolic-ref -q "$incoming" >/dev/null 2>&1; then
        die "the invocation-unique incoming ref already exists; nothing was fetched"
    fi
    LIFECYCLE_INCOMING="$incoming"
    if ! git -C "$REPO_ROOT" fetch --quiet "$bundle_snapshot" "+${branch}:${incoming}"; then
        die "bundle fetched no ref — inspect it with: git bundle list-heads ${bundle_out}"
    fi
    incoming_tip=$(git -C "$REPO_ROOT" rev-parse "$incoming")

    # A chamber starts at this exact base and must add commits above it. Rewriting the
    # inherited history creates an unrelated look-alike branch whose integration has to
    # rediscover the new commits by subject. Refuse that shape before it can replace a
    # useful local agent branch. The temporary ref is removed on both paths.
    if ! git -C "$REPO_ROOT" merge-base --is-ancestor "$chamber_base" "$incoming_tip"; then
        die "the returned branch rewrote its chamber base instead of extending it. The chamber still holds the work; add new commits above ${chamber_base} and collect again"
    fi

    branch_ref="refs/heads/${branch}"
    if git -C "$REPO_ROOT" symbolic-ref -q "$branch_ref" >/dev/null 2>&1; then
        die "local ${branch} is a symbolic ref; nothing was replaced"
    fi
    # `update-ref` can move a checked-out branch even though its worktree has files
    # from the old commit. Refuse rather than changing a branch another worktree uses.
    occupancy=$(python3 "$SAFE_FILE" worktree "$REPO_ROOT" "$branch_ref" occupied) || {
        die "could not inspect worktree occupancy; nothing was replaced"
    }
    [ "$occupancy" = absent ] || [ "$occupancy" = occupied ] ||
        die "worktree occupancy returned an invalid result; nothing was replaced"
    if [ "$occupancy" = occupied ]; then
        die "local ${branch} is occupied (checked out in a worktree); nothing was replaced"
    fi
    old_tip=$(git -C "$REPO_ROOT" rev-parse --verify --quiet "$branch_ref") || old_tip=""
    if [ -n "$old_tip" ] &&
       ! git -C "$REPO_ROOT" merge-base --is-ancestor "$old_tip" "$incoming_tip"; then
        die "local ${branch} contains work the returned branch does not extend; nothing was replaced"
    fi
    # `update-ref` alone does not know about worktrees, so make the occupancy check
    # immediately before the conditional update as well as above.  The old value is
    # the compare-and-swap: a concurrent ref movement now refuses rather than being
    # overwritten by a force-update.
    occupancy=$(python3 "$SAFE_FILE" worktree "$REPO_ROOT" "$branch_ref" occupied) ||
        die "could not inspect worktree occupancy; nothing was replaced"
    [ "$occupancy" = absent ] || [ "$occupancy" = occupied ] ||
        die "worktree occupancy returned an invalid result; nothing was replaced"
    if [ "$occupancy" = occupied ]; then
        die "local ${branch} is occupied (checked out in a worktree); nothing was replaced"
    fi
    if [ -n "$old_tip" ]; then
        git -C "$REPO_ROOT" update-ref --no-deref "$branch_ref" "$incoming_tip" "$old_tip" ||
            die "local ${branch} moved during collection; nothing was replaced (git error above)"
    else
        git -C "$REPO_ROOT" update-ref --no-deref "$branch_ref" "$incoming_tip" "0000000000000000000000000000000000000000" ||
            die "local ${branch} moved during collection; nothing was replaced (git error above)"
    fi
    # A worktree can be added after the last occupancy read. If it checked out the
    # incoming tip after our CAS, branch and worktree already agree and collection is
    # safe. If it checked out the old tip before the CAS completed, restore the branch
    # with another CAS so its index and files remain clean too.
    worktree_state=$(python3 "$SAFE_FILE" worktree "$REPO_ROOT" "$branch_ref" tree) ||
        die "local ${branch} was updated, but worktree occupancy could not be rechecked; inspect it before changing anything"
    if [ "$worktree_state" != absent ]; then
        occupied_tree=${worktree_state#tree:}
        [ "tree:${occupied_tree}" = "$worktree_state" ] ||
            die "local ${branch} was updated, but worktree state was invalid; inspect it before changing anything"
        incoming_tree=$(git -C "$REPO_ROOT" rev-parse "${incoming_tip}^{tree}") ||
            die "local ${branch} was updated, but its incoming tree could not be resolved"
        old_tree=""
        if [ -n "$old_tip" ]; then
            old_tree=$(git -C "$REPO_ROOT" rev-parse "${old_tip}^{tree}") ||
                die "local ${branch} was updated, but its previous tree could not be resolved"
        fi
        if [ "$occupied_tree" = "$incoming_tree" ]; then
            : # The worktree began after the CAS and already agrees with the branch.
        elif [ -n "$old_tip" ] && [ "$occupied_tree" = "$old_tree" ]; then
            git -C "$REPO_ROOT" update-ref --no-deref "$branch_ref" "$old_tip" "$incoming_tip" ||
                die "local ${branch} became occupied and could not be safely restored; inspect it before changing anything"
            die "local ${branch} became occupied during collection; its previous value was restored"
        else
            die "local ${branch} was updated, but the new worktree index matches neither its previous nor incoming tree; inspect it before changing anything"
        fi
    fi

    # A later re-author may move the branch away from this exact chamber tip. Record
    # successful publication outside the chamber-writable drawer so `rm` can distinguish
    # durable collection from an object imported by a collection that later refused.
    collected_marker=$(collected_marker_of "$task" "$incoming_tip")
    mkdir -p "$(dirname "$collected_marker")" ||
        die "local ${branch} was updated, but its collection marker could not be created"
    python3 "$SAFE_FILE" retain "$bundle_snapshot" "$collected_marker" ||
        die "local ${branch} was updated, but its recoverable collection bundle could not be retained"
    LIFECYCLE_SNAPSHOT=""

    # An agent that committed nothing leaves its branch pointing at the base, and the
    # bundle for that branch builds and fetches perfectly. Saying "collected" over it
    # reports success on nothing, which is the one thing this tool must not do.
    landed=$(git -C "$REPO_ROOT" rev-list --count "${chamber_base}..${branch}")
    if [ "$landed" = "0" ]; then
        note "collected '${task}' — and it carries NO COMMITS."
        note "The agent made no commit. Read its report before assuming it did work:"
        note "  $0 report ${task}"
    else
        note "collected '${task}' into local branch ${branch} — ${landed} commit(s)"
    fi

    # **Nothing that came out of a chamber is attributed until somebody attributes
    # it.** The clone commits as `autoclave <autoclave@localhost>`, and `commit-msg`
    # — the hook that refuses a commit carrying no `Co-Authored-By` line — is switched
    # on there now, so ordinarily every returned commit already names its model.
    # Ordinarily is not always: the hook can be skipped, and a chamber created before
    # `new` set `core.hooksPath` never had it. `pre-push` reads `Reviewed-by:` trailers
    # and scans for credentials; it does not look at authorship at all. So this is the
    # last place anything checks, and it checks rather than assuming the hook ran.
    #
    # It says so rather than fixing it. Only the session knows which model actually
    # answered, and inventing a trailer here would be a worse lie than a missing one.
    # It also does not refuse: a branch is not made safer by being stranded in a
    # container, and `rm` already refuses to destroy work that was never collected.
    #
    # Judged the way `.githooks/commit-msg` judges it — `interpret-trailers --parse`,
    # and a real address — because that hook is the thing this is standing in for when
    # it did not run. A substring search over the whole message disagreed with it in
    # the direction that matters: `Co-Authored-By: nobody` in the middle of a body is
    # a match for a grep, is refused by the hook, and registers no co-author with git
    # or GitHub. The looser test called such a commit attributed and said nothing.
    if [ "$landed" != "0" ]; then
        unattributed=""
        for commit in $(git -C "$REPO_ROOT" rev-list "${chamber_base}..${branch}"); do
            git -C "$REPO_ROOT" show -s --format=%B "$commit" |
                git -C "$REPO_ROOT" interpret-trailers --parse |
                grep -qiE '^Co-Authored-By:[[:space:]]*.+<[^@[:space:]]+@[^>[:space:]]+>[[:space:]]*$' ||
                unattributed="${unattributed}$(git -C "$REPO_ROOT" log -1 --format='  %h %s' "$commit")
"
        done
        if [ -n "$unattributed" ]; then
            note ""
            note "UNATTRIBUTED — these commits carry no Co-Authored-By trailer:"
            printf '%s\n' "$unattributed"
            note "Nothing merges until each one names the model that wrote it: squash,"
            note "or amend the trailers in. See operations/autoclave/README.md."
        fi
    fi
    note ""
    note "nothing has been merged. Read it before anything else:"
    note "  git log --oneline HEAD..${branch}"
    note "  git diff HEAD...${branch}"
}

cmd_report() {
    task="${1:-}"; check_task "$task"
    report="$(outdir_of "$task")/report.md"
    # /out is the one host path the agent can write, so everything read back from it
    # is untrusted input. A symlink left there points `cat` at any file this user can
    # read — an SSH key, `private/` — and prints it straight into the session. `[ -e ]`
    # follows links, so `[ -L ]` is tested first and separately.
    #
    # The test above and a `cat` below are two operations on a path a running agent
    # can change in between, and the agent is running: it can pass the test as a file
    # and be read as a link. What closes that is opening the path once, with the kernel
    # refusing to follow a final symlink, and reading through *that* descriptor. See
    # `safe_file.py`. The shell tests stay because they say what is wrong; the helper
    # says no whatever happens.
    [ -L "$report" ] && die "report at ${report} is a symlink — refusing to read through it"
    [ -f "$report" ] || die "no report at ${report}"
    # The helper has already printed the precise reason to stderr, and it refuses
    # an oversized report before writing a byte -- `copy_bounded` compares the
    # opened file's size against the limit first, so nothing above is a truncated
    # report. Saying "no safe regular report" alone contradicted that message
    # whenever the cause was the size limit rather than the slot's kind.
    python3 "$SAFE_FILE" read "$report" "$MAX_REPORT_BYTES" ||
        die "the report at ${report} could not be read; its reason is printed above (a size refusal means the report is intact on disk and must be read directly)"
}

cmd_list() {
    need_docker
    docker ps --all --filter "label=verbatus.autoclave=1" \
        --format 'table {{.Label "verbatus.task"}}\t{{.State}}\t{{.Status}}\t{{.Names}}'
}

cmd_rm() {
    task="${1:-}"; check_task "$task"
    force="${2:-}"
    case "$force" in
        ''|force) : ;;
        *) die "rm takes a task and, at most, the word 'force'" ;;
    esac
    acquire_lifecycle_lock "$task"
    need_docker
    exists "$task" || die "no chamber '$task'"

    # **The chamber's tree is the only copy of what is in it.** `docker rm --force`
    # takes the container's filesystem with it, and the output drawer that survives
    # holds a bundle only if `collect` was run — so an uncommitted file, or a commit
    # nobody fetched, is gone at this line and nowhere else. That is losing work
    # silently, which hard rule 7 and GOVERNANCE 2 both name as the thing not to do,
    # and the way it actually happens is a tidy-up at the end of a session rather than
    # a decision. So the tree is read before it is destroyed, and `force` is the word
    # that says the loss is intended.
    #
    # A stopped chamber cannot be read at all, so it is refused rather than destroyed
    # on the strength of a warning nobody has to answer. `force` is still there for the
    # chamber that will not start, which is the one case where refusing outright would
    # leave a container this tool could no longer remove.
    if [ "$force" != force ]; then
        running "$task" ||
            die "chamber '$task' is stopped, so nothing here can read what is in it. Start it with 'docker start $(container_of "$task")' and try again, or destroy it unread with: $0 rm $task force"
        # A check that cannot run is a failure, not a pass — `.githooks/commit-msg`
        # says it in those words. Swallowing the exit status here read an unreachable
        # chamber, a broken clone or a docker error as a clean tree, and then destroyed
        # it on the strength of that.
        if ! loose=$(docker exec "$(container_of "$task")" \
            sh -c 'cd /work && git status --porcelain' 2>&1); then
            printf '%s\n' "$loose" >&2
            die "cannot read the tree in '$task' (above), so nothing here can call it safe to destroy. Look with '$0 shell $task', or destroy it unread with: $0 rm $task force"
        fi
        if [ -n "$loose" ]; then
            printf '%s\n' "$loose" >&2
            die "chamber '$task' has uncommitted or untracked work (above). Commit and '$0 collect $task', or destroy it with: $0 rm $task force"
        fi
        # **Every branch in there, not just `agent/<task>`.** Asking only about the
        # branch the launcher cut reads a chamber whose agent committed onto
        # `work/refactor`, or onto a detached HEAD, as holding nothing at all — its
        # tree is clean and `agent/<task>` still stands at the base, so this walked
        # straight past an hour of work into `docker rm --force`. `collect` bundles
        # only `agent/<task>` too, so that work was never collectable and nothing
        # ever said so. `for-each-ref` plus `HEAD` covers both shapes.
        #
        # The `|| :` on the HEAD read is for a chamber whose HEAD is unborn, which is
        # not a failure; the surrounding `set -e` inside the container still makes a
        # real failure a failure, and the `if !` here still makes that a refusal.
        if ! chamber_tips=$(docker exec "$(container_of "$task")" sh -c '
            set -eu
            cd /work
            git for-each-ref --format="%(objectname)" refs/heads
            git rev-parse --verify --quiet HEAD || :
        ' 2>&1); then
            printf '%s\n' "$chamber_tips" >&2
            die "cannot read the branches in '$task' (above), so nothing here can say whether it holds work. Destroy it unread with: $0 rm $task force"
        fi
        # Mere object presence is not collection: a refused `collect` has already
        # fetched the object before its incoming ref is removed. Require a durable ref,
        # or the host-only marker written only after destination publication succeeded.
        chamber_base=$(docker inspect \
            --format '{{index .Config.Labels "verbatus.base"}}' \
            "$(container_of "$task")" 2>/dev/null) || chamber_base=""
        for tip in $chamber_tips; do
            [ -n "$tip" ] || continue
            [ "$tip" = "$chamber_base" ] && continue
            if git -C "$REPO_ROOT" for-each-ref --contains "$tip" --format='%(refname)' \
                refs/heads refs/tags refs/remotes 2>/dev/null | grep -q .; then
                continue
            fi
            collected_marker=$(collected_marker_of "$task" "$tip")
            if [ ! -L "$collected_marker" ] && [ -f "$collected_marker" ]; then
                recoverable=$(python3 "$SAFE_FILE" bundle-tip "$collected_marker" \
                    "refs/heads/agent/${task}" "$tip" 2>/dev/null) || recoverable=""
                if [ "$recoverable" = recoverable ]; then
                    continue
                fi
            fi
            die "chamber '$task' holds a commit this repository does not have a durably collected copy of (${tip}). Run '$0 collect $task' — and if it is on a branch other than agent/${task}, move or merge it there first, because that is the only branch collection bundles. Or destroy it with: $0 rm $task force"
        done
    fi

    docker rm --force "$(container_of "$task")" >/dev/null
    # The snapshot ref exists only to get the working tree into the clone. Once the
    # chamber is gone nothing reads it, and leaving one branch per chamber behind is
    # the clutter the one-branch-per-task rule exists to prevent. The commit itself
    # stays reachable from the reflog, so a collected branch built on it is not
    # orphaned by this.
    if git -C "$REPO_ROOT" rev-parse --verify --quiet \
        "refs/heads/autoclave/snapshot-${task}" >/dev/null; then
        git -C "$REPO_ROOT" update-ref -d "refs/heads/autoclave/snapshot-${task}"
        note "snapshot ref for '${task}' removed"
    fi
    # **The staged specs move into the output drawer rather than being deleted.** That
    # comment used to say removing them lost nothing, because they were a copy of files
    # still on this machine. Unlocking the mount in a8f0956 made that false and nothing
    # noticed: `/specs` is writable, the brief tells the agent the specs are its to write,
    # and `collect` only bundles commits from `/work` — so an agent's edits lived in
    # exactly one place and `rm` deleted it. Hard rule 7 is that nothing is lost silently,
    # and an agent that forgot to report an edit made this the silent case. The drawer is
    # kept forever, so moving them there costs nothing and keeps the only copy.
    # Found by CodeRabbit on pull request 16.
    # **Never onto a previous copy.** A task name may be reused after `rm`, the drawer
    # persists across both, and the first draft of this deleted `<outdir>/specs` before
    # moving the new one — destroying the earlier chamber's edits to save a rerun of the
    # same name, which is the loss it was written to prevent. Each retention gets its own
    # numbered directory instead, and nothing overwrites anything. Found by CodeRabbit on
    # pull request 16.
    specs_stage="${REPO_ROOT}/workbench/autoclave/.specs/${task}"
    if [ -d "$specs_stage" ]; then
        mkdir -p "$(outdir_of "$task")"
        kept="$(outdir_of "$task")/specs"
        n=1
        while [ -e "$kept" ]; do
            kept="$(outdir_of "$task")/specs-${n}"
            n=$((n + 1))
        done
        if mv "$specs_stage" "$kept" 2>/dev/null; then
            note "staged specs kept at ${kept} — read them for edits the agent made"
        else
            note "staged specs could not be moved; they remain at ${specs_stage}"
        fi
    fi
    # The output drawer is deliberately kept. Nothing is lost silently, and the
    # bundle is the only surviving evidence that the dispatch happened.
    note "chamber '${task}' destroyed. Output kept at $(outdir_of "$task")"
}

# Print the header comment and stop at the first line of actual shell, so the
# help text cannot drift out of step with the script the way a fixed line range
# does the moment anyone edits above it.
usage() { awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"; }

case "${1:-}" in
    doctor)  shift; cmd_doctor "$@" ;;
    build)   shift; cmd_build "$@" ;;
    login)   shift; cmd_login "$@" ;;
    new)     shift; cmd_new "$@" ;;
    dispatch) shift; cmd_dispatch "$@" ;;
    shell)   shift; cmd_shell "$@" ;;
    exec)    shift; cmd_exec "$@" ;;
    collect) shift; cmd_collect "$@" ;;
    report)  shift; cmd_report "$@" ;;
    list)    shift; cmd_list "$@" ;;
    rm)      shift; cmd_rm "$@" ;;
    ''|-h|--help|help) usage ;;
    *) die "unknown command '$1' — run '$0 help'" ;;
esac
