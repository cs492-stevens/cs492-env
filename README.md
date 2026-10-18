# CS492 Development Environment

This project packages a fixed AMD64 Debian Trixie VM inside a host-native Docker
image. Students work in a normal Debian shell and can develop external kernel
modules and FUSE 3 filesystems without granting the outer container permission
to modify the host kernel.

The published OCI image has native AMD64 and ARM64 container variants. Docker
selects the host-native variant, while both variants run the same AMD64 guest:

- AMD64 Linux can use KVM when `/dev/kvm` is available.
- Apple Silicon, macOS, and Windows use QEMU TCG emulation.

## Student Prerequisites

- Python 3.10 or newer
- Git
- Docker Engine or Docker Desktop configured for Linux containers
- Approximately 4 GB for the image and persistent VM state

Windows users should use Docker Desktop's WSL 2 backend. Docker Desktop handles
the Linux bind mount used to share the current Windows directory with the VM.

## Install The Launcher

Install a tagged launcher release directly from this Git repository.

macOS and Linux:

```sh
python3 -m pip install "cs492-env @ git+https://github.com/cs492-stevens/cs492-env.git@v0.1.0"
```

Windows PowerShell:

```powershell
py -m pip install "cs492-env @ git+https://github.com/cs492-stevens/cs492-env.git@v0.1.0"
```

The installation provides the `cs492-env` command. Run it from the directory
that should be mounted into the guest:

```sh
cd path/to/assignment
cs492-env
```

The VM boots to an automatically logged-in `dev` shell. The launch directory is
mounted at `/workspace`. Shut down cleanly from inside the guest with:

```sh
sudo poweroff
```

## Launcher Commands

| Command | Purpose |
| --- | --- |
| `cs492-env` | Start the persistent VM |
| `cs492-env clean` | Start a disposable VM without changing persistent state |
| `cs492-env pull` | Pull the currently selected image |
| `cs492-env status` | Display the release, image, volume, and local state |
| `cs492-env stop` | Stop a stuck VM |
| `cs492-env reset` | Delete the current release's persistent VM changes |
| `cs492-env --version` | Display the installed launcher version |

`reset` asks for confirmation and does not modify files under `/workspace`.

Common launcher options are:

| Option | Purpose |
| --- | --- |
| `--workspace PATH` | Mount another host directory instead of the current directory |
| `--cpus NUMBER` | Override the guest virtual CPU count |
| `--memory MIB` | Override guest memory |
| `--accel auto\|kvm\|tcg` | Override QEMU acceleration selection |
| `--offline` | Use cached release information without contacting the course website |
| `--yes` | Skip reset confirmation |

The existing `VM_CPUS`, `VM_MEMORY`, and `VM_ACCEL` environment variables are
also accepted.

## Persistence

The container contains an immutable base disk at:

```text
/opt/vm/debian-base.qcow2
```

For release `2026.1`, the launcher creates a Docker volume named:

```text
cs492-env-2026.1
```

The volume contains `/state/debian-overlay.qcow2`, which records package
installations, home-directory contents, and other guest filesystem changes.
The temporary outer container is removed after shutdown, but the named volume
and overlay remain.

Each release name selects a separate volume. When the course moves to a new
image, the old overlay remains in Docker but is not attached to the new base
disk. A SHA-256 identifier provides an additional check: the entrypoint refuses
to boot an overlay created for a different base image.

Source files persist independently through the `/workspace` host bind mount.

## Development Tools

The guest includes:

- The compiler, `make`, `kmod`, and headers matching the pinned guest kernel
- The FUSE 3 runtime, development headers, and `pkg-config`
- `gdb`, `strace`, Git, `tmux`, full `vim`, `nano`, and `just`
- uv and a uv-managed Python 3.14 installation

Kernel and header metapackages are held because QEMU directly boots the
immutable kernel shipped in the container. Students should not upgrade those
packages.

A minimal external kernel module is included under `examples/hello`:

```sh
cd /workspace/examples/hello
make
sudo insmod hello.ko
sudo dmesg | tail
sudo rmmod hello
```

