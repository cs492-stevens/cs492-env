from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from cs492_env import cli
from cs492_env import release_manifest


DIGEST = "sha256:" + "a" * 64


def manifest(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "schema_version": 1,
        "release": "2026.1",
        "image_tag": "2026.1",
        "image_digest": DIGEST,
        "minimum_cli_version": "0.1.0",
        "recommended_cli_version": "0.1.0",
        "notice": None,
    }
    document.update(overrides)
    return document


def payload(**overrides: object) -> bytes:
    return json.dumps(manifest(**overrides)).encode()


def settings(workspace: Path, **overrides: object) -> cli.Settings:
    values: dict[str, object] = {
        "command": "run",
        "workspace": workspace,
        "cpus": None,
        "memory": None,
        "acceleration": None,
        "offline": False,
        "assume_yes": False,
        "image_override": None,
        "release_override": None,
    }
    values.update(overrides)
    return cli.Settings(**values)  # type: ignore[arg-type]


class FakeResponse:
    def __init__(self, content: bytes):
        self.content = content

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, limit: int) -> bytes:
        return self.content[:limit]


class ManifestTests(unittest.TestCase):
    def test_trusted_image_repository_matches_publish_default(self) -> None:
        justfile = (Path(__file__).parents[1] / "justfile").read_text()

        self.assertEqual(cli.IMAGE_REPOSITORY, "rtsang1/cs492-env")
        self.assertIn(
            'image := env_var_or_default("IMAGE", "rtsang1/cs492-env")',
            justfile,
        )

    def test_valid_manifest_selects_digest_and_versioned_volume(self) -> None:
        release = cli.decode_manifest(payload())

        self.assertEqual(release.name, "2026.1")
        self.assertEqual(
            release.image,
            f"{cli.IMAGE_REPOSITORY}@{DIGEST}",
        )
        self.assertEqual(release.volume, "cs492-env-2026.1")

    def test_manifest_rejects_unknown_image_repository_field(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown image"):
            cli.decode_manifest(payload(image="attacker/example"))

    def test_manifest_rejects_bad_digest(self) -> None:
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            cli.decode_manifest(payload(image_digest="sha256:1234"))

    def test_manifest_rejects_unknown_schema(self) -> None:
        with self.assertRaisesRegex(ValueError, "schema_version"):
            cli.decode_manifest(payload(schema_version=2))

    def test_manifest_rejects_boolean_schema(self) -> None:
        with self.assertRaisesRegex(ValueError, "schema_version"):
            cli.decode_manifest(payload(schema_version=True))

    def test_manifest_rejects_duplicate_fields(self) -> None:
        duplicate = payload().decode().replace(
            '"release": "2026.1",',
            '"release": "2026.1", "release": "2026.2",',
        )
        with self.assertRaisesRegex(ValueError, "duplicate field"):
            cli.decode_manifest(duplicate.encode())

    def test_manifest_rejects_terminal_controls_in_notice(self) -> None:
        with self.assertRaisesRegex(ValueError, "control characters"):
            cli.decode_manifest(payload(notice="message\u001b[2J"))

    def test_manifest_rejects_recommended_version_below_minimum(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot precede"):
            cli.decode_manifest(
                payload(
                    minimum_cli_version="2.0.0",
                    recommended_cli_version="1.0.0",
                )
            )

    def test_manifest_size_is_bounded(self) -> None:
        with self.assertRaisesRegex(ValueError, "too large"):
            cli.decode_manifest(b" " * (cli.MANIFEST_LIMIT + 1))

    def test_fetch_caches_valid_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "stable.json"
            with (
                mock.patch.object(cli, "cache_path", return_value=cache),
                mock.patch.object(
                    cli.urllib.request,
                    "urlopen",
                    return_value=FakeResponse(payload()),
                ),
            ):
                release = cli.fetch_release(offline=False)

            self.assertEqual(release.name, "2026.1")
            self.assertEqual(cli.decode_manifest(cache.read_bytes()), release)

    def test_fetch_failure_uses_cached_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "stable.json"
            cache.write_bytes(payload(release="2026.2", image_tag="2026.2"))
            with (
                mock.patch.object(cli, "cache_path", return_value=cache),
                mock.patch.object(
                    cli.urllib.request,
                    "urlopen",
                    side_effect=urllib.error.URLError("offline"),
                ),
                mock.patch("sys.stderr", new_callable=io.StringIO),
            ):
                release = cli.fetch_release(offline=False)

            self.assertEqual(release.name, "2026.2")

    def test_cache_write_failure_does_not_discard_fetched_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch.object(
                    cli, "cache_path", return_value=Path(directory) / "stable.json"
                ),
                mock.patch.object(
                    cli.urllib.request,
                    "urlopen",
                    return_value=FakeResponse(payload()),
                ),
                mock.patch.object(
                    cli, "write_cached_manifest", side_effect=OSError("read-only")
                ),
                mock.patch("sys.stderr", new_callable=io.StringIO),
            ):
                release = cli.fetch_release(offline=False)

        self.assertEqual(release.name, "2026.1")

    def test_offline_without_cache_uses_bundled_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(
                cli, "cache_path", return_value=Path(directory) / "missing.json"
            ):
                release = cli.fetch_release(offline=True)

        self.assertEqual(release, cli.BUNDLED_RELEASE)

    def test_minimum_cli_version_is_enforced(self) -> None:
        release = cli.release_from_document(
            manifest(minimum_cli_version="9.0.0", recommended_cli_version="9.0.0")
        )

        with self.assertRaisesRegex(cli.CliError, "requires launcher"):
            cli.check_cli_compatibility(release)


class ReleaseManifestTests(unittest.TestCase):
    def test_buildkit_digest_generates_versioned_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata = root / "metadata.json"
            output = root / "release" / "stable.2026.1.json"
            metadata.write_text(
                json.dumps({"containerimage.digest": DIGEST}),
                encoding="utf-8",
            )

            release_manifest.write_manifest(metadata, "2026.1", output)

            generated = json.loads(output.read_text(encoding="utf-8"))
            validated = cli.release_from_document(generated)
            self.assertEqual(validated.name, "2026.1")
            self.assertEqual(validated.image_tag, "2026.1")
            self.assertEqual(validated.image_digest, DIGEST)
            self.assertEqual(validated.minimum_cli_version, cli.__version__)
            self.assertEqual(validated.recommended_cli_version, cli.__version__)

    def test_missing_buildkit_digest_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "valid image digest"):
            release_manifest.image_digest({})

    def test_invalid_release_name_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "release has an invalid format"):
            release_manifest.manifest_document("../bad", DIGEST)


