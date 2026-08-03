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

IMAGE_NAME="verbatus-autoclave"
IMAGE_TAG="dev"
IMAGE="${IMAGE_NAME}:${IMAGE_TAG}"

# Container names are prefixed so `list` and `rm` can never touch a container
# belonging to something else on this machine.
PREFIX="verbatus-ac"

# The repository root, resolved from this script rather than the caller's cwd,
# so every command works from anywhere.
REPO_ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)

# The one thing in `/out` that a POSIX shell cannot do for itself: open a path once,
# refusing to follow a symlink at the end of it. See the file's own header.
SAFE_FILE="${REPO_ROOT}/operations/autoclave/safe_file.py"

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
    if command -v docker >/dev/null 2>&1; then docker --version; else echo "not installed"; fi
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
                0) echo "signed in (${volume})" ;;
                1) echo "not signed in — the volume exists but ${vendor} says no; run: $0 login ${vendor}" ;;
                *) echo "unknown — the volume exists but ${vendor} could not be asked" ;;
            esac
        fi
    done
}

cmd_build() {
    need_docker
    # Context is the repository root so the Dockerfile can reach
    # requirements-dev.txt; .dockerignore there denies everything else.
    # The uid and gid are the caller's, so bundles land owned by the host user.
    docker build \
        --file "${REPO_ROOT}/operations/autoclave/Dockerfile" \
        --build-arg "UID=$(id -u)" \
        --build-arg "GID=$(id -g)" \
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
    mount_asked=$(auth_dir "$vendor_asked") || return 2
    check_asked=$(auth_check "$vendor_asked") || return 2
    has_volume "$volume_asked" || return 1
    answer=$(docker run --rm --volume "${volume_asked}:${mount_asked}" "$IMAGE" \
        sh -c "${check_asked} >/dev/null 2>&1; printf 'ac-auth:%s' \$?" 2>/dev/null) || return 2
    case "$answer" in
        ac-auth:0) return 0 ;;
        ac-auth:*) return 1 ;;
        *) return 2 ;;
    esac
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
    docker image inspect "$IMAGE" >/dev/null 2>&1 || die "image not built — run: $0 build"

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
    login_status=0
    if [ -t 0 ]; then
        docker run --rm --interactive --tty \
            --volume "${volume}:${mount}" \
            "$IMAGE" \
            sh -c "$tool" || login_status=$?
    else
        note "(no terminal here — running without one; follow the URL or code below)"
        note ""
        docker run --rm --interactive \
            --volume "${volume}:${mount}" \
            "$IMAGE" \
            sh -c "$tool" || login_status=$?
    fi

    note ""
    # **Whether files appeared is not whether a sign-in worked.** The old test was
    # `ls -A` on the mount, and both CLIs write configuration into that directory
    # before, during and after any sign-in — so it reported success for an attempt
    # that was cancelled at the vendor's own page. Ask the vendor instead.
    asked=0; authenticated "$vendor" "$volume" || asked=$?
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
require_signed_in() {
    asked=0; authenticated "$1" "$2" || asked=$?
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

    # Resolve the base to a commit on the host, so the chamber is pinned to an exact
    # tree rather than to whatever a name meant at clone time. It is git-only, so it
    # belongs above the Docker check for the same reason `login`'s vendor check does:
    # a mistyped base should say the base is wrong, not that Docker is missing.
    base_sha=$(git -C "$REPO_ROOT" rev-parse --verify "${base}^{commit}") \
        || die "cannot resolve base '$base'"

    # **Every prerequisite that only reads is checked before anything is written.**
    # These four used to sit below the snapshot, which is the first thing `new` writes
    # to the host repository, so a `new` that failed for a missing engine, an unbuilt
    # image or a name already in use left `refs/heads/autoclave/snapshot-<task>` and its
    # commit standing — and `rm` refuses a task with no container, so the one command
    # that deletes that ref could not be reached to do it.
    need_docker
    docker image inspect "$IMAGE" >/dev/null 2>&1 || die "image not built — run: $0 build"
    exists "$task" && die "chamber '$task' already exists — use 'rm $task' first"

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
            require_signed_in claude "$AUTH_VOL_CLAUDE"
            auth_mounts="--volume ${AUTH_VOL_CLAUDE}:${AUTH_DIR_CLAUDE}" ;;
        codex)
            require_signed_in codex "$AUTH_VOL_CODEX"
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
            base_sha="$snap"
            # Counted from the same two commands the snapshot was built from, so the
            # number describes exactly what was captured. `status --porcelain -z`
            # would have been simpler and wrong: it emits a rename as *two*
            # NUL-terminated fields, so counting NUL bytes counted every rename twice.
            dirty=$(( $(git -C "$REPO_ROOT" diff --name-only -z "$base_sha" |
                            tr -cd '\000' | wc -c) +
                      $(git -C "$REPO_ROOT" ls-files --others --exclude-standard -z |
                            tr -cd '\000' | wc -c) ))
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
        $auth_mounts \
        --workdir /work \
        "$IMAGE" \
        sleep infinity >/dev/null || {
        # The one failure past the snapshot that can leave a ref standing. `rm`
        # refuses a task with no container, so nothing else would ever delete it.
        [ -n "$snapshot_ref" ] &&
            git -C "$REPO_ROOT" update-ref -d "$snapshot_ref" 2>/dev/null
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
        # which is the state `CLAUDE.md`'s "What may be missing or wrong" warns about.
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
    docker exec "$(container_of "$task")" sh -c '
        printf "%s\n" \
            "{" \
            "  \"env\": {" \
            "    \"CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH\": \"64\"" \
            "  }" \
            "}" > /work/.claude/settings.local.json
    ' || die "chamber started but the spawn-depth override could not be written"

    note "chamber '${task}' is up"
    note "  base:   ${base_sha}"
    note "  branch: agent/${task}"
    note "  output: ${outdir}"
    note ""
    note "  shell:   $0 shell ${task}"
    note "  collect: $0 collect ${task}"
}

