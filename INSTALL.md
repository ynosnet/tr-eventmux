# TR-EventMux installieren und aktualisieren

Diese Anleitung beschreibt die empfohlene Docker-Compose-Installation sowie
eine alternative Installation als systemd-Dienst.

Für eine kurze, vollständig lineare Ersteinrichtung siehe
[QUICKSTART.md](QUICKSTART.md).

## Empfohlene Installation mit Docker Compose

### Voraussetzungen

- Linux-Server, NAS oder Docker-Host im selben erreichbaren Netzwerk wie
  TVHeadend und die M3U-Quellen
- Git
- Docker Engine
- Docker Compose v2 (`docker compose`)
- Internetzugriff beim ersten Build

Der Build lädt ein passendes statisches `ffmpeg-multikey`-Archiv von GitHub.
Diese ffmpeg-Variante wird für vollständige Telerising-Kompatibilität benötigt.

### Repository herunterladen

```bash
git clone https://github.com/ynosnet/tr-eventmux.git
cd tr-eventmux
```

### Lokale Konfiguration erstellen

```bash
cp config.yaml.example config.yaml
mkdir -p data
nano config.yaml
```

`config.yaml` enthält private Quell-URLs und wird von Git ignoriert. Reale
URLs, Zugangsdaten und Tokens gehören ausschließlich in diese lokale Datei.

Mindestens anzupassen sind:

```yaml
host_for_playlist: "192.168.1.10"

sources:
  - key: "provider"
    name: "Provider"
    source_m3u: "http://telerising:5000/api/provider/file/channels.m3u"
    slots: 5
    channel_number_start: 901
    channel_name_template: "Provider Event {slot}"
    channel_id_template: "provider.event{slot}"
    group_title: "Provider Events"
    event_group_filter: "Events"
```

`host_for_playlist` oder `public_base_url` muss aus dem TVHeadend-Container
erreichbar sein. `127.0.0.1` bezeichnet in einem Container nur diesen Container.
Läuft TR-EventMux auf dem Docker-Host, verwende dessen LAN-Adresse oder
gegebenenfalls `host.docker.internal`.

### Quellenmodus auswählen

Ein eigener Schalter ist nicht erforderlich:

- `sources:` aktiviert den Multi-Source-Modus.
- `source_m3u` auf oberster Ebene ohne `sources:` aktiviert den kompatiblen
  Single-Source-Modus.

Für neue Installationen wird `sources:` empfohlen. Das gilt auch bei nur einem
Provider, weil später weitere Quellen ergänzt werden können, ohne das
Konfigurationsformat erneut umzustellen.

Multi-Source erzeugt URLs wie:

```text
/slot/provider1/1.ts
```

Single-Source erzeugt URLs wie:

```text
/slot/1.ts
```

Beim Wechsel des Modus ändern sich diese stabilen Slot-URLs. Deshalb danach den
Dienst neu starten und das IPTV-Netzwerk in TVHeadend neu einlesen lassen.

### Container bauen und starten

```bash
docker compose up -d --build
```

Der erste Build kann wegen des ffmpeg-Downloads einige Minuten dauern.

### Installation prüfen

```bash
docker compose ps
docker compose logs --tail=100 tr-eventmux
curl http://127.0.0.1:8787/status.json
curl http://127.0.0.1:8787/playlist.m3u
curl http://127.0.0.1:8787/xmltv.xml
```

Die persistenten Dateien befinden sich auf dem Host unter:

```text
./data/state.json
./data/xmltv.xml
```

`state.json` enthält unter anderem die gespeicherten Slot-Zuordnungen und
verankerte Provider-Countdowns. Die Datei sollte nicht regelmäßig gelöscht
werden.

## TVHeadend einrichten

### IPTV-Netzwerk

Unter `Configuration -> DVB Inputs -> Networks` ein `IPTV Automatic Network`
anlegen. Als M3U-URL verwenden:

```text
http://<TR-EVENTMUX-HOST>:8787/playlist.m3u
```

In den Experteneinstellungen des neuen Netzwerks empfiehlt sich:

- `Service-ID` auf `1` setzen, damit die Services direkt angelegt und sichtbar
  werden, ohne zunächst jeden Stream scannen zu müssen.
- `Scan nach Erstellen` deaktivieren, da der zusätzliche Scan bei der festen
  TR-EventMux-Playlist normalerweise nicht benötigt wird.

Anschließend die sichtbaren Services einmalig auf TVHeadend-Kanäle mappen.

### XMLTV-EPG

Die XMLTV-Ausgabe steht über HTTP bereit:

```text
http://<TR-EVENTMUX-HOST>:8787/xmltv.xml
```

Zusätzlich wird sie standardmäßig lokal gespeichert:

```text
./data/xmltv.xml
```

Der Pfad kann in `config.yaml` geändert oder mit einem leeren Wert deaktiviert
werden:

```yaml
xmltv_output_path: "/data/xmltv.xml"
```

M3U und XMLTV verwenden dieselben stabilen Channel-IDs. Der XMLTV-Grabber in
TVHeadend muss diese IDs den zuvor gemappten Kanälen zuordnen.

## Aktualisieren

