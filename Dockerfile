FROM python:3.12-alpine

LABEL org.opencontainers.image.title="TR-EventMux" \
      org.opencontainers.image.description="Stable TVHeadend event slots for dynamic Telerising M3U playlists."

ARG TARGETARCH
ARG TARGETVARIANT
ARG FFMPEG_MULTIKEY_REPO=DEvmIb/ffmpeg-multikey
ARG FFMPEG_MULTIKEY_VERSION=latest
ARG FFMPEG_MULTIKEY_ASSET_URL=
ARG STREAMLINK_DRM_REPO=ImAleeexx/streamlink-drm
ARG STREAMLINK_DRM_REF=master

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TVH_EVENTS_CONFIG=/config/config.yaml \
    TVH_EVENTS_STATE=/data/state.json \
    FFMPEG_PATH=/opt/ffmpeg/ffmpeg \
    STREAMLINK_DRM_PATH=/usr/local/bin/streamlink-drm

RUN apk add --no-cache ca-certificates curl git tzdata

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Install the ClearKey-capable Streamlink fork for experimental DASH handling.
# The package exposes the usual "streamlink" command; keep a stable
# "streamlink-drm" alias for configuration and debugging.
RUN python -m pip install --no-cache-dir \
      "git+https://github.com/${STREAMLINK_DRM_REPO}.git@${STREAMLINK_DRM_REF}" \
    && ln -sf /usr/local/bin/streamlink /usr/local/bin/streamlink-drm \
    && streamlink-drm --version \
    && streamlink-drm --help | grep -q -- "-decryption_key"

COPY tools/install_ffmpeg_multikey.py /app/tools/install_ffmpeg_multikey.py

# Lädt beim Build das passende statische ffmpeg-multikey-Archiv für die
# Zielarchitektur und prüft direkt, ob das Binary im Container ausführbar ist.
# Dieser teure, selten geänderte Layer liegt bewusst vor dem Anwendungscode,
# damit Änderungen an app.py keinen erneuten ffmpeg-Download auslösen.
RUN python /app/tools/install_ffmpeg_multikey.py --install-dir /opt/ffmpeg

COPY app.py /app/app.py
COPY config.yaml.example /app/config.yaml.example
COPY assets /app/assets

EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8787/status.json >/dev/null || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8787"]
