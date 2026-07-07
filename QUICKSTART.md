# TR-EventMux Schnellstart

Diese Anleitung führt ohne Umwege zu einer funktionierenden
Docker-Installation. Sie verwendet den empfohlenen Multi-Source-Modus.

Du möchtest ohne Docker installieren? Springe zur vollständigen
[systemd-Anleitung](INSTALL.md#installation-ohne-docker-mit-systemd).

## Was du brauchst

- einen Linux-Server, ein NAS oder einen anderen Rechner mit Docker
- Docker Compose v2 (`docker compose`)
- eine funktionierende Telerising-M3U-URL
- die IP-Adresse des Rechners, auf dem TR-EventMux laufen soll
- TVHeadend im selben erreichbaren Netzwerk

Beispiel für eine Server-IP:

```text
192.168.1.10
```

Verwende deine eigene IP-Adresse.

## 1. Projekt herunterladen

Öffne ein Terminal auf dem Docker-Rechner:

```bash
git clone https://github.com/ynosnet/tr-eventmux.git
cd tr-eventmux
```

## 2. Konfiguration vorbereiten

```bash
cp config.yaml.example config.yaml
mkdir -p data
nano config.yaml
```

Wenn `nano` nicht vorhanden ist, kannst du einen anderen Texteditor verwenden.

## 3. Erreichbare Adresse eintragen

Suche am Anfang der Datei:

```yaml
host_for_playlist: "tr-eventmux"
```

Ersetze den Wert durch die IP-Adresse des Docker-Rechners:

```yaml
host_for_playlist: "192.168.1.10"
```

Wichtig: Verwende nicht `127.0.0.1`, wenn TVHeadend in einem anderen Container
oder auf einem anderen Rechner läuft.

## 4. Einen Provider eintragen

Für den ersten Test genügt ein Eintrag unter `sources:`.

Lösche zunächst die weiteren Beispiel-Provider oder kommentiere sie aus. Der
Block kann anschließend so aussehen:

```yaml
sources:
  - key: "provider1"
    name: "Provider 1"
    source_m3u: "http://DEINE-TELERISING-ADRESSE/api/provider/file/channels.m3u"
    slots: 5
    channel_number_start: 901
    channel_name_template: "Provider 1 Event {slot}"
    channel_id_template: "provider1.event{slot}"
    group_title: "Provider 1 Events"
    event_group_filter: "Events"
    date_format: "auto"
    title_split: "first_dash"
```

Ersetze nur diese URL:

```text
http://DEINE-TELERISING-ADRESSE/api/provider/file/channels.m3u
```

Die URL muss vom TR-EventMux-Container erreichbar sein.

YAML achtet auf Einrückungen. Verwende Leerzeichen und keine Tabulatoren.

## 5. Container starten

Speichere `config.yaml` und führe aus:

```bash
docker compose up -d --build
```

Der erste Build kann einige Minuten dauern.

## 6. Prüfen, ob TR-EventMux läuft

```bash
docker compose ps
docker compose logs --tail=100 tr-eventmux
```

Öffne danach im Browser:

```text
http://192.168.1.10:8787/
```

Ersetze `192.168.1.10` wieder durch deine Server-IP.

Zusätzlich kannst du diese Adressen prüfen:

```text
http://192.168.1.10:8787/status.json
http://192.168.1.10:8787/playlist.m3u
http://192.168.1.10:8787/xmltv.xml
```

Wenn die Statusseite erscheint, läuft der Dienst.

## 7. Playlist in TVHeadend eintragen

Öffne in TVHeadend:

```text
Configuration -> DVB Inputs -> Networks
```

Lege ein neues `IPTV Automatic Network` an.

Als URL trägst du ein:

```text
http://192.168.1.10:8787/playlist.m3u
```

Öffne vor dem Speichern die Experteneinstellungen:

- Setze `Service-ID` auf `1`. Dadurch werden die Services direkt sichtbar und
  müssen nicht erst einzeln gescannt werden.
- Entferne den Haken bei `Scan nach Erstellen`. Der zusätzliche Scan ist bei
  dieser festen M3U normalerweise nicht erforderlich.

Speichere anschließend das Netzwerk. Die Services sollten kurz darauf unter
`Configuration -> DVB Inputs -> Services` erscheinen.

Mappe die erkannten Services anschließend unter:

```text
Configuration -> DVB Inputs -> Services
```

auf TVHeadend-Kanäle.

## 8. XMLTV-EPG verwenden

Die XMLTV-Adresse lautet:

```text
http://192.168.1.10:8787/xmltv.xml
```

Dieselbe Datei wird außerdem auf dem Docker-Rechner gespeichert:

```text
./data/xmltv.xml
```

M3U und XMLTV verwenden passende Channel-IDs. Ordne die XMLTV-Kanäle in
TVHeadend den zuvor gemappten IPTV-Kanälen zu.

## 9. Weitere Provider hinzufügen

Kopiere einen vorhandenen Block unter `sources:` und passe folgende Werte an:

```yaml
- key: "provider2"
  name: "Provider 2"
  source_m3u: "http://DEINE-ZWEITE-URL/channels.m3u"
  slots: 5
  channel_number_start: 911
  channel_name_template: "Provider 2 Event {slot}"
  channel_id_template: "provider2.event{slot}"
  group_title: "Provider 2 Events"
```

Beachte:

- `key` muss eindeutig sein.
- Kanalnummernbereiche dürfen sich nicht überschneiden.
- `channel_id_template` muss eindeutig sein.

Danach:

```bash
docker compose restart tr-eventmux
```

## Wenn etwas nicht funktioniert

### Die Webseite öffnet sich nicht

```bash
docker compose ps
docker compose logs --tail=200 tr-eventmux
```

Prüfe außerdem:

- Ist die Server-IP richtig?
- Ist Port `8787` erreichbar?
- Läuft der Container?

### In TVHeadend erscheinen keine Kanäle

Öffne die Playlist zuerst im Browser:

```text
http://SERVER-IP:8787/playlist.m3u
```

Wenn sie im Browser funktioniert, aber nicht in TVHeadend, ist meistens
`host_for_playlist` aus Sicht des TVHeadend-Containers nicht erreichbar.

### Es werden keine Events erkannt

Öffne:

```text
http://SERVER-IP:8787/status.json
```

Prüfe in `config.yaml`:

- `source_m3u`
- `event_group_filter`
- `date_format`

Zum Test kannst du alle Gruppen zulassen:

```yaml
event_group_filter: ""
```

### Konfiguration wurde geändert, aber nichts passiert

```bash
docker compose restart tr-eventmux
curl http://127.0.0.1:8787/refresh
```

### Neueste Version installieren

```bash
git pull --ff-only
docker compose up -d --build
```

## Wo geht es weiter?

- Vollständige Installation und Updates: [INSTALL.md](INSTALL.md)
- Alle Optionen und Endpunkte: [README.md](README.md)
- Sanitisiertes Konfigurationsbeispiel: [config.yaml.example](config.yaml.example)
