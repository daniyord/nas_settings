# Built from docker/media, as the other media-* services, for the shared page style
FROM python:3.13-slim
RUN pip install --no-cache-dir fastapi==0.115.12 uvicorn==0.34.2
COPY common/page.css media-auth/*.py media-auth/login.html /app/
WORKDIR /app
CMD ["python", "-u", "/app/app.py"]
