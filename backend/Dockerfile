# Same interpreter as CI (actions/setup-python 3.12) and requires-python: the image runs what is tested.
FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr poppler-utils && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --shell /usr/sbin/nologin appuser
COPY --chown=appuser:appuser . .
# qdrant extra: the vector index behind POST /api/v1/ask (inert unless QDRANT_URL is set).
RUN pip install --no-cache-dir ".[qdrant,vertex]"
USER appuser
