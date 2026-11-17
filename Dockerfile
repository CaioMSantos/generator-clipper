FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/models

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

# Bake the whisper model into the image so the first clip isn't delayed by a download.
ARG WHISPER_MODEL=small
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('${WHISPER_MODEL}', device='cpu', compute_type='int8')"

COPY config ./config

RUN useradd --create-home --uid 1000 clipper && mkdir -p /app/data && chown -R clipper /app/data /models
USER clipper

VOLUME ["/app/data"]
ENTRYPOINT ["clipper"]
CMD ["run"]
