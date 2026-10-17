#!/usr/bin/env python3.14
"""Construct the bootable Debian guest filesystem and QEMU artifacts."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


ROOTFS = Path("/build/rootfs")
RAW_DISK = Path("/build/debian.raw")
ARTIFACTS = Path("/artifacts")
OUTPUT = ARTIFACTS / "debian-base.qcow2"
ENVIRONMENT_NAME = "cs492-env"

GUEST_PACKAGES = (
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
    "strace",
    "tmux",
    "vim",
    "iproute2",
    "iputils-ping",
    "procps",
)


@dataclass(frozen=True)
class BuildConfig:
    """Values supplied by the Docker build for one guest image."""

    release: str
    snapshot: str
    disk_size: str
    python_version: str

    @property
    def mirror(self) -> str:
        return f"http://snapshot.debian.org/archive/debian/{self.snapshot}/"

    @property
    def security_mirror(self) -> str:
        return (
            "http://snapshot.debian.org/archive/debian-security/"
            f"{self.snapshot}/"
        )


def run(*command: str) -> None:
    """Run a required image-construction command and stop on failure."""
    subprocess.run(command, check=True)


def chroot(*command: str, userspec: str | None = None) -> None:
    """Run a command within the guest root filesystem."""
    chroot_command = ["chroot"]
    if userspec is not None:
        chroot_command.append(f"--userspec={userspec}")
    chroot_command.extend((str(ROOTFS), *command))
    run(*chroot_command)


def write_lines(path: Path, *lines: str) -> None:
    """Write a generated configuration file with a final newline."""
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def install_file(source: Path, destination: Path, mode: int) -> None:
    """Copy a file into the rootfs and assign its guest permissions."""
    shutil.copyfile(source, destination)
    destination.chmod(mode)


def prepare_directories(config: BuildConfig) -> None:
    """Create build output directories before bootstrapping Debian."""
    del config
    Path("/build").mkdir(exist_ok=True)
    ARTIFACTS.mkdir(exist_ok=True)


def bootstrap_rootfs(config: BuildConfig) -> None:
    """Resolve the pinned Debian package set into a plain directory."""
    # No VM, disk partition, or Debian installer is involved at this point.
    run(
        "mmdebstrap",
        "--variant=minbase",
        "--architectures=amd64",
        "--components=main",
        f"--include={','.join(GUEST_PACKAGES)}",
        '--aptopt=Acquire::Check-Valid-Until "false"',
        '--aptopt=Acquire::http::No-Cache "true"',
        '--aptopt=Acquire::https::No-Cache "true"',
        '--aptopt=Acquire::http::Pipeline-Depth "0"',
        '--aptopt=Acquire::https::Pipeline-Depth "0"',
        '--aptopt=Acquire::BrokenProxy "true"',
        config.release,
        str(ROOTFS),
        f"deb [check-valid-until=no] {config.mirror} {config.release} main",
        (
            "deb [check-valid-until=no] "
            f"{config.security_mirror} {config.release}-security main"
        ),
    )


def install_guest_uv(config: BuildConfig) -> None:
    """Copy the builder's architecture-compatible uv executables."""
    del config
    for executable in ("uv", "uvx"):
        install_file(
            Path("/usr/local/bin") / executable,
            ROOTFS / "usr/local/bin" / executable,
            0o555,
        )


def write_system_configuration(config: BuildConfig) -> None:
    """Generate identity, package, storage, and network configuration."""
    write_lines(ROOTFS / "etc/hostname", ENVIRONMENT_NAME)
    write_lines(
        ROOTFS / "etc/hosts",
        "127.0.0.1 localhost",
        f"127.0.1.1 {ENVIRONMENT_NAME}",
        "::1 localhost ip6-localhost ip6-loopback",
    )
    write_lines(
        ROOTFS / "etc/apt/sources.list",
        f"deb [check-valid-until=no] {config.mirror} {config.release} main",
        (
            "deb [check-valid-until=no] "
            f"{config.security_mirror} {config.release}-security main"
        ),
    )
    write_lines(
        ROOTFS / "etc/apt/apt.conf.d/99snapshot",
        'Acquire::Check-Valid-Until "false";',
    )
    install_file(
        Path("/config/apt/99broken-proxy"),
        ROOTFS / "etc/apt/apt.conf.d/99broken-proxy",
        0o444,
    )

    # QEMU presents the root image as /dev/vda and exports the host workspace
    # over 9p under the mount tag "work".
    write_lines(
        ROOTFS / "etc/fstab",
        f"LABEL={ENVIRONMENT_NAME} / ext4 defaults 0 1",
        "work /workspace 9p trans=virtio,version=9p2000.L,msize=262144 0 0",
    )
    (ROOTFS / "workspace").mkdir(exist_ok=True)

    (ROOTFS / "etc/systemd/network").mkdir(parents=True, exist_ok=True)
    write_lines(
        ROOTFS / "etc/systemd/network/20-wired.network",
        "[Match]",
        "Name=en* eth*",
        "",
        "[Network]",
        "DHCP=yes",
    )


