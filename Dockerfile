FROM python:3.12-slim

# INSTALL_DEV=true (docker-compose.yml, local development) adds the test and lint tools.
# The default is the PRODUCTION image: no test tools, no tests, no source reloading, non-root.
ARG INSTALL_DEV=false

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /code

# System dependencies needed to build some Python packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install uv
RUN pip install --no-cache-dir "uv==0.13.5"

# Dependencies: EXACTLY the versions in uv.lock (regenerate with `uv lock` after changing pyproject.toml), never "whatever is newest".
COPY pyproject.toml uv.lock ./
RUN uv export --locked --no-emit-project --no-hashes $([ "$INSTALL_DEV" = "true" ] && echo "--extra dev") -o /tmp/requirements.txt \
    && uv pip install --system --no-cache -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# The application itself. Tests are NOT baked into the image: docker-compose.yml mounts ./tests for development.
COPY ./app ./app
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic
COPY scripts ./scripts
RUN uv pip install --system --no-cache --no-deps .

# Run as an unprivileged user. Only the media directory (uploads) needs to be writable.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /code/media \
    && chown -R app:app /code/media
USER app

EXPOSE 8000

# Liveness of the API process (the worker service disables this: it serves no HTTP).
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"

# No --reload here: that is a development convenience and is set by docker-compose.yml only.
# Behind a reverse proxy, set FORWARDED_ALLOW_IPS to the proxy's address (uvicorn trusts X-Forwarded-* only from it).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
