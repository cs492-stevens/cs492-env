#!/usr/bin/env python3.14
"""Bootstrap, configure, and package the AMD64 Debian guest."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


BUILD_ROOTFS = Path("/build/rootfs")
RAW_DISK = Path("/build/debian.raw")
ARTIFACTS = Path("/artifacts")
OUTPUT = ARTIFACTS / "debian-base.qcow2"
PYTHON_ASSETS = Path("/python-assets")
ENVIRONMENT_NAME = "cs492-env"

GUEST_PACKAGES = (
    "apt",
    "systemd-sysv",
    "systemd-resolved",
    "dbus",
    "udev",
    "linux-image-amd64",
    "linux-headers-amd64",
    "build-essential",
    "make",
    "kmod",
    "sudo",
    "bash-completion",
    "ca-certificates",
    "curl",
    "file",
    "fuse3",
    "git",
    "gdb",
    "just",
    "less",
    "libfuse3-dev",
    "nano",
    "pkg-config",
    "python3",
    "strace",
    "tmux",
    "vim",
    "valgrind",
    "iproute2",
    "iputils-ping",
    "procps",
)


@dataclass(frozen=True)
class BuildConfig:
    """Values supplied by the Docker build for one construction phase."""

    phase: str
    release: str
    snapshot: str
    disk_size: str
    python_version: str

    @property
    def rootfs(self) -> Path:
        return Path("/") if self.phase == "configure" else BUILD_ROOTFS

    @property
    def mirror(self) -> str:
        return f"https://snapshot.debian.org/archive/debian/{self.snapshot}/"

    @property
    def security_mirror(self) -> str:
        return (
            "https://snapshot.debian.org/archive/debian-security/"
            f"{self.snapshot}/"
        )


def run(*command: str) -> None:
    """Run a required image-construction command and stop on failure."""
    environment = os.environ | {
        "DEBIAN_FRONTEND": "noninteractive",
        "LC_ALL": "C.UTF-8",
    }
    subprocess.run(command, check=True, env=environment)


def run_guest(config: BuildConfig, *command: str, user: str | None = None) -> None:
    """Run a command while the guest filesystem is the Docker stage root."""
    if config.phase != "configure":
        raise RuntimeError("guest commands can run only during configuration")
    if user is None:
        run(*command)
        return
    run(
        "setpriv",
        f"--reuid={user}",
        f"--regid={user}",
        "--init-groups",
        *command,
    )


def write_lines(path: Path, *lines: str) -> None:
    """Write a generated configuration file with a final newline."""
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def install_file(source: Path, destination: Path, mode: int) -> None:
    """Copy a file into the rootfs and assign its guest permissions."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    destination.chmod(mode)


def prepare_bootstrap(config: BuildConfig) -> None:
    """Create the directory populated by the extraction-only bootstrap."""
    BUILD_ROOTFS.parent.mkdir(exist_ok=True)
    BUILD_ROOTFS.mkdir(exist_ok=True)


def bootstrap_rootfs(config: BuildConfig) -> None:
    """Download and extract the pinned Debian package closure."""
    # The extract variant never enters the target. Package configuration happens
    # in the next Docker stage, where /proc is available at the stage root even
    # under Docker Desktop's architecture emulation.
    run(
        "mmdebstrap",
        "--mode=root",
        "--variant=extract",
        "--format=directory",
        "--architectures=amd64",
        "--components=main",
        "--skip=chroot/mount",
        "--skip=cleanup/apt",
        "--include=?or(?essential,?priority(required))",
        f"--include={','.join(GUEST_PACKAGES)}",
        '--aptopt=Acquire::Check-Valid-Until "false"',
        '--aptopt=Acquire::http::No-Cache "true"',
        '--aptopt=Acquire::https::No-Cache "true"',
        '--aptopt=Acquire::http::Pipeline-Depth "0"',
        '--aptopt=Acquire::https::Pipeline-Depth "0"',
        '--aptopt=Acquire::BrokenProxy "true"',
        config.release,
        str(BUILD_ROOTFS),
        f"deb [check-valid-until=no] {config.mirror} {config.release} main",
        (
            "deb [check-valid-until=no] "
            f"{config.security_mirror} {config.release}-security main"
        ),
    )


def clear_bootstrap_devices(config: BuildConfig) -> None:
    """Leave an empty /dev for devtmpfs instead of copying device nodes."""
    device_directory = config.rootfs / "dev"
    for entry in device_directory.iterdir():
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    (device_directory / "pts").mkdir()
    (device_directory / "shm").mkdir()


def install_guest_uv(config: BuildConfig) -> None:
    """Copy the bootstrap stage's AMD64 uv executables into the guest."""
    for executable in ("uv", "uvx"):
        install_file(
            Path("/usr/local/bin") / executable,
            config.rootfs / "usr/local/bin" / executable,
            0o555,
        )


def create_dev_account(config: BuildConfig) -> None:
    """Create the interactive user and freeze kernel package versions."""
    run_guest(
        config,
        "useradd",
        "--create-home",
        "--user-group",
        "--shell",
        "/bin/bash",
        "dev",
    )
    run_guest(
        config,
        "apt-mark",
        "hold",
        "linux-image-amd64",
        "linux-headers-amd64",
    )


