#!/usr/bin/env python3
"""Stable TVHeadend event slots backed by a dynamic M3U playlist."""

from __future__ import annotations

import html
import base64
import binascii
import hashlib
import ipaddress
import json
import logging
import os
import re
import shlex
import socket
import subprocess
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, AsyncIterator, Iterator, Optional
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response, StreamingResponse

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("TVH_EVENTS_CONFIG", APP_DIR / "config.yaml"))
STATE_PATH = Path(os.environ.get("TVH_EVENTS_STATE", APP_DIR / "data" / "state.json"))
EVENT_LOGO_DIR = APP_DIR / "assets" / "event-logos"
MPEGTS_JS_PATH = APP_DIR / "assets" / "vendor" / "mpegts" / "mpegts-1.8.0.js"

_LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
LOG = logging.getLogger("tr-eventmux")
logging.basicConfig(
    level=_LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# Set the level explicitly so the in-memory ring buffer keeps capturing INFO
# records even if the root logger level is changed elsewhere (e.g. by pytest).
LOG.setLevel(_LOG_LEVEL)

_app_log_lock = threading.Lock()
_app_log_lines: deque[str] = deque(
    maxlen=max(50, int(os.environ.get("TVH_EVENTS_UI_LOG_LINES", "500")))
)


class RingBufferLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:
            return
        with _app_log_lock:
            _app_log_lines.append(line)


_ui_log_handler = RingBufferLogHandler()
_ui_log_handler.setFormatter(
    logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
)
logging.getLogger().addHandler(_ui_log_handler)

DATE_YMD_RE = re.compile(
    r"\[(?P<year>\d{2})/(?P<month>\d{2})/(?P<day>\d{2})\s+"
    r"(?P<hour>\d{2}):(?P<minute>\d{2})\]\s*(?P<name>.*)"
)
DATE_SHORT_RE = re.compile(
    r"\[(?P<first>\d{1,2})/(?P<second>\d{1,2})\s+"
    r"(?P<hour>\d{2}):(?P<minute>\d{2})\]\s*(?P<name>.*)"
)
LIVE_RE = re.compile(
    r"(?:\[\s*live\s*\]|\(\s*live\s*\))\s*(?P<name>.*)",
    re.IGNORECASE,
)
LIVE_COUNTDOWN_RE = re.compile(
    r"^(?P<name>.*?)\s+Noch\s+"
    r"(?:(?P<hours>\d+)\s*(?:Std\.?|Stunden?))?"
    r"(?:\s*(?P<minutes>\d+)\s*(?:Min\.?|Minuten?))?\.?\s*$",
    re.IGNORECASE,
)
ATTR_RE = re.compile(r"""([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
KODIPROP_DRM_LEGACY_RE = re.compile(
    r"^#KODIPROP:inputstream\.adaptive\.drm_legacy="
    r"(?P<system>[^|]+)\|(?P<url>https?://\S+)\s*$",
    re.IGNORECASE,
)
PIPE_CURL_RE = re.compile(r"\$\(\s*curl\s+(?P<args>.*?)\)", re.IGNORECASE)
PIPE_INPUT_RE = re.compile(
    r"(?:^|\s)-i\s+(?:\"(?P<double>[^\"]+)\"|'(?P<single>[^']+)'|(?P<bare>\S+))",
    re.IGNORECASE,
)
SHELL_VALUE_RE = r"(?:\"(?:\\.|[^\"])*\"|'(?:\\.|[^'])*'|\S+)"
SAFE_FFMPEG_INPUT_OPTIONS: set[str] = {
    "-allowed_extensions",
    "-cookies",
    "-headers",
    "-http_proxy",
    "-protocol_whitelist",
    "-referer",
    "-rw_timeout",
    "-seekable",
    "-timeout",
    "-tls_verify",
    "-user_agent",
}

DEFAULTS: dict[str, Any] = {
    "host_for_playlist": "127.0.0.1",
    "port": 8787,
    "slots": 5,
    "channel_number_start": 901,
    "channel_name_prefix": "Event",
    "group_title": "Events",
    "event_group_filter": "Events",
    "timezone": "Europe/Berlin",
    "default_duration_minutes": 180,
    "epg_max_duration_minutes": 240,
    "lookback_minutes": 30,
    "lookahead_hours": 96,
    "past_events_display_limit": 10,
    "idle_replay_enabled": True,
    "refresh_seconds": 300,
    "tune_early_minutes": 5,
    "live_countdown_enabled": False,
    "allow_upcoming_stream": False,
    "keep_stream_while_listed": True,
    "title_split": "first_dash",
    "date_format": "auto",
    "priority_keywords": [],
    "exclude_keywords": [],
    "ffmpeg": os.environ.get("FFMPEG_PATH", "/usr/bin/ffmpeg"),
    "ffmpeg_base_args": [
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "warning",
    ],
    "ffmpeg_reconnect_args": [
        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_delay_max",
        "5",
    ],
    "ffmpeg_user_agent": "",
    "ffmpeg_extra_input_args": [],
    "ffmpeg_extra_output_args": [],
    "ffmpeg_drm_input_args": [
        "-fflags",
        "+genpts+nobuffer",
        "-avioflags",
        "direct",
    ],
    "ffmpeg_map_args": ["-map", "0:v:0", "-map", "0:a?"],
    "ffmpeg_codec_args": ["-c", "copy"],
    "ffmpeg_drm_output_args": [
        "-copytb",
        "1",
        "-muxpreload",
        "0",
        "-flush_packets",
        "1",
        "-ignore_unknown",
        "-sn",
        "-dn",
    ],
    "ffmpeg_mpegts_flags": "+resend_headers",
    "ffmpeg_drm_mpegts_flags": "+resend_headers+initial_discontinuity",
    "ffmpeg_mpegts_output_args": ["-muxdelay", "0", "-f", "mpegts"],
    "ffmpeg_reconnect": True,
    "license_timeout_seconds": 15,
    "request_timeout_seconds": 25,
    "manifest_probe_enabled": True,
    "manifest_probe_failure_mode": "warn",
    "manifest_probe_attempts": 3,
    "manifest_probe_retry_seconds": 0.75,
    "manifest_probe_timeout_seconds": 8,
    "manifest_probe_bytes": 65536,
    "stream_start_attempts": 4,
    "stream_start_retry_seconds": 2.0,
    "stream_no_data_retries": 3,
    "stream_refresh_on_failure": True,
    "live_logo_max_bytes": 2 * 1024 * 1024,
    # Logos often live on the same (possibly LAN) host as the operator-trusted
    # source, so the SSRF block for the logo proxy is opt-in and off by default.
    "live_logo_block_private_hosts": False,
    "verify_tls": True,
    "source_user_agent": "",
    "xmltv_language": "de",
    "xmltv_output_path": os.environ.get(
        "TVH_EVENTS_XMLTV", str(STATE_PATH.with_name("xmltv.xml"))
    ),
}

_state_lock = threading.RLock()
_refresh_lock = threading.Lock()
_stop_refresh = threading.Event()
_refresh_thread: Optional[threading.Thread] = None
_memory_state: dict[str, Any] = {}
_config_cache_lock = threading.Lock()
# (stat signature, parsed config) of the last successfully parsed config file.
_config_cache: Optional[tuple[tuple[int, int], dict[str, Any]]] = None


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    global _refresh_thread
    refresh_state()
    _stop_refresh.clear()
    _refresh_thread = threading.Thread(
        target=background_refresher, name="m3u-refresher", daemon=True
    )
    _refresh_thread.start()
    try:
        yield
    finally:
        _stop_refresh.set()
        if _refresh_thread and _refresh_thread.is_alive():
            _refresh_thread.join(timeout=3)


app = FastAPI(
    title="TR-EventMux",
    description="Stable TVHeadend event slots for dynamic Telerising M3U playlists.",
    version="1.0.0",
    lifespan=lifespan,
)


@dataclass
class SourceEvent:
    stable_key: str
    start: str
    stop: str
    title: str
    desc: str
    url: str
    group: str = ""
    logo: str = ""
    raw_name: str = ""
    source_tvg_id: str = ""
    priority_score: int = 0
    source_key: str = "default"
    source_name: str = "Events"
    stream_type: str = "url"
    license_url: str = ""
    ffmpeg_input_args: Optional[list[str]] = None


@dataclass
class LiveChannel:
    id: str
    name: str
    url: str
    group: str = ""
    logo: str = ""
    source_tvg_id: str = ""
    channel_number: str = ""
    source_key: str = "default"
    source_name: str = "Live TV"
    stream_type: str = "url"
    license_url: str = ""
    ffmpeg_input_args: Optional[list[str]] = None


def now_iso(zone: ZoneInfo) -> str:
    return datetime.now(zone).isoformat()


def _parse_config() -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeError(f"Konfiguration kann nicht gelesen werden: {exc}") from exc
    if not isinstance(loaded, dict):
        raise RuntimeError("Die Konfiguration muss ein YAML-Objekt sein")

    cfg = {**DEFAULTS, **loaded}
    if not cfg.get("sources") and not str(cfg.get("source_m3u", "")).strip():
        raise RuntimeError("source_m3u oder sources fehlt in der Konfiguration")
    if int(cfg["default_duration_minutes"]) < 1:
        raise RuntimeError("default_duration_minutes muss mindestens 1 sein")
    if int(cfg["epg_max_duration_minutes"]) < 1:
        raise RuntimeError("epg_max_duration_minutes muss mindestens 1 sein")
    try:
        ZoneInfo(str(cfg["timezone"]))
    except ZoneInfoNotFoundError as exc:
        raise RuntimeError(f"Unbekannte Zeitzone: {cfg['timezone']}") from exc
    normalized_sources(cfg)
    return cfg


def load_config() -> dict[str, Any]:
    """Return the parsed config, reparsing the YAML only when the file changes.

    The config is read on nearly every request and twice per second by each
    active stream monitor, so a mtime/size based cache keeps that path off the
    disk and YAML parser. A shallow copy is handed out so callers can never
    mutate the shared cached dict.
    """
    global _config_cache
    try:
        stat = CONFIG_PATH.stat()
    except OSError as exc:
        raise RuntimeError(f"Konfigurationsdatei fehlt: {CONFIG_PATH}") from exc
    signature = (stat.st_mtime_ns, stat.st_size)

    with _config_cache_lock:
        cached = _config_cache
    if cached is not None and cached[0] == signature:
        return dict(cached[1])

    cfg = _parse_config()
    with _config_cache_lock:
        _config_cache = (signature, cfg)
    return dict(cfg)


def zone_for(cfg: dict[str, Any]) -> ZoneInfo:
    return ZoneInfo(str(cfg["timezone"]))


def safe_source_key(value: Any) -> str:
    key = re.sub(r"[^a-z0-9_-]+", "-", str(value).strip().casefold()).strip("-")
    if not key:
        raise RuntimeError("Jede Quelle benötigt einen gültigen key")
    return key


def normalized_sources(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Return source-specific configs while preserving the legacy single source."""
    configured = cfg.get("sources")
    if not configured:
        source = dict(cfg)
        source.update(
            {
                "key": "default",
                "source_name": str(cfg.get("source_name", "Events")),
                "channel_name_template": str(
                    cfg.get("channel_name_template")
                    or f"{cfg.get('channel_name_prefix', 'Event')} {{slot}}"
                ),
                "channel_id_template": str(
                    cfg.get("channel_id_template", "event.slot{slot}")
                ),
                "_legacy_route": True,
            }
        )
        source["slots"] = int(source["slots"])
        if source["slots"] < 1:
            raise RuntimeError("slots muss mindestens 1 sein")
        try:
            for slot_id in range(1, source["slots"] + 1):
                source["channel_name_template"].format(
                    source=source["key"], slot=slot_id
                )
                source["channel_id_template"].format(
                    source=source["key"], slot=slot_id
                )
        except (KeyError, ValueError) as exc:
            raise RuntimeError(f"Ungültiges Kanal-Template: {exc}") from exc
        return [source]
    if not isinstance(configured, list) or not configured:
        raise RuntimeError("sources muss eine nicht-leere YAML-Liste sein")

    result = []
    seen_keys: set[str] = set()
    seen_numbers: set[int] = set()
    seen_channel_ids: set[str] = set()
    for index, item in enumerate(configured, start=1):
        if not isinstance(item, dict):
            raise RuntimeError(f"sources[{index}] muss ein YAML-Objekt sein")
        source = {**cfg, **item}
        source.pop("sources", None)
        key = safe_source_key(item.get("key") or f"source{index}")
        if key in seen_keys:
            raise RuntimeError(f"Doppelter Quellen-key: {key}")
        seen_keys.add(key)
        source["key"] = key
        source["source_name"] = str(item.get("name") or key)
        source["source_m3u"] = str(
            item.get("source_m3u") or item.get("url") or ""
        ).strip()
        if not source["source_m3u"]:
            raise RuntimeError(f"Quelle {key}: source_m3u oder url fehlt")
        source["slots"] = int(item.get("slots", cfg.get("slots", 5)))
        if source["slots"] < 1:
            raise RuntimeError(f"Quelle {key}: slots muss mindestens 1 sein")
        source["channel_number_start"] = int(
            item.get("channel_number_start", cfg.get("channel_number_start", 901))
        )
        source["channel_name_template"] = str(
            item.get("channel_name_template")
            or item.get("channel_name_prefix", key) + "_event{slot}"
        )
        source["channel_id_template"] = str(
            item.get("channel_id_template") or f"{key}.event{{slot}}"
        )
        source["group_title"] = str(
            item.get("group_title", cfg.get("group_title", "Events"))
        )
        source["_legacy_route"] = False

        for slot_id in range(1, source["slots"] + 1):
            number = source["channel_number_start"] + slot_id - 1
            try:
                source["channel_name_template"].format(source=key, slot=slot_id)
                channel_id = source["channel_id_template"].format(
                    source=key, slot=slot_id
                )
            except (KeyError, ValueError) as exc:
                raise RuntimeError(
                    f"Quelle {key}: ungültiges Kanal-Template: {exc}"
                ) from exc
            if number in seen_numbers:
                raise RuntimeError(f"Doppelte Kanalnummer in sources: {number}")
            if channel_id in seen_channel_ids:
                raise RuntimeError(f"Doppelte channel_id in sources: {channel_id}")
            seen_numbers.add(number)
            seen_channel_ids.add(channel_id)
        result.append(source)
    return result


def parse_extinf(line: str) -> tuple[dict[str, str], str]:
    attrs = {
        match.group(1).lower(): match.group(2) if match.group(2) is not None else match.group(3)
        for match in ATTR_RE.finditer(line)
    }
    name = line.split(",", 1)[1].strip() if "," in line else ""
    return attrs, name


def split_event_title(remainder: str, split_mode: str) -> tuple[str, str]:
    if split_mode == "none" or "-" not in remainder:
        return remainder.strip(), ""
    if split_mode == "last_dash":
        title, desc = remainder.rsplit("-", 1)
    else:
        title, desc = remainder.split("-", 1)
    return title.strip(), desc.strip()


def parse_event_name(
    raw_name: str,
    zone: ZoneInfo,
    split_mode: str,
    date_format: str = "auto",
    reference_time: Optional[datetime] = None,
    live_countdown_enabled: bool = False,
) -> Optional[tuple[datetime, str, str]]:
    date_format = date_format.strip().casefold()
    live_match = LIVE_RE.search(raw_name)
    if live_match:
        reference = reference_time or datetime.now(zone)
        remainder = live_match["name"].strip()
        countdown_match = (
            LIVE_COUNTDOWN_RE.fullmatch(remainder) if live_countdown_enabled else None
        )
        if countdown_match and (
            countdown_match["hours"] is not None
            or countdown_match["minutes"] is not None
        ):
            # The provider countdown only has minute precision. Normalizing the
            # fetch time prevents seconds from making the EPG start drift.
            start = reference.replace(second=0, microsecond=0) + timedelta(
                hours=int(countdown_match["hours"] or 0),
                minutes=int(countdown_match["minutes"] or 0),
            )
            remainder = countdown_match["name"].strip()
        else:
            start = reference - timedelta(minutes=1)
        title, desc = split_event_title(remainder, split_mode)
        return start, title, desc

    match = DATE_YMD_RE.search(raw_name)
    start: Optional[datetime] = None
    remainder = ""
    if match and date_format in {"auto", "ymd", "yy/mm/dd"}:
        try:
            start = datetime(
                2000 + int(match["year"]),
                int(match["month"]),
                int(match["day"]),
                int(match["hour"]),
                int(match["minute"]),
                tzinfo=zone,
            )
        except ValueError:
            LOG.warning("Ungültiges Event-Datum ignoriert: %s", raw_name)
            return None
        remainder = match["name"].strip()
    else:
        match = DATE_SHORT_RE.search(raw_name)
        if not match or date_format not in {
            "auto",
            "md",
            "mm/dd",
            "dm",
            "dd/mm",
        }:
            return None
        first = int(match["first"])
        second = int(match["second"])
        if date_format in {"dm", "dd/mm"}:
            month, day = second, first
        elif date_format in {"md", "mm/dd"}:
            month, day = first, second
        elif first > 12 and second <= 12:
            month, day = second, first
        else:
            # Provider format used by DAZN-like playlists: [MM/DD HH:MM].
            month, day = first, second

        reference = reference_time or datetime.now(zone)
        candidates = []
        for year in (reference.year - 1, reference.year, reference.year + 1):
            try:
                candidates.append(
                    datetime(
                        year,
                        month,
                        day,
                        int(match["hour"]),
                        int(match["minute"]),
                        tzinfo=zone,
                    )
                )
            except ValueError:
                continue
        if not candidates:
            LOG.warning("Ungültiges Event-Datum ignoriert: %s", raw_name)
            return None
        # A year-less provider date is interpreted as the occurrence nearest
        # to now, which also handles December/January rollover.
        start = min(candidates, key=lambda value: abs(value - reference))
        remainder = match["name"].strip()

    if start is None:
        return None
    title, desc = split_event_title(remainder, split_mode)
    return start, title, desc


def keyword_score(title: str, desc: str, cfg: dict[str, Any]) -> int:
    haystack = f"{title} {desc}".casefold()
    return sum(
        100
        for keyword in cfg.get("priority_keywords", []) or []
        if str(keyword).casefold() in haystack
    )


def is_excluded(title: str, desc: str, cfg: dict[str, Any]) -> bool:
    haystack = f"{title} {desc}".casefold()
    return any(
        str(keyword).casefold() in haystack
        for keyword in cfg.get("exclude_keywords", []) or []
    )


def stable_event_key(start: datetime, attrs: dict[str, str], raw_name: str) -> str:
    # Deliberately excludes the dynamic URL so an event keeps its slot after URL rotation.
    tvg_id = attrs.get("tvg-id", "").strip()
    if tvg_id:
        return f"tvg:{tvg_id}"
    canonical_name = LIVE_RE.sub(r"\g<name>", raw_name).strip()
    return f"{start.isoformat()}|{canonical_name}"


def shell_value(value: str) -> str:
    parts = shlex.split(value, posix=True)
    return parts[0] if parts else ""


def ffmpeg_input_args_from_pipe_script(script: str, input_start: int) -> list[str]:
    """Keep safe provider input options that may be required for MPD/segments."""
    prefix = script[:input_start]
    args: list[str] = []
    option_pattern = "|".join(re.escape(option) for option in SAFE_FFMPEG_INPUT_OPTIONS)
    pattern = re.compile(
        rf"(?:^|\s)(?P<option>{option_pattern})\s+(?P<value>{SHELL_VALUE_RE})",
        re.IGNORECASE,
    )
    for match in pattern.finditer(prefix):
        option = match["option"]
        value = shell_value(match["value"])
        if value:
            args.extend([option, value])
    return args


def parse_pipe_stream(line: str) -> tuple[str, str, list[str]]:
    """Extract media/license URLs from provider pipe commands without executing them."""
    if not line.casefold().startswith("pipe://"):
        return line, "", []
    try:
        command = shlex.split(line[len("pipe://") :], posix=True)
    except ValueError as exc:
        raise ValueError(f"Ungültige pipe://-Zeile: {exc}") from exc
    if len(command) < 3 or command[0] not in {"bash", "sh"} or command[1] != "-c":
        raise ValueError("Nur pipe://bash -c bzw. pipe://sh -c wird unterstützt")
    script = command[2]

    input_matches = list(PIPE_INPUT_RE.finditer(script))
    if not input_matches:
        raise ValueError("Im pipe://-Kommando fehlt die ffmpeg-Eingabe (-i)")
    input_match = input_matches[-1]
    media_url = next(
        value for value in input_match.group("double", "single", "bare") if value
    )
    if not media_url.casefold().startswith(("http://", "https://")):
        raise ValueError("Die ffmpeg-Eingabe im pipe://-Kommando ist keine HTTP(S)-URL")
    ffmpeg_input_args = ffmpeg_input_args_from_pipe_script(script, input_match.start())

    curl_matches = list(PIPE_CURL_RE.finditer(script))
    if not curl_matches:
        raise ValueError("Im pipe://-Kommando fehlt der Provider-Abruf per curl")
    curl_match = curl_matches[-1]
    try:
        curl_args = shlex.split(curl_match["args"], posix=True)
    except ValueError as exc:
        raise ValueError(f"Ungültiger curl-Aufruf im pipe://-Kommando: {exc}") from exc
    license_urls = [
        value
        for value in curl_args
        if value.casefold().startswith(("http://", "https://"))
    ]
    if not license_urls:
        raise ValueError("Im pipe://-Kommando fehlt eine HTTP(S)-Provider-URL")
    return media_url, license_urls[-1], ffmpeg_input_args


def kodiprop_license_url(line: str) -> str:
    match = KODIPROP_DRM_LEGACY_RE.match(line)
    if not match:
        return ""
    if match["system"].strip().casefold() != "org.w3.clearkey":
        return ""
    return match["url"]


def live_channel_id(
    source_key: str, attrs: dict[str, str], name: str, group: str
) -> str:
    tvg_id = attrs.get("tvg-id", "").strip()
    identity = f"tvg:{tvg_id}" if tvg_id else f"name:{group}|{name}"
    return hashlib.sha256(f"{source_key}|{identity}".encode("utf-8")).hexdigest()[:16]


def parse_live_channels(text: str, cfg: dict[str, Any]) -> list[LiveChannel]:
    """Parse non-event, non-VOD M3U entries for the separate live-TV view."""
    zone = zone_for(cfg)
    split_mode = str(cfg.get("title_split", "first_dash"))
    date_format = str(cfg.get("date_format", "auto"))
    source_key = str(cfg.get("key", "default"))
    source_name = str(cfg.get("source_name", source_key))
    pending: Optional[dict[str, Any]] = None
    channels_by_id: dict[str, LiveChannel] = {}

    for raw_line in text.lstrip("\ufeff").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.upper().startswith("#EXTINF"):
            attrs, name = parse_extinf(line)
            is_event = parse_event_name(
                name,
                zone,
                split_mode,
                date_format=date_format,
                live_countdown_enabled=bool(cfg.get("live_countdown_enabled", False)),
            )
            is_vod = str(attrs.get("tr-vod", "")).strip().casefold() in {
                "1",
                "true",
                "yes",
            }
            if not name or is_event or is_vod:
                pending = None
                continue
            pending = {
                "attrs": attrs,
                "name": name,
                "group": attrs.get("group-title", ""),
                "license_url": "",
            }
            continue

        if line.startswith("#"):
            if pending is not None:
                license_url = kodiprop_license_url(line)
                if license_url:
                    pending["license_url"] = license_url
            continue

        if pending is None:
            continue

        attrs = pending["attrs"]
        try:
            media_url, pipe_license_url, ffmpeg_input_args = parse_pipe_stream(line)
        except ValueError as exc:
            LOG.warning("Live-TV-Kanal %r ignoriert: %s", pending["name"], exc)
            pending = None
            continue
        license_url = pipe_license_url or pending.get("license_url", "")
        channel_id = live_channel_id(
            source_key, attrs, pending["name"], pending["group"]
        )
        channels_by_id[channel_id] = LiveChannel(
            id=channel_id,
            name=pending["name"],
            url=media_url,
            group=pending["group"],
            logo=attrs.get("tvg-logo", ""),
            source_tvg_id=attrs.get("tvg-id", ""),
            channel_number=attrs.get("tvg-chno", ""),
            source_key=source_key,
            source_name=source_name,
            stream_type="pipe_drm" if license_url else "url",
            license_url=license_url,
            ffmpeg_input_args=ffmpeg_input_args,
        )
        pending = None

    channels = sorted(
        channels_by_id.values(),
        key=lambda channel: (
            channel.source_name.casefold(),
            channel.group.casefold(),
            channel.name.casefold(),
        ),
    )
    LOG.info("Dynamische M3U geparst: %d Live-TV-Kanal/Kanäle erkannt", len(channels))
    return channels


def parse_m3u(
    text: str,
    cfg: dict[str, Any],
    reference_time: Optional[datetime] = None,
) -> list[SourceEvent]:
    """Parse each EXTINF block and use its first following non-comment line as URL."""
    zone = zone_for(cfg)
    configured_groups = cfg.get("event_group_filter")
    if isinstance(configured_groups, (list, tuple, set)):
        required_groups = {
            str(group).strip().casefold()
            for group in configured_groups
            if str(group).strip()
        }
    else:
        group = str(configured_groups or "").strip().casefold()
        required_groups = {group} if group else set()
    split_mode = str(cfg.get("title_split", "first_dash"))
    date_format = str(cfg.get("date_format", "auto"))
    pending: Optional[dict[str, Any]] = None
    events_by_key: dict[str, SourceEvent] = {}

    for raw_line in text.lstrip("\ufeff").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.upper().startswith("#EXTINF"):
            attrs, raw_name = parse_extinf(line)
            parsed = parse_event_name(
                raw_name,
                zone,
                split_mode,
                date_format=date_format,
                reference_time=reference_time,
                live_countdown_enabled=bool(cfg.get("live_countdown_enabled", False)),
            )
            group = attrs.get("group-title", "")
            if (
                not parsed
                or (
                    required_groups
                    and group.strip().casefold() not in required_groups
                )
            ):
                pending = None
                continue
            start, title, desc = parsed
            if not title or is_excluded(title, desc, cfg):
                pending = None
                continue
            pending = {
                "attrs": attrs,
                "raw_name": raw_name,
                "start": start,
                "title": title,
                "desc": desc,
                "group": group,
                "license_url": "",
            }
            continue

        if line.startswith("#"):
            # Includes any number of #KODIPROP and other metadata lines.
            if pending is not None:
                license_url = kodiprop_license_url(line)
                if license_url:
                    pending["license_url"] = license_url
            continue

        if pending is None:
            continue

        attrs = pending["attrs"]
        start = pending["start"]
        source_key = str(cfg.get("key", "default"))
        key = f"{source_key}|{stable_event_key(start, attrs, pending['raw_name'])}"
        try:
            media_url, pipe_license_url, ffmpeg_input_args = parse_pipe_stream(line)
        except ValueError as exc:
            LOG.warning("Event %r ignoriert: %s", pending["raw_name"], exc)
            pending = None
            continue
        license_url = pipe_license_url or pending.get("license_url", "")
        event = SourceEvent(
            stable_key=key,
            start=start.isoformat(),
            stop="",
            title=pending["title"],
            desc=pending["desc"],
            url=media_url,
            group=pending["group"],
            logo=attrs.get("tvg-logo", ""),
            raw_name=pending["raw_name"],
            source_tvg_id=attrs.get("tvg-id", ""),
            priority_score=keyword_score(pending["title"], pending["desc"], cfg),
            source_key=source_key,
            source_name=str(cfg.get("source_name", source_key)),
            stream_type="pipe_drm" if license_url else "url",
            license_url=license_url,
            ffmpeg_input_args=ffmpeg_input_args,
        )
        events_by_key[key] = event
        pending = None

    events = sorted(
        events_by_key.values(),
        key=lambda event: (
            datetime.fromisoformat(event.start),
            -event.priority_score,
            event.title.casefold(),
        ),
    )
    LOG.info("Dynamische M3U geparst: %d Event(s) erkannt", len(events))
    return events


def has_live_countdown(raw_name: Any) -> bool:
    live_match = LIVE_RE.search(str(raw_name or ""))
    return bool(
        live_match
        and LIVE_COUNTDOWN_RE.fullmatch(live_match["name"].strip())
    )


def preserve_live_countdown_starts(
    events: list[SourceEvent],
    cfg: dict[str, Any],
    previous_state: Optional[dict[str, Any]],
) -> list[SourceEvent]:
    """Keep the known start when a listed event changes to a provider countdown."""
    if not bool(cfg.get("live_countdown_enabled", False)) or not previous_state:
        return events

    previous_by_key: dict[str, dict[str, Any]] = {}
    for previous in previous_state.get("detected_events", []):
        stable_key = str(previous.get("stable_key", "")).strip()
        if stable_key:
            previous_by_key[stable_key] = previous
        source_tvg_id = str(previous.get("source_tvg_id", "")).strip()
        if source_tvg_id:
            previous_by_key[
                f"{previous.get('source_key', cfg.get('key', 'default'))}"
                f"|tvg:{source_tvg_id}"
            ] = previous

    preserved = 0
    for event in events:
        if not has_live_countdown(event.raw_name):
            continue
        previous = next(
            (
                previous_by_key[key]
                for key in event_slot_keys(event)
                if key in previous_by_key
                and str(previous_by_key[key].get("title", "")).casefold()
                == event.title.casefold()
            ),
            None,
        )
        if previous is None:
            continue
        try:
            datetime.fromisoformat(str(previous["start"]))
        except (KeyError, TypeError, ValueError):
            continue
        event.start = str(previous["start"])
        preserved += 1

    if preserved:
        LOG.info(
            "Countdown-Start für Quelle %s bei %d Event(s) beibehalten",
            cfg.get("key", "default"),
            preserved,
        )
    return events


def download_source_m3u(cfg: dict[str, Any]) -> str:
    headers = {}
    if cfg.get("source_user_agent"):
        headers["User-Agent"] = str(cfg["source_user_agent"])
    response = requests.get(
        str(cfg["source_m3u"]),
        timeout=float(cfg["request_timeout_seconds"]),
        headers=headers,
        verify=bool(cfg["verify_tls"]),
    )
    response.raise_for_status()
    if not response.encoding or response.encoding.lower() == "iso-8859-1":
        response.encoding = response.apparent_encoding or "utf-8"
    return response.text


def with_stops(events: list[SourceEvent], cfg: dict[str, Any]) -> list[SourceEvent]:
    duration = timedelta(minutes=int(cfg["default_duration_minutes"]))
    result: list[SourceEvent] = []
    for event in events:
        copy = SourceEvent(**asdict(event))
        copy.stop = (datetime.fromisoformat(copy.start) + duration).isoformat()
        result.append(copy)
    return result


def slot_shells(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    count = int(cfg["slots"])
    first_number = int(cfg["channel_number_start"])
    source_key = str(cfg.get("key", "default"))
    source_name = str(cfg.get("source_name", source_key))
    name_template = str(
        cfg.get("channel_name_template")
        or f"{cfg.get('channel_name_prefix', 'Event')} {{slot}}"
    )
    id_template = str(cfg.get("channel_id_template", "event.slot{slot}"))
    legacy_route = bool(cfg.get("_legacy_route", source_key == "default"))
    slots = []
    for slot_id in range(1, count + 1):
        channel_number = first_number + slot_id - 1
        logo_file = EVENT_LOGO_DIR / f"event-{channel_number}.png"
        slot = {
            "id": slot_id,
            "source_key": source_key,
            "source_name": source_name,
            "channel_id": id_template.format(source=source_key, slot=slot_id),
            "name": name_template.format(source=source_key, slot=slot_id),
            "number": channel_number,
            "group_title": str(cfg.get("group_title", "Events")),
            "stream_path": (
                f"/slot/{slot_id}.ts"
                if legacy_route
                else f"/slot/{source_key}/{slot_id}.ts"
            ),
            "events": [],
        }
        if logo_file.is_file():
            slot["logo_path"] = (
                f"/slot/{slot_id}/logo"
                if legacy_route
                else f"/slot/{source_key}/{slot_id}/logo"
            )
        slots.append(slot)
    return slots


def previous_slot_map(state: dict[str, Any]) -> dict[str, int]:
    result: dict[str, int] = {}
    for key, slot_id in (state.get("slot_memory") or {}).items():
        try:
            result[str(key)] = int(slot_id)
        except (TypeError, ValueError):
            continue
    for slot in state.get("slots", []):
        slot_id = int(slot.get("id", 0))
        for event in slot.get("events", []):
            if event.get("stable_key"):
                result[str(event["stable_key"])] = slot_id
            source_tvg_id = str(event.get("source_tvg_id", "")).strip()
            source_key = str(event.get("source_key") or slot.get("source_key", "default"))
            if source_tvg_id:
                result[f"{source_key}|tvg:{source_tvg_id}"] = slot_id
    return result


def event_slot_keys(event: SourceEvent) -> list[str]:
    keys = [event.stable_key] if event.stable_key else []
    source_tvg_id = str(event.source_tvg_id or "").strip()
    if source_tvg_id:
        keys.append(f"{event.source_key}|tvg:{source_tvg_id}")
    return list(dict.fromkeys(keys))


def stored_event_slot_keys(event: dict[str, Any]) -> list[str]:
    keys = []
    stable_key = str(event.get("stable_key", "")).strip()
    if stable_key:
        keys.append(stable_key)
    source_tvg_id = str(event.get("source_tvg_id", "")).strip()
    if source_tvg_id:
        keys.append(
            f"{event.get('source_key', 'default')}|tvg:{source_tvg_id}"
        )
    return list(dict.fromkeys(keys))


def slot_memory_from_slots(
    slots: list[dict[str, Any]], previous: Optional[dict[str, Any]] = None
) -> dict[str, int]:
    """Keep assignments only for events still present in the successful plan."""
    memory: dict[str, int] = {}
    for slot in slots:
        slot_id = int(slot.get("id", 0))
        if slot_id < 1:
            continue
        for event in slot.get("events", []):
            stable_key = str(event.get("stable_key", "")).strip()
            if stable_key:
                memory[stable_key] = slot_id
            source_tvg_id = str(event.get("source_tvg_id", "")).strip()
            source_key = str(event.get("source_key") or slot.get("source_key", "default"))
            if source_tvg_id:
                memory[f"{source_key}|tvg:{source_tvg_id}"] = slot_id
    return memory


def intervals_overlap(left: SourceEvent, right: SourceEvent) -> bool:
    return (
        datetime.fromisoformat(left.start) < datetime.fromisoformat(right.stop)
        and datetime.fromisoformat(right.start) < datetime.fromisoformat(left.stop)
    )


def event_sort_key(event: SourceEvent) -> tuple[Any, ...]:
    return (
        datetime.fromisoformat(event.start),
        -event.priority_score,
        event.title.casefold(),
    )


def extend_running_events_while_listed(
    events: list[SourceEvent],
    cfg: dict[str, Any],
    previous_state: Optional[dict[str, Any]],
    now: datetime,
) -> list[SourceEvent]:
    """Renew a short playback lease for assigned events still in the source M3U."""
    if not bool(cfg.get("keep_stream_while_listed", True)) or not previous_state:
        return events

    lease = timedelta(
        seconds=max(300, int(cfg.get("refresh_seconds", 120)) * 2)
    )
    listed_by_key = {
        key: event
        for event in events
        for key in event_slot_keys(event)
    }
    renewed_keys: set[str] = set()

    for slot in previous_state.get("slots", []):
        started = []
        for previous_event in slot.get("events", []):
            try:
                start = datetime.fromisoformat(previous_event["start"])
                stop = datetime.fromisoformat(previous_event["stop"])
            except (KeyError, TypeError, ValueError):
                continue
            if start <= now and stop >= now - lease:
                started.append((start, previous_event))
        if not started:
            continue

        _, previous_event = max(started, key=lambda item: item[0])
        previous_keys = [
            str(previous_event.get("stable_key", "")).strip(),
            (
                f"{previous_event.get('source_key', cfg.get('key', 'default'))}"
                f"|tvg:{previous_event.get('source_tvg_id')}"
                if previous_event.get("source_tvg_id")
                else ""
            ),
        ]
        listed = next(
            (listed_by_key[key] for key in previous_keys if key in listed_by_key),
            None,
        )
        if listed is None or listed.stable_key in renewed_keys:
            continue
        renewed_keys.add(listed.stable_key)
        nominal_stop = datetime.fromisoformat(listed.stop)
        renewed_stop = now + lease
        if renewed_stop > nominal_stop:
            listed.stop = renewed_stop.isoformat()

    return events


def build_schedule(
    source_events: list[SourceEvent],
    cfg: dict[str, Any],
    previous_state: Optional[dict[str, Any]] = None,
) -> tuple[list[dict[str, Any]], list[SourceEvent], list[SourceEvent]]:
    zone = zone_for(cfg)
    now = datetime.now(zone)
    min_time = now - timedelta(minutes=int(cfg["lookback_minutes"]))
    max_start = now + timedelta(hours=int(cfg["lookahead_hours"]))
    planned_events = extend_running_events_while_listed(
        with_stops(source_events, cfg), cfg, previous_state, now
    )
    candidates = [
        event
        for event in planned_events
        if datetime.fromisoformat(event.stop) >= min_time
        and datetime.fromisoformat(event.start) <= max_start
    ]

    slots = slot_shells(cfg)
    old_slots = previous_slot_map(previous_state or {})
    dropped: list[SourceEvent] = []

    for event in sorted(candidates, key=event_sort_key):
        preferred_id = next(
            (old_slots[key] for key in event_slot_keys(event) if key in old_slots),
            None,
        )
        candidate_ids: list[int] = []
        if preferred_id and 1 <= preferred_id <= len(slots):
            candidate_ids.append(preferred_id)
        candidate_ids.extend(
            slot["id"] for slot in slots if slot["id"] not in candidate_ids
        )

        assigned = False
        for slot_id in candidate_ids:
            slot = slots[slot_id - 1]
            slot_events = [SourceEvent(**item) for item in slot["events"]]
            if any(intervals_overlap(event, existing) for existing in slot_events):
                continue
            slot["events"].append(asdict(event))
            assigned = True
            break
        if not assigned:
            dropped.append(event)
            LOG.warning(
                "Kein freier Slot für Event %r ab %s", event.title, event.start
            )

    for slot in slots:
        slot["events"].sort(key=lambda item: item["start"])
    return slots, candidates, dropped


def read_state_file() -> dict[str, Any]:
    if not STATE_PATH.is_file():
        return {}
    try:
        value = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        LOG.error("Persistenter Status kann nicht gelesen werden: %s", exc)
        return {}


def write_state_file(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_PATH.with_name(f"{STATE_PATH.name}.tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(STATE_PATH)


def previous_source_state(
    previous: dict[str, Any], source_key: str, legacy: bool = False
) -> dict[str, Any]:
    for source in previous.get("sources", []):
        if source.get("key") == source_key:
            return source
    return previous if legacy and not previous.get("sources") else {}


def empty_source_state(
    source_cfg: dict[str, Any], previous: Optional[dict[str, Any]] = None
) -> dict[str, Any]:
    previous = previous or {}
    configured_slots = slot_shells(source_cfg)
    previous_slots = {
        int(slot.get("id", 0)): slot for slot in previous.get("slots", [])
    }
    for slot in configured_slots:
        old_slot = previous_slots.get(slot["id"])
        if old_slot:
            slot["events"] = old_slot.get("events", [])
    return {
        "key": source_cfg["key"],
        "name": source_cfg["source_name"],
        "source_m3u": source_cfg["source_m3u"],
        "updated_at": previous.get("updated_at"),
        "last_attempt_at": previous.get("last_attempt_at"),
        "detected_event_count": int(previous.get("detected_event_count", 0)),
        "detected_events": previous.get("detected_events", []),
        "live_channel_count": int(previous.get("live_channel_count", 0)),
        "live_channels": previous.get("live_channels", []),
        "scheduled_event_count": int(previous.get("scheduled_event_count", 0)),
        "dropped_events": previous.get("dropped_events", []),
        "slot_memory": previous.get("slot_memory", {}),
        "slots": configured_slots,
        "error": previous.get("error"),
        "stale": bool(previous.get("stale", False)),
    }


def aggregate_source_states(
    cfg: dict[str, Any],
    source_states: list[dict[str, Any]],
    last_attempt_at: Optional[str],
) -> dict[str, Any]:
    errors = [
        {"source_key": source["key"], "source_name": source["name"], **source["error"]}
        for source in source_states
        if source.get("error")
    ]
    successful_updates = [
        source["updated_at"] for source in source_states if source.get("updated_at")
    ]
    return {
        "schema_version": 2,
        "updated_at": max(successful_updates) if successful_updates else None,
        "last_attempt_at": last_attempt_at,
        "source_m3u": [source["source_m3u"] for source in source_states],
        "source_count": len(source_states),
        "sources": source_states,
        "detected_event_count": sum(
            int(source.get("detected_event_count", 0)) for source in source_states
        ),
        "detected_events": [
            event
            for source in source_states
            for event in source.get("detected_events", [])
        ],
        "live_channel_count": sum(
            int(source.get("live_channel_count", 0)) for source in source_states
        ),
        "live_channels": [
            channel
            for source in source_states
            for channel in source.get("live_channels", [])
        ],
        "scheduled_event_count": sum(
            int(source.get("scheduled_event_count", 0)) for source in source_states
        ),
        "dropped_events": [
            event
            for source in source_states
            for event in source.get("dropped_events", [])
        ],
        "slots": [
            slot for source in source_states for slot in source.get("slots", [])
        ],
        "errors": errors,
        "error": (
            {
                "type": "SourceError",
                "message": f"{len(errors)} von {len(source_states)} Quelle(n) konnten nicht geladen werden",
                "at": last_attempt_at,
            }
            if errors
            else None
        ),
        "stale": any(bool(source.get("stale")) for source in source_states),
    }


def base_state(cfg: dict[str, Any], previous: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    previous = previous or {}
    source_configs = normalized_sources(cfg)
    source_states = [
        empty_source_state(
            source_cfg,
            previous_source_state(
                previous,
                source_cfg["key"],
                legacy=bool(source_cfg.get("_legacy_route")),
            ),
        )
        for source_cfg in source_configs
    ]
    state = aggregate_source_states(
        cfg, source_states, previous.get("last_attempt_at")
    )
    # Preserve the exact top-level timestamps/error from a current schema state.
    if previous.get("schema_version") == 2:
        state["updated_at"] = previous.get("updated_at")
        state["error"] = previous.get("error")
        state["errors"] = previous.get("errors", state["errors"])
        state["stale"] = bool(previous.get("stale", state["stale"]))
    return state


def store_state(state: dict[str, Any]) -> None:
    global _memory_state
    with _state_lock:
        _memory_state = json.loads(json.dumps(state))
        try:
            write_state_file(state)
        except OSError:
            LOG.exception(
                "Status konnte nicht nach %s geschrieben werden; In-Memory-Status bleibt aktiv",
                STATE_PATH,
            )


def current_raw_state() -> dict[str, Any]:
    with _state_lock:
        if _memory_state:
            return json.loads(json.dumps(_memory_state))
        return read_state_file()


def refresh_state() -> dict[str, Any]:
    with _refresh_lock:
        cfg = load_config()
        zone = zone_for(cfg)
        previous = current_raw_state()
        attempted_at = now_iso(zone)
        source_states = []
        for source_cfg in normalized_sources(cfg):
            prior = previous_source_state(
                previous,
                source_cfg["key"],
                legacy=bool(source_cfg.get("_legacy_route")),
            )
            try:
                source_text = download_source_m3u(source_cfg)
                source_events = parse_m3u(source_text, source_cfg)
                live_channels = parse_live_channels(source_text, source_cfg)
                source_events = preserve_live_countdown_starts(
                    source_events, source_cfg, prior
                )
                slots, candidates, dropped = build_schedule(
                    source_events, source_cfg, prior
                )
                source_state = {
                    "key": source_cfg["key"],
                    "name": source_cfg["source_name"],
                    "source_m3u": source_cfg["source_m3u"],
                    "updated_at": now_iso(zone),
                    "last_attempt_at": attempted_at,
                    "detected_event_count": len(source_events),
                    "detected_events": [
                        asdict(event)
                        for event in with_stops(source_events, source_cfg)
                    ],
                    "live_channel_count": len(live_channels),
                    "live_channels": [asdict(channel) for channel in live_channels],
                    "scheduled_event_count": len(candidates) - len(dropped),
                    "dropped_events": [asdict(event) for event in dropped],
                    "slot_memory": slot_memory_from_slots(slots, prior),
                    "slots": slots,
                    "error": None,
                    "stale": False,
                }
                LOG.info(
                    "Quelle %s aktualisiert: %d Events erkannt, %d eingeplant, "
                    "%d verworfen, %d Live-TV-Kanäle",
                    source_cfg["key"],
                    len(source_events),
                    len(candidates) - len(dropped),
                    len(dropped),
                    len(live_channels),
                )
            except Exception as exc:
                LOG.exception(
                    "Aktualisierung der Quelle %s fehlgeschlagen",
                    source_cfg["key"],
                )
                source_state = empty_source_state(source_cfg, prior)
                source_state["last_attempt_at"] = attempted_at
                source_state["error"] = {
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "at": attempted_at,
                }
                source_state["stale"] = bool(source_state.get("updated_at"))
            source_states.append(source_state)

        state = aggregate_source_states(cfg, source_states, attempted_at)
        store_state(state)
        write_xmltv_file(state, cfg)
        return state


def get_state() -> dict[str, Any]:
    cfg = load_config()
    state = current_raw_state()
    if state:
        return base_state(cfg, state)
    # Return a useful empty service state even before the first successful fetch.
    return base_state(cfg)


def active_and_next(
    events: list[dict[str, Any]], now: datetime
) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    current = None
    next_event = None
    for event in events:
        start = datetime.fromisoformat(event["start"])
        stop = datetime.fromisoformat(event["stop"])
        if start <= now < stop:
            current = event
            break
        if start > now and next_event is None:
            next_event = event
    if current:
        for event in events:
            if datetime.fromisoformat(event["start"]) >= datetime.fromisoformat(current["stop"]):
                next_event = event
                break
    return current, next_event


def public_status(state: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(state))
    now = datetime.now(zone_for(cfg))
    for slot in result.get("slots", []):
        _, next_event = active_and_next(slot.get("events", []), now)
        source_key = str(slot.get("source_key", "default"))
        current, playback_mode, _ = playback_for_slot(
            int(slot["id"]), result, cfg, source_key, now=now
        )
        slot["current_event"] = current
        slot["playback_mode"] = playback_mode
        slot["next_event"] = next_event
    result["now"] = now.isoformat()
    result["ok"] = result.get("error") is None
    return result


def mini_epg_entries(
    status: dict[str, Any], cfg: dict[str, Any]
) -> list[dict[str, Any]]:
    """Build a chronological, display-friendly list of current/upcoming events."""
    zone = zone_for(cfg)
    now = datetime.now(zone)
    slot_by_key: dict[str, dict[str, Any]] = {}
    for slot in status.get("slots", []):
        for event in slot.get("events", []):
            if event.get("stable_key"):
                slot_by_key[str(event["stable_key"])] = slot

    dropped_keys = {
        str(event["stable_key"])
        for event in status.get("dropped_events", [])
        if event.get("stable_key")
    }
    entries = []
    for event in status.get("detected_events", []):
        try:
            start = datetime.fromisoformat(event["start"]).astimezone(zone)
            stop_value = event.get("stop")
            stop = epg_stop(start, stop_value, cfg, zone)
        except (KeyError, TypeError, ValueError):
            LOG.warning("Ungültiges Event im Mini-EPG ignoriert: %r", event)
            continue
        if stop <= now:
            continue

        key = str(event.get("stable_key", ""))
        slot = slot_by_key.get(key)
        entries.append(
            {
                **event,
                "start_dt": start,
                "stop_dt": stop,
                "is_current": start <= now < stop,
                "slot_id": slot.get("id") if slot else None,
                "slot_name": slot.get("name") if slot else None,
                "slot_number": slot.get("number") if slot else None,
                "is_dropped": key in dropped_keys,
            }
        )
    return sorted(
        entries,
        key=lambda event: (
            event["start_dt"],
            -int(event.get("priority_score", 0)),
            str(event.get("title", "")).casefold(),
        ),
    )


def recent_past_events(
    status: dict[str, Any], cfg: dict[str, Any]
) -> list[dict[str, Any]]:
    """Return the most recent finished events, newest first."""
    zone = zone_for(cfg)
    now = datetime.now(zone)
    limit = max(0, int(cfg.get("past_events_display_limit", 10)))
    entries = []
    for event in status.get("detected_events", []):
        try:
            start = datetime.fromisoformat(event["start"]).astimezone(zone)
            stop_value = event.get("stop")
            stop = epg_stop(start, stop_value, cfg, zone)
        except (KeyError, TypeError, ValueError):
            continue
        if stop > now:
            continue
        entries.append({**event, "start_dt": start, "stop_dt": stop})
    return sorted(
        entries,
        key=lambda event: (
            event["start_dt"],
            str(event.get("title", "")).casefold(),
        ),
        reverse=True,
    )[:limit]


def source_config_for_key(
    cfg: dict[str, Any], source_key: str
) -> Optional[dict[str, Any]]:
    return next(
        (
            source
            for source in normalized_sources(cfg)
            if source["key"] == source_key
        ),
        None,
    )


def app_log_lines(limit: int = 200) -> list[str]:
    with _app_log_lock:
        lines = list(_app_log_lines)
    if limit <= 0:
        return lines
    return lines[-limit:]


def replay_event_for_slot(
    slot_id: int,
    state: dict[str, Any],
    cfg: dict[str, Any],
    source_key: str,
    now: Optional[datetime] = None,
) -> Optional[dict[str, Any]]:
    """Assign recent finished events to idle slots, newest first and cyclically."""
    source_cfg = source_config_for_key(cfg, source_key)
    if source_cfg is None or not bool(source_cfg.get("idle_replay_enabled", True)):
        return None
    current_time = now or datetime.now(zone_for(cfg))
    active_keys = {
        key
        for slot in state.get("slots", [])
        if str(slot.get("source_key", "default")) == source_key
        for event in slot.get("events", [])
        if datetime.fromisoformat(event["start"])
        <= current_time
        < datetime.fromisoformat(event["stop"])
        for key in stored_event_slot_keys(event)
    }
    finished = []
    for event in state.get("detected_events", []):
        if str(event.get("source_key", "default")) != source_key:
            continue
        try:
            stop = datetime.fromisoformat(event["stop"])
        except (KeyError, TypeError, ValueError):
            continue
        event_keys = set(stored_event_slot_keys(event))
        if (
            stop <= current_time
            and event.get("url")
            and active_keys.isdisjoint(event_keys)
        ):
            finished.append(event)
    if not finished:
        return None
    finished.sort(
        key=lambda event: (
            datetime.fromisoformat(event["start"]),
            str(event.get("title", "")).casefold(),
        ),
        reverse=True,
    )
    return finished[(slot_id - 1) % len(finished)]


def background_refresher() -> None:
    while not _stop_refresh.is_set():
        try:
            cfg = load_config()
            wait_seconds = max(30, int(cfg["refresh_seconds"]))
        except Exception:
            LOG.exception("Konfiguration für Hintergrundaktualisierung ungültig")
            wait_seconds = 60
        if _stop_refresh.wait(wait_seconds):
            break
        refresh_state()


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    cfg = load_config()
    status = public_status(get_state(), cfg)
    rows = []
    for slot in status["slots"]:
        current = slot.get("current_event")
        next_event = slot.get("next_event")
        current_text = "–"
        if current:
            prefixes = {"replay": "Replay: ", "preview": "Vorschau: "}
            prefix = prefixes.get(str(slot.get("playback_mode")), "")
            current_text = prefix + str(current["title"])
            current_html = (
                player_link(
                    f'{slot["stream_path"][:-3]}.m3u',
                    slot["stream_path"],
                    current_text,
                    f'{slot["name"]} · {current_text}',
                    poster_path=slot.get("logo_path", ""),
                )
                + " "
                + stream_link(
                    ffmpeg_debug_path(slot.get("source_key", "default"), slot["id"]),
                    "ffmpeg",
                    "debug-link",
                )
            )
        else:
            current_html = html.escape(current_text)
        rows.append(
            "<tr>"
            f"<td>{slot['number']}</td>"
            f"<td>{html.escape(str(slot.get('source_name', 'Events')))}</td>"
            f"<td>{html.escape(slot['name'])}</td>"
            f"<td>{current_html}</td>"
            f"<td>{html.escape(next_event['title']) if next_event else '–'}</td>"
            "</tr>"
        )
    epg_entries = mini_epg_entries(status, cfg)
    current_events = [event for event in epg_entries if event["is_current"]]
    epg_rows = []
    previous_date = None
    for event in epg_entries:
        start = event["start_dt"]
        stop = event["stop_dt"]
        event_date = start.date()
        if event_date != previous_date:
            epg_rows.append(
                '<tr class="epg-day"><th colspan="6">'
                f"{start.strftime('%d.%m.%Y')}"
                "</th></tr>"
            )
            previous_date = event_date

        if event["is_current"]:
            time_text = f"Jetzt · bis {stop.strftime('%H:%M')}"
            row_class = "current"
        else:
            time_text = f"{start.strftime('%H:%M')}–{stop.strftime('%H:%M')}"
            row_class = ""

        if event.get("slot_name"):
            slot_text = (
                f"{event['slot_name']} "
                f"({event['slot_number']})"
            )
            slot_class = "slot"
            stream_path = (
                f"/slot/{event['source_key']}/{event['slot_id']}.m3u"
                if event.get("source_key") != "default"
                else f"/slot/{event['slot_id']}.m3u"
            )
            transport_path = (
                f"/slot/{event['source_key']}/{event['slot_id']}.ts"
                if event.get("source_key") != "default"
                else f"/slot/{event['slot_id']}.ts"
            )
            poster_path = (
                f"/slot/{event['source_key']}/{event['slot_id']}/logo"
                if event.get("source_key") != "default"
                else f"/slot/{event['slot_id']}/logo"
            )
            slot_html = player_link(
                stream_path,
                transport_path,
                slot_text,
                f"{slot_text} · {event.get('title') or 'Event'}",
                poster_path=poster_path,
            )
            if event["is_current"]:
                slot_html += " " + stream_link(
                    ffmpeg_debug_path(event.get("source_key", "default"), event["slot_id"]),
                    "ffmpeg",
                    "debug-link",
                )
        elif event.get("is_dropped"):
            slot_text = "Kein freier Slot"
            slot_class = "slot missing"
            slot_html = html.escape(slot_text)
        else:
            slot_text = "Nicht eingeplant"
            slot_class = "slot missing"
            slot_html = html.escape(slot_text)
        state_text = (
            "Läuft"
            if event["is_current"]
            else "Geplant"
            if event.get("slot_name")
            else "Kein Slot"
            if event.get("is_dropped")
            else "Offen"
        )

        description = str(event.get("desc") or "").strip()
        title_html = f"<strong>{html.escape(str(event.get('title') or 'Event'))}</strong>"
        if description:
            title_html += f'<div class="desc">{html.escape(description)}</div>'
        epg_rows.append(
            f'<tr class="{row_class}">'
            f'<td class="time">{html.escape(time_text)}</td>'
            f'<td>{html.escape(str(event.get("source_name") or event.get("source_key") or "Events"))}</td>'
            f'<td>{html.escape(str(event.get("group") or "Event"))}</td>'
            f"<td>{title_html}</td>"
            f'<td class="{slot_class}">{slot_html}</td>'
            f'<td class="state">{state_text}</td>'
            "</tr>"
        )
    epg_html = (
        '<table class="epg"><thead><tr><th>Zeit</th><th>Quelle</th><th>Gruppe</th><th>Sendung</th>'
        f"<th>Slot</th><th>Status</th></tr></thead><tbody>{''.join(epg_rows)}</tbody></table>"
        if epg_rows
        else '<p class="empty">Aktuell sind keine laufenden oder kommenden Events vorhanden.</p>'
    )
    past_events = recent_past_events(status, cfg) if not current_events else []
    past_rows = []
    for event in past_events:
        start = event["start_dt"]
        stop = event["stop_dt"]
        past_rows.append(
            "<tr>"
            f'<td class="time">{start.strftime("%d.%m.%Y %H:%M")}–{stop.strftime("%H:%M")}</td>'
            f'<td>{html.escape(str(event.get("source_name") or event.get("source_key") or "Events"))}</td>'
            f'<td>{html.escape(str(event.get("group") or "Event"))}</td>'
            f'<td><strong>{html.escape(str(event.get("title") or "Event"))}</strong>'
            + (
                f'<div class="desc">{html.escape(str(event["desc"]))}</div>'
                if event.get("desc")
                else ""
            )
            + "</td></tr>"
        )
    past_html = (
        '<h2>Zuletzt gelaufen</h2>'
        '<p>Da aktuell kein Event läuft, werden die zuletzt beendeten Events angezeigt.</p>'
        '<table class="past"><thead><tr><th>Zeit</th><th>Quelle</th><th>Gruppe</th><th>Sendung</th>'
        f"</tr></thead><tbody>{''.join(past_rows)}</tbody></table>"
        if past_rows
        else ""
    )
    log_lines = app_log_lines(80)
    log_html = (
        '<pre class="log">'
        + html.escape("\n".join(log_lines))
        + "</pre>"
        if log_lines
        else '<p class="empty">Noch keine App-Logzeilen im Speicher.</p>'
    )
    error = status.get("error")
    error_html = (
        '<p class="error"><strong>Letzter Fehler:</strong> '
        f"{html.escape(error['message'])}</p>"
        + "".join(
            '<p class="error source-error">'
            f"<strong>{html.escape(str(item['source_name']))}:</strong> "
            f"{html.escape(str(item['message']))}</p>"
            for item in status.get("errors", [])
        )
        if error
        else '<p class="ok">Quelle erfolgreich geladen.</p>'
    )
    body = f"""<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>TR-EventMux</title>
  {UI_THEME_INIT}
  <style>
    {UI_THEME_CSS}
    body {{ font: 16px system-ui, sans-serif; max-width: 1100px; margin: 2rem auto; padding: 0 1rem; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ padding: .65rem; border-bottom: 1px solid var(--border); text-align: left; }}
    th {{ background: var(--surface); }}
    .ok {{ color: var(--ok); }} .error {{ color: var(--error); }}
    .source-error {{ margin-left: 1rem; font-size: .92rem; }}
    code {{ background: var(--surface); padding: .15rem .35rem; }}
    td a {{ font-weight: 650; }}
    .debug-link {{ color: var(--muted); font-size: .85rem; font-weight: 500; margin-left: .35rem; }}
    h2 {{ margin-top: 2.2rem; }}
    .epg-day th {{ background: var(--surface-strong); color: var(--text); padding: .45rem .65rem; }}
    .epg .current td {{ background: var(--current); }}
    .epg .time {{ white-space: nowrap; font-variant-numeric: tabular-nums; }}
    .epg .slot {{ white-space: nowrap; }}
    .epg .missing {{ color: var(--error); }}
    .epg .state {{ color: var(--muted); white-space: nowrap; }}
    .epg .current .state {{ color: var(--ok); font-weight: 700; }}
    .desc {{ color: var(--muted); font-size: .9rem; margin-top: .2rem; }}
    .empty {{ padding: 1rem; background: var(--surface); }}
    .log {{ max-height: 24rem; overflow: auto; background: var(--log-bg); color: var(--log-text); padding: .8rem; font: 12px ui-monospace, SFMono-Regular, Consolas, monospace; white-space: pre-wrap; }}
    @media (max-width: 700px) {{
      body {{ margin-top: 1rem; }}
      th, td {{ padding: .5rem; }}
      .epg th:nth-child(6), .epg td:nth-child(6) {{ display: none; }}
      .epg .slot {{ white-space: normal; }}
    }}
  </style>
</head>
<body>
  {UI_THEME_TOGGLE}
  <h1>TR-EventMux</h1>
  <p><a href="/live">Live TV</a> · <a href="/playlist.m3u">Event-Playlist</a> ·
     <a href="/xmltv.xml">XMLTV</a> ·
     <a href="/status.json">JSON-Status</a> · <a href="/logs">Logs</a> ·
     <a href="/refresh">Jetzt aktualisieren</a></p>
  {error_html}
  <p>Letzte erfolgreiche Aktualisierung: <code>{html.escape(str(status.get('updated_at') or 'noch nie'))}</code><br>
     Quellen: <strong>{status.get('source_count', 1)}</strong> ·
     Erkannte Events: <strong>{status.get('detected_event_count', 0)}</strong> ·
     Live-TV-Kanäle: <strong>{status.get('live_channel_count', 0)}</strong></p>
  <table>
    <thead><tr><th>Nr.</th><th>Quelle</th><th>Slot</th><th>Aktuell</th><th>Nächstes Event</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
  <h2>Mini-EPG</h2>
  <p>{len(epg_entries)} laufende oder kommende Events, chronologisch nach Startzeit.</p>
  {epg_html}
  {past_html}
  <h2>App-Log</h2>
  <p>Letzte {len(log_lines)} Zeilen aus dem internen App-Log. Vollansicht: <a href="/logs">/logs</a></p>
  {log_html}
  {UI_WEB_PLAYER}
</body>
</html>"""
    return HTMLResponse(body, headers={"Cache-Control": "no-store"})


@app.get("/live", response_class=HTMLResponse)
def live_tv_index() -> HTMLResponse:
    state = get_state()
    channels = sorted(
        state.get("live_channels", []),
        key=lambda channel: (
            str(channel.get("source_name", "")).casefold(),
            str(channel.get("group", "")).casefold(),
            str(channel.get("name", "")).casefold(),
        ),
    )
    sections = []
    current_heading: Optional[tuple[str, str]] = None
    cards = []
    for channel in channels:
        heading = (
            str(channel.get("source_name") or channel.get("source_key") or "Live TV"),
            str(channel.get("group") or "Ohne Gruppe"),
        )
        if current_heading is not None and heading != current_heading:
            sections.append(
                '<section class="channel-section">'
                f"<h2>{html.escape(current_heading[0])} · {html.escape(current_heading[1])}</h2>"
                f'<div class="grid">{"".join(cards)}</div></section>'
            )
            cards = []
        current_heading = heading
        source_key = str(channel.get("source_key", "default"))
        channel_id = str(channel.get("id", ""))
        name = str(channel.get("name") or "Unbenannter Kanal")
        search_text = " ".join(
            [
                name,
                heading[0],
                heading[1],
                str(channel.get("source_tvg_id") or ""),
            ]
        ).casefold()
        logo_html = (
            f'<img src="/live/{html.escape(source_key, quote=True)}/'
            f'{html.escape(channel_id, quote=True)}/logo" alt="" loading="lazy">'
            if channel.get("logo")
            else '<div class="logo-placeholder">TV</div>'
        )
        cards.append(
            f'<article class="channel" data-search="{html.escape(search_text, quote=True)}">'
            f"{logo_html}<div><strong>{html.escape(name)}</strong>"
            f'<p>{html.escape(heading[1])}</p>'
            + player_link(
                f"/live/{source_key}/{channel_id}.m3u",
                f"/live/{source_key}/{channel_id}.ts",
                "Abspielen",
                f"{name} · {heading[1]}",
                css_class="play",
                poster_path=(
                    f"/live/{source_key}/{channel_id}/logo"
                    if channel.get("logo")
                    else ""
                ),
            )
            + "</div></article>"
        )
    if current_heading is not None:
        sections.append(
            '<section class="channel-section">'
            f"<h2>{html.escape(current_heading[0])} · {html.escape(current_heading[1])}</h2>"
            f'<div class="grid">{"".join(cards)}</div></section>'
        )
    content = (
        "".join(sections)
        if sections
        else '<p class="empty">Aktuell wurden keine normalen Live-TV-Kanäle erkannt.</p>'
    )
    body = f"""<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Live TV · TR-EventMux</title>
  {UI_THEME_INIT}
  <style>
    {UI_THEME_CSS}
    body {{ font: 16px system-ui, sans-serif; max-width: 1200px; margin: 2rem auto; padding: 0 1rem; }}
    .toolbar {{ display: flex; gap: .7rem; flex-wrap: wrap; align-items: center; }}
    input {{ flex: 1 1 20rem; padding: .7rem; border: 1px solid var(--border); border-radius: .4rem; background: var(--field); color: var(--text); font: inherit; }}
    .playlist, .play {{ display: inline-block; background: var(--link); color: var(--button-text); padding: .55rem .75rem; border-radius: .35rem; text-decoration: none; font-weight: 700; }}
    h2 {{ margin-top: 2rem; font-size: 1.15rem; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: .8rem; }}
    .channel {{ display: grid; grid-template-columns: 72px 1fr; gap: .8rem; align-items: center; border: 1px solid var(--border); border-radius: .5rem; padding: .8rem; background: var(--field); }}
    .channel img, .logo-placeholder {{ width: 72px; height: 54px; object-fit: contain; background: var(--surface); border-radius: .3rem; }}
    .logo-placeholder {{ display: grid; place-items: center; color: var(--muted); font-weight: 800; }}
    .channel p {{ color: var(--muted); margin: .2rem 0 .65rem; font-size: .9rem; }}
    .play {{ padding: .35rem .55rem; font-size: .9rem; }}
    .empty {{ padding: 1rem; background: var(--surface); }}
  </style>
</head>
<body>
  {UI_THEME_TOGGLE}
  <p><a href="/">Zurück zur Event-Übersicht</a></p>
  <h1>Live TV</h1>
  <p><strong>{len(channels)}</strong> in den zuletzt geladenen M3U-Quellen gelistete Kanäle. Ein Stream wird erst beim Öffnen gestartet.</p>
  <div class="toolbar">
    <input id="channel-search" type="search" placeholder="Sender, Gruppe oder Quelle suchen…" autocomplete="off">
    <a class="playlist" href="/live/playlist.m3u">Alle als M3U</a>
  </div>
  <main>{content}</main>
  {UI_WEB_PLAYER}
  <script>
    const search = document.getElementById("channel-search");
    search.addEventListener("input", () => {{
      const query = search.value.trim().toLocaleLowerCase("de");
      document.querySelectorAll(".channel").forEach(card => {{
        card.hidden = query && !card.dataset.search.includes(query);
      }});
      document.querySelectorAll(".channel-section").forEach(section => {{
        section.hidden = !Array.from(section.querySelectorAll(".channel")).some(card => !card.hidden);
      }});
    }});
  </script>
</body>
</html>"""
    return HTMLResponse(body, headers={"Cache-Control": "no-store"})


@app.get("/status.json")
def status_json() -> dict[str, Any]:
    cfg = load_config()
    return public_status(get_state(), cfg)


@app.get("/assets/mpegts.js")
def mpegts_javascript() -> Response:
    if not MPEGTS_JS_PATH.is_file():
        raise HTTPException(status_code=404, detail="Webplayer-Bibliothek fehlt")
    return Response(
        content=MPEGTS_JS_PATH.read_bytes(),
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@app.get("/logs", response_class=PlainTextResponse)
def logs() -> PlainTextResponse:
    return PlainTextResponse(
        "\n".join(app_log_lines(0)) + "\n",
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/refresh")
def refresh() -> dict[str, Any]:
    cfg = load_config()
    return public_status(refresh_state(), cfg)


def public_base_url(cfg: dict[str, Any]) -> str:
    explicit = str(cfg.get("public_base_url") or "").strip().rstrip("/")
    if explicit:
        return explicit
    host = str(cfg["host_for_playlist"]).strip()
    if "://" in host:
        parsed = urlsplit(host)
        if parsed.port:
            return host.rstrip("/")
        return f"{host.rstrip('/')}:{int(cfg['port'])}"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{int(cfg['port'])}"


def m3u_quote(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\r", " ").replace("\n", " ")


@app.get("/playlist.m3u", response_class=PlainTextResponse)
def playlist() -> PlainTextResponse:
    cfg = load_config()
    state = get_state()
    base_url = public_base_url(cfg)
    lines = ["#EXTM3U"]
    for slot in state["slots"]:
        group = m3u_quote(slot.get("group_title", cfg["group_title"]))
        logo_attr = (
            f' tvg-logo="{m3u_quote(base_url + slot["logo_path"])}"'
            if slot.get("logo_path")
            else ""
        )
        lines.append(
            f'#EXTINF:-1 tvg-id="{m3u_quote(slot["channel_id"])}" '
            f'tvg-name="{m3u_quote(slot["name"])}" tvg-chno="{slot["number"]}" '
            f'group-title="{group}"{logo_attr},{slot["name"]}'
        )
        lines.append(f"{base_url}{slot['stream_path']}")
    return PlainTextResponse(
        "\n".join(lines) + "\n",
        media_type="audio/x-mpegurl",
        headers={"Cache-Control": "no-store"},
    )


def live_channel_for(source_key: str, channel_id: str) -> dict[str, Any]:
    for channel in get_state().get("live_channels", []):
        if (
            str(channel.get("source_key")) == source_key
            and str(channel.get("id")) == channel_id
        ):
            return channel
    raise HTTPException(status_code=404, detail="Unbekannter Live-TV-Kanal")


def placeholder_logo_response() -> Response:
    body = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="144" height="108" '
        'viewBox="0 0 144 108">'
        '<rect width="144" height="108" rx="8" fill="#f3f5f7"/>'
        '<text x="72" y="64" text-anchor="middle" font-family="sans-serif" '
        'font-size="30" font-weight="700" fill="#69737e">TV</text></svg>'
    )
    return Response(
        content=body,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=300"},
    )


def host_resolves_to_private_address(host: str) -> bool:
    """Return True if a hostname resolves only to non-public addresses.

    The logo proxy fetches a URL taken from the (operator-configured) playlist
    on behalf of the client. Blocking loopback/private/link-local targets keeps
    it from being abused to reach internal services (SSRF).
    """
    if not host:
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        # Let the actual request fail/return the placeholder on DNS errors.
        return False
    addresses = {info[4][0] for info in infos}
    if not addresses:
        return False
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return True
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return True
    return False


def detected_image_media_type(content: bytes, content_type: str) -> str:
    normalized = content_type.split(";", 1)[0].strip().casefold()
    allowed = {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
    }
    if normalized in allowed:
        return normalized
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    return ""


@app.get("/live/{source_key}/{channel_id}/logo")
def live_channel_logo(source_key: str, channel_id: str) -> Response:
    try:
        normalized_key = safe_source_key(source_key)
    except RuntimeError:
        return placeholder_logo_response()
    try:
        channel = live_channel_for(normalized_key, channel_id)
    except HTTPException:
        return placeholder_logo_response()
    logo_url = str(channel.get("logo") or "").strip()
    if not logo_url.casefold().startswith(("http://", "https://")):
        return placeholder_logo_response()

    cfg = load_config()
    source_cfg = {
        source["key"]: source for source in normalized_sources(cfg)
    }.get(normalized_key, cfg)
    if bool(source_cfg.get("live_logo_block_private_hosts", False)) and (
        host_resolves_to_private_address(urlsplit(logo_url).hostname or "")
    ):
        LOG.warning(
            "Logo für Live-TV-Kanal %s/%s zeigt auf eine interne Adresse und wird blockiert: %s",
            normalized_key,
            channel_id,
            logo_url,
        )
        return placeholder_logo_response()
    headers = {}
    if source_cfg.get("source_user_agent"):
        headers["User-Agent"] = str(source_cfg["source_user_agent"])
    max_bytes = max(1024, int(source_cfg.get("live_logo_max_bytes", 2 * 1024 * 1024)))
    try:
        upstream = requests.get(
            logo_url,
            timeout=float(source_cfg["request_timeout_seconds"]),
            headers=headers,
            verify=bool(source_cfg["verify_tls"]),
            stream=True,
            allow_redirects=True,
        )
        try:
            upstream.raise_for_status()
            chunks = []
            size = 0
            for chunk in upstream.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                size += len(chunk)
                if size > max_bytes:
                    raise RuntimeError("Logo überschreitet die erlaubte Größe")
                chunks.append(chunk)
            content = b"".join(chunks)
            media_type = detected_image_media_type(
                content, upstream.headers.get("content-type", "")
            )
            if not content or not media_type:
                raise RuntimeError("Logo-Antwort ist kein unterstütztes Bild")
        finally:
            upstream.close()
    except (requests.RequestException, RuntimeError, OSError) as exc:
        LOG.warning(
            "Logo für Live-TV-Kanal %s/%s konnte nicht geladen werden: %s",
            normalized_key,
            channel_id,
            exc,
        )
        return placeholder_logo_response()

    return Response(
        content=content,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/live/playlist.m3u", response_class=PlainTextResponse)
def live_tv_playlist() -> PlainTextResponse:
    cfg = load_config()
    base_url = public_base_url(cfg)
    lines = ["#EXTM3U"]
    for channel in get_state().get("live_channels", []):
        source_key = str(channel.get("source_key", "default"))
        channel_id = str(channel.get("id", ""))
        name = str(channel.get("name") or "Live TV")
        group = str(channel.get("group") or channel.get("source_name") or "Live TV")
        tvg_id = str(channel.get("source_tvg_id") or f"live.{source_key}.{channel_id}")
        attrs = [
            f'tvg-id="{m3u_quote(tvg_id)}"',
            f'tvg-name="{m3u_quote(name)}"',
            f'group-title="{m3u_quote(group)}"',
        ]
        if channel.get("logo"):
            attrs.append(
                f'tvg-logo="{base_url}/live/{source_key}/{channel_id}/logo"'
            )
        if channel.get("channel_number"):
            attrs.append(f'tvg-chno="{m3u_quote(channel["channel_number"])}"')
        lines.append(f'#EXTINF:-1 {" ".join(attrs)},{name}')
        lines.append(f"{base_url}/live/{source_key}/{channel_id}.ts")
    return PlainTextResponse(
        "\n".join(lines) + "\n",
        media_type="audio/x-mpegurl",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/live/{source_key}/{channel_id}.m3u", response_class=PlainTextResponse)
def live_channel_playlist(source_key: str, channel_id: str) -> PlainTextResponse:
    try:
        normalized_key = safe_source_key(source_key)
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail="Unbekannte Quelle") from exc
    channel = live_channel_for(normalized_key, channel_id)
    cfg = load_config()
    name = str(channel.get("name") or "Live TV")
    stream_url = f"{public_base_url(cfg)}/live/{normalized_key}/{channel_id}.ts"
    lines = [
        "#EXTM3U",
        (
            f'#EXTINF:-1 tvg-id="{m3u_quote(channel.get("source_tvg_id") or channel_id)}" '
            f'tvg-name="{m3u_quote(name)}" '
            f'group-title="{m3u_quote(channel.get("group") or "Live TV")}",{name}'
        ),
        stream_url,
    ]
    return PlainTextResponse(
        "\n".join(lines) + "\n",
        media_type="audio/x-mpegurl",
        headers={"Cache-Control": "no-store"},
    )


def slot_playlist_response(
    source_key: str, slot_id: int, request: Optional[Request] = None
) -> PlainTextResponse:
    cfg = load_config()
    state = get_state()
    base_url = public_base_url(cfg)
    slot = next(
        (
            item
            for item in state.get("slots", [])
            if item.get("source_key") == source_key and int(item.get("id", 0)) == slot_id
        ),
        None,
    )
    if slot is None:
        raise HTTPException(status_code=404, detail="Unbekannter Slot")
    group = m3u_quote(slot.get("group_title", cfg["group_title"]))
    logo_attr = (
        f' tvg-logo="{m3u_quote(base_url + slot["logo_path"])}"'
        if slot.get("logo_path")
        else ""
    )
    query = str(request.url.query) if request is not None else ""
    stream_url = f"{base_url}{slot['stream_path']}" + (f"?{query}" if query else "")
    lines = [
        "#EXTM3U",
        (
            f'#EXTINF:-1 tvg-id="{m3u_quote(slot["channel_id"])}" '
            f'tvg-name="{m3u_quote(slot["name"])}" tvg-chno="{slot["number"]}" '
            f'group-title="{group}"{logo_attr},{slot["name"]}'
        ),
        stream_url,
    ]
    return PlainTextResponse(
        "\n".join(lines) + "\n",
        media_type="audio/x-mpegurl",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'inline; filename="{m3u_quote(slot["name"])}.m3u"',
        },
    )


def slot_logo_response(source_key: str, slot_id: int) -> Response:
    source_cfg = source_config_for_key(load_config(), source_key)
    if source_cfg is None or not 1 <= slot_id <= int(source_cfg["slots"]):
        raise HTTPException(status_code=404, detail="Unbekannter Slot")
    channel_number = int(source_cfg["channel_number_start"]) + slot_id - 1
    logo_file = EVENT_LOGO_DIR / f"event-{channel_number}.png"
    if not logo_file.is_file():
        raise HTTPException(status_code=404, detail="Kein Logo für diesen Slot")
    return Response(
        content=logo_file.read_bytes(),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


def xml_attr(value: Any) -> str:
    return html.escape(str(value), quote=True)


def xml_text(value: Any) -> str:
    return html.escape(str(value), quote=False)


def stream_link(path: Any, label: Any, css_class: str = "") -> str:
    class_attr = f' class="{html.escape(css_class, quote=True)}"' if css_class else ""
    return (
        f'<a{class_attr} href="{html.escape(str(path), quote=True)}">'
        f"{html.escape(str(label))}</a>"
    )


def player_link(
    playlist_path: Any,
    stream_path: Any,
    label: Any,
    title: Any,
    css_class: str = "",
    poster_path: Any = "",
) -> str:
    classes = "stream-action"
    if css_class:
        classes += f" {css_class}"
    poster_attr = (
        f' data-poster-url="{html.escape(str(poster_path), quote=True)}"'
        if poster_path
        else ""
    )
    return (
        f'<a class="{html.escape(classes, quote=True)}" '
        f'href="{html.escape(str(playlist_path), quote=True)}" '
        f'data-stream-url="{html.escape(str(stream_path), quote=True)}" '
        f'data-stream-title="{html.escape(str(title), quote=True)}"'
        f"{poster_attr}>"
        f"{html.escape(str(label))}</a>"
    )


UI_THEME_INIT = """<script>
    (() => {
      const saved = localStorage.getItem("tr-eventmux-theme");
      const theme = saved || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
      document.documentElement.dataset.theme = theme;
      document.documentElement.dataset.playerMode =
        localStorage.getItem("tr-eventmux-player-mode") || "web";
    })();
  </script>"""

UI_THEME_CSS = """
    :root {
      color-scheme: light;
      --bg: #ffffff;
      --text: #20242a;
      --surface: #f3f5f7;
      --surface-strong: #e8edf3;
      --border: #d8dde3;
      --link: #175bb5;
      --muted: #59636e;
      --ok: #176b35;
      --error: #a12622;
      --current: #eaf7ee;
      --field: #ffffff;
      --button-text: #ffffff;
      --log-bg: #111820;
      --log-text: #dce6ef;
    }
    :root[data-theme="dark"] {
      color-scheme: dark;
      --bg: #11161c;
      --text: #e7edf3;
      --surface: #1b232c;
      --surface-strong: #25313d;
      --border: #354250;
      --link: #79b5ff;
      --muted: #a9b6c3;
      --ok: #72d493;
      --error: #ff928c;
      --current: #173326;
      --field: #151c23;
      --button-text: #ffffff;
      --log-bg: #090d11;
      --log-text: #dce6ef;
    }
    body { background: var(--bg); color: var(--text); }
    a { color: var(--link); }
    .ui-controls {
      position: fixed; top: .8rem; right: .8rem; z-index: 30;
      display: flex; gap: .45rem;
    }
    .theme-toggle, .player-mode-toggle {
      border: 1px solid var(--border); border-radius: 999px;
      background: var(--surface); color: var(--text);
      padding: .45rem .7rem; font: 600 13px system-ui, sans-serif;
      cursor: pointer;
    }
    .player-modal[hidden] { display: none; }
    .player-modal {
      position: fixed; inset: 0; z-index: 100;
      display: grid; place-items: center; padding: 1rem;
      background: rgba(4, 8, 12, .82); backdrop-filter: blur(8px);
    }
    .player-dialog {
      width: min(1100px, 96vw); overflow: hidden;
      border: 1px solid var(--border); border-radius: .8rem;
      background: var(--field); box-shadow: 0 1.2rem 4rem rgba(0, 0, 0, .45);
    }
    .player-header {
      display: flex; align-items: center; gap: .8rem;
      padding: .75rem 1rem; background: var(--surface);
    }
    .player-title { flex: 1; min-width: 0; }
    .player-title strong, .player-title small { display: block; }
    .player-title small { color: var(--muted); margin-top: .15rem; }
    .player-close {
      border: 1px solid var(--border); border-radius: 999px;
      background: var(--field); color: var(--text);
      width: 2.2rem; height: 2.2rem; cursor: pointer; font-size: 1.15rem;
    }
    .player-stage { background: #000; aspect-ratio: 16 / 9; }
    .player-stage video { display: block; width: 100%; height: 100%; background: #000; }
    .player-message {
      margin: 0; padding: .7rem 1rem; min-height: 1.2rem;
      color: var(--muted); font-size: .9rem;
    }
    @media (max-width: 700px) {
      .ui-controls { position: static; justify-content: flex-end; margin-bottom: .8rem; }
      .player-modal { padding: .35rem; }
    }
"""

UI_THEME_TOGGLE = """<div class="ui-controls">
    <button class="player-mode-toggle" id="player-mode-toggle" type="button" aria-label="Wiedergabemodus wechseln"></button>
    <button class="theme-toggle" id="theme-toggle" type="button" aria-label="Farbschema wechseln"></button>
  </div>
  <script>
    (() => {
      const themeButton = document.getElementById("theme-toggle");
      const modeButton = document.getElementById("player-mode-toggle");
      const updateTheme = () => {
        const dark = document.documentElement.dataset.theme === "dark";
        themeButton.textContent = dark ? "☀ Hell" : "◐ Dunkel";
        themeButton.setAttribute("aria-pressed", String(dark));
      };
      const updateMode = () => {
        const web = document.documentElement.dataset.playerMode !== "vlc";
        modeButton.textContent = web ? "▶ Webplayer" : "◆ VLC";
        modeButton.setAttribute("aria-pressed", String(web));
        modeButton.title = web
          ? "Streams werden im Browser geöffnet"
          : "Streams werden über eine M3U-Datei an VLC übergeben";
      };
      themeButton.addEventListener("click", () => {
        const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
        document.documentElement.dataset.theme = next;
        localStorage.setItem("tr-eventmux-theme", next);
        updateTheme();
      });
      modeButton.addEventListener("click", () => {
        const next = document.documentElement.dataset.playerMode === "vlc" ? "web" : "vlc";
        document.documentElement.dataset.playerMode = next;
        localStorage.setItem("tr-eventmux-player-mode", next);
        updateMode();
      });
      updateTheme();
      updateMode();
    })();
  </script>"""

UI_WEB_PLAYER = """<div class="player-modal" id="web-player-modal" hidden>
    <section class="player-dialog" role="dialog" aria-modal="true" aria-labelledby="web-player-title">
      <header class="player-header">
        <div class="player-title">
          <strong id="web-player-title">Interner Webplayer</strong>
          <small id="web-player-subtitle">Stream wird vorbereitet…</small>
        </div>
        <button class="player-close" id="web-player-close" type="button" aria-label="Player schließen">×</button>
      </header>
      <div class="player-stage">
        <video id="web-player-video" controls playsinline></video>
      </div>
      <p class="player-message" id="web-player-message"></p>
    </section>
  </div>
  <script src="/assets/mpegts.js"></script>
  <script>
    (() => {
      const modal = document.getElementById("web-player-modal");
      if (!modal) return;
      const video = document.getElementById("web-player-video");
      const title = document.getElementById("web-player-title");
      const subtitle = document.getElementById("web-player-subtitle");
      const message = document.getElementById("web-player-message");
      const closeButton = document.getElementById("web-player-close");
      let player = null;

      const destroyPlayer = () => {
        if (player) {
          try { player.pause(); } catch (_) {}
          try { player.unload(); } catch (_) {}
          try { player.detachMediaElement(); } catch (_) {}
          try { player.destroy(); } catch (_) {}
          player = null;
        }
        video.pause();
        video.removeAttribute("src");
        video.removeAttribute("poster");
        video.load();
      };

      const closePlayer = () => {
        destroyPlayer();
        modal.hidden = true;
        document.body.style.overflow = "";
      };

      const openPlayer = async (trigger) => {
        destroyPlayer();
        const streamUrl = new URL(trigger.dataset.streamUrl, window.location.href).href;
        title.textContent = trigger.dataset.streamTitle || trigger.textContent.trim();
        subtitle.textContent = "Interner Webplayer · MPEG-TS";
        message.textContent = "Stream wird verbunden…";
        if (trigger.dataset.posterUrl) video.poster = trigger.dataset.posterUrl;
        modal.hidden = false;
        document.body.style.overflow = "hidden";

        if (!window.mpegts || !window.mpegts.isSupported()) {
          message.textContent =
            "Dieser Browser unterstützt den internen MPEG-TS-Player nicht. Bitte oben auf VLC umschalten.";
          return;
        }

        try {
          player = window.mpegts.createPlayer(
            {
              type: "mpegts",
              isLive: true,
              url: streamUrl,
            },
            {
              enableWorker: true,
              enableStashBuffer: true,
              stashInitialSize: 1024 * 1024,
              lazyLoad: false,
              liveBufferLatencyChasing: false,
              liveSync: false,
              autoCleanupSourceBuffer: true,
              autoCleanupMaxBackwardDuration: 60,
              autoCleanupMinBackwardDuration: 30,
              fixAudioTimestampGap: true,
            }
          );
          player.attachMediaElement(video);
          player.on(window.mpegts.Events.ERROR, (_type, detail, info) => {
            const extra = info && info.msg ? `: ${info.msg}` : "";
            message.textContent =
              `Wiedergabefehler (${detail || "unbekannt"})${extra}. VLC kann für die Diagnose genutzt werden.`;
          });
          player.on(window.mpegts.Events.MEDIA_INFO, () => {
            message.textContent = "Live · Stream verbunden · Stabilitätspuffer aktiv";
          });
          player.load();
          await video.play();
        } catch (error) {
          const detail = String(error && error.message ? error.message : error);
          message.textContent =
            error && (error.name === "NotAllowedError" || detail.includes("didn't interact"))
              ? "Player bereit. Bitte die Play-Taste im Videofenster drücken."
              : `Der Stream konnte nicht gestartet werden: ${detail}.`;
        }
      };

      document.addEventListener("click", event => {
        const trigger = event.target.closest("[data-stream-url]");
        if (!trigger || document.documentElement.dataset.playerMode === "vlc") return;
        event.preventDefault();
        openPlayer(trigger);
      });
      closeButton.addEventListener("click", closePlayer);
      modal.addEventListener("click", event => {
        if (event.target === modal) closePlayer();
      });
      document.addEventListener("keydown", event => {
        if (event.key === "Escape" && !modal.hidden) closePlayer();
      });
      window.addEventListener("pagehide", destroyPlayer);
    })();
  </script>"""


def ffmpeg_debug_path(source_key: Any, slot_id: Any) -> str:
    if str(source_key) == "default":
        return f"/slot/{slot_id}/ffmpeg"
    return f"/slot/{source_key}/{slot_id}/ffmpeg"


def xmltv_datetime(value: str) -> str:
    return datetime.fromisoformat(value).strftime("%Y%m%d%H%M%S %z")


def epg_stop(
    start: datetime,
    stop_value: Any,
    cfg: dict[str, Any],
    zone: Optional[ZoneInfo] = None,
) -> datetime:
    """Return a display-only stop capped independently from stream playback."""
    display_limit = start + timedelta(
        minutes=int(cfg.get("epg_max_duration_minutes", 240))
    )
    if stop_value:
        nominal_stop = datetime.fromisoformat(str(stop_value))
        if zone is not None:
            nominal_stop = nominal_stop.astimezone(zone)
    else:
        nominal_stop = start + timedelta(
            minutes=int(cfg["default_duration_minutes"])
        )
    return min(nominal_stop, display_limit)


def xmltv_sport_name(group: Any) -> str:
    value = str(group or "").strip()
    if not value:
        return ""
    mappings = {
        "american football": "Football",
        "bare knuckle fighting championship": "Bare Knuckle Fighting Championship",
        "basketball": "Basketball",
        "boxing": "Boxing",
        "darts": "Darts",
        "football": "Soccer",
        "mma": "MMA",
        "motorsport": "Motorsport",
        "padel": "Padel",
        "softball": "Softball",
        "tennis": "Tennis",
    }
    return mappings.get(value.casefold(), value)


def xmltv_teams(title: Any) -> list[str]:
    text = str(title or "").strip()
    if " - " not in text or " | " in text:
        return []
    teams = [part.strip() for part in text.split(" - ", 1)]
    if len(teams) != 2 or not all(teams):
        return []
    return teams


def render_xmltv(state: dict[str, Any], cfg: dict[str, Any]) -> str:
    """Render the shared XMLTV representation for HTTP and local file output."""
    language = xml_attr(cfg["xmltv_language"])
    output = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<tv generator-info-name="TR-EventMux">',
    ]
    for slot in state["slots"]:
        output.append(f'  <channel id="{xml_attr(slot["channel_id"])}">')
        output.append(f'    <display-name>{xml_text(slot["name"])}</display-name>')
        if slot.get("logo_path"):
            output.append(
                f'    <icon src="{xml_attr(public_base_url(cfg) + slot["logo_path"])}" />'
            )
        output.append("  </channel>")
    for slot in state["slots"]:
        for event in slot.get("events", []):
            start = datetime.fromisoformat(event["start"])
            stop = epg_stop(start, event.get("stop"), cfg)
            output.append(
                f'  <programme start="{xmltv_datetime(event["start"])}" '
                f'stop="{xmltv_datetime(stop.isoformat())}" '
                f'channel="{xml_attr(slot["channel_id"])}">'
            )
            output.append(
                f'    <title lang="{language}">{xml_text(event["title"])}</title>'
            )
            if event.get("desc"):
                output.append(
                    f'    <desc lang="{language}">{xml_text(event["desc"])}</desc>'
                )
            output.append(f'    <category lang="{language}">Event</category>')
            if event.get("group"):
                output.append(
                    f'    <category lang="{language}">{xml_text(event["group"])}</category>'
                )
            sport = xmltv_sport_name(event.get("group"))
            if sport:
                output.append(f"    <sport>{xml_text(sport)}</sport>")
            for team in xmltv_teams(event.get("title")):
                output.append(f"    <team>{xml_text(team)}</team>")
            if event.get("logo"):
                output.append(f'    <icon src="{xml_attr(event["logo"])}" />')
            output.append("  </programme>")
    output.append("</tv>")
    return "\n".join(output) + "\n"


def write_xmltv_file(state: dict[str, Any], cfg: dict[str, Any]) -> None:
    configured_path = str(cfg.get("xmltv_output_path") or "").strip()
    if not configured_path:
        return
    output_path = Path(configured_path)
    temporary = output_path.with_name(f"{output_path.name}.tmp")
    rendered = render_xmltv(state, cfg).encode("utf-8")
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if output_path.is_file() and output_path.read_bytes() == rendered:
            LOG.debug("XMLTV-Datei unverändert: %s", output_path)
            return
        temporary.write_bytes(rendered)
        temporary.replace(output_path)
        LOG.info("XMLTV-Datei aktualisiert: %s", output_path)
    except OSError:
        LOG.exception("XMLTV-Datei konnte nicht nach %s geschrieben werden", output_path)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


@app.get("/xmltv.xml", response_class=PlainTextResponse)
def xmltv() -> PlainTextResponse:
    cfg = load_config()
    state = get_state()
    return PlainTextResponse(
        render_xmltv(state, cfg),
        media_type="application/xml",
        headers={"Cache-Control": "no-store"},
    )


def current_event_for_slot(
    slot_id: int,
    state: dict[str, Any],
    cfg: dict[str, Any],
    source_key: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Optional[dict[str, Any]]:
    current_time = now or datetime.now(zone_for(cfg))
    source_cfg = source_config_for_key(cfg, source_key) if source_key else cfg
    effective_cfg = source_cfg or cfg
    early = timedelta(
        minutes=max(0, int(effective_cfg.get("tune_early_minutes", 5)))
    )
    for slot in state["slots"]:
        if source_key is not None and slot.get("source_key") != source_key:
            continue
        if int(slot["id"]) != slot_id:
            continue
        for event in slot.get("events", []):
            start = datetime.fromisoformat(event["start"])
            stop = datetime.fromisoformat(event["stop"])
            if start - early <= current_time < stop:
                return event
        return None
    return None


def next_live_activation_for_slot(
    slot_id: int,
    state: dict[str, Any],
    cfg: dict[str, Any],
    source_key: str,
    now: Optional[datetime] = None,
) -> Optional[datetime]:
    current_time = now or datetime.now(zone_for(cfg))
    source_cfg = source_config_for_key(cfg, source_key) or cfg
    early = timedelta(
        minutes=max(0, int(source_cfg.get("tune_early_minutes", 5)))
    )
    for slot in state["slots"]:
        if slot.get("source_key") != source_key or int(slot["id"]) != slot_id:
            continue
        activations = [
            datetime.fromisoformat(event["start"]) - early
            for event in slot.get("events", [])
            if datetime.fromisoformat(event["start"]) - early > current_time
        ]
        return min(activations) if activations else None
    return None


def upcoming_stream_for_slot(
    slot_id: int,
    state: dict[str, Any],
    cfg: dict[str, Any],
    source_key: str,
    now: Optional[datetime] = None,
) -> Optional[dict[str, Any]]:
    """Return the next assigned stream before its scheduled activation."""
    source_cfg = source_config_for_key(cfg, source_key)
    if source_cfg is None or not bool(source_cfg.get("allow_upcoming_stream", False)):
        return None
    current_time = now or datetime.now(zone_for(cfg))
    early = timedelta(
        minutes=max(0, int(source_cfg.get("tune_early_minutes", 5)))
    )
    for slot in state["slots"]:
        if slot.get("source_key") != source_key or int(slot["id"]) != slot_id:
            continue
        upcoming = [
            event
            for event in slot.get("events", [])
            if event.get("url")
            and datetime.fromisoformat(event["start"]) - early > current_time
        ]
        return (
            min(upcoming, key=lambda event: datetime.fromisoformat(event["start"]))
            if upcoming
            else None
        )
    return None


def playback_for_slot(
    slot_id: int,
    state: dict[str, Any],
    cfg: dict[str, Any],
    source_key: str,
    now: Optional[datetime] = None,
) -> tuple[Optional[dict[str, Any]], Optional[str], Optional[datetime]]:
    """Return event, mode and replay cutoff for the requested slot."""
    current_time = now or datetime.now(zone_for(cfg))
    live_event = current_event_for_slot(
        slot_id, state, cfg, source_key, now=current_time
    )
    if live_event:
        return live_event, "live", None
    upcoming_event = upcoming_stream_for_slot(
        slot_id, state, cfg, source_key, now=current_time
    )
    if upcoming_event:
        return upcoming_event, "preview", None
    replay_event = replay_event_for_slot(
        slot_id, state, cfg, source_key, now=current_time
    )
    if replay_event:
        return (
            replay_event,
            "replay",
            next_live_activation_for_slot(
                slot_id, state, cfg, source_key, now=current_time
            ),
        )
    return None, None, None


HEX_KEY_RE = re.compile(
    r"[0-9a-fA-F]{32}[:=][0-9a-fA-F]{32}"
    r"(?:(?:,|:)[0-9a-fA-F]{32}[:=][0-9a-fA-F]{32})*"
)
HEX_PAIR_RE = re.compile(r"([0-9a-fA-F]{32})[:=]([0-9a-fA-F]{32})")


def clearkey_value_to_hex(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("leerer Provider-Wert")
    if re.fullmatch(r"[0-9a-fA-F]{32}", text):
        return text.lower()
    padded = text + "=" * (-len(text) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (binascii.Error, UnicodeEncodeError) as exc:
        raise ValueError("Provider-Wert ist weder Hex noch Base64URL") from exc
    if len(decoded) != 16:
        raise ValueError("Provider-Wert hat nicht 16 Byte")
    return decoded.hex()


def normalize_decryption_keys(raw_text: str) -> str:
    compact = re.sub(r"\s+", "", raw_text)
    if not compact:
        raise RuntimeError("Der Provider-Endpunkt lieferte keine Daten")
    if len(compact) > 16384:
        raise RuntimeError("Der Provider-Endpunkt lieferte zu viele Daten")
    if HEX_KEY_RE.fullmatch(compact):
        pairs = HEX_PAIR_RE.findall(compact)
        return ":".join(f"{kid.lower()}={key.lower()}" for kid, key in pairs)
    embedded_pairs = HEX_PAIR_RE.findall(compact)
    if embedded_pairs:
        return ":".join(
            f"{kid.lower()}={key.lower()}" for kid, key in embedded_pairs
        )

    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "Der Provider-Endpunkt lieferte kein gueltiges Datenformat"
        ) from exc

    if isinstance(payload, str):
        return normalize_decryption_keys(payload)

    key_items: list[tuple[Any, Any]] = []
    if isinstance(payload, dict):
        if isinstance(payload.get("keys"), list):
            for item in payload["keys"]:
                if isinstance(item, dict):
                    key_items.append((item.get("kid"), item.get("k") or item.get("key")))
        elif payload.get("kid") and (payload.get("k") or payload.get("key")):
            key_items.append((payload.get("kid"), payload.get("k") or payload.get("key")))
        else:
            key_items.extend(payload.items())

    keys: list[str] = []
    for kid, key in key_items:
        try:
            keys.append(f"{clearkey_value_to_hex(kid)}={clearkey_value_to_hex(key)}")
        except ValueError:
            continue
    if not keys:
        raise RuntimeError(
            "Der Provider-Endpunkt lieferte kein gueltiges Datenformat"
        )
    return ":".join(keys)


def decryption_key_count(decryption_keys: str) -> int:
    return len(HEX_PAIR_RE.findall(decryption_keys))


def fetch_decryption_keys(license_url: str, cfg: dict[str, Any]) -> str:
    headers = {}
    user_agent = cfg.get("license_user_agent") or cfg.get("source_user_agent")
    if user_agent:
        headers["User-Agent"] = str(user_agent)
    configured_headers = cfg.get("license_headers") or {}
    if isinstance(configured_headers, dict):
        headers.update({str(key): str(value) for key, value in configured_headers.items()})
    response = requests.get(
        license_url,
        timeout=float(
            cfg.get("license_timeout_seconds", cfg["request_timeout_seconds"])
        ),
        headers=headers,
        verify=bool(cfg["verify_tls"]),
    )
    response.raise_for_status()
    return normalize_decryption_keys(response.text)


def probe_manifest_once(url: str, cfg: dict[str, Any]) -> None:
    headers = {}
    if cfg.get("ffmpeg_user_agent"):
        headers["User-Agent"] = str(cfg["ffmpeg_user_agent"])
    elif cfg.get("source_user_agent"):
        headers["User-Agent"] = str(cfg["source_user_agent"])
    response = requests.get(
        url,
        timeout=float(
            cfg.get("manifest_probe_timeout_seconds", cfg["request_timeout_seconds"])
        ),
        headers=headers,
        verify=bool(cfg["verify_tls"]),
        stream=True,
    )
    try:
        response.raise_for_status()
        limit = max(1024, int(cfg.get("manifest_probe_bytes", 65536)))
        chunk = next(response.iter_content(chunk_size=limit), b"")
    finally:
        response.close()
    if not chunk:
        raise RuntimeError("Manifest ist leer oder noch nicht bereit")
    stripped = chunk.lstrip()
    if b"<MPD" not in stripped[:1024] and b":MPD" not in stripped[:1024]:
        content_type = response.headers.get("content-type", "unbekannt")
        preview = stripped[:160].decode("utf-8", errors="replace").replace("\n", " ")
        raise RuntimeError(
            "Manifest sieht nicht nach MPEG-DASH aus "
            f"(Content-Type: {content_type}, Anfang: {preview!r})"
        )


def validate_manifest_url(url: str, cfg: dict[str, Any]) -> None:
    if not bool(cfg.get("manifest_probe_enabled", True)):
        return
    if not urlsplit(url).path.lower().endswith(".mpd"):
        return

    attempts = max(1, int(cfg.get("manifest_probe_attempts", 3)))
    retry_seconds = max(0.0, float(cfg.get("manifest_probe_retry_seconds", 0.75)))
    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            probe_manifest_once(url, cfg)
            return
        except (requests.RequestException, RuntimeError) as exc:
            last_error = str(exc)
            if attempt < attempts and retry_seconds:
                time.sleep(retry_seconds)

    message = (
        f"Manifest-Pruefung fehlgeschlagen nach {attempts} Versuch(en): "
        f"{last_error}"
    )
    failure_mode = str(cfg.get("manifest_probe_failure_mode", "warn")).casefold()
    if failure_mode in {"block", "error", "strict"}:
        raise RuntimeError(message)
    LOG.warning("%s; ffmpeg wird trotzdem gestartet", message)


FFMPEG_OVERRIDE_FIELDS = ("drm_args", "input_args", "output_args")


def parse_ffmpeg_arg_text(value: Any, field_name: str) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    try:
        return shlex.split(text)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Ungueltige ffmpeg-Parameter in {field_name}: {exc}",
        ) from exc


def ffmpeg_overrides_from_values(
    drm_args: Any = "",
    input_args: Any = "",
    output_args: Any = "",
) -> dict[str, list[str]]:
    return {
        "drm_args": parse_ffmpeg_arg_text(drm_args, "drm_args"),
        "input_args": parse_ffmpeg_arg_text(input_args, "input_args"),
        "output_args": parse_ffmpeg_arg_text(output_args, "output_args"),
    }


def ffmpeg_overrides_from_request(
    request: Optional[Request],
) -> dict[str, list[str]]:
    if request is None:
        return ffmpeg_overrides_from_values()
    return ffmpeg_overrides_from_values(
        request.query_params.get("drm_args", ""),
        request.query_params.get("input_args", ""),
        request.query_params.get("output_args", ""),
    )


def ffmpeg_override_query_from_values(
    drm_args: Any = "",
    input_args: Any = "",
    output_args: Any = "",
) -> str:
    params = {
        "drm_args": str(drm_args or "").strip(),
        "input_args": str(input_args or "").strip(),
        "output_args": str(output_args or "").strip(),
    }
    return urlencode({key: value for key, value in params.items() if value})


def mask_ffmpeg_command(command: list[str]) -> list[str]:
    masked: list[str] = []
    hide_next = False
    for value in command:
        if hide_next:
            count = decryption_key_count(value)
            label = f"<masked {count} key(s)>" if count else "<masked key(s)>"
            masked.append(label)
            hide_next = False
            continue
        masked.append(value)
        if value == "-cenc_decryption_keys":
            hide_next = True
    return masked


def config_arg_list(cfg: dict[str, Any], key: str) -> list[str]:
    value = cfg.get(key, [])
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    if isinstance(value, str):
        try:
            return shlex.split(value)
        except ValueError as exc:
            raise RuntimeError(
                f"Ungueltige ffmpeg-Konfiguration {key}: {exc}"
            ) from exc
    raise RuntimeError(f"ffmpeg-Konfiguration {key} muss Liste oder String sein")


def ffmpeg_command(
    url: str,
    cfg: dict[str, Any],
    decryption_keys: str = "",
    event_input_args: Optional[list[str]] = None,
    override_drm_input_args: Optional[list[str]] = None,
    override_input_args: Optional[list[str]] = None,
    override_output_args: Optional[list[str]] = None,
) -> list[str]:
    command = [str(cfg["ffmpeg"])]
    command.extend(config_arg_list(cfg, "ffmpeg_base_args"))
    if cfg.get("ffmpeg_reconnect") and url.lower().startswith(("http://", "https://")):
        command.extend(config_arg_list(cfg, "ffmpeg_reconnect_args"))
    if cfg.get("ffmpeg_user_agent"):
        command.extend(["-user_agent", str(cfg["ffmpeg_user_agent"])])
    if decryption_keys:
        command.extend(["-cenc_decryption_keys", decryption_keys])
        command.extend(config_arg_list(cfg, "ffmpeg_drm_input_args"))
        command.extend(str(value) for value in override_drm_input_args or [])
    command.extend(str(value) for value in event_input_args or [])
    command.extend(config_arg_list(cfg, "ffmpeg_extra_input_args"))
    command.extend(str(value) for value in override_input_args or [])
    command.extend(["-i", url])
    command.extend(config_arg_list(cfg, "ffmpeg_map_args"))
    command.extend(config_arg_list(cfg, "ffmpeg_codec_args"))
    if decryption_keys:
        command.extend(config_arg_list(cfg, "ffmpeg_drm_output_args"))
    command.extend(config_arg_list(cfg, "ffmpeg_extra_output_args"))
    command.extend(str(value) for value in override_output_args or [])
    mpegts_flags = (
        str(cfg.get("ffmpeg_drm_mpegts_flags", "+resend_headers+initial_discontinuity"))
        if decryption_keys
        else str(cfg.get("ffmpeg_mpegts_flags", "+resend_headers"))
    )
    if mpegts_flags:
        command.extend(["-mpegts_flags", mpegts_flags])
    command.extend(config_arg_list(cfg, "ffmpeg_mpegts_output_args"))
    command.append("pipe:1")
    return command


def stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)
    except OSError:
        pass


def stream_start_attempts(cfg: dict[str, Any]) -> int:
    return max(1, int(cfg.get("stream_start_attempts", 4)))


def stream_no_data_retries(cfg: dict[str, Any]) -> int:
    return max(0, int(cfg.get("stream_no_data_retries", 3)))


def stream_start_retry_seconds(cfg: dict[str, Any]) -> float:
    return max(0.0, float(cfg.get("stream_start_retry_seconds", 2.0)))


def stream_response_for_live_channel(
    source_key: str, channel_id: str
) -> StreamingResponse:
    cfg = load_config()
    source_cfg = {
        source["key"]: source for source in normalized_sources(cfg)
    }.get(source_key)
    if source_cfg is None:
        raise HTTPException(status_code=404, detail="Unbekannte Quelle")
    channel = live_channel_for(source_key, channel_id)

    try:
        decryption_keys = ""
        if channel.get("license_url"):
            decryption_keys = fetch_decryption_keys(
                str(channel["license_url"]), source_cfg
            )
        validate_manifest_url(str(channel["url"]), source_cfg)
        command = ffmpeg_command(
            str(channel["url"]),
            source_cfg,
            decryption_keys=decryption_keys,
            event_input_args=[
                str(value) for value in channel.get("ffmpeg_input_args") or []
            ],
        )
        LOG.info(
            "Starte ffmpeg für Live-TV-Kanal %s/%s: %s",
            source_key,
            channel_id,
            channel.get("name"),
        )
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=None,
            bufsize=0,
        )
    except (requests.RequestException, RuntimeError, OSError) as exc:
        LOG.exception(
            "Streamstart für Live-TV-Kanal %s/%s fehlgeschlagen",
            source_key,
            channel_id,
        )
        raise HTTPException(
            status_code=502, detail=f"Streamstart fehlgeschlagen: {exc}"
        ) from exc

    def stream() -> Iterator[bytes]:
        bytes_sent = 0
        try:
            if process.stdout is None:
                return
            while True:
                chunk = process.stdout.read(128 * 1024)
                if not chunk:
                    break
                bytes_sent += len(chunk)
                yield chunk
        finally:
            stop_process(process)
            LOG.info(
                "ffmpeg für Live-TV-Kanal %s/%s beendet (Exit %s, %d Bytes)",
                source_key,
                channel_id,
                process.poll(),
                bytes_sent,
            )

    return StreamingResponse(
        stream(),
        media_type="video/mp2t",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )


def refresh_after_stream_failure(source_key: str, slot_id: int, cfg: dict[str, Any]) -> None:
    if not bool(cfg.get("stream_refresh_on_failure", True)):
        return
    try:
        LOG.info(
            "Aktualisiere Quellen nach Streamfehler fuer Quelle %s, Slot %d",
            source_key,
            slot_id,
        )
        refresh_state()
    except Exception:
        LOG.exception(
            "Aktualisierung nach Streamfehler fuer Quelle %s, Slot %d fehlgeschlagen",
            source_key,
            slot_id,
        )


def stream_response_for_slot(
    source_key: str,
    slot_id: int,
    ffmpeg_overrides: Optional[dict[str, list[str]]] = None,
) -> StreamingResponse:
    cfg = load_config()
    overrides = ffmpeg_overrides or ffmpeg_overrides_from_values()
    source_configs = {source["key"]: source for source in normalized_sources(cfg)}
    source_cfg = source_configs.get(source_key)
    if source_cfg is None or not 1 <= slot_id <= int(source_cfg["slots"]):
        raise HTTPException(status_code=404, detail="Unbekannter Slot")

    event, mode, switch_at = playback_for_slot(
        slot_id, get_state(), cfg, source_key
    )
    if event is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "In diesem Slot ist weder ein Live-, Vorschau- "
                "noch ein Replay-Event verfügbar"
            ),
        )

    def start_process(
        selected_event: dict[str, Any], selected_mode: str
    ) -> subprocess.Popen[bytes]:
        decryption_keys = ""
        if selected_event.get("license_url"):
            decryption_keys = fetch_decryption_keys(
                str(selected_event["license_url"]), source_cfg
            )
        validate_manifest_url(str(selected_event["url"]), source_cfg)
        event_input_args = [
            str(value) for value in selected_event.get("ffmpeg_input_args") or []
        ]
        command = ffmpeg_command(
            str(selected_event["url"]),
            source_cfg,
            decryption_keys=decryption_keys,
            event_input_args=event_input_args,
            override_drm_input_args=overrides["drm_args"],
            override_input_args=overrides["input_args"],
            override_output_args=overrides["output_args"],
        )
        if any(overrides.values()):
            LOG.info(
                "ffmpeg-Testparameter fuer Quelle %s, Slot %d aktiv: "
                "decrypt=%d input=%d output=%d",
                source_key,
                slot_id,
                len(overrides["drm_args"]),
                len(overrides["input_args"]),
                len(overrides["output_args"]),
            )
        if decryption_keys:
            LOG.info(
                "Provider-Stream fuer Quelle %s, Slot %d: %d Datensatz/Datensaetze, %d Provider-Input-Arg(s)",
                source_key,
                slot_id,
                decryption_key_count(decryption_keys),
                len(event_input_args),
            )
        LOG.info(
            "Starte ffmpeg für Quelle %s, Slot %d (%s): %s",
            source_key,
            slot_id,
            selected_mode,
            selected_event["title"],
        )
        return subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=None,
            bufsize=0,
        )

    def start_process_with_retries(
        selected_event: dict[str, Any],
        selected_mode: str,
        selected_switch_at: Optional[datetime],
    ) -> tuple[subprocess.Popen[bytes], dict[str, Any], str, Optional[datetime]]:
        attempts = stream_start_attempts(source_cfg)
        retry_seconds = stream_start_retry_seconds(source_cfg)
        for attempt in range(1, attempts + 1):
            try:
                return (
                    start_process(selected_event, selected_mode),
                    selected_event,
                    selected_mode,
                    selected_switch_at,
                )
            except (requests.RequestException, RuntimeError, OSError) as exc:
                if attempt >= attempts:
                    raise
                LOG.warning(
                    "Streamstart fuer Quelle %s, Slot %d fehlgeschlagen "
                    "(%s, Versuch %d/%d): %s",
                    source_key,
                    slot_id,
                    selected_mode,
                    attempt,
                    attempts,
                    exc,
                )
                refresh_after_stream_failure(source_key, slot_id, source_cfg)
                if retry_seconds:
                    time.sleep(retry_seconds)
                latest_event, latest_mode, latest_switch_at = playback_for_slot(
                    slot_id, get_state(), cfg, source_key
                )
                if latest_event is None:
                    raise RuntimeError(
                        "Nach Streamfehler ist kein aktives Event mehr verfuegbar"
                    ) from exc
                selected_event = latest_event
                selected_mode = str(latest_mode)
                selected_switch_at = latest_switch_at
        raise RuntimeError("Streamstart fehlgeschlagen")

    try:
        process, event, mode, switch_at = start_process_with_retries(
            event, str(mode), switch_at
        )
    except (requests.RequestException, RuntimeError, OSError) as exc:
        LOG.exception(
            "Streamstart für Quelle %s, Slot %d fehlgeschlagen",
            source_key,
            slot_id,
        )
        raise HTTPException(
            status_code=502, detail=f"Streamstart fehlgeschlagen: {exc}"
        ) from exc

    def stream() -> Iterator[bytes]:
        active_process = process
        active_event = event
        active_mode = str(mode)
        active_switch_at = switch_at
        no_data_restarts = 0
        try:
            while True:
                bytes_sent = 0
                cutoff_timer: Optional[threading.Timer] = None
                monitor_stop = threading.Event()
                expected_key = str(active_event.get("stable_key", ""))

                def monitor_assignment(
                    monitored_process: subprocess.Popen[bytes],
                    monitored_mode: str,
                    monitored_key: str,
                ) -> None:
                    while not monitor_stop.wait(0.5):
                        desired, desired_mode, _ = playback_for_slot(
                            slot_id, get_state(), cfg, source_key
                        )
                        desired_key = (
                            str(desired.get("stable_key", "")) if desired else ""
                        )
                        if (
                            desired is None
                            or str(desired_mode) != monitored_mode
                            or desired_key != monitored_key
                        ):
                            stop_process(monitored_process)
                            return

                monitor_thread = threading.Thread(
                    target=monitor_assignment,
                    args=(active_process, active_mode, expected_key),
                    name=f"slot-monitor-{source_key}-{slot_id}",
                    daemon=True,
                )
                monitor_thread.start()
                if active_mode == "replay" and active_switch_at is not None:
                    delay = max(
                        0.0,
                        (
                            active_switch_at
                            - datetime.now(zone_for(cfg))
                        ).total_seconds(),
                    )
                    cutoff_timer = threading.Timer(
                        delay, stop_process, args=(active_process,)
                    )
                    cutoff_timer.daemon = True
                    cutoff_timer.start()
                    LOG.info(
                        "Replay auf Quelle %s, Slot %d endet für Live-Umschaltung um %s",
                        source_key,
                        slot_id,
                        active_switch_at.isoformat(),
                    )
                try:
                    if active_process.stdout is None:
                        return
                    while True:
                        chunk = active_process.stdout.read(128 * 1024)
                        if not chunk:
                            break
                        bytes_sent += len(chunk)
                        yield chunk
                finally:
                    monitor_stop.set()
                    if threading.current_thread() is not monitor_thread:
                        monitor_thread.join(timeout=1)
                    if cutoff_timer:
                        cutoff_timer.cancel()
                    stop_process(active_process)
                    LOG.info(
                        "ffmpeg für Quelle %s, Slot %d beendet "
                        "(%s, Exit %s, %d Bytes)",
                        source_key,
                        slot_id,
                        active_mode,
                        active_process.poll(),
                        bytes_sent,
                    )

                exit_code = active_process.poll()
                if bytes_sent == 0 and exit_code not in (None, -9):
                    max_no_data_retries = stream_no_data_retries(source_cfg)
                    if no_data_restarts < max_no_data_retries:
                        no_data_restarts += 1
                        LOG.warning(
                            "ffmpeg fuer Quelle %s, Slot %d lieferte keine Daten "
                            "(%s, Exit %s); Neustart %d/%d",
                            source_key,
                            slot_id,
                            active_mode,
                            exit_code,
                            no_data_restarts,
                            max_no_data_retries,
                        )
                        refresh_after_stream_failure(source_key, slot_id, source_cfg)
                        retry_seconds = stream_start_retry_seconds(source_cfg)
                        if retry_seconds:
                            time.sleep(retry_seconds)
                        next_event, next_mode, next_switch_at = playback_for_slot(
                            slot_id, get_state(), cfg, source_key
                        )
                        if next_event is None:
                            return
                        try:
                            (
                                active_process,
                                active_event,
                                active_mode,
                                active_switch_at,
                            ) = start_process_with_retries(
                                next_event, str(next_mode), next_switch_at
                            )
                        except (requests.RequestException, RuntimeError, OSError):
                            LOG.exception(
                                "Neustart nach leerem ffmpeg-Stream fuer Quelle %s, "
                                "Slot %d fehlgeschlagen",
                                source_key,
                                slot_id,
                            )
                            return
                        continue
                    LOG.warning(
                        "ffmpeg fuer Quelle %s, Slot %d lieferte keine Daten "
                        "(%s, Exit %s); keine weiteren Neustarts",
                        source_key,
                        slot_id,
                        active_mode,
                        exit_code,
                    )
                    return
                no_data_restarts = 0

                # Re-evaluate after EOF or a scheduled cutoff. A replay may be
                # restarted if it ended early; a newly active live event wins.
                time.sleep(0.2)
                next_event, next_mode, next_switch_at = playback_for_slot(
                    slot_id, get_state(), cfg, source_key
                )
                if next_event is None:
                    return
                try:
                    (
                        active_process,
                        active_event,
                        active_mode,
                        active_switch_at,
                    ) = start_process_with_retries(
                        next_event, str(next_mode), next_switch_at
                    )
                except (requests.RequestException, RuntimeError, OSError):
                    LOG.exception(
                        "Folgestream für Quelle %s, Slot %d konnte nicht gestartet werden",
                        source_key,
                        slot_id,
                    )
                    return
        finally:
            stop_process(active_process)

    return StreamingResponse(
        stream(),
        media_type="video/mp2t",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )


def ffmpeg_debug_response(
    source_key: str,
    slot_id: int,
    drm_args: Any = "",
    input_args: Any = "",
    output_args: Any = "",
) -> HTMLResponse:
    cfg = load_config()
    source_configs = {source["key"]: source for source in normalized_sources(cfg)}
    source_cfg = source_configs.get(source_key)
    if source_cfg is None or not 1 <= slot_id <= int(source_cfg["slots"]):
        raise HTTPException(status_code=404, detail="Unbekannter Slot")

    event, mode, _ = playback_for_slot(slot_id, get_state(), cfg, source_key)
    if event is None:
        raise HTTPException(
            status_code=404,
            detail="In diesem Slot ist gerade kein testbarer Stream aktiv",
        )

    overrides = ffmpeg_overrides_from_values(drm_args, input_args, output_args)
    decryption_keys = ""
    license_status = "Keine zusätzlichen Provider-Daten für dieses Event."
    if event.get("license_url"):
        try:
            decryption_keys = fetch_decryption_keys(str(event["license_url"]), source_cfg)
            license_status = (
                f"{decryption_key_count(decryption_keys)} Provider-Datensatz/Datensaetze geladen "
                "und in der Anzeige maskiert."
            )
        except (requests.RequestException, RuntimeError) as exc:
            decryption_keys = "0" * 32 + "=" + "0" * 32
            license_status = (
                "Provider-Daten konnten fuer die Vorschau nicht geladen werden: "
                + html.escape(str(exc))
            )

    event_input_args = [str(value) for value in event.get("ffmpeg_input_args") or []]
    stream_url = str(event["url"])
    command = ffmpeg_command(
        stream_url,
        source_cfg,
        decryption_keys=decryption_keys,
        event_input_args=event_input_args,
        override_drm_input_args=overrides["drm_args"],
        override_input_args=overrides["input_args"],
        override_output_args=overrides["output_args"],
    )
    command_text = shlex.join(mask_ffmpeg_command(command))
    override_query = ffmpeg_override_query_from_values(drm_args, input_args, output_args)
    slot_prefix = (
        f"/slot/{source_key}/{slot_id}"
        if source_key != "default"
        else f"/slot/{slot_id}"
    )
    m3u_path = f"{slot_prefix}.m3u" + (f"?{override_query}" if override_query else "")
    ts_path = f"{slot_prefix}.ts" + (f"?{override_query}" if override_query else "")

    def args_as_text(values: list[str]) -> str:
        return shlex.join([str(value) for value in values])

    def args_or_dash(values: list[str]) -> str:
        return args_as_text(values) if values else "-"

    effective_reconnect_args = (
        config_arg_list(source_cfg, "ffmpeg_reconnect_args")
        if source_cfg.get("ffmpeg_reconnect")
        and stream_url.lower().startswith(("http://", "https://"))
        else []
    )
    mpegts_flags = (
        str(source_cfg.get("ffmpeg_drm_mpegts_flags", ""))
        if decryption_keys
        else str(source_cfg.get("ffmpeg_mpegts_flags", ""))
    )
    effective_mpegts_args: list[str] = []
    if mpegts_flags:
        effective_mpegts_args.extend(["-mpegts_flags", mpegts_flags])
    effective_mpegts_args.extend(config_arg_list(source_cfg, "ffmpeg_mpegts_output_args"))
    effective_mpegts_args.append("pipe:1")

    body = f"""<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ffmpeg Slot {slot_id}</title>
  {UI_THEME_INIT}
  <style>
    {UI_THEME_CSS}
    body {{ font: 16px system-ui, sans-serif; max-width: 1100px; margin: 2rem auto; padding: 0 1rem; }}
    label {{ display: block; font-weight: 700; margin: 1rem 0 .35rem; }}
    textarea {{ width: 100%; min-height: 4.5rem; border: 1px solid var(--border); background: var(--field); color: var(--text); font: 13px ui-monospace, SFMono-Regular, Consolas, monospace; }}
    button, .button {{ display: inline-block; border: 1px solid var(--link); background: var(--link); color: var(--button-text); padding: .55rem .8rem; text-decoration: none; font-weight: 700; margin-right: .45rem; }}
    .button.secondary {{ background: var(--field); color: var(--link); }}
    code, pre {{ background: var(--surface); }}
    code {{ padding: .15rem .35rem; }}
    pre {{ overflow: auto; padding: .8rem; white-space: pre-wrap; }}
    .meta {{ color: var(--muted); }}
    .warn {{ color: var(--error); }}
  </style>
</head>
<body>
  {UI_THEME_TOGGLE}
  <p><a href="/">Zurueck zur Uebersicht</a></p>
  <h1>ffmpeg Slot {html.escape(str(slot_id))}</h1>
  <p><strong>{html.escape(str(event.get("title") or "Event"))}</strong><br>
     <span class="meta">Quelle: {html.escape(str(source_cfg.get("source_name") or source_key))} · Modus: {html.escape(str(mode))}</span></p>
  <p>{license_status}</p>
  <p>Stream-URL: <code>{html.escape(str(event.get("url") or ""))}</code></p>

  <h2>Temporare Testparameter</h2>
  <form method="get">
    <label for="drm_args">Zusätzliche Provider-Parameter</label>
    <textarea id="drm_args" name="drm_args">{html.escape(str(drm_args or ""))}</textarea>
    <label for="input_args">Zusatz vor <code>-i</code></label>
    <textarea id="input_args" name="input_args">{html.escape(str(input_args or ""))}</textarea>
    <label for="output_args">Zusatz nach <code>-c copy</code></label>
    <textarea id="output_args" name="output_args">{html.escape(str(output_args or ""))}</textarea>
    <p>
      <button type="submit">Vorschau aktualisieren</button>
      <a class="button secondary" href="{html.escape(m3u_path, quote=True)}">Teststream als M3U</a>
      <a class="button secondary" href="{html.escape(ts_path, quote=True)}">Direkter TS-Link</a>
    </p>
  </form>

  <h2>Aktueller Befehl</h2>
  <pre>{html.escape(command_text)}</pre>

  <h2>Aktive Argumente</h2>
  <p class="meta">Provider-Input: <code>{html.escape(args_or_dash(event_input_args))}</code></p>
  <p class="meta">Config Base: <code>{html.escape(args_or_dash(config_arg_list(source_cfg, "ffmpeg_base_args")))}</code></p>
  <p class="meta">Config Reconnect: <code>{html.escape(args_or_dash(effective_reconnect_args))}</code></p>
  <p class="meta">Config Provider Input: <code>{html.escape(args_or_dash(config_arg_list(source_cfg, "ffmpeg_drm_input_args")))}</code></p>
  <p class="meta">Config Extra Input: <code>{html.escape(args_or_dash(config_arg_list(source_cfg, "ffmpeg_extra_input_args")))}</code></p>
  <p class="meta">Config Mapping: <code>{html.escape(args_or_dash(config_arg_list(source_cfg, "ffmpeg_map_args")))}</code></p>
  <p class="meta">Config Codec: <code>{html.escape(args_or_dash(config_arg_list(source_cfg, "ffmpeg_codec_args")))}</code></p>
  <p class="meta">Config Provider Output: <code>{html.escape(args_or_dash(config_arg_list(source_cfg, "ffmpeg_drm_output_args")))}</code></p>
  <p class="meta">Config Extra Output: <code>{html.escape(args_or_dash(config_arg_list(source_cfg, "ffmpeg_extra_output_args")))}</code></p>
  <p class="meta">Config MPEG-TS: <code>{html.escape(args_or_dash(effective_mpegts_args))}</code></p>
  <p class="warn">Die Felder oben sind nur fuer diesen Testaufruf aktiv und werden nicht in der YAML gespeichert.</p>
</body>
</html>"""
    return HTMLResponse(body, headers={"Cache-Control": "no-store"})


@app.get("/slot/{source_key}/{slot_id}/ffmpeg", response_class=HTMLResponse)
def ffmpeg_source_debug(
    source_key: str,
    slot_id: int,
    drm_args: str = "",
    input_args: str = "",
    output_args: str = "",
) -> HTMLResponse:
    try:
        normalized_key = safe_source_key(source_key)
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail="Unbekannte Quelle") from exc
    return ffmpeg_debug_response(normalized_key, slot_id, drm_args, input_args, output_args)


@app.get("/live/{source_key}/{channel_id}.ts")
def stream_live_channel(source_key: str, channel_id: str) -> StreamingResponse:
    try:
        normalized_key = safe_source_key(source_key)
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail="Unbekannte Quelle") from exc
    return stream_response_for_live_channel(normalized_key, channel_id)


@app.get("/slot/{source_key}/{slot_id}.ts")
def stream_source_slot(
    source_key: str, slot_id: int, request: Request = None
) -> StreamingResponse:
    try:
        normalized_key = safe_source_key(source_key)
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail="Unbekannte Quelle") from exc
    return stream_response_for_slot(
        normalized_key, slot_id, ffmpeg_overrides_from_request(request)
    )


@app.get("/slot/{source_key}/{slot_id}.m3u", response_class=PlainTextResponse)
def slot_source_playlist(
    source_key: str, slot_id: int, request: Request = None
) -> PlainTextResponse:
    try:
        normalized_key = safe_source_key(source_key)
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail="Unbekannte Quelle") from exc
    return slot_playlist_response(normalized_key, slot_id, request)