cmd_shell() {
    task="${1:-}"; check_task "$task"; need_docker
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

cmd_dispatch() {
    task="${1:-}"; check_task "$task"
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
    # Validated rather than trusted, exactly as `operations/codex/seat.sh` validates the
    # same field. It reaches the container as an environment variable and never as
    # interpolated text, but a value beginning with `-` would still arrive as a flag.
    case "$model" in
        -*|*[!A-Za-z0-9._-]*) die "'$model' is not a plain model name" ;;
    esac
    # **Effort is judged against the vendor and the model, not against a vocabulary.**
    # `none minimal low medium high xhigh max` is the union of what the two vendors
    # accept, and no vendor accepts all seven. `.claude/agents/README.md` measured
    # which, under "Reachability, which is not negotiable": `minimal` is rejected by
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
    case "$vendor" in
        claude) reachable="low medium high xhigh max" ;;
        codex)
            case "$model" in
                gpt-5.3-codex-spark) reachable="low medium high xhigh" ;;
                *) reachable="none low medium high xhigh max" ;;
            esac ;;
    esac
    case " ${reachable} " in
        *" ${effort} "*) : ;;
        *) die "'$effort' is not an allowed effort for ${vendor} '${model}' — it takes: ${reachable}" ;;
    esac

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
    # a container that is already running.
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
    python3 "$SAFE_FILE" write "$brief" "$brief_in" ||
        die "brief slot ${brief_in} is not a safe regular file — refusing to write to it"

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
    # is a config override, `-c model_reasoning_effort=<level>`, which is how
    # `operations/codex/seat.sh` has always done it. Verified against `--help` on
    # both CLIs rather than assumed.
    case "$vendor" in
        claude)
            # --dangerously-skip-permissions is correct *here* and nowhere else:
            # the container is the boundary, so there is no host left to protect
            # by prompting, and a prompt inside a detached container is a hang.
            # This flag is the reason the chamber exists.
            docker exec -e AC_MODEL="$model" -e AC_EFFORT="$effort" \
                "$(container_of "$task")" sh -c '
                cd /work
                claude --dangerously-skip-permissions \
                    --model "$AC_MODEL" \
                    --effort "$AC_EFFORT" \
                    -p < /out/brief.md
            ' ;;
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
            docker exec -e AC_MODEL="$model" -e AC_EFFORT="$effort" \
                "$(container_of "$task")" sh -c '
                cd /work
                codex exec --skip-git-repo-check \
                    --dangerously-bypass-approvals-and-sandbox \
                    -m "$AC_MODEL" \
                    -c "model_reasoning_effort=$AC_EFFORT" \
                    -- - < /out/brief.md
            ' ;;
    esac

    note ""
    note "dispatch returned. Nothing has been collected and nothing merged."
    note "  what it wrote:  $0 collect ${task}"
    note "  what it said:   $0 report ${task}"
}

