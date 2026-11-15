FROM python:3.13-alpine

# ffmpeg grabs a frame and Pillow lays out the poster for movies filled in by hand
RUN apk add --no-cache ffmpeg font-dejavu
RUN pip install --no-cache-dir fastapi==0.115.12 uvicorn==0.34.2 pillow==12.3.0

COPY tpdb_match.py index.html /app/

CMD ["python", "-u", "/app/tpdb_match.py"]
