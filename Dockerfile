FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates tzdata ffmpeg python3 python3-pip python3-flask gunicorn \
    python3-cryptography python3-requests python3-aiohttp \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --break-system-packages --no-deps -r requirements.txt
RUN mkdir static && python3 -c "import urllib.request as u; u.urlretrieve('https://cdn.jsdelivr.net/npm/hls.js@1.5.17/dist/hls.min.js','static/hls.min.js')"
COPY app.py .
COPY static static
ENV DATA_DIR=/data HLS_DIR=/tmp/hls
EXPOSE 8000
CMD ["gunicorn", "-b", "0.0.0.0:8000", "-w", "1", "--threads", "16", "--timeout", "60", "app:app"]
