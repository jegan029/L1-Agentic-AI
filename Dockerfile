FROM python:3.11-slim AS base

LABEL maintainer="L1 Virtual Engineer Agent"
LABEL description="Automated L1 incident triage and SOP execution agent"

WORKDIR /app

# Install system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt requirements-connectors.txt requirements-saml.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-connectors.txt -r requirements-saml.txt

# Copy application code
COPY src/ src/
COPY data/ data/

ENV PYTHONPATH=/app

# Create non-root user. Nothing under /app is writable by it: state is in the database.
RUN groupadd -r l1agent && useradd -r -g l1agent -d /app l1agent
USER l1agent

# ─── Demo mode target (docker build --target demo) ───────────────────
FROM base AS demo
ENV AGENT_DEMO_MODE=true
ENTRYPOINT ["python", "-m", "src.l1_agent.demo"]

# ─── Production target. Last, so a plain `docker build .` produces it ─
FROM base AS production

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

# The listener binds to loopback unless told otherwise. In a container it must
# accept connections from the pod network; credentials are still mandatory.
ENV AGENT_WEBHOOK_HOST=0.0.0.0

# `serve` by default. Run migrations as a separate job with the migration role:
#   docker run --rm -e DATABASE_MIGRATION_URL=... l1-agent migrate
ENTRYPOINT ["python", "-m", "src.l1_agent.cli"]
CMD ["serve"]
