"""Launch and manage the CS492 development VM through Docker."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from . import __version__


GIT_URL = "https://github.com/cs492-stevens/cs492-env.git"
MANIFEST_URL = "https://cs492-stevens.github.io/cs492-env/stable.json"

# This is intentionally not configurable by the website manifest. Keeping the
# repository in trusted local code prevents a compromised page from selecting
# an arbitrary image that receives write access to the student's workspace.
IMAGE_REPOSITORY = "cs492-stevens/cs492-env"

CONTAINER_NAME = "cs492-env"
CACHE_FILE_NAME = "stable.json"
MANIFEST_LIMIT = 64 * 1024
MANIFEST_TIMEOUT = 3
VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
RELEASE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
TAG_PATTERN = re.compile(r"^[\w][\w.-]{0,127}$", re.ASCII)
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


class CliError(RuntimeError):
    """An expected launcher error that should be shown without a traceback."""


@dataclass(frozen=True)
class Release:
    """One immutable VM release selected by the course website."""

    name: str
    image_tag: str
    image_digest: str | None
    minimum_cli_version: str
    recommended_cli_version: str
    notice: str | None = None

    @property
    def image(self) -> str:
        if self.image_digest is not None:
            return f"{IMAGE_REPOSITORY}@{self.image_digest}"
        return f"{IMAGE_REPOSITORY}:{self.image_tag}"

    @property
    def volume(self) -> str:
        return f"cs492-env-{self.name}"


# A first run can still start when the course website is unavailable. Production
# releases should update this fallback along with the package version.
BUNDLED_RELEASE = Release(
    name="unpublished",
    image_tag="unpublished",
    image_digest="sha256:" + "0" * 64,
    minimum_cli_version="0.1.0",
    recommended_cli_version="0.1.0",
    notice="The installed launcher does not contain published fallback release metadata.",
)


@dataclass(frozen=True)
class Settings:
    """Validated command-line settings."""

    command: str
    workspace: Path
    cpus: int | None
    memory: int | None
    acceleration: str | None
    offline: bool
    assume_yes: bool
    image_override: str | None
    release_override: str | None


def parse_version(value: str) -> tuple[int, int, int]:
    """Parse the deliberately small semantic-version subset used by the CLI."""
    if not VERSION_PATTERN.fullmatch(value):
        raise ValueError(f"invalid CLI version: {value!r}")
    return tuple(int(part) for part in value.split("."))  # type: ignore[return-value]


def release_from_document(document: Any) -> Release:
    """Validate and convert the stable release manifest."""
    if not isinstance(document, dict):
        raise ValueError("manifest must be a JSON object")
    expected_fields = {
        "schema_version",
        "release",
        "image_tag",
        "image_digest",
        "minimum_cli_version",
        "recommended_cli_version",
        "notice",
    }
    if set(document) != expected_fields:
        missing = expected_fields - set(document)
        unknown = set(document) - expected_fields
        details = []
        if missing:
            details.append(f"missing {', '.join(sorted(missing))}")
        if unknown:
            details.append(f"unknown {', '.join(sorted(unknown))}")
        raise ValueError(f"manifest fields do not match the schema: {'; '.join(details)}")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise ValueError("unsupported manifest schema_version")

    required_strings = (
        "release",
        "image_tag",
        "image_digest",
        "minimum_cli_version",
        "recommended_cli_version",
    )
    values: dict[str, str] = {}
    for field in required_strings:
        value = document.get(field)
        if not isinstance(value, str):
            raise ValueError(f"manifest field {field!r} must be a string")
        values[field] = value

    if not RELEASE_PATTERN.fullmatch(values["release"]):
        raise ValueError("manifest release has an invalid format")
    if not TAG_PATTERN.fullmatch(values["image_tag"]):
        raise ValueError("manifest image_tag has an invalid format")
    if not DIGEST_PATTERN.fullmatch(values["image_digest"]):
        raise ValueError("manifest image_digest must be a SHA-256 digest")
    minimum_version = parse_version(values["minimum_cli_version"])
    recommended_version = parse_version(values["recommended_cli_version"])
    if recommended_version < minimum_version:
        raise ValueError("recommended_cli_version cannot precede minimum_cli_version")

    notice = document.get("notice")
    if notice is not None and (not isinstance(notice, str) or len(notice) > 4096):
        raise ValueError("manifest notice must be null or a short string")
    if notice is not None and any(
        (ord(character) < 32 and character not in "\n\t")
        or 127 <= ord(character) <= 159
        for character in notice
    ):
        raise ValueError("manifest notice contains terminal control characters")

    return Release(
        name=values["release"],
        image_tag=values["image_tag"],
        image_digest=values["image_digest"],
        minimum_cli_version=values["minimum_cli_version"],
        recommended_cli_version=values["recommended_cli_version"],
        notice=notice,
    )


def cache_path() -> Path:
    """Return the native per-user cache path without third-party dependencies."""
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        return root / "cs492-env" / "Cache" / CACHE_FILE_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library/Caches/cs492-env" / CACHE_FILE_NAME
    root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return root / "cs492-env" / CACHE_FILE_NAME


def decode_manifest(payload: bytes) -> Release:
    """Decode a bounded UTF-8 JSON manifest."""
    if len(payload) > MANIFEST_LIMIT:
        raise ValueError("manifest is too large")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        document: dict[str, Any] = {}
        for key, value in pairs:
            if key in document:
                raise ValueError(f"manifest contains duplicate field {key!r}")
            document[key] = value
        return document

    def reject_constant(value: str) -> None:
        raise ValueError(f"manifest contains non-standard JSON value {value}")

    try:
        document = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("manifest is not valid UTF-8 JSON") from error
    return release_from_document(document)


def read_cached_release(path: Path) -> Release | None:
    """Load the last valid manifest, ignoring missing or damaged cache data."""
    try:
        with path.open("rb") as cache:
            return decode_manifest(cache.read(MANIFEST_LIMIT + 1))
    except (OSError, ValueError):
        return None


def write_cached_manifest(path: Path, payload: bytes) -> None:
    """Atomically cache a manifest that has already passed validation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        temporary.write(payload)
        temporary_path = Path(temporary.name)
    try:
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


