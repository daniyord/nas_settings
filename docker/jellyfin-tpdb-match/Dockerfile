FROM python:3.13-alpine

RUN pip install --no-cache-dir fastapi==0.115.12 uvicorn==0.34.2

COPY tpdb_match.py index.html /app/

CMD ["python", "-u", "/app/tpdb_match.py"]