class ArgumentTests(unittest.TestCase):
    def test_no_command_defaults_to_run(self) -> None:
        parsed = cli.parse_args([])

        self.assertEqual(parsed.command, "run")

    def test_environment_resource_overrides_are_preserved(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"VM_CPUS": "4", "VM_MEMORY": "4096", "VM_ACCEL": "tcg"},
            clear=False,
        ):
            parsed = cli.parse_args([])

        self.assertEqual(parsed.cpus, 4)
        self.assertEqual(parsed.memory, 4096)
        self.assertEqual(parsed.acceleration, "tcg")

    def test_invalid_environment_resource_is_rejected(self) -> None:
        with mock.patch.dict(os.environ, {"VM_CPUS": "zero"}, clear=False):
            with self.assertRaisesRegex(cli.CliError, "VM_CPUS"):
                cli.parse_args([])

    def test_local_image_override_skips_manifest(self) -> None:
        parsed = cli.parse_args(
            ["--image", "cs492-env:dev", "--release", "dev", "--offline"]
        )
        with mock.patch.object(cli, "fetch_release") as fetch:
            release = cli.resolve_release(parsed)

        fetch.assert_not_called()
        self.assertEqual(cli.image_reference(release, parsed), "cs492-env:dev")


class PlatformTests(unittest.TestCase):
    def test_windows_uses_fixed_unprivileged_identity(self) -> None:
        with mock.patch.object(cli.os, "name", "nt"):
            self.assertEqual(cli.host_identity(), (1000, 1000))

    def test_posix_uses_invoking_identity(self) -> None:
        with (
            mock.patch.object(cli.os, "name", "posix"),
            mock.patch.object(cli.os, "getuid", return_value=501),
            mock.patch.object(cli.os, "getgid", return_value=20),
        ):
            self.assertEqual(cli.host_identity(), (501, 20))

    def test_root_identity_is_rejected(self) -> None:
        with (
            mock.patch.object(cli.os, "name", "posix"),
            mock.patch.object(cli.os, "getuid", return_value=0),
            mock.patch.object(cli.os, "getgid", return_value=0),
        ):
            with self.assertRaisesRegex(cli.CliError, "regular host user"):
                cli.host_identity()

    def test_kvm_detection_requires_amd64_character_device(self) -> None:
        device = os.stat_result((stat.S_IFCHR, 0, 0, 1, 0, 36, 0, 0, 0, 0))
        with (
            mock.patch.object(cli.sys, "platform", "linux"),
            mock.patch.object(cli.platform, "machine", return_value="x86_64"),
            mock.patch.object(cli.os, "stat", return_value=device),
        ):
            self.assertEqual(cli.kvm_group(), 36)

    def test_kvm_group_zero_is_not_forwarded(self) -> None:
        device = os.stat_result((stat.S_IFCHR, 0, 0, 1, 0, 0, 0, 0, 0, 0))
        with (
            mock.patch.object(cli.sys, "platform", "linux"),
            mock.patch.object(cli.platform, "machine", return_value="x86_64"),
            mock.patch.object(cli.os, "stat", return_value=device),
        ):
            self.assertIsNone(cli.kvm_group())

    def test_selinux_rejects_home_as_workspace(self) -> None:
        with (
            mock.patch.object(cli, "selinux_enforcing", return_value=True),
            mock.patch.object(cli.Path, "home", return_value=Path.home()),
        ):
            with self.assertRaisesRegex(cli.CliError, "recursively relabel"):
                cli.workspace_mount(Path.home().resolve())


class DockerCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        self.release = cli.release_from_document(manifest())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_persistent_run_mounts_versioned_volume_and_workspace(self) -> None:
        completed = subprocess.CompletedProcess([], 0)
        parsed = settings(self.workspace, cpus=4, memory=4096)
        with (
            mock.patch.object(cli, "host_identity", return_value=(1000, 1000)),
            mock.patch.object(cli, "selinux_enforcing", return_value=False),
            mock.patch.object(cli, "kvm_group", return_value=None),
            mock.patch.object(cli.sys.stdin, "isatty", return_value=False),
            mock.patch.object(cli.sys.stdout, "isatty", return_value=False),
            mock.patch.object(cli.subprocess, "run", return_value=completed) as run,
        ):
            result = cli.run_vm(parsed, self.release, self.release.image)

        self.assertEqual(result, 0)
        arguments = run.call_args.args[0]
        self.assertIn("type=bind,src=.,dst=/workspace", arguments)
        self.assertIn("type=volume,src=cs492-env-2026.1,dst=/state", arguments)
        self.assertIn("VM_MODE=persistent", arguments)
        self.assertIn("VM_CPUS=4", arguments)
        self.assertIn("VM_MEMORY=4096", arguments)
        self.assertEqual(run.call_args.kwargs["cwd"], self.workspace)

    def test_clean_run_has_no_state_volume(self) -> None:
        completed = subprocess.CompletedProcess([], 0)
        parsed = settings(self.workspace, command="clean")
        with (
            mock.patch.object(cli, "host_identity", return_value=(1000, 1000)),
            mock.patch.object(cli, "selinux_enforcing", return_value=False),
            mock.patch.object(cli, "kvm_group", return_value=None),
            mock.patch.object(cli.sys.stdin, "isatty", return_value=False),
            mock.patch.object(cli.sys.stdout, "isatty", return_value=False),
            mock.patch.object(cli.subprocess, "run", return_value=completed) as run,
        ):
            cli.run_vm(parsed, self.release, self.release.image)

        arguments = run.call_args.args[0]
        self.assertIn("VM_MODE=ephemeral", arguments)
        self.assertFalse(any("type=volume" in argument for argument in arguments))

    def test_kvm_is_forwarded_when_available(self) -> None:
        completed = subprocess.CompletedProcess([], 0)
        parsed = settings(self.workspace)
        with (
            mock.patch.object(cli, "host_identity", return_value=(1000, 1000)),
            mock.patch.object(cli, "selinux_enforcing", return_value=False),
            mock.patch.object(cli, "kvm_group", return_value=36),
            mock.patch.object(cli.sys.stdin, "isatty", return_value=False),
            mock.patch.object(cli.sys.stdout, "isatty", return_value=False),
            mock.patch.object(cli.subprocess, "run", return_value=completed) as run,
        ):
            cli.run_vm(parsed, self.release, self.release.image)

        arguments = run.call_args.args[0]
        self.assertIn("/dev/kvm", arguments)
        self.assertIn("36", arguments)

    def test_reset_requires_confirmation(self) -> None:
        with (
            mock.patch.object(cli, "docker_inspect", side_effect=(False, True)),
            mock.patch("builtins.input", return_value="n"),
            mock.patch.object(cli.subprocess, "run") as run,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            result = cli.reset_volume(self.release, assume_yes=False)

        self.assertEqual(result, 0)
        run.assert_not_called()

    def test_reset_refuses_while_container_exists(self) -> None:
        with mock.patch.object(cli, "docker_inspect", return_value=True):
            with self.assertRaisesRegex(cli.CliError, "stop the VM"):
                cli.reset_volume(self.release, assume_yes=True)

    def test_stop_does_not_resolve_the_release_manifest(self) -> None:
        parsed = settings(self.workspace, command="stop")
        with (
            mock.patch.object(cli, "docker_available"),
            mock.patch.object(cli, "resolve_release") as resolve,
            mock.patch.object(cli, "stop_vm", return_value=0),
        ):
            result = cli.execute(parsed)

        self.assertEqual(result, 0)
        resolve.assert_not_called()


if __name__ == "__main__":
    unittest.main()
