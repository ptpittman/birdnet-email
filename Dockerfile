FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir pillow

WORKDIR /app
COPY digest.py ./
COPY assets ./assets

ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "/app/digest.py"]