def fetch_release(*, offline: bool, manifest_url: str = MANIFEST_URL) -> Release:
    """Fetch the stable release, falling back to cache and bundled metadata."""
    path = cache_path()
    if not offline:
        request = urllib.request.Request(
            manifest_url,
            headers={
                "Accept": "application/json",
                "User-Agent": f"cs492-env/{__version__}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=MANIFEST_TIMEOUT) as response:
                payload = response.read(MANIFEST_LIMIT + 1)
            release = decode_manifest(payload)
            try:
                write_cached_manifest(path, payload)
            except OSError as error:
                print(
                    f"warning: could not cache CS492 release information: {error}",
                    file=sys.stderr,
                )
            return release
        except (OSError, ValueError, urllib.error.URLError) as error:
            print(f"warning: could not refresh CS492 release information: {error}", file=sys.stderr)

    cached = read_cached_release(path)
    if cached is not None:
        return cached
    return BUNDLED_RELEASE


def upgrade_command(version: str) -> str:
    """Return the explicit VCS installation command for a tagged CLI release."""
    requirement = f"cs492-env @ git+{GIT_URL}@v{version}"
    executable = "py" if os.name == "nt" else f'"{sys.executable}"'
    return (
        f"{executable} -m pip install --upgrade --force-reinstall "
        f'"{requirement}"'
    )


def check_cli_compatibility(release: Release) -> None:
    """Enforce manifest compatibility and report recommended CLI updates."""
    current = parse_version(__version__)
    minimum = parse_version(release.minimum_cli_version)
    recommended = parse_version(release.recommended_cli_version)
    if current < minimum:
        raise CliError(
            f"CS492 environment {release.name} requires launcher "
            f"{release.minimum_cli_version} or newer.\nRun:\n  "
            f"{upgrade_command(release.recommended_cli_version)}"
        )
    if current < recommended:
        print(
            f"A newer CS492 launcher ({release.recommended_cli_version}) is available.\n"
            f"Update with:\n  {upgrade_command(release.recommended_cli_version)}",
            file=sys.stderr,
        )
    if release.notice:
        print(release.notice, file=sys.stderr)


def positive_integer(value: str) -> int:
    """Argparse converter for positive resource values."""
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def environment_positive_integer(name: str) -> int | None:
    """Read an optional positive integer from the process environment."""
    value = os.environ.get(name)
    if value is None:
        return None
    try:
        return positive_integer(value)
    except (ValueError, argparse.ArgumentTypeError) as error:
        raise CliError(f"{name} must be a positive integer") from error


def parse_args(arguments: Sequence[str] | None = None) -> Settings:
    """Parse the intentionally small student-facing command line."""
    parser = argparse.ArgumentParser(
        prog="cs492-env",
        description="Run and manage the CS492 development VM.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=("run", "clean", "pull", "status", "stop", "reset"),
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="host directory mounted at /workspace (default: current directory)",
    )
    parser.add_argument("--cpus", type=positive_integer, help="guest virtual CPU count")
    parser.add_argument("--memory", type=positive_integer, help="guest RAM in MiB")
    parser.add_argument("--accel", choices=("auto", "kvm", "tcg"), help="QEMU acceleration")
    parser.add_argument("--offline", action="store_true", help="use cached release information")
    parser.add_argument("--yes", action="store_true", help="do not prompt before reset")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--image", help=argparse.SUPPRESS)
    parser.add_argument("--release", help=argparse.SUPPRESS)
    namespace = parser.parse_args(arguments)
    acceleration = namespace.accel or os.environ.get("VM_ACCEL")
    if acceleration not in (None, "auto", "kvm", "tcg"):
        raise CliError("VM_ACCEL must be auto, kvm, or tcg")
    return Settings(
        command=namespace.command,
        workspace=namespace.workspace.expanduser().resolve(),
        cpus=namespace.cpus or environment_positive_integer("VM_CPUS"),
        memory=namespace.memory or environment_positive_integer("VM_MEMORY"),
        acceleration=acceleration,
        offline=namespace.offline,
        assume_yes=namespace.yes,
        image_override=namespace.image,
        release_override=namespace.release,
    )


def resolve_release(settings: Settings, *, enforce_compatibility: bool = True) -> Release:
    """Resolve either a maintainer override or the website's stable release."""
    image = settings.image_override or os.environ.get("CS492_ENV_IMAGE")
    if image:
        release_name = (
            settings.release_override
            or os.environ.get("CS492_ENV_RELEASE")
            or "dev"
        )
        if not RELEASE_PATTERN.fullmatch(release_name):
            raise CliError("the release override has an invalid format")
        return Release(
            name=release_name,
            image_tag=image,
            image_digest=None,
            minimum_cli_version=__version__,
            recommended_cli_version=__version__,
        )

    release = fetch_release(
        offline=settings.offline or os.environ.get("CS492_ENV_OFFLINE") == "1",
        manifest_url=os.environ.get("CS492_ENV_MANIFEST_URL", MANIFEST_URL),
    )
    if enforce_compatibility:
        check_cli_compatibility(release)
    return release


def image_reference(release: Release, settings: Settings) -> str:
    """Return a local override verbatim or construct the trusted remote image."""
    return settings.image_override or os.environ.get("CS492_ENV_IMAGE") or release.image


def docker_available() -> None:
    """Verify that a reachable Docker daemon is running Linux containers."""
    if shutil.which("docker") is None:
        raise CliError("Docker is not installed or is not available on PATH")
    result = subprocess.run(
        ["docker", "info", "--format", "{{.OSType}}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "Docker did not return daemon information"
        raise CliError(f"Docker is not running or is not accessible: {detail}")
    if result.stdout.strip() != "linux":
        raise CliError("Docker must be configured to use Linux containers")


def host_identity() -> tuple[int, int]:
    """Return an unprivileged identity suitable for the outer QEMU process."""
    if os.name != "nt":
        uid = os.getuid()
        gid = os.getgid()
        if uid == 0 or gid == 0:
            raise CliError("run cs492-env as a regular host user, not as root")
        return uid, gid
    # Docker Desktop exposes Windows bind mounts through its Linux VM. A fixed
    # unprivileged identity is sufficient and becomes the guest dev account.
    return 1000, 1000


def selinux_enforcing() -> bool:
    """Return whether a Linux host requires a private bind-mount label."""
    if sys.platform != "linux" or shutil.which("getenforce") is None:
        return False
    result = subprocess.run(
        ["getenforce"],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() == "Enforcing"


def kvm_group() -> int | None:
    """Return the KVM device group when AMD64 Linux acceleration is available."""
    if sys.platform != "linux" or platform.machine().lower() not in ("amd64", "x86_64"):
        return None
    try:
        device = os.stat("/dev/kvm")
    except OSError:
        return None
    if not stat.S_ISCHR(device.st_mode):
        return None
    if device.st_gid == 0:
        return None
    return device.st_gid


def workspace_mount(workspace: Path) -> list[str]:
    """Construct a workspace mount that is safe for the current host."""
    if not workspace.is_dir():
        raise CliError(f"workspace is not a directory: {workspace}")
    if selinux_enforcing():
        dangerous_roots = {
            Path("/"),
            Path("/bin"),
            Path("/boot"),
            Path("/dev"),
            Path("/etc"),
            Path("/home"),
            Path("/lib"),
            Path("/lib64"),
            Path("/opt"),
            Path("/proc"),
            Path("/root"),
            Path("/run"),
            Path("/sbin"),
            Path("/srv"),
            Path("/sys"),
            Path("/tmp"),
            Path("/usr"),
            Path("/var"),
            Path.home().resolve(),
        }
        if workspace in dangerous_roots:
            raise CliError(
                "refusing to recursively relabel a broad SELinux workspace; "
                "select a project directory instead"
            )
        return ["--volume", f"{workspace}:/workspace:Z"]
    # Running Docker with cwd=workspace lets this shell-neutral relative source
    # work on Linux, macOS, PowerShell, and Command Prompt, including spaces.
    return ["--mount", "type=bind,src=.,dst=/workspace"]


def run_vm(settings: Settings, release: Release, image: str) -> int:
    """Start an interactive persistent or ephemeral VM."""
    uid, gid = host_identity()
    persistent = settings.command == "run"
    arguments = ["docker", "run", "--rm"]
    if sys.stdin.isatty():
        arguments.append("--interactive")
    if sys.stdout.isatty():
        arguments.append("--tty")
    arguments.extend(
        (
            "--name",
            CONTAINER_NAME,
            "--env",
            f"VM_MODE={'persistent' if persistent else 'ephemeral'}",
            "--env",
            f"HOST_UID={uid}",
            "--env",
            f"HOST_GID={gid}",
            "--security-opt",
            "no-new-privileges",
        )
    )
    arguments.extend(workspace_mount(settings.workspace))
    if persistent:
        arguments.extend(
            ("--mount", f"type=volume,src={release.volume},dst=/state")
        )
    if settings.cpus is not None:
        arguments.extend(("--env", f"VM_CPUS={settings.cpus}"))
    if settings.memory is not None:
        arguments.extend(("--env", f"VM_MEMORY={settings.memory}"))
    if settings.acceleration is not None:
        arguments.extend(("--env", f"VM_ACCEL={settings.acceleration}"))

    group = kvm_group()
    if group is not None and settings.acceleration != "tcg":
        arguments.extend(("--device", "/dev/kvm", "--group-add", str(group)))
    arguments.append(image)
    return subprocess.run(arguments, cwd=settings.workspace, check=False).returncode


def docker_inspect(kind: str, name: str) -> bool:
    """Return whether a Docker object exists without printing inspect output."""
    result = subprocess.run(
        ["docker", kind, "inspect", name],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def pull_image(image: str) -> int:
    """Pull the selected immutable image."""
    return subprocess.run(["docker", "pull", image], check=False).returncode


def show_status(release: Release, image: str, workspace: Path) -> int:
    """Display the selected release and local Docker state."""
    print(f"Release:   {release.name}")
    print(f"Image:     {image}")
    print(f"Volume:    {release.volume}")
    print(f"Workspace: {workspace}")
    print(f"Image local:  {'yes' if docker_inspect('image', image) else 'no'}")
    print(f"Volume local: {'yes' if docker_inspect('volume', release.volume) else 'no'}")
    print(f"VM container: {'present' if docker_inspect('container', CONTAINER_NAME) else 'absent'}")
    return 0


def stop_vm() -> int:
    """Stop the named VM container if it is running."""
    if not docker_inspect("container", CONTAINER_NAME):
        print("The CS492 VM is not running.")
        return 0
    return subprocess.run(
        ["docker", "stop", CONTAINER_NAME], check=False
    ).returncode


def reset_volume(release: Release, *, assume_yes: bool) -> int:
    """Delete the selected release's persistent overlay volume."""
    if docker_inspect("container", CONTAINER_NAME):
        raise CliError("stop the VM with `cs492-env stop` before resetting it")
    if not docker_inspect("volume", release.volume):
        print(f"Persistent volume {release.volume} does not exist.")
        return 0
    if not assume_yes:
        response = input(
            f"Delete all VM changes in {release.volume}? [y/N] "
        ).strip().lower()
        if response not in ("y", "yes"):
            print("Reset cancelled.")
            return 0
    return subprocess.run(
        ["docker", "volume", "rm", release.volume], check=False
    ).returncode


def execute(settings: Settings) -> int:
    """Resolve release state and execute one CLI command."""
    docker_available()
    if settings.command == "stop":
        return stop_vm()

    release = resolve_release(
        settings,
        enforce_compatibility=settings.command in ("run", "clean", "pull"),
    )
    image = image_reference(release, settings)
    if settings.command in ("run", "clean"):
        return run_vm(settings, release, image)
    if settings.command == "pull":
        return pull_image(image)
    if settings.command == "status":
        return show_status(release, image, settings.workspace)
    if settings.command == "reset":
        return reset_volume(release, assume_yes=settings.assume_yes)
    raise AssertionError(f"unhandled command: {settings.command}")


def main(arguments: Sequence[str] | None = None) -> None:
    """Console-script entry point."""
    try:
        raise SystemExit(execute(parse_args(arguments)))
    except CliError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2) from None
