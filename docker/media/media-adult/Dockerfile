# Built from docker/media, so the shared code in common/ is part of the context
FROM python:3.13-slim

# ffmpeg grabs a frame and Pillow lays out the poster of a movie or scene no database knows (poster.py)
COPY --from=mwader/static-ffmpeg:9.0.2 /ffmpeg /ffprobe /usr/local/bin/
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir fastapi==0.115.12 uvicorn==0.34.2 pillow==12.3.0

COPY common/*.py common/page.css common/page.js /app/common/
COPY media-adult/*.py media-adult/index.html media-adult/performers.html /app/

WORKDIR /app
CMD ["python", "-u", "/app/app.py"]
