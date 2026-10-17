#!/usr/bin/env bash
set -euo pipefail

uid=1000
gid=1000

# QEMU passes the invoking host user's numeric identity on the kernel command
# line. Defaults keep direct/manual boots usable when those arguments are absent.
read -r -a kernel_args < /proc/cmdline
for arg in "${kernel_args[@]}"; do
    case "${arg}" in
        cs492.uid=*) uid=${arg#cs492.uid=} ;;
        cs492.gid=*) gid=${arg#cs492.gid=} ;;
    esac
done

[[ "${uid}" =~ ^[1-9][0-9]*$ && "${gid}" =~ ^[1-9][0-9]*$ ]]

# Docker bind mounts use numeric ownership. Match the guest account to those
# IDs before opening the serial login so /workspace is writable without root.
current_uid=$(id -u dev)
current_gid=$(id -g dev)
existing_user=$(getent passwd "${uid}" | cut -d: -f1 || true)
if [[ -n "${existing_user}" && "${existing_user}" != dev ]]; then
    printf 'Cannot assign host UID %s: it belongs to guest user %s.\n' \
        "${uid}" "${existing_user}" >&2
    exit 1
fi

if [[ "${current_gid}" != "${gid}" ]]; then
    # Reuse an existing numeric group when possible; macOS commonly supplies a
    # primary GID such as 20 that already belongs to a Debian system group.
    if ! getent group "${gid}" >/dev/null; then
        groupmod --gid "${gid}" dev
    fi
    usermod --gid "${gid}" dev
fi
if [[ "${current_uid}" != "${uid}" ]]; then
    usermod --uid "${uid}" dev
fi
chown -R "${uid}:${gid}" /home/dev
