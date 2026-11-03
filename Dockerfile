FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 MUSIC_BRIDGE_HOME=/state
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml ./
COPY music_bridge ./music_bridge
RUN pip install --no-cache-dir .
ENTRYPOINT ["music-bridge", "--progress"]
CMD ["jobs", "status"]
