#!/usr/bin/env bash
set -euo pipefail

image=${IMAGE:-cs492-env:dev}
state_volume=${STATE_VOLUME:-cs492-env-dev}
container_name=${CONTAINER_NAME:-cs492-env}
workspace=${WORKSPACE:-$(pwd)}
mode=${VM_MODE:-persistent}
host_uid=${HOST_UID:-$(id -u)}
host_gid=${HOST_GID:-$(id -g)}

# Running QEMU as the invoking UID/GID is part of the workspace security model;
# allowing host UID 0 would let guest root create host-root-owned files.
if [[ "${mode}" != persistent && "${mode}" != ephemeral ]]; then
    printf 'VM_MODE must be either persistent or ephemeral.\n' >&2
    exit 2
fi
if [[ "${host_uid}" == 0 || "${host_gid}" == 0 ]]; then
    printf 'Run the environment as a regular host user, not as root.\n' >&2
    exit 2
fi

docker_args=(
    --rm
    --interactive
    --tty
    --name "${container_name}"
    --env "VM_MODE=${mode}"
    --env "HOST_UID=${host_uid}"
    --env "HOST_GID=${host_gid}"
    --security-opt no-new-privileges
)

# SELinux hosts must relabel the bind mount for the container. Other platforms,
# including Docker Desktop on macOS, use an ordinary bind mount.
if command -v getenforce >/dev/null && [[ "$(getenforce)" == Enforcing ]]; then
    docker_args+=(--volume "${workspace}:/workspace:Z")
else
    docker_args+=(--mount "type=bind,source=${workspace},target=/workspace")
fi

# Only the named volume survives `docker run --rm`; the outer container itself
# is intentionally disposable on every invocation.
if [[ "${mode}" == persistent ]]; then
    docker volume create "${state_volume}" >/dev/null
    docker_args+=(--mount "type=volume,source=${state_volume},target=/state")
fi

# Forward resource overrides only when the caller set them. Otherwise the
# defaults embedded in the image remain in effect.
if [[ -n "${VM_CPUS:-}" ]]; then
    docker_args+=(--env "VM_CPUS=${VM_CPUS}")
fi
if [[ -n "${VM_MEMORY:-}" ]]; then
    docker_args+=(--env "VM_MEMORY=${VM_MEMORY}")
fi
if [[ -n "${VM_ACCEL:-}" ]]; then
    docker_args+=(--env "VM_ACCEL=${VM_ACCEL}")
fi

# KVM only accelerates an AMD64 guest on an AMD64 Linux Docker host.
if [[ "$(uname -s)" == Linux && "$(uname -m)" == x86_64 && -c /dev/kvm && "${VM_ACCEL:-auto}" != tcg ]]; then
    docker_args+=(--device /dev/kvm)
    docker_args+=(--group-add "$(stat -c %g /dev/kvm)")
fi

exec docker run "${docker_args[@]}" "${image}"
