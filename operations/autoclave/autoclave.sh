#!/bin/sh
# The autoclave — a sealed chamber a writing agent works inside.
#
# The shape, in one paragraph. The host repository is mounted read-only at
# /src. The container clones it to /work and cuts a branch. The agent works
# there with a full shell and runs its own tests. When it is done the branch
# comes back as a git bundle through /out, the one writable host path, and the
# session reads the diff before anything enters the real repository. No
# credentials go in, so nothing can be pushed from inside.
#
# Usage:
#   autoclave.sh doctor                  what is installed, running, and built
#   autoclave.sh build                   build the image
#   autoclave.sh login <claude|codex>    sign a vendor in, once, into its volume
#   autoclave.sh new <task> [<base>]     start a chamber on a branch from <base>
#   autoclave.sh dispatch <task> <claude|codex> <brief-file> [model]
#                                        run an agent inside, against a written brief
#   autoclave.sh shell <task>            open a shell in a running chamber
#   autoclave.sh exec <task> <cmd>...    run one command in a chamber
#   autoclave.sh collect <task>          bring the branch back as agent/<task>
#   autoclave.sh report <task>           print the report the agent left, if any
#   autoclave.sh list                    every chamber, running or stopped
#   autoclave.sh rm <task>               destroy a chamber (never its output)
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
    # Reported as present or absent only. This never opens the volume: whether a
    # sign-in exists is an operational fact; what it contains is not this
    # script's business and never appears in its output.
    for pair in "claude:${AUTH_VOL_CLAUDE}" "codex:${AUTH_VOL_CODEX}"; do
        vendor=${pair%%:*}; volume=${pair#*:}
        printf 'auth %-7s ' "$vendor"
        if has_volume "$volume"; then
            echo "signed in (${volume})"
        else
            echo "not signed in — run: $0 login ${vendor}"
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

cmd_login() {
    vendor="${1:-}"
    need_docker
    docker image inspect "$IMAGE" >/dev/null 2>&1 || die "image not built — run: $0 build"
    case "$vendor" in
        claude) volume="$AUTH_VOL_CLAUDE"; mount="$AUTH_DIR_CLAUDE"; tool="claude" ;;
        codex)  volume="$AUTH_VOL_CODEX";  mount="$AUTH_DIR_CODEX";  tool="codex login" ;;
        *) die "login takes 'claude' or 'codex'" ;;
    esac

    has_volume "$volume" || docker volume create "$volume" >/dev/null

    note "Signing '${vendor}' into ${volume}."
    note "This is interactive and it is yours to complete — the sign-in happens"
    note "between you and the vendor, and nothing about it is read or stored by"
    note "this script. Follow whatever the CLI asks, then exit."
    note ""
    # --rm: the container is a booth for the sign-in. What persists is the volume.
    docker run --rm --interactive --tty \
        --volume "${volume}:${mount}" \
        "$IMAGE" \
        sh -c "$tool" || true

    note ""
    if docker run --rm --volume "${volume}:${mount}" "$IMAGE" \
        sh -c "test -n \"\$(ls -A ${mount} 2>/dev/null)\""; then
        note "${volume} now holds configuration. Chambers started from here will use it."
    else
        note "${volume} is still empty — the sign-in did not complete."
        note "Nothing is broken; run this again when you are ready."
    fi
}

