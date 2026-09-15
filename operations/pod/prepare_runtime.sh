#!/usr/bin/env bash
# Prepare a fresh Ubuntu 24.04 RunPod checkout before `uv sync`.
#
# This is deliberately a host prerequisite, not part of bootstrap: bootstrap needs
# both uv and its confinement boundary before it can run.  It installs no models,
# credentials, or repository dependencies.
set -euo pipefail

readonly UV_VERSION="0.12.1"
readonly UV_ARCHIVE="uv-x86_64-unknown-linux-gnu.tar.gz"
readonly UV_URL="https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/${UV_ARCHIVE}"
readonly UV_SHA256="90b2f223fb69d19db49e117da601f64978593417988530aa733d456141b4bcbb"
readonly UTIL_LINUX_VERSION="2.42.3"
readonly UTIL_LINUX_ARCHIVE="util-linux-${UTIL_LINUX_VERSION}.tar.xz"
readonly UTIL_LINUX_URL="https://www.kernel.org/pub/linux/utils/util-linux/v2.42/${UTIL_LINUX_ARCHIVE}"
readonly UTIL_LINUX_SHA256="66ac7c0e725278eb2b039e3104f2c91119341d941b41bac7a285c695f940bd57"
readonly SETPRIV="/usr/bin/setpriv"
readonly UV="/usr/local/bin/uv"
readonly BACKUP_DIR="/usr/local/lib/verbatus-runtime-prerequisites"
readonly CURL_CONNECT_TIMEOUT_SECONDS=20
readonly CURL_MAX_TIME_SECONDS=300
readonly CURL_RETRIES=3

require_root() {
    if [[ "$(id -u)" -ne 0 ]]; then
        echo "Run as root (for example: sudo bash operations/pod/prepare_runtime.sh)." >&2
        exit 1
    fi
}

landlock_probe() {
    "$1" --no-new-privs --landlock-access fs:write-file -- /bin/true
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
    echo "$UV_SHA256  $archive" | sha256sum --check --status
    tar -xzf "$archive" -C "$workdir"
    install -D -m 0755 "$workdir/uv-x86_64-unknown-linux-gnu/uv" "$UV"
    uv_is_pinned
    rm -rf "$workdir"
    trap - RETURN
}

install_setpriv() {
    if [[ -x "$SETPRIV" ]] && landlock_probe "$SETPRIV"; then
        return
    fi

    local workdir archive source built backup
    workdir="$(mktemp -d)"
    trap 'rm -rf "$workdir"' RETURN
    archive="$workdir/$UTIL_LINUX_ARCHIVE"
    download "$archive" "$UTIL_LINUX_URL"
    echo "$UTIL_LINUX_SHA256  $archive" | sha256sum --check --status
    tar -xJf "$archive" -C "$workdir"
    source="$workdir/util-linux-$UTIL_LINUX_VERSION"
    (
        cd "$source"
        ./configure --disable-all-programs --enable-setpriv --disable-nls
        make -j4 setpriv
    )
    built="$source/setpriv"
    landlock_probe "$built" || {
        echo "The kernel does not provide a working Landlock boundary; refusing to replace setpriv." >&2
        exit 1
    }

    mkdir -p "$BACKUP_DIR"
    backup="$BACKUP_DIR/setpriv.before-util-linux-${UTIL_LINUX_VERSION}"
    if [[ -x "$SETPRIV" && ! -e "$backup" ]]; then
        cp --preserve=mode,timestamps "$SETPRIV" "$backup"
    fi
    install -m 0755 "$built" "$SETPRIV"
    if ! landlock_probe "$SETPRIV"; then
        if [[ -e "$backup" ]]; then
            install -m 0755 "$backup" "$SETPRIV"
        fi
        echo "Installed setpriv did not establish Landlock; restored the prior binary." >&2
        exit 1
    fi
    rm -rf "$workdir"
    trap - RETURN
}

main() {
    require_root
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y --no-install-recommends \
        build-essential ca-certificates curl xz-utils pkg-config libcap-ng-dev ninja-build
    command -v ninja >/dev/null
    install_uv
    install_setpriv
    echo "Runtime prerequisites ready: $UV ($UV_VERSION), $("$SETPRIV" --version | head -n 1), /usr/bin/ninja"
}

main "$@"
