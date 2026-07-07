# TR-EventMux

Stable TVHeadend event slots for dynamic Telerising M3U playlists.

TR-EventMux liest eine oder mehrere dynamische Telerising-M3U-Quellen, erkennt
zeitgesteuerte Event-Kanäle und stellt TVHeadend dauerhaft stabile lokale
Slot-URLs bereit:

```text
http://<HOST>:8787/slot/1.ts
http://<HOST>:8787/slot/2.ts
...
```

Die echte Stream-URL darf wechseln. TVHeadend sieht weiterhin dieselben Kanäle,
dieselben M3U-IDs und dieselben XMLTV-Channel-IDs.

## Dokumentation

- [QUICKSTART.md](QUICKSTART.md): einfacher Einstieg mit Docker und TVHeadend
- [INSTALL.md](INSTALL.md): vollständige Installation, Updates und Fehlersuche
- [config.yaml.example](config.yaml.example): dokumentierte Beispielkonfiguration

## Features

- stabile M3U- und XMLTV-Ausgabe für TVHeadend
- mitgelieferte transparente Senderlogos für Event-Slots
- mehrere unabhängige Telerising-M3U-Quellen
- feste Slots für parallel laufende Events
- Slot-Erhalt über Neustarts per `/data/state.json`
- Parser für `[YY/MM/DD HH:MM]` und `[MM/DD HH:MM]`
- Live-Marker in eckigen oder runden Klammern und optionale Provider-Countdowns
- Unterstützung für `#KODIPROP`-Blöcke zwischen `#EXTINF` und Stream-URL
- sichere Behandlung von `pipe://bash -c`-Einträgen ohne Shell-Ausführung
- ffmpeg-MPEG-TS-Remux mit `-c copy`
- optionale Vorschauwiedergabe vor dem EPG-Start
- laufende Streams bleiben aktiv, solange der Provider sie weiter anbietet
- Docker-Build mit statischem `ffmpeg-multikey`
- HTML-Statusseite, JSON-Status und manuelles Refresh
- getrennte Live-TV-Ansicht mit Suche und bedarfsgestarteter Wiedergabe
- interner MPEG-TS-Webplayer mit dauerhaft umschaltbarem VLC-Modus
- heller und dunkler UI-Modus mit gespeicherter Browser-Auswahl

## Voraussetzungen

- Docker und Docker Compose
- eine erreichbare Telerising-M3U-Quelle
- TVHeadend mit einem IPTV Automatic Network

Alternativ wird Debian/Ubuntu ohne Docker über `install.sh` unterstützt.

Der Docker-Build benötigt Zugriff auf GitHub, um ein passendes statisches
`ffmpeg-multikey`-Archiv aus `DEvmIb/ffmpeg-multikey` zu laden.

## Docker Compose Installation

Einsteiger beginnen am besten mit [QUICKSTART.md](QUICKSTART.md). Die
vollständige Anleitung für Installation, Updates, TVHeadend, XMLTV, Backups und
systemd steht in [INSTALL.md](INSTALL.md).

Schnellstart:

```bash
cp config.yaml.example config.yaml
nano config.yaml
mkdir -p data
docker compose up -d --build
```

Danach prüfen:

```bash
curl http://127.0.0.1:8787/status.json
curl http://127.0.0.1:8787/playlist.m3u
curl http://127.0.0.1:8787/xmltv.xml
docker compose logs -f tr-eventmux
```

Die lokale Compose-Datei nutzt:

```yaml
services:
  tr-eventmux:
    image: tr-eventmux:latest
    container_name: tr-eventmux
```

## Installation ohne Docker

Für Debian/Ubuntu steht `install.sh` bereit. Nach dem Klonen installiert das
Skript die Anwendung, Python-Abhängigkeiten, die benötigte ffmpeg-Datei sowie
die systemd-Dienste und startet sie:

```bash
git clone https://github.com/ynosnet/tr-eventmux.git
cd tr-eventmux
sudo ./install.sh
sudo nano /opt/tr-eventmux/config.yaml
sudo systemctl restart tr-eventmux.service
```

