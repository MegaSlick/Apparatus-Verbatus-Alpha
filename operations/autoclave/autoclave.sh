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
    # `--device` asks the vendor for a code to type on another device instead of
    # standing up a callback server on localhost *inside the container*. That
    # callback is the reason the ordinary flow needs a browser on this machine: the
    # URL it prints points at `http://localhost:1455`, which resolves to the
    # container and is unreachable from a phone. Device auth has no callback, so the
    # sign-in can be finished from anywhere — which is the difference between "run
    # this when you are next at a keyboard" and "run this now".
    mode="${2:-browser}"
    # **Arguments are judged before infrastructure is touched**, the same ordering
    # `dispatch` and `new` already keep. Asking Docker first meant `login gemini`
    # answered "docker CLI not found" instead of naming the two vendors that exist —
    # a true statement about the wrong thing, and one that sends the reader off to
    # install something they did not need. Two tests assert the vendor error and fail
    # anywhere Docker is absent, which is every chamber and CI.
    case "$vendor" in
        claude) volume="$AUTH_VOL_CLAUDE"; mount="$AUTH_DIR_CLAUDE"; tool="claude /login" ;;
        codex)  volume="$AUTH_VOL_CODEX";  mount="$AUTH_DIR_CODEX";  tool="codex login" ;;
        *) die "login takes 'claude' or 'codex'" ;;
    esac
    case "$mode" in
        browser) : ;;
        device)
            [ "$vendor" = codex ] ||
                die "only codex offers device auth; run '$0 login claude' at a keyboard"
            tool="codex login --device-auth" ;;
        *) die "login mode is 'browser' or 'device'" ;;
    esac
    need_docker
    docker image inspect "$IMAGE" >/dev/null 2>&1 || die "image not built — run: $0 build"

    has_volume "$volume" || docker volume create "$volume" >/dev/null

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
    if [ -t 0 ]; then
        docker run --rm --interactive --tty \
            --volume "${volume}:${mount}" \
            "$IMAGE" \
            sh -c "$tool" || true
    else
        note "(no terminal here — running without one; follow the URL or code below)"
        note ""
        docker run --rm --interactive \
            --volume "${volume}:${mount}" \
            "$IMAGE" \
            sh -c "$tool" || true
    fi

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
    # disturbed, nothing is added to any commit the operator is preparing. `add -A`
    # against that index honours `.gitignore`, so `workbench/`, `private/` and
    # `scriptorium/` stay out exactly as they do everywhere else. Only when the base is
    # the default: an explicitly named base means that exact commit and nothing else.
    snapshot_ref=""
    if [ "$base" = "HEAD" ]; then
        snap_index=$(mktemp) || die "cannot create a temporary index"
        if GIT_INDEX_FILE="$snap_index" git -C "$REPO_ROOT" read-tree "$base_sha" 2>/dev/null &&
           GIT_INDEX_FILE="$snap_index" git -C "$REPO_ROOT" add -A 2>/dev/null; then
            snap_tree=$(GIT_INDEX_FILE="$snap_index" git -C "$REPO_ROOT" write-tree 2>/dev/null)
        else
            snap_tree=""
        fi
        rm -f "$snap_index"
        base_tree=$(git -C "$REPO_ROOT" rev-parse "${base_sha}^{tree}")
        if [ -n "$snap_tree" ] && [ "$snap_tree" != "$base_tree" ]; then
            snap=$(git -C "$REPO_ROOT" commit-tree "$snap_tree" -p "$base_sha" \
                -m "autoclave snapshot for ${task}: the working tree at chamber creation") \
                || die "could not record the working-tree snapshot"
            # A ref, because `git clone` fetches refs and not dangling commits.
            snapshot_ref="refs/heads/autoclave/snapshot-${task}"
            git -C "$REPO_ROOT" update-ref "$snapshot_ref" "$snap" \
                || die "could not point ${snapshot_ref} at the snapshot"
            base_sha="$snap"
            dirty=$(git -C "$REPO_ROOT" status --porcelain | wc -l | tr -d ' ')
            note "working tree is not clean — the chamber gets a snapshot of it"
            note "  ${dirty} path(s) beyond ${base}, captured now and frozen"
        fi
    fi

    need_docker
    docker image inspect "$IMAGE" >/dev/null 2>&1 || die "image not built — run: $0 build"
    exists "$task" && die "chamber '$task' already exists — use 'rm $task' first"

    outdir=$(outdir_of "$task")
    mkdir -p "$outdir"

    # --network none would be the honest default, but the agent CLIs need to
    # reach their model provider. Egress is therefore open and that is a stated
    # limit, not an oversight: see the README. What is *not* open is any route
    # to write the host — /src is read-only. It is readable, though, except for the
    # three masked drawers below.
    # The vendor's auth volume is mounted when it exists, and its absence is not an
    # error: a chamber is useful for running tests and reading code before any vendor has
    # been signed in. Read-write, because both CLIs refresh their own tokens and a
    # read-only mount would turn a one-time sign-in into a recurring one.
    auth_mounts=""
    case "$vendor" in
        claude)
            has_volume "$AUTH_VOL_CLAUDE" && \
                auth_mounts="--volume ${AUTH_VOL_CLAUDE}:${AUTH_DIR_CLAUDE}" ;;
        codex)
            has_volume "$AUTH_VOL_CODEX" && \
                auth_mounts="--volume ${AUTH_VOL_CODEX}:${AUTH_DIR_CODEX}" ;;
    esac

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
        # /work/AGENTS.md is the one Codex actually reads. A copy at the filesystem
        # root is not enough: probed on 2026-08-02, a Codex seat reported that no
        # instruction file had been loaded automatically at all, and that /AGENTS.md
        # existed but was not in its context. Discovery starts at the working
        # directory, which is /work. Every Codex agent dispatched into a chamber
        # before this — including that day's audit and review seats — ran with no
        # standing limits beyond whatever its prompt carried.
        #
        # No backticks in this block: it is double-quoted so the base commit can be
        # interpolated, which means a backtick runs on the host. One in this comment
        # did exactly that before it was caught.
        cp /opt/autoclave/CLAUDE.md /work/AGENTS.md
        # The brief is scaffolding, not the agent's work, and it must not read as
        # either. Untracked at the repository root it is 'stray documentation' to
        # \`.githooks/doc-allowlist.sh\`, so \`check-static.sh\` failed inside every
        # chamber — on a file the agent did not write and cannot correct. An agent
        # told to run the gate before handing work back either reports itself blocked
        # or deletes its own brief to make the gate pass. Excluded in the clone rather
        # than added to the tracked \`.gitignore\`, because this file exists only here.
        printf '/AUTOCLAVE.md\n/AGENTS.md\n' >> /work/.git/info/exclude
        git config user.name  'autoclave'
        git config user.email 'autoclave@localhost'
    " || die "chamber started but setup failed — inspect with: docker logs $(container_of "$task")"

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
    case "$effort" in
        none|minimal|low|medium|high|xhigh|max) : ;;
        *) die "'$effort' is not an allowed effort" ;;
    esac

    need_docker
    running "$task" || die "chamber '$task' is not running — start it with: $0 new $task"
    case "$vendor" in
        claude) volume="$AUTH_VOL_CLAUDE" ;;
        codex)  volume="$AUTH_VOL_CODEX" ;;
    esac
    has_volume "$volume" || die "'$vendor' is not signed in — run: $0 login $vendor"

    # A chamber holds one vendor's credential, chosen when it was created, because a
    # mount cannot be added to a running container. Refusing here is what makes that
    # choice mean something: without it a chamber built for one vendor would be
    # dispatched to the other and fail deep inside the CLI with an auth error.
    chamber_vendor=$(docker inspect --format '{{index .Config.Labels "verbatus.vendor"}}' \
        "$(container_of "$task")" 2>/dev/null) || chamber_vendor=""
    [ "$chamber_vendor" = "$vendor" ] || die \
        "chamber '$task' holds no ${vendor} credential (it was created for '${chamber_vendor:-none}'). Create one with: $0 new <task> <base> ${vendor}"

    # The brief travels as a file through the scratch drawer, never as a shell
    # argument. A brief is prose written by a session; interpolating it into a
    # command line makes its punctuation executable.
    # Same reason as `report`: an agent that replaces brief.md with a symlink turns
    # this `cp` into a write through it, onto any file this user can write.
    brief_in="$(outdir_of "$task")/brief.md"
    [ -L "$brief_in" ] && die "brief slot ${brief_in} is a symlink — refusing to write through it"
    cp "$brief" "$brief_in"

    note "dispatching ${vendor} into '${task}'"
    note "  brief:  $(outdir_of "$task")/brief.md"
    note "  report: $(outdir_of "$task")/report.md"
    note "  model:  ${model}, effort ${effort}"
    note ""

    # Model and effort reach the container as environment variables, for the same
    # reason the brief travels as a file: a value interpolated into a quoted command
    # line brings its punctuation with it.
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
                    -p "$(cat /out/brief.md)"
            ' ;;
        codex)
            # stdin is closed deliberately. `codex exec` waits forever on an open
            # stdin when nothing is attached, which in a detached container means
            # a dispatch that never returns and never says why.
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
            # prompt rather than as an unknown option.
            docker exec -e AC_MODEL="$model" -e AC_EFFORT="$effort" \
                "$(container_of "$task")" sh -c '
                cd /work
                codex exec --skip-git-repo-check \
                    --dangerously-bypass-approvals-and-sandbox \
                    -m "$AC_MODEL" \
                    -c "model_reasoning_effort=$AC_EFFORT" \
                    -- "$(cat /out/brief.md)" < /dev/null
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

    # An agent that committed nothing leaves its branch pointing at the base, and the
    # bundle for that branch builds and fetches perfectly. Saying "collected" over it
    # reports success on nothing, which is the one thing this tool must not do.
    chamber_base=$(docker inspect --format '{{index .Config.Labels "verbatus.base"}}' \
        "$(container_of "$task")" 2>/dev/null) || chamber_base=""
    landed=$(git -C "$REPO_ROOT" rev-list --count "${chamber_base}..${branch}" 2>/dev/null || echo "?")
    if [ "$landed" = "0" ]; then
        note "collected '${task}' — and it carries NO COMMITS."
        note "The agent made no commit. Read its report before assuming it did work:"
        note "  $0 report ${task}"
    else
        note "collected '${task}' into local branch ${branch} — ${landed} commit(s)"
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
    [ -L "$report" ] && die "report at ${report} is a symlink — refusing to read through it"
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
