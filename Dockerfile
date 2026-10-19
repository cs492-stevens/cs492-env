# syntax=docker/dockerfile:1.7

ARG DEBIAN_RELEASE=trixie
ARG DEBIAN_SNAPSHOT=20261001T000000Z
ARG PYTHON_VERSION=3.14

# Download and extract the fixed AMD64 package set without entering the target
# rootfs. Avoiding a nested chroot keeps this stage compatible with emulation.
FROM --platform=linux/amd64 debian:${DEBIAN_RELEASE}-slim AS guest-bootstrap

ARG DEBIAN_RELEASE
ARG DEBIAN_SNAPSHOT
ARG PYTHON_VERSION

COPY config/apt/99broken-proxy /etc/apt/apt.conf.d/99broken-proxy

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /etc/apt/sources.list.d/* \
    && printf 'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/%s/ %s main\n' \
        "${DEBIAN_SNAPSHOT}" "${DEBIAN_RELEASE}" > /etc/apt/sources.list \
    && printf 'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian-security/%s/ %s-security main\n' \
        "${DEBIAN_SNAPSHOT}" "${DEBIAN_RELEASE}" >> /etc/apt/sources.list \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        curl \
        mmdebstrap \
        python3 \
    && rm -rf /var/lib/apt/lists/*

# The installer can place the AMD64 executables without launching them. Running
# uv itself here is unreliable under BuildKit's QEMU user-mode emulation.
RUN curl --proto '=https' --tlsv1.2 --location --silent --show-error --fail \
        https://astral.sh/uv/install.sh \
        --output /tmp/install-uv.sh \
    && env UV_UNMANAGED_INSTALL=/usr/local/bin sh /tmp/install-uv.sh \
    && rm /tmp/install-uv.sh \
    && test -x /usr/local/bin/uv \
    && test -x /usr/local/bin/uvx

COPY scripts/build-guest.py /usr/local/bin/build-guest
RUN python3 /usr/local/bin/build-guest \
        bootstrap \
        "${DEBIAN_RELEASE}" \
        "${DEBIAN_SNAPSHOT}" \
        unused \
        "${PYTHON_VERSION}"

# Run uv only on the build host architecture. It can download an AMD64 managed
# Python without executing that interpreter; the configured guest receives the
# resulting installation tree below.
FROM --platform=$BUILDPLATFORM debian:${DEBIAN_RELEASE}-slim AS native-tools

ARG DEBIAN_RELEASE
ARG DEBIAN_SNAPSHOT
ARG PYTHON_VERSION

COPY config/apt/99broken-proxy /etc/apt/apt.conf.d/99broken-proxy

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /etc/apt/sources.list.d/* \
    && printf 'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/%s/ %s main\n' \
        "${DEBIAN_SNAPSHOT}" "${DEBIAN_RELEASE}" > /etc/apt/sources.list \
    && printf 'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian-security/%s/ %s-security main\n' \
        "${DEBIAN_SNAPSHOT}" "${DEBIAN_RELEASE}" >> /etc/apt/sources.list \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        curl \
        e2fsprogs \
        qemu-utils \
    && rm -rf /var/lib/apt/lists/*

ENV PATH="/root/.local/bin:${PATH}"
RUN curl --proto '=https' --tlsv1.2 --location --silent --show-error --fail \
        https://astral.sh/uv/install.sh \
        --output /tmp/install-uv.sh \
    && env UV_UNMANAGED_INSTALL=/usr/local/bin sh /tmp/install-uv.sh \
    && rm /tmp/install-uv.sh \
    && uv python install \
        "cpython-${PYTHON_VERSION}-linux-x86_64-gnu" \
        --install-dir /python-assets \
        --no-bin \
    && uv python install "${PYTHON_VERSION}" --default \
    && python3.14 --version \
    && uv cache clean

# Configure packages while the extracted guest is the stage root. Docker makes
# /proc available here, so AMD64 package tools run under emulation without
# requiring CAP_SYS_ADMIN or a nested chroot.
FROM --platform=linux/amd64 scratch AS guest-rootfs

ARG DEBIAN_RELEASE
ARG DEBIAN_SNAPSHOT
ARG PYTHON_VERSION

ENV HOME=/root \
    PATH="/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

COPY --from=guest-bootstrap /build/rootfs/ /
COPY --from=native-tools /python-assets/ /python-assets/
COPY scripts/build-guest.py /usr/local/bin/build-guest
COPY guest/ /guest/

# mmdebstrap extracted every package and retained its snapshot metadata and
# archives. Register all payloads, then let apt order package configuration.
RUN printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d \
    && chmod 0755 /usr/sbin/policy-rc.d \
    && dpkg --unpack --force-depends /var/cache/apt/archives/*.deb \
    && apt-get --yes --no-download --fix-broken install \
    && dpkg --configure --pending \
    && test -z "$(dpkg --audit)" \
    && rm /usr/sbin/policy-rc.d \
    && rm -f /var/cache/apt/archives/*.deb \
    && rm -rf /var/lib/apt/lists/* \
    && install -d -m 0700 -o _apt -g root /var/lib/apt/lists/partial

RUN python3 /usr/local/bin/build-guest \
        configure \
        "${DEBIAN_RELEASE}" \
        "${DEBIAN_SNAPSHOT}" \
        unused \
        "${PYTHON_VERSION}" \
    && rm -rf /root/.cache /root/.local /guest /usr/local/bin/build-guest

# Disk creation does not execute guest binaries, so it can run natively on the
# build host while packaging the fixed AMD64 root filesystem.
FROM native-tools AS guest-builder

ARG DEBIAN_RELEASE
ARG DEBIAN_SNAPSHOT
ARG GUEST_DISK_SIZE=8G
ARG PYTHON_VERSION

COPY --from=guest-rootfs / /build/rootfs/
COPY scripts/build-guest.py /usr/local/bin/build-guest
COPY config/ /config/

RUN python3.14 /usr/local/bin/build-guest \
        package \
        "${DEBIAN_RELEASE}" \
        "${DEBIAN_SNAPSHOT}" \
        "${GUEST_DISK_SIZE}" \
        "${PYTHON_VERSION}"

FROM debian:${DEBIAN_RELEASE}-slim AS runtime

ARG DEBIAN_RELEASE
ARG DEBIAN_SNAPSHOT
ARG IMAGE_VERSION=dev

COPY config/apt/99broken-proxy /etc/apt/apt.conf.d/99broken-proxy

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /etc/apt/sources.list.d/* \
    && printf 'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/%s/ %s main\n' \
        "${DEBIAN_SNAPSHOT}" "${DEBIAN_RELEASE}" > /etc/apt/sources.list \
    && printf 'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian-security/%s/ %s-security main\n' \
        "${DEBIAN_SNAPSHOT}" "${DEBIAN_RELEASE}" >> /etc/apt/sources.list \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        openssh-client \
        qemu-system-x86 \
        qemu-utils \
        util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && install -d /opt/vm /state /workspace

COPY --from=guest-builder /artifacts/ /opt/vm/
COPY scripts/entrypoint.sh /usr/local/bin/cs492-env

RUN chmod 0555 /usr/local/bin/cs492-env \
    && chmod 0444 /opt/vm/*

LABEL org.opencontainers.image.title="CS492 kernel module development environment" \
      org.opencontainers.image.description="Persistent AMD64 Debian VM for external kernel module development" \
      org.opencontainers.image.version="${IMAGE_VERSION}"

ENV IMAGE_VERSION="${IMAGE_VERSION}" \
    LANG=C.UTF-8 \
    VM_CPUS=2 \
    VM_MEMORY=2048

ENTRYPOINT ["/usr/local/bin/cs492-env"]
