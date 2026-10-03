# Home-Assistant-Integration: Anzeige der N.MUSIC-Quelle (Handoff)

Dieses Dokument fasst den Stand aus einem Chat auf claude.ai zusammen, damit die Arbeit
im Repo (mdtv2-tools, TB-Fork) direkt weitergehen kann. Es beschreibt (1) was in Home
Assistant (HA) bereits gebaut ist, (2) was auf dem Pi noch fehlt und (3) die vereinbarte
Schnittstelle zwischen beiden.

## 1. Ziel

Auf der B&O-Masterlink-Quelle **N.MUSIC** spielt der Pi (MDT-HAT + HifiBerry-kompatibler
DAC) Audio über mehrere Provider ein. Die Software nimmt immer den Provider, der zuletzt
einen Stream aufgebaut hat:

- **sendspin** → Music Assistant (MA). In HA sichtbar als `media_player.masterlink_bridge_ma`.
- **airplay** → shairport-sync, z. B. vom iPhone.
- später ggf. weitere (z. B. MPD).

HA zeigt heute Quelle, Station/Playlist, Titel und Interpret an, **aber nur, solange MA
spielt**. Sobald ein anderer Provider übernimmt (z. B. AirPlay), weiß HA nichts mehr davon,
und die Anzeige verschwindet.

**Ziel:** Der Pi meldet HA bei jeder Änderung, welcher Provider aktiv ist und was er spielt
(Titel, Interpret, Album, wenn verfügbar). HA zeigt das in derselben Karte an, die auch das
spätere ESP32-Display (Guition ESP32-S3-4848S040, ESPHome/LVGL) nutzen wird.

## 2. Aufgabe im Repo (offen)

### 2.1 Datenquelle auf dem Pi klären

Laut README läuft alles über Redis pub/sub; nur `mdtv2-broker` spricht mit der MCU, eigene
Abonnenten sind ausdrücklich vorgesehen. `ml-source-bridge` kennt den aktiven Provider und
verarbeitet die shairport-sync-Metadaten (Titel/Interpret für das B&O-Display).

Zu prüfen:

- Publiziert `ml-source-bridge` Provider-Wechsel und Metadaten (Titel/Interpret/Album,
  Play-Start/-Ende) bereits auf einen Redis-Kanal? Wenn ja: auf welchen, mit welchem Format?
- Wenn nein: in der Bridge an den passenden Stellen ein `redis.publish(...)` ergänzen,
  z. B. Kanal `link:ml:nowplaying`, Payload wie in Abschnitt 3.

**Erledigt (Branch `feat/nowplaying`).** Vorher gab es keinen solchen Kanal; die Bridge hat
die Metadaten nur intern an das B&O-Display gegeben. Jetzt publiziert sie pro Quelle
(neues Modul `ml-source-bridge/core/nowplaying.py`, Details in `ml-source-bridge/README.md`):

- `HSET state:nowplaying <source_byte> <json>` – aktueller Stand (für den Start von `ha-notifier`)
- `PUBLISH link:ml:nowplaying <json>` – eine Nachricht pro Änderung

Der Payload entspricht Abschnitt 3, zusätzlich `source_byte` (z. B. `"0x7a"`), damit
`ha-notifier` nach Quelle filtern kann. Gepollt wird jede Sekunde, publiziert nur bei
Änderung. Kein Zeitstempel im Payload, damit sich Duplikate per Stringvergleich erkennen
lassen. Beim Beenden der Bridge geht für jede Quelle `idle` raus.

### 2.2 Neuer Dienst `ha-notifier`

Kleiner Python-Dienst, der den Redis-Kanal abonniert und bei jeder Änderung einen HTTP-POST
an einen HA-Webhook schickt.

Anforderungen:

- Konfiguration in `/etc/ml-source-bridge.toml`, eigener Abschnitt, z. B.:
  ```toml
  [ha_notifier]
  enabled    = true
  url        = "http://homeassistant.local:8123/api/webhook/<webhook_id>"
  timeout_s  = 3
  debounce_ms = 300
  ```
- Debounce bzw. Duplikate unterdrücken: identische Payloads nicht erneut senden; bei
  schnellen Folgeänderungen nur den letzten Stand senden.
- Robust gegen nicht erreichbares HA (Timeout, Fehler loggen, nicht abstürzen, beim nächsten
  Event erneut versuchen).
