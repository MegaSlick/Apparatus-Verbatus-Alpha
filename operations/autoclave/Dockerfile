# The autoclave image — the sealed chamber a writing agent works inside.
#
# Everything here exists so an agent can do real work: read the repository,
# write code, install what a task needs, and run the tests itself. Nothing here
# gives it a way back to the host. The host repository is mounted read-only and
# the only writable path outside the container is a scratch drawer.
#
# Debian trixie, not bookworm: pyproject.toml requires Python >= 3.12 and
# bookworm ships 3.11. This base carries Node 22 for the two agent CLIs and
# trixie's Python 3.13 for the pipeline.
FROM node:22-trixie-slim

# git          — the whole return path is a branch and a bundle.
# python3, venv, pip — the pipeline's language; venv because Debian marks the
#                system interpreter externally-managed and pip refuses it.
# build-essential — several wheels have no prebuilt arm/x86 form and compile.
# ca-certificates, curl — package installs and model API calls need TLS.
# ripgrep      — the agents reach for it; without it they fall back to slow greps.
# less         — git's pager. Without it `git log` dies in a way that reads as a
#                repository fault rather than a missing binary.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git \
      python3 \
      python3-venv \
      python3-pip \
      build-essential \
      ca-certificates \
      curl \
      ripgrep \
      less \
 && rm -rf /var/lib/apt/lists/*

# Both vendors, because a task may be given to either and the image must not
# decide that. **These default to `latest` and `build` passes no override, so every
# build takes whatever is newest that day.** The mechanism to pin exists and is not
# used; saying so is the point, because the comment here previously claimed pinning
# that was not happening and the README's version table is a snapshot of one build.
# An unpinned agent CLI changes behaviour under a session
# that did not ask it to.
ARG CLAUDE_CODE_VERSION=latest
ARG CODEX_VERSION=latest
RUN npm install -g \
      "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
      "@openai/codex@${CODEX_VERSION}" \
 && npm cache clean --force

# A non-root user. Not a security boundary — the container is the boundary —
# but files written into the scratch drawer land owned by a real uid instead of
# root, which otherwise leaves output the host cannot delete without sudo.
ARG UID=1000
ARG GID=1000
RUN if ! getent group "${GID}" >/dev/null; then groupadd -g "${GID}" agent; fi \
 && if ! getent passwd "${UID}" >/dev/null; then \
      useradd -m -u "${UID}" -g "${GID}" -s /bin/bash agent; \
    fi

# /src  — the host repository, mounted read-only at run time. Never written.
# /work — the agent's own clone. Its filesystem, not the host's.
# /out  — the scratch drawer, the one writable host path. Bundles land here.
RUN mkdir -p /src /work /out && chown -R "${UID}:${GID}" /work /out

# The two auth directories, created here and owned by the agent, because Docker
# decides a named volume's ownership from what it finds at the mount point. Absent
# from the image, the mount point is created by the daemon owned by root, and the
# container — which runs as ${UID} — cannot write it. That is not a theory: the
# first headless `codex login` in this image printed "failed to create login log
# directory /home/agent/.codex/log: Permission denied" and would have failed the
# same way in front of whoever ran the sign-in.
RUN mkdir -p /home/agent/.claude /home/agent/.codex \
 && chown -R "${UID}:${GID}" /home/agent

# The standing brief for any session that starts in here. Copied to /work by the
# launcher after the clone, so an agent reads it as the project's own CLAUDE.md
# would be read. Kept at a fixed path so the launcher never guesses.
COPY operations/autoclave/agent-brief.md /opt/autoclave/CLAUDE.md
# Also at the filesystem root. An agent CLI reads CLAUDE.md from its working
# directory upward, so a copy at `/` is picked up as an ancestor instruction
# alongside the repository's own at /work/CLAUDE.md — the agent gets both the
# chamber's limits and the project's rules without either overwriting the other.
# Written here as root, before the USER switch, because `/` is not writable after.
COPY operations/autoclave/agent-brief.md /CLAUDE.md
# **And under the name the other vendor reads.** Claude Code looks for `CLAUDE.md`;
# Codex looks for `AGENTS.md`. Only the first existed, so every Codex agent
# dispatched into a chamber may have been working with no standing limits at all
# beyond whatever its prompt happened to say — including the audit and review seats
# run on 2026-08-02. Mirroring the same file under both names is the catch-all:
# whichever CLI is dispatched, it finds the brief under the name it already looks
# for, and neither has to be told. The file says it exists under both names, so a
# reader who finds one does not go hunting for a different one.
COPY operations/autoclave/agent-brief.md /AGENTS.md

# The tests need pytest and ruff. Installed into a venv on the PATH, so an agent
# runs `python3 -m pytest` exactly as the git hooks do on the host.
COPY requirements-dev.txt /opt/autoclave/requirements-dev.txt
# Build context is the repository root, so both COPY paths are written from
# there. `.dockerignore` at that root denies everything and re-admits exactly
# these two files — `private/` holds the notification topic and must never
# enter a build context, and a deny-all list is the only form that stays true
# when someone adds a new secret drawer later.
RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir -r /opt/autoclave/requirements-dev.txt
ENV PATH="/opt/venv/bin:${PATH}"
# And again for login shells, which is not redundant. `ENV` sets the environment of
# the process the daemon starts; a **login** shell rebuilds PATH from `/etc/profile`
# and throws that away. Codex runs every model-generated command through
# `bash -lc`, so the first real dispatch into this image reported
# "No module named pytest" and "ruff: command not found" while both sat on the
# non-login PATH — the chamber's whole selling point, an agent running its own tests
# before handing work back, was silently off. Found by dispatching, not by reading.
RUN printf 'PATH="/opt/venv/bin:$PATH"\nexport PATH\n' \
      > /etc/profile.d/10-autoclave-venv.sh

# Git refuses to operate in a directory owned by another uid. The clone is made
# by this user, but /src arrives owned by the host's, so mark both safe.
RUN git config --system --add safe.directory /src \
 && git config --system --add safe.directory /work

USER ${UID}:${GID}
# **`HOME` is pinned, not inherited from whichever account holds this UID.** The block
# above skips `useradd` when the requested UID already exists, and in `node:22-trixie`
# UID 1000 already does — the base image's own `node` account, whose home is
# `/home/node`. On a host whose user is 1000, which is most Linux machines and most CI,
# the container would then run with `HOME=/home/node` while every credential volume
# mounts under `/home/agent`: both CLIs would look for their sign-in in the wrong place
# and report themselves signed out. It does not reproduce on the machine this was built
# for, whose UID is 501, which is exactly why it needed stating rather than testing.
# Found by CodeRabbit on pull request 15. The directory is already created and chowned
# to ${UID}:${GID} above, so pinning the variable is the whole of the fix.
ENV HOME=/home/agent
WORKDIR /work

# No entrypoint that runs an agent. The launcher decides what starts, so the
# same image serves a writer, a reader, or a person opening a shell to look.
CMD ["/bin/bash"]
