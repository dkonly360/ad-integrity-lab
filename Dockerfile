FROM python:3.12-slim

# Chromium for rendering, Tesseract for the vision channel.
RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium tesseract-ocr tesseract-ocr-eng fonts-dejavu-core \
        libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
        libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 \
    && rm -rf /var/lib/apt/lists/*

ENV CHROME_BIN=/usr/bin/chromium \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
# Files are stored flat in the repo (web upload); rebuild the package layout.
RUN mkdir -p engine templates artifacts data static \
    && for f in engine__*.py; do mv "$f" "engine/${f#engine__}"; done \
    && touch engine/__init__.py \
    && mv templates__index.html templates/index.html

EXPOSE 8080
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080} --log-level warning"]
