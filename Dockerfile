FROM python:3.13-slim-bookworm AS core
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY pyproject.toml server.py community_sources.py ./
COPY research_v2 ./research_v2
COPY config ./config
RUN pip install --no-cache-dir .
CMD ["python", "server.py", "--http"]

FROM core AS worker
RUN pip install --no-cache-dir '.[extract,browser]' && python -m playwright install --with-deps chromium
CMD ["python", "-m", "research_v2.worker"]