Vor einem Update empfiehlt sich ein Backup von Konfiguration und Status:

```bash
cp config.yaml config.yaml.bak
cp -a data data.bak
```

Danach aktualisieren und neu bauen:

```bash
git pull --ff-only
docker compose up -d --build
docker compose logs --tail=100 tr-eventmux
```

`config.yaml` und `data/` werden von Git ignoriert und bleiben erhalten.

## Backup und Wiederherstellung

Für ein vollständiges Laufzeit-Backup genügen normalerweise:

```text
config.yaml
data/state.json
```

`data/xmltv.xml` kann jederzeit neu erzeugt werden.

Zur Wiederherstellung das Repository erneut klonen, die gesicherten Dateien
zurückkopieren und `docker compose up -d --build` ausführen.

## Installation ohne Docker mit systemd

Die systemd-Installation eignet sich für Debian/Ubuntu-Systeme. Benötigt
werden Git, Internetzugriff während der Installation, systemd und ein Benutzer
mit `sudo`-Rechten.

Projekt herunterladen und Installer starten:

```bash
git clone https://github.com/ynosnet/tr-eventmux.git
cd tr-eventmux
sudo ./install.sh
```

`install.sh` erledigt automatisch:

- benötigte Debian-/Ubuntu-Pakete installieren
- den Systembenutzer `tr-eventmux` anlegen
- die Anwendung nach `/opt/tr-eventmux` kopieren
- Python-Umgebung und Abhängigkeiten installieren
- das zur Rechnerarchitektur passende `ffmpeg-multikey`-Archiv von GitHub laden
- `ffmpeg` nach `/opt/ffmpeg/ffmpeg` installieren und prüfen
- Konfiguration und Datenverzeichnis anlegen
- Hauptdienst, Refresh-Service und Refresh-Timer installieren
- Hauptdienst und Timer aktivieren und starten

Danach die Konfiguration bearbeiten und den Dienst neu starten:

```bash
sudo nano /opt/tr-eventmux/config.yaml
sudo systemctl restart tr-eventmux.service
```

Der Installer legt bei einer frischen Installation
`/opt/tr-eventmux/config.yaml` und `/opt/tr-eventmux/data/` an. Bei erneuter
Ausführung bleiben eine vorhandene `config.yaml` und die Laufzeitdaten erhalten.
Die lokale XMLTV-Datei liegt standardmäßig unter
`/opt/tr-eventmux/data/xmltv.xml`.

Die installierte ffmpeg-Datei liegt unter `/opt/ffmpeg/ffmpeg`.

Status und Logs:

```bash
systemctl status tr-eventmux.service
journalctl -u tr-eventmux.service -f
systemctl status tr-eventmux-refresh.timer
```

### Bestimmtes ffmpeg-Release verwenden

Standardmäßig wird das neueste passende Release verwendet. Für eine festgelegte
Version:

```bash
sudo FFMPEG_MULTIKEY_VERSION=n7.1 ./install.sh
```

Alternativ kann eine vollständige Archiv-URL vorgegeben werden:

```bash
sudo FFMPEG_MULTIKEY_ASSET_URL="https://example.invalid/archive.tar.xz" ./install.sh
```

### systemd-Installation aktualisieren

Im lokalen Repository:

```bash
git pull --ff-only
sudo ./install.sh
```

Der Installer ersetzt Programmdateien und Services, lässt aber eine vorhandene
`/opt/tr-eventmux/config.yaml` sowie `/opt/tr-eventmux/data/` unverändert.

TR-EventMux besitzt bereits einen internen Refresh anhand von
`refresh_seconds`. Der mitgelieferte systemd-Timer löst zusätzlich alle zwei
Minuten `/refresh` aus und kann bei Bedarf deaktiviert werden:

```bash
sudo systemctl disable --now tr-eventmux-refresh.timer
```

## Fehlerdiagnose

### Keine Events erkannt

- `event_group_filter` kontrollieren
- `date_format` kontrollieren
- prüfen, ob die Quelle unterstützte Datums- oder Live-Marker liefert
- `/status.json` und `/logs` aufrufen

### Playlist oder Streams aus TVHeadend nicht erreichbar

- `host_for_playlist` beziehungsweise `public_base_url` prüfen
- keine Container-interne `127.0.0.1` als externe Adresse verwenden
- Port `8787` in Firewall und Docker freigeben

### Stream startet nicht

```bash
docker compose logs -f tr-eventmux
curl http://127.0.0.1:8787/status.json
```

Bei Provider-Streams zusätzlich prüfen, ob der Container tatsächlich
`/opt/ffmpeg/ffmpeg` verwendet und die Stream-Endpunkte erreichbar sind.

Bei einer systemd-Installation:

```bash
/opt/ffmpeg/ffmpeg -hide_banner -version
systemctl status tr-eventmux.service
journalctl -u tr-eventmux.service -n 200 --no-pager
```

### Konfiguration prüfen

```bash
docker compose config --quiet
docker compose restart tr-eventmux
curl http://127.0.0.1:8787/refresh
```

Ein fehlgeschlagener Quellenabruf beendet den Dienst nicht. Der letzte
erfolgreiche Plan bleibt als `stale` verfügbar.
