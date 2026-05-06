FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends

# Install uv.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
 
WORKDIR /app
 
# Copy project files
COPY requirements.txt .
COPY pyproject.toml* uv.lock* ./
 
# Install dependencies using uv
RUN uv sync --frozen --no-cache
COPY ./data /app/data
COPY ./src /app/src
COPY ./templates /app/templates


RUN uv pip install playwright && \
    uv run playwright install --with-deps chromium
 
# Expose the Fastapi port (default: 8000)
EXPOSE 8000
 
# Run the application.
CMD ["/app/.venv/bin/uvicorn", "src.main:app", "--port", "8000", "--host", "0.0.0.0",  "--timeout-keep-alive", "500"]
