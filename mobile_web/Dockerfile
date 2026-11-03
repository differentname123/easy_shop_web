# Use --build-arg PYTHON_IMAGE=python:3.13-slim-trixie to test Python 3.13.
ARG PYTHON_IMAGE=python:3.11-slim-bookworm
FROM ${PYTHON_IMAGE} AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /opt/mobile_web

COPY requirements.txt constraints.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 mobileweb \
    && useradd --uid 10001 --gid mobileweb --no-create-home --home-dir /opt/mobile_web --shell /usr/sbin/nologin mobileweb

# Only application-owned files; no parent repository or private config.
COPY --chown=mobileweb:mobileweb auth.py cache.py cache_utils.py config.py main.py run.py ./
COPY --chown=mobileweb:mobileweb config.example.json ./
COPY --chown=mobileweb:mobileweb adapters/ ./adapters/
COPY --chown=mobileweb:mobileweb api/ ./api/
COPY --chown=mobileweb:mobileweb infrastructure/ ./infrastructure/
COPY --chown=mobileweb:mobileweb models/ ./models/
COPY --chown=mobileweb:mobileweb repositories/ ./repositories/
COPY --chown=mobileweb:mobileweb services/ ./services/
COPY --chown=mobileweb:mobileweb scripts/ ./scripts/
COPY --chown=mobileweb:mobileweb static/ ./static/
USER mobileweb:mobileweb

# Optional test image: docker build --target test -t mobile-web:test .
# Tests use offline substitutes; this target does not start an application server.
FROM base AS test
USER root
COPY requirements-dev.txt ./
RUN python -m pip install --no-cache-dir -r requirements-dev.txt
COPY --chown=mobileweb:mobileweb pytest.ini ./
COPY --chown=mobileweb:mobileweb tests/ ./tests/
USER mobileweb:mobileweb
CMD ["python", "-B", "-m", "pytest", "tests", "-q"]

# Default final image: runtime dependencies and application only, no tests/dev tools.
FROM base AS runtime
ENV WEB_HOST=0.0.0.0 \
    WEB_PORT=8000 \
    WEB_LOG_DIR=-
EXPOSE 8000
# Liveness only: this intentionally does not contact Mongo or the PDD API.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).close()"]
CMD ["python", "/opt/mobile_web/run.py"]
