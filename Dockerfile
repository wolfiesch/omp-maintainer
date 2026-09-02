# syntax=docker/dockerfile:1.7
# Standalone image: the OMP CLI is installed from npm, never from a checkout.
ARG BUN_VERSION=1.4.0
ARG OMP_PACKAGE_VERSION=18.1.2

FROM oven/bun:${BUN_VERSION}-slim AS web-builder
WORKDIR /build/web
COPY web/package.json ./
RUN bun install
COPY web/ ./
RUN bun run build

FROM oven/bun:${BUN_VERSION}-slim AS bun-runtime

FROM python:3.12-slim-bookworm AS runtime
ARG OMP_PACKAGE_VERSION

ENV BUN_INSTALL=/opt/bun \
    HOME=/srv/agent-home \
    PATH=/opt/bun/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        git \
        openssh-client \
        tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=bun-runtime /usr/local/bin/bun /usr/local/bin/bun
RUN bun install --global "@oh-my-pi/pi-coding-agent@${OMP_PACKAGE_VERSION}"

WORKDIR /app
COPY pyproject.toml README.md LICENSE THIRD_PARTY_NOTICES.md ./
COPY src/ ./src/
COPY --from=web-builder /build/web/dist/ ./src/omp_maintainer/static/
RUN pip install --no-cache-dir . \
    && install --directory --mode=0755 /usr/share/doc/omp-maintainer \
    && install --mode=0644 LICENSE /usr/share/doc/omp-maintainer/LICENSE \
    && install --mode=0644 THIRD_PARTY_NOTICES.md /usr/share/doc/omp-maintainer/THIRD_PARTY_NOTICES.md \
    && install --directory --mode=0700 /srv/agent-home \
    && install --directory --mode=0755 /srv/agent-home-stage/.agent \
    && install --directory --mode=0755 /srv/agent-home-stage/.omp/agent \
    && install --directory --mode=0700 /data

COPY docker/entrypoint.sh /usr/local/bin/omp-maintainer-entrypoint
RUN chmod 0755 /usr/local/bin/omp-maintainer-entrypoint

VOLUME ["/data"]
EXPOSE 8080
EXPOSE 8081

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/omp-maintainer-entrypoint"]
CMD ["python", "-m", "omp_maintainer", "serve"]
