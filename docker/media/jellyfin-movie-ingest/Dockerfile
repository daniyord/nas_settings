# Debian rather than Alpine: ffsubsync's speech detection (webrtcvad, cchardet) has no musl wheels
FROM python:3.13-slim

# Debian's ffmpeg 7.1 mixes 5.1 sound down to mono with the dialogue so quiet that ffsubsync rejects good
# matches (Body of Lies); this build's downmix is the one the checks were tried with
COPY --from=mwader/static-ffmpeg:9.0.2 /ffmpeg /ffprobe /usr/local/bin/
# bsdtar unpacks the zip, rar and 7z archives subtitle sites hand out; mkvpropedit marks a dub's track Bulgarian in place
RUN apt-get update && apt-get install -y --no-install-recommends libarchive-tools mkvtoolnix && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir fastapi==0.115.12 uvicorn==0.34.2 ffsubsync==0.5.1

COPY ingest.py sync_check.py bg_subtitles.py index.html /app/

CMD ["python", "-u", "/app/ingest.py"]
