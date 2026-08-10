FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# procps -> pgrep for the bot healthcheck; wget -> fetch Chart.js at build time.
# tesseract -> the payslip this was written against is a browser print pushed
# through a GDI driver, which converts all of its text to vector outlines: the
# PDF carries no text layer at all, so the only way to read it is to render
# each page and OCR the bitmap. An embedded text layer is still preferred when
# the document has one.
RUN apt-get update && apt-get install -y --no-install-recommends \
        procps wget tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY static ./static

# Bundle Chart.js + Nunito display font into the image (no runtime CDN
# dependency — repo convention). Local dev without these files just falls
# back to the system font stack.
RUN mkdir -p static/vendor \
    && wget -qO static/vendor/chart.umd.min.js \
       https://cdn.jsdelivr.net/npm/chart.js@4.4.6/dist/chart.umd.min.js \
    && wget -qO static/vendor/nunito-700.woff2 \
       https://cdn.jsdelivr.net/fontsource/fonts/nunito@latest/latin-700-normal.woff2 \
    && wget -qO static/vendor/nunito-800.woff2 \
       https://cdn.jsdelivr.net/fontsource/fonts/nunito@latest/latin-800-normal.woff2

RUN mkdir -p /app/data

# Default command; compose overrides per service.
CMD ["python", "-m", "app.bot"]