cmd_collect() {
    task="${1:-}"; check_task "$task"; need_docker
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
        git bundle create ${bundle_in} '${branch}' >/dev/null
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
    # `git bundle verify` reads the bundle header and refuses anything else, including
    # that file, and it runs before `fetch` gets a path at all. A symlink is covered by
    # the same call for the same reason: whatever it points at still has to be a
    # bundle. The `[ -L ]` test in front says which of the two it was.
    [ -L "$bundle_out" ] && die "bundle slot ${bundle_out} is a symlink — refusing to read through it"
    [ -f "$bundle_out" ] || die "bundle did not appear at ${bundle_out}"
    git -C "$REPO_ROOT" bundle verify "$bundle_out" >/dev/null 2>&1 \
        || die "what is at ${bundle_out} is not a git bundle. Nothing was fetched. Look at it before anything else — the chamber writes that path and this is what it left there"

    git -C "$REPO_ROOT" fetch --quiet "$bundle_out" "${branch}:${branch}" \
        || die "bundle fetched no ref — inspect it with: git bundle list-heads ${bundle_out}"

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
    # follows links, so `[ -L ]` is tested first and separately; the same guard
    # `capture-seat-report.sh` already carries, which is where this was found missing.
    #
    # The test above and a `cat` below are two operations on a path a running agent
    # can change in between, and the agent is running: it can pass the test as a file
    # and be read as a link. What closes that is opening the path once, with the kernel
    # refusing to follow a final symlink, and reading through *that* descriptor. See
    # `safe_file.py`. The shell tests stay because they say what is wrong; the helper
    # says no whatever happens.
    [ -L "$report" ] && die "report at ${report} is a symlink — refusing to read through it"
    [ -f "$report" ] || die "no report at ${report}"
    python3 "$SAFE_FILE" read "$report" || die "no safe regular report at ${report}"
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
        # **Does this repository have the object** — not "is the tip the same SHA".
        # Equality deadlocked the moment the host branch moved, which is what
        # `collect` itself tells the operator to do next: amend the trailers in, and
        # `rm` then refused for ever while `collect` refused to re-fetch a rewind, so
        # the only spelling left was the destructive one. Presence is the question
        # that was always meant: a commit whose object is here was collected, however
        # the branch has moved since.
        chamber_base=$(docker inspect \
            --format '{{index .Config.Labels "verbatus.base"}}' \
            "$(container_of "$task")" 2>/dev/null) || chamber_base=""
        for tip in $chamber_tips; do
            [ -n "$tip" ] || continue
            [ "$tip" = "$chamber_base" ] && continue
            git -C "$REPO_ROOT" cat-file -e "${tip}^{commit}" 2>/dev/null && continue
            die "chamber '$task' holds a commit this repository does not have (${tip}). Run '$0 collect $task' — and if it is on a branch other than agent/${task}, move or merge it there first, because that is the only branch collection bundles. Or destroy it with: $0 rm $task force"
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
