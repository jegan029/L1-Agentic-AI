# syntax=docker/dockerfile:1
#
# Images of L1 and L2 AI-Ops. One file, four targets:
#
#   docker build .                    control plane: API, workers, migrations   (the default)
#   docker build --target runner .    the runner: connectors, no database, no listener
#   docker build --target ui .        the dashboard, served by nginx
#   docker build --target demo .      the one-shot demonstration
#
# Base images are pinned by digest and Python packages by hash (requirements.lock), so a
# build today and a build next month install the same bytes. To move a pin, change the
# digest here or re-run the command at the top of the lock file, and review the diff.

ARG PYTHON_IMAGE=python:3.11-slim@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce
ARG NODE_IMAGE=node:20-alpine@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293
ARG NGINX_IMAGE=nginxinc/nginx-unprivileged:stable-alpine@sha256:56b8272e43a0525f67aed68d1e0794a712d10e3e45a563e118e9a77c817e6f86

# ─── Dashboard ────────────────────────────────────────────────────────
FROM ${NODE_IMAGE} AS ui-build
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
# `npm ci` installs exactly what the lock file names and checks each package's integrity hash.
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM ${NGINX_IMAGE} AS ui
LABEL org.opencontainers.image.title="L1 and L2 AI-Ops dashboard"
# The template is rendered into /etc/nginx/conf.d at start. With a read-only root
# filesystem, mount a writable volume there and at /tmp.
COPY deploy/nginx/default.conf.template /etc/nginx/templates/default.conf.template
COPY --from=ui-build /build/dist /usr/share/nginx/html
# Where /api, /auth, /webhook and /runner are sent. Set it to the API's service.
ENV API_UPSTREAM=http://127.0.0.1:8080 \
    UI_STYLE_SRC="" \
    UI_FONT_SRC=""
USER 101
EXPOSE 8080

# ─── Python: everything the three Python targets share ────────────────
FROM ${PYTHON_IMAGE} AS base

LABEL org.opencontainers.image.title="L1 and L2 AI-Ops" \
      org.opencontainers.image.description="Triage of ITSM incidents by executing published SOPs"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app \
    HOME=/tmp

WORKDIR /app

# git is used by the Git knowledge source. curl is not installed: probes use Python.
RUN apt-get update && \
    apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

# Every package, and every package it depends on, at a pinned version with a checked hash.
# setuptools and wheel come with the base image and are only for building packages; nothing
# here imports them at run time, and the copies they bundle are what an image scan flags.
COPY requirements.lock ./
RUN pip install --require-hashes --no-deps -r requirements.lock && \
    pip uninstall --yes setuptools wheel

COPY src/ src/
COPY data/sample_sops/ data/sample_sops/
COPY data/sample_drafts/ data/sample_drafts/

# A fixed numeric user, so that `runAsNonRoot` can be verified by the cluster. Nothing
# under /app is writable by it: state is in the database, and scratch files go to /tmp.
RUN groupadd --gid 10001 l1 && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /tmp l1
USER 10001:10001

# ─── Demo (docker build --target demo) ────────────────────────────────
FROM base AS demo
ENV AGENT_DEMO_MODE=true
ENTRYPOINT ["python", "-m", "src.l1_agent.demo"]

# ─── Runner (docker build --target runner) ────────────────────────────
# The same code, started as the runner. It opens no port and needs no database.
FROM base AS runner
LABEL org.opencontainers.image.title="L1 and L2 AI-Ops runner"
ENTRYPOINT ["python", "-m", "src.l1_agent.cli"]
CMD ["runner", "run"]

# ─── Control plane. Last, so a plain `docker build .` produces it ─────
FROM base AS production

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3).status == 200 else 1)"]

# The listener binds to loopback unless told otherwise. In a container it must
# accept connections from the pod network; credentials are still mandatory.
ENV AGENT_WEBHOOK_HOST=0.0.0.0
EXPOSE 8080

# `serve` by default. Other commands: `worker`, and `migrate` with the migration role:
#   docker run --rm -e DATABASE_MIGRATION_URL=... <image> migrate
ENTRYPOINT ["python", "-m", "src.l1_agent.cli"]
CMD ["serve"]
