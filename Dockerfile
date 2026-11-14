FROM python:3.13-slim-bookworm AS core
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY pyproject.toml server.py community_sources.py ./
COPY research_v2 ./research_v2
COPY config ./config
RUN pip install --no-cache-dir .
CMD ["python", "server.py", "--http"]

FROM core AS worker
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright XDG_CACHE_HOME=/opt/browser-cache
RUN pip install --no-cache-dir '.[extract,browser]' && python -m playwright install --with-deps chromium
RUN python -m venv /opt/research-ozon \
    && /opt/research-ozon/bin/pip install --no-cache-dir 'camoufox==0.5.9' . \
    && /opt/research-ozon/bin/python -m playwright install-deps firefox \
    && apt-get install -y --no-install-recommends xvfb xauth \
    && /opt/research-ozon/bin/python -c 'from camoufox.fpgen_model import ensure_fpgen_model; ensure_fpgen_model()' \
    && /opt/research-ozon/bin/python -m camoufox fetch official/158.0.1-beta.41 \
    && chmod -R a+rX /opt/playwright /opt/browser-cache
CMD ["python", "-m", "research_v2.worker"]
