FROM python:3.11-slim AS base

LABEL maintainer="L1 Virtual Engineer Agent"
LABEL description="Automated L1 incident triage and SOP execution agent"

WORKDIR /app

# Install system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY src/ src/
COPY data/ data/

ENV PYTHONPATH=/app

# Create non-root user
RUN groupadd -r l1agent && useradd -r -g l1agent -d /app l1agent
USER l1agent

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

# Default entry point
ENTRYPOINT ["python", "-m", "src.l1_agent.main"]

# ─── Demo mode target ────────────────────────────────────────────────
FROM base AS demo
ENV AGENT_DEMO_MODE=true
ENTRYPOINT ["python", "-m", "src.l1_agent.demo"]
