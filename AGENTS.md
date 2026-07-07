# Project context: TR-EventMux

Read this file and then `README.md` before making changes.

## Purpose

TR-EventMux is a Python/FastAPI service for TVHeadend, installable through
Docker Compose or systemd. Dynamic
Telerising M3U playlists can contain event channels whose real stream URLs
change regularly. TVHeadend should not depend on those changing URLs. Instead,
it consumes stable local slot channels:

```text
http://<HOST>:8787/slot/1.ts
http://<HOST>:8787/slot/2.ts
...
```

The service refreshes one or more source M3Us, detects event entries with date
prefixes such as `[26/06/21 00:00]`, assigns overlapping events to fixed slots
per source, and remuxes the active stream to MPEG-TS with ffmpeg.

## Important files

```text
app.py                 FastAPI application and core logic
config.yaml            local configuration, ignored by Git
config.yaml.example    sanitized documented example configuration
Dockerfile
docker-compose.yml
requirements.txt
README.md
INSTALL.md
QUICKSTART.md
install.sh
tools/install_ffmpeg_multikey.py
tests/test_app.py
```

## Public endpoints

Do not rename or remove these functional endpoints:

```text
GET /                  HTML status page
GET /status.json       complete status, errors and slot assignment
GET /refresh           manual refresh
GET /playlist.m3u      stable mini-M3U for TVHeadend
GET /xmltv.xml         XMLTV EPG
GET /slot/{id}.ts      active MPEG-TS stream for a slot
GET /slot/{source}/{id}.ts  active MPEG-TS stream for a multi-source slot
```

M3U and XMLTV must keep matching IDs such as:

```text
event.slot1
event.slot2
event.slot3
```

## Implementation rules

- The parser supports `[YY/MM/DD HH:MM]` and provider dates without a year such
  as `[MM/DD HH:MM]`; `date_format` can force the interpretation.
- `event_group_filter` accepts a single value, a list, or an empty value for
  all groups.
- Any number of comment lines, especially `#KODIPROP`, may appear between
  `#EXTINF` and the stream URL.
- The first non-comment line after a matching `#EXTINF` is the real stream URL.
- `pipe://bash -c` entries are never executed as shell commands. The parser only
  extracts license and MPD URLs.
- The changing stream URL must not be part of the stable event ID.
- Overlapping events must be assigned to different slots.
- Previous slot assignments should be preserved when possible.
- Persistent `slot_memory` is stored per source in `/data/state.json`.
- If there are more overlapping events than slots, excess events are reported
  under `dropped_events`.
- The service and fixed mini-M3U must work even when no events are detected.
- Failed downloads must not stop the service. The error must appear in logs and
  `status.json`, while the previous successful plan remains available as stale.
- ffmpeg reads the dynamic URL and writes MPEG-TS to `pipe:1`.
- Video and audio are remuxed with `-c copy`.
- ffmpeg warnings and errors should remain visible in container logs.

## Configuration hygiene

Never commit real Telerising URLs, credentials, tokens, or private network
details. Keep `config.yaml.example` sanitized with placeholders only.

Important keys include:

```yaml
source_m3u:
sources:
key:
channel_name_template:
channel_id_template:
host_for_playlist:
public_base_url:
slots:
channel_number_start:
timezone:
event_group_filter:
default_duration_minutes:
refresh_seconds:
tune_early_minutes:
live_countdown_enabled:
allow_upcoming_stream:
keep_stream_while_listed:
xmltv_output_path:
priority_keywords:
exclude_keywords:
ffmpeg_extra_input_args:
ffmpeg_extra_output_args:
```

`host_for_playlist` or `public_base_url` must be reachable from the TVHeadend
container. Inside a container, `127.0.0.1` only means that same container.

## Local checks

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python -m unittest discover -s tests -v
.venv/Scripts/python -m py_compile app.py tests/test_app.py
```

Use `.venv/bin/python` on Linux/macOS.

If Docker is available:

```bash
docker compose config --quiet
docker compose up -d --build
docker compose logs -f tr-eventmux
```

Then check:

```bash
curl http://127.0.0.1:8787/status.json
curl http://127.0.0.1:8787/playlist.m3u
curl http://127.0.0.1:8787/xmltv.xml
```

## Current baseline

Parser, slot planning, stale-error handling and ffmpeg command construction have
tests. Changes to parser or slot logic should include focused tests.