- **Play-Ende melden:** Wenn ein AirPlay-Stream endet (shairport „play end“) oder kein
  Provider mehr aktiv ist, `"state": "idle"` senden, damit die Anzeige verschwindet.
- Beim Start des Dienstes einmal den aktuellen Stand senden.
- systemd-Unit `ha-notifier.service`, analog zu den vorhandenen Units; Einbindung in
  `install.sh`.
- Abhängigkeiten minimal halten (`redis`, `requests` oder `urllib`).

**Erledigt (Branch `feat/nowplaying`):** `ha-notifier/` mit Unit und Einbindung in
`install.sh`, nur Standardbibliothek plus `redis`. Abweichungen und Ergänzungen zur Vorgabe:

- `sources = ["N.MUSIC"]` wählt die Quellen, die an den Webhook gehen (ohne Angabe: alle).
  Nötig, weil der Sensor in HA nur eine Quelle abbildet.
- `idle_debounce_ms` (Standard 3000): längere Wartezeit beim Verlassen von `playing`, damit
  das kurze `Paused` von shairport-sync beim Aufwecken des ML-Systems die Karte nicht
  flackern lässt.
- `keepalive_s` (Standard 300): Ein unveränderter Stand wird regelmäßig erneut gesendet,
  damit HA nach einem Neustart den aktuellen Stand hat. Bewusste Ausnahme von „identische
  Payloads nicht erneut senden“.
- `retry_s` (Standard 30): Ist HA nicht erreichbar, wird nicht erst beim nächsten Event,
  sondern nach dieser Zeit erneut gesendet.

### 2.3 Optional, zweiter Schritt: Cover bei AirPlay

**Erledigt (Branch `feat/nowplaying`):** shairport-sync meldet das Cover per MPRIS als
`mpris:artUrl` (Datei im `cover_art_cache_directory`). Die Bridge reicht das als `art_url`
weiter, `ha-notifier` liefert genau diese Dateien auf Port 8099 aus und schickt HA eine
`cover_url`. Der Browser lädt das Cover direkt vom Pi; HA wird per http im LAN geöffnet,
daher keine Image-Entity nötig.

shairport-sync kann Cover-Bilder liefern (binär). Idee: auf dem Pi als Datei ablegen,
per kleinem HTTP-Endpunkt bereitstellen und die URL im Payload als `cover_url` mitschicken.
Erst angehen, wenn 2.1/2.2 laufen.

## 3. Schnittstelle Pi → HA (vereinbart)

HTTP `POST` an `http://<ha>:8123/api/webhook/<webhook_id>`, `Content-Type: application/json`:

```json
{
  "source": "N.MUSIC",
  "provider": "airplay",
  "display": "Apple Music",
  "state": "playing",
  "title": "Theme from Harry's Game",
  "artist": "Clannad",
  "album": "Magical Ring"
}
```

| Feld       | Bedeutung                                                                 |
|------------|---------------------------------------------------------------------------|
| `source`   | B&O-Quelle (`display_name` aus der Bridge-Konfiguration), z. B. `N.MUSIC`  |
| `provider` | technischer Provider-Name: `sendspin`, `airplay`, …                        |
| `display`  | Anzeigename aus `[provider_displays]`, z. B. „Apple Music“                 |
| `state`    | `playing`, `paused` oder `idle`                                            |
| `title`    | Titel (leer, wenn unbekannt)                                              |
| `artist`   | Interpret (leer, wenn unbekannt)                                          |
| `album`    | Album (leer, wenn unbekannt)                                              |
| `cover_url`| optional, siehe 2.3                                                        |

Bei `provider = sendspin` darf der Pi ebenfalls senden; HA nimmt die Metadaten in diesem
Fall aber aus dem MA-Player (dort sind sie vollständiger).

## 4. HA-Seite

### 4.1 Bereits umgesetzt

- `media_player.masterlink_bridge_ma` – MA-Player für N.MUSIC (Sendspin).
  Relevante Attribute: `media_content_id` (Radio: `library://radio/<n>`, Tidal-Titel:
  `tidal--<instanz>://track/<id>`, Bibliothekstitel: `library://track/<id>`),
  `media_title`, `media_artist`, `media_album_name` (bei Radio = Sendername),
  `source` (= `Music Assistant Queue`, solange MA spielt), `entity_picture`.
- `input_select.radio_station_list` – Radiosender (MA-Favoriten), wird von einer
  Befüllungs-Automatisierung gefüllt; Default-Sender über `input_text.radio_default_station`.
