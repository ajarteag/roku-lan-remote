# syntax=docker/dockerfile:1
# Roku LAN Remote web server. Stdlib-only Python, so the image is just the
# interpreter plus server.py and the static UI. Nothing runs inside the target
# stage (no RUN), so arm64 and amd64 both build natively without emulation.
FROM python:3.13-alpine

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    PORT=8000

WORKDIR /app
COPY server.py macros.json ./
COPY static/ ./static/
# /data holds config.json + macros.json. Mount a host directory or a named
# volume there; it is created owned by the unprivileged uid the server runs as.
COPY --chown=1000:1000 macros.json /data/macros.json

USER 1000:1000
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD python3 -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/api/health', timeout=2)"

CMD ["python3", "server.py"]
