#!/usr/bin/env bash
# Prepare a fresh Ubuntu 24.04 RunPod checkout before `uv sync`.
#
# A host prerequisite, not part of bootstrap: bootstrap needs uv before it can run.
# It installs no models, credentials, or repository dependencies.
set -euo pipefail

readonly UV_VERSION="0.12.1"
readonly UV_ARCHIVE="uv-x86_64-unknown-linux-gnu.tar.gz"
readonly UV_URL="https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/${UV_ARCHIVE}"
readonly UV_SHA256="90b2f223fb69d19db49e117da601f64978593417988530aa733d456141b4bcbb"
readonly UV="/usr/local/bin/uv"
readonly CURL_CONNECT_TIMEOUT_SECONDS=20
readonly CURL_MAX_TIME_SECONDS=300
readonly CURL_RETRIES=3

require_root() {
    if [[ "$(id -u)" -ne 0 ]]; then
        echo "Run as root (for example: sudo bash operations/pod/prepare_runtime.sh)." >&2
        exit 1
    fi
}

download() {
    local destination url
    destination="$1"
    url="$2"
    curl --fail --location --proto '=https' --tlsv1.2 \
        --connect-timeout "$CURL_CONNECT_TIMEOUT_SECONDS" \
        --max-time "$CURL_MAX_TIME_SECONDS" \
        --retry "$CURL_RETRIES" --retry-delay 2 --retry-connrefused \
        --output "$destination" "$url"
}

uv_is_pinned() {
    local version
    version="$("$UV" --version 2>/dev/null)" || return 1
    [[ "$version" == "uv ${UV_VERSION}" || "$version" == "uv ${UV_VERSION} "* ]]
}

install_uv() {
    if [[ -x "$UV" ]] && uv_is_pinned; then
        return
    fi

    local workdir archive
    workdir="$(mktemp -d)"
    trap 'rm -rf "$workdir"' RETURN
    archive="$workdir/$UV_ARCHIVE"
    download "$archive" "$UV_URL"
    # Named, not a bare status: a silent non-zero cannot tell a bad download from a bad pin.
    if ! echo "$UV_SHA256  $archive" | sha256sum --check --status; then
        echo "uv archive $archive failed its pinned sha256 $UV_SHA256" >&2
        return 1
    fi
    tar -xzf "$archive" -C "$workdir"
    install -D -m 0755 "$workdir/uv-x86_64-unknown-linux-gnu/uv" "$UV"
    if ! uv_is_pinned; then
        echo "installed uv at $UV is not the pinned version $UV_VERSION" >&2
        return 1
    fi
    rm -rf "$workdir"
    trap - RETURN
}

main() {
    require_root
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y --no-install-recommends \
        build-essential ca-certificates curl ninja-build
    command -v ninja >/dev/null
    install_uv
    echo "Runtime prerequisites ready: $UV ($UV_VERSION), /usr/bin/ninja"
}

main "$@"
