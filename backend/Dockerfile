FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr poppler-utils && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --shell /usr/sbin/nologin appuser
COPY --chown=appuser:appuser . .
RUN pip install --no-cache-dir .
USER appuser
