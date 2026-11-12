# Same interpreter as CI (actions/setup-python 3.12) and requires-python: the image runs what is tested.
FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr poppler-utils && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --shell /usr/sbin/nologin appuser
COPY --chown=appuser:appuser . .
# qdrant: the vector index behind POST /api/v1/ask (inert unless QDRANT_URL is set).
# embeddings: fastembed (ONNX) for EMBEDDING_PROVIDER=local, the staging/production default, and RERANK_MODEL.
# Model files are not baked in: provision them once into EMBEDDING_CACHE_DIR (scripts/fetch_models.py,
# docs/DEPLOYMENT.md). pip itself is upgraded first: the base image's pip has published advisories.
RUN pip install --no-cache-dir --upgrade "pip>=26.2" && pip install --no-cache-dir ".[qdrant,vertex,embeddings]"
ENV EMBEDDING_CACHE_DIR=/opt/models
RUN mkdir -p /opt/models && chown appuser:appuser /opt/models
USER appuser
