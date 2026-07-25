# Vision AI production image — Fly.io / Railway / any container host.
# Layer strategy: requirements first so lib installs cache across code
# changes; app code copies last.

FROM python:3.12-slim

# Runtime deps for httpx / TLS / speedy JSON. Keep minimal to shrink image.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (cache-friendly)
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# Copy the rest of the app (config.yaml lives inside backend/app/)
COPY backend /app/backend
COPY frontend_mvp /app/frontend_mvp

# Persistent volume mounts here (Fly volume, Railway disk, etc.)
ENV DATA_DIR=/data
RUN mkdir -p /data

# Fly injects PORT at runtime; default 8000 for local `docker run`
ENV PORT=8000

EXPOSE 8000

CMD ["sh", "-c", "uvicorn backend.app.api.main:app --host 0.0.0.0 --port ${PORT}"]