- `input_text.n_music_inhalt` – Name der per Beo4 gestarteten Playlist; wird nach 2 min
  Nicht-Spielen bzw. bei Senderwechsel geleert.
- Skripte `script.radio_station_wahlen` (Sender wählen/neu starten) und
  `script.n_music_vor_zurueck` (bei Radio Sender vor/zurück, sonst Titel vor/zurück).
- Automatisierungen: Beo4-Ziffern → Radiosender, Beo4-Farbtasten → Playlists
  (Event `mlgw.ML_telegram`, `payload_type: BEO4_KEY`, `source: N.MUSIC`).
- Template-Sensoren (über die HA-Oberfläche angelegt):
  - `sensor.n_music_quelle` → `Internet Radio` / `Tidal` / `Music Assistant` / `Extern` / `–`
  - `sensor.n_music_inhalt` → Sendername bzw. Playlist-Name / `–`
- Dashboard: `custom:button-card` mit Cover als Hintergrund, Kopfzeile
  „N.MUSIC · Quelle · Inhalt“, Titel, Interpret, Buttons Zurück/Play-Pause/Weiter.

### 4.2 Erweiterung für Streams ohne MA

**Umgesetzt** in `home-assistant/` (Punkte 1–3; Einrichtung siehe
`home-assistant/README.md`). Abweichend vom Entwurf unten: Webhook-ID per `!secret`,
Karte bei AirPlay ohne Cover und ohne Buttons, `sensor.n_music_inhalt` zeigt dann das Album.
Eine pausierte AirPlay-Wiedergabe blendet die Karte aus, wie bisher bei MA.

Ursprünglicher Entwurf:

1. Trigger-Template-Sensor in `configuration.yaml` (die Oberfläche kann keine Trigger-Sensoren):
   ```yaml
   template:
     - trigger:
         - trigger: webhook
           webhook_id: mdt-nmusic-<langer-zufallswert>
           allowed_methods: [POST]
           local_only: true
       sensor:
         - name: MDT N.MUSIC
           unique_id: mdt_nmusic
           state: "{{ trigger.json.state | default('idle') }}"
           attributes:
             provider: "{{ trigger.json.provider | default('') }}"
             display: "{{ trigger.json.display | default('') }}"
             title: "{{ trigger.json.title | default('') }}"
             artist: "{{ trigger.json.artist | default('') }}"
             album: "{{ trigger.json.album | default('') }}"
   ```
2. `sensor.n_music_quelle` erweitern: Spielt laut `sensor.mdt_n_music` ein anderer Provider
   als `sendspin`, dessen `display`-Namen anzeigen (z. B. „AirPlay“ / „Apple Music“).
3. button-card: Titel/Interpret/Album je nach Quelle aus dem MA-Player oder aus den
   Attributen von `sensor.mdt_n_music`; Sichtbarkeit an `sensor.n_music_quelle != '–'`
   koppeln statt an den MA-Player.
4. Später: ESP32-Display liest dieselben Sensoren.

## 5. Offene Fragen (beantwortet)

- *Welche Redis-Kanäle nutzt die Bridge für Provider-Wechsel und Metadaten?* – Bisher keine.
  Neu: `state:nowplaying` / `link:ml:nowplaying`, siehe 2.1.
- *Liefert shairport-sync das Album?* – Ja, die Bridge liest `xesam:album` schon per D-Bus
  (Interface `org.gnome.ShairportSync.RemoteControl`, Property `Metadata`). Ob das iPhone es
  mitschickt, hängt von der App ab; fehlt es, bleibt `album` leer. Sendspin (MPRIS) und MPD
  (`Album:`) liefern es ebenfalls.
- *Wie wird ein Provider-Ende erkannt?* – Über MPRIS `PlaybackStatus` (AirPlay, Sendspin)
  bzw. MPD `state`. `Playing` → `playing`, `Paused` → `paused`, `Stopped` oder Player nicht
  erreichbar → `idle`. Bei AirPlay meldet shairport-sync `Stopped`, sobald der Client die
  Session beendet („play end“); Sendspin meldet seinen MPRIS-Namen ab, wenn kein Stream mehr
  aktiv ist. Bei mehreren Providern auf einer Quelle bleibt ein pausierter Provider die
  Quelle, bis ein anderer zu spielen beginnt.
- Noch auf dem Pi zu prüfen: wie lange shairport-sync nach einer Pause auf dem iPhone
  `Paused` meldet, bevor es auf `Stopped` geht.
