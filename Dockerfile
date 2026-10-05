FROM mcr.microsoft.com/dotnet/runtime:8.0-bookworm-slim

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        python3 \
        python3-pip \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*

ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONNET_RUNTIME=coreclr \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SDK_PATH=/opt/thorlabs/cct

RUN python3 -m venv "$VIRTUAL_ENV" \
    && pip install --no-cache-dir --upgrade pip setuptools wheel

WORKDIR /opt/ioc
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir '.[hardware]'

COPY docker/entrypoint.sh /usr/local/bin/cct10-entrypoint
RUN chmod 0755 /usr/local/bin/cct10-entrypoint \
    && mkdir -p /opt/thorlabs/cct \
    && useradd --system --uid 10001 --create-home --home-dir /home/ioc ioc \
    && chown -R ioc:ioc /home/ioc /opt/ioc

USER ioc

EXPOSE 5064/tcp 5064/udp

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD test "$(EPICS_CA_ADDR_LIST=127.0.0.1 EPICS_CA_AUTO_ADDR_LIST=NO \
      caproto-get --no-repeater --timeout 3 --terse -n "${PREFIX:-CCT10:}Connected")" = "1"

ENTRYPOINT ["/usr/local/bin/cct10-entrypoint"]
