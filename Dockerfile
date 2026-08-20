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

# Copy application source code
COPY vrmmeter/ /app/vrmmeter/
COPY README.md /app/

# Install the application into the venv
RUN uv sync --frozen --no-dev

# Create data directory for SQLite database persistence
RUN mkdir -p /app/data

# Expose port
EXPOSE 8000

# Volume for persistent SQLite DB and telemetry storage
VOLUME ["/app/data"]

# Default entrypoint
CMD ["python", "-m", "vrmmeter.main"]