cmd_new() {
    task="${1:-}"; check_task "$task"
    base="${2:-HEAD}"
    need_docker
    docker image inspect "$IMAGE" >/dev/null 2>&1 || die "image not built — run: $0 build"
    exists "$task" && die "chamber '$task' already exists — use 'rm $task' first"

    # Resolve the base to a commit on the host, so the chamber is pinned to an
    # exact tree rather than to whatever a name meant at clone time.
    base_sha=$(git -C "$REPO_ROOT" rev-parse --verify "${base}^{commit}") \
        || die "cannot resolve base '$base'"

    outdir=$(outdir_of "$task")
    mkdir -p "$outdir"

    # --network none would be the honest default, but the agent CLIs need to
    # reach their model provider. Egress is therefore open and that is a stated
    # limit, not an oversight: see the README. What is *not* open is any route
    # to the host — /src is read-only and no credentials are mounted.
    # Auth volumes are mounted when they exist, and their absence is not an error:
    # a chamber is useful for running tests and reading code before any vendor has
    # been signed in. Read-write, because both CLIs refresh their own tokens and a
    # read-only mount would turn a one-time sign-in into a recurring one.
    auth_mounts=""
    has_volume "$AUTH_VOL_CLAUDE" && \
        auth_mounts="${auth_mounts} --volume ${AUTH_VOL_CLAUDE}:${AUTH_DIR_CLAUDE}"
    has_volume "$AUTH_VOL_CODEX" && \
        auth_mounts="${auth_mounts} --volume ${AUTH_VOL_CODEX}:${AUTH_DIR_CODEX}"

    # Word splitting on $auth_mounts is the point: it is a flag list this script
    # built from two fixed constants, never from user input.
    # shellcheck disable=SC2086
    docker run --detach \
        --name "$(container_of "$task")" \
        --label "verbatus.autoclave=1" \
        --label "verbatus.task=${task}" \
        --volume "${REPO_ROOT}:/src:ro" \
        --volume "${outdir}:/out" \
        $auth_mounts \
        --workdir /work \
        "$IMAGE" \
        sleep infinity >/dev/null

    # Clone from the read-only mount. `--no-local` is deliberate: a local clone
    # hardlinks into /src/.git, and hardlinks into a read-only mount are exactly
    # the kind of shared state this arrangement exists to avoid.
    docker exec "$(container_of "$task")" sh -c "
        set -eu
        git clone --no-local --quiet /src /work
        cd /work
        git checkout --quiet ${base_sha}
        git switch --quiet -c 'agent/${task}'
        cp /opt/autoclave/CLAUDE.md /work/AUTOCLAVE.md
        git config user.name  'autoclave'
        git config user.email 'autoclave@localhost'
    " || die "chamber started but setup failed — inspect with: docker logs $(container_of "$task")"

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
    # The fourth argument is the model, and it is optional because leaving it out has
    # a correct meaning: run whatever that vendor's CLI defaults to. Naming one is how
    # a cheap seat gets used for a cheap job — a Luna chamber for a bounded "build this,
    # check it works, stop" unit costs a fraction of a Sol one, and without this
    # argument there was no way to ask for it. Passed straight through to the CLI, so
    # the vendor validates the name rather than this script keeping a list that rots.
    model="${4:-}"
    need_docker
    running "$task" || die "chamber '$task' is not running — start it with: $0 new $task"
    [ -n "$brief" ] || die "usage: $0 dispatch <task> <claude|codex> <brief-file> [model]"
    [ -f "$brief" ] || die "no brief at '$brief'"

    case "$vendor" in
        claude) volume="$AUTH_VOL_CLAUDE" ;;
        codex)  volume="$AUTH_VOL_CODEX" ;;
        *) die "dispatch takes 'claude' or 'codex'" ;;
    esac
    has_volume "$volume" || die "'$vendor' is not signed in — run: $0 login $vendor"

    # The brief travels as a file through the scratch drawer, never as a shell
    # argument. A brief is prose written by a session; interpolating it into a
    # command line makes its punctuation executable.
    cp "$brief" "$(outdir_of "$task")/brief.md"

    note "dispatching ${vendor} into '${task}'"
    note "  brief:  $(outdir_of "$task")/brief.md"
    note "  report: $(outdir_of "$task")/report.md"
    note "  model:  ${model:-the vendor default}"
    note ""

    # The model reaches the container as an environment variable rather than inside
    # the quoted script, for the same reason the brief travels as a file: a value
    # interpolated into a command line brings its punctuation with it. Empty means
    # unset, and the shell inside expands it to no argument at all.
    case "$vendor" in
        claude)
            # --dangerously-skip-permissions is correct *here* and nowhere else:
            # the container is the boundary, so there is no host left to protect
            # by prompting, and a prompt inside a detached container is a hang.
            # This flag is the reason the chamber exists.
            docker exec -e AC_MODEL="$model" "$(container_of "$task")" sh -c '
                cd /work
                claude --dangerously-skip-permissions \
                    ${AC_MODEL:+--model "$AC_MODEL"} \
                    -p "$(cat /out/brief.md)"
            ' ;;
        codex)
            # stdin is closed deliberately. `codex exec` waits forever on an open
            # stdin when nothing is attached, which in a detached container means
            # a dispatch that never returns and never says why.
            docker exec -e AC_MODEL="$model" "$(container_of "$task")" sh -c '
                cd /work
                codex exec --skip-git-repo-check \
                    ${AC_MODEL:+--model "$AC_MODEL"} \
                    "$(cat /out/brief.md)" < /dev/null
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

    # Refuse an empty hand-back rather than reporting success on nothing.
    docker exec "$(container_of "$task")" sh -c "
        set -eu
        cd /work
        git diff --quiet && git diff --cached --quiet || {
            echo 'autoclave: uncommitted changes in the chamber' >&2
            git status --short >&2
            exit 1
        }
        git bundle create ${bundle_in} '${branch}' >/dev/null 2>&1
    " || die "nothing to collect from '$task' — the agent left work uncommitted, or made no commit"

    [ -f "$bundle_out" ] || die "bundle did not appear at ${bundle_out}"

    git -C "$REPO_ROOT" fetch --quiet "$bundle_out" "${branch}:${branch}" \
        || die "bundle fetched no ref — inspect it with: git bundle list-heads ${bundle_out}"

    note "collected '${task}' into local branch ${branch}"
    note ""
    note "nothing has been merged. Read it before anything else:"
    note "  git log --oneline HEAD..${branch}"
    note "  git diff HEAD...${branch}"
}

cmd_report() {
    task="${1:-}"; check_task "$task"
    report="$(outdir_of "$task")/report.md"
    [ -f "$report" ] || die "no report at ${report}"
    cat "$report"
}

cmd_list() {
    need_docker
    docker ps --all --filter "label=verbatus.autoclave=1" \
        --format 'table {{.Label "verbatus.task"}}\t{{.State}}\t{{.Status}}\t{{.Names}}'
}

cmd_rm() {
    task="${1:-}"; check_task "$task"; need_docker
    exists "$task" || die "no chamber '$task'"
    docker rm --force "$(container_of "$task")" >/dev/null
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
