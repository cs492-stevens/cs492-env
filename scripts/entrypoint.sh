#!/usr/bin/env bash
set -euo pipefail

readonly base_disk=/opt/vm/debian-base.qcow2
readonly base_id=/opt/vm/base-id
readonly state_dir=/state
readonly persistent_disk="${state_dir}/debian-overlay.qcow2"
readonly persistent_id="${state_dir}/base-id"
readonly ssh_directory=/tmp/cs492-ssh
readonly ssh_key="${ssh_directory}/id_ed25519"
readonly console_log=/tmp/cs492-console.log
readonly qemu_log=/tmp/cs492-qemu.log
readonly ssh_port=2222

# These IDs are passed by the host launcher. QEMU later drops to them so guest
# root can modify the workspace only with the invoking host user's authority.
run_uid=${HOST_UID:-1000}
run_gid=${HOST_GID:-1000}
if [[ ! "${run_uid}" =~ ^[1-9][0-9]*$ || ! "${run_gid}" =~ ^[1-9][0-9]*$ ]]; then
    printf 'HOST_UID and HOST_GID must be positive integers.\n' >&2
    exit 2
fi

create_overlay() {
    local destination=$1
    qemu-img create -q \
        -f qcow2 \
        -F qcow2 \
        -b "${base_disk}" \
        "${destination}"
}

# The immutable disk in the image is never modified. Persistent and disposable
# runs both write to a small QCOW2 overlay backed by that base disk.
if [[ "${VM_MODE:-persistent}" == ephemeral ]]; then
    disk=/tmp/debian-overlay.qcow2
    create_overlay "${disk}"
else
    if [[ -e "${persistent_disk}" ]]; then
        if [[ ! -f "${persistent_id}" ]] || ! cmp -s "${base_id}" "${persistent_id}"; then
            printf '%s\n' \
                'The persistent VM disk belongs to a different base image.' \
                'Run `just reset` to discard it, or use the matching image version.' >&2
            exit 1
        fi
    else
        create_overlay "${persistent_disk}"
        cp "${base_id}" "${persistent_id}"
    fi
    disk=${persistent_disk}
fi
chown "${run_uid}:${run_gid}" "${disk}"

# KVM is usable only when the outer container and guest are both AMD64 and the
# launcher exposed /dev/kvm. Every other host uses QEMU's TCG emulation.
case "${VM_ACCEL:-auto}" in
    auto)
        if [[ "$(uname -m)" == x86_64 && -c /dev/kvm ]]; then
            accel=kvm
            cpu=host
        else
            accel=tcg,thread=multi
            cpu=max
        fi
        ;;
    kvm)
        accel=kvm
        cpu=host
        ;;
    tcg)
        accel=tcg,thread=multi
        cpu=max
        ;;
    *)
        printf 'Unsupported VM_ACCEL value: %s\n' "${VM_ACCEL}" >&2
        exit 2
        ;;
esac

printf 'Starting AMD64 Debian guest with %s acceleration...\n' "${accel%%,*}"

# Each container run receives a unique credential. Only the public key enters
# the guest, where configure-dev installs it after assigning the final UID.
install -d -m 0700 "${ssh_directory}"
ssh-keygen -q -t ed25519 -N '' -f "${ssh_key}"
public_key=$(base64 < "${ssh_key}.pub" | tr -d '\n')
chown -R "${run_uid}:${run_gid}" "${ssh_directory}"

# OpenSSH requires a passwd entry for its local UID. Docker Desktop IDs and
# Linux host IDs are not otherwise guaranteed to exist in the runtime image.
if ! getent passwd "${run_uid}" >/dev/null; then
    printf 'cs492-runner:x:%s:%s::%s:/usr/sbin/nologin\n' \
        "${run_uid}" "${run_gid}" "${ssh_directory}" >> /etc/passwd
fi

touch "${console_log}" "${qemu_log}"
chmod 0600 "${console_log}" "${qemu_log}"
chown "${run_uid}:${run_gid}" "${console_log}"