@app.get("/slot/{source_key}/{slot_id}/logo")
def source_slot_logo(source_key: str, slot_id: int) -> Response:
    try:
        normalized_key = safe_source_key(source_key)
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail="Unbekannte Quelle") from exc
    return slot_logo_response(normalized_key, slot_id)


@app.get("/slot/{slot_id}/ffmpeg", response_class=HTMLResponse)
def ffmpeg_debug(
    slot_id: int,
    drm_args: str = "",
    input_args: str = "",
    output_args: str = "",
) -> HTMLResponse:
    """Legacy single-source ffmpeg debug route."""
    cfg = load_config()
    sources = normalized_sources(cfg)
    if len(sources) != 1 or not sources[0].get("_legacy_route"):
        raise HTTPException(
            status_code=404,
            detail="Bei mehreren Quellen die URL /slot/{quelle}/{id}/ffmpeg verwenden",
        )
    return ffmpeg_debug_response(sources[0]["key"], slot_id, drm_args, input_args, output_args)


@app.get("/slot/{slot_id}.ts")
def stream_slot(
    slot_id: int, request: Request = None
) -> StreamingResponse:
    """Legacy single-source route kept for existing TVHeadend networks."""
    cfg = load_config()
    sources = normalized_sources(cfg)
    if len(sources) != 1 or not sources[0].get("_legacy_route"):
        raise HTTPException(
            status_code=404,
            detail="Bei mehreren Quellen die URL /slot/{quelle}/{id}.ts verwenden",
        )
    return stream_response_for_slot(
        sources[0]["key"], slot_id, ffmpeg_overrides_from_request(request)
    )


@app.get("/slot/{slot_id}.m3u", response_class=PlainTextResponse)
def slot_playlist(
    slot_id: int, request: Request = None
) -> PlainTextResponse:
    """Legacy single-source M3U route for easier playback in external players."""
    cfg = load_config()
    sources = normalized_sources(cfg)
    if len(sources) != 1 or not sources[0].get("_legacy_route"):
        raise HTTPException(
            status_code=404,
            detail="Bei mehreren Quellen die URL /slot/{quelle}/{id}.m3u verwenden",
        )
    return slot_playlist_response(sources[0]["key"], slot_id, request)


@app.get("/slot/{slot_id}/logo")
def slot_logo(slot_id: int) -> Response:
    """Legacy single-source logo route."""
    cfg = load_config()
    sources = normalized_sources(cfg)
    if len(sources) != 1 or not sources[0].get("_legacy_route"):
        raise HTTPException(
            status_code=404,
            detail="Bei mehreren Quellen die URL /slot/{quelle}/{id}/logo verwenden",
        )
    return slot_logo_response(sources[0]["key"], slot_id)