def create_dev_account(config: BuildConfig) -> None:
    """Create the interactive user and freeze kernel package versions."""
    del config
    chroot(
        "useradd",
        "--create-home",
        "--user-group",
        "--shell",
        "/bin/bash",
        "dev",
    )
    chroot(
        "apt-mark",
        "hold",
        "linux-image-amd64",
        "linux-headers-amd64",
    )


def install_guest_python(config: BuildConfig) -> None:
    """Install the managed Python toolchain as the normal guest user."""
    # Keep build-time DNS working while uv runs inside the chroot. The final
    # symlink is restored after systemd-resolved has been configured.
    resolv_conf = ROOTFS / "etc/resolv.conf"
    resolv_conf.unlink(missing_ok=True)
    shutil.copyfile("/etc/resolv.conf", resolv_conf)

    guest_environment = (
        "env",
        "HOME=/home/dev",
        "PATH=/usr/local/bin:/usr/bin:/bin",
    )
    chroot(
        *guest_environment,
        "uv",
        "python",
        "install",
        config.python_version,
        "--default",
        userspec="dev:dev",
    )
    chroot(
        *guest_environment,
        "uv",
        "cache",
        "clean",
        userspec="dev:dev",
    )
    resolv_conf.unlink()
    resolv_conf.symlink_to("/run/systemd/resolve/stub-resolv.conf")


def configure_interactive_login(config: BuildConfig) -> None:
    """Configure sudo, serial autologin, workspace ownership, and the shell."""
    del config
    write_lines(ROOTFS / "etc/sudoers.d/dev", "dev ALL=(ALL) NOPASSWD:ALL")
    (ROOTFS / "etc/sudoers.d/dev").chmod(0o440)

    # The serial console is the Docker user's terminal, so autologin avoids an
    # artificial password prompt while sudo remains available inside the VM.
    getty_override = ROOTFS / "etc/systemd/system/serial-getty@ttyS0.service.d"
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
        ROOTFS / "usr/local/sbin/configure-dev",
        0o555,
    )
    write_lines(
        ROOTFS / "etc/systemd/system/configure-dev.service",
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
        ROOTFS / "etc/motd",
        "CS492 kernel module development environment",
        "",
        "Shared files are mounted at /workspace.",
        "Use sudo insmod, sudo rmmod, and sudo dmesg for module work.",
        "Run sudo poweroff to exit cleanly.",
    )
    with (ROOTFS / "home/dev/.bashrc").open("a", encoding="utf-8") as bashrc:
        bashrc.write('\nexport PATH="$HOME/.local/bin:$PATH"\n')
        bashrc.write(f"export PS1='[\\u@{ENVIRONMENT_NAME} \\W]\\$ '\n")


def prepare_boot_files(config: BuildConfig) -> None:
    """Enable guest services and produce the final initramfs."""
    del config
    chroot(
        "systemctl",
        "enable",
        "systemd-networkd.service",
        "systemd-resolved.service",
    )
    chroot("update-initramfs", "-u", "-k", "all")

    # Each writable overlay receives its own machine ID on first boot.
    machine_id = ROOTFS / "etc/machine-id"
    machine_id.write_text("", encoding="utf-8")
    machine_id.chmod(0o644)
    (ROOTFS / "var/lib/dbus/machine-id").unlink(missing_ok=True)


def copy_boot_artifacts(config: BuildConfig) -> None:
    """Copy the newest installed kernel and initramfs out of the rootfs."""
    del config
    kernels = sorted((ROOTFS / "boot").glob("vmlinuz-*"))
    initrds = sorted((ROOTFS / "boot").glob("initrd.img-*"))
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
        str(ROOTFS),
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
    del config
    shutil.rmtree(ROOTFS)
    RAW_DISK.unlink()


# Pipeline order is declared as data so stages can be added, removed, or
# reordered without changing the execution machinery in main().
OPERATIONS = (
    prepare_directories,
    bootstrap_rootfs,
    install_guest_uv,
    write_system_configuration,
    create_dev_account,
    install_guest_python,
    configure_interactive_login,
    prepare_boot_files,
    copy_boot_artifacts,
    create_guest_disk,
    record_base_id,
    clean_build_files,
)


def parse_config(arguments: Sequence[str]) -> BuildConfig:
    """Validate positional build arguments and construct pipeline config."""
    if len(arguments) != 4:
        raise SystemExit(
            "usage: build-guest RELEASE SNAPSHOT DISK_SIZE PYTHON_VERSION"
        )
    return BuildConfig(*arguments)


def main() -> None:
    config = parse_config(sys.argv[1:])
    for operation in OPERATIONS:
        description = operation.__doc__ or operation.__name__
        print(f"==> {description}", flush=True)
        operation(config)


if __name__ == "__main__":
    main()
