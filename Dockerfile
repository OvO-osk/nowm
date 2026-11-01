FROM python:3.12-slim

# ffmpeg：YouTube/Instagram 音视频合并；chromium：小红书/快手/微博/TikTok 的验证页兜底
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY core.py server.py index.html icon.png favicon.png ./

ENV NOWM_CLOUD=1 PYTHONUNBUFFERED=1 PORT=10000
EXPOSE 10000
CMD ["python", "server.py", "--no-browser"]