qemu_args=(
    -name cs492-env \
    -machine q35 \
    -accel "${accel}" \
    -cpu "${cpu}" \
    -smp "${VM_CPUS:-2}" \
    -m "${VM_MEMORY:-2048}" \
    -kernel /opt/vm/vmlinuz \
    -initrd /opt/vm/initrd.img \
    -append "root=LABEL=cs492-env rootfstype=ext4 rw console=hvc0 quiet loglevel=3 panic=1 systemd.getty_auto=no cs492.uid=${run_uid} cs492.gid=${run_gid} cs492.ssh_key=${public_key}" \
    -drive "file=${disk},format=qcow2,if=virtio,cache=writeback,discard=unmap" \
    -netdev "user,id=net0,hostfwd=tcp:127.0.0.1:${ssh_port}-:22" \
    -device virtio-net-pci,netdev=net0 \
    -chardev "file,id=console0,path=${console_log}" \
    -device virtio-serial-pci \
    -device virtconsole,chardev=console0 \
    -object rng-random,filename=/dev/urandom,id=rng0 \
    -device virtio-rng-pci,rng=rng0 \
    -virtfs local,path=/workspace,mount_tag=work,security_model=none,multidevs=remap \
    -display none \
    -monitor none \
    -serial none \
    -no-reboot \
    "$@"
)

qemu_pid=
ssh_pid=

stop_processes() {
    if [[ -n "${ssh_pid}" ]] && kill -0 "${ssh_pid}" 2>/dev/null; then
        kill "${ssh_pid}" 2>/dev/null || true
        wait "${ssh_pid}" 2>/dev/null || true
    fi
    if [[ -n "${qemu_pid}" ]] && kill -0 "${qemu_pid}" 2>/dev/null; then
        kill "${qemu_pid}" 2>/dev/null || true
        wait "${qemu_pid}" 2>/dev/null || true
    fi
}

handle_signal() {
    stop_processes
    exit 130
}

trap stop_processes EXIT
trap handle_signal HUP INT TERM

# QEMU runs with the invoking user's authority because it serves the writable
# 9p workspace. It alone retains supplementary groups for optional KVM access.
setpriv \
    --reuid "${run_uid}" \
    --regid "${run_gid}" \
    --keep-groups \
    --no-new-privs \
    qemu-system-x86_64 "${qemu_args[@]}" \
    > "${qemu_log}" 2>&1 &
qemu_pid=$!

ssh_options=(
    -i "${ssh_key}"
    -p "${ssh_port}"
    -o BatchMode=yes
    -o ConnectTimeout=1
    -o ConnectionAttempts=1
    -o IdentitiesOnly=yes
    -o StrictHostKeyChecking=no
    -o UserKnownHostsFile=/dev/null
    -o LogLevel=ERROR
)

run_ssh() {
    setpriv \
        --reuid "${run_uid}" \
        --regid "${run_gid}" \
        --clear-groups \
        --no-new-privs \
        env HOME="${ssh_directory}" ssh "$@"
}

ssh_ready=false
for ((attempt = 0; attempt < 300; attempt++)); do
    if ! kill -0 "${qemu_pid}" 2>/dev/null; then
        break
    fi
    if run_ssh "${ssh_options[@]}" dev@127.0.0.1 true >/dev/null 2>&1; then
        ssh_ready=true
        break
    fi
    sleep 1
done

if [[ "${ssh_ready}" != true ]]; then
    printf 'The Debian guest did not become ready for SSH.\n' >&2
    if [[ -s "${qemu_log}" ]]; then
        printf '\nQEMU output:\n' >&2
        cat "${qemu_log}" >&2
    fi
    if [[ -s "${console_log}" ]]; then
        printf '\nGuest console output:\n' >&2
        cat "${console_log}" >&2
    fi
    exit 1
fi

printf 'SSH into Debian guest...\n\n'
set +e
run_ssh -tt "${ssh_options[@]}" -o LogLevel=QUIET dev@127.0.0.1 <&0 &
ssh_pid=$!
wait "${ssh_pid}"
ssh_status=$?
ssh_pid=
set -e

# Logging out of the SSH shell should end the one-command environment just as
# poweroff does. Request a clean shutdown, then fall back to terminating QEMU.
if kill -0 "${qemu_pid}" 2>/dev/null; then
    run_ssh "${ssh_options[@]}" -o LogLevel=QUIET dev@127.0.0.1 \
        sudo poweroff >/dev/null 2>&1 || true
fi
for ((attempt = 0; attempt < 300; attempt++)); do
    if ! kill -0 "${qemu_pid}" 2>/dev/null; then
        break
    fi
    sleep 0.1
done
if kill -0 "${qemu_pid}" 2>/dev/null; then
    printf 'SSH disconnected before the guest powered off; stopping QEMU.\n' >&2
    stop_processes
    exit "${ssh_status}"
fi

set +e
wait "${qemu_pid}"
qemu_status=$?
set -e
qemu_pid=
exit "${qemu_status}"
