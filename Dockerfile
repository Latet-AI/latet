# syntax=docker/dockerfile:1.7
# Latet in a container: the daemon, the dashboard, the documentation of its version and the runtime of the optional name model
# (the model itself is downloaded on request: `docker exec latet latet ner install --yes`), and the
# instances of a Latet server (`latet server run`, with the server extra).
# Build: docker build -t latet .    Run: docker compose up -d    (see docs/getting-started/docker and
# docs/getting-started/server)

ARG PYTHON_IMAGE=python:3.13-slim@sha256:bf44cdfcb76cd3b41e879bc058fc37ec5872002ccfde7fcb765e218cde0cd79c
ARG NODE_IMAGE=node:22-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.23@sha256:61d393e44e249f2e4b526b6c7ddcecce245946826e608e11c93ad4f5bba55b21

FROM ${UV_IMAGE} AS uv

# 1. The dashboard, built from its lockfile.
# Built once on the machine that builds (`$BUILDPLATFORM`), never per platform under emulation: the dashboard and the
# documentation are static files, the same for every platform.
FROM --platform=$BUILDPLATFORM ${NODE_IMAGE} AS dashboard
WORKDIR /src/dashboard
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY dashboard/ ./
# `npm run build` type-checks the tests too, and one reads the technical kinds the Python tests share.
COPY tests/data/technical_kinds.json /src/tests/data/technical_kinds.json
RUN npm run build

# 2. The documentation of this version, built from its lockfile, which the daemon serves at /_latet/docs/.
FROM --platform=$BUILDPLATFORM ${NODE_IMAGE} AS docs
WORKDIR /src/docs
COPY docs/package.json docs/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY docs/ ./
RUN ASTRO_TELEMETRY_DISABLED=1 npm run build:bundle

# 3. The wheel, and every dependency pinned by hash from uv.lock.
FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE LICENSING.md ./
COPY src/ src/
COPY --from=dashboard /src/src/latet/web/ src/latet/web/
COPY --from=docs /src/src/latet/docs/ src/latet/docs/
# The documentation's check: every address under /_latet/docs/, nothing fetched from another host, its size; it writes
# the hashes of its inline scripts (.csp.json), without which the daemon does not serve it.
COPY scripts/check_docs_bundle.py scripts/
RUN python scripts/check_docs_bundle.py --root src/latet/docs
RUN uv build --wheel --out-dir /dist \
    && uv export --frozen --no-dev --extra ner --extra ocr --extra server --no-emit-project --format requirements-txt --output-file /dist/requirements.txt

# 4. The runtime: no compiler, no uv, a user without privileges.
FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    LATET_CONTAINER=1 \
    LATET_HOST=0.0.0.0 \
    LATET_KEYRING=off \
    LATET_DATA_DIR=/data \
    LATET_CONFIG=/config/config.yaml
COPY --from=build /dist /tmp/dist
COPY THIRD_PARTY_NOTICES.md /usr/share/doc/latet/THIRD_PARTY_NOTICES.md
RUN pip install --require-hashes --requirement /tmp/dist/requirements.txt \
    && pip install --no-deps /tmp/dist/*.whl \
    && rm -rf /tmp/dist \
    && useradd --uid 10001 --user-group --create-home --home-dir /home/latet latet \
    && mkdir -p /data /config \
    && chown latet:latet /data /config
USER 10001:10001
WORKDIR /home/latet
VOLUME ["/data", "/config"]
EXPOSE 5283
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 CMD ["latet", "status"]
ENTRYPOINT ["latet"]
CMD ["serve"]
