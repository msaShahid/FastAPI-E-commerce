FROM python:3.12-slim

WORKDIR /code

# System dependencies needed to build some Python packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install uv
RUN pip install --no-cache-dir uv

# Copy dependency metadata
COPY pyproject.toml ./

# Copy application and tests
COPY ./app ./app
COPY ./tests ./tests
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic
COPY scripts ./scripts

RUN mkdir -p /code/media


# Install application and development dependencies
RUN uv pip install --system --no-cache . \
    pytest \
    pytest-asyncio \
    httpx \
    ruff

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload", "--proxy-headers"]
