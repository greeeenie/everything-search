# syntax=docker/dockerfile:1
FROM python:3.13-slim-bookworm AS runtime-deps
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY pyproject.toml /tmp/pyproject.toml
RUN --mount=type=cache,target=/root/.cache/pip \
    python -c 'import tomllib; project = tomllib.load(open("/tmp/pyproject.toml", "rb")); print(*project["build-system"]["requires"], *project["project"]["dependencies"], sep="\n")' > /tmp/research-base.txt \
    && pip install -r /tmp/research-base.txt

FROM runtime-deps AS worker-base
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright XDG_CACHE_HOME=/opt/browser-cache \
    RESEARCH_CAMOUFOX_PYTHON=/opt/research-ozon/bin/python
RUN apt-get update \
    && apt-get install -y --no-install-recommends libatomic1 xvfb xauth \
    && rm -rf /var/lib/apt/lists/*
RUN --mount=type=cache,target=/root/.cache/pip \
    python -c 'import tomllib; extras = tomllib.load(open("/tmp/pyproject.toml", "rb"))["project"]["optional-dependencies"]; print(*extras["extract"], *extras["browser"], sep="\n")' > /tmp/research-worker.txt \
    && pip install -r /tmp/research-worker.txt
RUN python -m playwright install --with-deps chromium
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m venv /opt/research-ozon \
    && /opt/research-ozon/bin/pip install -r /tmp/research-base.txt 'camoufox==0.5.9'
RUN /opt/research-ozon/bin/python -m playwright install-deps firefox
RUN /opt/research-ozon/bin/python -c 'from camoufox.fpgen_model import ensure_fpgen_model; ensure_fpgen_model()' \
    && /opt/research-ozon/bin/python -m camoufox fetch official/158.0.1-beta.41 \
    && chmod -R a+rX /opt/playwright /opt/browser-cache

FROM scratch AS app-source
COPY pyproject.toml server.py community_sources.py /src/
COPY research_v2 /src/research_v2
COPY config /src/config
COPY docs /src/docs

FROM runtime-deps AS core
COPY --from=app-source /src/ ./
RUN pip install --no-deps --no-build-isolation .
CMD ["python", "server.py", "--http"]

FROM worker-base AS worker
COPY --from=app-source /src/ ./
RUN pip install --no-deps --no-build-isolation . \
    && /opt/research-ozon/bin/pip install --no-deps --no-build-isolation .
CMD ["python", "-m", "research_v2.worker"]
