FROM python:3.12-slim

RUN useradd -m -u 1001 -s /bin/bash appuser

RUN apt-get update && apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Copy dependency files first
#COPY requirements.txt .
COPY pyproject.toml* uv.lock* ./

# Install dependencies
RUN uv sync --frozen --no-cache

# Copy application files
COPY ./data /app/data
COPY ./src /app/src
COPY ./templates /app/templates

# Install playwright + chromium
RUN uv pip install playwright && \
    uv run playwright install --with-deps chromium

# Change ownership to appuser
RUN chown -R appuser:appuser /app

# Switch to non-root user
USER appuser

# Expose FastAPI port
EXPOSE 8000

# Start application
CMD ["/app/.venv/bin/uvicorn", "src.main:app", "--port", "8000", "--host", "0.0.0.0", "--timeout-keep-alive", "500", "--workers", "4"]
