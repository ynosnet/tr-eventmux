#!/usr/bin/env bash
set -euo pipefail

APP_SRC="$(cd "$(dirname "$0")" && pwd)"
APP_DST="/opt/tr-eventmux"
FFMPEG_DST="/opt/ffmpeg"
SERVICE_USER="tr-eventmux"
SERVICE_GROUP="tr-eventmux"

if [[ $EUID -ne 0 ]]; then
  echo "Bitte mit sudo/root ausführen."
  exit 1
fi

apt-get update
apt-get install -y ca-certificates curl python3 python3-venv

if ! getent group "$SERVICE_GROUP" >/dev/null 2>&1; then
  groupadd --system "$SERVICE_GROUP"
fi
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  useradd \
    --system \
    --gid "$SERVICE_GROUP" \
    --home-dir "$APP_DST" \
    --shell /usr/sbin/nologin \
    "$SERVICE_USER"
fi

mkdir -p "$APP_DST"
if [[ "$APP_SRC" != "$APP_DST" ]]; then
  for item in "$APP_SRC"/*; do
    name="$(basename "$item")"
    if [[ "$name" == "config.yaml" || "$name" == "data" ]]; then
      continue
    fi
    cp -a "$item" "$APP_DST"/
  done
fi
if [[ ! -f "$APP_DST/config.yaml" ]]; then
  if [[ -f "$APP_SRC/config.yaml" ]]; then
    cp "$APP_SRC/config.yaml" "$APP_DST/config.yaml"
  else
    cp "$APP_DST/config.yaml.example" "$APP_DST/config.yaml"
    sed -i \
      's#xmltv_output_path: "/data/xmltv.xml"#xmltv_output_path: "/opt/tr-eventmux/data/xmltv.xml"#' \
      "$APP_DST/config.yaml"
  fi
fi
mkdir -p "$APP_DST/data"

python3 -m venv "$APP_DST/venv"
"$APP_DST/venv/bin/pip" install --upgrade pip
"$APP_DST/venv/bin/pip" install -r "$APP_DST/requirements.txt"

python3 "$APP_DST/tools/install_ffmpeg_multikey.py" --install-dir "$FFMPEG_DST"

chown -R "$SERVICE_USER:$SERVICE_GROUP" "$APP_DST"
chown -R root:root "$FFMPEG_DST"
chmod 0755 "$FFMPEG_DST/ffmpeg"
if [[ -f "$FFMPEG_DST/ffprobe" ]]; then
  chmod 0755 "$FFMPEG_DST/ffprobe"
fi

cp "$APP_DST/tr-eventmux.service" /etc/systemd/system/tr-eventmux.service
cp "$APP_DST/tr-eventmux-refresh.service" /etc/systemd/system/tr-eventmux-refresh.service
cp "$APP_DST/tr-eventmux-refresh.timer" /etc/systemd/system/tr-eventmux-refresh.timer

systemctl daemon-reload
systemctl enable --now tr-eventmux.service
systemctl enable --now tr-eventmux-refresh.timer

echo
echo "Installiert nach $APP_DST"
echo "ffmpeg installiert nach $FFMPEG_DST/ffmpeg"
echo "Jetzt config.yaml anpassen: sudo nano $APP_DST/config.yaml"
echo "Danach anwenden: sudo systemctl restart tr-eventmux.service"
echo "Status prüfen: systemctl status tr-eventmux.service"
