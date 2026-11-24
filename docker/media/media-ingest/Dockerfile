# Built from docker/media, so the shared code in common/ is part of the context
FROM python:3.13-slim

RUN pip install --no-cache-dir fastapi==0.115.12 uvicorn==0.34.2

COPY common/*.py common/page.css common/page.js /app/common/
COPY media-ingest/*.py media-ingest/index.html media-ingest/people.html /app/

WORKDIR /app
CMD ["python", "-u", "/app/app.py"]
