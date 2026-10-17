# CS492 Kernel Module Development Environment

This project packages a fixed AMD64 Debian Trixie VM inside a host-native Docker
image.
Users work in a normal Debian shell and can build, load, and unload external
kernel modules without granting the outer container permission to modify the
host kernel.

The published OCI image is intended to have two variants:

- An AMD64 container running the AMD64 guest with KVM when available.
- An ARM64 container running the same AMD64 guest with QEMU TCG.

Docker selects the host-native container variant. Do not force
`--platform linux/amd64` on Apple Silicon; the ARM64 container contains a native
ARM64 build of `qemu-system-x86_64`.

## Prerequisites

- Docker Engine or Docker Desktop
- [`just`](https://github.com/casey/just)
- Approximately 4 GB for the image and persistent VM state

The launcher targets ordinary rootful Docker Engine and Docker Desktop. Rootless
Docker and daemons configured with user-namespace remapping need additional UID
mapping work and are not supported by this first pass. SELinux-enforcing hosts
are detected and receive a private `:Z` label for the workspace bind mount.

Building the image also requires BuildKit support for an AMD64 build stage. This
is automatic on AMD64 and in Docker Desktop. An ARM64 Linux build host may need
binfmt/QEMU configured for Docker Buildx. Users of a published image do
not need build emulation support.

## Usage

After `IMAGE` and `TAG` have been configured for the image registry:

```sh
just pull
just run
```

The VM boots to an automatically logged-in `dev` shell. The repository is
mounted at `/workspace`, and the guest's root filesystem persists in a Docker
named volume. Shut the VM down cleanly with:

```sh
sudo poweroff
```

`just stop` can terminate a stuck VM, but an abrupt exit can require filesystem
journal recovery on the next boot.

To boot a disposable VM without changing persistent state:

```sh
just clean
```

To discard all changes made inside the persistent VM:

```sh
just reset
```

Resetting does not touch files under `/workspace`.

## Building And Testing A Module

The guest contains the compiler, `kmod`, and headers matching its pinned kernel,
along with the FUSE 3 runtime and development headers, `pkg-config`, `tmux`, full
`vim`, `just`, `uv`, and a uv-managed Python 3.14.
Kernel and header metapackages are held because QEMU boots the immutable kernel
shipped with the container; users should not upgrade those packages.
A minimal example is included:

```sh
cd /workspace/examples/hello
make
sudo insmod hello.ko
sudo dmesg | tail
sudo rmmod hello
sudo dmesg | tail
```

The guest reports `x86_64` from `uname -m` on every host architecture.

The managed Python is the default in interactive shells:

```sh
python --version
python3 --version
uv --version
just --version
```

Debian's system Python remains untouched; Python projects should use `uv` for
environments and dependencies.

## Publishing

Build an image for the current host architecture:

```sh
just build
```

Build and push a multi-architecture image through a configured Buildx builder:

```sh
IMAGE=registry.example.com/cs492-env TAG=2026.1 just publish
```

Users use the same values when pulling and running:

```sh
IMAGE=registry.example.com/cs492-env TAG=2026.1 just pull
IMAGE=registry.example.com/cs492-env TAG=2026.1 just run
```

For a published release, place `IMAGE` and `TAG` in a shared shell environment or
change their defaults in the `justfile`.

## Persistence Model

The container contains an immutable base disk at:

```text
/opt/vm/debian-base.qcow2
```

`just run` creates a small external overlay in the versioned Docker volume:

```text
/state/debian-overlay.qcow2
```

The overlay records all guest filesystem changes, including installed packages,
home-directory contents, and configuration. A SHA-256 identifier ties it to the
exact base disk. The entrypoint refuses to boot an old overlay against a changed
base image instead of risking filesystem corruption.

Use a new `TAG` for each incompatible image. This naturally gives it a
new default volume name. Source files should remain in `/workspace`, not only in
the VM disk, so moving between releases is straightforward.

Release tags must be immutable. Rebuilding and replacing the image under the
same tag can produce a different base-disk checksum and intentionally cause the
old overlay to be rejected.

## Configuration

The command runner accepts these environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `IMAGE` | `cs492-env` | OCI image repository |
| `TAG` | `dev` | Image tag and persistent-volume suffix |
| `STATE_VOLUME` | `cs492-env-$TAG` | Docker volume name |
| `VM_CPUS` | `2` | Guest virtual CPU count |
| `VM_MEMORY` | `2048` | Guest RAM in MiB |
| `VM_ACCEL` | `auto` | `auto`, `kvm`, or `tcg` |
| `VM_MODE` | `persistent` | `persistent` or `ephemeral` guest disk |
| `DEBIAN_RELEASE` | `trixie` | Debian guest and container release |
| `DEBIAN_SNAPSHOT` | `20261001T000000Z` | Debian snapshot used at build time |

`auto` uses KVM only when the container is running on AMD64 Linux with
`/dev/kvm` exposed. Docker Desktop and ARM64 hosts use multithreaded TCG.

The launcher passes the invoking user's numeric UID and GID into the guest and
runs QEMU under that identity. Guest root therefore cannot use the 9p workspace
share to create host-root-owned files. The QEMU command monitor is disabled.

## Image Construction

The multi-stage `Dockerfile` performs two separate jobs:

1. An AMD64 build stage installs uv and Python 3.14, then runs
   `scripts/build-guest.py`. That script uses `mmdebstrap` and a dated Debian
   snapshot to create the guest root filesystem, installs a Debian kernel,
   matching headers and development tools, and converts the result to a
   compressed QCOW2 image.
2. A host-native runtime stage installs `qemu-system-x86_64` and copies in the
   same AMD64 guest artifacts.

`mmdebstrap` is a Debian root-filesystem bootstrapper. It resolves packages from
APT repositories and installs them into an arbitrary directory without running
the normal graphical or text Debian installer. Here that directory is
`/build/rootfs` inside the temporary Docker builder container. Once configured,
the directory is copied into an ext4 filesystem image with `mkfs.ext4`; QEMU
later treats that image as the guest's root disk.

`build-guest.py` therefore does run inside a Docker build container, specifically
the fixed-AMD64 `guest-builder` stage. Python 3.14 and the image-building tools
exist only in that temporary stage. The final outer runtime image does not carry
Python or `mmdebstrap`; the Debian VM itself receives a separate uv-managed
Python 3.14 installation.

The VM kernel is booted directly by QEMU, so the disk does not need a partition
table or bootloader. Debian's systemd still runs as PID 1 inside the guest.

The Debian snapshot pins packages, but a production release should additionally
pin the digest of the `debian:trixie-slim` parent image in `Dockerfile` and
retain the resulting OCI image by digest.

The APT configuration in `config/apt/99broken-proxy` disables HTTP and HTTPS
cache use and pipelining, and includes the `BrokenProxy` compatibility setting.
It is used by both Docker build stages, `mmdebstrap`, and the resulting guest.
This avoids stale package responses commonly encountered behind Docker Desktop
or intercepting proxies, including on macOS.
