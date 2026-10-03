# Home-Assistant-Konfiguration für N.MUSIC (Stand 2026-10-03)

Sammlung aller aktuell gültigen HA-Bausteine rund um die B&O-Masterlink-Quelle **N.MUSIC**
(Music Assistant via Sendspin auf dem MDT-Pi, Beo4-Steuerung, Dashboard-Anzeige).
Verworfene Zwischenstände sind nicht enthalten. Siehe auch `HA_INTEGRATION.md` für die
geplante Erweiterung (Pi meldet AirPlay/andere Provider per Webhook an HA).

**Hinweis:** Fast alles wurde über die HA-Oberfläche angelegt (Helfer, Automatisierungen,
Skripte, Template-Sensoren). Der Code liegt in [`home-assistant/`](../home-assistant/)
(je Baustein eine Datei, maßgeblich); dieses Dokument erklärt Zweck und Zusammenspiel.
Platzhalter sind mit `<…>` markiert.

## Übersicht der Entities

| Entity | Typ | Zweck |
|---|---|---|
| `media_player.masterlink_bridge_ma` | MA-Player | Music Assistant → Pi (Sendspin) → Masterlink N.MUSIC |
| `media_player.hifiberry` | Media-Player | DAC auf dem Pi (aktuell nicht in Automatisierungen genutzt) |
| `input_select.radio_station_list` | Helfer (Auswahl) | Radiosender = MA-Favoriten, per Automatisierung befüllt |
| `input_text.radio_default_station` | Helfer (Text) | Default-Sender nach Neustart |
| `input_text.n_music_inhalt` | Helfer (Text, max. 255) | Name der per Beo4 gestarteten Playlist |
| `script.radio_station_wahlen` | Skript | Sender wählen bzw. neu starten, wenn schon gewählt |
| `script.n_music_vor_zurueck` | Skript | Vor/Zurück: bei Radio Senderwechsel, sonst Titelwechsel |
| `sensor.n_music_quelle` | Template-Sensor | Internet Radio / Tidal / Music Assistant / Extern / Provider-Name vom Pi (z. B. Apple Music) / – |
| `sensor.n_music_inhalt` | Template-Sensor | Sendername bzw. Playlist-Name, bei AirPlay & Co. Album / – |
| `sensor.mdt_n_music` | Trigger-Template-Sensor (Webhook) | Meldung vom Pi: Provider, Zustand, Titel/Interpret/Album |
| `automation.radio_liste_befuellen` | Automatisierung | `<tatsächliche ID>` – befüllt `input_select.radio_station_list` |

Beo4-Tastendrücke kommen als Event `mlgw.ML_telegram` mit
`payload_type: BEO4_KEY` und `payload.source` / `payload.command`
(z. B. `N.MUSIC` / `Digit-0`, `Yellow`).

### Relevante Attribute von `media_player.masterlink_bridge_ma`

| Attribut | Radio | Tidal-Track | Bibliotheks-Track (z. B. Playlist „Lieblingstitel“) |
|---|---|---|---|
| `media_content_id` | `library://radio/<n>` | `tidal--<instanz>://track/<id>` | `library://track/<id>` |
| `media_title` | aktueller Song (ICY) | Titel | Titel |
| `media_artist` | aktueller Interpret | Interpret | Interpret |
| `media_album_name` | **Sendername** | Album | Album |
| `source` | `Music Assistant Queue` | `Music Assistant Queue` | `Music Assistant Queue` |
| `app_id` | `music_assistant` | `music_assistant` | `music_assistant` |

Den Namen einer gestarteten Playlist liefert MA nicht, deshalb `input_text.n_music_inhalt`.

---

## 1. Automatisierungen

### 1.1 Masterlink Bridge Default-Lautstärke

→ [`home-assistant/automations/masterlink_default_lautstaerke.yaml`](../home-assistant/automations/masterlink_default_lautstaerke.yaml)

### 1.2 Befüllung der Senderliste – Ergänzung Default-Sender

Die Automatisierung selbst ist hier nicht dokumentiert (`<bestehender Code>`). Direkt nach
dem vorhandenen `input_select.set_options` steht:

→ [`home-assistant/automations/radio_liste_befuellen_default_snippet.yaml`](../home-assistant/automations/radio_liste_befuellen_default_snippet.yaml)

### 1.3 N.MUSIC change Radio Station

Spielt den gewählten Sender ab, aber nur bei echten Auswahländerungen (nicht beim
HA-Neustart, nicht bei Attributänderungen, nicht während die Liste befüllt wird).

→ [`home-assistant/automations/n_music_change_radio_station.yaml`](../home-assistant/automations/n_music_change_radio_station.yaml)