def install_guest_python(config: BuildConfig) -> None:
    """Install the host-downloaded AMD64 Python tree for the guest user."""
    version_parts = config.python_version.split(".")
    if len(version_parts) < 2 or not all(part.isdigit() for part in version_parts):
        raise RuntimeError(f"invalid Python version: {config.python_version}")
    minor_version = ".".join(version_parts[:2])
    installation_name = f"cpython-{minor_version}-linux-x86_64-gnu"

    home_directory = config.rootfs / "home/dev"
    local_directory = home_directory / ".local"
    install_directory = local_directory / "share/uv/python"
    install_directory.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(PYTHON_ASSETS, install_directory)

    # uv records this alias as an absolute link to its download directory.
    # Replace it after relocation so it resolves inside the guest home.
    full_installations = sorted(
        path
        for path in install_directory.iterdir()
        if path.name.startswith(f"cpython-{minor_version}.")
        and path.name.endswith("-linux-x86_64-gnu")
        and path.is_dir()
    )
    if len(full_installations) != 1:
        raise RuntimeError(
            f"expected one managed Python {minor_version} installation, "
            f"found {len(full_installations)}"
        )
    installation = install_directory / installation_name
    installation.unlink(missing_ok=True)
    installation.symlink_to(full_installations[0].name)

    interpreter = install_directory / installation_name / "bin" / f"python{minor_version}"
    if not interpreter.is_file():
        raise RuntimeError(f"managed Python executable not found: {interpreter}")

    bin_directory = local_directory / "bin"
    bin_directory.mkdir()
    relative_interpreter = (
        Path("../share/uv/python")
        / installation_name
        / "bin"
        / f"python{minor_version}"
    )
    for executable in ("python", "python3", f"python{minor_version}"):
        (bin_directory / executable).symlink_to(relative_interpreter)

    account = home_directory.stat()
    for path in (local_directory, *local_directory.rglob("*")):
        os.lchown(path, account.st_uid, account.st_gid)


def configure_interactive_login(config: BuildConfig) -> None:
    """Configure sudo, serial autologin, workspace ownership, and the shell."""
    rootfs = config.rootfs
    write_lines(rootfs / "etc/sudoers.d/dev", "dev ALL=(ALL) NOPASSWD:ALL")
    (rootfs / "etc/sudoers.d/dev").chmod(0o440)

    # The serial console is the Docker user's terminal, so autologin avoids an
    # artificial password prompt while sudo remains available inside the VM.
    getty_override = rootfs / "etc/systemd/system/serial-getty@ttyS0.service.d"
    getty_override.mkdir(parents=True, exist_ok=True)
    write_lines(
        getty_override / "autologin.conf",
        "[Unit]",
        "Wants=configure-dev.service",
        "After=configure-dev.service",
        "",
        "[Service]",
        "ExecStart=",
        "ExecStart=-/sbin/agetty --autologin dev --noclear %I $TERM",
    )

    install_file(
        Path("/guest/configure-dev.sh"),
        rootfs / "usr/local/sbin/configure-dev",
        0o555,
    )
    write_lines(
        rootfs / "etc/systemd/system/configure-dev.service",
        "[Unit]",
        "Description=Match the dev account to the shared workspace owner",
        "After=local-fs.target",
        "",
        "[Service]",
        "Type=oneshot",
        "ExecStart=/usr/local/sbin/configure-dev",
        "RemainAfterExit=yes",
    )

    write_lines(
        rootfs / "etc/motd",
        "CS492 kernel module development environment",
        "",
        "Shared files are mounted at /workspace.",
        "Use sudo insmod, sudo rmmod, and sudo dmesg for module work.",
        "Run sudo poweroff to exit cleanly.",
    )
    with (rootfs / "home/dev/.bashrc").open("a", encoding="utf-8") as bashrc:
        bashrc.write('\nexport PATH="$HOME/.local/bin:$PATH"\n')
        bashrc.write(f"export PS1='[\\u@{ENVIRONMENT_NAME} \\W]\\$ '\n")


def prepare_boot_files(config: BuildConfig) -> None:
    """Enable guest services and produce the final initramfs."""
    run_guest(
        config,
        "systemctl",
        "enable",
        "systemd-networkd.service",
        "systemd-resolved.service",
    )
    run_guest(config, "update-initramfs", "-u", "-k", "all")


def prepare_artifacts(config: BuildConfig) -> None:
    """Create the output directory for kernel and disk artifacts."""
    del config
    ARTIFACTS.mkdir(exist_ok=True)