The guest reports `x86_64` from `uname -m` on every host architecture.

## Release Selection

The launcher obtains the active release from:

```text
https://cs492-stevens.github.io/cs492-env/stable.json
```

The last valid manifest is cached in the platform's user cache directory. A
website outage therefore continues using the same release instead of silently
returning to an older image. If neither the website nor a cache is available,
the launcher uses the release metadata bundled with its installed version.

The launcher validates the manifest against an exact schema and constructs the image reference from a
trusted Docker Hub repository compiled into the package. The website cannot
redirect the launcher to another image repository. Images are run by their
immutable multi-architecture index digest.

The expected document is:

```json
{
  "schema_version": 1,
  "release": "2026.1",
  "image_tag": "2026.1",
  "image_digest": "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "minimum_cli_version": "0.1.0",
  "recommended_cli_version": "0.1.0",
  "notice": null
}
```

The trusted Docker Hub repository is `rtsang1/cs492-env`. Also replace the
unpublished bundled fallback digest before tagging the first launcher release.

## Maintainer Workflow

Maintainers need [`just`](https://github.com/casey/just) in addition to the
student prerequisites.

Build and run a local image:

```sh
just build
just run
```

The justfile invokes the Python launcher from `src/` with an offline local-image
override, so local development does not depend on the course website.

Build and publish the native AMD64 and ARM64 outer images containing the same
AMD64 guest:

```sh
IMAGE=rtsang1/cs492-env TAG=2026.1 just publish
```

After the multi-architecture push succeeds, the publish recipe reads BuildKit's
reported index digest and creates:

```text
release/stable.2026.1.json
```

Upload that file to the course website as:

```text
https://cs492-stevens.github.io/cs492-env/stable.json
```

No versioned manifest is written when the image build or push fails.

For each release:

1. Build and push a new immutable image tag.
2. Choose a new release name whenever the image contents change.
3. Run `just publish` to produce `release/stable.$TAG.json` from BuildKit metadata.
4. Upload that generated file as `/cs492-env/stable.json` on the course site.
5. Adjust the generated CLI compatibility fields if the release policy requires it.
6. Tag launcher releases as `vMAJOR.MINOR.PATCH` for the generated pip command.

`release/stable.example.json` is a publishable manifest template; its placeholder
digest must be replaced.

## Image Construction

The multi-stage `Dockerfile` separates guest construction into four stages:

1. A fixed-AMD64 bootstrap stage uses `mmdebstrap` in extraction-only mode to
   resolve and unpack the snapshot-pinned package closure.
2. A build-host-native tools stage runs uv to download both its native Python
   3.14 and an AMD64 managed Python installation without executing the latter.
3. A fixed-AMD64 scratch stage makes the extracted filesystem its own root,
   configures the packages and guest, and generates the initramfs.
4. A build-host-native stage copies the configured rootfs, writes it into an
   ext4 image, and converts the image to compressed QCOW2.

The final host-native runtime installs `qemu-system-x86_64` and copies in those
AMD64 guest artifacts. No construction phase requires a successful nested mount
or enters a nested chroot. This is important on Apple Silicon: Docker supplies
`/proc` at the scratch-stage root, allowing its architecture emulator to execute
Debian's AMD64 package scripts without `CAP_SYS_ADMIN`. uv itself runs only as a
native build-host binary. `mmdebstrap` does not boot a VM or run the Debian
installer.

The VM kernel is booted directly by QEMU, so the disk needs neither a partition
table nor a bootloader. Debian systemd runs as PID 1 inside the guest.

The APT configuration in `config/apt/99broken-proxy` disables cache use and
pipelining and enables the `BrokenProxy` compatibility setting. It is used by
the Docker stages, `mmdebstrap`, and the guest.

## Limitations

Rootless Docker and daemons configured with user-namespace remapping are not
currently supported. Full Windows and macOS launch validation requires testing
against Docker Desktop on those platforms. On SELinux-enforcing Linux hosts,
the launcher refuses to mount broad system or home roots because Docker's `:Z`
option would recursively relabel them; select a project subdirectory instead.
