# Multi-stage build using uv for ultra-fast, reproducible dependency resolution
FROM ghcr.io/astral-sh/uv:0.5.29 AS uv-bin

FROM python:3.12-slim

# Copy uv binaries from official uv image
COPY --from=uv-bin /uv /uvx /bin/

# Environment settings
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Install project dependencies
COPY pyproject.toml uv.lock* /app/
RUN uv sync --frozen --no-install-project --no-dev

# Copy application source code, configuration, and build scripts
COPY vrmmeter/ /app/vrmmeter/
COPY scripts/ /app/scripts/
COPY README.md /app/

# Install the application into the venv
RUN uv sync --frozen --no-dev

# Build and fetch frontend static assets (Tailwind CSS, Alpine.js, Chart.js)
RUN python scripts/build_assets.py && rm -rf /app/.cache


# Install gosu for lightweight runtime privilege dropping
RUN apt-get update && \
    apt-get install -y --no-install-recommends gosu && \
    rm -rf /var/lib/apt/lists/*

# Pre-create data directory and default vrmmeter user/group
RUN mkdir -p /app/data && \
    groupadd -r vrmmeter -g 1000 && \
    useradd -r -u 1000 -g vrmmeter -d /app -s /sbin/nologin vrmmeter && \
    chown -R vrmmeter:vrmmeter /app

# Copy entrypoint script
COPY scripts/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN sed -i 's/\r$//' /usr/local/bin/docker-entrypoint.sh && \
    chmod +x /usr/local/bin/docker-entrypoint.sh


# Expose port
EXPOSE 8000

# Volume for persistent SQLite DB and telemetry storage
VOLUME ["/app/data"]

# Entrypoint drops root privileges to configured PUID:PGID
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]

# Default command
CMD ["python", "-m", "vrmmeter.main"]


