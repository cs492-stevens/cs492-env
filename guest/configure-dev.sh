#!/usr/bin/env bash
set -euo pipefail

uid=1000
gid=1000
ssh_key=

# QEMU passes the invoking host user's numeric identity on the kernel command
# line. Defaults keep direct/manual boots usable when those arguments are absent.
read -r -a kernel_args < /proc/cmdline
for arg in "${kernel_args[@]}"; do
    case "${arg}" in
        cs492.uid=*) uid=${arg#cs492.uid=} ;;
        cs492.gid=*) gid=${arg#cs492.gid=} ;;
        cs492.ssh_key=*) ssh_key=${arg#cs492.ssh_key=} ;;
    esac
done

[[ "${uid}" =~ ^[1-9][0-9]*$ && "${gid}" =~ ^[1-9][0-9]*$ ]]

# Docker bind mounts use numeric ownership. Match the guest account to those
# IDs before opening the SSH login so /workspace is writable without root.
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

# The container creates a fresh key for every run and passes only its public
# half. Replacing authorized_keys also invalidates keys from earlier boots.
rm -f /home/dev/.ssh/authorized_keys
if [[ -n "${ssh_key}" ]]; then
    authorized_key=$(printf '%s' "${ssh_key}" | base64 --decode)
    read -r key_type key_data _ <<< "${authorized_key}"
    [[ "${key_type}" == ssh-ed25519 ]]
    [[ "${key_data}" =~ ^[A-Za-z0-9+/]+={0,3}$ ]]

    install -d -m 0700 -o "${uid}" -g "${gid}" /home/dev/.ssh
    printf '%s %s\n' "${key_type}" "${key_data}" \
        > /home/dev/.ssh/authorized_keys
    chown "${uid}:${gid}" /home/dev/.ssh/authorized_keys
    chmod 0600 /home/dev/.ssh/authorized_keys
fi
