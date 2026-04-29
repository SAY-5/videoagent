# syntax=docker/dockerfile:1.7

# ---- pipeline stage --------------------------------------------------
FROM golang:1.23-alpine AS pipeline-build
WORKDIR /src
COPY pipeline ./
RUN go build -ldflags="-s -w" -o /out/pipelined ./cmd/pipelined

# ---- python api stage ------------------------------------------------
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install ".[openai]"
COPY videoagent ./videoagent
COPY web ./web
COPY --from=pipeline-build /out/pipelined /usr/local/bin/pipelined

RUN useradd -u 10001 -m va && chown -R va /srv
USER va
EXPOSE 8000 8090

CMD ["uvicorn", "videoagent.api:app", "--host", "0.0.0.0", "--port", "8000"]