def write_system_configuration(config: BuildConfig) -> None:
    """Generate identity, package, storage, and network configuration."""
    rootfs = config.rootfs
    write_lines(rootfs / "etc/hostname", ENVIRONMENT_NAME)
    write_lines(
        rootfs / "etc/hosts",
        "127.0.0.1 localhost",
        f"127.0.1.1 {ENVIRONMENT_NAME}",
        "::1 localhost ip6-localhost ip6-loopback",
    )
    write_lines(
        rootfs / "etc/apt/sources.list",
        f"deb [check-valid-until=no] {config.mirror} {config.release} main",
        (
            "deb [check-valid-until=no] "
            f"{config.security_mirror} {config.release}-security main"
        ),
    )
    write_lines(
        rootfs / "etc/apt/apt.conf.d/99snapshot",
        'Acquire::Check-Valid-Until "false";',
    )
    install_file(
        Path("/config/apt/99broken-proxy"),
        rootfs / "etc/apt/apt.conf.d/99broken-proxy",
        0o444,
    )

    # QEMU presents the root image as /dev/vda and exports the host workspace
    # over 9p under the mount tag "work".
    write_lines(
        rootfs / "etc/fstab",
        f"LABEL={ENVIRONMENT_NAME} / ext4 defaults 0 1",
        "work /workspace 9p trans=virtio,version=9p2000.L,msize=262144 0 0",
    )
    (rootfs / "workspace").mkdir(exist_ok=True)

    (rootfs / "etc/systemd/network").mkdir(parents=True, exist_ok=True)
    write_lines(
        rootfs / "etc/systemd/network/20-wired.network",
        "[Match]",
        "Name=en* eth*",
        "",
        "[Network]",
        "DHCP=yes",
    )


def finalize_guest_files(config: BuildConfig) -> None:
    """Restore runtime-managed identity and resolver files."""
    rootfs = config.rootfs
    resolv_conf = rootfs / "etc/resolv.conf"
    resolv_conf.unlink(missing_ok=True)
    resolv_conf.symlink_to("/run/systemd/resolve/stub-resolv.conf")

    # Each writable overlay receives its own machine ID on first boot.
    machine_id = rootfs / "etc/machine-id"
    machine_id.write_text("", encoding="utf-8")
    machine_id.chmod(0o644)
    (rootfs / "var/lib/dbus/machine-id").unlink(missing_ok=True)


def copy_boot_artifacts(config: BuildConfig) -> None:
    """Copy the newest installed kernel and initramfs out of the rootfs."""
    kernels = sorted((config.rootfs / "boot").glob("vmlinuz-*"))
    initrds = sorted((config.rootfs / "boot").glob("initrd.img-*"))
    if not kernels or not initrds:
        raise RuntimeError("could not locate the guest kernel or initramfs")
    install_file(kernels[-1], ARTIFACTS / "vmlinuz", 0o444)
    install_file(initrds[-1], ARTIFACTS / "initrd.img", 0o444)


def create_guest_disk(config: BuildConfig) -> None:
    """Build an ext4 image from the rootfs and compress it as QCOW2."""
    # Direct kernel boot avoids a bootloader and partition table.
    run("truncate", "-s", config.disk_size, str(RAW_DISK))
    run(
        "mkfs.ext4",
        "-q",
        "-F",
        "-L",
        ENVIRONMENT_NAME,
        "-d",
        str(config.rootfs),
        str(RAW_DISK),
    )
    run(
        "qemu-img",
        "convert",
        "-q",
        "-c",
        "-f",
        "raw",
        "-O",
        "qcow2",
        str(RAW_DISK),
        str(OUTPUT),
    )
    run("qemu-img", "info", str(OUTPUT))


def record_base_id(config: BuildConfig) -> None:
    """Identify the immutable base image used by persistent overlays."""
    del config
    digest = hashlib.sha256()
    with OUTPUT.open("rb") as image:
        for chunk in iter(lambda: image.read(1024 * 1024), b""):
            digest.update(chunk)
    write_lines(ARTIFACTS / "base-id", digest.hexdigest())


def clean_build_files(config: BuildConfig) -> None:
    """Remove intermediate rootfs and raw disk data from the builder layer."""
    shutil.rmtree(config.rootfs)
    RAW_DISK.unlink()


# Each phase runs in a stage suited to its requirements. In particular, no
# operation performs a nested chroot or mount inside a Docker build container.
PHASE_OPERATIONS = {
    "bootstrap": (
        prepare_bootstrap,
        bootstrap_rootfs,
        clear_bootstrap_devices,
        install_guest_uv,
    ),
    "configure": (
        create_dev_account,
        install_guest_python,
        configure_interactive_login,
        prepare_boot_files,
    ),
    "package": (
        prepare_artifacts,
        write_system_configuration,
        finalize_guest_files,
        copy_boot_artifacts,
        create_guest_disk,
        record_base_id,
        clean_build_files,
    ),
}


def parse_config(arguments: Sequence[str]) -> BuildConfig:
    """Validate positional build arguments and construct phase config."""
    if len(arguments) != 5 or arguments[0] not in PHASE_OPERATIONS:
        phases = "|".join(PHASE_OPERATIONS)
        raise SystemExit(
            f"usage: build-guest {phases} RELEASE SNAPSHOT DISK_SIZE PYTHON_VERSION"
        )
    return BuildConfig(*arguments)


def main() -> None:
    config = parse_config(sys.argv[1:])
    for operation in PHASE_OPERATIONS[config.phase]:
        description = operation.__doc__ or operation.__name__
        print(f"==> {description}", flush=True)
        operation(config)


if __name__ == "__main__":
    main()
