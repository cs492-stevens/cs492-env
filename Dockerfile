# syntax=docker/dockerfile:1.7

ARG DEBIAN_RELEASE=trixie
ARG DEBIAN_SNAPSHOT=20261001T000000Z
ARG PYTHON_VERSION=3.14

# The guest is always AMD64, regardless of the runtime container architecture.
FROM --platform=linux/amd64 debian:${DEBIAN_RELEASE}-slim AS guest-builder

ARG DEBIAN_RELEASE
ARG DEBIAN_SNAPSHOT
ARG GUEST_DISK_SIZE=8G
ARG PYTHON_VERSION

COPY config/apt/99broken-proxy /etc/apt/apt.conf.d/99broken-proxy

RUN rm -f /etc/apt/sources.list.d/* \
    && printf 'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/%s/ %s main\n' \
        "${DEBIAN_SNAPSHOT}" "${DEBIAN_RELEASE}" > /etc/apt/sources.list \
    && printf 'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian-security/%s/ %s-security main\n' \
        "${DEBIAN_SNAPSHOT}" "${DEBIAN_RELEASE}" >> /etc/apt/sources.list \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        e2fsprogs \
        mmdebstrap \
        qemu-utils \
    && rm -rf /var/lib/apt/lists/*

# The current uv release provisions Python 3.14 for the image-construction
# script. This builder stage is discarded after the guest artifacts are made.
ENV PATH="/root/.local/bin:${PATH}"
RUN curl --proto '=https' --tlsv1.2 --location --silent --show-error --fail \
        https://astral.sh/uv/install.sh \
        --output /tmp/install-uv.sh \
    && env UV_UNMANAGED_INSTALL=/usr/local/bin sh /tmp/install-uv.sh \
    && rm /tmp/install-uv.sh \
    && uv python install "${PYTHON_VERSION}" --default \
    && python3.14 --version

COPY scripts/build-guest.py /usr/local/bin/build-guest
COPY guest/ /guest/
COPY config/ /config/

RUN python3.14 /usr/local/bin/build-guest \
        "${DEBIAN_RELEASE}" \
        "${DEBIAN_SNAPSHOT}" \
        "${GUEST_DISK_SIZE}" \
        "${PYTHON_VERSION}"

FROM debian:${DEBIAN_RELEASE}-slim AS runtime

ARG DEBIAN_RELEASE
ARG DEBIAN_SNAPSHOT
ARG IMAGE_VERSION=dev

COPY config/apt/99broken-proxy /etc/apt/apt.conf.d/99broken-proxy

RUN rm -f /etc/apt/sources.list.d/* \
    && printf 'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/%s/ %s main\n' \
        "${DEBIAN_SNAPSHOT}" "${DEBIAN_RELEASE}" > /etc/apt/sources.list \
    && printf 'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian-security/%s/ %s-security main\n' \
        "${DEBIAN_SNAPSHOT}" "${DEBIAN_RELEASE}" >> /etc/apt/sources.list \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
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
    VM_CPUS=2 \
    VM_MEMORY=2048

ENTRYPOINT ["/usr/local/bin/cs492-env"]
