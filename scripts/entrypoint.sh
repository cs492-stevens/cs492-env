#!/usr/bin/env bash
set -euo pipefail

readonly base_disk=/opt/vm/debian-base.qcow2
readonly base_id=/opt/vm/base-id
readonly state_dir=/state
readonly persistent_disk="${state_dir}/debian-overlay.qcow2"
readonly persistent_id="${state_dir}/base-id"

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

printf 'Starting AMD64 Debian guest with %s acceleration.\n' "${accel%%,*}"
printf 'Run `sudo poweroff` in Debian to exit cleanly.\n\n'

qemu_args=(
    -name cs492-env \
    -machine q35 \
    -accel "${accel}" \
    -cpu "${cpu}" \
    -smp "${VM_CPUS:-2}" \
    -m "${VM_MEMORY:-2048}" \
    -kernel /opt/vm/vmlinuz \
    -initrd /opt/vm/initrd.img \
    -append "root=LABEL=cs492-env rootfstype=ext4 rw console=ttyS0,115200n8 panic=1 cs492.uid=${run_uid} cs492.gid=${run_gid}" \
    -drive "file=${disk},format=qcow2,if=virtio,cache=writeback,discard=unmap" \
    -netdev user,id=net0 \
    -device virtio-net-pci,netdev=net0 \
    -object rng-random,filename=/dev/urandom,id=rng0 \
    -device virtio-rng-pci,rng=rng0 \
    -virtfs local,path=/workspace,mount_tag=work,security_model=none,multidevs=remap \
    -display none \
    -monitor none \
    -serial stdio \
    -no-reboot \
    "$@"
)

# Drop container root before QEMU starts serving the writable 9p workspace. The
# supplementary KVM group, when present, is retained only for /dev/kvm access.
exec setpriv \
    --reuid "${run_uid}" \
    --regid "${run_gid}" \
    --keep-groups \
    --no-new-privs \
    qemu-system-x86_64 "${qemu_args[@]}"
