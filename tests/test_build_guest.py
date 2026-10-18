from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts/build-guest.py"
SPEC = importlib.util.spec_from_file_location("build_guest", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("could not load scripts/build-guest.py")
build_guest = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = build_guest
SPEC.loader.exec_module(build_guest)


class BuildGuestTests(unittest.TestCase):
    def test_each_phase_selects_the_expected_rootfs(self) -> None:
        bootstrap = build_guest.BuildConfig("bootstrap", "trixie", "date", "8G", "3.14")
        configure = build_guest.BuildConfig("configure", "trixie", "date", "8G", "3.14")
        package = build_guest.BuildConfig("package", "trixie", "date", "8G", "3.14")

        self.assertEqual(bootstrap.rootfs, build_guest.BUILD_ROOTFS)
        self.assertEqual(configure.rootfs, Path("/"))
        self.assertEqual(package.rootfs, build_guest.BUILD_ROOTFS)

    def test_bootstrap_extracts_without_chroot_mounts(self) -> None:
        config = build_guest.BuildConfig(
            "bootstrap", "trixie", "20261001T000000Z", "8G", "3.14"
        )
        with mock.patch.object(build_guest, "run") as run:
            build_guest.bootstrap_rootfs(config)

        command = run.call_args.args
        self.assertEqual(command[0], "mmdebstrap")
        self.assertIn("--variant=extract", command)
        self.assertIn("--skip=chroot/mount", command)
        self.assertIn("--skip=cleanup/apt", command)
        self.assertNotIn("chroot", command)

    def test_phase_operations_do_not_include_nested_chroot(self) -> None:
        operation_names = {
            operation.__name__
            for operations in build_guest.PHASE_OPERATIONS.values()
            for operation in operations
        }

        self.assertNotIn("chroot", operation_names)
        self.assertIn("bootstrap_rootfs", operation_names)
        self.assertIn("create_dev_account", operation_names)
        self.assertIn("create_guest_disk", operation_names)

    def test_dockerfile_configures_rootfs_as_scratch_stage_root(self) -> None:
        dockerfile = (Path(__file__).parents[1] / "Dockerfile").read_text()

        self.assertIn("scratch AS guest-rootfs", dockerfile)
        self.assertIn("bootstrap \\", dockerfile)
        self.assertIn("configure \\", dockerfile)
        self.assertIn("package \\", dockerfile)
        self.assertIn("apt-get --yes --no-download --fix-broken install", dockerfile)
        self.assertIn('test -z "$(dpkg --audit)"', dockerfile)


if __name__ == "__main__":
    unittest.main()
