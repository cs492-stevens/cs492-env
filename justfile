set shell := ["bash", "-euo", "pipefail", "-c"]

image := env_var_or_default("IMAGE", "cs492-env")
tag := env_var_or_default("TAG", "dev")
image_ref := image + ":" + tag
snapshot := env_var_or_default("DEBIAN_SNAPSHOT", "20261001T000000Z")
release := env_var_or_default("DEBIAN_RELEASE", "trixie")
volume_prefix := replace(replace(replace(image, "/", "-"), ".", "-"), ":", "-")
state_volume := env_var_or_default("STATE_VOLUME", volume_prefix + "-" + tag)
container_name := env_var_or_default("CONTAINER_NAME", "cs492-env")
workspace := justfile_directory()

default:
    @just --list

# Build a runtime image for the current host architecture.
build:
    docker build \
      --build-arg "DEBIAN_RELEASE={{release}}" \
      --build-arg "DEBIAN_SNAPSHOT={{snapshot}}" \
      --build-arg "IMAGE_VERSION={{tag}}" \
      --tag "{{image_ref}}" \
      .

# Publish native AMD64 and ARM64 runtime images containing the same AMD64 guest.
publish:
    docker buildx build \
      --platform linux/amd64,linux/arm64 \
      --build-arg "DEBIAN_RELEASE={{release}}" \
      --build-arg "DEBIAN_SNAPSHOT={{snapshot}}" \
      --build-arg "IMAGE_VERSION={{tag}}" \
      --tag "{{image_ref}}" \
      --push \
      .

# Pull the configured prebuilt image.
pull:
    docker pull "{{image_ref}}"

# Start the VM using its persistent disk overlay.
run:
    IMAGE="{{image_ref}}" \
    STATE_VOLUME="{{state_volume}}" \
    CONTAINER_NAME="{{container_name}}" \
    WORKSPACE="{{workspace}}" \
    ./scripts/run.sh

# Start from a temporary overlay that is discarded on exit.
clean:
    IMAGE="{{image_ref}}" \
    CONTAINER_NAME="{{container_name}}" \
    WORKSPACE="{{workspace}}" \
    VM_MODE=ephemeral \
    ./scripts/run.sh

# Stop a currently running VM.
stop:
    docker stop "{{container_name}}"

# Permanently discard the persistent VM disk. Workspace files are unaffected.
reset:
    #!/usr/bin/env bash
    if docker container inspect "{{container_name}}" >/dev/null 2>&1; then
        printf 'Stop the VM with `just stop` before resetting it.\n' >&2
        exit 1
    fi
    if docker volume inspect "{{state_volume}}" >/dev/null 2>&1; then
        docker volume rm "{{state_volume}}"
    else
        printf 'Persistent volume %s does not exist.\n' "{{state_volume}}"
    fi

# Display the persistent disk volume and image configuration.
status:
    @printf 'Image:      %s\n' "{{image_ref}}"
    @printf 'Volume:     %s\n' "{{state_volume}}"
    @printf 'Workspace:  %s\n' "{{workspace}}"
    @docker volume inspect "{{state_volume}}" 2>/dev/null || true

# Run local static checks that do not require a Docker daemon.
check:
    bash -n scripts/*.sh guest/*.sh
    @if command -v python3 >/dev/null; then python3 -c 'compile(open("scripts/build-guest.py").read(), "scripts/build-guest.py", "exec")'; fi
    @if command -v shellcheck >/dev/null; then shellcheck scripts/*.sh guest/*.sh; fi
    @if command -v apt-config >/dev/null; then apt-config dump -c config/apt/99broken-proxy >/dev/null; fi
