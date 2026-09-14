FROM python:3.12.13-slim-bookworm@sha256:6e13e65c55e33adf203d77ee371cf8bf5d81bd4902ef07565721f46bf44917af

# Versions are intentionally literal; refresh alongside the recorded checksums.
RUN apt-get update && apt-get install -y --no-install-recommends smartmontools=7.3-1+b1 ca-certificates=20230311+deb12u1 xz-utils=5.4.1-1 \
    && rm -rf /var/lib/apt/lists/*
ADD https://github.com/just-containers/s6-overlay/releases/download/v3.2.3.2/s6-overlay-noarch.tar.xz /tmp/s6-overlay-noarch.tar.xz
ADD https://github.com/just-containers/s6-overlay/releases/download/v3.2.3.2/s6-overlay-x86_64.tar.xz /tmp/s6-overlay-x86_64.tar.xz
RUN echo '5379750ed30a84bbd2e2dd74847ba6b5bd29cd0b2e3ea2ec58049b57eb2eda12  /tmp/s6-overlay-noarch.tar.xz' | sha256sum -c - \
    && echo 'e6befcc96a437a3831386ecfc51808c5d3e939dc5fe3c02ae9284599e8aa2408  /tmp/s6-overlay-x86_64.tar.xz' | sha256sum -c - \
    && tar -C / -Jxpf /tmp/s6-overlay-noarch.tar.xz \
    && tar -C / -Jxpf /tmp/s6-overlay-x86_64.tar.xz \
    && rm /tmp/s6-overlay-*.tar.xz \
    && rm -rf /var/run && ln -s /run /var/run
COPY container/requirements.txt /opt/requirements.txt
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --require-hashes --only-binary=:all: -r /opt/requirements.txt
# Direct source installation needs no unpinned hatchling/build isolation resolver.
COPY src/ha_nuc9_ec /opt/app/ha_nuc9_ec
COPY container/health_monitor.py /opt/app/health_monitor.py
COPY container/rootfs/ /
ENV PYTHONPATH=/opt/app PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:/command:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    S6_READ_ONLY_ROOT=1 NUC9_BACKEND=linux
HEALTHCHECK --interval=5s --timeout=3s --start-period=15s --retries=2 CMD ["/opt/venv/bin/ha-nuc9-ec", "health"]
ENTRYPOINT ["/init"]