### 1.4 N.MUSIC Radio per Beo4-Ziffer

Ersetzt die früheren Einzel-Automatisierungen pro Sender.

→ [`home-assistant/automations/n_music_radio_beo4_ziffer.yaml`](../home-assistant/automations/n_music_radio_beo4_ziffer.yaml)

### 1.5 N.MUSIC Tidal-Playlist per Beo4-Farbtaste

Ersetzt die früheren Einzel-Automatisierungen pro Playlist.

→ [`home-assistant/automations/n_music_tidal_beo4_farbtaste.yaml`](../home-assistant/automations/n_music_tidal_beo4_farbtaste.yaml)

### 1.6 N.MUSIC Playlist-Info zurücksetzen

→ [`home-assistant/automations/n_music_playlist_info_reset.yaml`](../home-assistant/automations/n_music_playlist_info_reset.yaml)

---

## 2. Skripte

### 2.1 Radio Station wählen (`script.radio_station_wahlen`)

Ist der Sender schon gewählt → direkt abspielen. Sonst nur die Auswahl ändern; das
Abspielen übernimmt 1.3. Dadurch nie doppelter Start.

→ [`home-assistant/scripts/radio_station_wahlen.yaml`](../home-assistant/scripts/radio_station_wahlen.yaml)

### 2.2 N.MUSIC Vor/Zurück (`script.n_music_vor_zurueck`)

→ [`home-assistant/scripts/n_music_vor_zurueck.yaml`](../home-assistant/scripts/n_music_vor_zurueck.yaml)

---

## 3. Template-Sensoren

Über *Helfer → Template → Template-Sensor* angelegt (ohne Einheit, Geräte- und
Zustandsklasse).

Beide Sensoren prüfen zuerst `sensor.mdt_n_music`: Spielt laut Pi ein anderer Provider als
Sendspin (z. B. AirPlay direkt vom iPhone), zeigen sie dessen Anzeigenamen bzw. das Album.
Sonst gilt die bisherige Logik auf Basis des MA-Players.

### 3.1 N.MUSIC Quelle (`sensor.n_music_quelle`)

→ [`home-assistant/templates/n_music_quelle.jinja`](../home-assistant/templates/n_music_quelle.jinja)

### 3.2 N.MUSIC Inhalt (`sensor.n_music_inhalt`)

→ [`home-assistant/templates/n_music_inhalt.jinja`](../home-assistant/templates/n_music_inhalt.jinja)

---

### 3.3 MDT N.MUSIC (`sensor.mdt_n_music`, Webhook)

Trigger-Template-Sensor in `configuration.yaml` (die Oberfläche kann keine
Trigger-Sensoren). Wird von `ha-notifier` auf dem Pi bei jeder Änderung per Webhook
gesetzt; Zustand `playing` / `paused` / `idle`, Attribute `provider`, `display`, `title`,
`artist`, `album`. Die Webhook-ID steht in `secrets.yaml` (`mdt_nmusic_webhook`).

→ [`home-assistant/templates/mdt_n_music_webhook.yaml`](../home-assistant/templates/mdt_n_music_webhook.yaml)

## 4. Dashboard-Karte (`custom:button-card`, via HACS)

Cover als Hintergrund, Kopfzeile „N.MUSIC · Quelle · Inhalt“, Titel, Interpret,
Buttons Zurück / Play-Pause / Weiter (Vor/Zurück über `script.n_music_vor_zurueck`).

Sichtbar, solange `sensor.n_music_quelle` nicht `–` ist – also auch bei AirPlay & Co.
In dem Fall kommen Titel und Interpret aus `sensor.mdt_n_music`, statt des Covers gibt es
den Farbverlauf, und die Buttons sind ausgeblendet (HA kann diese Provider nicht steuern).

→ [`home-assistant/dashboard/n_music_button_card.yaml`](../home-assistant/dashboard/n_music_button_card.yaml)

---

## 5. Anbindung des Pi (siehe `HA_INTEGRATION.md`)

Umgesetzt: Der Pi meldet per Webhook, welcher Provider auf N.MUSIC spielt
(`sensor.mdt_n_music`, 3.3); Quelle, Inhalt und Karte berücksichtigen das (3.1, 3.2, 4).

Offen:

1. Vor/Zurück und Play/Pause bei AirPlay: Die Bridge könnte shairport-sync steuern, es gibt
   aber noch keinen Weg von HA dorthin. Bis dahin sind die Buttons in dem Fall ausgeblendet.
2. Cover bei AirPlay (`HA_INTEGRATION.md` 2.3).
3. ESP32-Display (Guition ESP32-S3-4848S040, ESPHome/LVGL) liest dieselben Sensoren.
