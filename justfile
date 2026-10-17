set shell := ["bash", "-euo", "pipefail", "-c"]

image := env_var_or_default("IMAGE", "cs492-env")
tag := env_var_or_default("TAG", "dev")
image_ref := image + ":" + tag
snapshot := env_var_or_default("DEBIAN_SNAPSHOT", "20261001T000000Z")
release := env_var_or_default("DEBIAN_RELEASE", "trixie")
workspace := justfile_directory()
python := env_var_or_default("PYTHON", "python3")

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
    PYTHONPATH=src "{{python}}" -m cs492_env \
      --offline \
      --image "{{image_ref}}" \
      --release "{{tag}}" \
      --workspace "{{workspace}}"

# Start from a temporary overlay that is discarded on exit.
clean:
    PYTHONPATH=src "{{python}}" -m cs492_env clean \
      --offline \
      --image "{{image_ref}}" \
      --release "{{tag}}" \
      --workspace "{{workspace}}"

# Stop a currently running VM.
stop:
    PYTHONPATH=src "{{python}}" -m cs492_env stop \
      --offline \
      --image "{{image_ref}}" \
      --release "{{tag}}"

# Permanently discard the persistent VM disk. Workspace files are unaffected.
reset:
    PYTHONPATH=src "{{python}}" -m cs492_env reset \
      --yes \
      --offline \
      --image "{{image_ref}}" \
      --release "{{tag}}"

# Display the persistent disk volume and image configuration.
status:
    PYTHONPATH=src "{{python}}" -m cs492_env status \
      --offline \
      --image "{{image_ref}}" \
      --release "{{tag}}" \
      --workspace "{{workspace}}"

# Run local static checks that do not require a Docker daemon.
check:
    bash -n scripts/*.sh guest/*.sh
    @if command -v python3 >/dev/null; then python3 -c 'compile(open("scripts/build-guest.py").read(), "scripts/build-guest.py", "exec")'; fi
    @if command -v python3 >/dev/null; then PYTHONPATH=src python3 -m unittest discover -s tests; fi
    @if command -v shellcheck >/dev/null; then shellcheck scripts/*.sh guest/*.sh; fi
    @if command -v apt-config >/dev/null; then apt-config dump -c config/apt/99broken-proxy >/dev/null; fi