Die vollständige Beschreibung einschließlich Updates und Statusprüfung steht
in [INSTALL.md](INSTALL.md#installation-ohne-docker-mit-systemd).

## Konfiguration

Erstelle zuerst eine lokale Konfiguration:

```bash
cp config.yaml.example config.yaml
```

Passe danach mindestens diese Werte an:

```yaml
host_for_playlist: "tr-eventmux"
sources:
  - key: "source1"
    source_m3u: "http://telerising.example.invalid:5000/api/source1/file/channels.m3u"
    slots: 5
```

`host_for_playlist` oder `public_base_url` muss aus Sicht des
TVHeadend-Containers erreichbar sein. Wenn die M3U-Quelle auf dem Docker-Host
läuft, verwende in `config.yaml` zum Beispiel `host.docker.internal` statt
`127.0.0.1`.

Wichtige Schlüssel:

| Schlüssel | Bedeutung |
|---|---|
| `source_m3u` | URL der Telerising-M3U-Quelle |
| `sources` | Liste unabhängiger M3U-Quellen |
| `host_for_playlist` | Hostname/IP für die generierte TVHeadend-Playlist |
| `public_base_url` | vollständige öffentliche Basis-URL, alternativ zu Host/Port |
| `slots` | Anzahl fester Slot-Kanäle pro Quelle |
| `channel_number_start` | erste Kanalnummer in der Mini-M3U |
| `channel_name_template` | Anzeigename, unterstützt `{source}` und `{slot}` |
| `channel_id_template` | stabile M3U-/XMLTV-ID |
| `logo_key` | optionaler Logo-Schlüssel unabhängig von der Kanalnummer; ohne Angabe `provider1`, `provider2`, ... nach Quellen-Reihenfolge |
| `event_group_filter` | nur diese Gruppen, leer bedeutet alle Gruppen |
| `date_format` | `auto`, `yy/mm/dd`, `mm/dd` oder `dd/mm` |
| `title_split` | Titel-/Beschreibungstrennung: `first_dash`, `last_dash` oder `none` |
| `live_countdown_enabled` | deutet Live-Countdowns und Provider-Datumshinweise als zukünftige Startzeit |
| `allow_upcoming_stream` | erlaubt den nächsten eingeplanten Stream bereits vor seiner Startzeit |
| `default_duration_minutes` | Dauer ohne bekannte Endzeit |
| `epg_max_duration_minutes` | maximale Programmlänge nur in XMLTV und Mini-EPG; beeinflusst die Stream-Laufzeit nicht |
| `lookback_minutes` / `lookahead_hours` | Zeitfenster für Planung und Anzeige |
| `past_events_display_limit` | Anzahl zuletzt beendeter Events auf der Statusseite |
| `idle_replay_enabled` | leere Slots dürfen zuletzt beendete Events erneut anbieten |
| `refresh_seconds` | Abrufintervall der Provider-M3U; Standard sind 300 Sekunden |
| `tune_early_minutes` | Stream vor offiziellem Beginn freigeben |
| `keep_stream_while_listed` | laufenden Stream über die angenommene Endzeit hinaus aktiv halten, solange der Event-Eintrag weiter vorhanden ist |
| `xmltv_output_path` | lokale XMLTV-Datei; leer deaktiviert die Dateiausgabe |
| `xmltv_socket_enabled` | aktiviert den optionalen XMLTV-Versand an einen Unix-Socket |
| `xmltv_socket_path` | optionaler Unix-Socket, an den XMLTV nach jedem Refresh gesendet wird |
| `xmltv_socket_timeout_seconds` | Timeout für den XMLTV-Socket-Versand |
| `xmltv_language` | Sprache für XMLTV-Kategorien |
| `priority_keywords` | Treffer bei Slot-Knappheit bevorzugen |
| `exclude_keywords` | passende Events ignorieren |
| `stream_engine` | `ffmpeg` oder `streamlink_drm`; kann pro Quelle überschrieben werden |
| `streamlink_drm` | Pfad zum Streamlink-DRM-Befehl, im Docker-Image `/usr/local/bin/streamlink-drm` |
| `streamlink_stream` | Stream-Auswahl für Streamlink, meist `best` |
| `streamlink_extra_args` | zusätzliche Streamlink-Argumente vor URL und Stream-Auswahl |
| `streamlink_key_mode` | Key-Auswahl für Streamlink-DRM: `all`, `first`, `second` oder `reverse` |
| `streamlink_reverse_keys` | dreht bei Streamlink-DRM die Provider-Key-Reihenfolge, wenn nur eine Spur sauber entschlüsselt wird |
| `ffmpeg` / `ffmpeg_*` | ffmpeg-Pfad und erweiterte Eingabe-, Mapping-, Codec- und MPEG-TS-Argumente |
| `ffmpeg_extra_input_args` | zusätzliche ffmpeg-Argumente vor `-i` |
| `ffmpeg_extra_output_args` | zusätzliche ffmpeg-Argumente vor der MPEG-TS-Ausgabe |
| `manifest_probe_*` | kurze MPD-Prüfung vor dem Streamstart |
| `stream_start_attempts` / `stream_no_data_retries` | Wiederholungen bei leeren Manifesten, 401 oder ausbleibenden Daten |
| `request_timeout_seconds` / `license_timeout_seconds` | HTTP-Timeouts für Quellen und Lizenzabfragen |
| `verify_tls` | TLS-Zertifikatsprüfung für HTTP-Abrufe |
| `source_user_agent` | optionaler User-Agent für den Quellenabruf |
| `live_logo_max_bytes` / `live_logo_block_private_hosts` | Grenzen und Schutzoptionen für den Live-TV-Logo-Proxy |

Kopiere keine echten Telerising-URLs, Zugangsdaten, Tokens oder privaten
Netzwerkdetails in öffentliche Issues, Beispiele, Tests oder Commits.

Slot-Logos werden nicht aus `channel_number_start` abgeleitet. Dadurch bleiben
sie stabil, wenn du Kanalnummern änderst. Pro Quelle kann optional `logo_key`
gesetzt werden; ohne Angabe nutzt TR-EventMux die Reihenfolge der Quellen
(`provider1`, `provider2`, ...).

### Single-Source und Multi-Source

Es gibt keinen separaten Modusschalter. Die Struktur der Konfiguration
entscheidet:

- Mit einer `sources:`-Liste läuft TR-EventMux im Multi-Source-Modus.
- Ohne `sources:`, aber mit `source_m3u` auf oberster Ebene, läuft der
  kompatible Single-Source-Modus.

Multi-Source wird für neue Installationen empfohlen – auch dann, wenn zunächst
nur ein Provider eingetragen wird:

```yaml
sources:
  - key: "provider1"
    source_m3u: "http://telerising.example.invalid/provider1.m3u"
    slots: 5
```

Die Slot-URLs enthalten dabei den Quellen-Key:

```text
/slot/provider1/1.ts
```

### Webplayer und VLC-Modus

Die Status- und Live-TV-Seiten starten Streams standardmäßig im eingebetteten
Webplayer. Der Schalter oben rechts wechselt dauerhaft zwischen:

- `Webplayer`: Wiedergabe direkt im Browser über das lokal mitgelieferte
  `mpegts.js`
- `VLC`: Öffnen der bisherigen Ein-Kanal-M3U im lokal installierten Player

Die Auswahl wird im Browser gespeichert. Der Webplayer greift auf dieselben
lokalen MPEG-TS-Endpunkte zu wie TVHeadend und VLC. Ob Bild und Ton im Browser
dekodiert werden können, hängt zusätzlich von den Codecs des Provider-Streams
und der Browserunterstützung ab. Für schnelle Diagnose bleibt der
`ffmpeg`-Debuglink unabhängig vom Wiedergabemodus sichtbar.

Der interne Player verwendet einen zusätzlichen Stabilitätspuffer und verzichtet
bewusst auf aggressives Nachspringen zur Live-Kante. Dadurch liegt seine
Wiedergabe etwas weiter hinter VLC, reagiert aber toleranter auf schwankende
MPEG-TS-Paketzustellung und Zeitstempel.

Der Single-Source-Modus verwendet stattdessen:

```yaml
source_m3u: "http://telerising.example.invalid/provider1.m3u"
slots: 5
channel_number_start: 901
channel_name_prefix: "Event"
event_group_filter: "Events"
```

Seine Slot-URLs bleiben ohne Quellen-Key:

```text
/slot/1.ts
```

Zum Wechsel die jeweils andere Struktur vollständig entfernen, den Dienst neu
starten und TVHeadend anschließend die Playlist neu einlesen lassen. Eine
`sources:`-Liste mit nur einem Eintrag bleibt technisch Multi-Source.

## TVHeadend Einrichtung

In TVHeadend unter `Configuration -> DVB Inputs -> Networks` ein
`IPTV Automatic Network` anlegen und diese URL verwenden:

```text
http://<HOST>:8787/playlist.m3u
```

Empfehlung für die Experteneinstellungen: `Service-ID` auf `1` setzen und
`Scan nach Erstellen` deaktivieren. Dadurch werden die Services direkt sichtbar
und müssen nicht zuerst einzeln gescannt werden.

Services einmalig auf Kanäle mappen, zum Beispiel:

```text
901 Event 1
902 Event 2
903 Event 3
904 Event 4
905 Event 5
```

XMLTV steht hier bereit:

```text
http://<HOST>:8787/xmltv.xml
```

Zusätzlich wird dieselbe XMLTV-Ausgabe nach jedem Refresh atomar nach
`xmltv_output_path` geschrieben. Mit der mitgelieferten Docker-Compose-Datei
liegt sie auf dem Host unter `./data/xmltv.xml`.

Optional kann TR-EventMux die XMLTV-Ausgabe nach jedem erfolgreichen Refresh
direkt an einen Unix-Socket senden. Für TVHeadend kann dazu dessen XMLTV-Socket
eingetragen werden:

```yaml
xmltv_socket_enabled: true
xmltv_socket_path: "/opt/containers/tvheadend/data/epggrab/xmltv.sock"
```

Ohne TVHeadend-Socket bleibt `xmltv_socket_enabled: false`. Der Pfad muss aus
Sicht des TR-EventMux-Prozesses erreichbar sein. Läuft TR-EventMux in Docker,
muss der Socket oder dessen Verzeichnis entsprechend in den Container gemountet
werden.

Der Provider-Abruf läuft weiterhin im konfigurierten `refresh_seconds`-Intervall.
Die XMLTV-Datei wird dabei nur ersetzt, wenn sich ihr erzeugter Inhalt geändert
hat; bei unverändertem EPG bleibt auch der Datei-Zeitstempel unverändert.
Ein aktivierter `xmltv_socket_path` wird unabhängig davon bei jedem erfolgreichen
Refresh mit der aktuellen XMLTV-Ausgabe beliefert.

Bei Quellen mit `live_countdown_enabled: true` werden Live-Marker, relative
Countdowns und erkannte Datumshinweise im Titel als zukünftige Startzeit
gedeutet. Solange `tvg-id` und bereinigter Titel gleich bleiben, wird diese
gespeicherte Startzeit bei weiteren Refreshes beibehalten. So verschiebt ein
veralteter Provider-Text wie `Noch 14 Std. 3 Min.` das EPG nicht fortlaufend
nach hinten. Wenn ein Titel zusätzlich eine konkrete Angabe wie
`Fr., 03.07.26, 16:45 Uhr` enthält, wird diese feste Zeit bevorzugt und nicht
der relative Countdown.

XMLTV und Mini-EPG begrenzen die angezeigte Programmlänge standardmäßig auf vier
Stunden (`epg_max_duration_minutes: 240`). Die technische Slot-Belegung bleibt
davon unabhängig: Ein laufender Stream kann weiterlaufen, solange der Provider
das Event noch in seiner M3U führt. Nach einem erfolgreichen Abruf werden
verschwundene Events aus Planung, EPG, Replay-Auswahl und Slot-Speicher entfernt.
Schlägt der Abruf fehl, bleibt dagegen der letzte erfolgreiche Stand als
`stale` verfügbar.

M3U und XMLTV verwenden identische IDs wie `event.slot1`, `event.slot2` und
`event.slot3`. Diese Endpunkte bleiben absichtlich stabil.

## Endpunkte

| Endpunkt | Funktion |
|---|---|
| `/` | HTML-Statusseite mit Slot-Übersicht und Mini-EPG |
| `/live` | normale, in den Quellen gelistete Live-TV-Kanäle |
| `/live/playlist.m3u` | gemeinsame Live-TV-M3U mit lokalen Stream-URLs |
| `/live/{quelle}/{kanal}.ts` | bedarfsgestarteter MPEG-TS-Live-TV-Stream |
| `/live/{quelle}/{kanal}.m3u` | Ein-Kanal-Playlist für Live TV |
| `/live/{quelle}/{kanal}/logo` | lokaler Proxy für das Senderlogo |
| `/status.json` | vollständiger Status, Fehler und Slot-Zuordnung |
| `/logs` | internes App-Log aus dem Ringpuffer |
| `/refresh` | manueller Abruf und Neuaufbau |
| `/playlist.m3u` | stabile Mini-M3U für TVHeadend |
| `/xmltv.xml` | Pseudo-EPG im XMLTV-Format |
| `/slot/{id}.ts` | aktiver MPEG-TS-Stream eines Slots |
| `/slot/{id}.m3u` | Ein-Kanal-Playlist eines Slots |
| `/slot/{id}/logo` | transparentes PNG-Logo eines Slots |
| `/slot/{id}/ffmpeg` | ffmpeg-Kommando anzeigen und Testparameter setzen |
| `/slot/{quelle}/{id}.ts` | Stream eines Slots bei Multi-Source-Konfiguration |
| `/slot/{quelle}/{id}.m3u` | Ein-Kanal-Playlist bei Multi-Source-Konfiguration |
| `/slot/{quelle}/{id}/logo` | transparentes PNG-Logo des Event-Slots |
| `/slot/{quelle}/{id}/ffmpeg` | ffmpeg-Kommando anzeigen und Testparameter setzen |

## ffmpeg-multikey

Für vollständige Kompatibilität mit Telerising-Quellen lädt der Docker-Build
automatisch ein passendes statisches `ffmpeg-multikey`-Archiv und installiert
`ffmpeg` nach:

```text
/opt/ffmpeg/ffmpeg
```

Für reproduzierbare Builds können Release oder Asset-URL gesetzt werden:

```bash
FFMPEG_MULTIKEY_VERSION=n7.1 docker compose build
FFMPEG_MULTIKEY_ASSET_URL=https://github.com/DEvmIb/ffmpeg-multikey/releases/download/<tag>/<asset>.tar.xz docker compose build
```

## streamlink-drm

Der Docker-Build installiert zusaetzlich den ClearKey-faehigen
`streamlink-drm`-Fork und legt ihn im Container hier ab:

```text
/usr/local/bin/streamlink-drm
```

Das Paket stellt intern den normalen `streamlink`-Befehl bereit; der Build legt
`streamlink-drm` als stabilen Alias an. Repo und Ref koennen fuer Tests gepinnt
werden:

```bash
docker compose build --build-arg STREAMLINK_DRM_REF=<commit-oder-tag>
docker compose build --build-arg STREAMLINK_DRM_REPO=ImAleeexx/streamlink-drm --build-arg STREAMLINK_DRM_REF=master
```

Der verwendete Fork registriert seine ClearKey-Optionen mit einem einfachen
Bindestrich (`-decryption_key`, `-decryption_key_2`). TR-EventMux extrahiert
dafuer aus Provider-Antworten im Format `kid=key:kid=key` nur die jeweiligen
Key-Werte und uebergibt diese an Streamlink-DRM. Wenn nur Bild oder nur Ton
sauber entschluesselt wird, kann pro Quelle `streamlink_reverse_keys: true`
oder gezielter `streamlink_key_mode: "first"` beziehungsweise `"second"`
gesetzt werden. Bei `first` wird nur `-decryption_key` uebergeben; der Fork
verwendet diesen Key dann intern fuer beide ffmpeg-Eingaben.

## Troubleshooting

Eine ausführlichere Fehlerdiagnose steht in [INSTALL.md](INSTALL.md).

Wenn TVHeadend die Playlist nicht laden kann, prüfe `host_for_playlist` oder
`public_base_url` aus Sicht des TVHeadend-Containers.

Wenn keine Events erkannt werden, prüfe `event_group_filter`, `date_format` und
ob die Event-Titel ein unterstütztes Datumspräfix, einen Live-Marker oder einen
erkennbaren Datumshinweis enthalten.

Wenn ein Stream nicht startet, prüfe:

```bash
docker compose logs -f tr-eventmux
curl http://127.0.0.1:8787/status.json
```

Ein fehlgeschlagener Download beendet TR-EventMux nicht. Der letzte erfolgreiche
Plan bleibt verfügbar und wird im Status als `stale: true` markiert.

## Entwicklung

Lokale Prüfung:

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python -m unittest discover -s tests -v
.venv/Scripts/python -m py_compile app.py tests/test_app.py
```

Unter Linux/macOS entsprechend `.venv/bin/python` verwenden.

Vor Änderungen an Parser, Slot-Planung oder XMLTV-Ausgabe bitte Tests ergänzen.
`config.yaml` ist lokale Laufzeitkonfiguration und gehört nicht ins Repository.

## Danksagung und benötigte Projekte

Ein besonderer Dank geht an [DEvmIb](https://github.com/DEvmIb/) für die Arbeit
an den Projekten, auf denen die Provider-Unterstützung von TR-EventMux aufbaut:

- [DEvmIb/telerising-unofficial](https://github.com/DEvmIb/telerising-unofficial)
  stellt die dynamischen Provider-Playlists und Stream-Endpunkte bereit, die
  TR-EventMux verarbeitet.
- [DEvmIb/ffmpeg-multikey](https://github.com/DEvmIb/ffmpeg-multikey)
  stellt die für vollständige Provider-Kompatibilität benötigte ffmpeg-Variante
  bereit. Das passende statische Binary wird beim Docker-Build eingebunden.
- [ImAleeexx/streamlink-drm](https://github.com/ImAleeexx/streamlink-drm)
  stellt den Streamlink-Fork bereit, der optional als alternative Stream-Engine
  fuer entsprechende DASH-Streams genutzt werden kann.

Diese externen Projekte werden unabhängig entwickelt und unterliegen ihren
jeweiligen Lizenzen und Nutzungsbedingungen.

## Lizenz

Siehe [LICENSE](LICENSE).
